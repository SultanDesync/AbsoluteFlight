"""Native tunnel vignette and greyout lanes for the G-load mechanic (T77 seam), build 183877 / Steam 26295378.

Ported from codex's T77 probe (research/re-log/2026-10-16_codex_native-vignette.md; scratch job
research-jobs/t77/native_probe.py, 36/36 local x64 harness checks and a user-accepted live proof).
Each lane replaces one direct CALL to the native uniform setter 0x1878300 inside the HUD
screen-effect publisher 0x9A0C90 with a CALL to a cave. The cave checks every gate in native code
and then tail-jumps to the original setter with the original return address and stack arguments.
It substitutes the outgoing float argument(s) only while a fresh command is present:

- vignette (site 0x9A270D, gVignetteVec4.xy): smoothstep start/end radii; native is about (.45, .85),
  and (-.02, -.01) masks the whole scene black (later HUD layers still draw on top);
- greyout (site 0x9A1E38, gCriticalHitPointsVec4.y): whole-screen desaturation 0..1.
183791 remap (static): each site found by its uniform name; every game offset the cave embeds is a
named constant below, checked against aligned 183791 code (publisher fields +0x26000, app members
and ship-component fields moved).

Gates in the cave (any failure passes the native arguments through unchanged): enabled; app
pointer, publisher identity (RDI), attachment handle and ship unchanged; frontend == 0; not the
alternate destination; ship active; planetary, power, follow and landing state zero; now before the
release pilot deadline; a coherent seqlocked command younger than its 100 ms deadline. One change
from the probe: the combat gate is optional (allow_combat), because G-strain matters most in combat;
combat coverage is the first thing to check live.

Storage is never freed: a native thread already inside the cave can always return through it.
"""
import ctypes as C
import math
import struct

from native_roll_patch import Code

SETTER = 0x18780C0
APP_GLOBAL = 0x6F5F868
OWNER_OFFSET = 0x733180  # application + this == publisher identity (RDI): HUD manager app+0x7330E0, +0xA0
ATTACHMENT_OFFSET = 0x72CF48  # application: attachment handle (qword)
FRONTEND_OFFSET = 0x89BD98  # application: frontend state (dword), 0 in game
COMBAT_OFFSET = 0x57AA6D  # application: combat flag (byte)
ALTERNATE_OFFSET = 0xFEC9C  # publisher: alternate upload destination (byte)
SHIP_ACTIVE_OFFSET = 0x60C8  # ship component: active (byte, 1)
SHIP_ZERO_OFFSETS = (0x6730, 0x675C, 0x6430, 0x7318)  # ship component: planetary (byte), power, follow, landing
VR_MODE_GLOBAL = 0x6EE3760  # nonzero: VR display; the seam is qualified for flat play only
SITES = {'vignette': 0x9A273D, 'greyout': 0x9A1E68}
BLOCK = 0x400
DATA_SIZE = 128
COMMAND_MS = 100
NEVER = 0xFFFFFFFFFFFFFFFF
NATIVE_VIGNETTE = (.45, .85)
BLACK_VIGNETTE = (-.02, -.01)


def original_call(rva):
    return b'\xe8' + struct.pack('<i', SETTER - rva - 5)


