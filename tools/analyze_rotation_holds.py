"""Offline T63/T66 matched-hold analysis; never opens a process or changes input.

CSV v1 (UTF-8, one atomic native receipt per invocation, three contiguous blocks
labelled pitch, yaw, both; order arbitrary). Required columns are REQUIRED_COLUMNS:

* schema_version=1; evidence_kind=native_receipt or synthetic. The latter must be
  used for generated fixtures. Neither label authenticates a capture.
* file_version, steam_buildid, exe_sha256 identify the exact T63 build below.
  session_id, process_id, ship_id, body_id, ship_class, context_id and
  engine_fingerprint must be nonempty and identical in all three holds. IDs are
  opaque exporter identities; engine_fingerprint must hash *effective* engine,
  selected controls, assists and relevant tuning, not merely a class name.
* tick_ms is a nondecreasing native GetTickCount64 millisecond clock; adjacent
  frames may share a millisecond. invocation is a nonnegative native ordinal
  advancing by exactly one inside a hold. time_s is finite, strictly increasing
  high-resolution monotonic time in seconds.
  Gaps between different holds are allowed; a hold cannot recur in a later block.
* receipt_invocation=invocation; admitted=qualified=fresh=applied=1 attest that
  all input, targets, observed rates and clamp data belong to that invocation.
  Missing, rejected and stale rows are errors, never silently filtered out.
* input_pitch/yaw/roll are independent normalized commands before radial shaping.
  Singles must be (+/-1,0,0) and (0,+/-1,0), and both must have matching signs.
  Both (+/-1,+/-1,0) tests full independent authority. A unit-radius stick at
  45 degrees (+/-sqrt(.5),+/-sqrt(.5),0) is explicitly a different test.
* target_pitch/yaw/roll are the native low XMM6/7/13 lanes at RVA 0x175E4F2,
  in their PRE-sign-flip convention. rate_pitch/yaw/roll are same-invocation
  native local right/up/forward rates, in the corresponding native angular
  units. Native rate feedback uses -target, so observed signs can oppose targets.
* gain_pitch/yaw/roll are effective native response gains. They must be finite,
  positive and matched across the capture (1e-6 relative tolerance).
* Supply either clamp_x/y/z, instantaneous 0/1 world-component MaxTorque
  occupancy, OR max_torque plus torque_before_x/y/z and torque_after_x/y/z.
  The latter are the actual native world XYZ immediately before/after its box
  clamp; after must equal clamp(before,-max_torque,+max_torque). Both forms may
  be supplied if consistent. MaxTorque must be constant across matched holds.

The final window_seconds of each hold is compared, after settle_seconds. No
interpolation or gap filling: right-endpoint samples are weighted by their
monotonic interval overlap, giving exactly matched duration. Targets and rates
must have a stable range in that window; active singleton responses cannot be
zero. Thresholds are explicit analysis choices, not native-engine properties.

Output is one compact JSON object. Signed singleton means provide preliminary
calibration only; comparisons use magnitudes with separate sign consistency.
Normalized combined norm = hypot(both_pitch/single_pitch,both_yaw/single_yaw)
using magnitudes, so it is sqrt(2) for independent axes, even if axis scales
differ. Also report raw diagonal/single ratios. Roll, cross-axis responses or
any clamp occupancy prevent an independent-axes-consistent verdict. This is
evidence about supplied stable windows, never production authority, admission
qualification, unrestricted additivity, or proof of later solver behaviour.

Source: research/re-log/2026-10-16_codex_rotation-additive.md and .json,
FileVersion 180836 / Steam 25732212. No live matched holds existed in that lab.
Usage: python3.11 -B tools/analyze_rotation_holds.py receipts.csv
Invalid data emits compact failure JSON and exit code 2. --help describes options.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import sys
from typing import Iterable, Mapping


FILE_VERSION = "180836"
STEAM_BUILDID = "25732212"
EXE_SHA256 = "13d5060d4efb9d2a6a6b1b349bc4257231056cc2a055df4bb15d816262cc3499"
AXES = ("pitch", "yaw", "roll")
WORLD_AXES = ("x", "y", "z")
HOLDS = ("pitch", "yaw", "both")
IDENTITY_COLUMNS = (
    "schema_version", "evidence_kind", "file_version", "steam_buildid",
    "exe_sha256", "session_id", "process_id", "ship_id", "body_id",
    "ship_class", "context_id", "engine_fingerprint",
)
REQUIRED_COLUMNS = IDENTITY_COLUMNS + (
    "hold", "time_s", "tick_ms", "invocation", "receipt_invocation",
    "admitted", "qualified", "fresh", "applied",
) + tuple(f"{prefix}_{axis}" for prefix in ("input", "target", "rate", "gain") for axis in AXES)
CLAMP_COLUMNS = tuple(f"clamp_{axis}" for axis in WORLD_AXES)
TORQUE_COLUMNS = ("max_torque",) + tuple(
    f"torque_{side}_{axis}" for side in ("before", "after") for axis in WORLD_AXES
)
ALLOWED_COLUMNS = set(REQUIRED_COLUMNS + CLAMP_COLUMNS + TORQUE_COLUMNS)
LIMITS = (
    "Offline analysis cannot authenticate exporter identities, freshness or admission claims.",
    "Singleton target calibration is preliminary measured data, not production authority.",
    "Only the supplied matched context/windows are compared; no other mode or class is qualified.",
    "World-component torque limits and downstream solver/external effects still apply.",
    "Synthetic rows do not constitute live proof.",
)


class AnalysisError(ValueError):
    """A rejected capture, with a stable machine-readable reason code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AnalysisConfig:
    window_seconds: float = 2.0
    settle_seconds: float = 0.5
    min_samples: int = 30
    max_gap_seconds: float = 0.1
    stable_relative_span: float = 0.05
    additive_relative_tolerance: float = 0.05
    roll_relative_limit: float = 0.02
    minimum_axis_magnitude: float = 1e-6


