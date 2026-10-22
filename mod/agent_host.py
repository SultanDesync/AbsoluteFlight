"""D21 host for a standalone bus or an explicitly constructed release adapter.

The default mode owns no game/output lane or observer. Release mode requires the
separate rotation adapter and its identity/offline/build gates. A pipe request
cannot choose the host mode, decisions, bindings, observers or output authority.
"""
import json
import os
from pathlib import Path
import threading
import time
import uuid

from agent_pipe import PROTOCOL, PipeServer, validate_name
from agent_scenarios import BudgetLedger, DecisionBounds, ScenarioRunner
from agent_device import OBSERVERS


def load_config(path, *, release=False):
    path = Path(path).resolve(strict=True)
    config = json.loads(path.read_text(encoding='utf-8'))
    allowed = {'schema', 'enabled', 'mode', 'lease_ms', 'bindings', 'decisions',
               'scenario_dir', 'ledger', 'logs', 'pipe_name'}
    if release:
        allowed |= {'preflight'}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError('unknown agent host config field')
    if config.get('schema') != 1 or config.get('enabled') is not True:
        raise ValueError('agent host is disabled; explicit development config required')
    mode = 'release-rotation' if release else 'bus-only'
    if config.get('mode') != mode:
        raise ValueError('expected ' + mode + ' mode')
    if type(config.get('lease_ms')) is not int or not 1 <= config['lease_ms'] <= 450:
        raise ValueError('lease_ms must be 1..450')
    for key in ('bindings', 'decisions', 'scenario_dir', 'ledger', 'logs'):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError('missing host path: ' + key)
        config[key] = (path.parent / config[key]).resolve()
    decisions = json.loads(config['decisions'].read_text(encoding='utf-8'))
    if not isinstance(decisions, dict) or not decisions:
        raise ValueError('trusted decisions must be an object')
    # This bus-only host cannot issue or inherit any live write decision.
    if set(decisions) != ({'D10'} if release else {'D21'}):
        raise ValueError('host decision does not match its delivery mode')
    config['decision_bounds'] = {key: DecisionBounds.from_dict(value) for key, value in decisions.items()}
    return config


def windows_kill_chord():
    import ctypes as C
    user = C.WinDLL('user32', use_last_error=True)
    user.GetAsyncKeyState.argtypes = [C.c_int]
    user.GetAsyncKeyState.restype = C.c_short
    # Ctrl+Alt+F12: terminal until the developer explicitly restarts the host.
    return lambda: all(user.GetAsyncKeyState(key) & 0x8000 for key in (0x11, 0x12, 0x7B))


