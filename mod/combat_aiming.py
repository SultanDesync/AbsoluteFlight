"""Copied manual-aim solutions and input friction, NMS 180836 / Steam 25732212.

Observers never change native prediction, target selection or game memory. The
release hooks own admission and native calling conventions. Prediction observes
the native solver's actual anchor displacement and T72-corrected relative
velocity; the manual reference is the returned point of 0x13F4D20. This v1 cue
describes manual centreline aim, including the native manual gun offset. It is
not a per-barrel muzzle, weapon gimbal or random spread simulation.
"""
import ctypes as C
import math
import struct
import threading
import time

from pilot_boundary import local_player_ship
from weapon_lead import APP_GLOBAL, OWNERS


MAX_AGE_MS = 50
SOLVER_CALLER = 0x158DC36
# Internal sites remain verifiable after the observed function entries detour.
PINS = {
    0x158DC29: bytes.fromhex('0f2975ff0f294defe84a000000'),
    0x158DC36: bytes.fromhex('0fc6c000488bc60f59c60f58c7'),
    0x13F4EB4: bytes.fromhex('0f580b0f110b440f28442470488bc3'),
}


def _vector(value):
    result = tuple(float(v) for v in value)
    if len(result) != 3 or not all(math.isfinite(v) for v in result):
        raise ValueError('invalid XYZ vector')
    return result


def _split(value):
    if len(value) != 2:
        raise ValueError('invalid split point')
    return _vector(value[0]), _vector(value[1])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _subtract_split(a, b):
    # Subtract each native half before combining: absolute high+low can discard
    # a close-range displacement in a large world coordinate.
    return tuple((ah - bh) + (al - bl)
                 for al, ah, bl, bh in zip(a[0], a[1], b[0], b[1]))


def _add_split(origin, displacement):
    return tuple(l + v for l, v in zip(origin[0], displacement)), origin[1]


def _native_time_valid(r, u, speed, native_time):
    if not all(math.isfinite(v) and v > 0 for v in (speed, native_time)):
        return False
    lead = tuple(x + v * native_time for x, v in zip(r, u))
    distance = math.hypot(*lead)
    travel = speed * native_time
    # The native near-equal-speed linear branch uses |u|^2 where its
    # numerator should use |r|^2. Do not publish its non-solutions. Also reject
    # unstable/cancelled roots rather than silently replacing native math.
    tolerance = max(0.02, 0.0005 * max(math.hypot(*r), travel))
    return (math.hypot(*r) > 1e-6 and math.isfinite(distance)
            and math.isfinite(travel) and abs(distance - travel) <= tolerance)


def target_plane_solution(position_split, r, u, speed, native_time,
                          manual_split, max_life):
    """Pure geometry, or None for an invalid/expired native intercept.

    Split points are ``(low_xyz, high_xyz)`` in the same primary context. Lead
    remains usable when the off-aim lag ray cannot reach the target plane within
    weapon life; in that case lag_split is None and lag_state explains why.
    """
    try:
        origin, manual = _split(position_split), _split(manual_split)
        r, u = _vector(r), _vector(u)
        speed, native_time, max_life = map(float, (speed, native_time, max_life))
        if (not math.isfinite(max_life) or max_life <= 0 or native_time > max_life
                or not _native_time_valid(r, u, speed, native_time)):
            return None
        ray = _subtract_split(manual, origin)
        ray_length = math.hypot(*ray)
        if not math.isfinite(ray_length) or ray_length <= 1e-6:
            return None
        b = tuple(v / ray_length for v in ray)
        lead = tuple(x + v * native_time for x, v in zip(r, u))
        lead_length = math.hypot(*lead)
        if lead_length <= 1e-6:
            return None
        aim = tuple(v / lead_length for v in lead)
        cross = (b[1] * aim[2] - b[2] * aim[1],
                 b[2] * aim[0] - b[0] * aim[2],
                 b[0] * aim[1] - b[1] * aim[0])
        error = math.degrees(math.atan2(math.hypot(*cross), _dot(b, aim)))
        range_ = math.hypot(*r)
        normal = tuple(v / range_ for v in r)
        w = tuple(speed * direction - relative for direction, relative in zip(b, u))
        denominator = _dot(w, normal)
        result = {
            'position_split': origin, 'manual_split': manual,
            'target_split': _add_split(origin, r),
            'lead_split': _add_split(origin, lead), 'lag_split': None,
            'aim_error_degrees': error, 'flight_time': native_time,
            'plane_time': None, 'range': range_, 'max_life': max_life,
            'manual_direction_xyz': b, 'lead_direction_xyz': aim,
            'relative_velocity_xyz': u, 'anchor_displacement_xyz': r,
            'speed': speed, 'lag_state': 'ray points away from target plane',
        }
        if not math.isfinite(denominator) or denominator <= 1e-6:
            return result
        plane_time = range_ / denominator
        result['plane_time'] = plane_time
        if not math.isfinite(plane_time) or plane_time <= 0 or plane_time > max_life:
            result['lag_state'] = 'target plane beyond weapon life'
            return result
        displacement = tuple(v * plane_time for v in w)
        if not all(math.isfinite(v) for v in displacement):
            result['lag_state'] = 'nonfinite lag point'
            return result
        result['lag_split'] = _add_split(origin, displacement)
        result['lag_state'] = 'ready'
        return result
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return None


