"""Build-180836 D6 roll cave: low XMM7 lane, native deadlines, verified restore."""
import contextlib
import ctypes as C
import json
from pathlib import Path
import struct
import time

K=C.WinDLL("kernel32",use_last_error=True)
K.OpenThread.argtypes=[C.c_uint32,C.c_int,C.c_uint32];K.OpenThread.restype=C.c_void_p
K.SuspendThread.argtypes=[C.c_void_p];K.SuspendThread.restype=C.c_uint32
K.ResumeThread.argtypes=[C.c_void_p];K.ResumeThread.restype=C.c_uint32
K.GetThreadContext.argtypes=[C.c_void_p,C.c_void_p];K.GetThreadContext.restype=C.c_int
K.CloseHandle.argtypes=[C.c_void_p]
K.VirtualProtectEx.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,C.c_uint32,C.POINTER(C.c_uint32)]
K.VirtualProtectEx.restype=C.c_int
K.FlushInstructionCache.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t];K.FlushInstructionCache.restype=C.c_int
K.GetCurrentProcess.argtypes=[];K.GetCurrentProcess.restype=C.c_void_p
K.VirtualAllocEx.argtypes=[C.c_void_p,C.c_void_p,C.c_size_t,C.c_uint32,C.c_uint32];K.VirtualAllocEx.restype=C.c_void_p
ORIGINAL=bytes.fromhex("44 0F 28 EF F3 44 0F 59 AE 88 66 00 00")
SITE_RVA=0x175DCE9


class Code:
    def __init__(self,base):self.base=base;self.data=bytearray();self.labels={};self.fixups=[]
    def emit(self,value):self.data.extend(value)
    def label(self,name):self.labels[name]=self.base+len(self.data)
    def relative(self,prefix,target,suffix=b""):
        self.emit(prefix);position=len(self.data);self.emit(bytes(4));self.emit(suffix)
        self.fixups.append((position,target,self.base+len(self.data)))
    def finish(self):
        for position,target,end in self.fixups:
            address=self.labels[target] if isinstance(target,str) else target
            struct.pack_into("<i",self.data,position,address-end)
        return bytes(self.data)


def cave_bytes(cave,site,data,get_tick,trace=False):
    c=Code(cave)
    c.emit(b"\x9c")  # save RFLAGS before every test
    c.relative(b"\x83\x3d",data,b"\x00")
    c.relative(b"\x0f\x84","original")
    c.relative(b"\x48\x3b\x35",data+8)
    c.relative(b"\x0f\x85","original")
    c.emit(bytes.fromhex("50 51 52 41 50 41 51 41 52 41 53"))  # all volatile GPRs
    c.emit(bytes.fromhex("48 81 EC 80 00 00 00"))  # 32-byte shadow + six XMM slots
    for register in range(6):
        c.emit(bytes([0xF3,0x0F,0x7F,0x44+8*register,0x24,0x20+16*register]))
    c.emit(b"\x48\xb8"+struct.pack("<Q",get_tick)+b"\xff\xd0")
    c.relative(b"\x48\x3b\x05",data+16)
    c.relative(b"\x0f\x83","restore")
    c.relative(b"\x48\x3b\x05",data+24)
    c.relative(b"\x0f\x83","restore")
    c.relative(b"\xf3\x0f\x10\x05",data+4)
    c.emit(bytes.fromhex("F3 0F 10 F8"))  # register MOVSS preserves XMM7 upper lanes
    if trace:
        c.relative(b"\x48\xff\x05",data+32)
        c.relative(b"\xf3\x0f\x11\x3d",data+40)
    c.label("restore")
    for register in range(6):
        c.emit(bytes([0xF3,0x0F,0x6F,0x44+8*register,0x24,0x20+16*register]))
    c.emit(bytes.fromhex("48 8D A4 24 80 00 00 00 41 5B 41 5A 41 59 41 58 5A 59 58"))
    c.label("original");c.emit(b"\x9d")
    c.emit(ORIGINAL)
    c.relative(b"\xe9",site+len(ORIGINAL))
    result=c.finish()
    if len(result)>=0x200:raise ValueError("cave overlaps its control block")
    return result


