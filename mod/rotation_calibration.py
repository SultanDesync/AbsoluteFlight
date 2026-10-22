"""Pure T66 receipt export and bounded, fixture-pinned validation calibration.

No process, input-bus, pipe, settings, or game APIs are used. Export identities
are supplied claims, not authenticated provenance. Even a native-receipt result
is preliminary evidence for a bounded validation run, never release authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import sys
from typing import Iterable, Mapping


AXES = ("pitch", "yaw", "roll")
HOLDS = ("pitch", "yaw", "both")
IDENTITY_FIELDS = ("file_version", "steam_buildid", "exe_sha256", "session_id",
                   "process_id", "ship_id", "body_id", "ship_class", "context_id")
PIN_FIELDS = tuple(key for key in IDENTITY_FIELDS if key != "session_id") + ("engine_fingerprint",)
ENGINE_SNAPSHOT_BYTES = 0x78
MAX_FIRST_INPUT = .25
NATIVE_CONVENTION = "pre_sign_flip_at_0x175E4F2"


class CalibrationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _analyzer():
    """Support release's mod/tools search paths and repository package imports."""
    # Full repository users may explicitly import the namespace while release
    # imports tools directly. Reuse that established namespace identity so its
    # documented AnalysisError type remains catchable if both names are loaded.
    namespaced = sys.modules.get("tools.analyze_rotation_holds")
    if namespaced is not None:
        return namespaced
    try:
        import analyze_rotation_holds
        return analyze_rotation_holds
    except ModuleNotFoundError as error:
        if error.name != "analyze_rotation_holds":
            raise
        from tools import analyze_rotation_holds
        return analyze_rotation_holds


def _fail(code, detail):
    raise CalibrationError(code, detail)


def _number(value, name):
    if isinstance(value, bool):
        _fail("invalid_number", f"{name}: booleans are not numeric evidence")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        _fail("invalid_number", f"{name}: finite numeric data required")
    if not math.isfinite(value):
        _fail("nonfinite", f"{name}: finite numeric data required")
    return value


def _integer(value, name):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        _fail("invalid_integer", f"{name}: nonnegative integer required")
    return int(value)


def _flag(value, name):
    if value is True or str(value) == "1":
        return 1
    if value is False or str(value) == "0":
        return 0
    _fail("invalid_flag", f"{name}: instantaneous 0/1 required")


def _text(value, name):
    if value is None or not str(value).strip():
        _fail("missing_identity", f"{name}: nonempty identity required")
    return str(value)


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        _fail("invalid_json", "Only finite JSON evidence is supported")
    return hashlib.sha256(encoded).hexdigest()


def _hex_digest(value, name):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        _fail("invalid_digest", f"{name}: lowercase SHA256 required")
    return value


def _engine_context(row, metadata=None):
    if not isinstance(row, Mapping) or (metadata is not None and not isinstance(metadata, Mapping)):
        _fail("invalid_identity", "Native receipt and metadata must be mappings")
    metadata = metadata or {}
    raw = row.get("engine_snapshot_hex")
    if (not isinstance(raw, str) or len(raw) != 2 * ENGINE_SNAPSHOT_BYTES
            or not re.fullmatch(r"[0-9a-fA-F]+", raw)):
        _fail("engine_snapshot", "Expected exact ship+0x6630, 0x78-byte engine snapshot")
    selected = row.get("selected_control")
    if selected is None or selected == "" or selected == {} or selected == 0 or isinstance(selected, bool):
        _fail("selected_control", "Actual selected-control identity is required")
    # Deep-copy and reject non-JSON/NaN identities. Gains intentionally do not
    # enter this hash: native effective gains may vary with command magnitude.
    _digest(selected)
    selected = json.loads(json.dumps(selected, allow_nan=False))
    limit = _number(row.get("max_torque"), "max_torque")
    if limit <= 0:
        _fail("invalid_torque", "max_torque must be positive")
    result = {"engine_snapshot_hex": raw.lower(), "selected_control": selected,
              "max_torque": limit, "freelook": _flag(row.get("freelook"), "freelook"),
              "state": _text(row.get("state"), "state"),
              "context": _text(row.get("context"), "context")}
    for key in ("settings_fingerprint", "source_fingerprint"):
        if key in metadata:
            result[key] = _hex_digest(metadata[key], key)
    if "mode_pins" in metadata:
        pins = metadata["mode_pins"]
        if not isinstance(pins, Mapping) or not pins:
            _fail("mode_pins", "Actual nonempty native mode/source pins mapping required")
        _digest(pins)
        result["mode_pins"] = json.loads(json.dumps(pins, allow_nan=False))
        if "mode_pins" in row and row["mode_pins"] != pins:
            _fail("mode_pins", "Native receipt mode/source pins differ from pinned metadata")
    return result


