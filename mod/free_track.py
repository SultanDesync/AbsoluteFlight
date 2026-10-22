"""Read-only OpenTrack FreeTrack 2.0 Enhanced ingress, independent of any game.

Reference: user-authored Absolute Head Tracking/src/OpenTrack.cpp. The mapping
contains 92-byte FreeTrackData within a 108-byte heap. Rotation is in radians.
No gameId/table write, UDP socket, virtual input, or camera access is performed.
"""
import ctypes as C
from dataclasses import dataclass
from enum import Enum
import math
import struct
import time

FRAME=struct.Struct("<iii20f")
COUNTER=struct.Struct("<i")
HEAP_SIZE=108
COPY_ATTEMPTS=3


class State(str,Enum):
    WAITING="waiting"
    CONNECTED="connected"
    STALE="stale"
    INVALID="invalid"


@dataclass(frozen=True)
class Sample:
    data_id:int
    radians:tuple
    translation_raw:tuple
    age_ms:float


def decode_frame(raw):
    if len(raw)!=FRAME.size:raise ValueError("FreeTrack frame must be exactly 92 bytes")
    values=FRAME.unpack(raw)
    pose=values[3:9]
    if not all(math.isfinite(x) for x in pose):raise ValueError("non-finite FreeTrack pose")
    return values[0],tuple(pose[:3]),tuple(pose[3:])


class WindowsTransport:
    """Single polling-thread owner; opens existing producer objects only.

    Counter brackets reject observed writer advances, including a counter copied
    from a different frame. The producer writes DataID last without this mutex:
    equal counters are not a true writer-side seqlock and cannot guarantee an
    arbitrary in-progress field write is atomic.
    """
    def __init__(self,mapping_name="FT_SharedMem",mutex_name="FT_Mutext"):
        self.mapping_name=mapping_name;self.mutex_name=mutex_name
        self.mapping=None;self.mutex=None;self.view=None
        self.api=C.WinDLL("kernel32",use_last_error=True)
        specs={
          "OpenFileMappingW":(C.c_void_p,[C.c_uint32,C.c_int,C.c_wchar_p]),
          "MapViewOfFile":(C.c_void_p,[C.c_void_p,C.c_uint32,C.c_uint32,C.c_uint32,C.c_size_t]),
          "OpenMutexW":(C.c_void_p,[C.c_uint32,C.c_int,C.c_wchar_p]),
          "WaitForSingleObject":(C.c_uint32,[C.c_void_p,C.c_uint32]),
          "ReleaseMutex":(C.c_int,[C.c_void_p]),
          "UnmapViewOfFile":(C.c_int,[C.c_void_p]),
          "CloseHandle":(C.c_int,[C.c_void_p])}
        for name,(result,args) in specs.items():
            fn=getattr(self.api,name);fn.restype=result;fn.argtypes=args
    def open(self):
        if self.view:return True
        self.mapping=self.api.OpenFileMappingW(4,False,self.mapping_name)
        if not self.mapping:return False
        self.view=self.api.MapViewOfFile(self.mapping,4,0,0,HEAP_SIZE)
        if self.view:self.mutex=self.api.OpenMutexW(0x100001,False,self.mutex_name)
        # Preserve the required mutex contract; the producer need not take it.
        if not self.view or not self.mutex:self.close();return False
        return True
    def copy(self):
        wait=self.api.WaitForSingleObject(self.mutex,2)
        if wait==0x80:  # abandoned: ownership acquired, but data may be incomplete
            self.api.ReleaseMutex(self.mutex);return None
        if wait==0x102:return None
        if wait!=0:raise OSError(C.get_last_error(),"FreeTrack mutex wait failed")
        try:
            for _ in range(COPY_ATTEMPTS):
                before=C.string_at(self.view,COUNTER.size)
                raw=C.string_at(self.view,FRAME.size)
                after=C.string_at(self.view,COUNTER.size)
                if before==raw[:COUNTER.size]==after:return raw
            return None
        finally:self.api.ReleaseMutex(self.mutex)
    def close(self):
        if self.view:self.api.UnmapViewOfFile(self.view)
        if self.mapping:self.api.CloseHandle(self.mapping)
        if self.mutex:self.api.CloseHandle(self.mutex)
        self.mapping=self.mutex=self.view=None


class Reader:
    def __init__(self,transport=None,stale_ms=500,clock=time.monotonic):
        self.transport=transport if transport is not None else WindowsTransport()
        self.stale_ms=max(50,min(5000,stale_ms));self.clock=clock
        self.state=State.WAITING;self.reason="producer not observed"
        self.connected=False;self.next_attempt=0
        self.last_id=None;self.last_frame=None;self.last_sample=None;self.age_ms=None
    def _retained(self,now):
        """Keep the last admitted pose without renewing its output timestamp."""
        if self.last_sample is None:return None
        self.age_ms=max(0,(now-self.last_frame)*1000)
        if self.age_ms>self.stale_ms:
            self.state=State.STALE;self.reason="frame counter stopped";return None
        sample=self.last_sample
        return Sample(sample.data_id,sample.radians,sample.translation_raw,self.age_ms)
    def poll(self):
        now=self.clock()
        if not self.connected:
            if now<self.next_attempt:return None
            self.next_attempt=now+1
            if not self.transport.open():return None
            self.connected=True;self.last_id=None;self.last_frame=None;self.last_sample=None;self.age_ms=None
        try:raw=self.transport.copy()
        except OSError:
            self.transport.close();self.connected=False;self.last_id=None;self.last_frame=None;self.last_sample=None;self.age_ms=None
            self.state=State.WAITING;self.reason="transport lost";self.next_attempt=now+1
            return None
        if raw is None:
            self.state=State.INVALID;self.reason="no counter-checked frame"
            retained=self._retained(now)
            if retained is not None:
                self.state=State.CONNECTED;self.reason="retaining previous counter-checked frame"
            return retained
        try:data_id,radians,xyz=decode_frame(raw)
        except ValueError as error:
            self.state=State.INVALID;self.reason=str(error);return None
        if self.last_id is None:
            self.last_id=data_id
            self.state=State.WAITING;self.reason="waiting for frame counter advance"
            return None
        if data_id!=self.last_id:
            self.last_id=data_id;self.last_frame=now
            self.last_sample=Sample(data_id,radians,xyz,0.)
        if self.last_frame is None:return None
        retained=self._retained(now)
        if retained is None:return None
        self.state=State.CONNECTED;self.reason="fresh finite frame"
        return retained
    def close(self):
        self.transport.close();self.connected=False
        self.last_id=self.last_frame=self.last_sample=None;self.age_ms=None
        self.state=State.WAITING;self.reason="closed"