class AgentHost:
    def __init__(self, device, config, *, clock=time.monotonic, kill_requested=lambda: False, release=False):
        mode = 'release-rotation' if release else 'bus-only'
        if config.get('enabled') is not True or config.get('mode') != mode:
            raise ValueError('explicit ' + mode + ' development opt-in required')
        if release and getattr(device, 'release_mode', None) != 'rotation':
            raise ValueError('qualified release device adapter required')
        self.device, self.config, self.clock = device, config, clock
        self.kill_requested = kill_requested
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.cancelled.set()
        self.session = False
        self.killed = False
        self.last_heartbeat = 0.
        self.job = None
        self.job_id = None
        self.result = None
        self.log_path = None
        self.server = None
        self.last_error = None
        self.ledger = BudgetLedger(config['ledger'])
        self.mode = mode
        self.observers = getattr(device, 'observers', OBSERVERS)
        self.name = validate_name(config.get('pipe_name') or (r'\\.\pipe\AbsoluteHOTAS-Agent-' + str(os.getpid())))

    def _log(self, value):
        if self.log_path:
            with self.log_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, separators=(',', ':'), allow_nan=False) + '\n')

    def _event(self, value):
        callback = getattr(self.device, 'event', None)
        if callback is not None:
            callback(value)
        self._log(value)

    def _expire(self, reason):
        self.cancelled.set()
        self.session = False
        self.last_error = reason
        self.device.disconnect()

    def disconnected(self):
        with self.lock:
            self._expire('client disconnected or protocol error')

    def kill(self):
        with self.lock:
            self.killed = True
            self._expire('kill hotkey')
            if self.server is not None:
                self.server.stopping.set()

    def tick(self):
        with self.lock:
            if self.kill_requested():
                self.kill()
            if self.session and self.clock() - self.last_heartbeat >= self.config['lease_ms'] / 1000.:
                self._expire('client lease expired')
            try:
                self.device.poll()
            except Exception as error:
                self._expire('device admission: ' + str(error))

    def _run(self, script, decision, job_id, cancelled):
        try:
            runner = ScenarioRunner(self.device, self.config['decision_bounds'], self.ledger,
                                    scenario_dir=self.config['scenario_dir'], observers=self.observers,
                                    cancelled=cancelled.is_set, on_event=self._event)
            result = runner.run(script, decision=decision)
            self._log({'job': job_id, 'result': result})
        except Exception as error:
            cancelled.set()
            result = {'ok': False, 'error': str(error)}
            try:
                self.device.neutral()
            except Exception as neutral_error:
                result['neutral_error'] = str(neutral_error)
        with self.lock:
            if not result.get('ok') and self.log_path:
                result['log'] = str(self.log_path)
                try:
                    result['tail'] = self.log_path.read_text(encoding='utf-8').splitlines()[-5:]
                except OSError as error:
                    result['log_error'] = str(error)
            self.result = result

    def handle(self, request):
        fields = {'hello': {'op', 'protocol'}, 'heartbeat': {'op'}, 'status': {'op'},
                  'resolve': {'op', 'action'}, 'run': {'op', 'script', 'decision'},
                  'neutral': {'op'}, 'stop': {'op'}}
        op = request.get('op')
        if op not in fields or set(request) - fields[op]:
            raise ValueError('unknown operation or request field')
        with self.lock:
            if self.killed:
                raise RuntimeError('host killed; restart required')
            alive = self.job is not None and self.job.is_alive()
            if op == 'hello':
                if request.get('protocol') != PROTOCOL:
                    raise ValueError('unsupported agent protocol')
                if alive:
                    raise RuntimeError('previous job is still stopping')
                self.device.connect()
                self.cancelled = threading.Event()
                self.session, self.last_heartbeat, self.last_error = True, self.clock(), None
                return {'ok': True, 'protocol': PROTOCOL, 'mode': self.mode,
                        'lease_ms': self.config['lease_ms'], 'delivery': self.mode != 'bus-only'}
            if op == 'status':
                return {'ok': True, 'mode': self.mode, 'session': self.session,
                        'job': self.job_id, 'running': alive, 'result': self.result,
                        'last_error': self.last_error}
            if op == 'resolve':
                action = request.get('action')
                if action is not None and (not isinstance(action, str) or len(action) > 200):
                    raise ValueError('invalid action')
                return {'ok': True, 'resolution': self.device.routing.describe(action)}
            if op in ('neutral', 'stop'):
                self._expire('client ' + op)
                return {'ok': True, 'neutral': True}
            if not self.session or self.cancelled.is_set():
                raise RuntimeError('hello required; session is disconnected')
            if self.clock() - self.last_heartbeat >= self.config['lease_ms'] / 1000.:
                self._expire('client lease expired')
                raise RuntimeError('lease expired; explicit hello required')
            if op == 'heartbeat':
                self.device.heartbeat()
                self.last_heartbeat = self.clock()
                return {'ok': True}
            if op == 'run':
                if alive:
                    raise RuntimeError('job already running')
                script, decision = request.get('script'), request.get('decision')
                if not isinstance(script, str) or not 1 <= len(script) <= 16384:
                    raise ValueError('script must be 1..16384 characters')
                if decision is not None and decision not in self.config['decision_bounds']:
                    raise ValueError('unknown trusted decision')
                self.device.begin_run()
                self.job_id, self.result = uuid.uuid4().hex, None
                logs = Path(self.config['logs'])
                logs.mkdir(parents=True, exist_ok=True)
                self.log_path = logs / (self.job_id + '.jsonl')
                with self.log_path.open('x', encoding='utf-8') as stream:
                    stream.write(json.dumps({'job': self.job_id, 'script': script, 'decision': decision}) + '\n')
                self.job = threading.Thread(target=self._run,
                                            args=(script, decision, self.job_id, self.cancelled),
                                            name='D21Scenario', daemon=True)
                self.job.start()
                return {'ok': True, 'job': self.job_id, 'running': True}
        raise ValueError('unhandled agent operation')

    def start(self):
        self.server = PipeServer(self.name, self.handle, tick=self.tick, disconnected=self.disconnected)
        self.server.start()
        return self

    def close(self):
        with self.lock:
            self._expire('host stopped')
        if self.server is not None:
            self.server.stop()
        if self.job is not None:
            self.job.join(2)
            if self.job.is_alive():
                raise RuntimeError('scenario did not stop')
        self.device.close()
