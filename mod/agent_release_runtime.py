"""Opt-in T62 release adapter: D10 rotation validation, with no extra hooks.

Only the release worker selects/publishes outputs. Pipe threads control a leased
virtual source and an immutable ownership request. They never publish a mailbox,
replace user settings, deliver keys, or change flight mode.
"""
import copy
import math
from pathlib import Path
import time

from agent_device import AgentDevice, OBSERVERS
from agent_host import AgentHost, load_config, windows_kill_chord
from agent_preflight import Preflight
from agent_release import ReleaseOwner
from agent_routing import AgentRouting

RELEASE_OBSERVERS = OBSERVERS + ('delivery', 'receipt', 'preflight', 'applied_receipts', 'combined_receipts')


class ReleaseDevice(AgentDevice):
    release_mode = 'rotation'
    observers = RELEASE_OBSERVERS

    def __init__(self, bus, routing, owner, gate, observe, lease_ms=250, refresh=lambda: None):
        super().__init__(bus, routing, lease_ms)
        if lease_ms > 250:
            raise ValueError('release rotation client lease must be <=250ms')
        self.owner, self.gate, self.observe = owner, gate, observe
        self.refresh = refresh
        self.epoch = None

    def connect(self):
        self.refresh()
        self.gate.check()
        super().connect()

    def heartbeat(self):
        self.gate.check()
        super().heartbeat()
        if self.epoch is not None:
            self.owner.renew()

    def validate_plan(self, steps, decision, bounds):
        if decision != 'D10':
            raise ValueError('release rotation requires D10')
        if bounds.max_axis > .25 or bounds.max_step_ms > 500 or bounds.max_run_ms > 10000:
            raise ValueError('trusted D10 release bounds exceed the initial rotation envelope')
        self.owner.check_plan(steps)
        self.gate.check()

    def admit_plan(self, steps, decision, reservation):
        self.epoch = self.owner.activate(steps, decision, reservation)

    def event(self, event):
        if self.epoch is None:
            return
        event = dict(event, epoch=self.epoch)
        if event.get('event') == 'step_start':
            self.owner.step_started(event)
        elif event.get('event') == 'step_end':
            self.owner.step_ended(event)

    def finish_run(self):
        self.owner.revoke('run ended')
        self.epoch = None

    def axis(self, token, value):
        # Releases remain admissible after focus/lease/identity loss.
        if value != 0.:
            self.gate.check()
            if self.epoch is None:
                raise RuntimeError('no admitted release run')
            self.owner.check_write(token, value, self.epoch)
        super().axis(token, value)

    def button(self, token, down):
        raise ValueError('digital delivery is outside the release rotation lane')

    def axes(self, vector):
        """Set one reserved vector; partial native snapshots remain zero-output."""
        if not isinstance(vector, (tuple, list)) or len(vector) not in (2, 3):
            raise ValueError('rotation vector requires two or three axes')
        checked = []
        for pair in vector:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError('rotation vector requires token/value pairs')
            token, value = pair
            if (self._control(token)[0] != 'axis' or token not in self.owner.axes.values() or
                    type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError('rotation vector contains an unqualified axis or value')
            checked.append((token, float(value)))
        vector = tuple(checked)
        if len({token for token, value in vector}) != len(vector):
            raise ValueError('rotation vector requires distinct axes')
        cleanup = all(value == 0. for token, value in vector)
        with self.lock:
            if not cleanup:
                self.gate.check()
                if self.epoch is None:
                    raise RuntimeError('no admitted release run')
                self.owner.check_write_vector(vector, self.epoch)
            error = None
            for token, value in vector:
                try:
                    # Whole-vector authorization above; individual nonzero
                    # components must not enter the single-axis step checker.
                    super().axis(token, value)
                except Exception as failure:
                    error = error or failure
                    if not cleanup:
                        break
            if error is not None:
                if not cleanup:
                    try:
                        self.owner.revoke('rotation vector write failed')
                    finally:
                        for token, value in vector:
                            try:
                                super().axis(token, 0.)
                            except Exception:
                                pass  # Try every axis; terminal neutral follows.
                raise error

    def poll(self):
        if self.epoch is not None:
            try:
                self.gate.check()
            except Exception:
                self.owner.revoke('preflight lost')
                raise
        super().poll()

    def read(self, names):
        if any(name not in self.observers for name in names):
            raise ValueError('unknown release observer')
        base = tuple(name for name in names if name in OBSERVERS)
        values = super().read(base)
        if 'context' in values:
            values['context'] = {'mode': 'release-rotation', 'game_delivery': True, 'decision': 'D10'}
        sample = self.observe()
        delivery=sample.get('delivery')
        current=bool(self.epoch is not None and delivery is not None and delivery.get('epoch')==self.epoch)
        if 'delivery' in names:
            values['delivery'] = delivery if current else None
        if 'receipt' in names:
            values['receipt'] = sample.get('receipt') if current else None
        if 'preflight' in names:
            values['preflight'] = self.gate.status()
        if 'applied_receipts' in names:
            values['applied_receipts'] = delivery.get('applied_receipts',0) if current else 0
        if 'combined_receipts' in names:
            values['combined_receipts'] = delivery.get('combined_receipts',0) if current else 0
        return values

    def disconnect(self):
        self.owner.revoke('client disconnected')
        self.epoch = None
        super().disconnect()


class ReleaseIntegration:
    """Worker-owned sampling/publication plus a separate pipe/lease service."""
    def __init__(self, bus, config_path, values, *, focused, ticks, clock=time.monotonic,
                 inspect=None, kill_requested=None, stop_agent=lambda: None):
        self.config = load_config(config_path, release=True)
        if self.config['lease_ms'] > 250:
            raise ValueError('release rotation lease must be <=250ms')
        routing = AgentRouting.from_file(self.config['bindings'])
        self.values_provider = values if callable(values) else lambda: copy.deepcopy(values)
        self.clock, self.ticks = clock, ticks
        self.context = {'time': -1e9, 'admitted': False, 'reason': 'waiting for release worker'}
        self.observation = {'delivery': None, 'receipt': None}
        self.selected = False
        self.last_epoch = None
        self.last_receipt = None
        self.receipt_baseline = None
        self.applied = 0
        self.combined = 0
        self.published = 0
        self.closed = False
        self.generations = {}
        self.expected_masks = {}
        self.expected_values = {}
        self.gate = Preflight(self.config.get('preflight'), inspect=inspect, focused=focused,
                              admitted=lambda: bool(not self.closed and self.context['admitted']
                                                    and 0 <= self.clock() - self.context['time'] <= .1))
        bindings = {'axes': dict(routing.axes), 'buttons': []}
        self.owner = ReleaseOwner(bindings, copy.deepcopy(self.values_provider()), preflight=self.gate.check,
                                  clock=clock, ticks=ticks, lease_ms=self.config['lease_ms'],on_revoke=stop_agent)
        self.device = ReleaseDevice(bus, routing, self.owner, self.gate,
                                    lambda: self.observation, self.config['lease_ms'], refresh=self.refresh)
        self.host = AgentHost(self.device, self.config, clock=clock,
                              kill_requested=kill_requested or windows_kill_chord(), release=True)

    def start(self):
        self.host.start()
        return self

    def refresh(self):
        self.owner.update_values(copy.deepcopy(self.values_provider()))

    def select(self, frames, physical_values, physical_translation, *, eligible, reason,
               now, dt, native_tick, physical_buttons=(), receipt=None):
        # This context is produced exclusively by the release worker after its
        # existing native/mode gates. It cannot be supplied through the pipe.
        user_values = self.values_provider()
        if self.owner.status()['active'] and user_values != self.owner.user_values:
            self.owner.revoke('user settings changed')
        ready = bool(eligible is True and user_values.get('flight-model') == 'vanilla'
                     and user_values.get('flight-controls-enabled') is True
                     and self.owner._physical_neutral(physical_values, physical_translation, physical_buttons))
        self.context = {'time': now, 'admitted': ready, 'reason': reason or ''}
        selection = self.owner.frame(frames, physical_values, physical_translation,
                                      eligible=eligible, reason=reason, now=now, dt=dt,
                                      native_tick=native_tick, physical_buttons=physical_buttons)
        self.selected = selection is not None
        if selection is not None and selection.epoch != self.last_epoch:
            self.last_epoch = selection.epoch
            self.receipt_baseline = None if receipt is None else receipt.get('sequence')
            self.applied = self.published = self.combined = 0
            self.generations = {}
            self.expected_masks = {}
            self.expected_values = {}
        if selection is not None:
            self.published += 1
        if receipt is not None and receipt.get('sequence') != self.last_receipt:
            self.last_receipt = receipt.get('sequence')
            # A current application's own tagged command generation is required;
            # an unrelated physical receipt is never evidence of a test write.
            if (self.generations.get(receipt.get('generation')) == self.last_epoch
                    and receipt.get('sequence') != self.receipt_baseline
                    and receipt.get('mask', 0) & 0x80000000
                    and receipt.get('applied_mask', 0) & 7 and not receipt.get('conflict')):
                self.applied += 1
                expected = self.expected_masks.get(receipt.get('generation'), 0)
                expected_values = self.expected_values.get(receipt.get('generation'))
                observed_values = receipt.get('values')
                matching_values = bool(expected_values is not None and isinstance(observed_values, (list, tuple))
                    and len(observed_values) == 6 and all(type(value) in (int, float) and math.isfinite(value)
                    and abs(value - wanted) <= 1e-6 for value, wanted in zip(observed_values, expected_values)))
                if expected.bit_count() >= 2 and receipt.get('applied_mask', 0) & expected == expected and matching_values:
                    self.combined += 1
        previous = self.observation.get('delivery') or {}
        self.observation = {
            'delivery': {'owner': 'agent' if selection is not None else 'physical',
                         'epoch': None if selection is None else selection.epoch,
                         'required_mask': 0 if selection is None else selection.required_mask,
                         'values': None if selection is None else list(selection.values),
                         'ttl_ms': None if selection is None else selection.ttl_ms,
                         'published': self.published, 'applied_receipts': self.applied,
                         'combined_receipts': self.combined,
                         'generation': previous.get('generation') if selection is not None else None,
                         'time': now, 'reason': reason or ''},
            'receipt': receipt,
        }
        return selection

    def published_generation(self, generation):
        value = self.observation.get('delivery')
        if value is not None and value['owner'] == 'agent':
            self.observation = dict(self.observation, delivery=dict(value, generation=generation))
            self.generations[generation] = value['epoch']
            self.expected_masks[generation] = value['required_mask']
            self.expected_values[generation] = tuple(value['values'])
            if len(self.generations) > 256:
                oldest = next(iter(self.generations))
                del self.generations[oldest]
                self.expected_masks.pop(oldest, None)
                self.expected_values.pop(oldest, None)

    def revoke(self, reason):
        self.owner.revoke(reason)
        self.context = {'time': self.clock(), 'admitted': False, 'reason': reason}

    def status(self):
        return {'enabled': True, 'mode': 'release-rotation', 'pipe': self.host.name,
                'owner': self.owner.status(), 'preflight': self.gate.status(),
                'delivery': self.observation['delivery'], 'error': self.host.last_error}

    def close(self):
        self.closed = True
        self.revoke('release shutdown')
        self.host.close()
