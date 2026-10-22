"""T93 thruster purge after a ditch: a throttle quick-time procedure raced against the hull (user design
2026-10-19). Pure state; no game memory.

"Thrusters flooded, emergency purge in process, hull overpressure, integrity failing." The pilot purges
each thruster group in turn (user 2026-10-19): mains, retro, up, down, starboard, port, each by holding its
own control inside a band, then boost blasts the ship out of the water. Time spent inside the band
advances the stage; outside it, progress stops. Leaving the band for longer than MISS_AFTER is a miss: a direct hit to
the hull and more time on the stage (user: "a miss could give you a direct hit to the ship health and
extend the purge timer"). Meanwhile water pressure drains the hull, faster the deeper the ship is.
Purge done first: the engines relight. Hull at zero first: the ship is wrecked. Success costs nothing but
the hull damage taken (user). The hull meter is ours; the game's real hull health is a later step.
"""
from dataclasses import dataclass
import math
import random

# Lateral sign of the pilot's right. The flight model thrusts +lateral along basis row 0, and the ship triad is
# right-handed (right x up = forward, mod/additive_rotation.py), so row 0 points to the pilot's left: -lateral moves
# the ship to starboard (user flight 2026-10-20: STARBOARD and PORT prompts were reversed).
STARBOARD = -1.
PORT = -STARBOARD
# (name, input, sign, seconds in the band, band low, band high, drift amplitude, drift period s). The input
# is a TranslationCommand channel (throttle, vertical, lateral; sign picks the direction) or the boost
# button (0 or 1). All values GUESS, tune in flight.
STAGES = (('MAINS', 'throttle', 1., 2.5, .55, .80, .06, 3.),
          ('RETRO', 'throttle', -1., 2.5, .40, .70, 0., 0.),
          ('UP', 'vertical', 1., 2.5, .50, .80, 0., 0.),
          ('DOWN', 'vertical', -1., 2.5, .50, .80, .06, 2.5),
          ('STARBOARD', 'lateral', STARBOARD, 2.5, .50, .80, 0., 0.),
          ('PORT', 'lateral', PORT, 2.5, .50, .80, .06, 3.),
          ('BOOST', 'boost', 1., 1., .5, 1., 0., 0.))
# HUD arrow per stage (user 2026-10-20: "a big red arrow to show which thruster input to give"):
# (what moves, which way). Throttle arrows sit in a lever slot, strafe arrows stand alone, boost is chevrons.
ARROWS = {('throttle', 1.): ('throttle', 'up'), ('throttle', -1.): ('throttle', 'down'),
          ('vertical', 1.): ('strafe', 'up'), ('vertical', -1.): ('strafe', 'down'),
          ('lateral', STARBOARD): ('strafe', 'right'), ('lateral', PORT): ('strafe', 'left'), ('boost', 1.): ('boost', 'up')}
LABELS = {'throttle': {1.: 'THROTTLE FORWARD', -1.: 'THROTTLE REVERSE'}, 'vertical': {1.: 'STRAFE UP', -1.: 'STRAFE DOWN'},
          'lateral': {STARBOARD: 'STRAFE RIGHT', PORT: 'STRAFE LEFT'}, 'boost': {1.: 'HOLD BOOST'}}
