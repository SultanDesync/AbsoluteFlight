"""Offline-qualified six-channel mailbox for D12; not attached to any game hook.

Windows x64 ABI: header[64], two slots[128]; each slot has a uint64 reader
count at +0 and a 64-byte payload at +16. One writer fills the inactive slot
then XCHGs the aligned uint64 generation at header+0. Readers pin a slot and
recheck the full generation before copying. A pinned slot is never overwritten.
The header's uint64 stop latch at +8 is independent of normal publication.
The reserved uint64 at +16 is a separate tagged-agent stop latch; it never
suppresses physical commands and requires an explicit neutral rearm.
"""
import ctypes as C
from dataclasses import dataclass
import math
import platform
import struct
import threading
import time

CHANNELS = ('yaw', 'pitch', 'roll', 'throttle', 'lateral', 'vertical')
HEADER_SIZE, SLOT_SIZE, PAYLOAD_OFFSET, PAYLOAD_SIZE = 64, 128, 16, 64
SIZE = HEADER_SIZE + 2 * SLOT_SIZE
PAYLOAD = struct.Struct('<QI4x6fQ16x')
# A per-payload authority tag. It is copied atomically with the values; changing
# a Python-side mode flag cannot broaden an invocation's latched permissions.
STRICT_ROTATION = 0x80000000


class _Code:
    def __init__(self):
        self.data, self.labels, self.fixups = bytearray(), {}, []

    def emit(self, value):
        self.data.extend(bytes.fromhex(value))

    def label(self, name):
        self.labels[name] = len(self.data)

    def jump(self, opcode, label):
        self.emit(opcode)
        self.fixups.append((len(self.data), label))
        self.data.extend(bytes(4))

    def finish(self):
        for offset, label in self.fixups:
            struct.pack_into('<i', self.data, offset, self.labels[label] - offset - 4)
        return bytes(self.data)


def reader_code(copy=True):
    """Position-independent native reader; RCX=header, RDX=64-byte output.

    Copy mode returns the full generation in RAX. Pin mode returns the slot
    base in RAX, and its caller must LOCK DEC qword[slot] after reading.
    Uses only volatile Windows x64 registers; no calls or stack use.
    """
    c = _Code()
    c.emit('49 89 ca')                         # r10 = header
    c.label('retry')
    c.emit('4d 8b 02 4d 89 c1 41 83 e1 01')  # r8=gen; r9=gen&1
    c.emit('49 c1 e1 07 4f 8d 4c 0a 40')     # r9=header+64+slot*128
    c.emit('f0 49 ff 01 4d 3b 02')           # pin; compare full generation
    c.jump('0f 85', 'unpin_retry')
    if copy:
        for offset in range(0, 64, 16):
            c.emit('f3 41 0f 6f 41 ' + f'{16 + offset:02x}')
            c.emit('f3 0f 7f 42 ' + f'{offset:02x}')
        c.emit('f0 49 ff 09 49 83 7a 08 00') # unpin; check stop latch
        c.jump('0f 84', 'return')
        c.emit('c7 42 08 00 00 00 00')        # stop suppresses the whole mask
        c.label('return')
        c.emit('4c 89 c0 c3')                  # return generation
    else:
        c.emit('4c 89 c8 c3')                  # return pinned slot base
    c.label('unpin_retry')
    c.emit('f0 49 ff 09')
    c.jump('e9', 'retry')
    return c.finish()


