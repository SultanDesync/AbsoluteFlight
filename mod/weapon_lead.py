"""T72 ship prediction input correction, NMS 183877 / Steam 26295378.

Native prediction already solves with a full velocity vector. Ordinary projectiles
inherit GameplayGlobals.ProjectileSpeedBoost times that velocity; prediction must
subtract the same inherited vector. This adapter only changes a private argument
copy in positively scoped player HUD/aim calls. It never changes physics velocity,
projectile motion, target selection or caller-owned memory.
"""
import ctypes as C
import math
import struct
import threading
import time
from pilot_boundary import local_player_ship

APP_GLOBAL = 0x6F5F868
INHERITANCE = 0x5331468
OWNERS = {'pip': 0x733180, 'aim_assist': 0x72FF60}  # app+: frame owner objects
# These sites are below the hooked entries and remain verifiable on hot reload.
PINS = {
    0x15B9832: bytes.fromhex('410f5c370f58c8'),
    0x99684C: bytes.fromhex('e8ef2cc200'),
    0x1419C4B: bytes.fromhex('e8f0f81900'),
    0x1419CA2: bytes.fromhex('e899f81900'),
    0x15A5A46: bytes.fromhex('f30f10051abad803'),
}
# Independent qualification: a sampling mismatch must not disable the T72 fix.
TARGET_PINS = {
    0x15B9561: bytes.fromhex('488b89680400004d8bf0488bf24885c90f84fa02000080bf180b0000000f84ed020000e86784770184c00f84e0020000'),
    0x29A520: bytes.fromhex('4c8b4608488d4e40488d54242048899c2480000000e8e6c6ab02'),
    0x2D56C26: bytes.fromhex('4983b8380a000001488bda75170f1001488bc20f29020f1049100f294a10'),
    0x2D313F3: bytes.fromhex('488b9e280300004883bb380a00000175090f1041600f1102eb46'),
    0x2D319F0: bytes.fromhex('488b91900200004885d27420488b81280300004885c074144883b88003000000740a0fb64201c0e8052401c332c0c3'),
}
TARGET_MAX_AGE_MS = 150


