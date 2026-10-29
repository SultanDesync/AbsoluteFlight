"""Build-pinned additive pitch/yaw and per-class rotation shaping host. Default off, with native admission.

This owns the same three native sites as the finite rotation_validation addon.
The release worker runs validation first and this controller refuses that owner.
Only new physical pitch/yaw bus frames may extend a production lease. Immutable
native guards still decide admission; Python eligibility is never its proof.
"""
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import threading

from flight_shaping import shape_axis
import runtime_config
from planetary_flight import AIR_CLASSES
from hotas_settings import PROFILE_KEYS, parse_token

MAX_LEASE_MS = 50
BUILD_SHA256 = 'd393c6782d29e1915854c0d5cf42505c8093740c05dc53705dbed7e0d1d4ac37'
FIGHTER_ENDPOINTS = {'pitch_negative': 1.1641868352890015, 'pitch_positive': -1.1641868352890015,
                     'yaw_negative': -1., 'yaw_positive': 1.}
# T90: per-class rotation shaping (Flight Tuning > Rotation): top pitch/yaw/roll rate multipliers
# and one turn-response (gain) multiplier. Same seam and gates as additive; any Newtonian class.
# Corvette (10) since 2026-10-22 (user), except while its autopilot flies (runtime.corvette_autopilot).
ROTATION_CLASSES = (1, 2, 3, 4, 6, 7, 8, 9, 10)
ROTATION_RANGE = (.5, 2.)
ROTATION_FIELDS = {'pitch': 'pitch_rate', 'yaw': 'yaw_rate', 'roll': 'roll_rate', 'response': 'turn_response'}
NATIVE_ROTATION = {key: 1. for key in ROTATION_FIELDS}
DEFAULT_REQUEST = {'additive': True, 'rotation': NATIVE_ROTATION, 'air': False}
OPT_IN_KEYS = ('additive_pitch_yaw', *ROTATION_FIELDS.values(), 'air_nose_authority', 'air_slip_authority')
# T92: the per-frame air terms are ignored once older than this (fall back to none).
AIR_STALE_MS = 250
NO_AIR = (1., 1., 0., 0.)  # scale, damping, pitch sway, yaw sway
# Weathervane sway (T92): observed-rate signs that turn the nose up and right. Native forms observed
# pitch/yaw/roll as the body angular velocity on matrix rows 0/1/2 (0x175DD25..0x175DE42, build
# 180836) and the ship triad is right-handed (right x up = forward), so pitch and yaw rates move the
# nose with opposite signs. Flight 2026-10-19 (user): with the first signs (+1, -1) the yaw sway
# pushed the nose further off the flight path and wound into a spin, so the absolute sign was the other
# one; the handedness result flips both together. CONFIRMED yaw by flight, pitch by handedness (to fly).
NOSE_UP_PITCH, NOSE_RIGHT_YAW = -1., 1.
SWAY_MAX = 1.  # rad/s; the native MaxTorque box clamp still bounds the resulting torque
# Buffet (user 2026-10-19): at a bad aerodynamic angle in fast thick air the nose shakes, so holding it
# there is work. Starts at BUFFET_SLIP of slip and is full at the full-slip angle; two incommensurate
# tones per axis so it never settles into a visible rhythm.
BUFFET_SLIP, BUFFET_FULL = math.radians(10.), math.radians(35.)
BUFFET_TONES = ((6.3, .0, 1.), (10.7, 1.3, .6))  # Hz, phase, weight
RETAINED = []  # native code/data must survive an in-flight callback and hot reload


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def settings_fingerprint(runtime):
    """Pin persisted meaning, independent of the currently edited class or opt-in."""
    store = runtime.store
    return _hash({'values': {k: v for k, v in store.values.items() if k not in PROFILE_KEYS},
                  'profiles': {k: {n: v for n, v in p.items() if n not in OPT_IN_KEYS}
                               for k, p in store.profiles.items()},
                  'buttons': store.buttons, 'modifier': store.button_modifier})


