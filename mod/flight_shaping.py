"""Consumer-side axis shaping and HOTAS/HOSAS throttle styles, no game or device APIs.

shape() is a verbatim port of AbsoluteHOTAS AxisShapingPolicy::Shape; its source test
vectors are copied into tests/test_flight_shaping.py. Inputs are bus-calibrated: sticks and
levers arrive as unit positions 0..1. Throttle output is the NMS normal-flight u (0..1, E5)
plus an analog brake demand (0..1, native brake action 0x8B); None means "not armed", so the
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
    STYLE_KEYS=('throttle-style','hosas-stick-mode')

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
            return ThrottleCommand(*hosas_direct(signed,c['throttle-deadzone'],c['hosas-saturation'],c['hosas-brake-authority']))
        request=shape(signed,1.,1.,c['throttle-deadzone'])
        return ThrottleCommand(self.rate.step(request,dt,c['rate-throttle-ramp'],c['rate-throttle-decay']))


def shape_axis(config,channel,unit):
    """Signed shaped output for a flight channel from its calibrated unit, or None if missing."""
    if unit is None:return None
    sens_key='strafe-lateral-sensitivity' if channel=='strafe-vertical' else f'{channel}-sensitivity'
    return shape(bipolar(unit,config[f'{channel}-inverted']),config[sens_key],config[f'{channel}-saturation'],config[f'{channel}-deadzone'])