def engine_fingerprint(row: Mapping, metadata: Mapping | None = None) -> str:
    """Hash actual engine bytes, selected controls, torque and immutable context.

    Optional settings/source fingerprints must be supplied on both sides of a
    validation comparison. Effective gains remain separately measured diagnostics.
    """
    return _digest(_engine_context(row, metadata))


def capture_identity(metadata: Mapping, row: Mapping) -> dict:
    """Build the exact identity accepted by validate_calibration from one fixture."""
    if not isinstance(metadata, Mapping) or not isinstance(row, Mapping):
        _fail("invalid_identity", "Metadata and native fixture mappings required")
    identity = {key: _text(metadata.get(key), key) for key in IDENTITY_FIELDS}
    _hex_digest(identity["exe_sha256"], "exe_sha256")
    for raw_key, key in (("ship", "ship_id"), ("body", "body_id"), ("context", "context_id")):
        if _text(row.get(raw_key), raw_key) != identity[key]:
            _fail("identity_mismatch", f"Native {raw_key} differs from supplied {key}")
    if "ship_class" in row and str(row["ship_class"]) != identity["ship_class"]:
        _fail("identity_mismatch", "Native class differs from supplied ship_class")
    identity["engine_fingerprint"] = engine_fingerprint(row, metadata)
    return identity


def _hold_intervals(labels):
    if not isinstance(labels, (list, tuple)) or len(labels) != 3:
        _fail("invalid_holds", "Declare one inclusive invocation interval each for pitch, yaw and both")
    result = []
    for label in labels:
        if not isinstance(label, Mapping) or set(label) != {"hold", "start_invocation", "end_invocation"}:
            _fail("invalid_holds", "Hold labels require hold/start_invocation/end_invocation only")
        item = {"hold": str(label["hold"]),
                "start_invocation": _integer(label["start_invocation"], "start_invocation"),
                "end_invocation": _integer(label["end_invocation"], "end_invocation")}
        if item["hold"] not in HOLDS or item["end_invocation"] <= item["start_invocation"]:
            _fail("invalid_holds", "Each hold needs a valid nonempty interval")
        result.append(item)
    result.sort(key=lambda item: item["start_invocation"])
    if {item["hold"] for item in result} != set(HOLDS):
        _fail("invalid_holds", "Each pitch/yaw/both hold occurs once")
    if any(a["end_invocation"] >= b["start_invocation"] for a, b in zip(result, result[1:])):
        _fail("overlapping_holds", "Declared holds must not overlap")
    return result