def _fail(code: str, detail: str) -> None:
    raise AnalysisError(code, detail)


def _number(value: object, column: str, line: int) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail("invalid_number", f"row {line}: {column} must be finite numeric data")
    if not math.isfinite(result):
        _fail("nonfinite", f"row {line}: {column} is not finite")
    return result


def _integer(value: object, column: str, line: int) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        _fail("invalid_integer", f"row {line}: {column} must be a nonnegative integer")
    return int(str(value))


def _validate_config(config: AnalysisConfig) -> None:
    for name, value in asdict(config).items():
        if isinstance(value, bool) or not math.isfinite(value):
            _fail("invalid_config", f"{name} must be finite")
        if value < 0 or (value == 0 and name != "settle_seconds"):
            _fail("invalid_config", f"{name} must be positive (settle_seconds may be zero)")
    if not isinstance(config.min_samples, int) or config.min_samples < 3:
        _fail("invalid_config", "min_samples must be an integer of at least three")
    if config.stable_relative_span >= 1 or config.additive_relative_tolerance >= 1:
        _fail("invalid_config", "relative stability/additivity tolerances must be below one")
    if config.roll_relative_limit >= 1:
        _fail("invalid_config", "roll_relative_limit must be below one")


def _validate_columns(columns: Iterable[str]) -> tuple[bool, bool]:
    columns = list(columns)
    if len(columns) != len(set(columns)):
        _fail("duplicate_columns", "CSV column names must be unique")
    missing = set(REQUIRED_COLUMNS).difference(columns)
    if missing:
        _fail("missing_columns", "missing: " + ",".join(sorted(missing)))
    unknown = set(columns).difference(ALLOWED_COLUMNS)
    if unknown:
        _fail("unknown_columns", "unknown: " + ",".join(sorted(unknown)))
    clamp_count = len(set(columns).intersection(CLAMP_COLUMNS))
    torque_count = len(set(columns).intersection(TORQUE_COLUMNS))
    if clamp_count not in (0, len(CLAMP_COLUMNS)) or torque_count not in (0, len(TORQUE_COLUMNS)):
        _fail("incomplete_torque", "supply each chosen clamp/torque column group in full")
    if not clamp_count and not torque_count:
        _fail("missing_torque", "need clamp_x/y/z or max_torque and before/after XYZ")
    return bool(clamp_count), bool(torque_count)