def _smoothstep(value):
    t = max(0., min(1., value))
    return t * t * (3. - 2. * t)


def slowdown_gain(solution, values, deflection):
    """One scalar for pitch/yaw: no steering, attraction or momentum writes.

    Strength and escape are fractions (0..0.5 and 0.25..1); radius is degrees.
    Escape begins at its threshold and smoothly restores authority at full
    deflection. A threshold of 1 still restores full authority at full stick.
    """
    if solution is None or not values.get('aim-slowdown-enabled', False):
        return 1.
    try:
        strength = float(values.get('aim-slowdown-strength', .2))
        radius = float(values.get('aim-slowdown-radius', 1.5))
        escape = float(values.get('aim-slowdown-escape', .75))
        error = float(solution['aim_error_degrees'])
        deflection = abs(float(deflection))
        if not all(math.isfinite(v) for v in (strength, radius, escape, error, deflection)):
            return 1.
        if (radius <= 0 or error < 0 or error >= radius or deflection >= 1
                or solution.get('mode') == 'lag' and solution.get('lag_split') is None):
            return 1.
        strength = max(0., min(.5, strength))
        escape = max(.25, min(1., escape))
        bubble = 1. - _smoothstep(error / radius)
        escaped = (0. if deflection <= escape else
                   _smoothstep((deflection - escape) / (1. - escape)))
        return max(.5, min(1., 1. - strength * bubble * (1. - escaped)))
    except (KeyError, TypeError, ValueError, OverflowError):
        return 1.


