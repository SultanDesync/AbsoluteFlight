"""T66 opt-in, finite live-validation addon; independent of T62's input pipe.

The release worker calls worker_tick; teardown calls shutdown before detaching
the shared Newtonian fixture. Neither function publishes inputs or modifies
settings. Missing/disabled local configuration does not allocate or patch.
"""
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import threading
import xml.etree.ElementTree as ET

import runtime_config
from rotation_sustained_envelope import ENVELOPE as SUSTAINED_ENVELOPE, freeze_windows, manifest as sustained_manifest

EXE_SHA256 = 'd1c7155764271eff0733c37d1849e685a11da3d54a41d685d7898fc0aad83a87'
CONFIG_NAME = 'rotation-validation.local.json'
RETAINED = []


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def load_plan(path):
    path = Path(path)
    if not path.exists():
        return {'enabled': False}
    return validate_plan(json.loads(path.read_text(encoding='utf-8-sig')))


def validate_plan(plan):
    if not isinstance(plan, dict) or type(plan.get('enabled')) is not bool:
        raise ValueError('T66 enabled must be an explicit boolean')
    if not plan['enabled']:
        return {'enabled': False}
    plan = json.loads(json.dumps(plan, allow_nan=False))
    allowed = {'schema', 'enabled', 'session_id', 'mode', 'duration_ms', 'lease_ms',
               'calibration', 'override_windows', 'live_authorized', 'max_input_amplitude'}
    sustained = type(plan.get('schema')) is int and plan.get('schema') == 2
    if sustained: allowed.add('validation_envelope')
    if sustained and (plan.get('validation_envelope') != SUSTAINED_ENVELOPE or plan.get('mode') != 'override'):
        raise ValueError('Schema2 requires explicit sustained override envelope')
    if set(plan) - allowed or type(plan.get('schema')) is not int or plan.get('schema') not in (1, 2):
        raise ValueError('Unknown T66 plan field/schema')
    if plan.get('live_authorized') is not True:
        raise ValueError('A declared live T66 session must be authorized before enabling')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(plan.get('session_id', ''))):
        raise ValueError('T66 needs a safe unique session id')
    if plan.get('mode') not in ('observe', 'override'):
        raise ValueError('T66 mode must be observe or override')
    for key, low, high in (('duration_ms', 1000, 30000), ('lease_ms', 1, 50)):
        if type(plan.get(key)) is not int or not low <= plan[key] <= high:
            raise ValueError('T66 invalid bounded ' + key)
    amplitude = plan.get('max_input_amplitude', .25)
    if (isinstance(amplitude, bool) or not isinstance(amplitude, (int, float))
            or not math.isfinite(amplitude) or not 0 < amplitude <= .25):
        raise ValueError('T66 first substitution authority is at most quarter axis')
    plan['max_input_amplitude'] = float(amplitude)
    windows = plan.get('override_windows', [])
    if not isinstance(windows, list) or len(windows) > (3 if sustained else 5):
        raise ValueError('T66 permits at most five declared substitution windows')
    previous_end, total = 0, 0
    for window in windows:
        if not isinstance(window, dict) or set(window) != {'start_ms', 'duration_ms'}:
            raise ValueError('T66 window requires start_ms/duration_ms')
        start, duration = window['start_ms'], window['duration_ms']
        if (type(start) is not int or type(duration) is not int or start < previous_end
                or not 1 <= duration <= (5000 if sustained else 200) or start + duration > plan['duration_ms']):
            raise ValueError('T66 windows must be ordered, disjoint and at most 200 ms')
        previous_end = start + duration
        total += duration
    if sustained:
        freeze_windows([(w['start_ms'], w['start_ms'] + w['duration_ms']) for w in windows], plan['duration_ms'])
    if total > (15000 if sustained else 1000):
        raise ValueError('T66 total substitution budget exceeds one second')
    if plan['mode'] == 'observe' and (windows or plan.get('calibration')):
        raise ValueError('Observe-only T66 cannot carry a substitution plan')
    if plan['mode'] == 'override' and (not windows or not isinstance(plan.get('calibration'), str)):
        raise ValueError('T66 substitution needs measured calibration and explicit windows')
    return plan


def settings_fingerprint(runtime):
    """Pin actual source/tuning/bindings; host-only diagnostics never form native admission."""
    return _hash(dict(runtime.store.values))


