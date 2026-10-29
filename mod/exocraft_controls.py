"""Exocraft controls (user 2026-10-28): the Exocraft page's axes and buttons as the game's own exocraft actions.

Driving stays the game's (no new driving physics, user); tools/native_analog_actions.py answers the game's own actions
while its VehicleMode action set is current. An unbound or missing axis is left out, so the game's own input answers it.

- Steering answers PlayerMoveX, the move stick, signed right positive, in first person: the exocraft adds it to
  VehicleSteer and the keys (0x182B027, CONFIRMED). VehicleSteer itself is left alone, so the stick never counts twice.
- Throttle answers VehicleThrust 0..1.
- Brake, or the back half of a Centre stop throttle, answers Vehicle_Reverse, as the S key does: the exocraft subtracts
  it from the throttle, so it slows and then reverses (0x182B167; 0.2.3-dev.9 live: VehicleBrake never reverses). The
  game reads it as a scalar or as the digital Held query, so past half travel it is also held as a game button
  (held_natives()).
- Camera pan answers CameraLookX/Y. In third person the exocraft heads where the camera looks (user, 0.2.3-dev.11), so
  there steering pans the camera left/right (added to the pan axis) and the move stick is left alone.
- Exocraft buttons held this tick answer 1.0 as well, because the game reads some of them as analog values (Vehicle_Dive
  0x37 in the Nautilon).
"""
from flight_shaping import bipolar, shape_axis
from native_analog_actions import LOOK_X, LOOK_Y, MOVE_X, REVERSE, THRUST

LOOK = (('exo-look-x', LOOK_X), ('exo-look-y', LOOK_Y))
REVERSE_HELD = .5  # reverse travel past which Vehicle_Reverse is also held as a button


def _ramp(value, deadzone):
    """0..1 travel past the deadzone, as 0..1 output."""
    return 0. if value <= deadzone else min(1., (value-deadzone)/max(1.-deadzone, 1e-4))


def _unit(values, unit, binding):
    token = values[binding]
    return unit(token) if token else None


def answers(values, unit, held_natives=(), third_person=False):
    """{action id: value} for the analog cave. unit(token) is the calibrated 0..1 position or None. third_person: the
    exocraft heads where the camera looks (user, 0.2.3-dev.11), so steering pans the camera left/right instead, added to
    the Camera pan left/right axis."""
    if not values.get('exo-enabled'):return {}
    out = {}
    steer = _unit(values, unit, 'bind-exo-steer-axis')
    steering = None if steer is None else shape_axis(values, 'exo-steer', steer)
    if steering is not None and not third_person:out[MOVE_X] = steering
    reverse = 0.
    throttle = _unit(values, unit, 'bind-exo-throttle-axis')
    if throttle is not None:
        if values['exo-throttle-style'] == 'split':
            s = bipolar(throttle, values['exo-throttle-inverted'])
            out[THRUST] = _ramp(s, values['exo-throttle-deadzone']); reverse = _ramp(-s, values['exo-throttle-deadzone'])
        else:
            out[THRUST] = _ramp(1.-throttle if values['exo-throttle-inverted'] else throttle, values['exo-throttle-deadzone'])
    pedal = _unit(values, unit, 'bind-exo-brake-axis')
    if pedal is not None:reverse = max(reverse, _ramp(1.-pedal if values['exo-brake-inverted'] else pedal, values['exo-brake-deadzone']))
    if pedal is not None or (throttle is not None and values['exo-throttle-style'] == 'split'):out[REVERSE] = reverse
    for channel, action in LOOK:
        look = _unit(values, unit, f'bind-{channel}-axis')
        if look is not None:out[action] = shape_axis(values, channel, look)
    if third_person and steering is not None:out[LOOK_X] = max(-1., min(1., out.get(LOOK_X, 0.)+steering))
    for action in held_natives:out[action] = 1.
    return out


def held_natives(out):
    """Game actions the axes hold as buttons this tick: Vehicle_Reverse past half travel."""
    return frozenset({REVERSE}) if out.get(REVERSE, 0.) >= REVERSE_HELD else frozenset()