class CombatAiming:
    """Read-only observers with TLS scopes and a lock-protected POD snapshot."""
    def __init__(self, base, lead, read=C.string_at, clock=None):
        self.base, self.lead, self.read = int(base), lead, read
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.local = threading.local()
        self._lock = threading.Lock()
        self._generation = 0
        self._hud_epoch = 0
        self._manual_epoch = 0
        self._latest = self._manual = None
        self.verified = False
        self.state = 'not verified'
        self.errors = 0

    def _get(self, address, fmt):
        return struct.unpack(fmt, self.read(int(address), struct.calcsize(fmt)))

    def _q(self, address):
        return self._get(address, '<Q')[0]

    def _point(self, address):
        raw = self.read(int(address), 32)
        return _split((struct.unpack_from('<3f', raw), struct.unpack_from('<3f', raw, 16)))

    def _stack(self, name):
        if not hasattr(self.local, name):
            setattr(self.local, name, [])
        return getattr(self.local, name)

    def reset(self, reason='reset'):
        with self._lock:
            self._generation += 1
            self._hud_epoch += 1
            self._manual_epoch += 1
            self._latest = self._manual = None
            self.state = reason

    def verify(self):
        self.reset('verifying')
        try:
            self.verified = all(self.read(self.base + rva, len(raw)) == raw
                                for rva, raw in PINS.items())
        except Exception:
            self.verified = False
        self.state = 'ready' if self.verified else 'code signature mismatch'
        return self.verified

    def begin_hud(self, owner, ship, eligible):
        # A nested rejected HUD update also invalidates any outer publication.
        with self._lock:
            self._hud_epoch += 1
            frame = {'owner': int(owner or 0), 'ship': int(ship or 0),
                     'epoch': self._hud_epoch, 'generation': self._generation,
                     'allowed': False, 'prediction': None}
            self._latest = None
            self.state = 'no current solution'
        self._stack('hud').append(frame)
        try:
            if not (self.verified and self.lead.verified and self.lead.target_verified
                    and eligible and ship):
                return frame
            app = self._q(self.base + APP_GLOBAL)
            if (not app or frame['owner'] != app + OWNERS['pip']
                    or local_player_ship(self.read, self.base) != ship):
                return frame
            body_owner = self._q(int(ship) + 0x6248)
            context = self._q(body_owner + 0x60 + 0x328) if body_owner else 0
            if not context or self._q(context + 0xA38) != 1:
                return frame
            frame.update(app=app, body_owner=body_owner, shooter_context=context, allowed=True)
        except Exception:
            self.errors += 1
        return frame

    def _identity(self, frame):
        return (self._q(self.base + APP_GLOBAL) == frame['app']
                and local_player_ship(self.read, self.base) == frame['ship']
                and self._q(frame['ship'] + 0x6248) == frame['body_owner']
                and self._q(frame['body_owner'] + 0x60 + 0x328) == frame['shooter_context']
                and self._q(frame['shooter_context'] + 0xA38) == 1)

    def _projectile(self, app, ship, speed):
        """Same selected-descriptor guard as T72, plus finite Life at +0x158."""
        index = self._get(ship + 0x104, '<i')[0]
        if not 0 <= index < 32:
            return None
        reality = self._q(app + 0x60)
        if not reality:
            return None
        key = self.read(reality + 0x6AE0 + index * 0x40, 16)
        begin, end = self._get(app + 0x6CD4E8, '<QQ')
        size = end - begin
        if not begin or size <= 0 or size % 24 or size > 24 * 4096:
            return None
        rows = self.read(begin, size)
        if self._get(app + 0x6CD4E8, '<QQ') != (begin, end):
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
            life = struct.unpack_from('<f', data, 0x158)[0]
            flags = struct.unpack_from('<I', data, 0x100)[0]
            if (not math.isfinite(actual_speed) or actual_speed <= 0 or speed != actual_speed
                    or not math.isfinite(life) or life <= 0
                    or gravity != 0 or flags & 0x18 or data[0x1A2]):
                return None
            if (self._get(ship + 0x104, '<i')[0] != index
                    or self._q(app + 0x60) != reality
                    or self._get(app + 0x6CD4E8, '<QQ') != (begin, end)
                    or self.read(reality + 0x6AE0 + index * 0x40, 16) != key
                    or self.read(begin + offset, 24) != rows[offset:offset + 24]):
                return None
            return {'projectile': key.split(b'\0', 1)[0].decode('ascii', errors='replace'),
                    'descriptor': descriptor, 'index': index, 'key': key,
                    'life': life, 'speed': actual_speed}
        return None

    def _target(self, frame):
        target = self.lead.target_sample(max_age_ms=MAX_AGE_MS,
                                         ship=frame['ship'], target=frame['target'])
        if target is None:
            return False
        keys = ('target_body', 'target_state', 'context')
        if any(target.get(key) != frame['raw_target'].get(key) for key in keys):
            return False
        body, state, context = (target[key] for key in keys)
        return (self._q(frame['target'] + 0x468) == body
                and self._q(body + 0x290) == state and self._q(body + 0x328) == context
                and self._q(state + 8) == context
                and self._q(context + 0xA38) == 1 and bool(self._q(context + 0x380))
                and bool(self._get(state + 1, '<B')[0] & 0x20)
                and bool(self._get(frame['target'] + 0xB18, '<B')[0])
                and self._get(frame['target'] + 0x6F8, '<ii') == frame['anchor_mode'])

    def prediction_begin(self, target, out, position, velocity, speed, eligible=True):
        # Push before any validation: unrelated or recursive calls mask outer data.
        frame = {'target': int(target or 0), 'out': int(out or 0), 'allowed': False,
                 'solver': None}
        self._stack('predictions').append(frame)
        hud = self._stack('hud')
        try:
            lead_stack = self.lead._stack()
            lead_scope = lead_stack[-1] if lead_stack else {}
            owned_pip = bool(hud and lead_scope.get('lane') == 'pip'
                             and lead_scope.get('owner') == hud[-1]['owner'])
            if owned_pip:
                # A later rejected pip prediction cannot keep an earlier accepted
                # target. Nested acquisition predictions belong to another lane
                # and neither replace nor erase the HUD's selected solution.
                hud[-1]['prediction'] = None
            if not (hud and hud[-1]['allowed'] and eligible and all((target, out, position, velocity))
                    and owned_pip and lead_scope.get('allowed')
                    and lead_scope.get('ship') == hud[-1]['ship']
                    and math.isfinite(speed) and speed > 0):
                return frame
            parent = hud[-1]
            if not self._identity(parent):
                return frame
            raw = self.lead.target_sample(max_age_ms=MAX_AGE_MS,
                                          ship=parent['ship'], target=int(target))
            metadata = self._projectile(parent['app'], parent['ship'], speed)
            if raw is None or metadata is None:
                return frame
            frame.update(ship=parent['ship'], app=parent['app'], epoch=parent['epoch'],
                         generation=parent['generation'], parent=parent, raw_target=raw,
                         body_owner=parent['body_owner'], shooter_context=parent['shooter_context'],
                         position_split=self._point(position), metadata=metadata,
                         speed=float(speed), anchor_mode=self._get(int(target) + 0x6F8, '<ii'))
            if self._target(frame):
                frame['allowed'] = True
        except Exception:
            self.errors += 1
        return frame

    def solver_after(self, r, u, speed, result, caller=SOLVER_CALLER):
        stack = self._stack('predictions')
        if not stack:
            return False
        frame = stack[-1]
        frame['solver'] = None
        try:
            if not (frame['allowed'] and int(caller) == SOLVER_CALLER
                    and float(speed) == frame['speed'] and r and u):
                return False
            displacement, relative = _vector(self._get(r, '<3f')), _vector(self._get(u, '<3f'))
            native_time = float(result)
            if (native_time > frame['metadata']['life']
                    or not _native_time_valid(displacement, relative, frame['speed'], native_time)):
                return False
            frame['solver'] = (displacement, relative, native_time)
            return True
        except Exception:
            self.errors += 1
            return False

    def prediction_end(self, target, out):
        stack = self._stack('predictions')
        if not stack or (stack[-1]['target'], stack[-1]['out']) != (int(target or 0), int(out or 0)):
            stack.clear()
            self.reset('prediction pairing mismatch')
            return False
        frame = stack.pop()
        try:
            if not (frame['allowed'] and frame['solver'] and self._identity(frame)
                    and self._target(frame)
                    and self._projectile(frame['app'], frame['ship'], frame['speed']) == frame['metadata']):
                return False
            hud = self._stack('hud')
            if not hud or hud[-1] is not frame['parent']:
                return False
            r, u, native_time = frame['solver']
            native_lead = self._point(out)
            observed = _subtract_split(native_lead, frame['position_split'])
            calculated = tuple(x + v * native_time for x, v in zip(r, u))
            residual = math.hypot(*(a - b for a, b in zip(observed, calculated)))
            if residual > max(.05, .0005 * math.hypot(*calculated)):
                return False
            # Keep exactly the native lead point for projection. The copied
            # native anchor/r/u are the authority for target-plane geometry.
            frame.update(native_lead_split=native_lead, tick=self.clock())
            frame['parent']['prediction'] = frame
            return True
        except Exception:
            self.errors += 1
            return False

    def observe_manual(self, owner, out, ship, eligible):
        epoch = generation = None
        try:
            app = self._q(self.base + APP_GLOBAL)
            if not app or int(owner or 0) != app + OWNERS['aim_assist']:
                # The global native helper also serves foreign owners. Such an
                # observation cannot erase the positively owned manual reference.
                return False
            with self._lock:
                self._manual = None
                self._manual_epoch += 1
                epoch, generation = self._manual_epoch, self._generation
            if not (self.verified and eligible and ship and out
                    and local_player_ship(self.read, self.base) == ship):
                return self._manual_failed(epoch, generation, 'manual reference ineligible')
            point, tick = self._point(out), self.clock()
            if (self._q(self.base + APP_GLOBAL) != app
                    or local_player_ship(self.read, self.base) != ship):
                return self._manual_failed(epoch, generation, 'manual identity changed')
            with self._lock:
                if generation != self._generation or epoch != self._manual_epoch:
                    return False
                self._manual = {'app': app, 'ship': int(ship), 'point': point,
                                'tick': tick, 'generation': generation, 'epoch': epoch}
            return True
        except Exception:
            self.errors += 1
            return self._manual_failed(epoch, generation, 'manual reference rejected')

    def _manual_failed(self, epoch, generation, reason):
        # A positively owned known-invalid update must stop consumption now,
        # including a published prior HUD solution. A superseded callback cannot
        # invalidate a newer manual observation or its newer HUD publication.
        with self._lock:
            if (epoch is not None and epoch == self._manual_epoch
                    and generation == self._generation):
                self._latest = None
                self.state = reason
        return False

    def end_hud(self, owner):
        stack = self._stack('hud')
        if not stack or stack[-1]['owner'] != int(owner or 0):
            stack.clear()
            self._stack('predictions').clear()
            self.reset('HUD pairing mismatch')
            return False
        hud = stack.pop()
        prediction = hud['prediction']
        if not (hud['allowed'] and prediction):
            return False
        try:
            with self._lock:
                manual = self._manual
                epoch, generation, manual_epoch = self._hud_epoch, self._generation, self._manual_epoch
            now = self.clock()
            if (hud['epoch'] != epoch or hud['generation'] != generation or not manual
                    or manual['generation'] != generation or manual['app'] != hud['app']
                    or manual['epoch'] != manual_epoch
                    or manual['ship'] != hud['ship'] or not self._identity(hud)
                    or not 0 <= now - manual['tick'] <= MAX_AGE_MS
                    or not 0 <= now - prediction['tick'] <= MAX_AGE_MS
                    or not self._target(prediction)
                    or self._projectile(hud['app'], hud['ship'], prediction['speed']) != prediction['metadata']):
                return False
            r, u, native_time = prediction['solver']
            solution = target_plane_solution(prediction['position_split'], r, u, prediction['speed'],
                                             native_time, manual['point'], prediction['metadata']['life'])
            if solution is None:
                return False
            solution.update(tick=min(prediction['tick'], manual['tick']), ship=hud['ship'],
                            target=prediction['target'], app=hud['app'], epoch=epoch,
                            generation=generation, anchor_mode=prediction['anchor_mode'],
                            projectile=prediction['metadata']['projectile'],
                            descriptor=prediction['metadata']['descriptor'],
                            target_body=prediction['raw_target']['target_body'],
                            target_state=prediction['raw_target']['target_state'],
                            context=prediction['raw_target']['context'],
                            lead_split=prediction['native_lead_split'])
            with self._lock:
                if (epoch != self._hud_epoch or generation != self._generation
                        or manual_epoch != self._manual_epoch or self._manual is not manual):
                    return False
                self._latest = solution
                self.state = 'sampled'
            return True
        except Exception:
            self.errors += 1
            return False

    def sample(self, ship=None, eligible=True, mode='lead', max_age_ms=MAX_AGE_MS):
        """Copied POD only; no native memory reads on input/render consumers."""
        if not eligible or mode not in ('lead', 'lag'):
            return None
        with self._lock:
            sample = self._latest
            if not (self.verified and sample and sample['epoch'] == self._hud_epoch
                    and sample['generation'] == self._generation):
                return None
            age = self.clock() - sample['tick']
            if (not 0 <= age <= min(MAX_AGE_MS, max_age_ms)
                    or ship is not None and ship != sample['ship']):
                return None
            result = {**sample, 'age_ms': age, 'mode': mode}
        # T72's copied target clock is independent; a new unqualified HUD or
        # target disappearance cannot leave an old assist factor admitted.
        target = self.lead.target_sample(max_age_ms=MAX_AGE_MS,
                                         ship=sample['ship'], target=sample['target'])
        if target is None or any(target.get(key) != sample[key]
                                 for key in ('target_body', 'target_state', 'context')):
            return None
        latest_projectile = getattr(self.lead, 'latest', {}).get('projectile')
        if latest_projectile is not None and latest_projectile != sample['projectile']:
            return None
        return result

    def status(self):
        with self._lock:
            return {'verified': self.verified, 'state': self.state,
                    'errors': self.errors, 'has_solution': self._latest is not None,
                    'epoch': self._hud_epoch}
