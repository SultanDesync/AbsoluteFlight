"""Pure D10 rotation-only ownership for the release publisher.

This module owns no game/bus handle, settings store, output lane, or thread. A
trusted host supplies durable ledger reservations and qualified preflight. The
release publisher must honor Selection.ttl_ms and validate its epoch before use.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
import math
import re
import threading
import time

from hotas_buttons import ButtonRouter
from hotas_control import FlightControl, MAILBOX_ORDER
from hotas_input import DeviceTable
from hotas_settings import lever_zone_settings
from newtonian_control import TranslationCommand, TranslationPublisher
from newtonian_runtime import FlightConsumer

DEVICE = 'virtual:agent-0'
ROTATION = ('yaw', 'pitch', 'roll')
MAX_AXIS, MAX_STEP_MS, MAX_RUN_MS = .25, 500, 10000
TOKEN = re.compile(r'virtual:agent-0@axis:0x3[0-7]$')


def _integer(value, name, low=0, high=2**63-1):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + ' is outside its integer bounds')
    return value


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + ' must be finite numeric data')
    return float(value)


@dataclass(frozen=True)
class PlanStep:
    verb: str
    token: str = ''
    value: float = 0.
    duration_ms: int = 0
    observers: tuple = ()
    axes: tuple = ()


@dataclass(frozen=True)
class Selection:
    values: tuple
    command: TranslationCommand
    consumer: FlightConsumer
    ttl_ms: int
    epoch: int
    required_mask: int = 0


@dataclass(frozen=True)
class _Run:
    epoch: int
    plan: tuple
    reservation: str
    client_deadline: int
    run_deadline: int
    next_step: int = 1
    current_step: int = 0
    step_deadline: int = 0


class ReleaseOwner:
    """Exclusive, bounded test selection; physical controls remain caller-owned.

    Events must include the activation's captured ``epoch``. On source loss,
    eligibility loss or physical intervention, ownership is terminally revoked.
    A step that outlives its declared duration produces zero until its end event;
    a heartbeat cannot extend that stimulus deadline.
    """
    def __init__(self, bindings, values, *, preflight, ticks, clock=time.monotonic, lease_ms=250,
                 on_revoke=lambda: None):
        if not callable(preflight) or not callable(ticks) or not callable(clock) or not callable(on_revoke):
            raise ValueError('preflight and monotonic clocks are required')
        _integer(lease_ms, 'lease_ms', 1, 500)
        if not isinstance(bindings, dict) or set(bindings) - {'axes', 'buttons'}:
            raise ValueError('rotation-only test bindings required')
        axes = bindings.get('axes')
        if not isinstance(axes, dict) or set(axes) != set(ROTATION) or bindings.get('buttons', []) != []:
            raise ValueError('exactly yaw/pitch/roll bindings and no buttons are allowed')
        if any(not isinstance(token, str) or not TOKEN.fullmatch(token) for token in axes.values()):
            raise ValueError('only canonical virtual rotation axis tokens are allowed')
        if len(set(axes.values())) != 3:
            raise ValueError('rotation bindings must use distinct axes')
        self.axes = dict(axes)
        self.preflight, self.ticks, self.clock, self.lease_ms = preflight, ticks, clock, lease_ms
        self.on_revoke = on_revoke
        self._lock = threading.RLock()
        self._run = None
        self._epoch = 0
        self._seen_reservations = set()
        self._reason = 'inactive'
        self._source_generation = None
        self._sample_now = 0.
        self._last_now = None
        self._last_selection = None
        self._last_selection_step = None
        self.update_values(values)

    def update_values(self, values):
        """Refresh from the user's current settings only outside test ownership.

        ``user_values`` is the untouched copied snapshot for change detection;
        ``values`` differs only in the six isolated test axis bindings.
        """
        if not isinstance(values, dict):
            raise ValueError('user settings must be an object')
        with self._lock:
            if self._run is not None:
                raise ValueError('settings cannot change during test ownership')
            original = deepcopy(values)
            selected = deepcopy(original)
            for channel in MAILBOX_ORDER:
                selected['bind-' + channel + '-axis'] = self.axes.get(channel)
            self.user_values, self.values = original, selected
            self._reset()

    def _reset(self):
        self.control = FlightControl(self.values)
        self.translation = TranslationPublisher()
        self.consumer = FlightConsumer()
        self.router = ButtonRouter()  # No digital action has live authority here.
        self.table = DeviceTable(now=lambda: self._sample_now)
        self._source_generation = None
        self._last_now = None
        self._last_selection = None
        self._last_selection_step = None

    def check_plan(self, plan):
        if not isinstance(plan, (list, tuple)) or not 1 <= len(plan) <= 128:
            raise ValueError('a finite rotation plan is required')
        checked = []
        for raw in plan:
            if isinstance(raw, dict):
                if set(raw) - {'verb', 'token', 'value', 'duration_ms', 'observers', 'axes'}:
                    raise ValueError('unknown rotation plan field')
                fields = raw
            else:
                fields = {name: getattr(raw, name, default) for name, default in
                          (('verb', None), ('token', ''), ('value', 0.), ('duration_ms', 0), ('observers', ()), ('axes', ()))}
            verb = fields.get('verb')
            token = fields.get('token', '')
            value = _number(fields.get('value', 0.), 'axis value')
            duration = _integer(fields.get('duration_ms', 0), 'duration_ms', 0, MAX_RUN_MS)
            observers = fields.get('observers', ())
            axes = fields.get('axes', ())
            if not isinstance(axes, (tuple, list)) or len(axes) > 3:
                raise ValueError('rotation vector must be a finite sequence')
            vector = []
            for pair in axes:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise ValueError('rotation vector requires token/value pairs')
                axis_token, axis_value = pair
                axis_value = _number(axis_value, 'rotation vector value')
                if axis_token not in self.axes.values() or not 0 < abs(axis_value) <= MAX_AXIS:
                    raise ValueError('rotation vector exceeds D10 rotation authority')
                vector.append((axis_token, axis_value))
            axes = tuple(vector)
            if len({pair[0] for pair in axes}) != len(axes):
                raise ValueError('rotation vector requires distinct axes')
            if not isinstance(observers, (tuple, list)):
                raise ValueError('observer names must be a finite sequence')
            if (len(observers) > 32 or any(not isinstance(name, str) or
                    not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name) for name in observers)
                    or len(set(observers)) != len(observers)):
                raise ValueError('invalid observer names')
            if verb == 'axis':
                if token not in self.axes.values() or not 1 <= duration <= MAX_STEP_MS or abs(value) > MAX_AXIS or observers or axes:
                    raise ValueError('D10 rotation stimulus exceeds its authority')
            elif verb == 'rotate':
                if token or value or observers or len(axes) not in (2, 3) or not 1 <= duration <= MAX_STEP_MS:
                    raise ValueError('invalid simultaneous rotation stimulus')
            elif verb == 'wait':
                if not duration or token or value or observers or axes:
                    raise ValueError('invalid wait step')
            elif verb == 'read':
                if token or value or duration or not observers or axes:
                    raise ValueError('read requires only host-qualified observer names')
            elif verb == 'neutral':
                if token or value or duration or observers or axes:
                    raise ValueError('invalid neutral step')
            else:
                raise ValueError('only rotation, wait, read and neutral are qualified')
            checked.append(PlanStep(verb, token, value, duration, tuple(observers), axes))
        if sum(step.duration_ms for step in checked) > MAX_RUN_MS:
            raise ValueError('D10 scheduled duration exceeds ten seconds')
        return tuple(checked)

    def activate(self, plan, decision, reservation):
        plan = self.check_plan(plan)
        if decision != 'D10':
            raise ValueError('only trusted D10 rotation authority is qualified')
        if self.values.get('flight-model') != 'vanilla':
            raise ValueError('D10 release qualification requires user-selected vanilla flight')
        if self.values.get('flight-controls-enabled') is not True:
            raise ValueError('user flight controls must already be enabled')
        if not isinstance(reservation, dict) or reservation.get('decision') != decision:
            raise ValueError('durable D10 budget reservation required')
        run_id = reservation.get('run_id')
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('durable reservation identity required')
        scheduled = sum(step.duration_ms for step in plan)
        reserved = _integer(reservation.get('reserved_ms'), 'reserved_ms')
        used = _integer(reservation.get('used_ms'), 'used_ms')
        limit = _integer(reservation.get('limit_ms'), 'limit_ms', 1)
        if reserved != scheduled or not reserved <= used <= limit:
            raise ValueError('reservation does not cover the complete plan')
        result = self.preflight()
        if result is not True and not (isinstance(result, dict) and result.get('ok') is True):
            raise ValueError('qualified release preflight failed')
        with self._lock:
            if self._run is not None or run_id in self._seen_reservations:
                raise ValueError('ownership or reservation is already consumed')
            tick = _integer(self.ticks(), 'native tick')
            self._seen_reservations.add(run_id)
            self._epoch += 1
            self._reset()
            self._run = _Run(self._epoch, plan, run_id, tick + self.lease_ms, tick + MAX_RUN_MS)
            self._reason = 'active D10 rotation'
            return self._epoch

    def renew(self):
        with self._lock:
            tick = _integer(self.ticks(), 'native tick')
            if not self._valid_owner(tick):
                self.revoke('client or run deadline expired')
                raise ValueError('release owner is not live')
            self._run = replace(self._run, client_deadline=min(tick + self.lease_ms, self._run.run_deadline))

    def revoke(self, reason):
        with self._lock:
            self._run = None
            self._last_selection = None
            self._reason = str(reason)[:256]
            self.control.disarm_all('test ownership revoked')
            self.translation.disarm('test ownership revoked')
            self.router = ButtonRouter()  # disarm() alone preserves mod toggle latches.
            # The host supplies an atomic, nonblocking tagged-mailbox stop latch.
            # Keep it ordered with publish/rearm, including repeated inactive revokes.
            self.on_revoke()

    def _valid_owner(self, tick):
        return self._run is not None and tick < min(self._run.client_deadline, self._run.run_deadline)

    def valid(self, epoch, native_tick):
        with self._lock:
            if (type(native_tick) is not int or native_tick < 0 or type(epoch) is not int
                    or not self._valid_owner(native_tick)):
                return False
            run = self._run
            if epoch != run.epoch:
                return False
            return not run.current_step or run.plan[run.current_step - 1].verb not in ('axis', 'rotate') or native_tick < run.step_deadline

    def check_write_vector(self, axes, epoch):
        """Admit only the complete immutable vector reserved for this step."""
        with self._lock:
            run = self._run
            if run is None or type(epoch) is not int or epoch != run.epoch:
                raise ValueError('stale release write epoch')
            try:
                tick = _integer(self.ticks(), 'native tick')
                if not self.valid(epoch, tick) or not run.current_step:
                    raise ValueError('release stimulus is not live')
                step = run.plan[run.current_step - 1]
                supplied = tuple((token, _number(value, 'axis write')) for token, value in axes)
                if step.verb != 'rotate' or supplied != step.axes:
                    raise ValueError('axis vector differs from reserved stimulus')
                return True
            except (TypeError, ValueError) as error:
                self.revoke(str(error))
                raise

    def check_write(self, token, value, epoch):
        """Authorize the exact nonzero raw stimulus immediately before its bus write.

        The host performs zero-valued cleanup independently, even after revocation.
        A delayed worker from an old epoch cannot invalidate a newer owner.
        """
        with self._lock:
            run = self._run
            if run is None or type(epoch) is not int or epoch != run.epoch:
                raise ValueError('stale release write epoch')
            try:
                tick = _integer(self.ticks(), 'native tick')
                value = _number(value, 'axis write')
                if not self.valid(epoch, tick) or not run.current_step:
                    raise ValueError('release stimulus is not live')
                step = run.plan[run.current_step - 1]
                if (step.verb != 'axis' or token != step.token or abs(value) > MAX_AXIS
                        or abs(value - step.value) > 1e-9):
                    raise ValueError('axis write differs from reserved stimulus')
                return True
            except ValueError as error:
                self.revoke(str(error))
                raise

    def publish(self, selection, callback):
        """Serialize an authorized selection with cancellation and epoch rearming.

        ``callback(adjusted_selection)`` must publish using a newly sampled native
        tick and its shortened TTL. Its deadline can never outlive the selection's
        original absolute deadline. An obsolete selection cannot rearm a new epoch.
        """
        if not callable(callback):
            raise ValueError('publication callback must be callable')
        with self._lock:
            tick = _integer(self.ticks(), 'native tick')
            run = self._run
            if (run is None or not isinstance(selection, Selection) or selection.epoch != run.epoch
                    or selection is not self._last_selection):
                return None
            if not self._valid_owner(tick):
                self.revoke('client or run deadline expired before publication')
                return None
            nonzero = any(selection.values)
            if nonzero and (self._last_selection_step != run.current_step
                            or not run.current_step or not self.valid(run.epoch, tick)):
                return None
            deadline = min(selection.command.deadline, run.client_deadline, run.run_deadline,
                           run.step_deadline if nonzero else selection.command.deadline)
            if deadline <= tick:
                return None
            adjusted = replace(selection, ttl_ms=min(selection.ttl_ms, deadline - tick),
                               command=replace(selection.command, deadline=deadline))
            try:
                return callback(adjusted)
            except Exception:
                self.revoke('agent publication failed')
                raise

    def step_started(self, event):
        with self._lock:
            tick = _integer(self.ticks(), 'native tick')
            run = self._run
            if not isinstance(event, dict) or not self._valid_owner(tick) or event.get('epoch') != run.epoch:
                raise ValueError('stale release step event')
            index = event.get('step')
            if type(index) is not int or index != run.next_step or run.current_step or index > len(run.plan):
                self.revoke('out-of-order step event')
                raise ValueError('out-of-order release step event')
            expected = run.plan[index - 1]
            actual = self.check_plan([{name: event.get(name, default) for name, default in
                                      (('verb', ''), ('token', ''), ('value', 0.), ('duration_ms', 0), ('observers', ()), ('axes', ()))}])[0]
            if actual != expected:
                self.revoke('step differs from reserved plan')
                raise ValueError('step differs from reserved rotation plan')
            self._run = replace(run, current_step=index,
                                step_deadline=min(tick + expected.duration_ms, run.run_deadline))

    def step_ended(self, event):
        with self._lock:
            run = self._run
            if not isinstance(event, dict) or run is None or event.get('epoch') != run.epoch:
                raise ValueError('stale release step event')
            if (event.get('step') != run.current_step or not run.current_step
                    or event.get('verb') != run.plan[run.current_step - 1].verb):
                self.revoke('out-of-order step end')
                raise ValueError('out-of-order release step end')
            self._run = replace(run, next_step=run.current_step + 1, current_step=0, step_deadline=0)

    @staticmethod
    def _physical_neutral(values, command, buttons):
        if len(values) != 6 or any(abs(_number(value, 'physical value')) > .02 for value in values) or buttons:
            return False
        if command is None:
            return True
        throttle = command.throttle
        return (abs(command.lateral) <= .02 and abs(command.vertical) <= .02
                and abs(throttle.value) <= .02 and not throttle.parking and not throttle.boost_requested
                and not throttle.speed_control)

    def frame(self, frames, physical_values, physical_translation, *, eligible, reason, now, dt,
              native_tick, physical_buttons=()):
        with self._lock:
            if self._run is None:
                return None
            try:
                now, dt = _number(now, 'now'), _number(dt, 'dt')
                _integer(native_tick, 'native tick')
                if dt < 0 or (self._last_now is not None and now < self._last_now):
                    raise ValueError('input clock moved backwards')
                self._last_now = now
                if not self._valid_owner(native_tick):
                    raise ValueError('client or run deadline expired')
                if not eligible:
                    raise ValueError(reason or 'release eligibility lost')
                if not self._physical_neutral(physical_values, physical_translation, physical_buttons):
                    raise ValueError('physical user intervention')
                self._sample_now = now
                frame = frames.get(DEVICE) if isinstance(frames, dict) else None
                self.table.update({DEVICE: frame} if frame is not None else {})
                if not self.table.fresh(DEVICE):
                    raise ValueError('virtual device missing, disconnected or stale')
                generation = frame['generation']
                if self._source_generation is not None and generation != self._source_generation:
                    raise ValueError('virtual source generation changed')
                self._source_generation = generation
                run = self._run
                step = run.plan[run.current_step - 1] if run.current_step else None
                active = step is not None and step.verb in ('axis', 'rotate') and native_tick < run.step_deadline
                outputs = self.control.step(self.table, True, min(dt, .25))
                selected = [0.] * 6
                required_mask = 0
                if active:
                    expected = dict(step.axes if step.verb == 'rotate' else ((step.token, step.value),))
                    required_mask = sum(1 << MAILBOX_ORDER.index(channel) for channel, token in self.axes.items()
                                        if token in expected and expected[token] != 0.)
                    observed_values = {}
                    complete = True
                    # Native SetAxis calls may expose partial source states.
                    # Emit zero until ALL requested values share one snapshot.
                    for channel, token in self.axes.items():
                        unit = self.table.unit(token)
                        if unit is None:
                            raise ValueError('virtual axis is unusable')
                        observed = unit * 2. - 1.
                        wanted = expected.get(token, 0.)
                        if abs(observed) > 2 / 65535 and abs(observed - wanted) > 2 / 65535:
                            raise ValueError('virtual stimulus differs from reserved step')
                        observed_values[token] = observed
                        complete = complete and abs(observed - wanted) <= 2 / 65535
                    if complete:
                        for channel, token in self.axes.items():
                            if token in expected and abs(observed_values[token]) > 2 / 65535:
                                index = MAILBOX_ORDER.index(channel)
                                maximum = min(MAX_AXIS, abs(expected[token]))
                                selected[index] = max(-maximum, min(maximum, outputs[index]))
                deadline = min(native_tick + 100, run.client_deadline, run.run_deadline,
                               run.step_deadline if active else native_tick + 100)
                ttl_ms = max(1, deadline - native_tick)
                # Vanilla-only D10 grants no translation or digital action authority.
                self.translation.step(self.table, self.values, False, native_tick,
                                      lever_zone_settings(self.values))
                command = TranslationCommand(self.translation.generation, deadline, False, reason='D10 rotation only')
                selected = Selection(tuple(selected), command, self.consumer, ttl_ms, run.epoch, required_mask)
                self._last_selection, self._last_selection_step = selected, run.current_step
                return selected
            except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
                self.revoke(str(error))
                return None

    def status(self):
        with self._lock:
            run = self._run
            return {'active': run is not None, 'epoch': self._epoch, 'reason': self._reason,
                    'mode': 'rotation-only', 'decision': 'D10', 'game_delivery': run is not None,
                    'run_id': run.reservation if run else None,
                    'client_deadline': run.client_deadline if run else None,
                    'run_deadline': run.run_deadline if run else None,
                    'step': run.current_step if run else None,
                    'step_deadline': run.step_deadline if run else None}
