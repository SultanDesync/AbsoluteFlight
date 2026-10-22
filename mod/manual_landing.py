"""T101 stage 2: touchdown and the landing HUD. User decision L2, docs/MANUAL-LANDING.md.

With the gear down the vertical stick is a sink rate eased near the ground (planetary_flight.Gear), so the ship
settles onto its gear. Once it is low, slow, level and not climbing for a moment, it gets one tap of the game's Land
key and the game finishes the landing (gear animation, Landed state, exit prompt). No push into the ground is needed
(user 2026-10-21: pushing at the old 25 % slammed the ship into the ground).

Pure logic: the runtime feeds the detector the flight consumer's latest state and the pilot's vertical input every
worker tick and holds LAND_KEY on the key lane (D20) while holding() says so. If the game refuses (no landing site),
nothing else happens and it tries again after RETRY while the conditions hold.
"""
from dataclasses import dataclass
import math

LAND_KEY = 'key.0x45'  # E, the game's default Land; NMS reads holds as repeated key-downs (mod/key_lane.py)
PRESS_TIME = .6        # s the Land key is held: long enough for the game's hold, short of a second request
RETRY = 3.             # s before another press while the conditions still hold
PANEL_HEIGHT = 60.     # u above ground where the landing HUD appears with the gear down
TAPE_TOP = 30.         # u at the top of the HUD ground tape
DRIFT_SCALE = 8.       # u/s of drift at the HUD ring


@dataclass(frozen=True)
class Touchdown:
    """Thresholds. The first flights (2026-10-21, Fighter) read 1.5-3 u resting on the gear with 0.5-2 u/s of
    collision jitter."""
    height: float = 5.     # u: the game's height above ground or sea; a landed ship reads its gear height, not 0
    speed: float = 4.      # u/s
    tilt: float = 20.      # degrees from planet up
    climb: float = 1.      # u/s: rising faster than this is not a touchdown
    lift: float = .1       # share of up stick that means the pilot wants to stay up
    settle: float = .5     # s all of the above must hold

    @classmethod
    def from_values(cls, values):
        return cls(float(values.get('landing-touchdown-height', cls.height)))


def tilt(up, planet_up):
    """Degrees between the ship's up axis and planet up."""
    c = sum(a*b for a, b in zip(up, planet_up))
    return math.degrees(math.acos(max(-1., min(1., c))))


def ground_frame(basis, planet_up, velocity):
    """Body-frame landing cues. level: planet up along the ship's right and forward axes (a spirit-level bubble,
    on the high side); drift: velocity over the ground along right and forward; vertical_speed: + up."""
    right, _, forward = basis
    vs = sum(v*u for v, u in zip(velocity, planet_up))
    ground = tuple(v-vs*u for v, u in zip(velocity, planet_up))
    dot = lambda a, b: sum(x*y for x, y in zip(a, b))
    return {'level': (dot(planet_up, right), dot(planet_up, forward)),
            'drift': (dot(ground, right), dot(ground, forward)), 'vertical_speed': vs}


class TouchdownDetector:
    def __init__(self):
        self.since = None      # time the conditions started holding
        self.pressed_at = None
        self.last = None       # status for the menu, HUD and telemetry

    def reset(self):
        self.since = self.pressed_at = None

    def ready(self, settings, planetary, vertical, speed, flying):
        """Whether this tick looks like a ship resting on, or settling onto, the ground. Sets self.last."""
        if not flying or not planetary or not planetary.get('gear'):self.last = 'gear up';return False
        clearance = planetary.get('clearance')
        if clearance is None or not math.isfinite(clearance) or clearance > settings.height:self.last = 'descend';return False
        if vertical > settings.lift or (planetary.get('vertical_speed') or 0.) > settings.climb:self.last = 'climbing';return False
        if speed is None or speed > settings.speed:self.last = 'slow down';return False
        if planetary.get('tilt') is not None and planetary['tilt'] > settings.tilt:self.last = 'level off';return False
        self.last = 'settling';return True

    def step(self, now, settings, planetary, vertical, speed, flying):
        """True on the tick the Land key should go down."""
        if not self.ready(settings, planetary, vertical, speed, flying):
            self.since = None;return False
        if self.since is None:self.since = now
        if now-self.since < settings.settle:return False
        if self.pressed_at is not None and now-self.pressed_at < RETRY:self.last = 'landing';return False
        self.pressed_at = now;self.last = 'landing';return True

    def holding(self, now):
        """Whether the Land key is down now (PRESS_TIME after a press)."""
        return self.pressed_at is not None and now-self.pressed_at < PRESS_TIME


def panel(planetary, settings, status, values):
    """Landing HUD plan with the gear down and close to the ground, else None. Positions are fractions of the
    ring radius: level at the tilt limit sits on the ring, drift at DRIFT_SCALE does too."""
    if not planetary or not planetary.get('gear') or not values.get('hud-landing', True):return None
    clearance = planetary.get('clearance')
    if clearance is None or not math.isfinite(clearance) or clearance > PANEL_HEIGHT:return None
    edge = math.sin(math.radians(settings.tilt))
    clip = lambda x, y, r=1.3: (x, y) if math.hypot(x, y) <= r else (x*r/math.hypot(x, y), y*r/math.hypot(x, y))
    lx, lf = planetary.get('level', (0., 0.))
    dx, df = planetary.get('drift', (0., 0.))
    speed = math.hypot(dx, df)
    return {'level': clip(lx/edge, lf/edge), 'level_ok': (planetary.get('tilt') or 0.) <= settings.tilt,
            'drift': clip(dx/DRIFT_SCALE, df/DRIFT_SCALE), 'drift_ok': speed <= settings.speed,
            'ground': max(0., clearance), 'band': settings.height, 'top': TAPE_TOP,
            'vertical_speed': planetary.get('vertical_speed') or 0., 'status': (status or '').upper()}