class ShipLeadCorrection:
    def __init__(self, base, read=C.string_at, clock=None):
        self.base, self.read = int(base), read
        # Release injects GetTickCount64, matching the flight HUD's tick domain.
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.target_verified = False
        self.target_state = 'not verified'
        self.target_errors = 0
        self._target_lock = threading.Lock()
        self._target_epoch = 0
        self._latest_target = None
        self.local = threading.local()
        self.verified = False
        self.applied = dict.fromkeys(OWNERS, 0)
        self.latest = {'state': 'not verified', 'inheritance_multiplier': None}

    def _get(self, address, fmt):
        return struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))

    def _q(self, address):
        return self._get(address, '<Q')[0]

    def verify(self):
        self._latest_target = None
        self.verified = all(self.read(self.base + rva, len(raw)) == raw
                            for rva, raw in PINS.items())
        try:
            self.target_verified = self.verified and all(
                self.read(self.base + rva, len(raw)) == raw for rva, raw in TARGET_PINS.items())
        except Exception:
            self.target_verified = False
        self.target_state = 'ready' if self.target_verified else 'code signature mismatch'
        self.latest = {'state': 'ready' if self.verified else 'code signature mismatch',
                       'inheritance_multiplier': self.coefficient() if self.verified else None}
        return self.verified

    def coefficient(self):
        value = self._get(self.base + INHERITANCE, '<f')[0]
        if not math.isfinite(value):
            raise ValueError('nonfinite projectile inheritance')
        return value

    def _stack(self):
        if not hasattr(self.local, 'stack'):
            self.local.stack = []
        return self.local.stack

    def begin(self, lane, owner):
        # Always mask an outer scope before any reads or validation can fail.
        frame = {'lane': lane, 'owner': int(owner or 0), 'allowed': False}
        if lane == 'pip':
            # No prediction in the next HUD update means no current target.
            # Aim acquisition candidates must never replace the HUD selection.
            with self._target_lock:
                self._target_epoch += 1
                self._latest_target = None
                frame['target_epoch'] = self._target_epoch
        self._stack().append(frame)
        return frame

    def qualify(self, frame, ship, eligible):
        if not self._stack() or self._stack()[-1] is not frame:
            return False
        if not self.verified or not eligible or frame['lane'] not in OWNERS or not ship:
            return False
        app = self._q(self.base + APP_GLOBAL)
        if (not app or frame['owner'] != app + OWNERS[frame['lane']]
                or local_player_ship(self.read, self.base) != ship):
            return False
        body_owner = self._q(ship + 0x6258)
        if not body_owner:
            return False
        body = body_owner + 0x50
        if not self._primary(body):
            return False
        frame.update(allowed=True, app=app, ship=int(ship), body=body)
        return True

    def end(self, lane, owner):
        stack = self._stack()
        if stack and (stack[-1]['lane'], stack[-1]['owner']) == (lane, int(owner or 0)):
            stack.pop()
        else:
            # A broken pairing must not leave a stale admitted outer scope.
            stack.clear()

    def _primary(self, body):
        context = self._q(body + 0x328)
        return bool(context and self._q(context + 0xA38) == 1)

    def target_sample(self, max_age_ms=TARGET_MAX_AGE_MS, *, ship=None, target=None):
        """Copied POD sample or None; no game-memory reads on the render thread.

        Positions are primary-context split world coordinates in native units.
        Use displacement_xyz for range; it subtracts the halves before combining.
        The target position is the rigid origin, not a weapon-specific aim anchor.
        """
        with self._target_lock:
            sample = self._latest_target
            if not self.target_verified or sample is None:
                return None
            age = self.clock() - sample['tick']
            if (not 0 <= age <= min(max_age_ms, TARGET_MAX_AGE_MS)
                    or ship is not None and ship != sample['ship']
                    or target is not None and target != sample['target']):
                return None
            return {**sample, 'age_ms': age}

    @property
    def latest_target(self):
        return self.target_sample()

    def _sample_target(self, frame, target, body, position, velocity):
        """Observe the existing pip call, independently of projectile eligibility."""
        epoch = frame.get('target_epoch')
        if not self.target_verified or frame['lane'] != 'pip':
            return
        with self._target_lock:
            if epoch != self._target_epoch:
                return
            self._latest_target = None
            self.target_state = 'target unavailable'
        tick = self.clock()
        state, context = self._q(body + 0x290), self._q(body + 0x328)
        if (not state or not context or not self._get(target + 0xB18, '<B')[0]
                or self._q(context + 0xA38) != 1 or not self._q(context + 0x380)):
            return
        raw = self.read(state, 0x70)
        if not raw[1] & 0x20 or struct.unpack_from('<Q', raw, 8)[0] != context:
            return
        low = struct.unpack_from('<3f', raw, 0x40)
        high = struct.unpack_from('<3f', raw, 0x50)
        target_velocity = struct.unpack_from('<3f', raw, 0x60)
        own_position = self.read(position, 32)
        own_low = struct.unpack_from('<3f', own_position)
        own_high = struct.unpack_from('<3f', own_position, 16)
        own_velocity = self._get(velocity, '<3f')
        if not all(math.isfinite(v) for v in low + high + target_velocity + own_low + own_high + own_velocity):
            self.target_state = 'nonfinite target sample'
            return
        # Recheck owners and the exact primary-context identities after copying.
        if (self._q(target + 0x468) != body or self._q(body + 0x290) != state
                or self._q(body + 0x328) != context or self._q(state + 8) != context
                or self._q(context + 0xA38) != 1 or not self._q(context + 0x380)
                or not self._get(state + 1, '<B')[0] & 0x20
                or not self._get(target + 0xB18, '<B')[0]
                or self._q(self.base + APP_GLOBAL) != frame['app']
                or local_player_ship(self.read, self.base) != frame['ship']
                or self._q(frame['ship'] + 0x6258) + 0x50 != frame['body']
                or not self._primary(frame['body']) or epoch != self._target_epoch):
            self.target_state = 'target identity changed'
            return
        sample = {
            'tick': tick, 'target': int(target), 'target_body': body, 'target_state': state,
            'context': context, 'ship': frame['ship'], 'lane': 'pip',
            'position_low_xyz': low, 'position_high_xyz': high,
            'position_xyz': tuple(h + l for h, l in zip(high, low)),
            'velocity_xyz': target_velocity,
            'shooter_position_low_xyz': own_low, 'shooter_position_high_xyz': own_high,
            'shooter_position_xyz': tuple(h + l for h, l in zip(own_high, own_low)),
            'shooter_velocity_xyz': own_velocity,
            'displacement_xyz': tuple((h - oh) + (l - ol)
                                      for h, oh, l, ol in zip(high, own_high, low, own_low)),
        }
        with self._target_lock:
            if epoch == self._target_epoch:
                self._latest_target = sample
                self.target_state = 'sampled'

    def _projectile(self, app, ship, speed):
        # Exactly the ID source and descriptor map used by native 17EABD0.
        index = self._get(ship + 0x60 + 0xA4, '<i')[0]
        if not 0 <= index < 32:
            return None
        reality = self._q(app + 0x60)
        if not reality:
            return None
        key = self.read(reality + 0xB678 + index * 0x40, 16)
        begin, end = self._get(app + 0x6CDB48, '<QQ')
        size = end - begin
        if not begin or size <= 0 or size % 24 or size > 24 * 4096:
            return None
        rows = self.read(begin, size)
        if self._get(app + 0x6CDB48, '<QQ') != (begin, end):
            return None
        for offset in range(0, size, 24):
            if rows[offset:offset + 16] != key:
                continue
            descriptor = struct.unpack_from('<Q', rows, offset + 16)[0]
            if not descriptor:
                return None
            data = self.read(descriptor, 0x1B0)
            actual_speed = struct.unpack_from('<f', data, 0x138)[0]
            gravity = struct.unpack_from('<f', data, 0x148)[0]
            flags = struct.unpack_from('<I', data, 0x100)[0]
            if (not math.isfinite(actual_speed) or actual_speed <= 0 or speed != actual_speed
                    or gravity != 0 or flags & 0x18 or data[0x1A2]):
                return None
            # A weapon/table change while taking the snapshot keeps native math.
            if (self._get(ship + 0x104, '<i')[0] != index
                    or self.read(reality + 0xB678 + index * 0x40, 16) != key):
                return None
            return key.split(b'\0', 1)[0].decode('ascii', errors='replace')
        return None

    def arguments(self, target, out, position, velocity, speed):
        stack = self._stack()
        if not stack or not stack[-1]['allowed']:
            return None
        frame = stack[-1]
        if not all((target, out, position, velocity)) or not math.isfinite(speed) or speed <= 0:
            return None
        if (self._q(self.base + APP_GLOBAL) != frame['app']
                or local_player_ship(self.read, self.base) != frame['ship']
                or self._q(frame['ship'] + 0x6258) + 0x50 != frame['body']
                or not self._primary(frame['body'])):
            return None
        target_body = self._q(target + 0x468)
        if not target_body or not self._primary(target_body):
            return None
        try:
            self._sample_target(frame, target, target_body, position, velocity)
        except Exception as error:
            # An observation failure cannot change the established T72 arguments.
            with self._target_lock:
                if frame.get('target_epoch') == self._target_epoch:
                    self._latest_target = None
                    self.target_errors += 1
                    self.target_state = 'sample rejected: ' + repr(error)
        projectile = self._projectile(frame['app'], frame['ship'], speed)
        if projectile is None:
            return None
        multiplier = self.coefficient()  # Actual live value, never a fixed 1.5.
        raw = self.read(velocity, 16)
        original = struct.unpack_from('<3f', raw)
        scaled = tuple(v * multiplier for v in original)
        if not all(math.isfinite(v) and abs(v) <= 3.4028234663852886e38
                   for v in original + scaled):
            return None
        self.latest = {'state': 'native' if multiplier == 1.0 else 'corrected',
                       'inheritance_multiplier': multiplier, 'projectile': projectile,
                       'lane': frame['lane'], 'velocity_xyz': original,
                       'inherited_velocity_xyz': scaled}
        if multiplier == 1.0:
            return None
        # Legacy SUBPS requires alignment. The returned c_void_p retains storage
        # through its _objects reference and pyMHF's local ret tuple until the
        # original returns, including concurrent threads and recursive calls.
        storage = C.create_string_buffer(31)
        pointer = C.cast(storage, C.c_void_p)
        pointer.value = (pointer.value + 15) & ~15
        C.memmove(pointer, struct.pack('<3f', *scaled) + raw[12:16], 16)
        self.applied[frame['lane']] += 1
        return target, out, position, pointer, speed

    def status(self):
        return {**self.latest, 'verified': self.verified, 'applied': dict(self.applied),
                'target_verified': self.target_verified, 'target_state': self.target_state,
                'target_errors': self.target_errors, 'target': self.target_sample()}
