"""Release head tracking: OpenTrack FreeTrack pose -> first-person cockpit camera (H3/H4b/D17 path).

Build 180836 / Steam 25732212 only (the release runtime refuses any other binary hash).
Composition point and gates are the qualified H3 ones (research/re-log/2026-10-15_codex_h3-*.md,
camera-h4b.md, docs/OPENTRACK-CAMERA-PROOF.md), unchanged:
- post-hook on the free-look helper RVA 0x1422A50, admitted only from caller return 0x656C43
  (third-person calls arrive from 0x6837FD and are rejected; H3 third-person negative);
- fresh pilot: the release flight clock (block.pilot_until, renewed on exact caller 0x175BFC3)
  and ship+0x60C8==1; frontend flag manager+0x875498==0 (photo, galaxy map, menus);
  *(manager+0x72C840) is cGcCameraBehaviourFirstPerson (vtable 0x4A4D230) and the callback's
  this is exactly that behaviour+0x20;
- only the 48-byte returned rotation block is composed (W lanes byte-exact), read back,
  and rolled back on any fault. Rotation only; translation stays native.

Leases (T50: a stalled publisher must hand back, never freeze an offset): the pump thread
publishes an immutable command with a 400 ms deadline only while the FreeTrack counter is
advancing. The camera fades to native from 150 ms source/pilot age over 200 ms (D9) and is
refused outright past the deadline, so a stalled pump, a stopped OpenTrack or a lost pilot
all return the native camera. After any hand-back the pose ramps in over RAMP_MS.
Centre: first fresh frame after enable, an explicit recentre, or a new producer (counter
restart / reconnect). Never on a stall: the harness re-centred after any 100 ms output gap,
which would have silently moved straight-ahead to wherever the pilot was looking.
"""
from collections import Counter,namedtuple
import ctypes as C
import math
import struct
import threading
import time

from head_math import PHYSICAL_LIMITS,compose_basis
from head_pose_source import fade_weight,semantic_angles

CALLER=0x661A29
APP_GLOBAL=0x6F60828
FRONTEND=0x89BD98  # 0x875498 on 180836
BEHAVIOUR=0x72CF30  # 0x72C840 on 180836; this is still behaviour+0x20 at caller 0x661A24
FIRST_PERSON_VTABLE=0x4B0F6C8
CONFIG_OFFSET=0x20
PILOT_ACTIVE=0x60C8
COMMAND_MS=400   # D9 hard clear
PILOT_MS=400     # release flight clock lease (flight_clock: pilot_until = tick + 400)
WARMUP_MS=250    # advancing counter required before (re)use
GAP_MS=100       # longer output gap re-warms (keeps the centre)
RAMP_MS=200
POLL_SECONDS=.01
MAX_ERRORS=10
AXES=('yaw','pitch','roll')

Command=namedtuple('Command','angles last_change ready_since deadline stamp',defaults=(None,))  # stamp: perf_counter of the sample
INTERVAL_RANGE=(.004,.05)  # seconds; bounds for the measured source sample interval
FILTER_RESET_SECONDS=.1  # a gap longer than this between camera frames restarts the filter at the target
BETA=.05                 # extra cutoff (Hz) per degree/second of head speed
DERIVATIVE_CUTOFF=1.     # Hz


def smoothing_cutoff(strength):
    """Menu strength 0..1 -> One Euro minimum cutoff in Hz (None = off). 0.5 gives about 2.9 Hz."""
    if strength<=0:return None
    return .5+9.5*(1.-min(1.,strength))**2


class OneEuro:
    """One Euro filter (Casiez et al. 2012) for one angle: low cutoff while still (no jitter), higher
    while moving (little lag). Runs per rendered frame with real dt, so it also interpolates the
    pump's ~60-100 Hz pose steps into smooth per-frame motion."""
    def __init__(self):self.x=None;self.dx=0.

    @staticmethod
    def _alpha(cutoff,dt):
        tau=1./(2*math.pi*cutoff);return 1./(1.+tau/dt)

    def reset(self):self.x=None;self.dx=0.

    def __call__(self,value,dt,min_cutoff):
        if self.x is None or dt<=0:
            if self.x is None:self.x=value
            return self.x
        dx=(value-self.x)/dt
        self.dx+=self._alpha(DERIVATIVE_CUTOFF,dt)*(dx-self.dx)
        cutoff=min_cutoff+BETA*abs(self.dx)
        self.x+=self._alpha(cutoff,dt)*(value-self.x)
        return self.x


