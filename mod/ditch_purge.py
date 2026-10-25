"""T93 thruster purge after a ditch, raced against the hull (user design 2026-10-19; redesigned 2026-10-24 from the
browser prototype tools/prototypes/purge_gates.html). Pure state; no game memory.

"Thrusters flooded, emergency purge in process, hull overpressure, integrity failing." Two styles (Ship Damage page,
*Purge style*):

Telemetry (default). Short: mains, then launch. Full (user 2026-10-24: "optional full purge cycle for all
translational thrusters"): mains, retro, up, down, starboard, port. Each stage opens on the spoken countdown with the thrust at idle. On "one" an ignition window opens and the thrust
must rise into the burn band while it is open (user: "the timing to start thrust input can't be wide open"): already
in the band when it opens doesn't count, so the pilot comes up from below. Then the band is held, as before: time
inside advances the stage, leaving it for MISS_AFTER is a miss. The readout draws the requirement as one trace: an
idle band at 0, a steep rise, the burn band (user: "a band at 0 then rise steeply to the burn phase level").

Gates (default off, user sketch 2026-10-24). A side-scroller: mains sets forward speed (spooling up, coasting down
when released), strafe up lifts against gravity, neutral falls; thread the gates, the sea floor scrapes.

Both: a miss is a direct hit to the hull (user 2026-10-19). Water pressure drains the hull, faster the deeper the ship
and the longer the purge has run (user 2026-10-24: "the hull integrity countdown should provide pressure to go
faster"). Purge done first: the engines relight. Hull at zero first: the ship is wrecked. Success costs nothing but
the hull damage taken (user). The hull meter is ours; the game's real hull health is a later step.
"""
from dataclasses import dataclass
import math
import random

STYLES = ('telemetry', 'gates')
# Lateral sign of the pilot's right. The flight model thrusts +lateral along basis row 0, and the ship triad is
# right-handed (right x up = forward, mod/additive_rotation.py), so row 0 points to the pilot's left: -lateral moves
# the ship to starboard (user flight 2026-10-20: STARBOARD and PORT prompts were reversed).
STARBOARD = -1.
PORT = -STARBOARD
# Timing after the first flight (user 2026-10-20: "the hull is failing too fast ... more warning to begin").
WARNING = 6.         # s of warning after the pilot wakes: the block and the first control show, the hull holds
# T99: each stage opens on a spoken countdown ("Mains cleared. Purge launch thrusters in 3, 2, 1", mod/purge_voice.py).
# The hull holds and nothing counts until "one" (the longest line needs 4.3 s).
COUNTDOWN = 4.3
COUNT_STEP = .8      # s between the spoken "three", "two" and "one"; "one" is the opening
MISS_AFTER = .4      # s outside the band before it counts as a miss (edge jitter is free)
MISS_COOLDOWN = 1.5  # s after a miss before another can count (one slip at a band edge is one miss)
MISS_HIT = .05       # hull share lost per miss (a missed band, a missed ignition window, a gate wall)
MISS_EXTEND = 1.     # s added to the current stage per miss
DRAIN_FREE = 5.      # u below the surface where pressure does no harm
DRAIN_AT_100 = .04   # hull share per second at 100 u deep (empty in about 25 s); linear with depth
# The depth pressure stops growing at this depth (user 2026-10-20: "enough time to complete the procedure").
DRAIN_DEPTH_MAX = 60.
DRAIN_DOUBLE = 30.   # s of running purge after which the drain has doubled (prototype default, user 2026-10-24)
IMPACT_HIT = .1      # hull share lost per ditch speed of impact above the ditch speed, bounded
IMPACT_HIT_MAX = .25
IMPACT_TIME = .25    # hold time grows by this share per ditch speed above it, bounded
IMPACT_TIME_MAX = 1.5
HISTORY = 2.         # s of lever trail kept for the HUD graph