def source_snapshot(runtime):
    """Return exact calibration/generation pins and actual device-frame ordinals.

    No host loop counter is a device sample. Repeated held input remains valid
    when the bus publishes new samples, but reading one sample twice never renews.
    """
    sources, frames, ages = {}, {}, []
    for axis in ('pitch', 'yaw'):
        token = runtime.store.values.get('bind-' + axis + '-axis')
        if token is None or runtime.table.unit(token) is None:
            raise ValueError(axis + ' device missing, stale or unbound')
        guid, index = parse_token(token)
        d = runtime.table.devices[guid]
        sequence, generation = d['sequence'], d['generation']
        if any(type(n) is not int or n <= 0 for n in (sequence, generation)):
            raise ValueError(axis + ' needs positive actual bus sequence/generation')
        age = runtime.table.now() - runtime.table.changed.get(guid, -1e9)
        if not math.isfinite(age) or not 0 <= age < MAX_LEASE_MS / 1000.:
            raise ValueError(axis + ' actual input older than 50 ms')
        sources[axis] = {'binding': token, 'generation': generation,
                         'minimum': d['axis_minimum'][index],
                         'maximum': d['axis_maximum'][index]}
        frames[axis] = (generation, sequence)
        ages.append(age)
    return _hash(sources), frames, max(ages)


def capture_identity(runtime):
    """Read current-process pins after offline preflight; never attach or write settings."""
    from native_roll_patch import K
    from native_rotation_trial import EXPECTED_SHA, read_engine_pins, read_mode_pins
    bridge = runtime.newtonian
    base = int(runtime.pm.process_base.lpBaseOfDll)
    return {'exe_sha256': EXPECTED_SHA, 'file_version': '183877',
            'steam_buildid': '26295378', 'process_id': os.getpid(),
            'fixture': _copy(bridge.fixture),
            'engine_pins': read_engine_pins(runtime.pm, bridge.fixture['ship']),
            'mode_pins': read_mode_pins(runtime.pm, base),
            'native_addresses': {'newtonian_data': bridge.data, 'frame': runtime.frame,
                                 'mailbox': runtime.box.address,
                                 'pilot_deadline': C.addressof(runtime.block) + 24,
                                 'tick': C.cast(K.GetTickCount64, C.c_void_p).value}}


def _rotation_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 1.
    return min(ROTATION_RANGE[1], max(ROTATION_RANGE[0], float(value)))


def rotation_request(profile, ship_class, air_flight=False):
    """What the class profile asks the seam for, or None for native rotation.

    air_flight: Sultan atmosphere flight is selected for a class with an air style (T88), so a nose
    authority below 1 needs the seam even when the class keeps native rotation rates (Shuttle).
    """
    additive = profile.get('additive_pitch_yaw', False) is True and ship_class == 2
    rotation = {key: _rotation_value(profile.get(field, 1.)) for key, field in ROTATION_FIELDS.items()}
    def below_one(key):
        value = profile.get(key, 1.)
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value < 1.
    weathervane = profile.get('air_weathervane', 0.)
    swaying = not isinstance(weathervane, bool) and isinstance(weathervane, (int, float)) and weathervane > 0.
    # Nose loss (dynamic pressure, or sideslip) or the weathervane sway needs the air terms on the seam.
    air = bool(air_flight and (below_one('air_nose_authority') or below_one('air_slip_authority') or swaying))
    if not additive and rotation == NATIVE_ROTATION and not air:
        return None
    return {'additive': additive, 'rotation': rotation, 'air': air}


def _number(value, default):
    ok = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    return float(value) if ok else default