def _rotation_delivery(record):
    """Prove commanded delivery and actual native zero separately for each axis.

    A production singleton has one D12 bit. Its uncommanded lane is never
    described as an injected zero: both same-invocation getter observations,
    no conflict, absent mask/application bit and exact native/scalar zero prove
    the neutral lane. Signed zero is accepted; finite noise is not inferred zero.
    """
    if _integer(record.get("d12_provenance_bits"), "d12_provenance_bits") != 3:
        _fail("d12_provenance", "Both same-invocation native scalar getter observations are required")
    if _integer(record.get("d12_conflict"), "d12_conflict") != 0:
        _fail("d12_conflict", "Conflicted native input cannot calibrate rotation")
    mask = _integer(record.get("d12_mask"), "d12_mask")
    applied = _integer(record.get("d12_applied_mask"), "d12_applied_mask")
    paths = {}
    for axis, bit in (("yaw", 1), ("pitch", 2)):
        command = _number(record.get("input_" + axis), "input_" + axis)
        native = _number(record.get("native_input_" + axis), "native_input_" + axis)
        output = _number(record.get("scalar_" + axis), "scalar_" + axis)
        if abs(command) > 1 or abs(output) > 1:
            _fail("invalid_number", f"Normalized {axis} command and scalar output must be within [-1, 1]")
        if command != 0:
            if not mask & bit or not applied & bit:
                _fail("d12_not_applied", f"Nonzero {axis} needs its declared and same-invocation applied D12 bit")
            if struct.pack('<f', command) != struct.pack('<f', output):
                _fail("d12_output_mismatch", f"Actual {axis} scalar output differs from the D12 command")
            paths[axis] = "d12_command"
        else:
            if mask & bit or applied & bit:
                _fail("d12_zero_claim", f"Zero {axis} cannot claim an injected D12 delivery")
            if native != 0 or output != 0:
                _fail("native_not_zero", f"Uncommanded {axis} needs actual native getter and scalar zero")
            paths[axis] = "native_zero"
    return paths


def _native_association(record, invocation):
    """Validate explicit same-call facts without equating independent counters."""
    if not _flag(record.get('association_complete'), 'association_complete'):
        _fail('native_association', 'Native same-call D12/Newtonian association is incomplete')
    d12_invocation = _integer(record.get('d12_invocation'), 'd12_invocation')
    if not invocation or not d12_invocation:
        _fail('invocation_mismatch', 'Native source invocation counters must be positive')
    facts = {'invocation': invocation, 'd12_invocation': d12_invocation}
    for domain, expected in (('newtonian', invocation), ('d12', d12_invocation)):
        key = 'associated_' + domain + '_invocation'
        facts[key] = _integer(record.get(key), key)
        if facts[key] != expected:
            _fail('invocation_mismatch', f'{domain} association snapshot differs from its actual source counter')
    for key, expected in (('newtonian_latch_invocation', invocation),
                          ('d12_processed_invocation', d12_invocation),
                          ('d12_throttle_invocation', d12_invocation)):
        facts[key] = _integer(record.get(key), key)
        if facts[key] != expected:
            _fail('invocation_mismatch', f'{key} is stale in its own counter domain')
    for kind in ('stack', 'thread'):
        values = []
        for domain in ('native', 'newtonian', 'd12'):
            key = domain + '_' + kind
            facts[key] = _integer(record.get(key), key)
            values.append(facts[key])
        if not values[0] or len(set(values)) != 1:
            _fail('native_association', f'Actual native and both source {kind} identities must agree')
    if _integer(record.get('frame'), 'frame') != facts['native_stack']:
        _fail('native_association', 'Original native target frame differs from the associated native stack')
    return facts


