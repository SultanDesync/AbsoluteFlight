"""T88 Sultan planetary flight: gravity, air density, drag, engine loss and lift on top of the T55 law.

Pure functions; no game memory. docs/PLANETARY-FLIGHT.md section 5 is the spec.

Gravity is ours alone. Ship setup disables backend gravity (motion+0x78 = 0, B+0x27C = 0 in every
T86 flight capture), and the final cave pins those values, so a ship flown by Sultan feels no native
gravity and adding g_vec here cannot double it. If the backend factor ever turns on (the native
forced-fall path), the cave hands the frame back to native.

Per-class air styles (user direction 2026-10-19, amends D3): sleek hulls emulate aerodynamic flight,
blocky hulls fly on thrusters. This module carries the terms; AIR_CLASSES says which classes have a
tuned style. Other classes keep the native atmosphere model.
"""
from dataclasses import dataclass, replace
import math

from newtonian_flight import FlightResult, compute_acceleration

# Fighter first (user 2026-10-19); Hauler and Shuttle bricks (T92); the rest by class (T98, user 2026-10-20):
# the specials fly aero in every direction, Explorer is balanced. Corvette (10): its baseline style, incomplete
# (user 2026-10-22).
AIR_CLASSES = frozenset((1, 2, 3, 4, 6, 7, 8, 9, 10))
LIFT_SPEED = 150.  # u/s; below this lift fades out linearly (no carving at hover speeds), GUESS
DRAG_LIMIT = 3.    # drag never exceeds this many times the forward authority (fast entries stay flyable)
LIFT_LIMIT = 2.    # same bound for lift
# Wings lift in the pitch plane; a yaw skid only gets a weak fuselage side force. Without this the air
# swung the flight path onto the nose within half a second in yaw, so no slip was left for the
# weathervane to fight the pilot with (user flight 2026-10-19).
LIFT_SIDE = .25
# Sideslip (user design 2026-10-19): strafing or yawing hard puts the nose off the flight path. The slip
# costs nose authority (here) and makes the air push the nose back with a sway (rotation seam, T92).
SLIP_FULL = math.radians(35.)  # slip angle that brings the full effect
SLIP_SPEED = .5                # share of the terminal speed where slip effects reach full strength
SLIP_MIN_SPEED = 20.           # u/s; slower drift has no flight path to slip from
MIN_NOSE = .25                 # the rotation cave range-checks its scale to 0.25..1
MAV_LIFT_MAX = 4.   # MAV lift never scales a class's own MAV thrust past this (a weak ship, or no thrust)
MAX_AIR_FADE = 64.  # fade exponent ceiling in thick air (x**64 is nil below the cap)
DENSITY_RATE = 1.  # largest change in smoothed density per second (no step when the nearest planet changes)
# Density knots between the thick-air top and the atmosphere top (docs/PLANETARY-FLIGHT.md 5.1a):
# with the defaults 1 km / 12 km they fall at 1, 3, 8 and 12 km.
DENSITY_KNOTS = ((0., 1.), (.18, .6), (.636, .15), (1., 0.))


def _dot(a, b):return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def _norm(v):return math.sqrt(_dot(v, v))


def _scale(v, k):return (v[0]*k, v[1]*k, v[2]*k)


def _add(*vs):return tuple(sum(c) for c in zip(*vs))


@dataclass(frozen=True)
class Air:
    """Planet settings shared by all classes (Atmosphere Flight page)."""
    gravity: float = 20.        # u/s^2 at full native gravity multiplier; native compensation uses 20*factor (B+0x27C)
    # D2, amended 2026-10-19 (user: glide, and blocky hulls drop like rocks without thrust): 'thrusting' holds
    # altitude only while the engines work (lever out of coast or any strafe input), so coasting glides on the
    # wings or falls; 'always' is the original D2; 'off' leaves gravity to the pilot. True/False mean always/off.
    compensation: object = 'off'  # lift trim (user 2026-10-19) replaces it by default
    thick_top: float = 1000.    # u above sea level; density 1 below
    top: float = 12000.         # u above sea level; density 0 above

    def __post_init__(self):
        mode = {True: 'always', False: 'off'}.get(self.compensation, self.compensation)
        object.__setattr__(self, 'compensation', mode if mode in ('off', 'thrusting', 'always') else 'thrusting')

    @classmethod
    def from_values(cls, values):
        return cls(float(values.get('air-gravity', cls.gravity)), values.get('air-gravity-compensation', cls.compensation),
                   float(values.get('air-thick-top', cls.thick_top)), float(values.get('air-top', cls.top)))

    def compensating(self, throttle, lateral, vertical):
        """Whether gravity compensation holds the ship this frame (the parking brake always does)."""
        if throttle.parking or self.compensation == 'always':return True
        if self.compensation == 'off':return False
        return throttle.zone != 'coast' or abs(lateral) > .02 or abs(vertical) > .02


