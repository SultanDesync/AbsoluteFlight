"""Runtime glue for button bindings (T61): routers per tick, mod and key/mouse lanes out.

The release runtime calls step() once per tick on its worker thread and reads parking for the
flight publisher. Bindings are split by output lane, each with its own eligibility:
- mod actions (parking brake, flight model, lever mode, aiming pip, head tracking) act only while flying;
- keyboard/mouse actions act whenever NMS has focus and the HOTAS menu is closed, because the quick
  menu, cockpit dialogs and the pause menu are exactly where they are needed.
step_cursor() moves the mouse cursor from pitch/yaw while a frontend page is open (mod/stick_cursor.py).
Settings changes requested by buttons are returned by take_changes() so the runtime can apply them
outside its own lock: the settings listener takes that lock again. Native actions wait for T67.
"""
from dataclasses import replace

from hotas_buttons import ButtonRouter, ButtonState
from hotas_settings import FLIGHT_MODELS
from stick_cursor import StickCursor, cursor_mode, cursor_reason

# Mod actions the release runtime acts on today (same list as hotas_menu.LIVE_MOD_ACTIONS).
LIVE_MOD_ACTIONS = ('mod.parking-brake', 'mod.gear', 'mod.boost', 'mod.trim-up', 'mod.trim-down', 'mod.trim-hover', 'mod.trim-reset',
                    'mod.flight-model-next', 'mod.flight-model-toggle', 'mod.lever-mode-toggle',
                    'mod.pip-toggle',
                    'mod.head-tracking-toggle', 'mod.head-recentre', 'mod.head-tracking-on', 'mod.head-tracking-off',
                    'mod.eject')
KEY_LANES = ('key', 'mouse')
# Runtime disarm reasons under which mod actions still act. A receipt stall disarms the axes, but the
# buttons stay live (alpha.18, 2026-10-23: braked to a stop, the game's flight updates stalled for up to
# 39 s and every press of the toggled parking brake was dropped, so it could not be released).
BUTTONS_LIVE_REASONS = (None, 'native receipts stale')


def flight_buttons_live(reason):
    return reason in BUTTONS_LIVE_REASONS
EMPTY = ButtonState(frozenset(), (), frozenset())


def is_key_action(action_id):
    return action_id.startswith(('key.', 'mouse.'))


