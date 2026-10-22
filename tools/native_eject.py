"""T93 eject: the native quick-menu ship eject, callable whenever Sultan flies the ship.

Build NMS.exe 180836 / Steam 25732212 (research/re-log/2026-10-19_claude_ditching.md, "Addendum: why the
quick-menu Eject refuses"). The quick menu greys ShipEject (0x41) out in "critical flight phases" at
0x152AFCC..0x152B01C. When it is allowed, its handler runs a straight-line block, 0x1536B3D..0x1536CD7:
find the player's ship, work out an exit position from the ship's axes, call 0x17567C0 with it, then
Eject(ship, player, animate 0, force 1) at 0x1754180. This module copies that exact block into a stub
next to the game, with its RIP-relative operands and outside calls relocated, its one outside branch
(no player ship) sent to a 'return 0' exit, and its stack-frame locals moved to a scratch frame of our
own. The only register the block reads that it does not set itself is RSI (the menu entry, for its
`cmp [rsi+4], 0x41`), which the stub points at a fake entry with 0x41 there.

The stub is a plain Win64 function, int stub(void): 1 after the eject call, 0 without a player ship.
The caller runs it on the flight thread outside UpdateControlled (the outer ship Update after-hook).
Construction never attaches to a process; bytes are verified against the recorded hash first.
"""
import ctypes as C
import hashlib
import struct

import capstone as cs

BLOCK_RVA = 0x1536B3D
BLOCK_END = 0x1536CD7  # just after `call 0x1754180` (Eject)
BLOCK_SHA256 = '9480f3bc7cc6f19dd4df71f0ce2896307cb3581f0c37be50934349e336834046'
FAIL_RVA = 0x153B44C   # the handler's common exit: the block's `je` when the player has no ship
EJECT_RVA = 0x1754180
ACTION = 0x41          # cGcQuickMenuActions.ShipEject
FRAME_SIZE = 0x5000    # the block's RBP locals reach rbp+0x3F80
ENTRY_SIZE = 0x80
STUB_SIZE = 0x400


def _rel32(out, at, value, address):
    if not -0x80000000 <= value <= 0x7FFFFFFF:
        raise ValueError(f'stub too far from the game for the operand at {address:#x}')
    struct.pack_into('<i', out, at, value)


def allocate_after_image(handle, base, read=C.string_at, tries=4096):
    """First free 64 KiB block just past the module image. Every target the block reaches lies inside the
    image, so all its rel32 operands fit (2026-10-20: allocate_near picked a block 2 GiB below the eject
    site, out of reach of the data the block reads, and the build failed with 'argument out of range')."""
    from native_roll_patch import K
    header = struct.unpack('<I', read(base+0x3C, 4))[0]
    size = struct.unpack('<I', read(base+header+0x50, 4))[0]  # OptionalHeader.SizeOfImage
    candidate = (base+size+0xFFFF) & ~0xFFFF
    for _ in range(tries):
        address = K.VirtualAllocEx(handle, candidate, 0x1000, 0x3000, 0x40)
        if address:return address
        candidate += 0x10000
    raise RuntimeError('No free block after the game image for the eject stub')


def _relocate(block, old_base, new_base, base, fail):
    """Copy block from old_base to new_base. RIP-relative displacements and rel32 branches that leave the
    block are rewritten to the same absolute targets (base = module base); a branch to FAIL_RVA goes to
    `fail` instead. Branches inside the block keep their offsets (the copy is contiguous)."""
    md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64); md.detail = True
    out = bytearray(block); end = old_base+len(block); seen = 0
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
            if target == base+FAIL_RVA:target = fail
            elif i.mnemonic != 'call':raise ValueError(f'unexpected outside jump at {i.address:#x}')
            _rel32(out, offset+i.size-4, target-next_new, i.address)
    if seen != len(block):raise ValueError('block does not decode cleanly')
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
        if self.read(self.base+0x1536CD2, 5)[:1] != b'\xe8':raise RuntimeError('Eject call missing')
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