@dataclass(frozen=True)
class AirStyle:
    """One class's air character (per-class Flight Tuning rows)."""
    terminal_speed: float = 400.  # u/s at full forward thrust in thick air
    engine_loss: float = .2       # share of thrust lost in thick air
    lift: float = 0.              # 1/s: how fast the velocity turns toward the nose in thick air
    side_drag: float = 1.         # drag for sideways and vertical motion, as a multiple of nose-on drag
    nose_authority: float = 1.    # pitch/yaw rate share left at full dynamic pressure (thick air, terminal speed)
    induced_drag: float = 0.      # turn drag: speed bled per unit of lift, so pulling hard in a bank decelerates
    slip_authority: float = 1.    # pitch/yaw rate share left in a full slide (strafe or yaw off the flight path)
    weathervane: float = 0.       # 1/s: how hard the air swings the nose back onto the flight path (rotation seam)
    sway_damping: float = .5      # how quickly that swing settles: low wallows, high snaps straight (rotation seam)
    damping: float = 0.           # share of the thick-air drag at terminal speed that is linear, so the air
                                  # resists from the first u/s; terminal speed stays exact (user 2026-10-19)
    boost: float = 1.             # share of the boost kept in thick air; it returns as the air thins
    overspeed_penalty: float = 0. # above the terminal speed: multiplies nose loss, sway, buffet and turn drag
    wing_support: float = 0.      # glide: share of weight the wings carry at half the terminal speed, as v^2
    glide_ratio: float = 0.       # with the engines idle, nose-on drag is the wings' lift / this (0 = full drag)
    # T98 (user 2026-10-20): the specials answer strafe with aero in every direction; the bricks lift on MAVs
    # that only just carry them and fly on their mains.
    omni: float = 0.              # 0..1: share of lift and wing support that works sliding sideways or backward
    mav_lift: float = 0.          # thick-air MAV up thrust in planet weights, whatever the engine tier (0 = off)

    FIELDS = ('terminal_speed', 'engine_loss', 'lift', 'side_drag', 'nose_authority',
              'induced_drag', 'slip_authority', 'weathervane', 'sway_damping', 'damping', 'boost', 'overspeed_penalty', 'wing_support', 'glide_ratio',
              'omni', 'mav_lift')

    @classmethod
    def from_profile(cls, profile):
        return cls(*(float(profile.get('air_'+k, getattr(cls, k))) for k in cls.FIELDS))


def density(altitude, air):
    """0..1 air density at this altitude above sea level: one smooth curve through the strata."""
    span = air.top-air.thick_top
    if altitude <= air.thick_top or span <= 0.:return 1. if altitude <= air.thick_top else 0.
    x = (altitude-air.thick_top)/span
    if x >= 1.:return 0.
    for (x0, r0), (x1, r1) in zip(DENSITY_KNOTS, DENSITY_KNOTS[1:]):
        if x <= x1:
            u = (x-x0)/(x1-x0)
            return r0+(r1-r0)*u*u*(3.-2.*u)
    return 0.


def gravity_multiplier(distance, radius):
    """Native point-gravity falloff (T86 R2): 1 inside the radius, (radius/distance)^4 outside."""
    if distance <= radius:return 1.
    return (radius/distance)**4


# User 2026-10-21: gravity was felt 78k u out, since the native falloff (radius/d)^4 never reaches 0. The game has
# no orbits, so we imagine orbital velocity at the atmosphere top: gravity ends there, airless planets included (same
# altitude), easing out over the last GRAVITY_FADE u so crossing it is no jolt.
GRAVITY_FADE = 2000.


