"""Ship condition: intact, engines out (T93 ditch at sea), wrecked. Pure state; no game memory.

One object per flight consumer. The flight thread knocks the ship out (with the impact speed); the worker
thread then runs the thruster purge (mod/ditch_purge.py) and relights or wrecks the ship, and reads
control_share() to fade the pilot's inputs back after a relight. An optional restart_after relights by
time alone (simulation time, advanced by step()).

'wrecked' (user, 2026-10-19): the hull failed before the purge finished. The engines stay dead; the ship
is grounded until repaired. Repair is not built yet: ejecting clears it for now. Ditching and terrain
collisions (T94, cause 'collision') share this state.

The hull meter (0..1) is ours and carries over between ditches on the same ship until repair().
"""
import random

INTACT, ENGINES_OUT, WRECKED = 'intact', 'engines_out', 'wrecked'
RESTART_RAMP_MS = 1000.  # controls fade back in over this after a restart
# Wreck fall (user 2026-10-24): a disabling crash used to eject and respawn the wreck landed within 50 ms of the touch,
# wherever it was, so a clipped tree or outcrop left it floating. Now the blacked-out wreck falls first, under the
# engines-out physics, and the eject waits until it comes to rest. If the hit stopped it dead, it keeps some of its
# horizontal speed (applied by the flight consumer).
FALL_MIN_MS = 3000.      # the wreck tumbles at least this long, at rest or not (user: "a healthy enough period")
FALL_MAX_MS = 14000.     # the eject comes after this at the latest
REST_SPEED = 3.          # u/s below which the wreck counts as at rest...
REST_MS = 500.           # ...for this long
MOMENTUM_SHARE = .85     # share of the pre-hit horizontal velocity a stopped wreck carries on with
MOMENTUM_MAX = 120.      # u/s
# Tumble (user 2026-10-25: "some sort of tumble physics"): the runtime feeds the falling wreck's rotation channels
# a spin picked at the hit, (yaw, pitch, roll) stick shares with random signs, scaled down as the wreck slows, so it
# stops spinning as it settles. The game rotates the ship as it would for the stick.
TUMBLE = (.5, 1., 1.)    # user 2026-10-25: faster tumble for more intensity (was .25, .7, 1.)
TUMBLE_FULL_SPEED = 50.  # u/s at and above which the tumble is full; it fades linearly below


