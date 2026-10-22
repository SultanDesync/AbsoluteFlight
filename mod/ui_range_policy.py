"""Mod-only U3 value and pointer ownership, independent of NMS/ctypes."""
from dataclasses import dataclass
import math,struct

def unit(value):
    if not math.isfinite(value):raise ValueError('Non-finite value')
    return struct.unpack('<f',struct.pack('<f',min(1.,max(0.,value))))[0]

def contains(rect,x,y):
    return rect is not None and rect[0]<=x<rect[2] and rect[1]<=y<rect[3]

def project(rect,scale,matrix):
    if len(rect)!=4 or len(scale)!=2 or len(matrix)!=6 or not all(math.isfinite(x) for x in (*rect,*scale,*matrix)):
        raise ValueError('Invalid layout')
    a,b,c,d,e,f=matrix
    if abs(b)>1e-6 or abs(c)>1e-6 or not (.1<a<10 and .1<d<10 and all(.1<s<10 for s in scale)):
        raise ValueError('Unqualified rotated/reflected layout')
    x,y,r,bt=rect
    if r<=x or bt<=y:raise ValueError('Empty bounds')
    return (a*x*scale[0]+e,d*y*scale[1]+f,a*r*scale[0]+e,d*bt*scale[1]+f)

def instrument(row):
    x,y,r,b=row;h=b-y
    if not 20<=h<=250:raise ValueError('Implausible native row height')
    w=min(r-x,10.5*h)
    # Use the value-side region of a native row; its label area is left of this.
    panel=(r-w,y,r,b);track=(r-w+.35*h,y+.66*h,r-.35*h,y+.66*h)
    return panel,track,.16*h

@dataclass
class RangeModel:
    value:float=.5
    armed:bool=False
    owned:bool=False
    editing:bool=False
    start:float=.5
    suppress:bool=False
    last_time:float=0.
    last_event:str='idle'
    quarantine:bool=False

    def cancel(self,reason):
        if self.owned:self.value=self.start
        was_owned=self.owned
        self.owned=False;self.editing=False;self.armed=False;self.suppress=False
        self.quarantine=self.quarantine or was_owned
        self.last_event='cancel:'+reason
        return was_owned

    def step(self,now,eligible,state,x,y,panel,track,radius,enabled=True):
        if state not in (0,1,2,3) or not all(math.isfinite(v) for v in (now,x,y)):
            self.cancel('invalid-input');raise ValueError('Invalid pointer sample')
        self.suppress=False;self.last_event='idle'
        if not eligible or not enabled:
            self.cancel('scope' if eligible is False else 'disabled')
            # A captured gesture stays swallowed until its release when the
            # feature is disabled on the same page; page scope remains external.
            if eligible and self.quarantine:
                self.suppress=True
                if state in (0,1):self.quarantine=False
            self.last_time=now
            return
        if self.owned and self.last_time and now-self.last_time>.25:
            self.cancel('frame-gap')
        self.last_time=now
        if self.quarantine:
            self.suppress=True
            if state in (0,1):self.quarantine=False
            return
        if not self.owned:
            if state in (0,1):self.armed=True
            elif state==2:
                if self.armed and contains(panel,x,y):
                    self.start=self.value;self.owned=True
                    knob_x=track[0]+self.value*(track[2]-track[0])
                    self.editing=abs(x-knob_x)<=radius*1.7 and abs(y-track[1])<=radius*1.7
                    self.last_event='press-handle' if self.editing else 'press-body'
                self.armed=False
        if self.owned:
            self.suppress=True
            if self.editing and state in (2,3):self.value=unit((x-track[0])/(track[2]-track[0]))
            if state in (0,1):
                self.owned=False;self.editing=False;self.armed=True;self.last_event='release'

    def native(self,value):
        value=unit(value)
        if not self.owned and not self.suppress:self.value=value
        return self.value