class MemoryInfo(C.Structure):
    _fields_=[("base",C.c_void_p),("allocation",C.c_void_p),("allocation_protect",C.c_uint32),
              ("region_size",C.c_size_t),("state",C.c_uint32),("protect",C.c_uint32),("kind",C.c_uint32)]


K.VirtualQueryEx.argtypes=[C.c_void_p,C.c_void_p,C.POINTER(MemoryInfo),C.c_size_t]
K.VirtualQueryEx.restype=C.c_size_t


def allocate_near(handle,site):
    lower=max(0x10000,(site-0x7FFF0000)&~0xFFFF);upper=site+0x7FFF0000
    cursor=lower
    while cursor<upper:
        info=MemoryInfo()
        if not K.VirtualQueryEx(handle,cursor,C.byref(info),C.sizeof(info)):
            raise OSError(C.get_last_error(),"VirtualQueryEx")
        start=info.base or 0;end=start+info.region_size
        if end<=cursor:raise RuntimeError("nonadvancing memory query")
        if info.state==0x10000:
            candidate=(max(start,cursor)+0xFFFF)&~0xFFFF
            if candidate+0x1000<=min(end,upper):
                address=K.VirtualAllocEx(handle,candidate,0x1000,0x3000,0x40)
                if address:
                    if abs(address-site)>=0x80000000:raise RuntimeError("allocation outside rel32 range")
                    return address
        cursor=end
    raise RuntimeError("No nearby allocation; no patch applied")


class BusyInstruction(Exception):pass


@contextlib.contextmanager
def stopped_threads(pid,site,size):
    import os,psutil
    handles=[]
    # In-process callers (the release mod) must not suspend themselves, and
    # nothing may allocate while other threads are suspended: a suspended
    # thread can hold the heap lock. Context buffers are allocated up front.
    own=K.GetCurrentThreadId() if pid==os.getpid() else None
    threads=[t.id for t in psutil.Process(pid).threads() if t.id!=own]
    storage=C.create_string_buffer((0x4D0+16)*len(threads)+15)
    try:
        for thread_id in threads:
            handle=K.OpenThread(0x4A,False,thread_id)
            if not handle:
                if C.get_last_error()==87:continue  # thread exited after enumeration
                raise OSError(C.get_last_error(),"OpenThread")
            if K.SuspendThread(handle)==0xFFFFFFFF:
                K.CloseHandle(handle);raise OSError(C.get_last_error(),"SuspendThread")
            handles.append(handle)
        for index,handle in enumerate(handles):
            address=(C.addressof(storage)+15+index*(0x4D0+16))&~15
            C.c_uint32.from_address(address+0x30).value=0x100001
            if not K.GetThreadContext(handle,address):raise OSError(C.get_last_error(),"GetThreadContext")
            rip=C.c_uint64.from_address(address+0xF8).value
            if site<rip<site+size:raise BusyInstruction("thread inside replaced instructions")
        yield len(handles)
    finally:
        for handle in reversed(handles):
            K.ResumeThread(handle);K.CloseHandle(handle)


def write_code_in_thread(pm,pid,site,expected,replacement):
    """replace_code without suspending anything, for in-process use from the one thread that
    executes site while it is outside it (release mod: the UpdateControlled after-detour).
    Suspending threads in-process can freeze the game: a thread suspended while holding the GIL
    (or the heap lock) blocks the patcher forever (2026-10-16 load hang). pm/pid are unused and
    kept so this is a drop-in writer."""
    size=len(expected)
    if size!=len(replacement):raise ValueError("replacement length mismatch")
    if C.string_at(site,size)!=expected:raise RuntimeError("code ownership/expected bytes mismatch")
    process=K.GetCurrentProcess();old=C.c_uint32()
    if not K.VirtualProtectEx(process,site,size,0x40,C.byref(old)):raise OSError(C.get_last_error(),"VirtualProtectEx")
    try:
        C.memmove(site,replacement,size)
        if C.string_at(site,size)!=replacement:raise RuntimeError("code readback mismatch")
        if not K.FlushInstructionCache(process,site,size):raise OSError(C.get_last_error(),"FlushInstructionCache")
    except BaseException:
        C.memmove(site,expected,size);K.FlushInstructionCache(process,site,size)
        raise
    finally:
        ignored=C.c_uint32()
        if not K.VirtualProtectEx(process,site,size,old.value,C.byref(ignored)):raise OSError(C.get_last_error(),"page protection restore")