def export_rows(raw: Iterable[Mapping], labels, metadata: Mapping, frequency,
                *, baseline: bool = True) -> dict:
    """Export strict analyzer CSV rows from native observer receipts.

    labels is three {hold,start_invocation,end_invocation} mappings; intervals are
    inclusive. All rows *inside* intervals are required, including settling rows.
    Outside rows may be incomplete and are counted explicitly. QPC frequency is
    the actual QueryPerformanceFrequency result; no tick-to-seconds fallback.
    Raw ``applied`` means T66 replacement; CSV ``applied`` means same-invocation
    commanded D12 rotation delivery plus observed native zero on uncommanded
    lanes, checked independently. No injected zero is claimed. baseline=False exports
    replacement evidence for analysis but can never create a calibration.
    Source counters are independent. Native stack/thread and per-domain marker
    evidence associates them; JSON retains the actual D12 counter. CSV
    receipt_invocation labels the associated atomic T66 row in Newtonian ordinals.
    """
    if not isinstance(baseline, bool):
        _fail("invalid_baseline", "baseline must be a boolean")
    if not isinstance(metadata, Mapping):
        _fail("invalid_identity", "Explicit capture metadata mapping required")
    frequency = _integer(frequency, "qpc_frequency")
    if frequency <= 0 or frequency >= 1 << 63:
        _fail("qpc_frequency", "Positive actual QPC frequency required")
    intervals = _hold_intervals(labels)
    evidence = metadata.get("evidence_kind")
    if evidence not in ("synthetic", "native_receipt"):
        _fail("evidence_kind", "Explicit synthetic/native_receipt evidence kind required")
    records = list(raw)
    selected, last_invocation = [], None
    counts = {hold: 0 for hold in HOLDS}
    for record in records:
        if not isinstance(record, Mapping):
            _fail("invalid_receipt", "Native records must be mappings")
        invocation = _integer(record.get("invocation"), "invocation")
        if last_invocation is not None and invocation <= last_invocation:
            _fail("nonmonotonic", "Raw invocation ordinals must strictly increase")
        last_invocation = invocation
        label = next((item for item in intervals if item["start_invocation"] <= invocation <= item["end_invocation"]), None)
        if label is not None:
            selected.append((record, label["hold"]))
            counts[label["hold"]] += 1
    for label in intervals:
        if counts[label["hold"]] != label["end_invocation"] - label["start_invocation"] + 1:
            _fail("receipt_gap", f"Missing native invocation inside declared {label['hold']} hold")
    rows, identity, engine, qpc_origin, previous_qpc, previous_tick = [], None, None, None, None, None
    own_override_count = 0
    native_associations = []
    previous_d12_invocation = None
    delivery_paths = {axis: {"d12_command": 0, "native_zero": 0} for axis in ("pitch", "yaw")}
    for record, hold in selected:
        invocation = _integer(record["invocation"], "invocation")
        if not _flag(record.get("complete"), "complete"):
            _fail("incomplete_receipt", f"Invocation {invocation}: incomplete ({record.get('reason')})")
        if "capture_stage" in record and _integer(record["capture_stage"], "capture_stage") != 2:
            _fail("incomplete_receipt", "Native receipt has not reached the torque-sealed capture stage")
        if evidence == "native_receipt" or "ordinary_invocation" in record:
            if _integer(record.get("ordinary_invocation"), "ordinary_invocation") != invocation:
                _fail("invocation_mismatch", "Ordinary-mode evidence is from another native invocation")
            if not _flag(record.get("mode_capture_complete"), "mode_capture_complete"):
                _fail("mode_capture", "Same-invocation native ordinary/source mode capture is incomplete")
            if (not isinstance(record.get("mode_pins"), Mapping) or not record["mode_pins"]
                    or not isinstance(metadata.get("mode_pins"), Mapping) or not metadata["mode_pins"]):
                _fail("mode_capture", "Actual native mode/source pins and matching declared pins are required")
        for field in ("admitted", "qualified", "fresh"):
            if not _flag(record.get(field), field):
                _fail("unadmitted_receipt", f"Invocation {invocation}: {field}=0")
        association = _native_association(record, invocation)
        d12_invocation = association['d12_invocation']
        if previous_d12_invocation is not None and d12_invocation <= previous_d12_invocation:
            _fail('invocation_mismatch', 'D12 source counter must increase independently across accepted receipts')
        previous_d12_invocation = d12_invocation
        native_associations.append(association)
        for axis, path in _rotation_delivery(record).items():
            delivery_paths[axis][path] += 1
        own_applied = _flag(record.get("applied"), "applied")
        own_override_count += own_applied
        if baseline and own_applied:
            _fail("not_native_baseline", "Replacement-applied rows cannot calibrate native authority")
        this_identity = capture_identity(metadata, record)
        this_engine = _engine_context(record, metadata)
        if this_engine["freelook"]:
            _fail("freelook", "Freelook is outside the supported calibration context")
        if identity is None:
            identity, engine = this_identity, this_engine
        elif this_identity != identity or this_engine != engine:
            _fail("identity_changed", "Engine, selected controls, state, or capture identity changed")
        qpc = _integer(record.get("qpc"), "qpc")
        if qpc >= 1 << 63:
            _fail("qpc_range", "QPC must fit the native positive LARGE_INTEGER range")
        tick = _integer(record.get("tick_ms"), "tick_ms")
        if previous_qpc is not None and (qpc <= previous_qpc or tick < previous_tick):
            _fail("nonmonotonic", "QPC must strictly increase; tick_ms must not decrease")
        if qpc_origin is None:
            qpc_origin = qpc
        previous_qpc, previous_tick = qpc, tick
        time_s = (qpc-qpc_origin)/frequency
        if not math.isfinite(time_s) or rows and time_s <= rows[-1]["time_s"]:
            _fail("nonmonotonic", "QPC conversion must produce strictly increasing finite seconds")
        row = {"schema_version": "1", "evidence_kind": evidence, **identity,
               "hold": hold, "time_s": time_s,
               "tick_ms": tick, "invocation": invocation, "receipt_invocation": invocation,
               "admitted": "1", "qualified": "1", "fresh": "1", "applied": "1"}
        for prefix in ("input", "target", "rate", "gain"):
            for axis in AXES:
                name = f"{prefix}_{axis}"
                row[name] = _number(record.get(name), name)
        for axis in AXES:
            native = _number(record.get(f"native_target_{axis}"), f"native_target_{axis}")
            if baseline and native != row[f"target_{axis}"]:
                _fail("target_mismatch", "Native baseline target differs from selected target")
        row["max_torque"] = this_engine["max_torque"]
        for side in ("before", "after"):
            for axis in ("x", "y", "z"):
                name = f"torque_{side}_{axis}"
                row[name] = _number(record.get(name), name)
        rows.append(row)
    result = {"schema_version": 1, "baseline": baseline, "evidence_kind": evidence,
              "identity": identity, "engine_context": engine, "rows": rows,
              "hold_intervals": intervals, "qpc_frequency": frequency, "qpc_origin": qpc_origin,
              "source_count": len(records), "outside_hold_count": len(records)-len(rows),
              "own_override_count": own_override_count,
              "delivery_paths": delivery_paths,
              "native_associations": native_associations,
              "production_authority": False, "live_qualification": False}
    result["content_sha256"] = _digest(result)
    return result


