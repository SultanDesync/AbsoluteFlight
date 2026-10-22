"""T55's game-independent translation law and command-freshness contract.

This module returns acceleration only. The native integration seam owns mass, time,
velocity and collision response. Coordinates are right, up, forward; basis vectors
are supplied by the qualified native reader. Boost is a mod-owned acceleration
multiplier supplied at the call boundary; it never changes the speed cap.
"""
from collections import deque
from dataclasses import dataclass, fields
import functools
import math

from flight_shaping import bipolar, shape

ZERO = (0., 0., 0.)
# The existing native helper rejects larger XYZ components. Automatic recovery
# must fit this ceiling together with pilot thrust, rather than become coast.
NATIVE_ACCELERATION_LIMIT = 1e6


# Momentum parity (user design, 2026-10-17): changing a velocity should cost what it took to build
# it. Building speed v under the acceleration fade takes T(v) = (cap/F) * tau(v/cap), with
# tau(x) = integral from 0 to x of du / (1 - u^k). Two caps follow, each blended in by `momentum`:
# - braking mirrors the fade, F * (1 - x^k): coming down the tail costs exactly what climbing it did,
#   so a flip-and-retro reversal takes exactly 2x the build-up at every speed;
# - sideways thrust (bending the vector) is capped at the average build-up acceleration
#   v / T(v) = F * x / tau(x), so bending at speed costs in proportion to the investment.
# Low speed keeps the full forward authority. Past PARITY_MAX_X of the cap (the fade's asymptote,
# pulse exits) the values at PARITY_MAX_X are used.
PARITY_MAX_X = .97
PARITY_STEPS = 512
PARITY_MIN_SPEED = 1.  # u/s; below this there is no velocity direction to redirect


@functools.lru_cache(maxsize=64)
def _build_time_table(exponent):
    """tau at PARITY_STEPS+1 even points over 0..PARITY_MAX_X (midpoint rule)."""
    step = PARITY_MAX_X/PARITY_STEPS
    table = [0.]
    for i in range(PARITY_STEPS):
        u = (i+.5)*step
        table.append(table[-1]+step/max(1e-6, 1.-u**exponent))
    return tuple(table)


def mirrored_braking(speed, cap, forward_authority, exponent):
    """Faded forward authority at `speed`: braking through the tail costs what climbing it did."""
    if forward_authority <= 0. or cap <= 0.:
        return 0.
    if exponent <= 0.:
        return forward_authority
    x = min(max(speed/cap, 0.), PARITY_MAX_X)
    return forward_authority*(1.-x**exponent)


def parity_acceleration(speed, cap, forward_authority, exponent):
    """Average acceleration it took to build `speed` from rest with this authority and fade."""
    if forward_authority <= 0. or cap <= 0.:
        return 0.
    if exponent <= 0.:
        return forward_authority  # no fade: the build-up ran at full authority throughout
    x = min(max(speed/cap, 0.), PARITY_MAX_X)
    if x <= 0.:
        return forward_authority
    table = _build_time_table(float(exponent))
    position = x/PARITY_MAX_X*PARITY_STEPS
    i = min(int(position), PARITY_STEPS-1)
    tau = table[i]+(table[i+1]-table[i])*(position-i)
    return forward_authority*x/tau


def _finite(*values):
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError('Non-finite flight input')


def _clamp(value, low, high):
    return min(high, max(low, value))


def _vec(value):
    if len(value) != 3:
        raise ValueError('Flight vectors need exactly three components')
    _finite(*value)
    return tuple(float(x) for x in value)


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _norm(v):
    return math.sqrt(_dot(v, v))


@dataclass(frozen=True)
class Basis:
    right: tuple = (1., 0., 0.)
    up: tuple = (0., 1., 0.)
    forward: tuple = (0., 0., 1.)

    def __post_init__(self):
        axes = tuple(_vec(v) for v in (self.right, self.up, self.forward))
        # Reject malformed native snapshots rather than silently scaling thrust.
        if any(abs(_dot(v, v)-1.) > .002 for v in axes):
            raise ValueError('Flight basis must have unit axes')
        if any(abs(_dot(axes[i], axes[j])) > .002
               for i, j in ((0, 1), (0, 2), (1, 2))):
            raise ValueError('Flight basis must be orthogonal')
        for key, value in zip(('right', 'up', 'forward'), axes):
            object.__setattr__(self, key, value)

    def to_world(self, body):
        body = _vec(body)
        return tuple(sum(body[i]*axis[j] for i, axis in
                         enumerate((self.right, self.up, self.forward))) for j in range(3))

    def to_body(self, world):
        world = _vec(world)
        return tuple(_dot(world, axis) for axis in (self.right, self.up, self.forward))