def replace_code(pm,pid,site,expected,replacement):
    if len(expected)!=len(replacement):raise ValueError("replacement length mismatch")
    for attempt in range(20):
        try:
            with stopped_threads(pid,site,len(expected)) as count:
                if pm.read_bytes(site,len(expected))!=expected:
                    raise RuntimeError("code ownership/expected bytes mismatch")
                old=C.c_uint32()
                if not K.VirtualProtectEx(pm.process_handle,site,len(expected),0x40,C.byref(old)):
                    raise OSError(C.get_last_error(),"VirtualProtectEx")
                try:
                    pm.write_bytes(site,replacement,len(replacement))
                    if pm.read_bytes(site,len(replacement))!=replacement:raise RuntimeError("code readback mismatch")
                    if not K.FlushInstructionCache(pm.process_handle,site,len(replacement)):
                        raise OSError(C.get_last_error(),"FlushInstructionCache")
                except BaseException:
                    pm.write_bytes(site,expected,len(expected))
                    K.FlushInstructionCache(pm.process_handle,site,len(expected))
                    raise
                finally:
                    ignored=C.c_uint32()
                    if not K.VirtualProtectEx(pm.process_handle,site,len(expected),old.value,C.byref(ignored)):
                        raise OSError(C.get_last_error(),"page protection restore")
                return count
        except BusyInstruction:
            time.sleep(.01)
    raise RuntimeError("Unable to quiesce patch boundary")


class RollPatch:
    def __init__(self,pm,pid,ship,get_tick,manifest,trace=False):
        self.pm,self.pid=pm,pid
        self.site=pm.process_base.lpBaseOfDll+SITE_RVA
        if pm.read_bytes(self.site,len(ORIGINAL))!=ORIGINAL:raise RuntimeError("Roll originals differ; no write")
        self.cave=allocate_near(pm.process_handle,self.site)
        self.block=self.cave+0x200;self.installed=False
        self.patch=b"\xE9"+struct.pack("<i",self.cave-self.site-5)+b"\x90"*(len(ORIGINAL)-5)
        code=cave_bytes(self.cave,self.site,self.block,get_tick,trace=trace)
        pm.write_bytes(self.cave,code,len(code))
        pm.write_bytes(self.block,struct.pack("<IfQQQ",0,0,ship,0,0),32)
        if trace:pm.write_bytes(self.block+32,bytes(16),16)
        if pm.read_bytes(self.cave,len(code))!=code:raise RuntimeError("cave readback failed")
        K.FlushInstructionCache(pm.process_handle,self.cave,len(code))
        self.metadata={"pid":pid,"site":hex(self.site),"site_rva":hex(SITE_RVA),"cave":hex(self.cave),
            "block":hex(self.block),"original":ORIGINAL.hex(),"patch":self.patch.hex(),"code":code.hex(),"trace":trace,
            "retention":"4KiB allocation retained until process exit; no dangling instruction/pilot pointers"}
        with Path(manifest).open("x") as f:json.dump(self.metadata,f,indent=2);f.write("\n")
    def install(self):
        count=replace_code(self.pm,self.pid,self.site,ORIGINAL,self.patch);self.installed=True
        return count
    def clear(self):
        self.pm.write_bytes(self.block,bytes(4),4)
        self.pm.write_bytes(self.block+16,bytes(8),8)
    def restore(self):
        self.clear()
        current=self.pm.read_bytes(self.site,len(ORIGINAL))
        if current==self.patch:
            replace_code(self.pm,self.pid,self.site,self.patch,ORIGINAL)
        elif current!=ORIGINAL:
            raise RuntimeError("Restore refused: code no longer belongs to this patch")
        self.installed=False
        if self.pm.read_bytes(self.site,len(ORIGINAL))!=ORIGINAL:raise RuntimeError("original-byte restoration failed")
        return True