# Telemetry stages per purge length: (name, input, sign, voice). The input is a TranslationCommand channel; voice
# picks the stage's countdown line (purge_voice.countdown_clip). Values from the prototype the user tuned (2026-10-24).
LENGTHS = {
    'short': (('MAINS', 'throttle', 1., 'MAINS'), ('LAUNCH', 'vertical', 1., 'LAUNCH')),
    'full': (('MAINS', 'throttle', 1., 'MAINS'), ('RETRO', 'throttle', -1., 'RETRO'), ('UP', 'vertical', 1., 'UP'),
             ('DOWN', 'vertical', -1., 'DOWN'), ('STARBOARD', 'lateral', STARBOARD, 'STARBOARD'),
             ('PORT', 'lateral', PORT, 'PORT')),
}
HOLD = {'short': 7., 'full': 2.5}  # s inside the burn band per stage: short about 30 s in all (user 2026-10-24), full the first design's 2.5 s
BAND = .25           # burn band width at Normal
CENTRE = (.4, .75)   # range of the burn band centre (seeded)
DRIFT = .06          # band drift amplitude and period
DRIFT_PERIOD = 3.
IDLE = .1            # top of the idle band through the countdown
WINDOW = .6          # s the ignition window stays open after "one"
RISE = .25           # s the requirement takes to climb from idle to the burn band (the head of the window)
RETRY = 2.           # s to the next window after a missed one
IGNITE_GRACE = .6    # s after ignition before leaving the band counts
VARIATION = .15      # hold time varies by up to this share
DIFFICULTY = {'easy': 1.6, 'normal': 1., 'hard': .6}  # band width multiplier
# HUD cue per input (user 2026-10-24): forward = a green arrow lying flat below the reticle and receding into it,
# back = the same arrow coming toward the pilot; the strafes = a red arrow standing on a deck line beside the reticle
# on that side, chevrons moving out.
ARROWS = {('throttle', 1.): 'forward', ('throttle', -1.): 'back', ('vertical', 1.): 'up', ('vertical', -1.): 'down',
          ('lateral', STARBOARD): 'right', ('lateral', PORT): 'left'}
LABELS = {('throttle', 1.): 'THROTTLE FORWARD', ('throttle', -1.): 'THROTTLE REVERSE', ('vertical', 1.): 'STRAFE UP',
          ('vertical', -1.): 'STRAFE DOWN', ('lateral', STARBOARD): 'STRAFE RIGHT', ('lateral', PORT): 'STRAFE LEFT'}

# Gates: world units, the course H tall (0 = sea floor); x is distance travelled. Prototype defaults.
H = 100.
SHIP_W, SHIP_H, GATE_T = 10., 5., 6.
FIRST_GATE = 120.
GIVE = 1.            # hitbox forgiveness on each side
GRAVITY = 60.        # u/s²
LIFT = 140.          # u/s² at full strafe up (hover at 43 %)
VDRAG = 1.5          # /s vertical drag
SPEED = 60.          # u/s at full mains
ACCEL = 40.          # u/s² spool up
COAST = 15.          # u/s² coast down with mains released
GAP = {'easy': 40., 'normal': 30., 'hard': 22.}
SPACING = 90.
GATES = 8
STALL = .6           # s at STALL_SPEED after a wall hit; then the ship slips through
STALL_SPEED = .2
SCRAPE_HIT = .03
SCRAPE_COOLDOWN = 1.


def drain_rate(depth):
    """Hull share per second lost to water pressure at depth u below the surface."""
    return DRAIN_AT_100*max(0., min(depth, DRAIN_DEPTH_MAX)-DRAIN_FREE)/(100.-DRAIN_FREE)


def pressure(t):
    """Drain multiplier after t seconds of running purge."""
    return 1.+t/DRAIN_DOUBLE