def code_bytes(cave, base, data, tick, lane, allow_combat=True):
    """The T77 probe cave, byte for byte, except the optional combat gate."""
    c = Code(cave)
    # Entry is an ordinary x64 callee, RSP%16=8. Eight pushes keep that
    # remainder; 0x88 then aligns RSP before calling GetTickCount64.
    c.emit(bytes.fromhex('9c 50 51 52 41 50 41 51 41 52 41 53 48 81 ec 88 00 00 00'))
    for r in range(6):
        c.emit(bytes([0xf3, 0x0f, 0x7f, 0x44+8*r, 0x24, 0x20+16*r]))
    c.relative(bytes.fromhex('48 ff 05'), data+80)  # visits
    c.relative(bytes.fromhex('f3 0f 11 15'), data+88)  # native x (xmm2)
    c.relative(bytes.fromhex('f3 0f 11 1d'), data+92)  # native y (xmm3)

    def reject(prefix, address, suffix=b'', condition=bytes.fromhex('0f 85')):
        c.relative(prefix, address, suffix); c.relative(condition, 'out')
    reject(bytes.fromhex('83 3d'), data, b'\x01')
    c.emit(b'\x48\xb8' + struct.pack('<Q', base+APP_GLOBAL) + b'\x48\x8b\x00')
    reject(bytes.fromhex('48 3b 05'), data+24)
    c.emit(bytes.fromhex('83 b8') + struct.pack('<I', FRONTEND_OFFSET) + b'\x00')  # frontend == 0
    c.relative(bytes.fromhex('0f 85'), 'out')
    if not allow_combat:
        c.emit(bytes.fromhex('80 b8') + struct.pack('<I', COMBAT_OFFSET) + b'\x00')  # combat == 0
        c.relative(bytes.fromhex('0f 85'), 'out')
    reject(bytes.fromhex('48 3b 3d'), data+32)  # RDI publisher identity
    c.emit(bytes.fromhex('80 bf') + struct.pack('<I', ALTERNATE_OFFSET) + b'\x00')  # exclude alternate destination
    c.relative(bytes.fromhex('0f 85'), 'out')
    c.emit(bytes.fromhex('4c 8b 90') + struct.pack('<I', ATTACHMENT_OFFSET))
    reject(bytes.fromhex('4c 3b 15'), data+48)  # same attachment handle
    c.relative(bytes.fromhex('4c 8b 15'), data+40)
    c.emit(bytes.fromhex('41 80 ba') + struct.pack('<I', SHIP_ACTIVE_OFFSET) + b'\x01')  # ship active
    c.relative(bytes.fromhex('0f 85'), 'out')
    for offset in SHIP_ZERO_OFFSETS:
        # planetary, power, follow and landing all require native zero.
        if offset == SHIP_ZERO_OFFSETS[0]: c.emit(b'\x41\x80\xba' + struct.pack('<I', offset) + b'\x00')
        else: c.emit(b'\x41\x83\xba' + struct.pack('<I', offset) + b'\x00')
        c.relative(bytes.fromhex('0f 85'), 'out')
    c.emit(b'\x48\xb8' + struct.pack('<Q', tick) + b'\xff\xd0')
    c.relative(bytes.fromhex('48 89 05'), data+104)
    reject(bytes.fromhex('48 3b 05'), data+16, condition=bytes.fromhex('0f 83'))  # hard stop
    c.relative(bytes.fromhex('4c 8b 15'), data+56)
    c.emit(bytes.fromhex('49 3b 02'))  # now < release pilot deadline
    c.relative(bytes.fromhex('0f 83'), 'out')
    # Coherent command snapshot, including deadline and both floats.
    c.relative(bytes.fromhex('44 8b 15'), data+4)
    c.emit(bytes.fromhex('41 f6 c2 01'))
    c.relative(bytes.fromhex('0f 85'), 'out')
    reject(bytes.fromhex('48 3b 05'), data+8, condition=bytes.fromhex('0f 83'))
    c.relative(bytes.fromhex('4c 8b 1d'), data+64)
    reject(bytes.fromhex('44 3b 15'), data+4)
    if lane == 'vignette':
        c.emit(bytes.fromhex('44 89 5c 24 40 49 c1 eb 20 44 89 5c 24 50'))
    elif lane == 'greyout':
        c.emit(bytes.fromhex('44 89 5c 24 50'))
    else: raise ValueError(lane)
    c.relative(bytes.fromhex('48 ff 05'), data+72)  # applied
    c.label('out')
    for r in range(6):
        c.emit(bytes([0xf3, 0x0f, 0x6f, 0x44+8*r, 0x24, 0x20+16*r]))
    c.emit(bytes.fromhex('48 8d a4 24 88 00 00 00 41 5b 41 5a 41 59 41 58 5a 59 58 9d'))
    # RIP+0 indirect jump preserves all original argument/volatile registers,
    # even when the near allocator places us >2GiB from the original setter.
    c.emit(bytes.fromhex('ff 25 00 00 00 00') + struct.pack('<Q', base+SETTER))
    raw = c.finish()
    if len(raw) >= BLOCK: raise ValueError('cave overlaps its data block')
    return raw


class Lane:
    """One cave plus its data block, in this process. publish() is the single writer."""
    def __init__(self, lane, base, cave, tick, pilot_address, allow_combat=True,
                 memmove=C.memmove, string_at=C.string_at):
        self.lane = lane; self.base = int(base); self.cave = int(cave); self.data = self.cave+BLOCK
        self.rva = SITES[lane]; self.site = self.base+self.rva
        self.original = original_call(self.rva)
        self.patch = b'\xe8' + struct.pack('<i', self.cave-self.site-5)
        self.code = code_bytes(self.cave, self.base, self.data, tick, lane, allow_combat)
        self.memmove = memmove; self.string_at = string_at
        self.sequence = 0; self.identity = None; self.installed = False
        self._write(0, bytes(DATA_SIZE))
        self._write(16, struct.pack('<Q', NEVER))  # no hard stop: the 100 ms command lease is the stop
        self._write(56, struct.pack('<Q', int(pilot_address)))

    def _write(self, offset, raw):
        self.memmove(self.data+offset, raw, len(raw))

    def write_code(self):
        """Cave bytes into the (RWX) allocation, before the call site is switched."""
        self.memmove(self.cave, self.code, len(self.code))
        if self.string_at(self.cave, len(self.code)) != self.code: raise RuntimeError('cave readback mismatch')

    def set_identity(self, app, ship, attachment):
        """Expected app, publisher, ship and attachment; the cave passes native through on any mismatch."""
        identity = (int(app), int(app)+OWNER_OFFSET if app else 0, int(ship), int(attachment))
        if identity != self.identity:
            self.identity = identity
            self._write(24, struct.pack('<QQQQ', identity[0], identity[1], identity[2], identity[3]))

    def publish(self, now_tick, args, enabled):
        """Seqlocked command: two floats valid for COMMAND_MS. enabled False passes native through."""
        a, b = (float(x) for x in args)
        if not (math.isfinite(a) and math.isfinite(b)): raise ValueError('non-finite lane command')
        if self.lane == 'greyout':
            if not 0. <= a <= 1.: raise ValueError('greyout must be 0..1')
        elif not (-1. <= a < b <= 1. and b-a >= .009): raise ValueError('vignette radii must be ordered')
        self.sequence += 2
        self._write(4, struct.pack('<I', self.sequence-1))
        self._write(8, struct.pack('<Q', int(now_tick)+COMMAND_MS))
        self._write(64, struct.pack('<ff', a, b))
        self._write(4, struct.pack('<I', self.sequence))
        self._write(0, struct.pack('<I', int(bool(enabled))))

    def disable(self):
        self._write(0, bytes(4))

    def snapshot(self):
        raw = self.string_at(self.data, 112)
        applied, visits = struct.unpack_from('<QQ', raw, 72)
        return {'applied': applied, 'visits': visits, 'native': struct.unpack_from('<ff', raw, 88)}