def _verified_report(report):
    if not isinstance(report, Mapping):
        _fail("invalid_report", "Expected an export_rows report")
    payload = dict(report)
    expected = _hex_digest(payload.pop("content_sha256", None), "content_sha256")
    if _digest(payload) != expected:
        _fail("report_changed", "Export report was modified after export")
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or payload.get("baseline") is not True or payload.get("own_override_count") != 0:
        _fail("not_native_baseline", "Only unmodified native-baseline exports can calibrate")
    if payload.get("production_authority") is not False or payload.get("live_qualification") is not False:
        _fail("authority", "Export cannot grant live/production authority")
    return payload


def _float32(value, name):
    value = _number(value, name)
    try:
        result = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        _fail("float32_range", f"{name}: finite float32 required")
    if not math.isfinite(result) or result == 0:
        _fail("float32_range", f"{name}: finite nonzero float32 required")
    return result


def _tolerance_limit(value):
    """At most two float32 ULPs; a tolerance is explicit, never a rate gain."""
    rounded = abs(_float32(value, "measured_limit"))
    bits = struct.unpack("<I", struct.pack("<f", rounded))[0]
    if bits >= 0x7f7fffff:
        _fail("float32_range", "Measured authority leaves no finite float32 headroom")
    next_value = struct.unpack("<f", struct.pack("<I", bits+1))[0]
    return 2*(next_value-rounded)


