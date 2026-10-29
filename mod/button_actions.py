"""Runtime glue for button bindings (T61): routers per tick, mod and key/mouse lanes out.

The release runtime calls step() once per tick on its worker thread and reads parking for the
flight publisher. Bindings are split by output lane, each with its own eligibility:
- mod actions (parking brake, flight model, lever mode, aiming pip, head tracking) act only while flying;
- keyboard/mouse actions act whenever NMS has focus and the HOTAS menu is closed, because the quick menu, cockpit
  dialogs and the pause menu are exactly where they are needed. The game-control rows among them (hotas_buttons.
  NATIVE_FOR) reach the game one of two ways, picked by Button output: the game's own action (T59, native_lane) or
  their key. Without the game-action lane (not installed, or it failed) they send their key.
step_cursor() moves the mouse cursor from pitch/yaw while a frontend page is open (mod/stick_cursor.py).
Settings changes requested by buttons are returned by take_changes() so the runtime can apply them
outside its own lock: the settings listener takes that lock again.
"""
from dataclasses import replace

from hotas_buttons import BY_ID, NATIVE_FOR, ButtonRouter, ButtonState
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


def native_id(action_id):
    """The game action a game-control row presses on the direct path (cGcInputActions), or None."""
    return NATIVE_FOR.get(action_id)


def is_focus_action(action_id):
    """Actions live whenever NMS has focus: keys and mouse, game-control rows included."""
    return is_key_action(action_id)


class ButtonActions:
    def __init__(self, store, key_lane=None):
        self.store = store
        self.flight = ButtonRouter()
        self.keys = ButtonRouter()
        self.exo = ButtonRouter()  # Exocraft page bindings: live only while the game's exocraft controls are current
        self.exo_state = EMPTY
        self.axis_natives = frozenset()  # game actions the Exocraft axes hold as buttons (Vehicle_Reverse), set by the runtime
        self.key_lane = key_lane  # mod/key_lane.KeyLane, or None (tests, no Win32)
        self.native_lane = None  # tools/native_actions.NativeActions once the runtime installed it
        self.path = 'keys'       # the game-control rows' path this tick: direct | keys | fallback
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
        exo = [b for b in bindings if b.context == 'exocraft']
        bindings = [b for b in bindings if b.context != 'exocraft']
        self.flight.configure([b for b in bindings if not is_focus_action(b.action)], modifier)
        self.keys.configure([b for b in bindings if is_focus_action(b.action)], modifier)
        self.exo.configure(exo, modifier)
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
        return self.state.held | self.key_state.held | self.exo_state.held

    @property
    def exo_natives(self):
        """Game actions (cGcInputActions) the Exocraft buttons hold this tick."""
        return frozenset(BY_ID[a].native for a in self.exo_state.held if a in BY_ID and BY_ID[a].native is not None)

    def step(self, read, now, flight_eligible, key_eligible=None, driving=False):
        """key_eligible defaults to flight_eligible (tests and callers without a key lane). driving: the game's
        exocraft controls are current (VehicleMode); the Exocraft bindings act, and a button they share with another
        binding is theirs until you leave the exocraft (then it must be let go before it fires again)."""
        keys_eligible = flight_eligible if key_eligible is None else key_eligible
        self.exo_state = self.exo.step(read, now, bool(driving) and keys_eligible)
        if driving:
            taken = set(self.exo.watched()) - ({self.exo.modifier} if self.exo.modifier else set())
            read = (lambda token, read=read: None if token in taken else read(token)) if taken else read
        self.state = self.flight.step(read, now, flight_eligible)
        self.key_state = self.keys.step(read, now, keys_eligible)
        direct = self.store.values.get('button-output', 'direct') == 'direct'
        native = self.native_lane if direct else None
        self.path = 'direct' if native is not None else 'fallback' if direct else 'keys'
        free_keys = bool(self.store.values.get('key-output-enabled'))
        # Game-control rows go one way only: their game action, or their key. Other keys need Keyboard & mouse output.
        keyed = {a for a in self.key_state.held if (native is None if a in NATIVE_FOR else free_keys)}
        if self.key_lane is not None:
            self.key_lane.qualified = free_keys or native is None and any(a in NATIVE_FOR for a in self.keys.bindings_actions())
            held = keyed | (self.synthetic_keys if free_keys else frozenset()) if keys_eligible else keyed
            self.key_lane.step(ButtonState(frozenset(held), self.key_state.pressed, self.key_state.released))
        if self.native_lane is not None:
            # Exocraft buttons have only the game's action, whichever Button output is chosen.
            self.native_lane.step(({NATIVE_FOR[a] for a in self.key_state.held if a in NATIVE_FOR} if native is not None and keys_eligible
                                   else set()) | set(self.exo_natives) | (set(self.axis_natives) if driving else set()), int(now*1000))
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
        native = self.native_lane
        direct = self.path == 'direct'
        return {'output': lane.status if lane is not None else 'no lane', 'path': self.path,
                'held': sorted(a for a in self.key_state.held if not (direct and a in NATIVE_FOR)),
                'native': native.status() if native is not None else None,
                'native_held': sorted(a for a in self.key_state.held if direct and a in NATIVE_FOR),
                'send_failures': getattr(getattr(lane, 'sender', None), 'failures', 0),
                'cursor': self.cursor.state, 'frontend_flag': self.frontend}

    def release(self, reason='released'):
        """Release transient holds on shutdown/recovery; router latches survive."""
        self.flight.disarm()
        self.state=EMPTY
        self.keys.disarm()
        self.exo.disarm()
        self.exo_state=EMPTY
        self.axis_natives=frozenset()
        self.cursor.reset()
        if self.key_lane is not None:
            self.key_lane.release_all(reason)
        if self.native_lane is not None:
            self.native_lane.step(())
