"""Scoped replacement of native ship input friction, NMS 180836 / Steam 25732212.

The producer resets +0x54, optionally calculates native sticky gain, and joins at
RVA 0x142109F before both scalar pitch/yaw consumers. This cave replaces that
one factor; it never multiplies another slowdown onto it or adds steering.

Admission uses this invocation's D12 owner, stack, thread, scalar receipts,
original-input provenance, deadlines and stop latch. A second pinned mailbox
carries only a finite positive gain in [0.5, 1], leased for at most 50 ms.
All other calls replay the original instructions. No game access on import.
Installation/restoration require the runtime's qualified code writer; executable
and mailbox allocations remain alive until process exit, including after stop.
The producer has twelve direct native callers, so a flight-thread boundary alone
does not establish patch quiescence. Use the runtime's external all-thread writer
unless exclusive execution on that thread has been independently qualified.
"""
import ctypes as C
import math
import struct
import threading

from command_mailbox import CommandMailbox, reader_code
from native_roll_patch import Code, K
from native_six_axis import save, restore

SITE_RVA = 0x142109F
ORIGINAL = bytes.fromhex('f30f10442430410f28c8')
CALLER_RVA = 0x1759165
APP_GLOBAL = 0x6E8D708
PRODUCER_OFFSET = 0x60D0
FACTOR_OFFSET = 0x54
BLOCK = 0x800
DATA_SIZE = 128
COMMAND_MS = 50
MIN_GAIN = .5
MAX_GAIN = 1.

# Exact-build below-entry pins. The producer's saved outer RBP is [RBP+100],
# and its original return address is [RBP+108], from this prologue.
PINS = {
    0x141FEE1: bytes.fromhex('555657498dabf8feffff4881ecf0010000'),
    0x1420AC5: bytes.fromhex('c746540000803f'),
    0x142133E: bytes.fromhex('f30f594e54'),
    0x1421450: bytes.fromhex('f30f594e54'),
    0x1759160: bytes.fromhex('e86b6dccff'),
}

_RETAINED = []


def verify(read, base, *, site=True):
    """Check the qualified exact-build seams using the caller's read function."""
    return all(read(int(base)+rva, len(raw)) == raw
               for rva, raw in ({**PINS, SITE_RVA: ORIGINAL} if site else PINS).items())