def air_state(runtime, now_ms, profile=None, stick=(0., 0.)):
    """This frame's (scale, damping, pitch sway, yaw sway) from the planetary consumer state; NO_AIR when
    absent or older than AIR_STALE_MS.

    Weathervane: the air turns the nose toward the flight path at weathervane*slip_pressure per unit of
    slip direction (1/s, so a small slip closes at that rate). Sway damping: in fast air the native
    controller keeps only damping = 1-(1-sway_damping)*slip_pressure of its grip while the stick is
    centred, so a low value lets the nose wallow past the path and back; stick deflection restores the
    full grip. With the stick centred the nose then obeys theta'' = -G*(W*p*sin(theta) + damping*theta'), G the
    native rate gain, so the swing's damping ratio is damping*sqrt(G/(W*p))/2: low wallows, 1 settles.
    """
    consumer = getattr(runtime, 'flight_consumer', None)
    latest = getattr(consumer, 'latest', None) or {}
    planetary = latest.get('planetary') if latest.get('admitted') else None
    if not planetary:
        return NO_AIR
    tick = planetary.get('tick')
    if isinstance(tick, bool) or not isinstance(tick, int) or not 0 <= now_ms - tick < AIR_STALE_MS:
        return NO_AIR
    value = planetary.get('nose_authority')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return NO_AIR
    scale = min(1., max(0., float(value)))
    profile = profile or {}
    weathervane = max(0., _number(profile.get('air_weathervane', 0.), 0.))
    pressure = min(1., max(0., _number(planetary.get('slip_pressure', 0.), 0.)))
    centred = 1. - min(1., max(abs(_number(v, 0.)) for v in stick))
    sway_damping = min(1., max(0., _number(profile.get('air_sway_damping', 1.), 1.)))
    damping = 1. - (1. - sway_damping) * pressure * centred
    # Past the terminal speed the air fights harder (user 2026-10-19); the consumer computes the multiplier.
    over = max(1., _number(planetary.get('overspeed_multiplier', 1.), 1.))
    rate = weathervane * pressure * over
    pitch = NOSE_UP_PITCH * rate * _number(planetary.get('slip_up', 0.), 0.)
    yaw = NOSE_RIGHT_YAW * rate * _number(planetary.get('slip_right', 0.), 0.)
    buffet = max(0., _number(profile.get('air_buffet', 0.), 0.))
    slip = _number(planetary.get('slip', 0.), 0.)
    if buffet > 0. and slip > BUFFET_SLIP:
        amount = buffet * pressure * over * min(1., (slip - BUFFET_SLIP) / (BUFFET_FULL - BUFFET_SLIP))
        t = now_ms / 1000.
        def tone(offset):
            return sum(w * math.sin(2. * math.pi * f * t + p + offset) for f, p, w in BUFFET_TONES) / 1.6
        pitch += amount * tone(0.)
        yaw += amount * tone(2.1)
    clamp = lambda v: min(SWAY_MAX, max(-SWAY_MAX, v))
    return scale, damping, clamp(pitch), clamp(yaw)


def get_runtime_profile(identity, request=None):
    """Measured full-rate Fighter endpoints and/or per-class rotation shaping on this build.

    The environment preflight hashes the actual executable; native preparation
    checks all three original signatures and invocation guards pin current data.
    Engine strengths remain native response gains, not endpoint multipliers; shaping
    multiplies native targets and that gain, never the MaxTorque clamp.
    """
    request = DEFAULT_REQUEST if request is None else request
    if (identity.get('exe_sha256') != BUILD_SHA256 or identity.get('file_version') != '183877'
            or identity.get('steam_buildid') != '26295378'):
        raise ValueError('unsupported additive rotation game build')
    ship_class = identity.get('fixture', {}).get('ship_class')
    if request['additive'] and ship_class != 2:
        raise ValueError('additive pitch/yaw endpoints available for Fighter only')
    if ship_class not in ROTATION_CLASSES:
        raise ValueError('native rotation for this ship class')
    rotation = request['rotation']
    if set(rotation) != set(NATIVE_ROTATION) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            or not ROTATION_RANGE[0] <= v <= ROTATION_RANGE[1] for v in rotation.values()):
        raise ValueError('rotation shaping values must be 0.5..2.0')
    air = request.get('air', False) is True
    if not request['additive'] and rotation == NATIVE_ROTATION and not air:
        raise ValueError('nothing to apply; native rotation')
    engine = identity.get('engine_pins', {})
    if (set(engine) != {'engine_snapshot_hex', 'selected_control', 'max_torque_bits'}
            or len(bytes.fromhex(engine['engine_snapshot_hex'])) != 0x78
            or type(engine['selected_control']) is not int or engine['selected_control'] <= 0
            or type(engine['max_torque_bits']) is not int):
        raise ValueError('unsupported additive rotation engine data')
    torque = struct.unpack('<f', struct.pack('<I', engine['max_torque_bits']))[0]
    if not math.isfinite(torque) or torque <= 0:
        raise ValueError('unsupported additive rotation native MaxTorque')
    return {'endpoints': dict(FIGHTER_ENDPOINTS) if request['additive'] else None,
            'rotation': {key: float(value) for key, value in rotation.items()}, 'air': air,
            'max_input_amplitude': 1.}