def current_executable():
    """The running NMS.exe, checked against the exact build; never opens another process."""
    import psutil
    exe = Path(psutil.Process(os.getpid()).exe())
    if exe.name.lower() != 'nms.exe' or hashlib.sha256(exe.read_bytes()).hexdigest() != EXE_SHA256:
        raise ValueError('T66 requires the exact current NMS executable')
    return exe


def offline_environment():
    """The T66 validation trial: exact executable and saved Multiplayer=false (trials stay offline, D24)."""
    exe = current_executable()
    settings = exe.parent / 'SETTINGS/GCUSERSETTINGSDATA.MXML'
    values = [p.attrib.get('value') for p in ET.parse(settings).iter('Property')
              if p.attrib.get('name') == 'Multiplayer']
    if values != ['false']:
        raise ValueError('T66 requires saved Multiplayer=false and unchanged offline play')
    return exe, settings, settings.stat().st_mtime_ns


class RotationValidation:
    """One terminal session. No reload/re-arm, input driver or settings writer."""
    def __init__(self, root, data, *, plan=None, bridge_factory=None,
                 environment=offline_environment, tick=None, mode_reader=None):
        self.root, self.data = Path(root), Path(data)
        self.plan = load_plan(self.data / CONFIG_NAME) if plan is None else validate_plan(plan)
        self.bridge_factory, self.mode_reader = bridge_factory, mode_reader
        self.sustained_windows = (freeze_windows([(w['start_ms'], w['start_ms'] + w['duration_ms']) for w in self.plan['override_windows']], self.plan['duration_ms'])
                                  if self.plan.get('schema') == 2 else None)
        self.environment, self.tick = environment, tick
        self.bridge = None
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.started = None
        self.last_sequence = 0
        self.last_status = 0
        self.records = 0
        self.settings_pin = None
        self.fixture_pin = None
        self.calibration = None
        self.metadata = None
        self.network_settings = None
        self.network_stamp = None
        self.raw_path = self.status_path = self.stop_path = None
        self.state = 'waiting' if self.plan.get('enabled') else 'disabled'
        self.error = None
        self.restore_result = None

    def _now(self):
        if self.tick is not None:
            return int(self.tick())
        from native_roll_patch import K
        K.GetTickCount64.restype = C.c_uint64
        return int(K.GetTickCount64())

    @staticmethod
    def _fixture(runtime):
        b = runtime.newtonian
        f = b.fixture
        return (id(b), b.data, f['ship'], f['body'], f['state'], f['context'], f['ship_class'])

    def _eligible(self, runtime):
        return (runtime.active and not runtime.stopping.is_set() and runtime._focused()
                and runtime._frontend() == 0
                and not runtime.menu_open and runtime.capture is None
                and getattr(runtime, 'pilot_recovery', None) is None
                and runtime.store.values.get('flight-controls-enabled') is True
                and runtime.store.values.get('flight-model') == 'sultan'
                and runtime.newtonian is not None and runtime.newtonian.installed
                and runtime.patches is not None)

    def _initialize(self, runtime):
        from native_roll_patch import K
        from native_rotation_trial import RotationBridge, read_mode_pins, read_engine_pins
        self.environment_result = self.environment()
        _, self.network_settings, self.network_stamp = self.environment_result
        self.settings_pin = settings_fingerprint(runtime)
        self.fixture_pin = self._fixture(runtime)
        bridge = runtime.newtonian
        base = int(runtime.pm.process_base.lpBaseOfDll)
        modes = (self.mode_reader or read_mode_pins)(runtime.pm, base)
        frequency = C.c_int64()
        if not K.QueryPerformanceFrequency(C.byref(frequency)) or frequency.value <= 0:
            raise RuntimeError('T66 native high-resolution clock unavailable')
        self.frequency = frequency.value
        self.metadata = {'schema_version': 1, 'file_version': '183791',
                         'steam_buildid': '26271400', 'exe_sha256': EXE_SHA256,
                         'evidence_kind': 'native_receipt', 'session_id': self.plan['session_id'],
                         'process_id': str(os.getpid()), 'ship_id': str(bridge.fixture['ship']),
                         'body_id': str(bridge.fixture['body']), 'ship_class': str(bridge.fixture['ship_class']),
                         'context_id': str(bridge.fixture['context']),
                         'settings_fingerprint': self.settings_pin, 'mode_pins': modes,
                         'qpc_frequency': self.frequency, 'mode': self.plan['mode']}
        limits = {}
        if self.sustained_windows is not None:
            self.metadata['sustained_authority'] = sustained_manifest(self.sustained_windows, self.plan['duration_ms'])
            limits['sustained_windows'] = self.sustained_windows
        if self.plan['mode'] == 'override':
            from rotation_calibration import validate_calibration
            path = Path(self.plan['calibration'])
            if not path.is_absolute(): path = self.data / path
            self.calibration = json.loads(path.read_text(encoding='utf-8-sig'))
            # Full identity/engine validation happens again against this session's
            # first complete native row before any substitution publication.
            validate_calibration(self.calibration, self.calibration['identity'])
            if self.calibration['evidence_kind'] != 'native_receipt':
                raise ValueError('Synthetic calibration cannot authorize a live T66 substitution')
            if self.calibration.get('mode_pins') != modes:
                raise ValueError('T66 native source mode differs from calibration')
            if self.calibration['max_input_amplitude'] < self.plan['max_input_amplitude']:
                raise ValueError('T66 plan exceeds calibrated test input bound')
            limits.update({axis + '_limit': self.calibration['axes'][axis]['rate_limit']
                           for axis in ('pitch', 'yaw')})
            limits.update({axis + '_input_sign': (1 if self.calibration['axes'][axis]['observed_full_input'] > 0 else -1)
                           for axis in ('pitch', 'yaw')})
            engine = self.calibration['engine_context']
            limits['engine_pins'] = {'engine_snapshot_hex': engine['engine_snapshot_hex'],
                                     'selected_control': int(engine['selected_control']),
                                     'max_torque_bits': int.from_bytes(struct.pack('<f', engine['max_torque']), 'little')}
            if read_engine_pins(runtime.pm, bridge.fixture['ship']) != limits['engine_pins']:
                raise ValueError('Current native engine differs from measured calibration')
        factory = self.bridge_factory or RotationBridge
        self.bridge = factory(runtime.pm, os.getpid(), base, bridge.data, runtime.frame,
                              runtime.box.address, C.addressof(runtime.block) + 24,
                              C.cast(K.GetTickCount64, C.c_void_p).value, bridge.fixture,
                              counter=C.cast(K.QueryPerformanceCounter, C.c_void_p).value,
                              hard_duration_ms=self.plan['duration_ms'], mode_pins=modes,
                              max_input_amplitude=self.plan['max_input_amplitude'], **limits)
        RETAINED.append(self.bridge)
        prepare_metadata = self.bridge.prepare()
        if self.stopped.is_set():
            self.bridge.stop()
            return
        logs = self.data / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        prefix = logs / ('t66-' + self.plan['session_id'])
        self.raw_path = prefix.with_suffix('.jsonl')
        self.status_path = prefix.with_suffix('.status.json')
        self.stop_path = prefix.with_suffix('.stop')
        if self.stop_path.exists():
            raise ValueError('T66 session id has a terminal stop marker; choose a new id')
        # Exclusive creation prevents accidental reuse of a previous capture.
        with self.raw_path.open('x', encoding='utf8') as output:
            output.write(json.dumps({'type': 'metadata', **self.metadata}) + '\n')
        prefix.with_suffix('.sites.json').write_text(json.dumps(prepare_metadata,
                                                               indent=2), encoding='utf8')
        self.bridge.install(runtime._write_code)
        if self.sustained_windows is not None:
            self.metadata['native_origin_tick'] = self.bridge.native_origin
            prepare_metadata['native_origin_tick'] = self.bridge.native_origin
            self.raw_path.write_text(json.dumps({'type': 'metadata', **self.metadata}) + '\n', encoding='utf8')
            prefix.with_suffix('.sites.json').write_text(json.dumps(prepare_metadata, indent=2), encoding='utf8')
        if self.stopped.is_set():
            self.bridge.stop()
            self.bridge.restore(runtime._write_code)
            return
        self.started = self.bridge.native_origin if self.sustained_windows is not None else self._now()
        self.state = 'observing'

    def _drain(self):
        rows, sequence, dropped = self.bridge.read_records(self.last_sequence)
        if dropped:
            raise RuntimeError('T66 native receipt ring overflowed')
        if rows:
            with self.raw_path.open('a', encoding='utf8') as output:
                for row in rows:
                    output.write(json.dumps({'type': 'receipt', **row}, allow_nan=False) + '\n')
            self.records += len(rows)
            self.latest = rows[-1]
        self.last_sequence = sequence

    def _lease(self, elapsed):
        if self.plan['mode'] == 'observe': return False, self.plan['lease_ms']
        windows = (self.sustained_windows if self.sustained_windows is not None else
                   tuple((w['start_ms'], w['start_ms'] + w['duration_ms']) for w in self.plan['override_windows']))
        for start, end in windows:
            if start <= elapsed < end:
                return True, min(self.plan['lease_ms'], end - elapsed)
        return False, self.plan['lease_ms']

    def step(self, runtime):
        if not self.plan.get('enabled') or self.stopped.is_set(): return
        with self.lock:
            if self.stopped.is_set(): return
            try:
                eligible = self._eligible(runtime)
                if self.bridge is None:
                    if not eligible: return
                    self._initialize(runtime)
                    if self.stopped.is_set(): return
                if not eligible or self._fixture(runtime) != self.fixture_pin:
                    raise RuntimeError('T66 flight eligibility or fixture changed; session ended')
                if settings_fingerprint(runtime) != self.settings_pin:
                    raise RuntimeError('T66 bindings or tuning changed; session ended')
                if self.network_settings.stat().st_mtime_ns != self.network_stamp:
                    raise RuntimeError('T66 saved network settings changed; session ended')
                now = self._now()
                elapsed = now - self.started
                self._drain()
                if self.stop_path.exists():
                    self.shutdown(runtime._write_code, 'stopped by marker')
                    return
                if elapsed < 0 or elapsed >= self.plan['duration_ms']:
                    self.shutdown(runtime._write_code, 'completed')
                    return
                enabled, lease = self._lease(elapsed)
                kwargs = {}
                if enabled:
                    from rotation_calibration import capture_identity, validate_calibration
                    row = getattr(self, 'latest', None)
                    if row is None or not row.get('complete'):
                        raise RuntimeError('T66 needs a complete current native calibration observer')
                    current = capture_identity(self.metadata, row)
                    validate_calibration(self.calibration, current)
                    kwargs = {axis + '_full': self.calibration['axes'][axis]['signed_full_target']
                              for axis in ('pitch', 'yaw')}
                self.bridge.publish(enabled=enabled, now_ms=now, lease_ms=lease, **kwargs)
                self.state = 'bounded substitution' if enabled else 'observing'
                self._status(now)
            except Exception as error:
                self.error = str(error)
                self.shutdown(runtime._write_code, 'failed')

    def _status(self, now):
        if self.status_path is None or now - self.last_status < 100: return
        self.last_status = now
        self.status_path.write_text(json.dumps(self.status(), indent=2), encoding='utf8')

    def status(self):
        return {'task': 'T66', 'state': self.state, 'error': self.error,
                'records': self.records, 'session_id': self.plan.get('session_id'),
                'mode': self.plan.get('mode'), 'restore': self.restore_result,
                'live_qualification': False, 'production_enabled': False,
                'validation_envelope': SUSTAINED_ENVELOPE if self.sustained_windows is not None else 'initial-200ms',
                'sustained_authority': None if self.sustained_windows is None else sustained_manifest(self.sustained_windows, self.plan['duration_ms'])}

    def shutdown(self, writer, state='stopped'):
        self.stopped.set()  # precedes lock: a delayed preparation cannot re-arm
        bridge = self.bridge
        if bridge is not None:
            try: bridge.stop()
            except Exception as error: self.error = str(error)
        with self.lock:
            self.state = state
            if bridge is not None:
                # Logging failure must never skip restoration.
                try:
                    if self.raw_path is not None: self._drain()
                except Exception as error:
                    self.error = str(error)
                try:
                    self.restore_result = bridge.restore(writer)
                except Exception as error:
                    self.error = str(error)
                    self.state = 'restore failed'
                    raise
            if self.status_path is not None:
                self.status_path.write_text(json.dumps(self.status(), indent=2), encoding='utf8')


def worker_tick(runtime):
    addon = getattr(runtime, 'rotation_validation', None)
    if addon is None:
        root = Path(__file__).resolve().parents[1]
        data = runtime_config.data_dir(root)
        try:
            addon = RotationValidation(root, data)
        except Exception as error:
            runtime.log('T66 plan rejected: ' + str(error))
            addon = RotationValidation(root, data, plan={'enabled': False})
        runtime.rotation_validation = addon
    addon.step(runtime)


def shutdown(runtime):
    addon = getattr(runtime, 'rotation_validation', None)
    if addon is not None:
        addon.shutdown(runtime._write_code)