@dataclass(frozen=True)
class FlightProfile:
    acceleration_scale: float = 1.
    forward_ratio: float = 1.
    retro_ratio: float = .6
    lateral_ratio: float = .4
    up_ratio: float = .5
    down_ratio: float = .4
    speed_cap: float = 1200.
    speed_cap_multiplier: float = 1.
    brake_strength: float = 1.
    settle_time: float = 1.
    hold_threshold: float = .02
    jerk_limit: float = 0.
    speed_hold_gain: float = 1.
    auto_bank_on_yaw: bool = False
    boost_source: str = 'native'
    boost_multiplier: float = 1.
    accel_fade_exponent: float = 0.
    brake_floor: float = 1.
    overspeed_bleed_time: float = 0.
    boost_enabled: bool = False
    boost_heat_time: float = 4.
    boost_cool_time: float = 6.
    boost_relight_level: float = .3
    momentum: float = 0.  # 0 = off .. 1 = strict momentum parity
    boost_cold_strength: float = .4  # fraction of the boost multiplier delivered when cold
    boost_hot_zone: float = .7       # heat where boost reaches full strength; hot zone up to overheat

    def __post_init__(self):
        numeric = [getattr(self, f.name) for f in fields(self)
                   if f.name not in ('auto_bank_on_yaw', 'boost_source', 'boost_enabled')]
        _finite(*numeric)
        if any(v < 0. for v in numeric):
            raise ValueError('Flight tuning values cannot be negative')
        if self.speed_cap <= 0. or self.speed_cap_multiplier <= 0. or self.settle_time <= 0.:
            raise ValueError('Speed cap, its multiplier and settle time must be positive')
        if not 0. <= self.brake_strength <= 1.:
            raise ValueError('Brake strength is a fraction of available authority')
        if not 0. <= self.brake_floor <= 1.:
            raise ValueError('Brake floor must be a fraction')
        if self.boost_heat_time <= 0. or self.boost_cool_time <= 0.:
            raise ValueError('Boost heat and cooling times must be positive')
        if not 0. <= self.boost_relight_level < 1.:
            raise ValueError('Boost relight level must be below full heat')
        if not 0. <= self.momentum <= 1.:
            raise ValueError('Momentum must be a fraction')
        if not 0. <= self.boost_cold_strength <= 1. or not 0. < self.boost_hot_zone < 1.:
            raise ValueError('Boost cold strength must be a fraction and the hot zone inside 0..1')
        if self.boost_source not in ('native', 'fixed') or self.boost_multiplier < 1.:
            raise ValueError('Invalid boost source or multiplier')
        if not isinstance(self.boost_enabled, bool):
            raise ValueError('Boost enabled must be a boolean')

    @classmethod
    def from_mapping(cls, mapping):
        """Accept a settings profile while ignoring fields reserved for later phases."""
        names = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in mapping.items() if key in names})

    @property
    def effective_cap(self):
        return self.speed_cap*self.speed_cap_multiplier

    def authority(self, native_thrust_force, boost_multiplier=1.):
        _finite(native_thrust_force, boost_multiplier)
        if native_thrust_force < 0. or boost_multiplier <= 0.:
            raise ValueError('Invalid native thrust or boost multiplier')
        scale = native_thrust_force*self.acceleration_scale*boost_multiplier
        # Negative and positive body-axis authorities, with no normalization.
        return ((scale*self.lateral_ratio, scale*self.lateral_ratio),
                (scale*self.down_ratio, scale*self.up_ratio),
                (scale*self.retro_ratio, scale*self.forward_ratio))