def time_to_fail(hull, depth, t):
    """Seconds until the hull is gone at this depth, misses aside, or None while the depth does no harm. The rate
    grows linearly with purge time: hull = r*x + g*x²/2."""
    base = drain_rate(depth)
    if base <= 0.:return None
    r, g = base*pressure(t), base/DRAIN_DOUBLE
    return (-r+math.sqrt(r*r+2.*g*max(0., hull)))/g


@dataclass
class Stage:
    name: str
    input: str
    sign: float
    voice: str
    required: float
    centre: float = .5
    width: float = BAND
    flip: float = 1.


def stages(seed, impact_ratio=1., difficulty='normal', length='short'):
    """The telemetry stages for one ditch: varied by seed, longer after a harder hit, band width by difficulty."""
    rng = random.Random(seed); out = []
    length = length if length in LENGTHS else 'short'
    stretch = min(IMPACT_TIME_MAX, 1.+IMPACT_TIME*max(0., impact_ratio-1.))
    width = BAND*DIFFICULTY.get(difficulty, 1.)
    for name, channel, sign, voice in LENGTHS[length]:
        out.append(Stage(name, channel, sign, voice, HOLD[length]*stretch*rng.uniform(1.-VARIATION, 1.+VARIATION),
                         rng.uniform(*CENTRE), width, -1. if rng.random() < .5 else 1.))
    return out


class _Race:
    """What both styles share: the warning, the countdown, the hull and its drain, the outcome."""
    def __init__(self, impact_ratio, hull):
        self.warning = WARNING  # counts down first; nothing drains or advances until it ends
        self.countdown = 0.     # s until the current stage opens
        self.elapsed = 0.       # s since the warning ended
        self.purge_time = 0.    # s the hull has been draining (sets the pressure growth)
        self.hull = max(0., hull-min(IMPACT_HIT_MAX, IMPACT_HIT*max(0., impact_ratio-1.)))
        self.misses = 0; self.cooldown = 0.
        self.result = None      # None while running, then 'relit' or 'wrecked'
        self.history = []       # HUD trail; cleared at each stage change
        self.depth = 0.         # last depth seen, for the failure clock

    def _warn(self, dt, events):
        """True while the warning runs; its end starts the first countdown."""
        if self.warning <= 0.:return False
        self.warning = max(0., self.warning-dt)
        if self.warning == 0.:
            events.append('begin'); self._open(COUNTDOWN)
        return True

    def _open(self, countdown):
        self.countdown = countdown

    def _drain(self, dt, depth):
        self.hull -= drain_rate(depth)*pressure(self.purge_time)*dt; self.purge_time += dt

    def _failed(self, events):
        if self.hull > 0.:return False
        self.hull = 0.; self.result = 'wrecked'; events.append('wrecked')
        return True

    def count(self):
        """The count being spoken in a countdown: 3 or 2 in its last two steps, else None (the line before them,
        or the stage is open)."""
        if not self.countdown > 0. or self.countdown > 2.*COUNT_STEP:return None
        return 3 if self.countdown > COUNT_STEP else 2

    def fail_in(self):
        """Seconds to hull failure at the last depth seen, or None."""
        return time_to_fail(self.hull, self.depth, self.purge_time)