def code_bytes(cave, base, data, frame, flight_mailbox, gain_mailbox, tick,
               pilot_deadline, *, global_slot=None):
    """Emit a jump cave entered with the producer's original RSP%16 == 0.

    Saves all Windows volatile GPR/XMM registers and RFLAGS around the clock
    call and pinned-reader helper. The replayed MOVSS uses the restored original
    RSP and clears XMM0's upper lanes exactly as native memory MOVSS does.
    """
    c = Code(cave)
    save(c)

    def reject(raw, condition=b'\x0f\x85', label='out'):
        c.emit(bytes.fromhex(raw)); c.relative(condition, label)

    def check(prefix, address, suffix=b'', condition=b'\x0f\x85', label='out'):
        c.relative(prefix, address, suffix); c.relative(condition, label)

    def pointer(reg, address):
        # Only volatile RAX/R10/R11 are used by this generator.
        c.emit({0:b'\x48\xb8', 10:b'\x49\xba', 11:b'\x49\xbb'}[reg]
               + struct.pack('<Q', int(address)))

    def stopped(address, label='out'):
        pointer(11, address)
        reject('49 83 7b 08 00', label=label)

    check(b'\x48\x83\x3d', frame, b'\x01')
    c.relative(b'\x48\x8b\x05', frame+8)
    reject('48 85 c0', b'\x0f\x84')
    c.emit(bytes.fromhex('48 05 d0 60 00 00 48 39 c6'))
    c.relative(b'\x0f\x85', 'out')
    # Prove the specific native caller AND the outer invocation's actual stack.
    pointer(0, base+CALLER_RVA)
    reject('48 39 85 08 01 00 00')
    c.emit(bytes.fromhex('48 8b 85 00 01 00 00'))
    check(b'\x48\x3b\x05', frame+16)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
    check(b'\x3b\x05', frame+32)
    check(b'\x83\x3d', frame+56, b'\x00')
    check(b'\x83\x3d', frame+60, b'\x03')
    c.relative(b'\x48\x8b\x05', frame+24)
    reject('48 85 c0', b'\x0f\x84')
    check(b'\x48\x3b\x05', data+64, condition=b'\x0f\x84')
    # At least one scalar is ours, and each requested scalar was really replaced.
    c.relative(b'\x8b\x05', frame+72)
    c.emit(bytes.fromhex('83 e0 03 85 c0')); c.relative(b'\x0f\x84', 'out')
    c.relative(b'\x8b\x0d', frame+200)
    c.emit(bytes.fromhex('83 e1 03 39 c8')); c.relative(b'\x0f\x85', 'out')
    for index in (0, 1):
        # Original native input must have been neutral, including on unowned axes.
        c.relative(b'\x8b\x05', frame+176+4*index)
        c.emit(bytes.fromhex('25 ff ff ff 7f')+b'\x3d'+struct.pack('<f', .00001))
        c.relative(b'\x0f\x87', 'out')
        next_axis = 'axis_done_'+str(index)
        check(b'\xf7\x05', frame+72, struct.pack('<I', 1 << index),
              condition=b'\x0f\x84', label=next_axis)
        c.relative(b'\x8b\x05', frame+80+4*index)
        check(b'\x3b\x05', frame+184+4*index)
        c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 3f'))
        c.relative(b'\x0f\x87', 'out')
        c.label(next_axis)
    c.relative(b'\x48\x83\x3d', frame+104, b'\x00')
    c.relative(b'\x0f\x84', 'out')
    stopped(flight_mailbox)
    stopped(gain_mailbox)
    # Current native protected modes take precedence over a previous Python tick.
    c.relative(b'\x4c\x8b\x15', frame+8)  # R10 = ship, not producer
    for raw in ('41 80 ba c8 60 00 00 01', '41 80 ba 20 67 00 00 00',
                '41 83 ba 4c 67 00 00 00', '41 83 ba 08 73 00 00 00',
                '41 83 ba 20 64 00 00 00', '41 80 ba d0 73 00 00 00',
                '41 80 ba b5 76 00 00 00', '41 80 ba d4 6c 00 00 00',
                '41 80 ba 10 77 00 00 00'):
        reject(raw)
    pointer(0, base+APP_GLOBAL if global_slot is None else global_slot)
    reject('48 8b 00 48 85 c0', b'\x0f\x84')
    for raw in ('83 b8 98 54 87 00 00', '83 b8 78 b0 72 00 00',
                '83 b8 68 b0 72 00 00'):
        reject(raw)
    reject('80 b8 0d a4 57 00 01', b'\x0f\x87')  # known manual combat allowed
    pointer(0, tick); c.emit(bytes.fromhex('ff d0'))
    # Check all independently leased D12 clocks before publishing any observation.
    for address in (frame+40, frame+64, pilot_deadline):
        pointer(11, address); reject('49 3b 03', b'\x0f\x83')
    c.emit(bytes.fromhex('48 89 44 24 08'))  # now; no further OS calls
    # The native gain observation is one writer / seqlocked, independent of apply.
    c.relative(b'\x48\xff\x05', data)
    c.relative(b'\x48\xff\x05', data+8)
    c.relative(b'\x48\x89\x05', data+32)
    c.emit(bytes.fromhex('8b 46 54'))
    c.relative(b'\x89\x05', data+24)
    c.relative(b'\x89\x05', data+28)
    c.relative(b'\x48\x8b\x05', frame+24)
    c.relative(b'\x48\x89\x05', data+40)
    # Pin one complete gain snapshot. The writer cannot overwrite it until unpin.
    c.emit(b'\x48\xb9'+struct.pack('<Q', gain_mailbox))
    c.relative(b'\xe8', 'reader')
    c.emit(bytes.fromhex('48 89 44 24 10 4c 3b 40 38'))
    c.relative(b'\x0f\x85', 'unpin')  # payload generation == pinned generation
    reject('83 78 18 01', label='unpin')  # exactly the gain lane
    c.emit(bytes.fromhex('48 8b 50 10 48 2b 54 24 08'))
    reject('48 85 d2', b'\x0f\x8e', 'unpin')
    reject('48 83 fa 32', b'\x0f\x87', 'unpin')  # no command more than 50 ms ahead
    c.emit(bytes.fromhex('8b 40 20 3d 00 00 00 3f'))
    c.relative(b'\x0f\x82', 'unpin')
    reject('3d 00 00 80 3f', b'\x0f\x87', 'unpin')
    # Recheck both stop latches immediately before the one factor write. The
    # pinned payload stays immutable across any concurrent neutral publication.
    stopped(flight_mailbox, 'unpin')
    stopped(gain_mailbox, 'unpin')
    c.emit(bytes.fromhex('89 46 54'))
    c.relative(b'\x89\x05', data+28)
    c.relative(b'\x48\xff\x05', data+16)
    c.relative(b'\x48\x8b\x05', frame+24)
    c.relative(b'\x48\x89\x05', data+64)
    c.emit(bytes.fromhex('48 8b 44 24 10 48 8b 40 38'))
    c.relative(b'\x48\x89\x05', data+48)
    c.label('unpin')
    c.emit(bytes.fromhex('48 8b 44 24 10 f0 48 ff 08'))
    c.relative(b'\x48\xff\x05', data)
    c.label('out')
    restore(c)
    c.emit(ORIGINAL)
    c.relative(b'\xe9', base+SITE_RVA+len(ORIGINAL))
    c.label('reader'); c.emit(reader_code(False))
    raw = c.finish()
    if len(raw) >= BLOCK:
        raise ValueError('Aim slowdown cave overlaps its observation block')
    return raw


