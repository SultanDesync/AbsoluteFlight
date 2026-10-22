"""Measured maneuver disruption for enemy aim; no hooks, memory reads or game writes.

The caller supplies only positively admitted, primary-context native work snapshots.
All clocks are GetTickCount64 milliseconds; native ``dt_eff`` is simulation seconds.
Work velocity is sampled before integration, so the preceding snapshot's timestep
is the interval used for the next velocity difference. World-space differences
make the score independent of camera, nose attitude and coordinate orientation.
"""
from dataclasses import dataclass
import math


AIM_KNOBS = {
    'ai-aim-precision': (1., .25, 1.),
    'ai-evasion-loss': (2., 0., 5.),
    'ai-evasion-acceleration': (20., 1., 200.),
    'ai-aim-recovery': (.5, .1, 2.),
}
SAMPLE_MAX_AGE_MS = 150.
ATTACK_SECONDS = .08
ZERO = (0., 0., 0.)
# These reject transfers/teleports/solver discontinuities, rather than turning
# them into maximum evasion. Both ceilings are deliberately above flight tuning:
# a 10,000 u/s^2 measured acceleration or a 1,000 u/s one-frame jump reseeds.
MAX_ACCELERATION = 10000.
MAX_VELOCITY_STEP = 1000.


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def tuning_values(values=None):
    """Fill old settings with defaults and bound finite numeric menu values."""
    values = values or {}
    result = {}
    for key, (default, low, high) in AIM_KNOBS.items():
        value = values.get(key, default)
        result[key] = min(high, max(low, float(value))) if _finite(value) else default
    return result


def _vector(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3 or not all(map(_finite, value)):
        raise ValueError('invalid world vector')
    return tuple(float(v) for v in value)


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _basis(work):
    axes = []
    for key in ('right', 'up', 'forward'):
        value = work[key]
        if not isinstance(value, (tuple, list)) or len(value) not in (3, 4):
            raise ValueError('invalid body basis')
        axes.append(_vector(value[:3]))
    if (any(abs(_dot(a, a)-1.) > .002 for a in axes)
            or any(abs(_dot(axes[i], axes[j])) > .002
                   for i, j in ((0, 1), (0, 2), (1, 2)))):
        raise ValueError('nonorthonormal body basis')


@dataclass(frozen=True)
class EvasionSample:
    tick: float
    ship: int
    body: int
    invocation: int
    acceleration: float
    disruption: float
    acceleration_world: tuple = ZERO


class EvasionObserver:
    """One flight-thread writer, with immutable leased samples for aim consumers.

    ``observe`` returns a sample or None. A new baseline publishes zero; repeated
    invocation reads neither advance the observer nor renew the sample lease.
    Invalid snapshots reset, while valid identity/gap/discontinuity changes seed
    a zero baseline and cannot inherit an earlier ship's disruption.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self._previous = None
        self._latest = None
        self._disruption = 0.

    def _seed(self, snapshot):
        self._previous = snapshot
        self._disruption = 0.
        tick, _, identity, invocation, _, _ = snapshot
        self._latest = EvasionSample(tick, identity[0], identity[1], invocation, 0., 0.)
        return self._latest

    def observe(self, work, now, values=None):
        """Consume an admitted native work snapshot; never retain the input dict."""
        try:
            if not _finite(now) or now < 0.:
                raise ValueError('invalid observer clock')
            identity = tuple(work[key] for key in ('ship', 'body', 'state', 'context'))
            if (any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in identity)
                    or isinstance(work['context_id'], bool) or work['context_id'] != 1):
                raise ValueError('invalid primary identity')
            invocation = work['invocation']
            if isinstance(invocation, bool) or not isinstance(invocation, int) or invocation <= 0:
                raise ValueError('invalid invocation')
            previous = self._previous
            # A worker/hook reading one invocation twice must not add simulated
            # time, clear an established score, or extend freshness.
            if previous and identity == previous[2] and invocation == previous[3]:
                return self.sample(identity[0], identity[1], now)
            tick, dt = work['tick'], work['dt_eff']
            if (not _finite(tick) or tick < 0. or not 0. <= now-tick <= SAMPLE_MAX_AGE_MS
                    or not _finite(dt) or not .0001 <= dt <= .15):
                raise ValueError('invalid native time')
            velocity = _vector(work['outer_velocity'])
            _basis(work)
            snapshot = (float(tick), float(now), identity, invocation, velocity, float(dt))
            if (previous is None or identity != previous[2] or invocation != previous[3]+1
                    or not 0. < tick-previous[0] <= SAMPLE_MAX_AGE_MS
                    or not 0. < now-previous[1] <= SAMPLE_MAX_AGE_MS):
                return self._seed(snapshot)
            step = tuple(a-b for a, b in zip(velocity, previous[4]))
            acceleration_world = tuple(v/previous[5] for v in step)
            acceleration = math.hypot(*acceleration_world)
            if (not math.isfinite(acceleration) or acceleration > MAX_ACCELERATION
                    or math.hypot(*step) > MAX_VELOCITY_STEP):
                return self._seed(snapshot)
            knobs = tuning_values(values)
            target = min(1., acceleration/knobs['ai-evasion-acceleration'])
            tau = ATTACK_SECONDS if target > self._disruption else knobs['ai-aim-recovery']
            weight = -math.expm1(-previous[5]/tau)
            self._disruption = min(1., max(0., self._disruption + (target-self._disruption)*weight))
            self._previous = snapshot
            self._latest = EvasionSample(float(tick), identity[0], identity[1], invocation,
                                         acceleration, self._disruption, acceleration_world)
            return self._latest
        except (KeyError, TypeError, ValueError, OverflowError):
            self.reset()
            return None

    def sample(self, ship, body, now):
        """Return only a fresh sample for the currently selected ship/body."""
        sample = self._latest
        if (sample is None or not _finite(now) or (sample.ship, sample.body) != (ship, body)
                or not 0. <= now-sample.tick <= SAMPLE_MAX_AGE_MS):
            return None
        return sample