def gravity_fade(altitude, air):
    """1 below the fade band, easing to 0 at the atmosphere top (altitudes above sea level)."""
    if altitude >= air.top:return 0.
    start = air.top-min(GRAVITY_FADE, max(0., air.top))
    if altitude <= start:return 1.
    u = (air.top-altitude)/(air.top-start)
    return u*u*(3.-2.*u)


def planet_state(position, planets, air):
    """Nearest planet's frame, density and gravity vector at this position, or None without a planet."""
    best = None
    for p in planets:
        d = tuple(a-b for a, b in zip(position, p['centre']))
        r = _norm(d)
        if r < 1.:continue
        h = r-p['radius']
        if best is None or h < best[0]:best = (h, r, d, p)
    if best is None:return None
    h, r, d, p = best
    up = _scale(d, 1./r); altitude = h-p['sea']
    rho = density(altitude, air) if p.get('atmosphere') == 1 else 0.
    g = air.gravity*gravity_multiplier(r, p['radius'])*gravity_fade(altitude, air)
    return {'index': p['index'], 'up': up, 'altitude': altitude, 'height_base': h, 'density': rho,
            'atmosphere': p.get('atmosphere'), 'gravity': _scale(up, -g)}


def space_thrust(effective, planet_block, space_block, blend):
    """Effective ThrustForce as if in space: native blends the Planet block toward Space by ship+0x62C0
    (T86 R6), and Sultan's own engine loss replaces that blend. Falls back to the effective value."""
    values = (effective, planet_block, space_block, blend)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):return effective
    if not 0. <= blend <= 1. or not 0. < planet_block < 2000. or not 0. < space_block < 2000.:return effective
    corrected = effective+(space_block-planet_block)*(1.-blend)
    return corrected if 0. < corrected <= 2.*effective else effective


def air_fade(exponent, rho):
    """In air, drag is the speed limiter, so the space acceleration fade gives way: its exponent grows as
    1/(1-rho), unchanged in vacuum and effectively gone in thick air. Continuous at the atmosphere top."""
    if exponent <= 0.:return exponent
    return min(MAX_AIR_FADE, exponent/max(1e-6, 1.-rho))


def _forward_drag_coefficient(forward_authority, profile, style):
    """(k, c): quadratic and linear nose-on drag coefficients such that full forward thrust (after the class
    fade) balances k*v^2 + c*v exactly at the terminal speed, with style.damping the linear share there.
    A share, not a rate: a raw linear rate let low-thrust ships stall far below the terminal speed (60 u/s
    for a C-class Fighter at 1.4/s, user flight 2026-10-19)."""
    v = style.terminal_speed
    if v <= 0. or forward_authority <= 0.:return 0., 0.
    fade = 1.
    cap = profile.effective_cap
    if profile.accel_fade_exponent > 0. and v < cap:fade = 1.-(v/cap)**profile.accel_fade_exponent
    drag = forward_authority*max(fade, .05)
    share = min(1., max(0., style.damping))
    return (1.-share)*drag/(v*v), share*drag/v