@dataclass(frozen=True)
class LeverZones:
    reverse_end: float = .10
    parking_end: float = .18
    coast_end: float = .22
    cruise_start: float = .52
    cruise_end: float = .58
    full_start: float = .85
    boost_start: float = .90
    cruise_level: float = .5
    cruise_enabled: bool = True
    boost_enabled: bool = False  # requests the mod-owned multiplier, never native boost
    reverse_mode: str = 'full'

    def __post_init__(self):
        positions = (self.reverse_end, self.parking_end, self.coast_end,
                     self.cruise_start, self.cruise_end, self.full_start, self.boost_start)
        _finite(*positions, self.cruise_level)
        if not 0. < positions[0] or not positions[-1] < 1.:
            raise ValueError('Lever zones must fit strictly inside the lever range')
        if any(a >= b for a, b in zip(positions, positions[1:])):
            raise ValueError('Lever zone boundaries must be strictly ordered')
        if not 0. <= self.cruise_level <= 1. or self.reverse_mode not in ('full', 'proportional'):
            raise ValueError('Invalid cruise level or reverse mode')


@dataclass(frozen=True)
class ThrottleDemand:
    value: float = 0.  # signed thrust fraction, or signed fraction of cap in speed mode
    zone: str = 'coast'
    parking: bool = False
    boost_requested: bool = False
    speed_control: bool = False

    def __post_init__(self):
        _finite(self.value)
        if not -1. <= self.value <= 1.:
            raise ValueError('Throttle demand must be a signed unit value')


def map_lever(unit, zones=None, control='speed', inverted=False):
    """F8 positions and F9 nose-forward speed control; coast disables the follower.

    Intervals include their lower boundary. With cruise disabled its plateau joins
    the forward ramp; with boost disabled the upper zone is ordinary full thrust.
    """
    _finite(unit)
    zones = zones or LeverZones()
    if control not in ('speed', 'thrust'):
        raise ValueError('Lever controls must be speed or thrust')
    p = 1.-_clamp(unit, 0., 1.) if inverted else _clamp(unit, 0., 1.)
    if p < zones.reverse_end:
        value = -1. if zones.reverse_mode == 'full' else -(zones.reverse_end-p)/zones.reverse_end
        zone = 'reverse'
    elif p < zones.parking_end:
        return ThrottleDemand(zone='parking', parking=True)
    elif p <= zones.coast_end:
        return ThrottleDemand()
    elif p >= zones.boost_start and zones.boost_enabled:
        return ThrottleDemand(1., 'boost', boost_requested=True, speed_control=control == 'speed')
    elif p >= zones.full_start:
        value, zone = 1., 'full'
    elif not zones.cruise_enabled:
        value = (p-zones.coast_end)/(zones.full_start-zones.coast_end)
        zone = 'forward'
    elif p < zones.cruise_start:
        value = zones.cruise_level*(p-zones.coast_end)/(zones.cruise_start-zones.coast_end)
        zone = 'forward'
    elif p <= zones.cruise_end:
        value, zone = zones.cruise_level, 'cruise'
    else:
        value = zones.cruise_level+(1.-zones.cruise_level)*(p-zones.cruise_end)/(zones.full_start-zones.cruise_end)
        zone = 'forward'
    return ThrottleDemand(value, zone, speed_control=control == 'speed')


def map_hosas(unit, deadzone=.05, saturation=1., inverted=False):
    """Centred bipolar stick: negative demand is retro thrust, never native braking."""
    value = shape(bipolar(unit, inverted), 1., saturation, deadzone)
    return ThrottleDemand(value, 'thrust' if value else 'coast')


@dataclass(frozen=True)
class FlightResult:
    acceleration_world: tuple
    acceleration_body: tuple
    parking: bool
    cap_active: bool
    target_speed: object = None
    reason: str = 'flight'
    bleeding_overspeed: bool = False
    pilot_acceleration_world: object = None


def _envelope(acceleration, velocity, cap):
    speed = _norm(velocity)
    if speed < cap or speed == 0.:
        return acceleration, False
    outward = _dot(acceleration, velocity)/speed
    if outward <= 0.:
        return acceleration, True
    unit = tuple(v/speed for v in velocity)
    return tuple(a-outward*d for a, d in zip(acceleration, unit)), True