def load_csv(path: str | Path) -> list[dict[str, str]]:
    """Read strict CSV; analysis is separate and never silently drops any rows."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None:
            _fail("empty_capture", "CSV has no header")
        _validate_columns(reader.fieldnames)
        rows = []
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                _fail("row_width", f"row {line}: wrong number of CSV fields")
            rows.append(row)
        return rows


def _parse_rows(rows: list[Mapping[str, object]], config: AnalysisConfig) -> list[dict]:
    if not rows:
        _fail("empty_capture", "capture contains no receipt rows")
    use_flags, use_torque = _validate_columns(rows[0].keys())
    columns = set(rows[0])
    baseline = None
    parsed = []
    seen_holds = set()
    for line, raw in enumerate(rows, 2):
        if set(raw) != columns:
            _fail("row_schema", f"row {line}: all rows must have the same columns")
        row = {}
        for column in IDENTITY_COLUMNS:
            value = raw[column]
            if value is None or not str(value).strip():
                _fail("missing_identity", f"row {line}: empty {column}")
            row[column] = str(value)
        if row["schema_version"] != "1":
            _fail("schema_version", f"row {line}: expected schema_version=1")
        if row["evidence_kind"] not in ("native_receipt", "synthetic"):
            _fail("evidence_kind", f"row {line}: use native_receipt or synthetic")
        if (row["file_version"], row["steam_buildid"], row["exe_sha256"]) != (
            FILE_VERSION, STEAM_BUILDID, EXE_SHA256
        ):
            _fail("wrong_build", f"row {line}: not the exact T63 executable identity")
        if not re.fullmatch(r"[0-9a-f]{64}", row["engine_fingerprint"]):
            _fail("engine_fingerprint", f"row {line}: effective engine fingerprint must be lowercase SHA256")
        if baseline is None:
            baseline = {key: row[key] for key in IDENTITY_COLUMNS}
        for key, value in baseline.items():
            if row[key] != value:
                _fail("identity_changed", f"row {line}: changed {key}; holds are not matched")
        row["hold"] = str(raw["hold"])
        if row["hold"] not in HOLDS:
            _fail("invalid_hold", f"row {line}: hold must be pitch, yaw or both")
        row["time_s"] = _number(raw["time_s"], "time_s", line)
        for column in ("tick_ms", "invocation", "receipt_invocation"):
            row[column] = _integer(raw[column], column, line)
        if row["receipt_invocation"] != row["invocation"]:
            _fail("invocation_mismatch", f"row {line}: receipt belongs to another invocation")
        for column in ("admitted", "qualified", "fresh", "applied"):
            if str(raw[column]) != "1":
                _fail("unadmitted_receipt", f"row {line}: {column} must be 1")
        for prefix in ("input", "target", "rate", "gain"):
            for axis in AXES:
                column = f"{prefix}_{axis}"
                row[column] = _number(raw[column], column, line)
                if prefix == "gain" and row[column] <= 0:
                    _fail("invalid_gain", f"row {line}: {column} must be positive")
                if prefix == "input" and abs(row[column]) > 1 + 1e-6:
                    _fail("invalid_input", f"row {line}: normalized {column} outside [-1,1]")
        occupancy = {}
        if use_flags:
            for axis in WORLD_AXES:
                value = str(raw[f"clamp_{axis}"])
                if value not in ("0", "1"):
                    _fail("invalid_clamp", f"row {line}: clamp_{axis} must be instantaneous 0/1")
                occupancy[axis] = int(value)
        if use_torque:
            limit = _number(raw["max_torque"], "max_torque", line)
            if limit <= 0:
                _fail("invalid_torque", f"row {line}: max_torque must be positive")
            row["max_torque"] = limit
            for axis in WORLD_AXES:
                before = _number(raw[f"torque_before_{axis}"], f"torque_before_{axis}", line)
                after = _number(raw[f"torque_after_{axis}"], f"torque_after_{axis}", line)
                expected = min(limit, max(-limit, before))
                if not math.isclose(after, expected, rel_tol=1e-5, abs_tol=1e-6):
                    _fail("clamp_mismatch", f"row {line}: after_{axis} does not match native box clamp")
                occupied = int(abs(before) >= limit * (1 - 1e-6))
                if use_flags and occupancy[axis] != occupied:
                    _fail("clamp_mismatch", f"row {line}: clamp_{axis} disagrees with before/after")
                occupancy[axis] = occupied
        row["occupancy"] = occupancy
        if parsed:
            previous = parsed[-1]
            gap = row["time_s"] - previous["time_s"]
            if gap <= 0 or row["tick_ms"] < previous["tick_ms"] or row["invocation"] <= previous["invocation"]:
                _fail("nonmonotonic", f"row {line}: time/invocation must increase; tick_ms cannot decrease")
            if row["hold"] == previous["hold"]:
                if row["invocation"] != previous["invocation"] + 1:
                    _fail("receipt_gap", f"row {line}: missing native invocation inside hold")
                if gap > config.max_gap_seconds + 1e-9:
                    _fail("time_gap", f"row {line}: interval exceeds max_gap_seconds")
            elif row["hold"] in seen_holds:
                _fail("repeated_hold", f"row {line}: each hold must be a single contiguous block")
            for column in ("gain_pitch", "gain_yaw", "gain_roll") + (("max_torque",) if use_torque else ()):
                if not math.isclose(row[column], parsed[0][column], rel_tol=1e-6, abs_tol=1e-9):
                    _fail("engine_changed", f"row {line}: changed effective {column}")
        seen_holds.add(row["hold"])
        parsed.append(row)
    if seen_holds != set(HOLDS):
        _fail("missing_holds", "need exactly one block each of pitch, yaw and both")
    return parsed


def _window(rows: list[dict], config: AnalysisConfig) -> tuple[list[dict], list[float], dict]:
    end = rows[-1]["time_s"]
    start = end - config.window_seconds
    if start < rows[0]["time_s"] + config.settle_seconds - 1e-9:
        _fail("inadequate_window", f"{rows[0]['hold']}: need settling time plus full matched window")
    selected, weights = [], []
    for previous, current in zip(rows, rows[1:]):
        overlap = min(end, current["time_s"]) - max(start, previous["time_s"])
        if overlap > 1e-10:
            selected.append(current)
            weights.append(overlap)
    if len(selected) < config.min_samples:
        _fail("inadequate_samples", f"{rows[0]['hold']}: too few native receipts in stable window")
    if not math.isclose(math.fsum(weights), config.window_seconds, abs_tol=1e-8, rel_tol=1e-8):
        _fail("inadequate_window", f"{rows[0]['hold']}: incomplete matched window")
    means = {
        f"{prefix}_{axis}": math.fsum(r[f"{prefix}_{axis}"] * (w / config.window_seconds) for r, w in zip(selected, weights))
        for prefix in ("input", "target", "rate") for axis in AXES
    }
    occupancy = {axis: math.fsum(r["occupancy"][axis] * w for r, w in zip(selected, weights)) / config.window_seconds for axis in WORLD_AXES}
    return selected, weights, {
        "start_s": start, "end_s": end, "duration_s": config.window_seconds,
        "samples": len(selected), "means": means, "clamp_occupancy": occupancy,
    }


def _input_kind(blocks: dict[str, list[dict]]) -> str:
    singles = {}
    for hold, axis in (("pitch", "pitch"), ("yaw", "yaw")):
        active = blocks[hold][0][f"input_{axis}"]
        if not math.isclose(abs(active), 1, abs_tol=1e-6):
            _fail("input_not_full", f"{hold}: singleton input must be full scale")
        singles[axis] = active
        for row in blocks[hold]:
            for check_axis in AXES:
                expected = active if check_axis == axis else 0
                if not math.isclose(row[f"input_{check_axis}"], expected, abs_tol=1e-6):
                    _fail("mixed_input", f"{hold}: command changed or another axis is nonzero")
    first = blocks["both"][0]
    magnitudes = [abs(first[f"input_{axis}"]) for axis in ("pitch", "yaw")]
    if all(math.isclose(value, 1, abs_tol=1e-6) for value in magnitudes):
        kind = "full_independent_1_1"
    elif all(math.isclose(value, math.sqrt(0.5), abs_tol=1e-6) for value in magnitudes):
        kind = "unit_radius_45_degrees"
    else:
        _fail("unsupported_diagonal", "both must be full independent (1,1) or unit-radius 45 degrees")
    for row in blocks["both"]:
        if abs(row["input_roll"]) > 1e-6:
            _fail("mixed_input", "both: commanded roll must be zero")
        for axis in ("pitch", "yaw"):
            value = row[f"input_{axis}"]
            if not math.isclose(value, first[f"input_{axis}"], abs_tol=1e-6) or value * singles[axis] <= 0:
                _fail("mixed_input", "both: inputs must be constant and match singleton signs")
    return kind


def _analyze_holds(rows: Iterable[Mapping[str, object]], config: AnalysisConfig | None = None) -> dict:
    config = config or AnalysisConfig()
    _validate_config(config)
    parsed = _parse_rows(list(rows), config)
    blocks = {hold: [r for r in parsed if r["hold"] == hold] for hold in HOLDS}
    kind = _input_kind(blocks)
    windows, selected = {}, {}
    for hold in HOLDS:
        selected[hold], _, windows[hold] = _window(blocks[hold], config)
    comparison = {}
    warnings = []
    for prefix in ("target", "rate"):
        singles = {axis: windows[axis]["means"][f"{prefix}_{axis}"] for axis in ("pitch", "yaw")}
        if any(abs(value) < config.minimum_axis_magnitude for value in singles.values()):
            _fail("zero_single_axis", f"{prefix}: singleton response too small for a ratio")
        for hold in HOLDS:
            for axis in AXES:
                values = [r[f"{prefix}_{axis}"] for r in selected[hold]]
                scale = abs(singles[axis]) if axis != "roll" else min(abs(v) for v in singles.values())
                if max(values) - min(values) > config.stable_relative_span * scale + config.minimum_axis_magnitude:
                    _fail("unstable_window", f"{hold}: {prefix}_{axis} is not stable")
        combined = {axis: windows["both"]["means"][f"{prefix}_{axis}"] for axis in ("pitch", "yaw")}
        retention = {axis: abs(combined[axis]) / abs(singles[axis]) for axis in ("pitch", "yaw")}
        signs = {axis: combined[axis] * singles[axis] > 0 for axis in ("pitch", "yaw")}
        norm = math.hypot(*combined.values())
        comparison[prefix] = {
            "single_pitch": singles["pitch"], "single_yaw": singles["yaw"],
            "both_pitch": combined["pitch"], "both_yaw": combined["yaw"],
            "per_axis_retention": retention, "sign_consistent": signs,
            "combined_norm": norm,
            "combined_over_pitch_single": norm / abs(singles["pitch"]),
            "combined_over_yaw_single": norm / abs(singles["yaw"]),
            "combined_over_expected_additive_norm": norm / math.hypot(*singles.values()),
            "normalized_combined_norm": math.hypot(*retention.values()),
        }
        for hold in HOLDS:
            scale = min(abs(value) for value in singles.values())
            if any(abs(r[f"{prefix}_roll"]) > config.roll_relative_limit * scale for r in selected[hold]):
                warnings.append(f"roll_contamination:{prefix}:{hold}")
        for hold, other in (("pitch", "yaw"), ("yaw", "pitch")):
            if any(abs(r[f"{prefix}_{other}"]) > config.roll_relative_limit * abs(singles[other]) for r in selected[hold]):
                warnings.append(f"cross_axis_response:{prefix}:{hold}")
        if not all(signs.values()):
            warnings.append(f"sign_inconsistent:{prefix}")
    for hold in HOLDS:
        if any(value > 0 for value in windows[hold]["clamp_occupancy"].values()):
            warnings.append(f"torque_saturation:{hold}")
    feedback_signs = {}
    for axis in ("pitch", "yaw"):
        feedback_signs[axis] = comparison["target"][f"single_{axis}"] * comparison["rate"][f"single_{axis}"] < 0
    if not all(feedback_signs.values()):
        warnings.append("native_feedback_sign_mismatch")
    if kind != "full_independent_1_1":
        warnings.append("unit_radius_diagonal_does_not_test_full_independent_inputs")
    consistent = kind == "full_independent_1_1" and not warnings and all(
        abs(value - 1) <= config.additive_relative_tolerance
        for prefix in ("target", "rate")
        for value in comparison[prefix]["per_axis_retention"].values()
    )
    result = {
        "ok": True, "schema_version": 1, "analysis_scope": "offline_supplied_rows",
        "evidence_kind": parsed[0]["evidence_kind"],
        "identity": {key: parsed[0][key] for key in IDENTITY_COLUMNS if key not in ("schema_version", "evidence_kind")},
        "config": asdict(config), "input_kind": kind, "rows": len(parsed),
        "windows": windows, "comparison": comparison,
        "native_feedback_sign_consistent": feedback_signs,
        "matched_independent_axes_consistent": consistent,
        "unrestricted_additive_success": False,
        "live_qualification": False, "production_authority": False,
        "preliminary_calibration": {
            "target_convention": "pre_sign_flip_at_0x175E4F2",
            "signed_full_pitch_target": comparison["target"]["single_pitch"],
            "signed_full_yaw_target": comparison["target"]["single_yaw"],
            "production_authority": False,
        },
        "warnings": warnings, "limits": list(LIMITS),
    }
    # A finite input can overflow a weighted sum/ratio. Never emit NaN/Infinity.
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, OverflowError):
        _fail("numeric_overflow", "finite input values overflowed the analysis")
    return result


def analyze_holds(rows: Iterable[Mapping[str, object]], config: AnalysisConfig | None = None) -> dict:
    """Analyze supplied receipts, raising AnalysisError on rejected evidence."""
    try:
        return _analyze_holds(rows, config)
    except OverflowError as exc:
        raise AnalysisError("numeric_overflow", "finite input values overflowed the analysis") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--window-seconds", type=float, default=2.0)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--max-gap-seconds", type=float, default=0.1)
    args = parser.parse_args(argv)
    try:
        result = analyze_holds(load_csv(args.csv_path), AnalysisConfig(
            window_seconds=args.window_seconds, settle_seconds=args.settle_seconds,
            min_samples=args.min_samples, max_gap_seconds=args.max_gap_seconds,
        ))
    except (AnalysisError, OSError, csv.Error, UnicodeError, OverflowError) as exc:
        result = {"ok": False, "code": getattr(exc, "code", "read_error"), "detail": str(exc),
                  "analysis_scope": "offline_supplied_rows", "live_qualification": False,
                  "production_authority": False, "unrestricted_additive_success": False}
        print(json.dumps(result, separators=(",", ":"), allow_nan=False))
        return 2
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