def air_terms(velocity, basis, rho, forward_authority, profile, style):
    """(drag, lift) world accelerations for this velocity in air of density rho."""
    speed = _norm(velocity)
    if rho <= 0. or speed <= 0. or forward_authority <= 0.:return (0., 0., 0.), (0., 0., 0.)
    quadratic, linear = _forward_drag_coefficient(forward_authority, profile, style)
    k = (quadratic*speed+linear)*rho
    body = basis.to_body(velocity)
    drag = basis.to_world((-k*style.side_drag*body[0], -k*style.side_drag*body[1], -k*body[2]))
    magnitude = _norm(drag); limit = DRAG_LIMIT*forward_authority
    if magnitude > limit:drag = _scale(drag, limit/magnitude)
    lift = (0., 0., 0.)
    # Lift turns the velocity toward the nose. With all-direction aero (T98) it turns it toward the hull's
    # own plane instead (right and forward), so a strafing or backing hull keeps its slide and still lifts.
    omni = min(1., max(0., style.omni))
    planar = math.hypot(body[0], body[2])
    cosine = (1.-omni)*max(0., body[2])/speed+omni*planar/speed
    aim = (omni*body[0]/planar, 0., 1.-omni+omni*body[2]/planar) if planar > 0. else (0., 0., 1.)
    reach = _norm(aim)
    if style.lift > 0. and cosine > 0. and reach > 1e-6:
        # |v|*aim - v keeps speed (to first order) and bleeds a little as induced drag. Strongest along the
        # aim, gone at 90 degrees from it, faded below LIFT_SPEED.
        rate = style.lift*rho*cosine*min(1., speed/LIFT_SPEED)
        right, up, forward = (rate*(speed*a/reach-b) for a, b in zip(aim, body))
        lift = basis.to_world((right*(LIFT_SIDE+(1.-LIFT_SIDE)*omni), up, forward))
        magnitude = _norm(lift); limit = LIFT_LIMIT*forward_authority
        if magnitude > limit:lift = _scale(lift, limit/magnitude)
        if style.induced_drag > 0.:
            # Pull-load drag: lift costs speed as its square, so a plain roll is free and a hard pull in
            # the bank bleeds energy. Bounded by the forward authority.
            # Past the terminal speed the overspeed penalty multiplies it (and its bound): pulling at overspeed
            # bleeds speed hard.
            boost = overspeed_multiplier(style, rho, speed)
            induced = min(forward_authority*boost, boost*style.induced_drag*_dot(lift, lift)/forward_authority)
            drag = _add(drag, _scale(velocity, -induced/speed))
    return drag, lift


def sideslip(velocity, basis):
    """(angle, right, up): how far the flight path is off the nose, and its body right/up direction
    components (unit velocity), or zeros below SLIP_MIN_SPEED."""
    speed = _norm(velocity)
    if speed < SLIP_MIN_SPEED:return 0., 0., 0.
    right, up, forward = (c/speed for c in basis.to_body(velocity))
    return math.acos(max(-1., min(1., forward))), right, up


def slip_pressure(style, rho, speed):
    """0..1 dynamic pressure for slip effects: full at SLIP_SPEED of the terminal speed in thick air."""
    if style.terminal_speed <= 0. or not rho > 0. or not speed > 0.:return 0.
    return min(1., rho*(speed/(SLIP_SPEED*style.terminal_speed))**2)


def wing_support(velocity, basis, rho, gravity, style):
    """Glide: with the engines idle the wings carry min(1, support*rho*(v_forward/(SLIP_SPEED*terminal))^2) of
    the weight along the ship's own up axis, so a level coast at speed glides, a bank sinks unless you pull,
    nose down trades height for speed and a slow ship sinks. Blocky hulls (support 0) drop.
    All-direction aero (T98) counts sideways and backward motion with weight omni, so a strafe glides too."""
    right, _, forward = basis.to_body(velocity)
    omni = min(1., max(0., style.omni))
    along = math.hypot(max(0., forward)+omni*max(0., -forward), omni*right)
    if style.wing_support <= 0. or style.terminal_speed <= 0. or not rho > 0. or along <= 0. or gravity <= 0.:
        return (0., 0., 0.)
    share = min(1., style.wing_support*rho*(along/(SLIP_SPEED*style.terminal_speed))**2)
    return _scale(basis.up, gravity*share)


def mav_scale(profile, style, rho, gravity, thrust_force):
    """T98 bricks: multiplier on the class's MAV ratios (strafe, up, down) in this air. In thick air the up
    thrusters lift style.mav_lift planet weights whatever the engine tier, just enough to get up and point
    the mains; the other MAVs scale alike. It fades back to the class's own MAVs as the air thins."""
    if style.mav_lift <= 0. or not rho > 0. or not gravity > 0.:return 1.
    up = profile.authority(thrust_force, 1.)[1][1]
    if not up > 0.:return 1.
    thick = min(MAV_LIFT_MAX, style.mav_lift*gravity/up)
    return 1.+(thick-1.)*min(1., rho)