class _Native:
    """Current-process synthetic helpers only; construction never opens NMS."""
    def __init__(self):
        if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
            raise RuntimeError('Mailbox native helpers require Windows x64')
        k = C.WinDLL('kernel32', use_last_error=True)
        k.VirtualAlloc.argtypes = [C.c_void_p, C.c_size_t, C.c_uint32, C.c_uint32]
        k.VirtualAlloc.restype = C.c_void_p
        k.VirtualProtect.argtypes = [C.c_void_p, C.c_size_t, C.c_uint32, C.POINTER(C.c_uint32)]
        k.VirtualProtect.restype = C.c_int
        k.FlushInstructionCache.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t]
        k.FlushInstructionCache.restype = C.c_int
        k.GetCurrentProcess.restype = C.c_void_p
        # Helpers live until interpreter exit, so a native call never outlives code.
        self.address = k.VirtualAlloc(None, 4096, 0x3000, 0x04)
        if not self.address:
            raise C.WinError(C.get_last_error())
        entries = {
            'load': (0, bytes.fromhex('48 8b 01 c3'), [C.c_void_p], C.c_uint64),
            'exchange': (32, bytes.fromhex('48 87 11 48 89 d0 c3'), [C.c_void_p, C.c_uint64], C.c_uint64),
            'unpin': (64, bytes.fromhex('f0 48 ff 09 c3'), [C.c_void_p], None),
            'pin': (128, reader_code(False), [C.c_void_p], C.c_void_p),
            'copy': (512, reader_code(), [C.c_void_p, C.c_void_p], C.c_uint64),
        }
        for name, (offset, code, arguments, result) in entries.items():
            C.memmove(self.address + offset, code, len(code))
            setattr(self, name, C.CFUNCTYPE(result, *arguments)(self.address + offset))
        old = C.c_uint32()
        if not k.VirtualProtect(self.address, 4096, 0x20, C.byref(old)):
            raise C.WinError(C.get_last_error())
        if not k.FlushInstructionCache(k.GetCurrentProcess(), self.address, 4096):
            raise C.WinError(C.get_last_error())


_native = None
_native_lock = threading.Lock()


def native_helpers():
    global _native
    with _native_lock:
        if _native is None:
            _native = _Native()
        return _native


def validate_values(values,limits=(.25,.5,5.)):
    if tuple(limits) not in ((.25,.5,5.),(1.,1.,30.),(1.,1.,60.)):raise ValueError('Unknown mailbox authority')
    steering,throttle,mav=limits
    if len(values) != 6 or not all(math.isfinite(v) for v in values):
        raise ValueError('Six finite channel values required')
    if any(abs(v) > steering for v in values[:3]) or not 0 <= values[3] <= throttle:
        raise ValueError('D10 rotational/throttle authority exceeded')
    if math.hypot(*values[4:]) > mav+1e-10:
        raise ValueError('D12 MAV target norm exceeded')
    return tuple(float(v) for v in values)


@dataclass(frozen=True)
class Snapshot:
    deadline: int
    mask: int
    values: tuple
    generation: int

    def admitted(self, now_tick):
        return bool(self.mask) and now_tick < self.deadline


