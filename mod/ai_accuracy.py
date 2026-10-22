"""Player-targeted NPC gun dispersion, NMS180836 / Steam25732212 (T83).

The gun's native behavior lookup return is replaced with a private D0-byte copy,
held until that gun call returns. Only GunDispersionAngle+B4 changes. No shared
table, aim point, projectile, physics or damage memory is written. Lasers do not
use this consumer. Static ABI/consumer evidence: T83 aim research; live unqualified.
"""
import ctypes as C
import math
import struct
import threading

from ai_evasion_model import EvasionObserver, tuning_values
from ai_pilots import wanted_mode
from pilot_boundary import local_player_ship

APP_GLOBAL = 0x6E8D708
MANAGER_FROM_APP = 0x6CD8B0
GUN_LOOKUP_CALLER = 0x17145E9
ROW_SIZE = 0xD0
DISPERSION = 0xB4
MAX_SPREAD = 60.0  # Native scale <=1/3, well below its [0,1] saturation.
# Below detour entries, including the exact native call that resolves lookup171E340.
PINS = {
    0x1714532: bytes.fromhex('448bb1740a0000bf010000000f2970b8498bd90f2978a84d8be8488bf1'),
    0x17145E4: bytes.fromhex('e8579d0000448ba6007d00004889442460'),
    0x17146CC: bytes.fromhex('488b442460f30f102d1f5541034885c07408f30f10a8b4000000f30f5e2d16574103'),
    0x171478C: bytes.fromhex('0f57c9f30f5fcd0f28ef660f5ad0f30f5de9'),
    0x17147B9: bytes.fromhex('0f59eb0f285d800f585d900f586c2450'),
    0x1714931: bytes.fromhex('488d44245048894424200f11442460e8ab92e6ff'),
    0x171E3D8: bytes.fromhex('488d5368488b5c2430488b7424384883c4205fe9d0010000'),
    0x171E3F0: bytes.fromhex('488d5318488b5c2430488b7424384883c4205fe9b8010000'),
    0x4B29E04: struct.pack('<f', 180.),
}


def enabled(values):
    readability = values.get('ai-readability', 1.)
    return (values.get('ai-aim-tuning-enabled') is True
            and values.get('flight-model') == 'sultan' and wanted_mode(values) == 'readable'
            and not isinstance(readability, bool) and isinstance(readability, (int, float))
            and math.isfinite(readability) and readability > 0.)


def spread_value(native, disruption, values):
    """Precision is a spread multiplier, never a promised percentage of hits."""
    if not math.isfinite(native) or not 0. <= native <= MAX_SPREAD:
        raise ValueError('unexpected native gun dispersion')
    knobs = tuning_values(values)
    strength = min(1., max(0., disruption)) if math.isfinite(disruption) else 0.
    desired = min(MAX_SPREAD, native / knobs['ai-aim-precision'] *
                  (1. + knobs['ai-evasion-loss'] * strength))
    readability = values.get('ai-readability', 1.)
    if (isinstance(readability, bool) or not isinstance(readability, (int, float))
            or not math.isfinite(readability)):
        readability = 0.
    return native + (desired - native) * min(1., max(0., readability))