class ButtonActions:
    def __init__(self, store, key_lane=None):
        self.store = store
        self.flight = ButtonRouter()
        self.keys = ButtonRouter()
        self.key_lane = key_lane  # mod/key_lane.KeyLane, or None (tests, no Win32)
        self.state = EMPTY
        self.key_state = EMPTY
        self.changes = []
        self.cursor = StickCursor()
        self.frontend = None  # last frontend flag the runtime passed to step_cursor
        self.other_model = 'vanilla-strafe'  # what flight-model-toggle returns to from Sultan
        self.on_head_recentre = None  # HeadTracker.recentre; takes only the tracker's own lock
        self.synthetic_keys = frozenset()  # key actions the mod holds itself (T101 touchdown: the game's Land key)
        self.reload()

    def reload(self):
        """Settings changed: rebind. Held presses are released; a toggled parking brake survives
        unless the brake was switched to Hold to brake."""
        hold = self.store.values.get('parking-brake-mode') == 'hold'
        # Parking brake mode (Buttons page, T106) decides the brake's behaviour, not the stored binding.
        bindings = [replace(b, behaviour='press' if hold else 'toggle') if b.action == 'mod.parking-brake' else b
                    for b in self.store.button_bindings()]
        modifier = self.store.button_modifier
        self.flight.configure([b for b in bindings if not is_key_action(b.action)], modifier)
        self.keys.configure([b for b in bindings if is_key_action(b.action)], modifier)
        if hold:
            self.flight.latched.discard('mod.parking-brake')

    @property
    def parking(self):
        return 'mod.parking-brake' in self.state.held

    @property
    def gear(self):
        """T101 Landing gear: down while toggled on (or while a Press binding is held)."""
        return 'mod.gear' in self.state.held

    def raise_gear(self):
        """T101: put a toggled gear up (the game took off). False when it is not toggled down (a Press binding held)."""
        if 'mod.gear' not in self.flight.latched:return False
        self.flight.latched.discard('mod.gear');return True

    @property
    def eject(self):
        return 'mod.eject' in self.state.held

    @property
    def boost(self):
        return 'mod.boost' in self.state.held

    @property
    def trim_up(self):
        return 'mod.trim-up' in self.state.held

    @property
    def trim_down(self):
        return 'mod.trim-down' in self.state.held

    @property
    def trim_events(self):
        """Trim to hover / Trim reset presses this tick (T88 lift trim)."""
        return tuple(a for a in self.state.pressed if a in ('mod.trim-hover', 'mod.trim-reset'))

    @property
    def held(self):
        return self.state.held | self.key_state.held

    def step(self, read, now, flight_eligible, key_eligible=None):
        """key_eligible defaults to flight_eligible (tests and callers without a key lane)."""
        self.state = self.flight.step(read, now, flight_eligible)
        keys_eligible = flight_eligible if key_eligible is None else key_eligible
        self.key_state = self.keys.step(read, now, keys_eligible)
        if self.key_lane is not None:
            self.key_lane.qualified = bool(self.store.values.get('key-output-enabled'))
            held = self.key_state.held | self.synthetic_keys if keys_eligible else self.key_state.held
            self.key_lane.step(ButtonState(held, self.key_state.pressed, self.key_state.released))
        values = self.store.values
        for action in self.state.pressed:
            if action == 'mod.flight-model-next':
                keys = [k for k, _ in FLIGHT_MODELS]
                current = self._pending('flight-model', values)
                self.changes.append(('flight-model', keys[(keys.index(current)+1) % len(keys)]))
            elif action == 'mod.flight-model-toggle':
                current = self._pending('flight-model', values)
                if current != 'sultan':
                    self.other_model = current
                self.changes.append(('flight-model', self.other_model if current == 'sultan' else 'sultan'))
            elif action == 'mod.lever-mode-toggle':
                current = self._pending('lever-controls', values)
                self.changes.append(('lever-controls', 'thrust' if current == 'speed' else 'speed'))
            elif action == 'mod.pip-toggle':
                current = self._pending('pip-mode', values)
                self.changes.append(('pip-mode', 'lag' if current == 'lead' else 'lead'))
            elif action == 'mod.head-tracking-toggle':
                self.changes.append(('head-tracking-enabled', not self._pending('head-tracking-enabled', values)))
            elif action in ('mod.head-tracking-on', 'mod.head-tracking-off'):
                self.changes.append(('head-tracking-enabled', action == 'mod.head-tracking-on'))
            elif action == 'mod.head-recentre' and self.on_head_recentre is not None:
                self.on_head_recentre()
        return self.state

    def step_cursor(self, pitch_unit, yaw_unit, dt, focused, hotas_menu_open, frontend):
        """Rate-mode cursor from pitch/yaw. frontend: the game's frontend flag, None if unreadable."""
        values = self.store.values
        gate = (bool(values.get('key-output-enabled')), bool(values.get('cursor-stick-enabled')), focused, hotas_menu_open, frontend)
        self.frontend = frontend
        qualified = cursor_mode(*gate) and self.key_lane is not None
        dx, dy = self.cursor.step(values, pitch_unit, yaw_unit, dt, qualified, cursor_reason(*gate))
        if dx or dy:
            self.key_lane.move(dx, dy)
        return dx, dy

    def _pending(self, control_id, values):
        """Latest requested value, so two taps in one tick both count."""
        for cid, value in reversed(self.changes):
            if cid == control_id:
                return value
        return values[control_id]

    def take_changes(self):
        changes, self.changes = self.changes, []
        return changes

    def key_status(self):
        """For the status file and the Buttons page: lane state, keys/mouse held, SendInput failures."""
        lane = self.key_lane
        return {'output': lane.status if lane is not None else 'no lane',
                'held': sorted(self.key_state.held),
                'send_failures': getattr(getattr(lane, 'sender', None), 'failures', 0),
                'cursor': self.cursor.state, 'frontend_flag': self.frontend}

    def release(self, reason='released'):
        """Release transient holds on shutdown/recovery; router latches survive."""
        self.flight.disarm()
        self.state=EMPTY
        self.keys.disarm()
        self.cursor.reset()
        if self.key_lane is not None:
            self.key_lane.release_all(reason)
