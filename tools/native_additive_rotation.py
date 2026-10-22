"""OFFLINE T66 kernel prototype; no installer, allocation, or game access.

Exact static candidate: NMS 180836 / Steam 25732212, RVA 0x175E4F2.
Source: research/re-log/2026-10-16_codex_rotation-additive.{md,json}.
Only XMM6.low (pitch) and XMM7.low (yaw) are replaced, in the native
PRE-sign-flip angular-rate convention. XMM13/roll and subsequent feedback,
world-component MaxTorque clamp and integration remain native. Upper lanes
survive. The displaced MOVSS XMM9 and LEA RBX are replayed semantically.

THIS IS NOT READY TO INSTALL. The Qualification record is an ADAPTER
PREREQUISITE, never a replacement for complete native mode/property/owner,
input provenance, class/body, frontend, combat/autopilot/VR/pulse/landing and
other protected-state gates. No current release adapter produces this record.
A future adapter must evaluate those gates in this exact native invocation,
invalidate before entry/exit or any protected transition, and uniquely bind a
positive ticket to that invocation's RSI ship, RBP frame, native thread and
generation. Copying a Python admission boolean or a previous frame is invalid.
The kernel can check the record's identity/freshness, not establish its truth.

Standalone memory contract (all addresses stable, valid, aligned, retained):
* Control and Qualification start zeroed/disabled. They have ONE serialized
  writer each. Publication uses an atomic odd sequence, a full writer barrier,
  payload writes, a full writer barrier, then a fresh nonzero even sequence.
  Sequence/generation never wrap or get reused. This module has no publisher.
* Qualification is written only by a trusted native adapter on the pinned
  execution thread, remains valid only within that invocation, and supplies a
  native lease independent of the command deadline and hard-deadline slot.
* Control is copied to private stack memory and accepted only if its sequence
  remains unchanged through the clock call; Qualification is also rechecked.
  Command generation must equal the qualification generation and strictly
  exceed the native-only consumed_generation slot. One application per fresh
  generation, so a new frame needs a new command and ticket.
* All deadlines use a monotonic, nonwrapping GetTickCount64-style clock. Now
  must precede all three independently. Command and native lease must be at
  most max_lease_ms ahead; issued_tick must be in the last max_lease_ms.
  The separate hard-deadline slot is native-owned, terminal and never extended
  while this kernel is reachable. There is no automatic enable or rearm.
* Entry RSP must be 16-byte aligned (the candidate native seam contract), the
  supplied clock must implement Win64 ABI, and all code/data rel32 references
  must fit. Nonvolatile registers are the clock's ABI responsibility. Volatile
  registers, RFLAGS and MXCSR are explicitly saved/restored by this kernel.

pitch_limit/yaw_limit have NO defaults: supply independently measured native
scales later. Neither this prototype nor its synthetic fixture selects engine
rates or promises a sqrt(2) realized response under torque saturation.
"""
import ctypes as C
import math
import struct


BUILD = (180836, 25732212)
SITE_RVA = 0x175E4F2
GLOBAL_RVA = 0x4B33680
ORIGINAL = bytes.fromhex('f3 44 0f 10 0d 85 51 3d 03 48 8d 9e c0 6c 00 00')
SCHEMA = 1
QUALIFIED = 1
MAX_LEASE_MS = 50
MAX_CODE_SIZE = 0x1000


class Control(C.LittleEndianStructure):
    """96-byte command POD. Zero initialization intentionally leaves disabled."""
    _fields_ = [('sequence', C.c_uint64), ('enabled', C.c_uint32),
                ('schema', C.c_uint32), ('ship', C.c_uint64),
                ('frame', C.c_uint64), ('thread', C.c_uint32),
                ('reserved', C.c_uint32), ('invocation', C.c_uint64),
                ('generation', C.c_uint64), ('ticket', C.c_uint64),
                ('pitch', C.c_float), ('yaw', C.c_float),
                ('command_deadline', C.c_uint64), ('issued_tick', C.c_uint64),
                ('reserved_tail', C.c_uint64)]


class Qualification(C.LittleEndianStructure):
    """72-byte native adapter receipt; a positive proof is NOT a full gate set."""
    _fields_ = [('sequence', C.c_uint64), ('active', C.c_uint32),
                ('qualified', C.c_uint32), ('ship', C.c_uint64),
                ('frame', C.c_uint64), ('thread', C.c_uint32),
                ('reserved', C.c_uint32), ('invocation', C.c_uint64),
                ('generation', C.c_uint64), ('ticket', C.c_uint64),
                ('lease_deadline', C.c_uint64)]


