"""T93 eject: the native quick-menu ship eject, callable whenever Sultan flies the ship.

Build NMS.exe 183877 / Steam 26295378 (research/re-log/2026-10-19_claude_ditching.md, "Addendum: why the
quick-menu Eject refuses", build 180836; re-checked on 183791 against the aligned code, 2026-10-28). The quick
menu greys ShipEject (0x41) out in "critical flight phases"; on 183791 that test is 0x15520D0(ship), which returns
the refusal text (UI_SHIP_EJECT_ERROR, UI_SHIP_EJECT_PULSE_ERROR, UI_BIGGS_EJECT_ERROR) or null, called by the menu
build (0x1556EAD) and again by the handler. The handler runs a straight-line block, 0x1562A73..0x1562C7D: find the
player's ship, ask 0x15520D0 (refused: show the text, leave), work out an exit position from the ship's axes, call
0x1789910 with it, then Eject(ship, player, animate 0, force 1) at 0x17872D0. This module copies that exact block
into a stub next to the game, with its RIP-relative operands and outside calls relocated, its outside branches
(no player ship, refused) sent to a 'return 0' exit, and its stack-frame locals moved to a scratch frame of our own.
The refusal call is replaced by `xor eax, eax` (allowed), as on 180836, where the block came after the menu's test
and never asked; so the refusal path, the only code in the block that reads a register it does not set (R15, the
menu), is never taken. RSI (the menu entry, read by the 180836 block's `cmp [rsi+4], 0x41`) still points at a fake
entry with 0x41 there; the 183791 block no longer reads it.

The stub is a plain Win64 function, int stub(void): 1 after the eject call, 0 without a player ship.
The caller runs it on the flight thread outside UpdateControlled (the outer ship Update after-hook).
Construction never attaches to a process; bytes are verified against the recorded hash first.
"""
import ctypes as C
import hashlib
import struct

import capstone as cs

BLOCK_RVA = 0x1562983
BLOCK_END = 0x1562B8D  # just after `call 0x17870F0` (Eject)
BLOCK_SHA256 = '04fdc24797f72d9a2e036d2645919dc5a089d03d318c38ae2abbb648056f8c3a'
FAIL_RVA = 0x1567304   # the handler's common exit: the block's `je` (no player ship) and `jmp` (refused)
EJECT_RVA = 0x17870F0
EJECT_CALL = 0x1562B88  # the block's `call EJECT_RVA`
REFUSAL_RVA = 0x1551FE0  # eject refusal text, or null when allowed (the quick menu's own test)
ALLOWED = bytes.fromhex('31 c0 0f 1f 00')  # xor eax, eax; nop: in place of the block's `call REFUSAL_RVA`
ACTION = 0x41          # cGcQuickMenuActions.ShipEject
FRAME_SIZE = 0x5000    # the block's RBP locals reach rbp+0x4000
ENTRY_SIZE = 0x80
STUB_SIZE = 0x400


def _rel32(out, at, value, address):
    if not -0x80000000 <= value <= 0x7FFFFFFF:
        raise ValueError(f'stub too far from the game for the operand at {address:#x}')
    struct.pack_into('<i', out, at, value)


def allocate_after_image(handle, base, read=C.string_at, tries=4096, length=0x1000):
    """First free 64 KiB block just past the module image. Every target the block reaches lies inside the
    image, so all its rel32 operands fit (2026-10-20: allocate_near picked a block 2 GiB below the eject
    site, out of reach of the data the block reads, and the build failed with 'argument out of range')."""
    from native_roll_patch import K
    header = struct.unpack('<I', read(base+0x3C, 4))[0]
    size = struct.unpack('<I', read(base+header+0x50, 4))[0]  # OptionalHeader.SizeOfImage
    candidate = (base+size+0xFFFF) & ~0xFFFF
    for _ in range(tries):
        address = K.VirtualAllocEx(handle, candidate, length, 0x3000, 0x40)
        if address:return address
        candidate += 0x10000
    raise RuntimeError('No free block after the game image for the eject stub')