class Purge(_Race):
    """Telemetry style. Phases per stage: countdown (idle, the hull holds), window (ignite), retry (after a missed
    window: the hull drains until the next one), burn (hold the band)."""
    style = 'telemetry'

    def __init__(self, seed, impact_ratio=1., difficulty='normal', hull=1., length='short'):
        super().__init__(impact_ratio, hull)
        self.stages = stages(seed, impact_ratio, difficulty, length)
        self.index = 0; self.done = 0.; self.outside = 0.; self.missed = False; self.grace = 0.
        self.phase = 'countdown'; self.open_at = COUNTDOWN; self.stage_start = 0.
        self.was_out = False; self.early = False
        self.flow = 0.          # HUD arrow phase: advances faster with more thrust

    def _open(self, countdown):
        self.countdown = countdown; self.open_at = self.elapsed+countdown

    @property
    def stage(self):
        return self.stages[min(self.index, len(self.stages)-1)]

    @property
    def burn_start(self):
        return self.open_at+RISE

    def burn_band(self, stage=None, t=None):
        """(low, high) of the burn band at t s after the burn start (now by default); it drifts."""
        s = stage or self.stage; t = self.elapsed-self.burn_start if t is None else t
        half = s.width/2.; low, high = max(.05, s.centre-half), min(1., s.centre+half)
        shift = s.flip*DRIFT*math.sin(2.*math.pi*t/DRIFT_PERIOD)
        shift = max(-low, min(1.-high, shift))
        return low+shift, high+shift

    def band(self):
        """The band the lever should be in now: idle through a countdown, else the burn band."""
        return (0., IDLE) if self.phase in ('countdown', 'retry') else self.burn_band()

    def required(self, t):
        """The required (low, high) at elapsed time t: idle, a steep rise from the window's opening, the burn band."""
        if t < self.open_at:return 0., IDLE
        low, high = self.burn_band(t=t-self.burn_start)
        if t >= self.burn_start:return low, high
        k = (t-self.open_at)/RISE
        return low*k, IDLE+(high-IDLE)*k

    def end_at(self):
        """Elapsed time this stage ends if the pilot stays in the band from here."""
        if self.phase == 'burn':return self.elapsed+max(0., self.stage.required-self.done)
        return self.burn_start+self.stage.required

    def trace(self, behind=2., ahead=3., step=.05):
        """The requirement around now for the HUD: [(t from now, low, high, kind)], kind 'idle', 'window' (the
        ignition window, until ignition) or 'burn'. This stage only: the next one uses its own control."""
        out = []; t = max(self.stage_start, self.elapsed-behind); end = min(self.end_at(), self.elapsed+ahead)
        while t <= end+1e-9:
            if t < self.open_at:kind = 'idle'
            elif self.phase != 'burn' and t <= self.open_at+WINDOW:kind = 'window'
            else:kind = 'burn'
            out.append((t-self.elapsed, *self.required(t), kind)); t += step
        return out

    def progress(self):
        """0..1 of the whole procedure, by hold time."""
        if self.result == 'relit':return 1.
        total = sum(s.required for s in self.stages)
        done = sum(s.required for s in self.stages[:self.index])+min(self.done, self.stage.required)
        return min(1., done/total) if total > 0. else 1.

    def value(self, inputs, stage=None):
        """The current stage's input, 0..1 in its own direction. inputs: throttle, vertical, lateral (signed units)."""
        s = stage or self.stage
        return min(1., max(0., s.sign*float(inputs.get(s.input, 0.) or 0.)))

    def step(self, dt, inputs, depth):
        """Advance by dt seconds with the pilot's inputs (see value()) and the ship depth u below the surface.
        Returns the events of this step: 'begin' (warning over, first countdown), 'stage' (next countdown), 'go'
        (a countdown is over, the ignition window opens), 'ignite', 'miss', 'relit', 'wrecked'."""
        events = []
        if self.result is not None or not dt > 0.:return events
        dt = min(dt, .25); lever = self.value(inputs); self.depth = depth
        self.flow += dt*(.5+2.5*lever)
        if self._warn(dt, events):return events
        self.elapsed += dt; self.cooldown = max(0., self.cooldown-dt)
        if self.phase in ('countdown', 'retry'):  # the trail shows where the pilot is; a countdown costs nothing
            if self.phase == 'retry':self._drain(dt, depth)
            self.countdown = max(0., self.countdown-dt)
            self.early = lever > IDLE; self.was_out = lever < self.burn_band(t=0.)[0]
            self._trail(lever, lever <= IDLE)
            if self.countdown == 0.:
                self.phase = 'window'; self.early = False; events.append('go')
        elif self.phase == 'window':
            self._drain(dt, depth)
            low, high = self.burn_band(); inside = low <= lever <= high
            self._trail(lever, inside)
            if lever < low:self.was_out = True
            if inside and self.was_out:
                self.phase = 'burn'; self.grace = IGNITE_GRACE; events.append('ignite')
            elif self.elapsed-self.open_at > WINDOW:  # closed without ignition: a hit and another window
                self.misses += 1; self.hull -= MISS_HIT; events.append('miss')
                self.phase = 'retry'; self._open(RETRY)
        else:
            self._drain(dt, depth); self.grace = max(0., self.grace-dt)
            low, high = self.burn_band(); inside = low <= lever <= high
            self._trail(lever, inside)
            if inside:
                self.done += dt; self.outside = 0.; self.missed = False
            elif self.grace == 0.:
                self.outside += dt
                if self.outside >= MISS_AFTER and not self.missed and self.cooldown == 0.:
                    self.missed = True; self.misses += 1; self.cooldown = MISS_COOLDOWN
                    self.hull -= MISS_HIT; self.stage.required += MISS_EXTEND
                    events.append('miss')
        if self._failed(events):return events
        if self.phase == 'burn' and self.done >= self.stage.required:
            if self.index+1 >= len(self.stages):
                self.done = self.stage.required; self.result = 'relit'; events.append('relit')
            else:
                self.index += 1; self.done = 0.; self.outside = 0.; self.missed = False; self.history = []
                self.phase = 'countdown'; self.stage_start = self.elapsed; self._open(COUNTDOWN)
                events.append('stage')
        return events

    def _trail(self, lever, inside):
        self.history.append((self.elapsed, lever, inside))
        while self.history and self.history[0][0] < self.elapsed-HISTORY:self.history.pop(0)

    def prompt(self):
        return LABELS[(self.stage.input, self.stage.sign)]

    def arrow(self):
        return ARROWS[(self.stage.input, self.stage.sign)]

    def status(self):
        return {'style': self.style, 'stage': self.stage.name, 'prompt': self.prompt(), 'warning': self.warning,
                'countdown': self.countdown, 'stage_index': self.index, 'progress': self.progress(), 'phase': self.phase,
                'hull': self.hull, 'misses': self.misses, 'result': self.result, 'band': self.band()}


