"""Explicit bounded T66 sustained authority. Pure validation/code generation."""
import hashlib
import json
import math
import struct

ENVELOPE = 't66-sustained-v1'
PRODUCTION_ENVELOPE = 't66-production-validation-v1'
MAX_WINDOW_MS = 5000
MAX_TOTAL_MS = 15000
MIN_GAP_MS = 500
ORIGIN_OFFSET = 136


def freeze_windows(windows, hard_duration_ms):
    if type(hard_duration_ms) is not int or not 1 <= hard_duration_ms <= 30000:
        raise ValueError('Sustained session must be <=30000ms')
    if not isinstance(windows, (tuple, list)) or not 1 <= len(windows) <= 3:
        raise ValueError('Sustained authority needs one to three immutable windows')
    out = []
    for window in windows:
        if (not isinstance(window, (tuple, list)) or len(window) != 2
                or any(type(value) is not int for value in window)):
            raise ValueError('Sustained windows are integer (start_ms,end_ms) pairs')
        start, end = window
        if not 0 <= start < end <= hard_duration_ms or end - start > MAX_WINDOW_MS:
            raise ValueError('Sustained window exceeds declared/session bound')
        if out and start - out[-1][1] < MIN_GAP_MS:
            raise ValueError('Sustained windows need >=500ms native fallback gaps')
        out.append((start, end))
    if sum(end - start for start, end in out) > MAX_TOTAL_MS:
        raise ValueError('Sustained total authority exceeds15000ms')
    return tuple(out)


def manifest(windows, hard_duration_ms, *, max_input_amplitude=.25, envelope=ENVELOPE):
    windows = freeze_windows(windows, hard_duration_ms)
    if envelope not in (ENVELOPE, PRODUCTION_ENVELOPE):
        raise ValueError('Unknown finite rotation validation envelope')
    if (type(max_input_amplitude) not in (int, float)
            or not math.isfinite(max_input_amplitude) or not 0 < max_input_amplitude <= 1):
        raise ValueError('Finite native input amplitude must be positive and <=1')
    if envelope == ENVELOPE and max_input_amplitude > .25:
        raise ValueError('Historical sustained authority remains bounded to quarter input')
    payload = {'envelope': envelope, 'windows': [list(item) for item in windows],
               'hard_duration_ms': hard_duration_ms, 'max_window_ms': MAX_WINDOW_MS,
               'max_total_ms': MAX_TOTAL_MS, 'min_gap_ms': MIN_GAP_MS,
               'max_input_amplitude': max_input_amplitude, 'max_lease_ms': 50,
               'native_origin_offset': ORIGIN_OFFSET}
    payload['sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return payload


def emit_window_gate(c, own, windows, hard_duration_ms, *, reject='out', observed_result=None,
                     expected_origin=None):
    """RAX is current native tick, preserved; only saved volatile R10/R11 used.

    The copied immutable windows are native code constants. No false/true host
    publication can move their endpoints or spend outside their union.
    """
    windows = freeze_windows(windows, hard_duration_ms)
    if observed_result is not None:
        # 0 means not reached, 2 means evaluated/rejected, 1 means admitted.
        c.relative(b'\xc7\x05', observed_result, struct.pack('<I', 2))
    if expected_origin is None:
        c.relative(bytes.fromhex('4c 8b 1d'), own + ORIGIN_OFFSET)  # r11=install epoch
    else:
        if type(expected_origin) is not int or not 0 < expected_origin < 2**64 - hard_duration_ms:
            raise ValueError('Finite production requires a positive immutable native epoch')
        # Production investigator authority cannot move by editing both data
        # timestamps together. This epoch is compiled before any site is patched.
        c.emit(b'\x49\xbb' + struct.pack('<Q', expected_origin))
        c.relative(bytes.fromhex('4c 3b 1d'), own + ORIGIN_OFFSET)
        c.relative(b'\x0f\x85', reject)
    c.emit(bytes.fromhex('4d 89 da 49 81 c2') + struct.pack('<I', hard_duration_ms))
    c.relative(b'\x0f\x82', reject)  # overflow cannot form a new session
    c.relative(bytes.fromhex('4c 3b 15'), own + 24)
    c.relative(b'\x0f\x85', reject)  # origin+duration == native terminal deadline
    c.emit(bytes.fromhex('49 89 c2 4d 29 da'))  # r10=now-origin
    c.relative(b'\x0f\x82', reject)
    for index, (start, end) in enumerate(windows):
        next_label = 'sustained_next_' + str(index)
        c.emit(bytes.fromhex('49 81 fa') + struct.pack('<I', start))
        c.relative(b'\x0f\x82', next_label)
        c.emit(bytes.fromhex('49 81 fa') + struct.pack('<I', end))
        c.relative(b'\x0f\x82', 'sustained_window_admitted')
        c.label(next_label)
    c.relative(b'\xe9', reject)
    c.label('sustained_window_admitted')
    if observed_result is not None:
        c.relative(b'\xc7\x05', observed_result, struct.pack('<I', 1))