def shape(relative,values):
    """Centred semantic degrees -> camera command: deadzone, scale, invert, physical clamp."""
    dz=float(values['head-deadzone']);out=[]
    for axis,v,limit in zip(AXES,relative,PHYSICAL_LIMITS):
        v=math.copysign(max(0.,abs(v)-dz),v)*float(values[f'head-{axis}-scale'])
        if values[f'head-{axis}-inverted']:v=-v
        out.append(max(-limit,min(limit,v))+0.)
    return tuple(out)


class HeadSource:
    """FreeTrack samples -> centred semantic degrees (relative is None until warm and centred)."""
    def __init__(self):
        self.center=None;self.relative=None
        self.last_id=None;self.last_change=None;self.warm_since=None

    def recentre(self):
        self.center=None;self.relative=None

    def update(self,sample,now,reconnected=False):
        if sample is None:return
        angles=semantic_angles(sample.radians)
        restarted=self.last_id is not None and ((sample.data_id-self.last_id)&0xffffffff)>=0x80000000
        if self.last_id is None or reconnected or restarted:
            self.warm_since=now;self.recentre();self.last_change=now;self.last_id=sample.data_id
        elif sample.data_id!=self.last_id:
            if now-self.last_change>GAP_MS:self.warm_since=now;self.relative=None
            self.last_change=now;self.last_id=sample.data_id
        if now-self.warm_since<WARMUP_MS or now-self.last_change>GAP_MS:return
        if self.center is None:self.center=angles
        self.relative=tuple((a-b+180)%360-180 for a,b in zip(angles,self.center))


class ProcessMemory:
    """Exact in-process reads/writes through Read/WriteProcessMemory: a bad pointer fails, never faults."""
    def __init__(self):
        k=self.k=C.WinDLL('kernel32',use_last_error=True)
        k.GetCurrentProcess.restype=C.c_void_p
        for name in ('ReadProcessMemory','WriteProcessMemory'):
            fn=getattr(k,name);fn.restype=C.c_int
            fn.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)]
        self.handle=k.GetCurrentProcess()

    def read(self,address,size):
        buf=C.create_string_buffer(size);count=C.c_size_t()
        if not self.k.ReadProcessMemory(self.handle,address,buf,size,C.byref(count)) or count.value!=size:raise OSError('exact read failed')
        return buf.raw

    def write(self,address,data):
        buf=C.create_string_buffer(data,len(data));count=C.c_size_t()
        if not self.k.WriteProcessMemory(self.handle,address,buf,len(data),C.byref(count)) or count.value!=len(data):raise OSError('exact write failed')

    def u8(self,address):return self.read(address,1)[0]
    def u32(self,address):return struct.unpack('<I',self.read(address,4))[0]
    def u64(self,address):return struct.unpack('<Q',self.read(address,8))[0]


def _tick():
    k=C.WinDLL('kernel32');k.GetTickCount64.restype=C.c_uint64
    return k.GetTickCount64