class ShipCondition:
    def __init__(self):
        self.ship = None
        self.knockouts = 0  # counts every knock-out, so the worker can black the pilot out once per crash
        self.hull = 1.
        self.reset()

    def reset(self):
        self.condition = INTACT
        self.cause = None
        self.impact = None         # u/s at the knock-out
        self.elapsed = 0.          # simulation seconds since the knock-out
        self.restart_after = None  # seconds; None never restarts by itself
        self.restarted_ms = None   # tick of the last restart, for the control ramp
        self.blast_until_ms = None # boost blast out of the water after a purge relight
        self.settle_until_ms = None # a ditch's surfacing hold (planetary_flight.surface_hold) runs until this tick
        self.fall_since_ms = None  # a disabling crash's wreck fall began at this tick (None: not falling)
        self.fall_still_ms = None  # the falling wreck has been at rest since this tick
        self.fall_kick = False     # the flight consumer still has to restore the wreck's momentum
        self.fall_spin = (0., 0., 0.)  # the falling wreck's tumble at full speed (yaw, pitch, roll)
        self.purge = None          # ditch_purge.Purge while the thrusters are being purged

    def select_ship(self, ship):
        if ship != self.ship:
            self.ship = ship
            self.hull = 1.
            self.reset()

    @property
    def engines_out(self):
        return self.condition != INTACT

    @property
    def wrecked(self):
        return self.condition == WRECKED

    def knock_out(self, cause, restart_after=None, impact=None):
        """Engines out from now on; restart_after seconds of flight later they relight (None: a purge or
        repair has to)."""
        if self.condition != INTACT:return False
        self.condition, self.cause, self.elapsed, self.impact = ENGINES_OUT, cause, 0., impact
        self.restart_after = None if restart_after is None else max(0., float(restart_after))
        self.restarted_ms = None
        self.knockouts += 1
        return True

    def relight(self, now_ms):
        """Engines back (purge done or timer): keep the hull, ramp the controls in."""
        if self.condition != ENGINES_OUT:return False
        self.reset()
        self.restarted_ms = now_ms
        return True

    @property
    def falling(self):
        return self.fall_since_ms is not None

    def start_fall(self, now_ms):
        self.fall_since_ms = now_ms; self.fall_still_ms = None; self.fall_kick = True
        rng = random.Random(now_ms)
        self.fall_spin = tuple(m*(1. if rng.random() < .5 else -1.)*rng.uniform(.75, 1.) for m in TUMBLE)

    def hold_fall(self, ms):
        """The game stood still for ms (paused): the fall's timers wait as long."""
        if self.fall_since_ms is not None:self.fall_since_ms += ms
        if self.fall_still_ms is not None:self.fall_still_ms += ms

    def tumble(self, speed):
        """(yaw, pitch, roll) stick shares for the falling wreck at this speed (u/s); centred when not falling."""
        if self.fall_since_ms is None or speed is None:return (0., 0., 0.)
        k = min(1., max(0., speed/TUMBLE_FULL_SPEED))
        return tuple(s*k for s in self.fall_spin)

    def fall_done(self, now_ms, speed):
        """While falling: 'rest' once the speed has stayed under REST_SPEED for REST_MS (and the fall has run
        FALL_MIN_MS), 'timeout' after FALL_MAX_MS, else None. Ends the fall when it returns a reason."""
        if self.fall_since_ms is None:return None
        if speed is not None and speed < REST_SPEED:
            if self.fall_still_ms is None:self.fall_still_ms = now_ms
        else:self.fall_still_ms = None
        reason = ('rest' if self.fall_still_ms is not None and now_ms-self.fall_still_ms >= REST_MS
                  and now_ms-self.fall_since_ms >= FALL_MIN_MS
                  else 'timeout' if now_ms-self.fall_since_ms >= FALL_MAX_MS else None)
        if reason:self.fall_since_ms = None; self.fall_still_ms = None; self.fall_kick = False
        return reason

    def settling(self, now_ms):
        until = self.settle_until_ms
        return until is not None and self.engines_out and now_ms < until

    def blasting(self, now_ms):
        until = self.blast_until_ms
        return until is not None and not self.engines_out and now_ms < until

    def wreck(self, cause=None):
        if self.condition == WRECKED:return False
        self.condition = WRECKED; self.cause = cause or self.cause; self.purge = None; self.hull = 0.
        return True

    def repair(self):
        """Ejected or repaired: intact with a full hull."""
        self.reset(); self.hull = 1.

    def step(self, dt, now_ms):
        if self.condition != ENGINES_OUT or not dt > 0.:return
        self.elapsed += dt
        if self.restart_after is not None and self.elapsed >= self.restart_after:self.relight(now_ms)

    def remaining(self):
        if self.condition != ENGINES_OUT or self.restart_after is None:return None
        return max(0., self.restart_after-self.elapsed)

    def control_share(self, now_ms):
        """0 with the engines out, rising to 1 over RESTART_RAMP_MS after a restart."""
        if self.engines_out:return 0.
        restarted = self.restarted_ms  # written by another thread; read once
        if restarted is None:return 1.
        share = (now_ms-restarted)/RESTART_RAMP_MS
        return 1. if share >= 1. or share < 0. else share

    def status(self):
        purge = self.purge
        return {'condition': self.condition, 'cause': self.cause, 'restart_in': self.remaining(),
                'hull': purge.hull if purge is not None else self.hull,
                'purge': None if purge is None else purge.status()}
