"""T55 input publication, independent of native throttle/MAV fallbacks.

The publisher produces an immutable generation. The flight-thread consumer uses the
current native velocity and basis; a worker can never publish a guessed world vector.
Translation channels arm at neutral individually at load, after the input changed meaning (binding,
inversion, throttle style, lever zones) and after a device dropout: HOTAS neutral is its coast zone, because the bottom of the
Sultan lever is reverse thrust rather than idle. After a plain drop to native (launch, landing, ship
change, menu) they resume where they are, and the class jerk limit eases thrust in. Live 2026-10-19:
a lever held at full through a launch reinstall gave no thrust (about 25 u/s) until pulled back to
coast, the same dead-input problem T91 removed for the sticks.
"""
from dataclasses import dataclass, replace
import math

from flight_shaping import LATERAL_COMMAND_SIGN, shape_axis
from newtonian_flight import LeverZones, ThrottleDemand, map_hosas, map_lever


@dataclass(frozen=True)
class TranslationCommand:
    generation: int
    deadline: int
    enabled: bool
    lateral: float = 0.
    vertical: float = 0.
    throttle: object = ThrottleDemand()
    reason: str = ''
    trim: float = 0.  # T88 lift trim: share of vertical authority added to the vertical command


class TranslationPublisher:
    """Worker-thread-only shaping; disabled frames never retain a thrust command."""
    def __init__(self):
        self.generation = 0
        self.armed = dict.fromkeys(('strafe-lateral', 'strafe-vertical', 'throttle'), False)
        self.reasons = {}
        self.signature = None
        self.neutral_required = set(self.armed)  # channels that wait for neutral before arming

    def disarm(self, reason, needs_neutral=False):
        self.armed = dict.fromkeys(self.armed, False)
        self.reasons = dict.fromkeys(self.armed, reason)
        if needs_neutral:
            self.neutral_required = set(self.armed)

    def step(self, table, values, eligible, now_tick, zone_values, parking_brake=False, boost=False):
        """parking_brake: the bound parking-brake button (T61); it overrides throttle demand."""
        self.generation += 1
        signature = tuple(values.get(k) for k in (
            'flight-model', 'throttle-style', 'throttle-inverted', 'lever-controls',
            'bind-throttle-axis', 'bind-strafe-lateral-axis', 'bind-strafe-vertical-axis',
            'strafe-lateral-inverted', 'strafe-vertical-inverted')) + tuple(sorted(zone_values.items()))
        if signature != self.signature:
            self.disarm('configuration changed', needs_neutral=True)
            self.signature = signature
        enabled = bool(eligible and values['flight-controls-enabled'] and values['flight-model'] == 'sultan')
        if not enabled:
            self.disarm('native fallback')
            return TranslationCommand(self.generation, now_tick + 100, False, reason='native fallback')
        out = []
        for channel in ('strafe-lateral', 'strafe-vertical'):
            unit = table.unit(values[f'bind-{channel}-axis'])
            if unit is None or not math.isfinite(unit):
                self.armed[channel] = False
                self.neutral_required.add(channel)
                self.reasons[channel] = 'device missing or stale'
                out.append(0.)
                continue
            shaped = shape_axis(values, channel, unit)
            if not self.armed[channel]:
                if abs(shaped) <= .02 or channel not in self.neutral_required:
                    self.armed[channel] = True
                    self.neutral_required.discard(channel)
                else:
                    shaped = 0.
            self.reasons[channel] = 'armed' if self.armed[channel] else 'waiting for centre'
            out.append(shaped*LATERAL_COMMAND_SIGN if channel == 'strafe-lateral' else shaped)
        unit = table.unit(values['bind-throttle-axis'])
        demand = ThrottleDemand(0., 'coast')
        if unit is None or not math.isfinite(unit):
            self.armed['throttle'] = False
            self.neutral_required.add('throttle')
            self.reasons['throttle'] = 'device missing or stale'
        else:
            if values['throttle-style'] == 'hotas':
                position = 1. - unit if values['throttle-inverted'] else unit
                mapped = map_lever(position, LeverZones(**zone_values), values.get('lever-controls', 'speed'))
                neutral = mapped.zone == 'coast'
            else:
                mapped = map_hosas(unit, values['throttle-deadzone'], values['hosas-saturation'], values['throttle-inverted'])
                neutral = abs(mapped.value) <= .02
            if neutral or 'throttle' not in self.neutral_required:
                self.armed['throttle'] = True
                self.neutral_required.discard('throttle')
            if self.armed['throttle']:
                demand = mapped
            self.reasons['throttle'] = 'armed' if self.armed['throttle'] else 'waiting for coast/centre'
        if parking_brake:
            demand = ThrottleDemand(zone='parking', parking=True)
        elif boost and not demand.parking:
            demand = replace(demand, boost_requested=True)
        return TranslationCommand(self.generation, now_tick + 100, True, *out, demand)