class EvasiveAccuracy:
    def __init__(self, base, read, clock):
        self.base, self.read, self.clock = int(base), read, clock
        self.observer = EvasionObserver()
        self.motion_lock = threading.Lock()
        self.local = threading.local()
        self.verified = False
        self.closed = False
        self.retained_on_fault = []
        self.adjusted = 0
        self.errors = 0
        self.latest = {'state': 'not verified'}

    def verify(self):
        self.verified = all(self.read(self.base + rva, len(raw)) == raw for rva, raw in PINS.items())
        self.latest = {'state': 'ready' if self.verified else 'code signature mismatch'}
        return self.verified

    def _q(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def _primary(self, body):
        context = self._q(body + 0x328)
        return bool(context and self._q(context + 0xA38) == 1)

    def _stack(self):
        if not hasattr(self.local, 'stack'):
            self.local.stack = []
        return self.local.stack

    def observe(self, work, now, values):
        with self.motion_lock:
            if self.closed or not self.verified or not enabled(values):
                self.observer.reset()
                return
            self.observer.observe(work, now, values)

    def reset(self):
        with self.motion_lock:
            self.observer.reset()

    def _sample(self, ship, body, now):
        with self.motion_lock:
            return self.observer.sample(ship, body, now)

    def begin(self, actor, target, values, ship, eligible):
        # A sentinel is unconditional: a rejected nested gun must mask an outer one.
        frame = {'actor': int(actor or 0), 'allowed': False, 'copies': []}
        self._stack().append(frame)
        if self.closed or not self.verified or not eligible or not enabled(values):
            return frame
        try:
            now = self.clock()
            target = int(target or 0)
            sample = self._sample(ship, target, now)
            if sample is None:
                self.latest = {'state': 'waiting for fresh Sultan motion'}
                return frame
            if not actor or not ship or local_player_ship(self.read, self.base) != ship:
                return frame
            app = self._q(self.base + APP_GLOBAL)
            physics = self._q(actor + 0x40)
            if (not app or not physics or physics + 0x60 == target
                    or self._q(ship + 0x6248) + 0x60 != target
                    or not self._primary(target) or not self._primary(physics + 0x60)):
                return frame
            role = bytes(self.read(actor + 0x2C0, 16))
            if len(role) != 16 or not role.split(b'\0', 1)[0]:
                return frame
            frame.update(allowed=True, app=app, ship=ship, target=target, own=physics + 0x60,
                         role=role, values={**tuning_values(values),
                                            'ai-readability': values.get('ai-readability', 1.)}, sample=sample)
        except Exception as error:
            self.errors += 1
            self.latest = {'state': 'target rejected', 'error': repr(error)}
        return frame

    def row(self, manager, role, original, caller):
        """None preserves the native return; an integer substitutes a retained copy."""
        stack = self._stack()
        if self.closed or not stack or not stack[-1]['allowed'] or not original:
            return None
        frame = stack[-1]
        if caller != GUN_LOOKUP_CALLER or manager != frame['app'] + MANAGER_FROM_APP:
            return None
        try:
            current_sample = self._sample(frame['ship'], frame['target'], self.clock())
            if (current_sample is None
                    or local_player_ship(self.read, self.base) != frame['ship']
                    or self._q(self.base + APP_GLOBAL) != frame['app']
                    or self._q(frame['ship'] + 0x6248) + 0x60 != frame['target']
                    or self._q(frame['actor'] + 0x40) + 0x60 != frame['own']
                    or not self._primary(frame['target']) or not self._primary(frame['own'])
                    or bytes(self.read(role, 16)) != frame['role']):
                return None
            raw = bytes(self.read(original, ROW_SIZE))
            if len(raw) != ROW_SIZE or raw[:16] != b'SPACE'.ljust(16, b'\0'):
                return None
            native = struct.unpack_from('<f', raw, DISPERSION)[0]
            # A changed table belongs to another mod/patch. The accepted AI tuning
            # never edits this field; its shipped SPACE value is exactly5.5.
            if native != 5.5:
                self.latest = {'state': 'native gun dispersion changed; kept'}
                return None
            sample = current_sample
            desired = spread_value(native, sample.disruption, frame['values'])
            if struct.pack('<f', desired) == raw[DISPERSION:DISPERSION + 4]:
                self.latest = {'state': 'native accuracy', 'disruption': sample.disruption}
                return None
            storage = C.create_string_buffer(ROW_SIZE + 15)
            address = (C.addressof(storage) + 15) & ~15
            changed = bytearray(raw)
            struct.pack_into('<f', changed, DISPERSION, desired)
            C.memmove(address, bytes(changed), ROW_SIZE)
            frame['copies'].append(storage)  # lifetime through the gun's native return
            self.adjusted += 1
            self.latest = {'state': 'evasive aim applied', 'disruption': sample.disruption,
                           'acceleration': sample.acceleration, 'native_spread': native,
                           'applied_spread': desired}
            return address
        except Exception as error:
            self.errors += 1
            self.latest = {'state': 'native after aim error', 'error': repr(error)}
            return None

    def end(self, actor):
        stack = self._stack()
        if stack and stack[-1]['actor'] == int(actor or 0):
            stack.pop()
        else:
            # Do not free a copy that an unmatched native invocation might still
            # be using. Disable the feature and retain those rare fault frames.
            self.retained_on_fault.extend(stack)
            stack.clear()
            self.closed = True
            self.latest = {'state': 'disabled after scope mismatch'}

    def close(self):
        self.closed = True
        self.reset()
        # Active TLS frames keep their copies until their own after callbacks.

    def status(self):
        return {**self.latest, 'adjusted_lookups': self.adjusted, 'errors': self.errors}