def compute_acceleration(profile, lateral, vertical, throttle, velocity_world, basis,
                         native_thrust_force, dt, parking_latched=False,
                         previous_acceleration_world=ZERO, boost_multiplier=1.,
                         command_fresh=True):
    """Return the replacement XYZ acceleration; never an integrated velocity.

    Runtime admission/fallback is deliberately outside this pure law. A stale
    command bypasses jerk smoothing and returns zero immediately. Parking uses a
    timestep-correct exponential controller, bounded by directional authority and
    the acceleration that would stop each component in this invocation. Its safety
    bound takes precedence over jerk smoothing at engage and near zero.
    """
    _finite(lateral, vertical, dt)
    if dt <= 0.:
        raise ValueError('Flight timestep must be positive')
    velocity = _vec(velocity_world)
    previous = _vec(previous_acceleration_world)
    parking = bool(parking_latched or throttle.parking)
    if not command_fresh:
        return FlightResult(ZERO, ZERO, parking, False, reason='expired')
    # The parking controller retains its accepted unboosted stopping bounds.
    limits = profile.authority(native_thrust_force, 1. if parking else boost_multiplier)
    body_velocity = basis.to_body(velocity)
    target_speed = None
    if parking:
        gain = -math.expm1(-dt/profile.settle_time)/dt
        if _norm(velocity) <= profile.hold_threshold:
            gain = 1./dt
        requested = tuple(_clamp(-v*gain, -negative*profile.brake_strength,
                                 positive*profile.brake_strength)
                          for v, (negative, positive) in zip(body_velocity, limits))
    else:
        fractions = (_clamp(lateral, -1., 1.), _clamp(vertical, -1., 1.), throttle.value)
        requested = tuple(u*(positive if u >= 0. else negative)
                          for u, (negative, positive) in zip(fractions, limits))
        if throttle.speed_control:
            target_speed = throttle.value*profile.effective_cap
            # Exponential discretization avoids target overshoot at low frame rates.
            gain = -math.expm1(-profile.speed_hold_gain*dt)/dt
            forward = _clamp((target_speed-body_velocity[2])*gain, -limits[2][0], limits[2][1])
            requested = (requested[0], requested[1], forward)
    acceleration = basis.to_world(requested)
    if profile.jerk_limit > 0.:
        delta = tuple(a-b for a, b in zip(acceleration, previous))
        change = _norm(delta)
        step = profile.jerk_limit*dt
        if change > step:
            acceleration = tuple(b+d*step/change for b, d in zip(previous, delta))
    # A live profile change or a rotating basis can lower an axis's current
    # authority below the previous world command. That bound overrides smoothing.
    body = basis.to_body(acceleration)
    acceleration = basis.to_world(tuple(_clamp(a, -negative, positive)
                                       for a, (negative, positive) in zip(body, limits)))
    if parking:
        # Any previous thrust/jerk state must neither accelerate along drift nor
        # carry a braking component through zero into reverse motion.
        body = basis.to_body(acceleration)
        safe = []
        for a, v, (negative, positive) in zip(body, body_velocity, limits):
            bound = min(abs(v)/dt, (negative if v > 0. else positive)*profile.brake_strength)
            safe.append(_clamp(a, -bound, 0.) if v > 0. else _clamp(a, 0., bound) if v < 0. else 0.)
        acceleration = basis.to_world(tuple(safe))
    speed = _norm(velocity)
    cap = profile.effective_cap
    if not parking and 0. < speed < cap:
        parallel = _dot(acceleration, velocity)/speed
        factor = 1.
        if parallel > 0. and profile.accel_fade_exponent > 0.:
            factor = _clamp(1.-(speed/cap)**profile.accel_fade_exponent, 0., 1.)
        elif parallel < 0.:
            factor = max(profile.brake_floor, min(1., .25*cap/speed))
        if factor != 1.:
            # Change only the radial component. Steering remains fully available.
            acceleration = tuple(a+parallel*(factor-1.)*v/speed
                                 for a, v in zip(acceleration, velocity))
    if not parking and profile.momentum > 0. and speed > PARITY_MIN_SPEED:
        # Momentum parity: braking mirrors the fade and sideways thrust is capped at the average
        # build-up acceleration. Outward thrust keeps the fade alone.
        unit = tuple(v/speed for v in velocity)
        parallel = _dot(acceleration, unit)
        side = tuple(a-parallel*u for a, u in zip(acceleration, unit))

        def blend(magnitude, limit):
            return magnitude if magnitude <= limit else magnitude*(1.-profile.momentum*(1.-limit/magnitude))
        forward = limits[2][1]; exponent = profile.accel_fade_exponent
        if parallel < 0.:
            parallel = -blend(-parallel, mirrored_braking(speed, cap, forward, exponent))
        magnitude = _norm(side)
        if magnitude > 0.:
            keep = blend(magnitude, parity_acceleration(speed, cap, forward, exponent))/magnitude
            side = tuple(s*keep for s in side)
        acceleration = tuple(parallel*u+s for u, s in zip(unit, side))
    acceleration, cap_active = _envelope(acceleration, velocity, cap)
    # Automatic bleed is outside pilot authority and jerk smoothing. Retain the
    # bounded pilot command separately so consumers do not smooth bleed back in
    # as residual pilot retro on the next invocation.
    pilot_acceleration = acceleration
    bleeding = bool(not parking and speed > cap and profile.overspeed_bleed_time > 0.)
    if bleeding:
        # Remove outward pilot thrust BEFORE adding automatic braking, otherwise
        # it could cancel the bleed. Pilot retro and perpendicular steering stack.
        deceleration = (speed-cap)*(-math.expm1(-dt/profile.overspeed_bleed_time)/dt)
        unit = tuple(v/speed for v in velocity)
        headroom = min(((a+NATIVE_ACCELERATION_LIMIT)/u if u > 0.
                        else (NATIVE_ACCELERATION_LIMIT-a)/(-u))
                       for a, u in zip(acceleration, unit) if u != 0.)
        headroom = max(0., headroom)
        if deceleration > headroom:
            # Stay inside the bridge ceiling even if the limiting division
            # rounds upward. Only extreme recovery takes longer than its tau.
            deceleration = math.nextafter(headroom, 0.)
        acceleration = tuple(a-deceleration*u for a, u in zip(acceleration, unit))
        bleeding = deceleration > 0.
    return FlightResult(acceleration, basis.to_body(acceleration), parking, cap_active,
                        target_speed, 'parking' if parking else 'flight', bleeding, pilot_acceleration)


