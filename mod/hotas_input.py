"""Bus-backed device reading, per-binding calibrated values and axis capture.

The input bus (D11) owns polling and device identity; this consumer resolves canonical
binding tokens to calibrated unit values. A channel whose device is missing, disconnected,
stale or out of range reads None, which disarms only that channel; nothing is ever rebound
automatically. DeviceTable works on plain dicts, so it is tested without the DLL.
"""
import ctypes as C
import time
from hotas_settings import parse_token
import hotas_buttons

STALE_SECONDS=.1
CAPTURE_THRESHOLD=8000  # raw units of 0..65535, as in the T4 qualification
CAPTURE_FRAMES=3
CAPTURE_TIMEOUT=10.


class DeviceTable:
    """Latest device frames keyed by instance GUID, with per-device freshness."""
    def __init__(self,now=time.monotonic):
        self.now=now;self.devices={};self.changed={}

    def update(self,frames):
        """frames: {guid: {'name','connected','sequence','generation','raw','axis_minimum','axis_maximum',
        'digital_down','press_count','release_count'}}; the digital keys are optional."""
        t=self.now()
        for guid,frame in frames.items():
            previous=self.devices.get(guid)
            if previous is None or frame['sequence']!=previous['sequence'] or frame['generation']!=previous['generation']:
                self.changed[guid]=t
            self.devices[guid]=frame
        for guid in set(self.devices)-set(frames):
            del self.devices[guid];self.changed.pop(guid,None)

    def names(self):return {guid:d['name'] for guid,d in self.devices.items()}

    def fresh(self,guid):
        d=self.devices.get(guid)
        return d is not None and d['connected'] and self.now()-self.changed.get(guid,-1e9)<=STALE_SECONDS

    def raw(self,token):
        guid,index=parse_token(token)
        if not self.fresh(guid):return None
        return self.devices[guid]['raw'][index]

    def unit(self,token):
        """Calibrated 0..1 for a binding token, or None when unusable."""
        if token is None:return None
        guid,index=parse_token(token)
        if not self.fresh(guid):return None
        d=self.devices[guid];low,high,raw=d['axis_minimum'][index],d['axis_maximum'][index],d['raw'][index]
        if high<=low or not low<=raw<=high:return None
        return (raw-low)/(high-low)

    def digital(self,token):
        """(down, press_count, release_count, generation) for a button/hat token, or None when unusable."""
        device,channel=hotas_buttons.parse_token(token)
        if not self.fresh(device):return None
        d=self.devices[device]
        if 'digital_down' not in d:return None
        return (bool(d['digital_down'][channel//64]>>(channel%64)&1),d['press_count'][channel],d['release_count'][channel],d['generation'])

    def snapshot_raw(self):
        return {guid:list(d['raw']) for guid,d in self.devices.items() if self.fresh(guid)}


class AxisCapture:
    """Bind by movement: the first axis to move CAPTURE_THRESHOLD from where it rested wins."""
    def __init__(self,table,now=time.monotonic):
        self.table=table;self.now=now;self.baseline=table.snapshot_raw();self.started=now();self.streak={}

    def poll(self):
        """Token string when captured, 'timeout', or None while listening."""
        if self.now()-self.started>CAPTURE_TIMEOUT:return 'timeout'
        best=None
        for guid,raw in self.table.snapshot_raw().items():
            base=self.baseline.setdefault(guid,list(raw))
            for index,value in enumerate(raw):
                delta=abs(value-base[index])
                if delta>=CAPTURE_THRESHOLD and (best is None or delta>best[0]):best=(delta,guid,index)
        if best is None:
            self.streak={};return None
        key=(best[1],best[2]);self.streak={key:self.streak.get(key,0)+1}
        if self.streak[key]>=CAPTURE_FRAMES:return f'{best[1]}@axis:0x{0x30+best[2]:02X}'
        return None


class ButtonCapture:
    """Bind by press: the first button or hat direction to register a new press wins.

    Press counters, not levels, so a button already held when capture starts never binds, and a
    tap shorter than one poll still does.
    """
    def __init__(self,table,now=time.monotonic,modifier=None):
        """modifier: the shift button token. A binding made while it is held comes back as
        '<token>+shift'. Pressed and released alone, it comes back as itself, so the menu can say it is
        the shift button instead of ignoring the press silently (live 2026-10-16)."""
        self.table=table;self.now=now;self.started=now();self.baseline=self._sample();self.modifier=modifier
        self.modifier_pressed=False

    def _sample(self):
        out={}
        for guid,d in self.table.devices.items():
            if 'press_count' in d and self.table.fresh(guid):out[guid]=(d['generation'],list(d['press_count']))
        return out

    def poll(self):
        """Token string when captured, 'timeout', or None while listening."""
        if self.now()-self.started>CAPTURE_TIMEOUT:return 'timeout'
        for guid,(generation,counts) in self._sample().items():
            base=self.baseline.get(guid)
            if base is None or base[0]!=generation:
                self.baseline[guid]=(generation,counts);continue  # new device or reconnect: baseline only
            for channel,(was,now_count) in enumerate(zip(base[1],counts)):
                if was!=now_count:
                    if channel<hotas_buttons.BUTTON_COUNT:token=f'{guid}@button:{channel+1}'
                    else:
                        p,d=divmod(channel-hotas_buttons.BUTTON_COUNT,4)
                        token=f'{guid}@pov:{p}:{hotas_buttons.POV_DIRECTIONS[d]}'
                    if token==self.modifier:self.modifier_pressed=True;continue
                    if self.modifier:
                        state=self.table.digital(self.modifier)
                        if state is not None and state[0]:return token+'+shift'
                    return token
        if self.modifier_pressed:
            state=self.table.digital(self.modifier)
            if state is not None and not state[0]:return self.modifier
        return None


class KeyCapture:
    """Bind a keyboard key: the first key in the catalog to go down after capture starts.

    key_down(vk) -> bool, the physical key state (GetAsyncKeyState in the runtime). Keys already
    down at the start are ignored until released. Esc is not capturable: it is NMS's Back, which
    cancels the capture first; the Back (Esc) preset row covers it.
    """
    def __init__(self,key_down,now=time.monotonic):
        self.key_down=key_down;self.now=now;self.started=now()
        self.vks=[vk for vk in hotas_buttons.KEY_NAMES if vk!=0x1B]
        self.held={vk for vk in self.vks if key_down(vk)}

    def poll(self):
        """'key.0xNN' when captured, 'timeout', or None while listening."""
        if self.now()-self.started>CAPTURE_TIMEOUT:return 'timeout'
        for vk in self.vks:
            down=self.key_down(vk)
            if vk in self.held:
                if not down:self.held.discard(vk)
            elif down:return f'key.0x{vk:02X}'
        return None


CLEAR_KEYS=(0x2E,0x08)  # Delete, Backspace


class ClearableCapture:
    """Wraps a button or axis capture: Delete or Backspace pressed during capture returns 'clear'.
    Esc and right click are NMS's Back, which the menu cannot tell apart from leaving, so they cancel."""
    def __init__(self,inner,key_down):
        self.inner=inner;self.key_down=key_down;self.held={vk for vk in CLEAR_KEYS if key_down(vk)}

    def poll(self):
        for vk in CLEAR_KEYS:
            down=self.key_down(vk)
            if vk in self.held:
                if not down:self.held.discard(vk)
            elif down:return 'clear'
        return self.inner.poll()


class BusReader:
    """Reads every bus device each poll. Wraps the pinned DLL loaded by DirectInputBus."""
    def __init__(self,bus):
        self.bus=bus;self.index={};self.last_scan=0.

    def scan(self):
        api,abi=self.bus.api,self.bus.abi;index={}
        for i in range(max(0,min(64,api.getDeviceCount()))):
            info=abi.DeviceInfoV1(structSize=C.sizeof(abi.DeviceInfoV1))
            if api.getDevice(i,C.byref(info)) or info.deviceIndex!=i:continue
            identity=info.persistentId.decode(errors='replace')
            guid=identity.upper() if identity.startswith('{') else identity.lower()
            if guid.startswith('{') or guid=='virtual:agent-0':index[guid]=(i,info.instanceName.decode(errors='replace'))
        self.index=index;self.last_scan=time.monotonic()

    def read(self,rescan_seconds=2.):
        if not self.index or time.monotonic()-self.last_scan>rescan_seconds:self.scan()
        api,abi=self.bus.api,self.bus.abi;frames={}
        for guid,(i,name) in self.index.items():
            snap=abi.DeviceSnapshotV1(structSize=C.sizeof(abi.DeviceSnapshotV1))
            if api.getSnapshot(i,C.byref(snap)) or snap.deviceIndex!=i:continue
            frames[guid]={'name':name,'connected':bool(snap.connected),'sequence':snap.sequence,'generation':snap.producerGeneration,
                          'raw':list(snap.rawAxes),'axis_minimum':list(snap.axisMinimum),'axis_maximum':list(snap.axisMaximum),
                          'digital_down':list(snap.digitalDown),'press_count':list(snap.pressCount),'release_count':list(snap.releaseCount)}
        return frames