def derive_calibration(report: Mapping, *, config=None, float32_tolerance=0.,
                       max_input_amplitude=MAX_FIRST_INPUT) -> dict:
    """Fit independently measured signed targets; circular both-holds are valid.

    A negative full-axis hold is divided by its signed input to express the scale
    for a positive normalized command. The original signed observation is kept.
    No gain or additivity is inferred from the diagonal response.
    """
    source_report_hash = report.get("content_sha256") if isinstance(report, Mapping) else None
    report = _verified_report(report)
    analysis = _analyzer().analyze_holds(report["rows"], config)
    if analysis["input_kind"] != "full_independent_1_1":
        _fail("calibration_input", "Calibrate with full independent single/combined native holds")
    if analysis["warnings"]:
        _fail("unsafe_calibration", "Native windows failed: " + ",".join(analysis["warnings"]))
    amplitude = _number(max_input_amplitude, "max_input_amplitude")
    if not 0 < amplitude <= MAX_FIRST_INPUT:
        _fail("validation_bound", "First validation amplitude must be in (0,.25]")
    tolerance = _number(float32_tolerance, "float32_tolerance")
    if tolerance < 0:
        _fail("calibration_tolerance", "Float32 tolerance cannot be negative")
    axes = {}
    for axis in ("pitch", "yaw"):
        window = analysis["windows"][axis]
        selected = [row for row in report["rows"] if row["hold"] == axis
                    and window["start_s"] < row["time_s"] <= window["end_s"]]
        observed = window["means"][f"target_{axis}"]
        command = window["means"][f"input_{axis}"]
        scale = _float32(observed/command, f"signed_full_{axis}_target")
        measured = max(abs(row[f"target_{axis}"]) for row in selected)
        if tolerance > _tolerance_limit(measured):
            _fail("calibration_tolerance", "Tolerance exceeds two measured float32 ULPs")
        if abs(scale) > measured+tolerance:
            _fail("calibration_tolerance", "Rounded target exceeds measured bound; declare a tiny float32 tolerance")
        axes[axis] = {"signed_full_target": scale, "observed_full_input": command,
                      "observed_signed_target": observed, "measured_abs_max": measured,
                      "rate_limit": measured+tolerance,
                      "measured_gain": report["rows"][0][f"gain_{axis}"],
                      "observed_signed_rate": window["means"][f"rate_{axis}"]}
    result = {"schema_version": 1, "calibration_scope": "bounded_live_validation_only",
              "target_convention": NATIVE_CONVENTION, "evidence_kind": report["evidence_kind"],
              "identity": report["identity"], "engine_context": report["engine_context"],
              "mode_pins": report["engine_context"].get("mode_pins"),
              "calibration_session": report["identity"]["session_id"],
              "axes": axes, "float32_tolerance": tolerance, "max_input_amplitude": amplitude,
              "source_report_sha256": source_report_hash,
              "source_rows": len(report["rows"]), "qpc_frequency": report["qpc_frequency"],
              "analysis_config": analysis["config"],
              "production_authority": False, "live_qualification": False,
              "unrestricted_additive_success": False}
    result["content_sha256"] = _digest(result)
    return validate_calibration(result, report["identity"])