def _relocate(block, old_base, new_base, base, fail):
    """Copy block from old_base to new_base. RIP-relative displacements and rel32 branches that leave the
    block are rewritten to the same absolute targets (base = module base); a branch to FAIL_RVA goes to
    `fail` instead, and the one call to REFUSAL_RVA becomes ALLOWED. Branches inside the block keep their
    offsets (the copy is contiguous)."""
    md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64); md.detail = True
    out = bytearray(block); end = old_base+len(block); seen = 0; allowed = 0
    for i in md.disasm(block, old_base):
        seen += i.size
        offset = i.address-old_base; new_address = new_base+offset; next_new = new_address+i.size
        if any(o.type == cs.x86.X86_OP_MEM and o.mem.base == cs.x86.X86_REG_RIP for o in i.operands):
            # Capstone leaves disp_offset unset for some prefixed SSE forms (movdqa): find the field instead.
            at = i.disp_offset if i.disp_size == 4 else bytes(i.bytes).rfind(struct.pack('<i', i.disp))
            if at <= 0:raise ValueError(f'RIP displacement not found at {i.address:#x}')
            _rel32(out, offset+at, i.address+i.size+i.disp-next_new, i.address)
        if i.group(cs.x86.X86_GRP_JUMP) or i.group(cs.x86.X86_GRP_CALL):
            target = i.operands[0].imm if i.operands and i.operands[0].type == cs.x86.X86_OP_IMM else None
            if target is None:raise ValueError(f'indirect branch at {i.address:#x}')
            if old_base <= target < end:continue
            if i.size not in (5, 6):raise ValueError(f'short branch leaves the block at {i.address:#x}')
            if i.mnemonic == 'call' and target == base+REFUSAL_RVA and i.size == len(ALLOWED):
                out[offset:offset+i.size] = ALLOWED; allowed += 1;continue  # rax = 0: not refused
            if target == base+FAIL_RVA:target = fail
            elif i.mnemonic != 'call':raise ValueError(f'unexpected outside jump at {i.address:#x}')
            _rel32(out, offset+i.size-4, target-next_new, i.address)
    if seen != len(block):raise ValueError('block does not decode cleanly')
    if allowed != 1:raise ValueError('eject refusal call not found exactly once')
    return bytes(out)


def stub_code(stub, block, base, frame, entry):
    """Win64 stub at address stub: saves the callee-saved registers the block uses, points RBP at frame and
    RSI at entry, runs the relocated block and returns 1, or 0 through the fail exit."""
    if hashlib.sha256(block).hexdigest() != BLOCK_SHA256:raise ValueError('Eject block changed (game build?)')
    for value in (frame, entry):
        if not value:raise ValueError('frame and entry required')
    prologue = (bytes.fromhex('55 53 56 57 41 54 41 55 41 56 41 57')  # 8 pushes keep RSP = 8 mod 16
                + bytes.fromhex('48 83 ec 58')                          # shadow space + xmm saves, RSP 16-aligned
                + bytes.fromhex('0f 29 74 24 20 0f 29 7c 24 30 44 0f 29 44 24 40')  # movaps xmm6..8
                + b'\x48\xbd'+struct.pack('<Q', frame)+b'\x48\xbe'+struct.pack('<Q', entry))
    epilogue = (bytes.fromhex('0f 28 74 24 20 0f 28 7c 24 30 44 0f 28 44 24 40 48 83 c4 58')
                + bytes.fromhex('41 5f 41 5e 41 5d 41 5c 5f 5e 5b 5d c3'))
    success = bytes.fromhex('b8 01 00 00 00')
    fail_body = bytes.fromhex('31 c0')
    start = stub+len(prologue)
    after = start+len(block)+len(success)+len(epilogue)
    code = (prologue+_relocate(block, base+BLOCK_RVA, start, base, after)+success+epilogue+fail_body+epilogue)
    if len(code) > STUB_SIZE:raise ValueError('Eject stub overflow')
    return code


def entry_bytes():
    raw = bytearray(ENTRY_SIZE); struct.pack_into('<I', raw, 4, ACTION)
    return bytes(raw)


class Ejector:
    """Owns the stub, its scratch frame and fake menu entry for the life of the process (never freed: a
    flight-thread call may be running). install() builds from the live module; eject() runs it."""
    def __init__(self, base, read_bytes=C.string_at, allocate_near=None, write=None):
        self.base = base; self.read = read_bytes; self.allocate_near = allocate_near; self.write = write
        self.stub = None; self.function = None; self.calls = 0

    def install(self, process_handle):
        block = self.read(self.base+BLOCK_RVA, BLOCK_END-BLOCK_RVA)
        if hashlib.sha256(block).hexdigest() != BLOCK_SHA256:raise RuntimeError('Eject block signature mismatch')
        if self.read(self.base+EJECT_CALL, 5)[:1] != b'\xe8':raise RuntimeError('Eject call missing')
        self.frame = C.create_string_buffer(FRAME_SIZE)
        self.entry = C.create_string_buffer(entry_bytes(), ENTRY_SIZE)
        stub = (self.allocate_near(process_handle, self.base) if self.allocate_near
                else allocate_after_image(process_handle, self.base, self.read))
        code = stub_code(stub, block, self.base, C.addressof(self.frame), C.addressof(self.entry))
        if self.write is not None:self.write(stub, code)
        else:C.memmove(stub, code, len(code))
        if C.string_at(stub, len(code)) != code:raise RuntimeError('Eject stub readback failed')
        self.stub = stub; self.function = C.CFUNCTYPE(C.c_int)(stub)
        return stub

    def eject(self):
        if self.function is None:raise RuntimeError('Eject stub not installed')
        self.calls += 1
        return int(self.function())