class CommandMailbox:
    """Single-writer local transport prototype; not a production input bus.

    Native cave integration must use the same pinned-reader ABI. Do not mirror
    these fields into the legacy caves with separate WriteProcessMemory calls.
    """
    def __init__(self,limits=(.25,.5,5.)):
        validate_values([0]*6,limits);self.limits=tuple(limits)
        self.storage = C.create_string_buffer(SIZE)
        self.address = C.addressof(self.storage)
        if self.address % 8:
            raise RuntimeError('Unaligned mailbox')
        self.native = native_helpers()
        self.writer_lock = threading.Lock()

    def slot(self, generation):
        return self.address + HEADER_SIZE + (generation & 1) * SLOT_SIZE

    def publish(self, values, now_tick, *, enabled=True, ttl_ms=100, timeout=.05,
                strict_rotation=False):
        if type(strict_rotation) is not bool:
            raise ValueError('strict_rotation must be boolean')
        values = validate_values(values,self.limits)
        if not isinstance(now_tick, int) or now_tick < 0 or not isinstance(ttl_ms, int) or not 1 <= ttl_ms <= 100:
            raise ValueError('D12 deadline must use native ticks and TTL <=100ms')
        if now_tick + ttl_ms >= 1 << 64 or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Invalid tick range or writer timeout')
        mask = sum(1 << i for i, value in enumerate(values) if enabled and value != 0)
        if strict_rotation and mask:
            if any(abs(value) > .25 for value in values[:3]) or any(values[3:]):
                raise ValueError('Strict rotation permits only rotational values within .25')
            mask |= STRICT_ROTATION
        with self.writer_lock:
            if strict_rotation and self.agent_stopped():
                raise RuntimeError('Agent stop latched; publish neutral then explicitly rearm agent')
            if enabled and self.native.load(self.address + 8):
                raise RuntimeError('Stop latched; publish neutral then explicitly rearm')
            generation = self.native.load(self.address) + 1
            if generation >= 1 << 64:
                raise OverflowError('Generation exhausted; new mailbox required')
            target = self.slot(generation)
            end = time.perf_counter() + timeout
            while self.native.load(target):
                if time.perf_counter() >= end:
                    raise TimeoutError('Inactive slot still pinned; active snapshot retained')
                time.sleep(0)
            payload = PAYLOAD.pack(now_tick + ttl_ms, mask, *values, generation)
            C.memmove(target + PAYLOAD_OFFSET, payload, PAYLOAD_SIZE)
            # XCHG is atomic with a full memory barrier, including across mappings.
            self.native.exchange(self.address, generation)
            return generation

    def read(self):
        output = C.create_string_buffer(PAYLOAD_SIZE)
        generation = self.native.copy(self.address, C.addressof(output))
        deadline, mask, *fields = PAYLOAD.unpack(output.raw)
        if fields[-1] != generation:
            raise RuntimeError('Mailbox generation mismatch')
        return Snapshot(deadline, mask, tuple(fields[:6]), generation)

    def disable(self):
        self.native.exchange(self.address + 8, 1)

    def stop_agent(self):
        """Immediately revoke tagged commands without touching physical input."""
        self.native.exchange(self.address + 16, 1)

    def agent_stopped(self):
        return bool(self.native.load(self.address + 16))

    def rearm_agent(self):
        with self.writer_lock:
            if self.read().values != (0.,) * 6:
                raise RuntimeError('Neutral publication required before agent rearm')
            self.native.exchange(self.address + 16, 0)

    def rearm(self):
        with self.writer_lock:
            if self.read().values != (0.,) * 6:
                raise RuntimeError('Neutral publication required before rearm')
            self.native.exchange(self.address + 8, 0)


class MappedMailbox(CommandMailbox):
    """Named Windows mapping, shared by the external writer and native readers.

    A random per-run Local name is supplied by the controller. Closing a view
    is permitted only after its native readers have been removed.
    """
    def __init__(self, name, create=False,limits=(.25,.5,5.)):
        validate_values([0]*6,limits);self.limits=tuple(limits)
        if not name.startswith('Local\\AbsoluteHOTAS-D12-') or len(name) > 120:
            raise ValueError('Per-run D12 mapping name required')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.k.CreateFileMappingW.argtypes = [C.c_void_p,C.c_void_p,C.c_uint32,C.c_uint32,C.c_uint32,C.c_wchar_p]
        self.k.CreateFileMappingW.restype = C.c_void_p
        self.k.OpenFileMappingW.argtypes = [C.c_uint32,C.c_int,C.c_wchar_p]
        self.k.OpenFileMappingW.restype = C.c_void_p
        self.k.MapViewOfFile.argtypes = [C.c_void_p,C.c_uint32,C.c_uint32,C.c_uint32,C.c_size_t]
        self.k.MapViewOfFile.restype = C.c_void_p
        self.k.UnmapViewOfFile.argtypes = [C.c_void_p]
        self.k.CloseHandle.argtypes = [C.c_void_p]
        self.handle = (self.k.CreateFileMappingW(C.c_void_p(-1),None,4,0,SIZE,name) if create
                       else self.k.OpenFileMappingW(0xf001f,False,name))
        if not self.handle:
            raise C.WinError(C.get_last_error())
        if create and C.get_last_error() == 183:
            self.k.CloseHandle(self.handle)
            raise RuntimeError('Mapping already exists; fresh run required')
        self.address = self.k.MapViewOfFile(self.handle,0xf001f,0,0,SIZE)
        if not self.address:
            self.k.CloseHandle(self.handle)
            raise C.WinError(C.get_last_error())
        self.name, self.native, self.writer_lock = name, native_helpers(), threading.Lock()
        if create:
            C.memset(self.address,0,SIZE)
            self.disable()

    def close(self):
        if self.address:
            self.k.UnmapViewOfFile(self.address)
            self.k.CloseHandle(self.handle)
            self.address = self.handle = 0
