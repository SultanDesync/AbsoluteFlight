"""BUS-0 probe: load the bus cdylib through ctypes, list devices and sample raw values.

Runs unchanged in a plain process (host B baseline) and inside NMS.exe under pyMHF (host A),
so the two outputs can be diffed. Reader-only: it never touches game memory and emits no input.

    python -I bus0_probe.py <path-to-absolute_input_bus.dll> [--seconds 5] [--hz 250] [--out result.json]

Prints one compact JSON object. `probe(...)` can be imported from a pyMHF mod and given the same arguments.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time

DEVICE_ID_CAPACITY = 48
DEVICE_NAME_CAPACITY = 128


class DeviceInfoV1(ctypes.Structure):
    _fields_ = [
        ("structSize", ctypes.c_uint32), ("deviceIndex", ctypes.c_uint32),
        ("instanceGuid", ctypes.c_uint8 * 16), ("productGuid", ctypes.c_uint8 * 16),
        ("vendorId", ctypes.c_uint16), ("productId", ctypes.c_uint16),
        ("axisCount", ctypes.c_uint32), ("buttonCount", ctypes.c_uint32), ("povCount", ctypes.c_uint32),
        ("persistentId", ctypes.c_char * DEVICE_ID_CAPACITY),
        ("instanceName", ctypes.c_char * DEVICE_NAME_CAPACITY),
        ("productName", ctypes.c_char * DEVICE_NAME_CAPACITY),
    ]


class DeviceSnapshotV1(ctypes.Structure):
    _fields_ = [
        ("structSize", ctypes.c_uint32), ("deviceIndex", ctypes.c_uint32),
        ("sequence", ctypes.c_uint64), ("producerGeneration", ctypes.c_uint64),
        ("connected", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 7),
        ("rawAxes", ctypes.c_int32 * 8), ("axisMinimum", ctypes.c_int32 * 8), ("axisMaximum", ctypes.c_int32 * 8),
        ("normalizedAxes", ctypes.c_float * 8), ("povHundredths", ctypes.c_int32 * 4),
        ("digitalDown", ctypes.c_uint64 * 3), ("pressCount", ctypes.c_uint32 * 144), ("releaseCount", ctypes.c_uint32 * 144),
    ]


GET_COUNT = ctypes.CFUNCTYPE(ctypes.c_uint32)
GET_DEVICE = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(DeviceInfoV1))
GET_SNAPSHOT = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(DeviceSnapshotV1))


class ApiV1(ctypes.Structure):
    _fields_ = [
        ("structSize", ctypes.c_uint32), ("abiVersion", ctypes.c_uint32),
        ("providerId", ctypes.c_char_p), ("displayName", ctypes.c_char_p), ("version", ctypes.c_char_p),
        ("capabilities", ctypes.c_uint64),
        ("getDeviceCount", GET_COUNT), ("getDevice", GET_DEVICE), ("getSnapshot", GET_SNAPSHOT),
        ("getProfileState", ctypes.c_void_p), ("getRuntimeContext", ctypes.c_void_p),
        ("beginCapture", ctypes.c_void_p), ("pollCapture", ctypes.c_void_p), ("cancelCapture", ctypes.c_void_p),
    ]


assert ctypes.sizeof(DeviceInfoV1) == 360 and ctypes.sizeof(DeviceSnapshotV1) == 1352 and ctypes.sizeof(ApiV1) == 104


def probe(dll_path: str, seconds: float = 5.0, hz: int = 250) -> dict:
    dll = ctypes.CDLL(dll_path)
    dll.AbsoluteInputBus_QueryApi.restype = ctypes.POINTER(ApiV1)
    dll.AbsoluteInputBus_QueryApi.argtypes = [ctypes.c_uint32]
    dll.AbsoluteInputBus_Start.restype = ctypes.c_uint32
    dll.AbsoluteInputBus_Start.argtypes = [ctypes.c_uint32]
    api_ptr = dll.AbsoluteInputBus_QueryApi(1)
    if not api_ptr:
        return {"ok": False, "error": "QueryApi(1) returned null"}
    api = api_ptr.contents
    result: dict = {
        "ok": True, "pid": os.getpid(), "exe": os.path.basename(sys.executable),
        "provider": api.providerId.decode(), "version": api.version.decode(), "abi": api.abiVersion,
        "capabilities": api.capabilities,
    }
    start_code = dll.AbsoluteInputBus_Start(hz)
    result["start"] = start_code
    try:
        t0 = time.perf_counter()
        devices = []
        for i in range(api.getDeviceCount()):
            info = DeviceInfoV1(structSize=ctypes.sizeof(DeviceInfoV1))
            if api.getDevice(i, ctypes.byref(info)) == 0:
                devices.append({
                    "index": i, "id": info.persistentId.decode(errors="replace"),
                    "vidpid": f"{info.vendorId:04X}:{info.productId:04X}",
                    "name": info.instanceName.decode(errors="replace"),
                    "counts": [info.axisCount, info.buttonCount, info.povCount],
                })
        # Per-device sample statistics over the window: min/max raw per axis, last snapshot, read cost.
        stats = {d["index"]: {"min": [None] * 8, "max": [None] * 8, "conn": set(), "gens": set()} for d in devices}
        snap = DeviceSnapshotV1(structSize=ctypes.sizeof(DeviceSnapshotV1))
        reads, read_ns, first_seq, last_seq = 0, 0, None, None
        while time.perf_counter() - t0 < seconds:
            for d in devices:
                r0 = time.perf_counter_ns()
                code = api.getSnapshot(d["index"], ctypes.byref(snap))
                read_ns += time.perf_counter_ns() - r0
                reads += 1
                if code != 0:
                    continue
                s = stats[d["index"]]
                s["conn"].add(snap.connected)
                s["gens"].add(snap.producerGeneration)
                if not snap.connected:
                    continue  # settling after Acquire, or unplugged: raw values are not device data
                first_seq = snap.sequence if first_seq is None else first_seq
                last_seq = snap.sequence
                for a in range(8):
                    v = snap.rawAxes[a]
                    s["min"][a] = v if s["min"][a] is None else min(s["min"][a], v)
                    s["max"][a] = v if s["max"][a] is None else max(s["max"][a], v)
                s["last_raw"] = list(snap.rawAxes)
                s["last_pov"] = list(snap.povHundredths)
                s["down"] = [c + 1 for c in range(144) if snap.digitalDown[c // 64] >> (c % 64) & 1]
                s["presses"] = sum(snap.pressCount)
            time.sleep(0.01)
        for d in devices:
            s = stats[d["index"]]
            d.update({"connected": sorted(s["conn"]), "generations": sorted(s["gens"]), "raw_min": s["min"], "raw_max": s["max"],
                      "last_raw": s.get("last_raw"), "last_pov": s.get("last_pov"), "down_channels": s.get("down"),
                      "presses": s.get("presses")})
        elapsed = time.perf_counter() - t0
        result.update({
            "devices": devices, "seconds": round(elapsed, 3),
            "bus_polls_per_s": round((last_seq - first_seq) / elapsed, 1) if first_seq is not None else None,
            "mean_get_snapshot_us": round(read_ns / max(reads, 1) / 1000, 2),
        })
    finally:
        dll.AbsoluteInputBus_Stop()
    return result


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    seconds, hz, out = 5.0, 250, None
    args = argv[1:]
    for i, a in enumerate(args):
        if a == "--seconds":
            seconds = float(args[i + 1])
        elif a == "--hz":
            hz = int(args[i + 1])
        elif a == "--out":
            out = args[i + 1]
    result = probe(argv[0], seconds, hz)
    text = json.dumps(result, separators=(",", ":"))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
