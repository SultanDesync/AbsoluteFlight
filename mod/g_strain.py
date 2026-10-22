"""G-load strain: greyout, tunnel vision and blackout from the ship's acceleration (user design, 2026-10-17).

In space the pilot feels the ship's acceleration, which is the vector the Sultan flight law commands
each frame (FlightConsumer.latest['acceleration_body'], u/s^2 along right, up, forward). Star Citizen
with G-safe off allows extreme G, but the pilot pays: short spikes are fine, sustained load greys
out, tunnels and blacks out, and recovery takes seconds. That makes pulsing hard manoeuvres the skill.

Model, all pure:
- g per axis = acceleration * unit_scale / 9.81 (unit_scale: metres per NMS unit, a tuning value).
- Load L = |(g_up+/tol_up, g_up-/tol_down, g_lat/tol_lat, g_fwd/tol_fwd)|: 1 means at tolerance.
  Accelerating up through the canopy (+Gz) drives blood to the feet (blackout, lowest positive
  tolerance); accelerating down (-Gz) is redout (lowest of all). Forward/back is the most tolerant.
- Strain s in 0..1 rises at (L-1)/onset while over tolerance and falls at 1/recovery after a short
  delay while under it. At s=1 the pilot is out until s falls to WAKE.
- Effects (user, 2026-10-18: no closing tunnel; mono first, dark edges, then a fade to black):
  greyout (native whole-screen desaturation) from s=.2, fully monochrome by s=.7; a dark, soft-edged
  periphery ('tunnel', drawn by the HUD) from s=.35, full by s=.9, which never closes the centre;
  'fade', a whole-view fade to black from s=.8 to s=1. While out the view stays black and fades back
  in over the last stretch before WAKE. Negative load is reported (redout) but the qualified native
  lanes are greyout and vignette only, so redout currently looks the same.
- Control: optional loss of pilot input during blackout only. The stick keeps full authority right up
  to it (user, 2026-10-17): greyout and tunnel are the warning, holding on is the skill.
"""
import math

G = 9.81
RECOVERY_DELAY = .5  # seconds under tolerance before strain starts to fall
WAKE = .6            # strain below which a blacked-out pilot comes back


def _smooth(edge0, edge1, x):
    t = min(1., max(0., (x-edge0)/(edge1-edge0)))
    return t*t*(3.-2.*t)


class GStrain:
    def __init__(self):
        self.strain = 0.; self.out = False; self.under = 0.; self.load = 0.; self.g = (0., 0., 0.); self.negative = False
        self.crash = False  # T93: knocked out by a crash; plays out even with G effects off

    def reset(self):
        self.__init__()

    def measure(self, acceleration_body, values):
        """Per-axis G and load for the HUD readout, without touching strain. Returns the load."""
        if acceleration_body is None or not all(math.isfinite(a) for a in acceleration_body):
            lateral = up = forward = 0.
        else:
            scale = float(values['g-unit-scale'])/G
            lateral, up, forward = (a*scale for a in acceleration_body)
        self.g = (lateral, up, forward); self.negative = up < 0.
        self.load = math.sqrt((max(up, 0.)/values['g-tolerance-up'])**2+(min(up, 0.)/values['g-tolerance-down'])**2
                              +(lateral/values['g-tolerance-lateral'])**2+(forward/values['g-tolerance-forward'])**2)
        return self.load

    def knock_out(self, hold):
        """Crash (T93 ditch, user 2026-10-19): the pilot passes out at once, so the dead stop is never seen,
        and stays out hold seconds longer than the normal recovery delay before recovering as usual."""
        self.strain = 1.; self.out = True; self.under = -max(0., hold); self.crash = True

    def step(self, acceleration_body, dt, values):
        """acceleration_body: (right, up, forward) u/s^2, or None when unknown (strain recovers)."""
        if not math.isfinite(dt) or dt <= 0.: return self.effects(values)
        dt = min(dt, .25)
        load = self.measure(acceleration_body, values)
        if load > 1.:
            self.under = 0.
            self.strain = min(1., self.strain+(load-1.)*dt/values['g-onset'])
        else:
            self.under += dt
            if self.under > RECOVERY_DELAY:
                self.strain = max(0., self.strain-dt/values['g-recovery'])
        if self.strain >= 1.: self.out = True
        elif self.out and self.strain <= WAKE: self.out = False
        if self.strain <= 0.: self.crash = False
        return self.effects(values)

    def effects(self, values):
        """{'greyout': 0..1, 'tunnel': 0..1, 'fade': 0..1, 'blackout': bool, 'control': 0..1}."""
        s = self.strain
        if self.out:
            return {'greyout': 1., 'tunnel': 1., 'fade': _smooth(WAKE, WAKE+.15, s), 'blackout': True,
                    'control': 0. if values['g-blackout-controls'] else 1.}
        return {'greyout': _smooth(.2, .7, s), 'tunnel': _smooth(.35, .9, s), 'fade': _smooth(.8, 1., s),
                'blackout': False, 'control': 1.}

    def status(self):
        return {'strain': round(self.strain, 3), 'load': round(self.load, 2), 'out': self.out,
                'g': [round(v, 1) for v in self.g], 'redout_side': self.negative}