class Gates(_Race):
    """Gates style: one run through GATES gates after the countdown. Mains (throttle forward) sets the speed the
    gates come at, strafe up lifts. A wall costs hull and stalls the ship, then it slips through."""
    style = 'gates'

    def __init__(self, seed, impact_ratio=1., difficulty='normal', hull=1., length=None):
        super().__init__(impact_ratio, hull)
        self.stage = Stage('GATES', 'throttle', 1., 'ALL', 0.)
        self.gap = GAP.get(difficulty, GAP['normal'])
        rng = random.Random(seed); self.gates = []; previous = -1
        for i in range(GATES):
            slot = previous
            while slot == previous:slot = rng.randrange(3)  # high, middle, low; never twice in a row
            previous = slot; self.gates.append({'x': FIRST_GATE+i*SPACING, 'slot': slot, 'hit': False, 'passed': False})
        self.dist = 0.; self.speed = 0.; self.y = H/2.; self.vy = 0.
        self.passed = 0; self.stall = 0.; self.scrape_cd = 0.
        self.mains = 0.; self.lift = 0.

    def gap_centre(self, slot):
        return (H-self.gap/2.-6., H/2., self.gap/2.+6.)[slot]

    def progress(self):
        return 1. if self.result == 'relit' else self.passed/len(self.gates)

    def step(self, dt, inputs, depth):
        """As Purge.step; events 'begin', 'go', 'miss' (a wall), 'scrape' (the sea floor), 'relit', 'wrecked'."""
        events = []
        if self.result is not None or not dt > 0.:return events
        dt = min(dt, .25); self.depth = depth
        self.mains = min(1., max(0., float(inputs.get('throttle', 0.) or 0.)))
        self.lift = min(1., max(0., float(inputs.get('vertical', 0.) or 0.)))
        if self._warn(dt, events):return events
        self.elapsed += dt
        if self.countdown > 0.:
            self.countdown = max(0., self.countdown-dt)
            if self.countdown == 0.:events.append('go')
            return events
        self._drain(dt, depth)
        target = SPEED*self.mains*(STALL_SPEED if self.stall > 0. else 1.)
        self.speed = (min(target, self.speed+ACCEL*dt) if target > self.speed
                      else max(target, self.speed-(200. if self.stall > 0. else COAST)*dt))
        self.stall = max(0., self.stall-dt); self.scrape_cd = max(0., self.scrape_cd-dt)
        self.dist += self.speed*dt
        self.vy += (self.lift*LIFT-GRAVITY-VDRAG*self.vy)*dt; self.y += self.vy*dt
        if self.y+SHIP_H/2. > H:self.y = H-SHIP_H/2.; self.vy = min(self.vy, 0.)
        if self.y-SHIP_H/2. < 0.:
            self.y = SHIP_H/2.; self.vy = max(self.vy, 0.)
            if self.scrape_cd == 0.:self.hull -= SCRAPE_HIT; self.scrape_cd = SCRAPE_COOLDOWN; events.append('scrape')
        front, back = self.dist+SHIP_W/2.-GIVE, self.dist-SHIP_W/2.+GIVE
        top, bottom = self.y+SHIP_H/2.-GIVE, self.y-SHIP_H/2.+GIVE
        for g in self.gates:
            if not g['hit'] and not g['passed'] and front > g['x'] and back < g['x']+GATE_T:
                c = self.gap_centre(g['slot'])
                if top > c+self.gap/2. or bottom < c-self.gap/2.:
                    g['hit'] = True; self.misses += 1; self.stall = STALL; self.hull -= MISS_HIT; events.append('miss')
            if not g['passed'] and self.dist-SHIP_W/2. > g['x']+GATE_T:g['passed'] = True; self.passed += 1
        self.history.append((self.elapsed, self.dist, self.y))
        while self.history and self.history[0][0] < self.elapsed-HISTORY:self.history.pop(0)
        if self._failed(events):return events
        if self.passed >= len(self.gates):self.result = 'relit'; events.append('relit')
        return events

    def view(self, ahead, behind):
        """The course around the ship for the HUD, distances from the ship: gates [(dx, gap low, gap high, state)]
        with state 'ahead', 'hit' or 'passed', and the ship's trail [(dx, y)]. Heights are 0..1 of the course."""
        gates = [(g['x']-self.dist, (self.gap_centre(g['slot'])-self.gap/2.)/H, (self.gap_centre(g['slot'])+self.gap/2.)/H,
                  'hit' if g['hit'] else 'passed' if g['passed'] else 'ahead')
                 for g in self.gates if -behind-GATE_T < g['x']-self.dist < ahead]
        return gates, [(d-self.dist, y/H) for _, d, y in self.history if d-self.dist > -behind]

    def prompt(self):
        return 'THROTTLE: SPEED  STRAFE UP: LIFT'

    def arrow(self):
        return None

    def status(self):
        return {'style': self.style, 'stage': self.stage.name, 'prompt': self.prompt(), 'warning': self.warning,
                'countdown': self.countdown, 'stage_index': 0, 'progress': self.progress(), 'phase': 'run',
                'hull': self.hull, 'misses': self.misses, 'result': self.result, 'band': None}


def make(style, seed, impact_ratio=1., difficulty='normal', hull=1., length='short'):
    """The purge for one ditch in the chosen style and length (Ship Damage page, *Purge style*, *Purge length*;
    gates has no length)."""
    return (Gates if style == 'gates' else Purge)(seed, impact_ratio, difficulty, hull, length)
