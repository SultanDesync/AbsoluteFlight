"""Consumer-side axis shaping and HOTAS/HOSAS throttle styles, no game or device APIs.

shape() is a verbatim port of AbsoluteHOTAS AxisShapingPolicy::Shape; its source test
vectors are copied into tests/test_flight_shaping.py. Inputs are bus-calibrated: sticks and
levers arrive as unit positions 0..1. Throttle output is the NMS normal-flight u (0..1, E5)
plus a brake demand (0..1, the game's Brake action) or, in Reverse thrust, a signed u from
ReverseThrust; None means "not armed", so the
consumer leaves throttle to native input.
"""
from dataclasses import dataclass
import math

STYLES=('hotas','hosas')
STICK_MODES=('direct','rate')
NEUTRAL_TOLERANCE=.02
MAX_STEP_SECONDS=.25
# Lateral strafe command per unit of stick pushed right. Sultan and the native MAV both move the ship to
# port on +lateral (basis row 0 is the pilot's left; ditch purge sides, user flight 2026-10-20), and a
# standard device reads X increasing to the right (S-TECS X probed 2026-10-20), so with Invert off
# the command is negated. Before settings schema 4 the default strafed backwards and players set Invert on to fix it.
LATERAL_COMMAND_SIGN=-1.


def _finite(*values):
    if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in values):
        raise ValueError('Non-finite shaping input')


def _clamp(v,low,high):
    return min(high,max(low,v))


def shape(normalized,sensitivity,saturation,deadzone):
    """AxisShapingPolicy::Shape: deadzone, saturation ramp, sensitivity, sign kept, clamp."""
    _finite(normalized,sensitivity,saturation,deadzone)
    dead=_clamp(deadzone,0.,.95)
    saturated=_clamp(saturation,.05,1.)
    magnitude=abs(normalized)
    if magnitude<=dead:return 0.
    ramp=_clamp((magnitude-dead)/max(saturated-dead,1e-4),0.,1.)
    return _clamp(math.copysign(ramp*sensitivity,normalized),-1.,1.)


def bipolar(unit,inverted=False):
    """Calibrated stick unit 0..1 to signed -1..1."""
    _finite(unit)
    unit=_clamp(unit,0.,1.)
    if inverted:unit=1.-unit
    return unit*2.-1.


def lever_thrust(unit,idle,saturation,detent=None):
    """HOTAS lever: source unipolar idle plateau and saturation, plus an optional cruise detent.

    detent=(center,width) in lever units holds the thrust at center across center±width, so a
    physical detent gives a stable speed. It is an NMS addition, not a source behaviour.
    """
    _finite(unit,idle,saturation)
    unit=_clamp(unit,0.,1.)
    if detent is not None:
        center,width=detent
        _finite(center,width)
        if abs(unit-center)<=width:unit=center
    if unit<idle:return 0.
    logical=(unit-idle)/max(1.-idle,1e-4)
    return _clamp(logical/_clamp(saturation,.05,1.),0.,1.)


def hosas_direct(signed,deadzone,saturation,brake_authority):
    """Centred stick, centre = stop: forward half is thrust u, back half is analog brake."""
    _finite(brake_authority)
    v=shape(signed,1.,saturation,deadzone)
    return max(v,0.),max(-v,0.)*_clamp(brake_authority,0.,1.)


@dataclass
class ThrottleCommand:
    thrust:float
    brake:float=0.
    pull:float=0.  # HOSAS direct back half before Brake strength (0..1), the reverse demand in Reverse thrust


# T116 reverse thrust (native HOSAS Direct). The game's own reverse has no speed limit: the forward follower is
# sign(u)*|u|*ThrustForce*taper, and the taper (keyed to the smoothed target's magnitude, ship+0x6714 taken
# abs at 0x178D87D on 183877) never stops a ship moving backwards, so held reverse ran past 2,000 u/s
# (dev.15). We close the loop on the forward speed the native force stage used (MAV snapshot) instead.
REVERSE_GAIN_SPEED=40.  # u/s of speed error that asks for full thrust either way
REVERSE_INTEGRAL_SECONDS=1.  # the integral term's time constant, against the game's minimum-speed push
REVERSE_STOP_BAND=2.  # u/s: reversing slower than this with the stick centred, the loop lets go


class ReverseThrust:
    """Signed u that flies the ship backwards at pull*cap, and brings it to a stop when the stick centres.

    step() returns None when the loop is idle (no pull and not reversing) or the speed is unknown, so the
    caller falls back to idle thrust: never reverse without a measured speed. Integrates only while the
    output is unsaturated, so a hard pull from cruise doesn't overshoot the cap.
    """
    def __init__(self):self.integral=0.

    def reset(self):self.integral=0.

    def step(self,pull,forward_speed,cap,dt):
        _finite(pull,cap,dt)
        if forward_speed is None or not math.isfinite(forward_speed):
            self.reset();return None
        pull=_clamp(pull,0.,1.)
        if pull<=0. and forward_speed>-REVERSE_STOP_BAND:
            self.reset();return None
        p=(-pull*max(cap,0.)-forward_speed)/REVERSE_GAIN_SPEED
        integral=self.integral+p*max(0.,min(dt,MAX_STEP_SECONDS))/REVERSE_INTEGRAL_SECONDS
        if abs(p+integral)<1.:self.integral=_clamp(integral,-1.,1.)
        return _clamp(p+self.integral,-1.,1.)