class AimSlowdownPatch:
    """Own a pinned gain transport and one leased native factor replacement.

    Construct only once the D12 frame/flight mailbox addresses are stable.
    install/restore take the runtime's synchronous all-thread write_code callback.
    Disabled/expired/invalid commands preserve native friction. Publish factor 1
    while custom friction owns the policy but its angular bubble is inactive.
    """
    def __init__(self, pm, pid, base, frame, flight_mailbox, tick_function,
                 pilot_deadline, *, allocate_near=None):
        if allocate_near is None:
            from native_roll_patch import allocate_near
        self.pm, self.pid, self.base = pm, pid, int(base)
        self._writer_lock, self.stopped = threading.RLock(), False
        self.frame, self.flight_mailbox = int(frame), int(flight_mailbox)
        self.tick_function, self.pilot_deadline = int(tick_function), int(pilot_deadline)
        if not all(address > 0 for address in
                   (self.base, self.frame, self.flight_mailbox, self.tick_function, self.pilot_deadline)):
            raise ValueError('Positive stable native addresses required')
        if not verify(pm.read_bytes, self.base):
            raise RuntimeError('Aim slowdown exact-build code mismatch; no patch applied')
        self.site = self.base+SITE_RVA
        self.box = CommandMailbox(limits=(1., 1., 30.))
        self.box.disable()
        self.cave = int(allocate_near(pm.process_handle, self.site))
        self.data = self.cave+BLOCK
        self.original = ORIGINAL
        self.patch = b'\xe9'+struct.pack('<i', self.cave-self.site-5)+b'\x90'*(len(ORIGINAL)-5)
        self.code = code_bytes(self.cave, self.base, self.data, self.frame,
                               self.flight_mailbox, self.box.address,
                               self.tick_function, self.pilot_deadline)
        pm.write_bytes(self.cave, self.code, len(self.code))
        pm.write_bytes(self.data, bytes(DATA_SIZE), DATA_SIZE)
        if pm.read_bytes(self.cave, len(self.code)) != self.code:
            raise RuntimeError('Aim slowdown cave readback failed')
        if not K.FlushInstructionCache(pm.process_handle, self.cave, len(self.code)):
            raise OSError('Aim slowdown cache flush failed')
        self.installed = False
        _RETAINED.append(self)

    def install(self, write_code):
        if self.stopped:
            raise RuntimeError('Stopped aim slowdown bridge cannot be installed again')
        if self.installed:
            return True
        if not verify(self.pm.read_bytes, self.base):
            raise RuntimeError('Aim slowdown code changed before installation')
        write_code(self.pm, self.pid, self.site, self.original, self.patch)
        if self.pm.read_bytes(self.site, len(self.patch)) != self.patch:
            raise RuntimeError('Aim slowdown installation readback failed')
        self.installed = True
        return True

    def publish(self, gain, now_tick, *, enabled=True, ttl_ms=COMMAND_MS):
        with self._writer_lock:
            if self.stopped:
                return None
            if (not isinstance(ttl_ms, int) or not 1 <= ttl_ms <= COMMAND_MS
                    or not math.isfinite(gain) or not MIN_GAIN <= gain <= MAX_GAIN):
                raise ValueError('Gain must be 0.5..1 with a 1..50 ms lease')
            if not enabled:
                self.disable()
                return None
            if self.box.native.load(self.box.address+8):
                self.box.publish((0.,)*6, now_tick, enabled=False, ttl_ms=ttl_ms)
                self.box.rearm()
            return self.box.publish((float(gain), 0., 0., 0., 0., 0.), now_tick, ttl_ms=ttl_ms)

    def disable(self):
        with self._writer_lock:
            self.box.disable()

    def stop(self):
        """Terminal stop, serialized against publication; this object cannot rearm."""
        with self._writer_lock:
            self.stopped = True
            self.box.disable()

    def restore(self, write_code):
        self.stop()
        current = self.pm.read_bytes(self.site, len(ORIGINAL))
        if current == self.patch:
            write_code(self.pm, self.pid, self.site, self.patch, self.original)
        elif current != self.original:
            raise RuntimeError('Aim slowdown restore refused: foreign site bytes')
        if self.pm.read_bytes(self.site, len(ORIGINAL)) != self.original:
            raise RuntimeError('Aim slowdown original-byte restoration failed')
        self.installed = False
        return True

    def snapshot(self):
        for _ in range(3):
            seq = int.from_bytes(self.pm.read_bytes(self.data, 8), 'little')
            if seq & 1:
                continue
            raw = self.pm.read_bytes(self.data, DATA_SIZE)
            if seq != int.from_bytes(self.pm.read_bytes(self.data, 8), 'little'):
                continue
            return {'installed': self.installed, 'stopped': self.stopped,
                    'visits': struct.unpack_from('<Q', raw, 8)[0],
                    'applied': struct.unpack_from('<Q', raw, 16)[0],
                    'native_gain': struct.unpack_from('<f', raw, 24)[0],
                    'effective_gain': struct.unpack_from('<f', raw, 28)[0],
                    'tick': struct.unpack_from('<Q', raw, 32)[0],
                    'invocation': struct.unpack_from('<Q', raw, 40)[0],
                    'generation': struct.unpack_from('<Q', raw, 48)[0]}
        return {'installed': self.installed, 'state': 'observation busy'}
