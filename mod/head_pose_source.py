"""Small FreeTrack-to-semantic pose relay for the first live camera proof."""
import math
from head_math import LIMITS,validate_angles


def semantic_angles(radians):
    """OpenTrack 2026.1 mapped FreeTrack output; qualified by UDP loopback."""
    if len(radians)!=3 or not all(math.isfinite(v) for v in radians):
        raise ValueError('Finite FreeTrack rotation required')
    y,p,r=radians
    return (-math.degrees(y),-math.degrees(p),math.degrees(r))


def fade_weight(age_ms):
    if age_ms<=150:return 1.
    return max(0.,1-(age_ms-150)/200)


class PoseSource:
    """Output activity only: unchanged upstream pose remains OpenTrack's policy.

    snapshot is an immutable (centered_angles, last_output_tick) pair. Startup,
    resets and long output gaps require 250ms of advancing counters before use.
    """
    def __init__(self,limits=LIMITS):
        validate_angles((0,0,0),limits);self.limits=tuple(limits)
        self.enabled=False;self.center=None;self.snapshot=None
        self.last_id=None;self.last_change=None;self.warm_since=None

    def toggle(self):
        self.enabled=not self.enabled;self.recenter();return self.enabled

    def recenter(self):
        self.center=None;self.snapshot=None

    def update(self,sample,now):
        if sample is None:return
        angles=semantic_angles(sample.radians)
        changed=self.last_id is None or sample.data_id!=self.last_id
        reset=self.last_id is not None and ((sample.data_id-self.last_id)&0xffffffff)>=0x80000000
        if changed:
            if self.last_change is None or now-self.last_change>100 or reset:
                self.warm_since=now;self.recenter()
            self.last_change=now;self.last_id=sample.data_id
        if self.last_change is None or now-self.last_change>100:
            self.warm_since=None
        if not self.enabled or self.warm_since is None or now-self.warm_since<250:return
        if self.center is None:self.center=angles
        relative=tuple((a-b+180)%360-180 for a,b in zip(angles,self.center))
        limited=tuple(max(-limit,min(limit,v)) for v,limit in zip(relative,self.limits))
        self.snapshot=(limited,self.last_change)