# Timing after the first flight (user 2026-10-20: "the hull is failing too fast ... more warning to begin").
WARNING = 6.         # s of warning after the pilot wakes: the block and the first control show, the hull holds
GRACE = 2.           # s at the start of each stage to find its control without a miss
# T99: each stage opens on a spoken countdown ("Mains cleared. Purge retros in 3, 2, 1", mod/purge_voice.py). The
# band and lever show, the hull holds and nothing counts until "one" (the longest line needs 4.3 s).
COUNTDOWN = 4.3
COUNT_STEP = .8      # s between the spoken "three", "two" and "one"; "one" is the opening
MISS_AFTER = .4      # s outside the band before it counts as a miss (edge jitter is free)
MISS_COOLDOWN = 1.5  # s after a miss before another can count (one slip at a band edge is one miss)
MISS_HIT = .05       # hull share lost per miss
MISS_EXTEND = 1.     # s added to the current stage per miss
DRAIN_FREE = 5.      # u below the surface where pressure does no harm
DRAIN_AT_100 = .04   # hull share per second at 100 u deep (empty in about 25 s); linear with depth
# The pressure stops growing at this depth (user 2026-10-20: "enough time to complete the procedure"): the ship
# keeps sinking through the T99 countdowns, and past ~150 u a perfect run was wrecked. At 60 u a perfect run after
# the hardest impact keeps about 27 % hull (offline sim, tests/test_ditch_purge.py).
DRAIN_DEPTH_MAX = 60.
IMPACT_HIT = .1      # hull share lost per ditch speed of impact above the ditch speed, bounded
IMPACT_HIT_MAX = .25
IMPACT_TIME = .25  # stage time grows by this share per ditch speed above it, bounded
IMPACT_TIME_MAX = 1.5
VARIATION = .15    # stage time varies by up to this share, band centres by up to BAND_SHIFT
BAND_SHIFT = .05
DIFFICULTY = {'easy': 1.6, 'normal': 1., 'hard': .6}  # band width multiplier
HISTORY = 2.       # s of lever trail kept for the HUD graph


def drain_rate(depth):
    """Hull share per second lost to water pressure at depth u below the surface."""
    return DRAIN_AT_100*max(0., min(depth, DRAIN_DEPTH_MAX)-DRAIN_FREE)/(100.-DRAIN_FREE)


@dataclass
class Stage:
    name: str
    input: str
    sign: float
    required: float
    low: float
    high: float
    drift: float = 0.
    period: float = 0.


def stages(seed, impact_ratio=1., difficulty='normal'):
    """The stages for one ditch: varied by seed, longer after a harder hit, band width by difficulty."""
    rng = random.Random(seed); out = []
    stretch = min(IMPACT_TIME_MAX, 1.+IMPACT_TIME*max(0., impact_ratio-1.))
    width = DIFFICULTY.get(difficulty, 1.)
    for name, channel, sign, seconds, low, high, drift, period in STAGES:
        if channel == 'boost':  # a button: held is inside, no band to vary
            out.append(Stage(name, channel, sign, seconds, low, high, 0., 0.));continue
        centre = (low+high)/2.+rng.uniform(-BAND_SHIFT, BAND_SHIFT)
        half = (high-low)/2.*width
        low, high = max(.05, centre-half), min(1., centre+half)
        out.append(Stage(name, channel, sign, seconds*stretch*rng.uniform(1.-VARIATION, 1.+VARIATION), low, high, drift, period))
    return out