def validate_calibration(calibration: Mapping, current_identity: Mapping) -> dict:
    """Reject corrupt/unsafe calibration or a changed exact current fixture.

    Session labels may change between observation and bounded validation; process,
    ship, body, class, context, exact executable and effective engine must match.
    This validates supplied data; it does not qualify a live native invocation.
    """
    if not isinstance(calibration, Mapping) or not isinstance(current_identity, Mapping):
        _fail("invalid_calibration", "Calibration and current identity mappings required")
    payload = dict(calibration)
    expected = _hex_digest(payload.pop("content_sha256", None), "content_sha256")
    if _digest(payload) != expected:
        _fail("calibration_changed", "Calibration content hash mismatch")
    required = {"schema_version", "calibration_scope", "target_convention", "evidence_kind", "identity",
                "engine_context", "mode_pins", "calibration_session", "axes", "float32_tolerance", "max_input_amplitude",
                "source_report_sha256", "source_rows", "qpc_frequency", "analysis_config",
                "production_authority", "live_qualification", "unrestricted_additive_success"}
    if set(payload) != required or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        _fail("invalid_calibration", "Expected exact schema 1 calibration")
    if (payload["calibration_scope"] != "bounded_live_validation_only"
            or payload["target_convention"] != NATIVE_CONVENTION
            or payload["evidence_kind"] not in ("native_receipt", "synthetic")):
        _fail("invalid_calibration", "Unsupported calibration scope/convention/evidence")
    if any(payload[key] is not False for key in ("production_authority", "live_qualification", "unrestricted_additive_success")):
        _fail("authority", "Calibration cannot grant production or live authority")
    identity = payload["identity"]
    if not isinstance(identity, Mapping) or set(identity) != set(IDENTITY_FIELDS + ("engine_fingerprint",)):
        _fail("invalid_calibration", "Exact recorded build/process/fixture identity is required")
    for key in PIN_FIELDS:
        if _text(identity.get(key), key) != _text(current_identity.get(key), key):
            _fail("fixture_changed", f"Current fixture differs: {key}")
    _hex_digest(identity["exe_sha256"], "exe_sha256")
    _hex_digest(identity["engine_fingerprint"], "engine_fingerprint")
    analyzer = _analyzer()
    if (identity["file_version"], identity["steam_buildid"], identity["exe_sha256"]) != (analyzer.FILE_VERSION, analyzer.STEAM_BUILDID, analyzer.EXE_SHA256):
        _fail("wrong_build", "Calibration is not for the exact T66 executable")
    try:
        analyzer._validate_config(analyzer.AnalysisConfig(**payload["analysis_config"]))
    except (TypeError, ValueError) as error:
        _fail("analysis_config", f"Invalid measured-window configuration: {error}")
    if payload["calibration_session"] != identity["session_id"] or not identity["session_id"]:
        _fail("invalid_calibration", "Calibration session identity is inconsistent")
    engine = payload["engine_context"]
    if not isinstance(engine, Mapping) or _digest(engine) != identity["engine_fingerprint"]:
        _fail("engine_fingerprint", "Calibration effective-engine context hash mismatch")
    # Re-parse the actual context rather than accepting a forged hash of malformed data.
    checked = _engine_context(engine, engine)
    if checked != engine or checked["freelook"] or checked["context"] != identity["context_id"]:
        _fail("engine_context", "Unsupported or malformed effective engine context")
    if payload["mode_pins"] != engine.get("mode_pins"):
        _fail("mode_pins", "Calibration mode/source pins differ from effective context")
    _hex_digest(payload["source_report_sha256"], "source_report_sha256")
    if _integer(payload["source_rows"], "source_rows") < 9 or _integer(payload["qpc_frequency"], "qpc_frequency") <= 0:
        _fail("invalid_calibration", "Measured source receipts and actual QPC frequency required")
    amplitude = _number(payload["max_input_amplitude"], "max_input_amplitude")
    if not 0 < amplitude <= MAX_FIRST_INPUT:
        _fail("validation_bound", "First validation amplitude must be in (0,.25]")
    tolerance = _number(payload["float32_tolerance"], "float32_tolerance")
    if tolerance < 0:
        _fail("calibration_tolerance", "Float32 tolerance cannot be negative")
    if not isinstance(payload["axes"], Mapping) or set(payload["axes"]) != {"pitch", "yaw"}:
        _fail("invalid_calibration", "Independent pitch/yaw measurements are required")
    for axis, values in payload["axes"].items():
        fields = {"signed_full_target", "observed_full_input", "observed_signed_target", "measured_abs_max",
                  "rate_limit", "measured_gain", "observed_signed_rate"}
        if not isinstance(values, Mapping) or set(values) != fields:
            _fail("invalid_calibration", f"Incomplete {axis} measurement")
        values = {key: _number(value, f"{axis}.{key}") for key, value in values.items()}
        scale, measured, limit = values["signed_full_target"], values["measured_abs_max"], values["rate_limit"]
        command, observed, rate = values["observed_full_input"], values["observed_signed_target"], values["observed_signed_rate"]
        if scale != _float32(scale, "signed_full_target") or measured <= 0 or limit <= 0:
            _fail("calibration_bound", "Target must be float32 with a positive measured bound")
        if tolerance > _tolerance_limit(measured) or limit != measured+tolerance or abs(scale) > limit:
            _fail("calibration_bound", "Rate limit must be measured authority plus explicit tiny float32 tolerance")
        if not math.isclose(abs(command), 1, abs_tol=1e-6) or not math.isclose(scale, observed/command, rel_tol=2e-7):
            _fail("calibration_sign", "Signed full target does not match measured singleton input")
        if observed*rate >= 0 or rate == 0 or abs(observed) > measured+_tolerance_limit(measured):
            _fail("calibration_sign", "Native feedback signs/measurement bounds are inconsistent")
        if values["measured_gain"] <= 0:
            _fail("invalid_gain", "Measured native gains must be positive")
    # A detached JSON copy prevents caller mutation after validation.
    return json.loads(json.dumps(calibration, allow_nan=False))
