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
INTACT, ENGINES_OUT, WRECKED = 'intact', 'engines_out', 'wrecked'
RESTART_RAMP_MS = 1000.  # controls fade back in over this after a restart


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