class Purge:
    def __init__(self, seed, impact_ratio=1., difficulty='normal', hull=1.):
        self.stages = stages(seed, impact_ratio, difficulty)
        self.index = 0; self.done = 0.; self.elapsed = 0.; self.stage_time = 0.
        self.outside = 0.; self.missed = False; self.misses = 0; self.cooldown = 0.
        self.warning = WARNING  # counts down first; nothing drains or advances until it ends
        self.countdown = 0.     # s until the current stage opens (after the warning and each stage change)
        self.hull = max(0., hull-min(IMPACT_HIT_MAX, IMPACT_HIT*max(0., impact_ratio-1.)))
        self.result = None  # None while running, then 'relit' or 'wrecked'
        self.history = []   # (elapsed, stage input value, inside); cleared at each stage change

    @property
    def stage(self):
        return self.stages[min(self.index, len(self.stages)-1)]

    def band(self, stage=None, t=None):
        """(low, high) of a stage at stage time t (now by default); PRIME drifts."""
        s = stage or self.stage; t = self.stage_time if t is None else t
        if not s.drift or not s.period:return s.low, s.high
        shift = s.drift*math.sin(2.*math.pi*t/s.period)
        shift = max(-s.low, min(1.-s.high, shift))
        return s.low+shift, s.high+shift

    def progress(self):
        """0..1 of the whole procedure, by required band time."""
        total = sum(s.required for s in self.stages)
        done = sum(s.required for s in self.stages[:self.index])+min(self.done, self.stage.required)
        return min(1., done/total) if total > 0. else 1.

    def value(self, inputs, stage=None):
        """The current stage's input, 0..1 in its own direction. inputs: throttle, vertical, lateral (signed
        units) and boost (bool)."""
        s = stage or self.stage
        if s.input == 'boost':return 1. if inputs.get('boost') else 0.
        return min(1., max(0., s.sign*float(inputs.get(s.input, 0.) or 0.)))

    def step(self, dt, inputs, depth):
        """Advance by dt seconds with the pilot's inputs (see value()) and the ship depth u below the
        surface. Returns the events of this step: 'begin' (warning over, first countdown), 'stage' (next
        countdown), 'go' (a countdown is over, the band counts), 'miss', 'relit', 'wrecked'."""
        events = []
        if self.result is not None or not dt > 0.:return events
        dt = min(dt, .25); lever = self.value(inputs)
        if self.warning > 0.:  # warning: the hull holds and nothing counts yet
            self.warning = max(0., self.warning-dt)
            if self.warning == 0.:
                events.append('begin'); self.countdown = COUNTDOWN
            return events
        if self.countdown > 0.:  # the lever trail shows where the pilot is; the hull holds
            self.elapsed += dt; self.countdown = max(0., self.countdown-dt)
            low, high = self.band(); self._trail(lever, low <= lever <= high)
            if self.countdown == 0.:events.append('go')
            return events
        self.elapsed += dt; self.stage_time += dt; self.cooldown = max(0., self.cooldown-dt)
        self.hull -= drain_rate(depth)*dt
        low, high = self.band()
        inside = low <= lever <= high
        self._trail(lever, inside)
        if inside:
            self.done += dt; self.outside = 0.; self.missed = False
        elif self.stage_time > GRACE:
            self.outside += dt
            if self.outside >= MISS_AFTER and not self.missed and self.cooldown == 0.:
                self.missed = True; self.misses += 1; self.cooldown = MISS_COOLDOWN
                self.hull -= MISS_HIT; self.stage.required += MISS_EXTEND
                events.append('miss')
        if self.hull <= 0.:
            self.hull = 0.; self.result = 'wrecked'; events.append('wrecked')
            return events
        if self.done >= self.stage.required:
            self.index += 1; self.done = 0.; self.stage_time = 0.; self.outside = 0.; self.missed = False
            self.history = []
            if self.index >= len(self.stages):
                self.index = len(self.stages)-1; self.done = self.stage.required
                self.result = 'relit'; events.append('relit')
            else:
                events.append('stage'); self.countdown = COUNTDOWN
        return events

    def _trail(self, lever, inside):
        self.history.append((self.elapsed, lever, inside))
        while self.history and self.history[0][0] < self.elapsed-HISTORY:self.history.pop(0)

    def preview(self, seconds=4., step=.25):
        """Band ahead for the HUD graph, assuming the pilot stays inside it: [(t ahead, low, high)]. A countdown
        shows the band waiting at its opening value."""
        out = []; index = self.index; done = self.done; t_stage = self.stage_time; t = 0.
        while t < self.countdown and t <= seconds and index < len(self.stages):
            out.append((t, *self.band(self.stages[index], t_stage))); t += step
        while t <= seconds and index < len(self.stages):
            s = self.stages[index]
            low, high = self.band(s, t_stage)
            out.append((t, low, high))
            t += step; done += step; t_stage += step
            if done >= s.required:index += 1; done = 0.; t_stage = 0.
        return out

    def count(self):
        """The count being spoken in a countdown: 3 or 2 in its last two steps, else None (the line before them,
        or the stage is open)."""
        if not self.countdown > 0. or self.countdown > 2.*COUNT_STEP:return None
        return 3 if self.countdown > COUNT_STEP else 2

    def prompt(self):
        return LABELS[self.stage.input][self.stage.sign]

    def arrow(self):
        return ARROWS[(self.stage.input, self.stage.sign)]

    def status(self):
        return {'stage': self.stage.name, 'prompt': self.prompt(), 'warning': self.warning, 'countdown': self.countdown,
                'stage_index': self.index, 'progress': self.progress(),
                'hull': self.hull, 'misses': self.misses, 'result': self.result, 'band': self.band()}