def glide_drag(drag, velocity, basis, rho, gravity, forward_authority, profile, style, power):
    """User 2026-10-19: aerodynamic hulls glide about 3-4:1 with the power off. The nose-on part of the
    parasitic drag blends from the drag of the lift the wings make, gravity*W*rho*(v/v_glide)^2/glide_ratio
    (exactly weight/ratio in a level glide at the full-support speed, and growing as v^2 above it), at no
    forward thrust, to the full thrust-calibrated drag at full forward thrust (power = applied share).
    Turn drag, side and vertical drag stay whole. Not strict physics: drag that tracks power is what lets
    a released throttle glide while full thrust still tops out at the terminal speed."""
    forward = basis.to_body(velocity)[2]
    if not rho > 0. or forward <= 0. or style.terminal_speed <= 0. or gravity <= 0.:return drag
    plain, _ = air_terms(velocity, basis, rho, forward_authority, profile, replace(style, induced_drag=0.))
    turn = tuple(d-p for d, p in zip(drag, plain))
    body = list(basis.to_body(plain))
    glide = -gravity*max(style.wing_support, 0.)*rho*(forward/(SLIP_SPEED*style.terminal_speed))**2/style.glide_ratio
    if glide > body[2]:  # gentler than the full drag (both negative)
        share = min(1., max(0., power))
        body[2] = glide+(body[2]-glide)*share
    return _add(basis.to_world(tuple(body)), turn)


OVERSPEED_MAX = 2.  # overspeed is counted up to q = 3 (penalty multiplier 1 + 2*penalty at most)


def overspeed(style, rho, speed):
    """0..OVERSPEED_MAX: dynamic pressure above the class terminal speed, q - 1 with q = rho*(speed/terminal)^2.
    q = 1 is the speed full unboosted thrust holds in this air, so anything above it is boost or a dive."""
    if style.terminal_speed <= 0. or not rho > 0. or not speed > 0.:return 0.
    return min(OVERSPEED_MAX, max(0., rho*(speed/style.terminal_speed)**2-1.))


def overspeed_multiplier(style, rho, speed):
    """User design 2026-10-19: past the terminal speed every air penalty on manoeuvring grows."""
    return 1.+max(0., style.overspeed_penalty)*overspeed(style, rho, speed)


def nose_authority(style, rho, speed, slip=0.):
    """Pitch/yaw rate scale for a blocky hull in air (T92): 1 at rest or in vacuum, falling linearly with
    dynamic pressure q = rho*(speed/terminal)^2 to style.nose_authority at q = 1 and held there above it.
    The rotation seam applies it to the class's top pitch and yaw rates; roll and turn response stay."""
    if style.terminal_speed <= 0. or not rho > 0. or not speed > 0.:return 1.
    scale = 1.
    if style.nose_authority < 1.:
        q = min(1., rho*(speed/style.terminal_speed)**2)
        scale = 1.-(1.-max(0., style.nose_authority))*q
    if style.slip_authority < 1. and slip > 0.:
        # Sideslip: the slide takes nose authority until forward thrust and lift put the ship back on its path.
        share = min(1., math.sin(min(slip, math.pi/2.))/math.sin(SLIP_FULL))*slip_pressure(style, rho, speed)
        scale *= 1.-(1.-max(0., style.slip_authority))*share
    return max(MIN_NOSE, scale/overspeed_multiplier(style, rho, speed))


# T93 ditch at sea (user design 2026-10-19). Native has no water physics for the ship; it stays dry only
# because the ground-clearance term treats the sea as ground (research/re-log/2026-10-19_claude_ditching.md).
# With the setting on, the helper cancels that term over water and these terms take over.
HULL = 3.               # u from the ship position down to the keel: contact below this altitude (GUESS)
FLOAT_BUOYANCY = 2.     # intact hull: buoyancy / weight when fully under, floats with the position at sea level
WRECK_BUOYANCY = .6     # flooded hull after a ditch: sinks
WATER_ENGINE_LOSS = .5  # thrust share lost fully under water
EMERGENCY_LIFT = .25    # share of up authority that positive lift trim keeps with the thrusters flooded
BLAST_ACCEL = 150.      # u/s^2 up: the boost blast that throws a relit ship out of the water (user 2026-10-19), GUESS
BLAST_MS = 1500         # how long the blast lasts after the purge relights the thrusters
WATER_DRAG_LIMIT = 400. # u/s^2 (about 20 g): the impact slam is bounded
MIN_WATER_G = 5.        # u/s^2: water drag keeps its strength with planet gravity turned down


