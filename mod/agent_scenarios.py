"""D21 bounded scripts, qualified observer assertions, and durable budget reservations.

Pure Python: this module neither opens the game nor adds observers. The host owns
the lease/watchdog, preflight, virtual bus backend, and trusted decision config.
All scheduled time (including waits) is reserved before the first input and is
never refunded, so a crashed/interrupted run cannot reclaim its allowance.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import threading
import time
import uuid


class ScriptError(ValueError):
    """Invalid, unauthorized, cancelled, or failed bounded work."""


def _integer(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScriptError(f'{name} must be an integer >= {minimum}')
    return value


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScriptError(f'{name} must be finite numeric data')
    return float(value)


@dataclass(frozen=True)
class DecisionBounds:
    max_axis: float
    max_step_ms: int
    max_run_ms: int
    cumulative_ms: int
    max_commands: int = 128

    def __post_init__(self):
        if not 0 <= _number(self.max_axis, 'max_axis') <= 1:
            raise ScriptError('max_axis must be between 0 and 1')
        for name in ('max_step_ms', 'max_run_ms', 'cumulative_ms', 'max_commands'):
            _integer(getattr(self, name), name)
        if self.max_step_ms > self.max_run_ms or self.max_run_ms > self.cumulative_ms:
            raise ScriptError('bounds must satisfy step <= run <= cumulative')

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ScriptError('decision bounds must be an object')
        allowed = {'max_axis', 'max_step_ms', 'max_run_ms', 'cumulative_ms', 'max_commands'}
        if set(data) - allowed:
            raise ScriptError('unknown decision bounds')
        try:
            return cls(**data)
        except TypeError as exc:
            raise ScriptError('incomplete decision bounds') from exc

    def narrowed(self, overrides):
        if not isinstance(overrides, dict):
            raise ScriptError('scenario bounds must be an object')
        if set(overrides) - {'max_axis', 'max_step_ms', 'max_run_ms', 'cumulative_ms', 'max_commands'}:
            raise ScriptError('unknown scenario bounds')
        for name, value in overrides.items():
            if _number(value, name) > getattr(self, name):
                raise ScriptError(f'scenario cannot broaden {name}')
        return replace(self, **overrides)


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_GENESIS = '0' * 64


class BudgetLedger:
    """Append-only, hash-chained JSONL. An invalid tail fails closed.

    The stable sidecar lock is shared by processes and threads. Reservations are
    fsynced while holding it before execution. An interrupted append invalidates
    the ledger instead of silently discarding already spent time.
    """
    def __init__(self, path, *, lock_timeout=2.0):
        self.path = Path(path).resolve()
        if not math.isfinite(lock_timeout) or lock_timeout <= 0:
            raise ScriptError('ledger lock timeout must be finite and positive')
        self.lock_timeout = lock_timeout
        key = os.path.normcase(str(self.path.resolve()))
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(key, threading.Lock())

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._thread_lock.acquire(timeout=self.lock_timeout):
            raise ScriptError('budget ledger busy')
        fd = None
        acquired = False
        try:
            fd = os.open(str(self.path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'0')
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ScriptError('budget ledger busy')
                    time.sleep(.01)
            yield
        finally:
            try:
                if fd is not None:
                    try:
                        if acquired:
                            os.lseek(fd, 0, os.SEEK_SET)
                            if os.name == 'nt':
                                import msvcrt
                                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
            finally:
                self._thread_lock.release()

    @staticmethod
    def _digest(record):
        payload = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def _load(self):
        if not self.path.exists():
            return [], {}
        if self.path.stat().st_size > 16 * 1024 * 1024:
            raise ScriptError('budget ledger exceeds read limit')
        data = self.path.read_bytes()
        if not data:
            raise ScriptError('existing budget ledger is empty or truncated')
        if data and not data.endswith(b'\n'):
            raise ScriptError('budget ledger is truncated')
        records, totals, run_ids = [], {}, set()
        previous = _GENESIS
        try:
            for line in data.splitlines():
                record = json.loads(line)
                if not isinstance(record, dict) or set(record) != {
                    'v', 'seq', 'decision', 'reserved_ms', 'run_id', 'prev', 'sha256'
                }:
                    raise ScriptError('invalid budget ledger record')
                digest = record['sha256']
                body = {key: value for key, value in record.items() if key != 'sha256'}
                if (type(record['v']) is not int or record['v'] != 1 or
                        type(record['seq']) is not int or record['seq'] != len(records) + 1 or
                        record['prev'] != previous or digest != self._digest(body)):
                    raise ScriptError('budget ledger chain mismatch')
                _integer(record['reserved_ms'], 'reserved_ms', 0)
                if (not isinstance(record['decision'], str) or not record['decision'] or
                        not isinstance(record['run_id'], str) or not record['run_id'] or
                        record['run_id'] in run_ids):
                    raise ScriptError('invalid budget ledger identity')
                run_ids.add(record['run_id'])
                decision = record['decision']
                totals[decision] = totals.get(decision, 0) + record['reserved_ms']
                previous = digest
                records.append(record)
        except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
            raise ScriptError('budget ledger is corrupt') from exc
        return records, totals

    def reserve(self, decision, milliseconds, limit):
        if not isinstance(decision, str) or not decision:
            raise ScriptError('decision is required')
        _integer(milliseconds, 'reservation', 0)
        _integer(limit, 'cumulative limit')
        with self._locked():
            records, totals = self._load()
            total = totals.get(decision, 0) + milliseconds
            if total > limit:
                raise ScriptError(f'{decision} cumulative budget exhausted')
            record = {'v': 1, 'seq': len(records) + 1, 'decision': decision,
                      'reserved_ms': milliseconds, 'run_id': uuid.uuid4().hex,
                      'prev': records[-1]['sha256'] if records else _GENESIS}
            record['sha256'] = self._digest(record)
            payload = (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
            # A partial write is never treated as a successful reservation.
            with self.path.open('ab', buffering=0) as stream:
                if stream.write(payload) != len(payload):
                    raise ScriptError('incomplete budget reservation')
                os.fsync(stream.fileno())
            return {'decision': decision, 'reserved_ms': milliseconds,
                    'used_ms': total, 'limit_ms': limit, 'run_id': record['run_id']}


@dataclass(frozen=True)
class _Step:
    verb: str
    token: str = ''
    value: float = 0.0
    duration_ms: int = 0
    observers: tuple = ()
    axes: tuple = ()


def _duration(text):
    match = re.fullmatch(r'(\d+(?:\.\d+)?|\.\d+)(ms|s)', text)
    if not match:
        raise ScriptError('duration requires finite positive ms or s units')
    value = float(match[1]) * (1000 if match[2] == 's' else 1)
    if not math.isfinite(value) or value <= 0 or value > 86_400_000:
        raise ScriptError('duration out of range')
    # Rounding up prevents fractional-duration budget undercounting.
    return math.ceil(value)


def _json_data(value):
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return len(value) <= 256 and all(_json_data(item) for item in value)
    if isinstance(value, dict):
        return len(value) <= 128 and all(isinstance(k, str) and _json_data(v) for k, v in value.items())
    return False


class ScenarioRunner:
    """Execute only fully validated finite scripts on a host-supplied backend.

    `cancelled()` returns true on lease loss/kill/disconnect; it is checked every
    10ms while waiting. The independent host watchdog must neutralize even when a
    backend method stalls. Backend resolve() is a pure lookup; read() may expose
    only the observer names explicitly registered by the host.

    Optional backend hooks raise to reject: validate_plan(steps, decision,
    bounds) runs before any budget is spent; admit_plan(steps, decision,
    reservation) runs after the durable reservation and before input or events.
    The frozen steps are supplied as a tuple. finish_run() always runs during
    cleanup, before neutral(), even if validation or admission failed.
    """
    def __init__(self, backend, decisions, ledger, *, scenario_dir=None,
                 observers=(), clock=time.monotonic, sleep=time.sleep,
                 cancelled=lambda: False, on_event=lambda event: None):
        self.backend = backend
        self.decisions = dict(decisions)
        if not self.decisions or any(not isinstance(v, DecisionBounds) for v in self.decisions.values()):
            raise ScriptError('trusted decision bounds required')
        self.ledger = ledger
        self.scenario_dir = Path(scenario_dir) if scenario_dir else None
        self.observers = frozenset(observers)
        self.clock, self.sleep, self.cancelled = clock, sleep, cancelled
        if not callable(on_event):
            raise ScriptError('on_event must be callable')
        self.on_event = on_event

    def _token(self, action, kind):
        token = action if '@' in action else self.backend.resolve(action)
        if not isinstance(token, str):
            raise ScriptError(f'unresolved {action}')
        token = token.lower()
        pattern = (r'virtual:agent-0@axis:0x3[0-7]' if kind == 'axis' else
                   r'virtual:agent-0@(?:button:(?:[1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])|pov:[0-3]:(?:up|right|down|left))')
        if not re.fullmatch(pattern, token):
            raise ScriptError(f'{action} is not an agent {kind} token')
        return token

    def _parse(self, script, bounds):
        if not isinstance(script, str) or not script.strip() or len(script) > 65536:
            raise ScriptError('script must contain 1..65536 characters')
        commands = script.split(';')
        # Accept a final semicolon, but no empty commands between operations.
        if not commands[-1].strip():
            commands.pop()
        if len(commands) > bounds.max_commands:
            raise ScriptError('too many commands')
        steps = []
        for command in commands:
            try:
                words = shlex.split(command)
            except ValueError as exc:
                raise ScriptError('malformed command') from exc
            if not words:
                raise ScriptError('empty command')
            verb = words[0].lower()
            if verb not in {'axis', 'rotate', 'press', 'hold', 'release', 'wait', 'read', 'neutral', 'run'}:
                # Documented terse analog action spelling: pitch .5 300ms.
                if len(words) == 3:
                    words.insert(0, 'axis')
                    verb = 'axis'
                else:
                    raise ScriptError(f'unknown command {verb}')
            if verb == 'axis' and len(words) == 4:
                try:
                    value = float(words[2])
                except ValueError as exc:
                    raise ScriptError('axis requires numeric magnitude') from exc
                if not math.isfinite(value) or abs(value) > bounds.max_axis:
                    raise ScriptError('axis magnitude exceeds decision bound')
                step = _Step(verb, self._token(words[1], 'axis'), value, _duration(words[3]))
            elif verb == 'rotate' and len(words) in (4, 5):
                if not callable(getattr(self.backend, 'axes', None)):
                    raise ScriptError('backend does not support bounded rotation vectors')
                axes = []
                for assignment in words[1:-1]:
                    if assignment.count('=') != 1:
                        raise ScriptError('rotate requires action=value assignments')
                    action, raw_value = assignment.split('=')
                    try:
                        value = float(raw_value)
                    except ValueError as exc:
                        raise ScriptError('rotate requires numeric magnitudes') from exc
                    if not math.isfinite(value) or value == 0 or abs(value) > bounds.max_axis:
                        raise ScriptError('rotation vector exceeds decision magnitude or contains zero')
                    axes.append((self._token(action, 'axis'), value))
                if len({token for token, value in axes}) != len(axes):
                    raise ScriptError('rotation vector requires distinct axes')
                step = _Step(verb, duration_ms=_duration(words[-1]), axes=tuple(axes))
            elif verb in {'press', 'hold', 'release'}:
                if len(words) not in (2, 3) or (verb == 'hold' and len(words) != 3):
                    raise ScriptError(f'{verb} requires action and finite duration')
                duration = _duration(words[2]) if len(words) == 3 else (50 if verb == 'press' else 0)
                step = _Step(verb, self._token(words[1], 'button'), duration_ms=duration)
            elif verb == 'wait' and len(words) == 2:
                step = _Step(verb, duration_ms=_duration(words[1]))
            elif verb == 'read' and len(words) >= 2:
                names = tuple(''.join(words[1:]).split(','))
                if not names or any(name not in self.observers for name in names):
                    raise ScriptError('read requires qualified observers')
                if len(set(names)) != len(names):
                    raise ScriptError('duplicate observer')
                step = _Step(verb, observers=names)
            elif verb == 'neutral' and len(words) == 1:
                step = _Step(verb)
            else:
                raise ScriptError(f'invalid {verb} command')
            if step.duration_ms > bounds.max_step_ms:
                raise ScriptError('step duration exceeds decision bound')
            steps.append(step)
        if sum(step.duration_ms for step in steps) > bounds.max_run_ms:
            raise ScriptError('run duration exceeds decision bound')
        return steps

    def _scenario(self, script, decision):
        words = shlex.split(script) if isinstance(script, str) else []
        if not words or words[0] != 'run':
            return script, decision, None, [], None
        if len(words) != 2 or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', words[1]):
            raise ScriptError('run requires one safe scenario name')
        if self.scenario_dir is None:
            raise ScriptError('scenario directory not configured')
        root = self.scenario_dir.resolve()
        path = (root / (words[1] + '.json')).resolve()
        if path.parent != root:
            raise ScriptError('scenario path escapes configured directory')
        if path.stat().st_size > 65536:
            raise ScriptError('scenario file too large')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) - {'decision', 'script', 'bounds', 'pass', 'description'}:
            raise ScriptError('invalid scenario object')
        if not {'decision', 'script', 'bounds', 'pass'} <= set(data):
            raise ScriptError('scenario must declare decision, script, bounds, and pass criteria')
        scenario_decision = data['decision']
        if decision is not None and scenario_decision != decision:
            raise ScriptError('scenario decision differs from requested decision')
        criteria = data['pass']
        if not isinstance(criteria, list) or not criteria or len(criteria) > 128:
            raise ScriptError('scenario requires finite pass criteria')
        for criterion in criteria:
            if (not isinstance(criterion, dict) or set(criterion) - {'observer', 'min', 'max', 'equals'} or
                    criterion.get('observer') not in self.observers or len(criterion) < 2):
                raise ScriptError('pass criterion requires a qualified observer and bound')
            for key in ('min', 'max'):
                if key in criterion:
                    _number(criterion[key], key)
            if ('min' in criterion and 'max' in criterion and criterion['min'] > criterion['max']):
                raise ScriptError('pass criterion min exceeds max')
            if 'equals' in criterion and not _json_data(criterion['equals']):
                raise ScriptError('pass criterion equals must be finite JSON data')
        return data['script'], scenario_decision, data['bounds'], criteria, words[1]

    def run(self, script, decision=None):
        started = self.clock()
        result = {'ok': False}
        trace = []
        try:
            if not isinstance(script, str) or not script.strip() or len(script) > 65536:
                raise ScriptError('script must contain 1..65536 characters')
            script, decision, overrides, criteria, scenario = self._scenario(script, decision)
            if not isinstance(decision, str) or decision not in self.decisions:
                raise ScriptError('explicit trusted decision required')
            bounds = self.decisions[decision]
            if overrides is not None:
                bounds = bounds.narrowed(overrides)
            steps = tuple(self._parse(script, bounds))
            self._check_cancelled()
            validate_plan = getattr(self.backend, 'validate_plan', None)
            if validate_plan is not None:
                validate_plan(steps, decision, bounds)
            self._check_cancelled()
            result['budget'] = self.ledger.reserve(decision, sum(s.duration_ms for s in steps), bounds.cumulative_ms)
            self._check_cancelled()
            admit_plan = getattr(self.backend, 'admit_plan', None)
            if admit_plan is not None:
                admit_plan(steps, decision, result['budget'])
            self._check_cancelled()
            if scenario:
                result['scenario'] = scenario
                result['verdict'] = 'error'
            deadline = self.clock() + bounds.max_run_ms / 1000
            readings = {}
            for index, step in enumerate(steps):
                self._check_cancelled(deadline)
                trace.append({'step': index + 1, 'verb': step.verb})
                event = {'event': 'step_start', 'step': index + 1,
                         'verb': step.verb, 'duration_ms': step.duration_ms}
                if step.token:
                    event['token'] = step.token
                if step.verb == 'axis':
                    event['value'] = step.value
                if step.observers:
                    event['observers'] = list(step.observers)
                if step.axes:
                    event['axes'] = [list(pair) for pair in step.axes]
                self.on_event(event)
                self._check_cancelled(deadline)
                if step.verb == 'axis':
                    try:
                        self.backend.axis(step.token, step.value)
                        self._wait(step.duration_ms, deadline)
                    finally:
                        self.backend.axis(step.token, 0.0)
                elif step.verb == 'rotate':
                    try:
                        self.backend.axes(step.axes)
                        self._wait(step.duration_ms, deadline)
                    finally:
                        self.backend.axes(tuple((token, 0.) for token, value in step.axes))
                elif step.verb in ('press', 'hold'):
                    try:
                        self.backend.button(step.token, True)
                        self._wait(step.duration_ms, deadline)
                    finally:
                        self.backend.button(step.token, False)
                elif step.verb == 'release':
                    self.backend.button(step.token, False)
                    self._wait(step.duration_ms, deadline)
                elif step.verb == 'wait':
                    self._wait(step.duration_ms, deadline)
                elif step.verb == 'neutral':
                    self.backend.neutral()
                elif step.verb == 'read':
                    readings.update(self._read(step.observers))
                self._check_cancelled(deadline)
                self.on_event({'event': 'step_end', 'step': index + 1, 'verb': step.verb})
                self._check_cancelled(deadline)
            result['read'] = readings
            if criteria:
                names = tuple(dict.fromkeys(c['observer'] for c in criteria))
                readings.update(self._read(names))
                self._check_cancelled(deadline)
                failures = []
                for criterion in criteria:
                    value = readings[criterion['observer']]
                    passed = True
                    for key, comparison in (('min', lambda a, b: a >= b), ('max', lambda a, b: a <= b)):
                        if key in criterion:
                            passed = passed and comparison(_number(value, criterion['observer']), criterion[key])
                    if 'equals' in criterion:
                        passed = passed and type(value) is type(criterion['equals']) and value == criterion['equals']
                    if not passed:
                        failures.append(criterion['observer'])
                result['verdict'] = 'fail' if failures else 'pass'
                if failures:
                    raise ScriptError('pass criteria failed: ' + ','.join(failures))
            result.update(ok=True, read=readings)
        except Exception as exc:
            result['error'] = str(exc)[:512] or type(exc).__name__
            result['trace'] = trace[-16:]
        finally:
            try:
                finish_run = getattr(self.backend, 'finish_run', None)
                if finish_run is not None:
                    finish_run()
            except Exception as exc:
                result.update(ok=False, cleanup_error=(str(exc)[:256] or type(exc).__name__))
            neutral_error = None
            try:
                self.backend.neutral()
            except Exception as exc:
                neutral_error = str(exc)[:256] or type(exc).__name__
                result.update(ok=False, neutral_error=neutral_error)
            try:
                event = {'event': 'neutral', 'ok': neutral_error is None}
                if neutral_error:
                    event['error'] = neutral_error
                self.on_event(event)
            except Exception as exc:
                result.update(ok=False, log_error=(str(exc)[:256] or type(exc).__name__))
                result.setdefault('error', 'event logger failed')
            result['t'] = round(max(0.0, self.clock() - started), 4)
        return result

    def _read(self, names):
        values = self.backend.read(names)
        if not isinstance(values, dict) or set(values) != set(names) or not _json_data(values):
            raise ScriptError('observer returned invalid, missing, or unrequested data')
        self.on_event({'event': 'read', 'observers': list(names), 'values': values})
        return values

    def _check_cancelled(self, deadline=None):
        if self.cancelled():
            raise ScriptError('run cancelled or lease expired')
        if deadline is not None and self.clock() > deadline:
            raise ScriptError('run deadline exceeded')

    def _wait(self, milliseconds, deadline):
        end = self.clock() + milliseconds / 1000
        while self.clock() < end:
            self._check_cancelled(deadline)
            self.sleep(min(.01, max(0, end - self.clock())))
        self._check_cancelled(deadline)