def pins_changed(current, pinned, planetary=False):
    """Whether the native route or engine identity moved under an installed seam.

    In planetary flight (T92) native blends the effective engine block every frame, so only the
    selected controls and MaxTorque are identity; the cave skips the snapshot under the same switch.
    """
    if current['mode_pins'] != pinned['mode_pins']:
        return True
    keys = ('selected_control', 'max_torque_bits') if planetary else tuple(pinned['engine_pins'])
    return any(current['engine_pins'].get(key) != pinned['engine_pins'].get(key) for key in keys)


def _bridge(runtime, identity, profile):
    from native_rotation_trial import ProductionRotationBridge
    addresses = identity['native_addresses']
    return ProductionRotationBridge(
        runtime.pm, identity['process_id'], int(runtime.pm.process_base.lpBaseOfDll),
        addresses['newtonian_data'], addresses['frame'], addresses['mailbox'],
        addresses['pilot_deadline'], addresses['tick'], identity['fixture'],
        signed_endpoints=profile['endpoints'], exe_sha256=identity['exe_sha256'],
        mode_pins=identity['mode_pins'], engine_pins=identity['engine_pins'],
        rotation=profile.get('rotation', NATIVE_ROTATION),
        # T92: the Newtonian bridge's T88 PLANETARY switch also opens this seam in air.
        allow_planetary_flight=bool(getattr(runtime.newtonian, 'allow_planetary_flight', False)),
        air_authority=profile.get('air', False) is True)