@dataclass(frozen=True)
class Water:
    """Ditch at sea settings (Ship Damage page, all classes)."""
    ditch: bool = False
    ditch_speed: float = 120.     # u/s at water contact, any direction, that wipes out
    ditch_sink_rate: float = 30.  # u/s of descent at water contact that wipes out
    sink_speed: float = 10.       # u/s a ditched ship settles at on its way to the sea floor
    purge_difficulty: str = 'normal'  # thruster purge band width (mod/ditch_purge.py)

    @classmethod
    def from_values(cls, values):
        return cls(bool(values.get('air-ditch', cls.ditch)), float(values.get('air-ditch-speed', cls.ditch_speed)),
                   float(values.get('air-ditch-sink-rate', cls.ditch_sink_rate)),
                   float(values.get('air-sink-speed', cls.sink_speed)), str(values.get('air-purge-difficulty', cls.purge_difficulty)))


def submersion(altitude):
    """0 at first contact (position HULL above the sea), 1 with the hull fully under (position HULL below)."""
    return min(1., max(0., (HULL-altitude)/(2.*HULL)))


def ditch_impact(velocity, up, water):
    """True when hitting the water this fast (any direction) or this steeply wipes the ship out."""
    return _norm(velocity) > water.ditch_speed or -_dot(velocity, up) > water.ditch_sink_rate


def water_terms(velocity, up, gravity, submerged, water, engines_out):
    """(buoyancy, drag) world accelerations in the sea. Drag is linear plus quadratic, half each at the sink
    speed, so a flooded hull settles at water.sink_speed; it scales with submersion and is bounded."""
    if not submerged > 0.:return (0., 0., 0.), (0., 0., 0.)
    buoyancy = _scale(up, gravity*(WRECK_BUOYANCY if engines_out else FLOAT_BUOYANCY)*submerged)
    net = max(gravity, MIN_WATER_G)*(1.-WRECK_BUOYANCY)
    v = max(1., water.sink_speed)
    speed = _norm(velocity)
    drag = _scale(velocity, -submerged*(net/(2.*v)+net/(2.*v*v)*speed))
    magnitude = _norm(drag)
    if magnitude > WATER_DRAG_LIMIT:drag = _scale(drag, WATER_DRAG_LIMIT/magnitude)
    return buoyancy, drag


@dataclass(frozen=True)
class Gear:
    """T101 manual landing (Atmosphere Flight page, docs/MANUAL-LANDING.md): gear down is hover mode. Gravity
    compensation always holds the ship, boost is off and the speed cap drops to the hover cap; lowering the
    gear at speed bleeds down to it over slow_time. The caves keep the native ground push out meanwhile."""
    hover_cap: float = 40.   # u/s, GUESS
    slow_time: float = 2.    # s, GUESS
    # User 2026-10-21 (first touchdowns needed a hard push into the ground): with the gear down the vertical stick
    # is a climb/sink rate, centred holds height, and the sink allowed shrinks near the ground so the ship eases on.
    vertical_rate: float = 10.  # u/s at full stick, GUESS
    ease: float = .4            # allowed sink per u of height above ground (1/s), GUESS
    touchdown_sink: float = 1.5  # u/s, the slowest the ease allows, GUESS
    rate_gain: float = 2.       # 1/s from sink-rate error to vertical acceleration, GUESS

    @classmethod
    def from_values(cls, values):
        return cls(float(values.get('landing-hover-cap', cls.hover_cap)),
                   vertical_rate=float(values.get('landing-vertical-rate', cls.vertical_rate)))

    def target_rate(self, vertical, clearance):
        """Vertical speed (u/s, + up) the stick asks for, with the sink eased near the ground."""
        rate = vertical*self.vertical_rate
        if rate < 0. and clearance is not None and math.isfinite(clearance):
            rate = max(rate, -max(self.touchdown_sink, self.ease*max(0., clearance)))
        return rate

    def vertical_command(self, vertical, velocity, up, basis, clearance, down_authority, up_authority):
        """The vertical stick share (along the ship's up axis) that steers the sink rate toward target_rate.
        Gravity is held separately (compensation), so this is only the correction."""
        error = self.target_rate(vertical, clearance)-_dot(velocity, up)
        along = _dot(basis.up, up)
        if along <= .1:return 0.
        wanted = error*self.rate_gain/along
        authority = up_authority if wanted >= 0. else down_authority
        return 0. if authority <= 0. else min(1., max(-1., wanted/authority))