@dataclass(frozen=True)
class LeaseSample:
    state: str
    usable: bool
    generation: object
    age: object
    reason: str
    now: float


class CommandLease:
    """100 ms generation lease with bounded timing receipts and explicit recovery.

    Submit a generation once, when a genuinely new coherent command is published;
    callback frequency must not renew it. A received timestamp is never a new
    generation. An expired lease can recover only from a strictly newer generation
    whose original production time is still within TTL. The first new generation
    is recovering and still coasts; a subsequent new generation becomes fresh.
    """
    def __init__(self, ttl=.1, capacity=256):
        _finite(ttl)
        if ttl <= 0. or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError('Command lease needs a positive TTL and ring capacity')
        self.ttl = ttl
        self.timing_ring = deque(maxlen=capacity)
        self.generation = None
        self.produced_at = None
        self.state = 'expired'
        self._last_now = None

    def _clock(self, now):
        _finite(now)
        if self._last_now is not None and now < self._last_now:
            raise ValueError('Command lease clock moved backwards')
        self._last_now = now

    def submit(self, generation, now, produced_at=None):
        self._clock(now)
        produced_at = now if produced_at is None else produced_at
        _finite(produced_at)
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError('Command generation must be a nonnegative integer')
        if self.generation is not None and generation <= self.generation:
            return False
        if produced_at > now or now-produced_at >= self.ttl:
            return False
        was_expired = (self.produced_at is not None and
                       (self.state == 'expired' or now-self.produced_at >= self.ttl))
        if was_expired and self.state != 'expired':
            self.timing_ring.append(LeaseSample('expired', False, self.generation,
                                    now-self.produced_at, 'command deadline', now))
        self.generation, self.produced_at = generation, produced_at
        self.state = 'recovering' if was_expired else 'fresh'
        return True

    def sample(self, now):
        self._clock(now)
        age = None if self.produced_at is None else now-self.produced_at
        if age is None or age >= self.ttl:
            self.state = 'expired'
            reason = 'no command' if age is None else 'command deadline'
        else:
            reason = 'new generation' if self.state == 'recovering' else 'within deadline'
        receipt = LeaseSample(self.state, self.state == 'fresh', self.generation,
                              age, reason, now)
        self.timing_ring.append(receipt)
        return receipt