class AdditiveRotation:
    """Renewable host; each native bridge retains one immutable fixture."""
    def __init__(self, data, *, bridge_factory=_bridge,
                 identity_reader=capture_identity, profile_reader=get_runtime_profile, environment=None):
        self.data = Path(data)
        self.bridge_factory, self.identity_reader, self.profile_reader = bridge_factory, identity_reader, profile_reader
        self.environment = environment
        self.executable_checked = False
        self.bridge = None
        self.lock = threading.RLock()
        self.publication_lock = threading.RLock()  # never spans a code-write wait
        self.revision = 0
        self.stopped = threading.Event()
        self.state, self.reason = 'off', 'disabled for this ship class'
        self.identity = self.fixture_owner = self.settings_pin = self.source_pin = None
        self.last_frames = None
        self.last_publication = None
        self.waiting_fresh = True
        self.profile = self.request = None
        self.restore_result = None
        self.native = None
        self.last_record = 0
        self.monitoring = False
        self.refresh_count = self.last_pin_check = None
        self.air_terms = None

    def _disable(self, reason):
        with self.publication_lock:
            if self.bridge is not None and self.bridge.installed and not self.stopped.is_set():
                self.bridge.publish(enabled=False)
            self.last_publication = None
            self.native = None
            self.monitoring = False
            self.state, self.reason = 'native fallback', reason
            if self.bridge is not None and self.bridge.installed:
                # The torque observer still produces receipts with substitution
                # disabled. Native-only gaps carry no production coverage claim.
                self._read_native(0)

    def disarm(self, reason='settings changed'):
        """Immediate data-only revocation; safe while the runtime metadata lock is held."""
        with self.publication_lock:
            self.revision += 1
            self.waiting_fresh = True
            self._disable(reason)

    @staticmethod
    def _trial_enabled(runtime):
        trial = getattr(runtime, 'rotation_validation', None)
        return bool(trial is not None and
                    (getattr(trial, 'plan', {}).get('enabled') or
                     (getattr(trial, 'bridge', None) is not None and
                      getattr(trial, 'state', None) not in ('disabled', 'stopped', 'complete'))))

    def _initialize(self, runtime, source_pin, request=DEFAULT_REQUEST):
        from rotation_validation import current_executable
        if not self.executable_checked:  # multiplayer may be on (D24)
            (self.environment or current_executable)()
            self.executable_checked = True
        settings_pin = settings_fingerprint(runtime)
        try:
            identity = self.identity_reader(runtime)
            profile = self.profile_reader(identity, request)
        except Exception as error:
            self.state, self.reason = 'native fallback', str(error)
            return False
        if self.stopped.is_set():
            return False
        self.identity, self.settings_pin, self.source_pin = _copy(identity), settings_pin, source_pin
        self.fixture_owner = runtime.newtonian
        self.refresh_count = getattr(runtime, 'newtonian_refresh_count', 0)
        self.profile, self.request = _copy(profile), _copy(request)
        self.bridge = self.bridge_factory(runtime, self.identity, self.profile)
        RETAINED.append(self.bridge)
        self.bridge.prepare()
        if self.stopped.is_set():
            self.bridge.stop()
            return False
        self.bridge.install(runtime._write_code)
        self.waiting_fresh = True
        return not self.stopped.is_set()

    def _replace_fixture(self, runtime):
        """Called on the worker after Newtonian's idle-boundary refresh."""
        self.disarm('native fixture or engine changed; waiting for fresh input')
        self.bridge.stop()
        self.restore_result = self.bridge.restore(runtime._write_code)
        if not isinstance(self.restore_result, dict) or any(v is not True for v in self.restore_result.values()):
            raise RuntimeError('production rotation restoration not verified')
        self.bridge = self.identity = self.profile = self.request = self.fixture_owner = None
        self.last_record, self.last_pin_check = 0, None

    def _read_native(self, now):
        rows, sequence, dropped = self.bridge.read_records(self.last_record)
        self.last_record = sequence
        if dropped and self.monitoring:
            raise RuntimeError('production native receipt overflow')
        if not self.monitoring:
            self.native = None
            return
        if rows:
            row = rows[-1]
            self.native = {key: row[key] for key in ('invocation', 'override_applied', 'admitted',
                           'fresh', 'qualified', 'complete', 'tick_ms') if key in row}
        if self.native is not None and not 0 <= now - self.native.get('tick_ms', -MAX_LEASE_MS) < MAX_LEASE_MS:
            self.native = None

    def step(self, runtime, values, *, reason=None, now_ms=None):
        with self.lock:
            if self.stopped.is_set():
                return
            revision = self.revision
            try:
                fixture = getattr(runtime, 'newtonian', None)
                ship_class = fixture.fixture['ship_class'] if fixture is not None else runtime.store.current_class
                class_profile = runtime.store.profile(ship_class)
                air_flight = (runtime.store.values.get('atmosphere-model') == 'sultan'
                              and ship_class in AIR_CLASSES)
                request = rotation_request(class_profile, ship_class, air_flight)
                selected = class_profile.get('additive_pitch_yaw', False) or request is not None
                if self._trial_enabled(runtime):
                    if self.bridge is not None:
                        raise RuntimeError('finite rotation validation owns the native sites')
                    self.state, self.reason = 'native fallback', 'finite rotation validation selected'
                    return
                if not selected:
                    self.waiting_fresh = True
                    self._disable('disabled for this ship class')
                    self.state = 'off'
                    return
                if request is None:  # additive selected on a class without endpoints
                    if self.bridge is not None:
                        self._replace_fixture(runtime)
                    self._disable('additive pitch/yaw endpoints available for Fighter only')
                    return
                if ship_class not in ROTATION_CLASSES:
                    if self.bridge is not None:
                        self._replace_fixture(runtime)
                    self._disable('native rotation for this ship class')
                    return
                if self.bridge is not None and request != self.request:
                    # Endpoints and scale are compiled into the cave; rebuild on change.
                    self._replace_fixture(runtime)
                    self._disable('rotation settings changed; waiting for fresh input')
                    return
                eligible = (reason is None and not getattr(runtime, 'corvette_autopilot', False) and runtime.active and not runtime.stopping.is_set()
                            and runtime._focused() and runtime._frontend() == 0
                            and not runtime.menu_open and runtime.capture is None
                            and getattr(runtime, 'pilot_recovery', None) is None
                            and runtime.store.values.get('flight-controls-enabled') is True
                            and runtime.store.values.get('flight-model') == 'sultan'
                            and fixture is not None and fixture.installed
                            and runtime.patches is not None
                            and getattr(runtime, 'agent_test_owns', False) is False)
                if not eligible:
                    self.waiting_fresh = True
                    self._disable(reason or 'flight not eligible')
                    return
                try:
                    source_pin, frames, age = source_snapshot(runtime)
                except ValueError as error:
                    self.waiting_fresh = True
                    self._disable(str(error))
                    return
                if self.bridge is not None:
                    changed = (fixture is not self.fixture_owner or _copy(fixture.fixture) != self.identity['fixture']
                               or getattr(runtime, 'newtonian_refresh_count', 0) != self.refresh_count)
                    now = int(now_ms) if now_ms is not None else int(self.bridge._now())
                    if not changed and (self.last_pin_check is None or now - self.last_pin_check >= 2000):
                        try:
                            current = self.identity_reader(runtime)
                        except Exception as error:
                            self.disarm(str(error))
                            return
                        planetary = (getattr(self.bridge, 'allow_planetary_flight', False) is True
                                     and getattr(runtime, 'planetary_on', False) is True)
                        changed = pins_changed(current, self.identity, planetary)
                        self.last_pin_check = now
                    if changed:
                        self._replace_fixture(runtime)
                        return
                if self.bridge is None:
                    if self._initialize(runtime, source_pin, request):
                        # Hashing/preparation and serialized code writes may
                        # outlive this bus sample. Poll again on the next tick.
                        self._disable('waiting for fresh pitch/yaw bus frame after install')
                    return
                if revision != self.revision or self.stopped.is_set():
                    return
                settings_pin = settings_fingerprint(runtime)
                if source_pin != self.source_pin or settings_pin != self.settings_pin:
                    self.disarm('source or settings changed; waiting for fresh input')
                    self.source_pin, self.settings_pin = source_pin, settings_pin
                    self.last_frames = frames
                    return
                now = int(now_ms) if now_ms is not None else int(self.bridge._now())
                if self.last_pin_check is None:
                    self.last_pin_check = now
                self._read_native(now)
                if self.last_frames is not None and any(
                        frames[axis][1] < self.last_frames[axis][1] for axis in frames):
                    self.disarm('actual input sequence regressed; waiting for fresh input')
                    self.last_frames = frames
                    return
                if not all(runtime.control.armed.get(axis) for axis in ('pitch', 'yaw')):
                    self.waiting_fresh = True
                    self._disable('pitch/yaw input disarmed')
                    return
                actual = [shape_axis(runtime.store.values, axis, runtime.table.unit(
                    runtime.store.values['bind-' + axis + '-axis'])) for axis in ('pitch', 'yaw')]
                if any(value is None for value in actual):
                    self.waiting_fresh = True
                    self._disable('pitch/yaw source expired; waiting for fresh input')
                    return
                if self.waiting_fresh:
                    # A new device frame on both axes, from wherever the stick is. A centre
                    # requirement left class rotation native after every menu or dialog until
                    # pitch and yaw were both centred (user, 2026-10-19).
                    if (self.last_frames is not None and
                            any(frames[axis] == self.last_frames[axis] for axis in frames)):
                        self._disable('waiting for fresh pitch/yaw frame')
                        return
                    self.waiting_fresh = False
                    self.last_frames = None
                if self.last_frames is not None:
                    if any(frames[axis] == self.last_frames[axis] for axis in frames):
                        if self.last_publication is not None and now - self.last_publication >= MAX_LEASE_MS:
                            self.waiting_fresh = True
                            self._disable('waiting for fresh pitch/yaw bus frames')
                        return
                # The mailbox contains these shaped lanes already. Native consumes
                # that atomic generation; this check cannot create native authority.
                if len(values) != 6 or any(not math.isfinite(v) for v in values):
                    raise ValueError('finite six-axis publication required')
                if any(abs(v) > self.profile['max_input_amplitude'] for v in values[:2]):
                    raise ValueError('published pitch/yaw exceeds full input')
                lease = min(MAX_LEASE_MS, max(1, int(MAX_LEASE_MS - age * 1000)))
                with self.publication_lock:
                    if revision != self.revision or self.stopped.is_set():
                        return
                    if self.profile.get('air') is True:
                        self.air_terms = self.bridge.set_air(*air_state(runtime, now, class_profile, values[:2]))
                    self.bridge.publish(enabled=True, now_ms=now, lease_ms=lease)
                    self.last_frames, self.last_publication = frames, now
                    self.monitoring = True
                    self.state, self.reason = 'armed', 'native admission required'
            except Exception as error:
                self.reason = str(error)
                self.shutdown(runtime._write_code, 'native fallback')

    def status(self):
        request = self.request or {}
        rotation = request.get('rotation', NATIVE_ROTATION)
        parts = [name for name, on in (('additive', request.get('additive')),
                                       ('shaping', rotation != NATIVE_ROTATION),
                                       ('air', request.get('air'))) if on]
        return {'state': self.state, 'reason': self.reason, 'mode': '+'.join(parts) or None,
                'rotation': dict(rotation),
                'air_authority': self.air_terms[0] if request.get('air') and self.air_terms else None,
                'air_terms': tuple(self.air_terms) if request.get('air') and self.air_terms else None,
                'runtime_profile': self.profile is not None,
                'lease_ms': MAX_LEASE_MS, 'native': self.native,
                'restore': self.restore_result}

    def shutdown(self, writer, state='stopped'):
        self.stopped.set()  # prevents installation/publication after any code-write wait
        bridge = self.bridge
        stop_error = None
        if bridge is not None:
            try:
                bridge.stop()
            except Exception as error:
                stop_error = error
        with self.lock:
            self.last_publication = None
            self.monitoring = False
            self.state = state
            if bridge is not None:
                try:
                    self.restore_result = bridge.restore(writer)
                    if not isinstance(self.restore_result, dict) or any(v is not True for v in self.restore_result.values()):
                        raise RuntimeError('production rotation restoration not verified')
                except Exception:
                    self.state = 'restore failed'
                    raise
            if stop_error is not None and self.restore_result is None:
                self.state = 'restore failed'
                raise stop_error


def worker_tick(runtime, values, *, reason=None, now_ms=None):
    addon = getattr(runtime, 'additive_rotation', None)
    if addon is None:
        root = Path(__file__).resolve().parents[1]
        addon = runtime.additive_rotation = AdditiveRotation(
            runtime_config.data_dir(root))
    addon.step(runtime, values, reason=reason, now_ms=now_ms)


def disarm(runtime, reason='settings changed'):
    addon = getattr(runtime, 'additive_rotation', None)
    if addon is not None:
        addon.disarm(reason)


def shutdown(runtime):
    addon = getattr(runtime, 'additive_rotation', None)
    if addon is not None:
        addon.shutdown(runtime._write_code)


def status(runtime):
    addon = getattr(runtime, 'additive_rotation', None)
    return addon.status() if addon is not None else {'state': 'off', 'reason': 'disabled for this ship class'}


def owns_sites(runtime):
    """Block the trial owner until production restoration has actually succeeded."""
    addon = getattr(runtime, 'additive_rotation', None)
    return bool(addon is not None and addon.bridge is not None and
                (addon.bridge.installed or addon.state == 'restore failed' or addon.restore_result is None))