@dataclass(frozen=True)
class PlanetaryResult:
    flight: object        # FlightResult with the final world command and the felt (non-gravity) body acceleration
    density: float
    gravity: tuple
    drag: tuple
    lift: tuple
    engine_efficiency: float
    buoyancy: tuple = (0., 0., 0.)
    water_drag: tuple = (0., 0., 0.)
    hover_trim: object = None  # T88: lift-trim share that holds the ship against gravity at this attitude


def compute(profile, style, air, planet, rho, lateral, vertical, throttle, velocity, basis,
            thrust_force, dt, *, previous_acceleration_world, boost_multiplier, command_fresh,
            water=None, submerged=0., engines_out=False, trim=0., gear=None, clearance=None):
    """T55 pilot law in air: thrust scaled by engine efficiency, then gravity compensation (and the speed
    follower's drag feed-forward) paid from the same per-axis authority, then drag, lift and gravity.
    In the sea (T93, water given) buoyancy and water drag join and engines lose thrust with submersion;
    engines_out drops the pilot's thrust and compensation entirely. gear (T101, a Gear while the gear is
    down) is hover mode: hover cap, no boost, compensation always on, and the vertical stick steers the sink
    rate (Gear.target_rate, eased by clearance, the game's height above ground) instead of thrust.

    The FlightResult's pilot_acceleration_world stays the bare pilot command, so jerk smoothing never
    absorbs gravity or air. A stale command still returns zero (the native cave coasts). trim is the pilot's
    lift trim, a share of vertical authority added to the vertical command (not counted as strafe input for
    While thrusting compensation)."""
    if water is None:submerged = 0.  # off the water a ditched ship still has its engines out
    if engines_out and command_fresh:
        return _engines_out(profile, style, planet, rho, velocity, basis, thrust_force, water, submerged, trim)
    efficiency = (1.-style.engine_loss*rho)*(1.-WATER_ENGINE_LOSS*submerged)
    # Thick air keeps only style.boost of the boost's extra thrust; it returns as the air thins.
    boost_multiplier = 1. if gear is not None else 1.+(boost_multiplier-1.)*(1.-(1.-style.boost)*rho)
    flown = replace(profile, acceleration_scale=profile.acceleration_scale*efficiency,
                    accel_fade_exponent=air_fade(profile.accel_fade_exponent, rho))
    if gear is not None:
        flown = replace(flown, speed_cap=gear.hover_cap, speed_cap_multiplier=1., overspeed_bleed_time=gear.slow_time)
    mav = mav_scale(flown, style, rho, _norm(planet['gravity']), thrust_force)
    if mav != 1.:
        flown = replace(flown, lateral_ratio=flown.lateral_ratio*mav, up_ratio=flown.up_ratio*mav,
                        down_ratio=flown.down_ratio*mav)
    if gear is not None:
        (_, _), (down_authority, up_authority), _ = flown.authority(thrust_force, 1.)
        trimmed = gear.vertical_command(vertical, velocity, planet['up'], basis, clearance, down_authority, up_authority)
    else:
        trimmed = min(1., max(-1., vertical+trim))
    result = compute_acceleration(flown, lateral, trimmed, throttle, velocity, basis, thrust_force, dt,
                                  previous_acceleration_world=previous_acceleration_world,
                                  boost_multiplier=boost_multiplier, command_fresh=command_fresh)
    if not command_fresh:
        return PlanetaryResult(result, rho, (0., 0., 0.), (0., 0., 0.), (0., 0., 0.), efficiency)
    gravity = planet['gravity']
    parking = result.parking
    limits = flown.authority(thrust_force, 1. if parking else boost_multiplier)
    unboosted = flown.authority(thrust_force, 1.)[2][1]
    drag, lift = air_terms(velocity, basis, rho, unboosted, flown, style)
    extra = (0., 0., 0.)
    compensating = gear is not None or air.compensating(throttle, lateral, vertical)
    if compensating:extra = _scale(gravity, -1.)  # the parking brake always holds position
    if gear is not None:  # T101: the sink-rate hold also pays the vertical drag, or it settles short of its rate
        extra = _add(extra, _scale(planet['up'], -_dot(drag, planet['up'])))
    pilot = result.pilot_acceleration_world or result.acceleration_world
    bleed = tuple(a-b for a, b in zip(result.acceleration_world, pilot))  # automatic overspeed bleed, outside authority

    def clamped(extra):
        body = basis.to_body(_add(pilot, extra))
        return basis.to_world(tuple(min(positive, max(-negative, a)) for a, (negative, positive) in zip(body, limits)))
    thrust = clamped(extra)
    if throttle.speed_control and not parking:
        # Without this the follower would settle short of the target by drag/gain. With gliding on the drag
        # depends on the power it is fed, so resolve both together (a few fixed-point passes converge).
        held = drag
        for _ in range(3 if style.glide_ratio > 0. and limits[2][1] > 0. else 1):
            if style.glide_ratio > 0. and limits[2][1] > 0.:
                held = glide_drag(drag, velocity, basis, rho, _norm(gravity), unboosted, flown, style,
                                  basis.to_body(thrust)[2]/limits[2][1])
            thrust = clamped(_add(extra, _scale(basis.forward, -_dot(held, basis.forward))))
    buoyancy, water_drag = (water_terms(velocity, planet['up'], _norm(gravity), submerged, water, False)
                            if water is not None else ((0., 0., 0.), (0., 0., 0.)))
    support = (0., 0., 0.) if compensating else wing_support(velocity, basis, rho, _norm(gravity), style)
    if style.glide_ratio > 0.:
        drag = glide_drag(drag, velocity, basis, rho, _norm(gravity), unboosted, flown, style,
                          basis.to_body(thrust)[2]/limits[2][1] if limits[2][1] > 0. else 1.)
    felt = _add(thrust, bleed, drag, lift, buoyancy, water_drag, support)
    world = _add(felt, gravity)
    flight = replace(result, acceleration_world=world, acceleration_body=basis.to_body(felt))
    # Hover trim: the vertical share whose thrust cancels gravity (less wing support) along the ship's up axis.
    (_, _), (down_authority, up_authority), _ = flown.authority(thrust_force, 1.)
    need = -_dot(_add(gravity, support), basis.up)
    hover = need/up_authority if need >= 0. and up_authority > 0. else need/down_authority if down_authority > 0. else None
    hover = None if hover is None else min(1., max(-1., hover))
    return PlanetaryResult(flight, rho, gravity, drag, lift, efficiency, buoyancy, water_drag, hover)


