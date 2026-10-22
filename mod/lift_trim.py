"""T88 lift trim (user design 2026-10-19): a pilot-set vertical thrust bias instead of automatic gravity
compensation.

The trim is a share of the ship's vertical authority, -1..1, added to the vertical strafe command on the
flight thread. Trim up/down move it at RATE while held, so a hat tap nudges it and a hold sweeps it. Trim
to hover takes the share the flight consumer computes every frame (gravity along the ship's up axis
against its current up or down authority); Trim reset clears it. It clears itself on landing and on a
ship change, so every takeoff starts untrimmed. Worker thread only; no game memory.
"""
import math

RATE = .25    # trim share per second while Trim up/down is held
LIMIT = 1.


class LiftTrim:
    def __init__(self):
        self.value = 0.
        self.ship = None

    def step(self, dt, up, down, events=(), hover=None, ship=None, landing=False):
        """Advance one worker tick and return the trim share.

        events: 'mod.trim-hover' / 'mod.trim-reset' presses this tick. hover: the consumer's current
        hover share, or None when there is no planet (then Trim to hover leaves the trim alone)."""
        if ship != self.ship or landing:
            self.ship = ship
            self.value = 0.
            if landing:return 0.
        for event in events:
            if event == 'mod.trim-reset':
                self.value = 0.
            elif event == 'mod.trim-hover' and isinstance(hover, (int, float)) and math.isfinite(hover):
                self.value = float(hover)
        if math.isfinite(dt) and dt > 0.:
            self.value += RATE*dt*((1. if up else 0.)-(1. if down else 0.))
        self.value = min(LIMIT, max(-LIMIT, self.value))
        return self.value