class Code:
    """Small rel32 assembler; refuse unreachable references before execution."""
    def __init__(self, base):
        self.base, self.data, self.labels, self.fixups = int(base), bytearray(), {}, []

    def emit(self, raw):
        self.data.extend(raw)

    def label(self, name):
        self.labels[name] = self.base+len(self.data)

    def relative(self, prefix, target, suffix=b''):
        self.emit(prefix)
        position = len(self.data)
        self.emit(bytes(4)); self.emit(suffix)
        self.fixups.append((position, target, self.base+len(self.data)))

    def finish(self):
        for position, target, end in self.fixups:
            address = self.labels[target] if isinstance(target, str) else int(target)
            displacement = address-end
            if not -(1 << 31) <= displacement < (1 << 31):
                raise ValueError('T66 rel32 reference is out of range')
            struct.pack_into('<i', self.data, position, displacement)
        return bytes(self.data)


def _limit_bits(value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError('A measured finite positive per-axis rate limit is required')
    try:
        raw = struct.pack('<f', value)
    except (OverflowError, struct.error) as error:
        raise ValueError('Per-axis limit must be a finite float32') from error
    bits = struct.unpack('<I', raw)[0]
    if bits == 0 or bits >= 0x7F800000:
        raise ValueError('Per-axis limit must be a finite positive float32')
    return bits


def code_bytes(cave, base, control, qualification, tick, hard_deadline,
               consumed_generation, *, pitch_limit, yaw_limit,
               max_lease_ms=MAX_LEASE_MS):
    """Generate bytes only. No runtime is connected to this standalone schema.

    A future adapter/installer must additionally verify the exact executable,
    original bytes, full native admission gates, quiescence and restoration.
    consumed_generation is one native-only uint64, initially zero, exclusively
    read/written by this pinned execution thread. The independent hard-deadline
    slot is a uint64; zero always expires. This function performs no writes.
    """
    limits = (_limit_bits(pitch_limit), _limit_bits(yaw_limit))
    addresses = (cave, base, control, qualification, tick, hard_deadline, consumed_generation)
    if any(not isinstance(a, int) or a <= 0 or a >= (1 << 64) for a in addresses):
        raise ValueError('Stable positive uint64 addresses are required')
    if any(a & 7 for a in (control, qualification, hard_deadline, consumed_generation)):
        raise ValueError('Control, qualification, hard deadline and generation must be 8-byte aligned')
    if not isinstance(max_lease_ms, int) or not 1 <= max_lease_ms <= MAX_LEASE_MS:
        raise ValueError('Native/command leases must be bounded to 1..50 ms')
    c = Code(cave)
    # 8 pushes, 256 bytes: preserve the candidate's entry RSP alignment.
    c.emit(bytes.fromhex('9c 50 51 52 41 50 41 51 41 52 41 53 48 81 ec 00 01 00 00'))
    for r in range(6):
        c.emit(bytes([0xF3, 0x0F, 0x7F, 0x44+8*r, 0x24, 0x20+16*r]))
    c.emit(bytes.fromhex('0f ae 9c 24 80 00 00 00'))  # STMXCSR [rsp+80]

    def reject(raw, condition=b'\x0f\x85'):
        c.emit(bytes.fromhex(raw)); c.relative(condition, 'out')

    def check(prefix, address, suffix=b'', condition=b'\x0f\x85'):
        c.relative(prefix, address, suffix); c.relative(condition, 'out')

    def stack_load(offset):
        c.emit(bytes.fromhex('48 8b 84 24')+struct.pack('<I', 0xA0+offset))

    # Initial sequence and private snapshot. R8 retains the initial sequence.
    c.relative(b'\x4c\x8b\x05', control)
    reject('4d 85 c0', b'\x0f\x84')
    reject('41 f6 c0 01')
    for offset in range(0, C.sizeof(Control), 8):
        c.relative(b'\x48\x8b\x05', control+offset)
        c.emit(bytes.fromhex('48 89 84 24')+struct.pack('<I', 0xA0+offset))
    reject('4c 39 84 24 a0 00 00 00')
    c.relative(b'\x48\x8b\x05', qualification)
    reject('48 85 c0', b'\x0f\x84')
    reject('a8 01')
    c.emit(bytes.fromhex('48 89 84 24 88 00 00 00'))
    # Exact schema/enable, reserved fields and positive native qualification.
    reject('83 bc 24 a8 00 00 00 01')
    reject('83 bc 24 ac 00 00 00 01')
    reject('83 bc 24 c4 00 00 00 00')
    reject('48 83 bc 24 f8 00 00 00 00')
    check(b'\x83\x3d', qualification+8, b'\x01')
    check(b'\x83\x3d', qualification+12, b'\x01')
    check(b'\x83\x3d', qualification+36, b'\x00')
    # Both records must pin actual RSI ship, actual RBP frame and actual TLS TID.
    stack_load(16); reject('48 85 c0', b'\x0f\x84'); reject('48 39 f0')
    stack_load(24); reject('48 85 c0', b'\x0f\x84'); reject('48 39 e8')
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
    reject('3b 84 24 c0 00 00 00')
    for offset in (16, 24, 40, 48, 56):
        stack_load(offset)
        if offset in (40, 48, 56):
            reject('48 85 c0', b'\x0f\x84')
        check(b'\x48\x3b\x05', qualification+offset)
    c.emit(bytes.fromhex('8b 84 24 c0 00 00 00'))
    check(b'\x3b\x05', qualification+32)
    stack_load(48)
    check(b'\x48\x3b\x05', consumed_generation, condition=b'\x0f\x86')
    # Clock may overwrite ALL volatiles, shadow space, flags and MXCSR.
    c.emit(b'\x48\xb8'+struct.pack('<Q', tick)+bytes.fromhex('ff d0'))
    c.emit(bytes.fromhex('48 89 84 24 98 00 00 00'))
    # Issued <= now and no older than the bounded lease.
    reject('48 3b 84 24 f0 00 00 00', b'\x0f\x82')
    c.emit(bytes.fromhex('48 2b 84 24 f0 00 00 00 48 3d')+struct.pack('<I', max_lease_ms))
    c.relative(b'\x0f\x87', 'out')
    # Deadline > now, with a maximum ahead bound for command/native leases.
    stack_load(72)
    for index, address in enumerate((None, qualification+64, hard_deadline)):
        if address is not None:
            c.relative(b'\x48\x8b\x05', address)
        reject('48 3b 84 24 98 00 00 00', b'\x0f\x86')
        c.emit(bytes.fromhex('48 2b 84 24 98 00 00 00'))
        if index < 2:
            c.emit(bytes.fromhex('48 3d')+struct.pack('<I', max_lease_ms))
            c.relative(b'\x0f\x87', 'out')
    # Unsigned absolute float bits enforce finite inclusive per-axis limits
    # without FP comparisons, denormal exceptions, or MXCSR side effects.
    for offset, bound in zip((64, 68), limits):
        c.emit(bytes.fromhex('8b 84 24')+struct.pack('<I', 0xA0+offset)
               +bytes.fromhex('25 ff ff ff 7f 3d')+struct.pack('<I', bound))
        c.relative(b'\x0f\x87', 'out')
    # Final sequence and freshness checks immediately precede committing.
    stack_load(0); check(b'\x48\x3b\x05', control)
    c.emit(bytes.fromhex('48 8b 84 24 88 00 00 00'))
    check(b'\x48\x3b\x05', qualification)
    stack_load(48)
    check(b'\x48\x3b\x05', consumed_generation, condition=b'\x0f\x86')
    c.relative(b'\x48\x89\x05', consumed_generation)
    c.emit(bytes.fromhex('f3 0f 10 84 24 e0 00 00 00 f3 0f 10 f0'
                         ' f3 0f 10 84 24 e4 00 00 00 f3 0f 10 f8'))
    c.label('out')
    c.emit(bytes.fromhex('0f ae 94 24 80 00 00 00'))  # LDMXCSR
    for r in range(6):
        c.emit(bytes([0xF3, 0x0F, 0x6F, 0x44+8*r, 0x24, 0x20+16*r]))
    c.emit(bytes.fromhex('48 8d a4 24 00 01 00 00 41 5b 41 5a 41 59 41 58 5a 59 58 9d'))
    # Relocate MOVSS, never copy its original RIP displacement into the cave.
    c.relative(bytes.fromhex('f3 44 0f 10 0d'), base+GLOBAL_RVA)
    c.emit(bytes.fromhex('48 8d 9e c0 6c 00 00'))
    c.relative(b'\xe9', base+SITE_RVA+len(ORIGINAL))
    raw = c.finish()
    if len(raw) > MAX_CODE_SIZE:
        raise ValueError('T66 kernel exceeds one reserved code page')
    return raw