def _engines_out(profile, style, planet, rho, velocity, basis, thrust_force, water, submerged, trim=0.):
    """Ditched: no pilot thrust, compensation or lift. Gravity, the flooded hull's buoyancy, water drag and
    (out of the water) air drag move the ship; the native rotation gets centred inputs from the runtime.
    Positive lift trim still gives a weak emergency lift (EMERGENCY_LIFT of up authority) to slow the sink."""
    gravity = planet['gravity']
    authority = profile.authority(thrust_force, 1.)
    unboosted = authority[2][1]
    drag, _ = air_terms(velocity, basis, rho*(1.-submerged), unboosted, profile, replace(style, lift=0.))
    buoyancy, water_drag = water_terms(velocity, planet['up'], _norm(gravity), submerged, water, True)
    lift = _scale(basis.up, EMERGENCY_LIFT*authority[1][1]*min(1., max(0., trim)))
    felt = _add(drag, buoyancy, water_drag, lift)
    flight = FlightResult(_add(felt, gravity), basis.to_body(felt), False, False, reason='engines out',
                          pilot_acceleration_world=(0., 0., 0.))
    return PlanetaryResult(flight, rho, gravity, drag, (0., 0., 0.), 0., buoyancy, water_drag)


class DensityFilter:
    """Rate-limited density so a change of nearest planet or atmosphere flag never steps the drag."""
    def __init__(self):
        self.value = None

    def step(self, target, dt):
        if self.value is None:self.value = target
        else:
            step = DENSITY_RATE*max(0., dt)
            self.value += max(-step, min(step, target-self.value))
        return self.value

    def reset(self):
        self.value = None