class RateThrottle:
    """HOSAS rate mode (source DualStick accumulator): stick deflection moves a retained target.

    ramp is target units per second at full deflection; decay pulls the target toward zero
    only while the stick is inside its deadzone.
    """
    def __init__(self):self.target=0.

    def set(self,target):
        _finite(target)
        self.target=_clamp(target,0.,1.)

    def step(self,request,dt,ramp,decay):
        _finite(request,dt,ramp,decay)
        if dt<0:raise ValueError('Negative time step')
        dt=min(dt,MAX_STEP_SECONDS)
        if request:self.target=_clamp(self.target+_clamp(request,-1.,1.)*ramp*dt,0.,1.)
        elif decay>0:self.target=max(0.,self.target-decay*dt)
        return self.target


class ThrottleChannel:
    """Applies the configured throttle style to one calibrated input.

    It arms wherever the input is (user, 2026-10-22: no neutral wait); FlightControl ramps the demand
    in over ARM_RAMP when it arms with the lever up. A change of style or stick mode re-arms it, and
    rate mode resumes from the speed the game holds.
    """
    STYLE_KEYS=('throttle-style','hosas-stick-mode','hosas-reverse')

    def __init__(self,config):
        self.config=dict(config);self.armed=False;self.rate=RateThrottle()

    def reconfigure(self,config):
        if any(config[k]!=self.config[k] for k in self.STYLE_KEYS):self.disarm()
        self.config=dict(config)

    def disarm(self):self.armed=False

    def _position(self,unit):
        c=self.config
        detent=(c['throttle-detent-center'],c['throttle-detent-width']) if c['cruise-detent-enabled'] else None
        return lever_thrust(unit,c['throttle-idle-plateau'],c['throttle-saturation'],detent)

    def neutral(self,unit):
        c=self.config
        if c['throttle-inverted']:unit=1.-unit
        if c['throttle-style']=='hotas':return unit<=c['throttle-idle-plateau']+NEUTRAL_TOLERANCE
        return abs(bipolar(unit))<=c['throttle-deadzone']+NEUTRAL_TOLERANCE

    def brake_demand(self,unit):
        """The back half of a HOSAS direct stick (0..1), without stepping the channel; 0 in any other mode or unbound."""
        c=self.config
        if unit is None or c['throttle-style']=='hotas' or c['hosas-stick-mode']!='direct':return 0.
        return hosas_direct(bipolar(unit,c['throttle-inverted']),c['throttle-deadzone'],c['hosas-saturation'],c['hosas-brake-authority'])[1]

    def step(self,unit,dt,native_u=None):
        """unit: calibrated input or None when the device is missing/stale (disarms)."""
        if unit is None:
            self.disarm();return None
        _finite(unit)
        c=self.config
        if not self.armed:
            self.armed=True
            # Rate mode resumes from the speed NMS currently holds when it is known.
            self.rate.set(native_u if native_u is not None else 0.)
        if c['throttle-style']=='hotas':
            lever=1.-_clamp(unit,0.,1.) if c['throttle-inverted'] else unit
            return ThrottleCommand(self._position(lever))
        signed=bipolar(unit,c['throttle-inverted'])
        if c['hosas-stick-mode']=='direct':
            thrust,pull=hosas_direct(signed,c['throttle-deadzone'],c['hosas-saturation'],1.)
            return ThrottleCommand(thrust,pull*_clamp(c['hosas-brake-authority'],0.,1.),pull)
        request=shape(signed,1.,1.,c['throttle-deadzone'])
        return ThrottleCommand(self.rate.step(request,dt,c['rate-throttle-ramp'],c['rate-throttle-decay']))


def strafe_sum(shaped,button):
    """A shaped strafe axis plus the strafe buttons (T111), held to -1..1."""
    if not button or not math.isfinite(button):return shaped
    return max(-1.,min(1.,shaped+button))


# T112: with keyboard flight on, a stick resting this close to centre publishes exactly 0 on pitch, yaw and roll, which
# leaves the axis to the game, so the mouse and the game's Q/E roll steer. A bound stick never rests at exactly 0, and
# without a deadzone it overrode the mouse (0.2.4-dev.7, DADS 2026-10-30; a hand-set deadzone fixed it).
MOUSE_YIELD=.03
YIELD_CHANNELS=('pitch','yaw','roll')


def shape_axis(config,channel,unit):
    """Signed shaped output for a flight channel from its calibrated unit, or None if missing."""
    if unit is None:return None
    sens_key='strafe-lateral-sensitivity' if channel=='strafe-vertical' else f'{channel}-sensitivity'
    deadzone=config[f'{channel}-deadzone']
    if channel in YIELD_CHANNELS and config.get('keyboard-flight-enabled'):deadzone=max(deadzone,MOUSE_YIELD)
    return shape(bipolar(unit,config[f'{channel}-inverted']),config[sens_key],config[f'{channel}-saturation'],deadzone)
