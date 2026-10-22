"""Native tunnel vignette and greyout lanes for the G-load mechanic (T77 seam), build 180836 / Steam 25732212.

Ported from codex's T77 probe (research/re-log/2026-10-16_codex_native-vignette.md; scratch job
research-jobs/t77/native_probe.py, 36/36 local x64 harness checks and a user-accepted live proof).
Each lane replaces one direct CALL to the native uniform setter 0x1841310 inside the HUD
screen-effect publisher 0x990EB0 with a CALL to a cave. The cave checks every gate in native code
and then tail-jumps to the original setter with the original return address and stack arguments.
It substitutes the outgoing float argument(s) only while a fresh command is present:

- vignette (site 0x99287C, gVignetteVec4.xy): smoothstep start/end radii; native is about (.45, .85),
  and (-.02, -.01) masks the whole scene black (later HUD layers still draw on top);
- greyout (site 0x991F74, gCriticalHitPointsVec4.y): whole-screen desaturation 0..1.

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

SETTER = 0x1841310
APP_GLOBAL = 0x6E8D708
OWNER_OFFSET = 0x7329C0  # application + this == publisher identity (RDI)
ATTACHMENT_OFFSET = 0x72C858
VR_MODE_GLOBAL = 0x6E11660  # nonzero: VR display; the seam is qualified for flat play only
SITES = {'vignette': 0x99287C, 'greyout': 0x991F74}
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
    c.emit(bytes.fromhex('83 b8 98 54 87 00 00'))  # frontend == 0
    c.relative(bytes.fromhex('0f 85'), 'out')
    if not allow_combat:
        c.emit(bytes.fromhex('80 b8 0d a4 57 00 00'))  # combat == 0
        c.relative(bytes.fromhex('0f 85'), 'out')
    reject(bytes.fromhex('48 3b 3d'), data+32)  # RDI publisher identity
    c.emit(bytes.fromhex('80 bf 9c 8c 0d 00 00'))  # exclude alternate destination
    c.relative(bytes.fromhex('0f 85'), 'out')
    c.emit(bytes.fromhex('4c 8b 90 58 c8 72 00'))
    reject(bytes.fromhex('4c 3b 15'), data+48)  # same attachment handle
    c.relative(bytes.fromhex('4c 8b 15'), data+40)
    c.emit(bytes.fromhex('41 80 ba c8 60 00 00 01'))  # ship active
    c.relative(bytes.fromhex('0f 85'), 'out')
    for offset in (0x6720, 0x674c, 0x6420, 0x7308):
        # planetary, power, follow and landing all require native zero.
        if offset == 0x6720: c.emit(b'\x41\x80\xba' + struct.pack('<I', offset) + b'\x00')
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