class HeadTracker:
    """Pump thread owns the FreeTrack reader; the camera hook only reads the published command."""
    def __init__(self,base,values,reader_factory=None,memory=None,clock=None,log=None):
        self.base=int(base);self.values=dict(values);self.enabled=bool(values['head-tracking-enabled'])
        if reader_factory is None:
            from free_track import Reader
            reader_factory=lambda:Reader(clock=time.perf_counter)
        self.reader_factory=reader_factory
        self.mem=memory if memory is not None else ProcessMemory()
        self.clock=clock or _tick();self.log=log or (lambda message:None)
        self.lock=threading.Lock();self.source=HeadSource()
        self.reader=None;self.reader_connected=False
        self.command=None;self.stopped=False;self.faulted=None
        self.counts=Counter();self.errors=0;self.last_gate=None;self.last_applied=None;self.last_weight=0.
        self.stopping=threading.Event();self.thread=None
        self.filters=[OneEuro() for _ in AXES];self.filter_time=None;self.perf=time.perf_counter  # camera thread only
        self.prev=None;self.latest=None;self.interval=.016  # interpolation: two newest (stamp, angles), camera thread only
        self.stamped_id=None  # pump thread: data id of the last stamped sample

    # ----- control (any thread; never the runtime lock) -----
    def settings_changed(self,values,changed):
        if not any(c.startswith('head-') for c in changed):return
        with self.lock:
            self.values=dict(values)
            enabled=bool(values['head-tracking-enabled'])
            if enabled!=self.enabled:
                self.enabled=enabled;self.command=None;self.source.recentre();self.filter_time=None
                self.log('head tracking '+('on' if enabled else 'off'))

    def recentre(self):
        """Take the next fresh pose as straight ahead; native camera until then."""
        with self.lock:
            self.source.recentre();self.command=None;self.counts['recentres']+=1;self.filter_time=None

    # ----- pump -----
    def start(self):
        self.thread=threading.Thread(target=self._run,name='AbsoluteHOTAS-head',daemon=True);self.thread.start()

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:self.poll(self.clock())
                except BaseException as error:
                    self.command=None;self.errors+=1;self.log('head source error: '+repr(error))
                    self._close_reader();self.stopping.wait(1.)
                self.stopping.wait(POLL_SECONDS)
        finally:self._close_reader()

    def _close_reader(self):
        reader,self.reader=self.reader,None;self.reader_connected=False
        if reader is not None:
            try:reader.close()
            except BaseException:pass

    def poll(self,now):
        if not self.enabled or self.stopped or self.faulted:
            self.command=None;self._close_reader();return
        if self.reader is None:self.reader=self.reader_factory()
        sample=self.reader.poll();connected=bool(getattr(self.reader,'connected',sample is not None))
        with self.lock:
            reconnected=connected and not self.reader_connected
            # Opening the mapping can return no sample until its counter advances.
            # Keep the reconnect pending so the first admitted sample recentres.
            if not connected:self.reader_connected=False
            elif sample is not None:self.reader_connected=True
            if not self.enabled:return
            self.source.update(sample,now,reconnected)
            relative,changed=self.source.relative,self.source.last_change
            if relative is None:
                self.command=None;return  # restart/re-warm cannot reuse an older command
            if sample is None or now-changed>=COMMAND_MS:return  # deadline lapses on its own
            previous=self.command
            ready=previous.ready_since if previous is not None and now<previous.deadline else now
            if self.source.last_id!=self.stamped_id or previous is None or previous.stamp is None:
                self.stamped_id=self.source.last_id;stamp=self.perf()
            else:stamp=previous.stamp
            self.command=Command(shape(relative,self.values),changed,ready,now+COMMAND_MS,stamp)

    # ----- camera hook (game thread; no locks) -----
    def smoothed(self,angles,stamp=None):
        """Per-frame pose (camera thread only): interpolate between the two newest source samples, shown
        one sample interval behind, so ~60-100 Hz pose steps become continuous motion at any frame
        rate; then One Euro smoothing for jitter. A frame gap or recentre restarts both at the target."""
        now=self.perf();last,self.filter_time=self.filter_time,now
        if last is None or now-last>FILTER_RESET_SECONDS:
            for f in self.filters:f.reset()
            self.prev=None;self.latest=None
        if stamp is None:stamp=now
        if self.latest is None or stamp!=self.latest[0]:
            if self.latest is not None:
                gap=stamp-self.latest[0]
                if gap>0:self.interval+=.2*(min(max(gap,INTERVAL_RANGE[0]),INTERVAL_RANGE[1])-self.interval)
            self.prev,self.latest=self.latest,(stamp,tuple(angles))
        else:self.latest=(stamp,tuple(angles))  # same sample, shaping changed (scale/invert edits)
        if self.prev is None or self.latest[0]<=self.prev[0]:pose=self.latest[1]
        else:
            (ta,a),(tb,b)=self.prev,self.latest
            f=min(1.,max(0.,(now-self.interval-ta)/(tb-ta)))
            pose=tuple(x+f*(y-x) for x,y in zip(a,b))
        cutoff=smoothing_cutoff(float(self.values.get('head-smoothing',0.)))
        if cutoff is None:return pose
        dt=0. if last is None else now-last
        return tuple(f(a,dt,cutoff) for f,a in zip(self.filters,pose))

    def gate(self,this,now,ship,pilot_until):
        if not ship or now>=pilot_until:return 'pilot_stale'
        if self.mem.u8(ship+PILOT_ACTIVE)!=1:return 'pilot_inactive'
        manager=self.mem.u64(self.base+APP_GLOBAL)
        if not manager or self.mem.u32(manager+FRONTEND)!=0:return 'frontend'
        behaviour=self.mem.u64(manager+BEHAVIOUR)
        if not behaviour or self.mem.u64(behaviour)!=self.base+FIRST_PERSON_VTABLE:return 'behaviour'
        if this!=behaviour+CONFIG_OFFSET:return 'config'
        return 'ready'

    def weight(self,command,now,pilot_until):
        if command is None:return 0.,'no_pose'
        if now>=command.deadline or command.deadline-now>COMMAND_MS:return 0.,'expired'
        age=max(now-command.last_change,now-(pilot_until-PILOT_MS))
        w=fade_weight(age)*max(0.,min(1.,(now-command.ready_since)/RAMP_MS))
        return (w,'ready') if w>0 else (0.,'stale' if age>150 else 'ramp')

    def camera(self,this,output,base_ptr,result,caller,ship,pilot_until):
        """Post-hook body for 0x1422A50. Returns the outcome; native bytes stay unless 'applied'."""
        if not self.enabled or self.stopped or self.faulted:return 'off'
        if caller!=CALLER:return 'caller'
        original=None;attempted=False
        try:
            now=self.clock()
            outcome=self.gate(this,now,ship,pilot_until)
            if outcome=='ready':
                command=self.command;w,outcome=self.weight(command,now,pilot_until)
            if outcome=='ready' and (result!=output or output==base_ptr):outcome='output_alias'
            if outcome!='ready':
                self.counts[outcome]+=1;self.last_gate=outcome;return outcome
            original=self.mem.read(output,48)
            composed=compose_basis(original,self.smoothed(command.angles,command.stamp),w,PHYSICAL_LIMITS)
            check=self.clock()  # re-check every gate immediately before the write
            if (self.command is not command or not self.enabled or self.stopped
                    or check>=min(command.deadline,pilot_until) or self.gate(this,check,ship,pilot_until)!='ready'):
                self.counts['changed']+=1;self.last_gate='changed';return 'changed'
            if composed!=original:
                attempted=True;self.mem.write(output,composed)
                if self.mem.read(output,48)!=composed:raise OSError('camera readback mismatch')
            self.counts['applied']+=1;self.last_gate='applied';self.last_applied=check;self.last_weight=w
            return 'applied'
        except BaseException as error:
            self.errors+=1;self.command=None;self.counts['error']+=1;self.last_gate='error'
            if attempted and original is not None:
                try:self.mem.write(output,original)
                except BaseException:self.counts['rollback_error']+=1
            if self.errors>=MAX_ERRORS and not self.faulted:
                self.faulted='disabled after repeated camera errors: '+repr(error)
                self.log('head tracking '+self.faulted)
            return 'error'

    # ----- status -----
    def status(self):
        now=self.clock();reader=self.reader;command=self.command;source=self.source
        if self.faulted:camera='faulted'
        elif not self.enabled:camera='off'
        elif self.last_applied is not None and now-self.last_applied<500:camera='applied'
        else:camera=self.last_gate or 'waiting'
        return {'enabled':self.enabled,'faulted':self.faulted,
                'source':reader.state.value if reader is not None and hasattr(reader.state,'value') else ('off' if not self.enabled else 'waiting'),
                'source_reason':getattr(reader,'reason',None),'counter':getattr(reader,'last_id',None),
                'output_age_ms':getattr(reader,'age_ms',None),'centred':source.center is not None,
                'pose':None if source.relative is None else [round(v,2) for v in source.relative],
                'command':None if command is None else [round(v,2) for v in command.angles],
                'command_age_ms':None if command is None else now-command.last_change,
                'camera':camera,'weight':round(self.last_weight,3),'applied':self.counts['applied'],
                'recentres':self.counts['recentres'],'errors':self.errors,
                'rejections':{k:v for k,v in self.counts.items() if k not in ('applied','recentres')}}

    def stop(self):
        self.stopped=True;self.command=None;self.stopping.set()
        if self.thread is not None and self.thread is not threading.current_thread():self.thread.join(1.)
