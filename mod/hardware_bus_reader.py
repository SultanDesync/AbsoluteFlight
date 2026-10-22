"""Narrow v1 consumer for BUS-0/first hardware flight; read-only provider API.

Loads the reviewed core without modifying it. Starts/stops only a provider this
instance owns. Calibration/capture/router features remain separate BUS-1/2 work.
"""
import ctypes as C
import hashlib,importlib.util,time
from pathlib import Path


class DirectInputBus:
    def __init__(self,config):
        self.config=config;self.started=False
        dll=Path(config['dll']).resolve(strict=True);abi=Path(config['abi_source']).resolve(strict=True)
        for path,key in ((dll,'dll_sha256'),(abi,'abi_sha256')):
            if hashlib.sha256(path.read_bytes()).hexdigest()!=config[key]:raise RuntimeError('Pinned bus source/binary changed')
        spec=importlib.util.spec_from_file_location('first_flight_bus_abi',abi);self.abi=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.abi)
        self.dll=C.CDLL(str(dll));self.dll.AbsoluteInputBus_QueryApi.argtypes=[C.c_uint32]
        self.dll.AbsoluteInputBus_QueryApi.restype=C.POINTER(self.abi.ApiV1)
        self.dll.AbsoluteInputBus_Start.argtypes=[C.c_uint32];self.dll.AbsoluteInputBus_Start.restype=C.c_uint32
        self.dll.AbsoluteInputBus_Stop.argtypes=[];self.dll.AbsoluteInputBus_Stop.restype=None
        pointer=self.dll.AbsoluteInputBus_QueryApi(1)
        if not pointer:raise RuntimeError('Bus ABI unavailable')
        self.api=pointer.contents
        if self.api.structSize<C.sizeof(self.abi.ApiV1) or self.api.abiVersion!=1 or self.api.providerId!=b'absolute-input-bus-nms' or self.api.capabilities&3!=3:
            raise RuntimeError('Bus ABI/provider mismatch')
        if not all((self.api.getDeviceCount,self.api.getDevice,self.api.getSnapshot,self.api.getProfileState)):
            raise RuntimeError('Required bus API missing')
        self.profile_fn=C.CFUNCTYPE(C.c_uint32,C.c_void_p)(self.api.getProfileState)
        self.inventory={}

    def profile_state(self):
        state=C.create_string_buffer(112);C.c_uint32.from_buffer(state).value=112
        return self.profile_fn(state)

    def start(self):
        if self.started:raise RuntimeError('Consumer already started')
        if self.profile_state()!=1:raise RuntimeError('Another consumer owns the provider')
        if self.dll.AbsoluteInputBus_Start(250):raise RuntimeError('Bus failed to start')
        self.started=True
        try:
            time.sleep(.35)
            count=self.api.getDeviceCount()
            if not 1<=count<=64:raise RuntimeError('Invalid device count')
            self.inventory={}
            for index in range(count):
                info=self.abi.DeviceInfoV1(structSize=C.sizeof(self.abi.DeviceInfoV1))
                if self.api.getDevice(index,C.byref(info)) or info.deviceIndex!=index:raise RuntimeError('Invalid descriptor')
                identity=info.persistentId.decode()
                if identity in self.inventory:raise RuntimeError('Duplicate GUID')
                self.inventory[identity]={'index':index,'name':info.instanceName.decode(errors='replace'),
                    'vidpid':f'{info.vendorId:04X}:{info.productId:04X}','counts':[info.axisCount,info.buttonCount,info.povCount]}
            if not self.inventory:raise RuntimeError('No physical devices')
        except BaseException:self.stop();raise
        return self

    def snapshot(self,identities):
        if not self.started:raise RuntimeError('Bus not running')
        for attempt in range(4):
            result={}
            for identity in identities:
                if identity not in self.inventory:raise RuntimeError('Bound device absent')
                info=self.inventory[identity];index=info['index']
                current=self.abi.DeviceInfoV1(structSize=C.sizeof(self.abi.DeviceInfoV1))
                if (self.api.getDevice(index,C.byref(current)) or current.structSize!=C.sizeof(current)
                    or current.deviceIndex!=index or current.persistentId.decode()!=identity
                    or current.instanceName.decode(errors='replace')!=info['name']
                    or f'{current.vendorId:04X}:{current.productId:04X}'!=info['vidpid']
                    or [current.axisCount,current.buttonCount,current.povCount]!=info['counts']):
                    raise RuntimeError('Device table changed')
                snap=self.abi.DeviceSnapshotV1(structSize=C.sizeof(self.abi.DeviceSnapshotV1))
                if self.api.getSnapshot(index,C.byref(snap)) or snap.structSize!=C.sizeof(snap) or snap.deviceIndex!=index:raise RuntimeError('Invalid snapshot')
                result[identity]={**info,'connected':bool(snap.connected),'sequence':snap.sequence,'generation':snap.producerGeneration,
                    'raw':list(snap.rawAxes),'axis_minimum':list(snap.axisMinimum),'axis_maximum':list(snap.axisMaximum)}
            if len({d['sequence'] for d in result.values()})==1:return result
        raise RuntimeError('Could not obtain a coherent device frame')

    def stop(self):
        if self.started:
            self.started=False;self.dll.AbsoluteInputBus_Stop()
            if self.profile_state()!=1:raise RuntimeError('Provider Stop did not reach NotReady')

    def __enter__(self):return self.start()
    def __exit__(self,*args):self.stop()
