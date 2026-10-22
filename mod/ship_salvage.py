"""Landed wreck: at the eject from a ditched or crashed ship, the game respawns it landed where it lies,
essentials damaged. Without this the ejected ship stays in flight mode, so boarding it again needs no launch and
the broken launch thruster never stops it (user 2026-10-21, logs 20261021T0202/0226Z: re-boarded 10 s after each
ditch eject and flew on; 20261021T0303Z: the same after a disabling crash).

Build 180836 / Steam 25732212 (research/re-log/2026-10-20_claude_ditch-salvage.md):
- Native landed respawn 0x5AFFB0 (per frame, from 0x5AFCC0 on game state +0xAAD0): when dword
  player_state+0xA8BD4 == 1 it SpawnNewShip(ownership, stored matrix, 1, -1, 1)s the primary ship, sets
  ship+0x7428 = 1 (the crash smoke is re-evaluated, research 2026-10-20 wrecked-ship-state) and clears the flag.
  The matrix is 4 rows at ownership+0x6CB0 (0x565250 stores it, 0x5B7630 sets the flag; native caller 0x8EA6F0
  ejects the player first, then sets both). CONFIRMED static; state 1 = landed LIKELY.
- 5-row matrices are (right, up, at, local position, sector) with sector = floor(p/1024)*1024 (0x5AFFB0 splits
  the stored position that way), so the stored position is the whole world position. CONFIRMED arithmetic.
- Ship matrix: the rigid body state [[ship+0x6248]+0x60+0x290]: axes +0x10/+0x20/+0x30, position halves
  +0x40/+0x50 (planetary_hud.BodyReader, T86). CONFIRMED.
All code bytes are pinned; any mismatch leaves this off (the old path: damage on boarding).
"""
import ctypes as C
import math
import struct

import ship_damage

PINS = {  # rva -> bytes at that rva (exe on disk, build 180836)
    0x5AFFB0: ('488bc4534881ecf000000083b9d48b0a0001488bd90f855502000048897808488b3d32d78d060f2970e80f2978d8660f6f3d'
               '5a1d5804440f2940c8440f108710cc0d00440f2948b866440f6f0d7f225804440f2950a8440f109740cc0d00440f295898'
               '440f109f30cc0d00410f59fa440f296088440f10a720cc0d00'),
    0x5B017C: ('488d8f605f0d00c64424200141b9ffffffff488d54243041b801000000e8d2abfaff488b0d63d58d06baffffffff4881c1'
               '605f0d00e8faddfaff4885c07459488bc8c6802874000001e8064c1b01c783d48b0a000000000084c07507c683'),
    0x565250: '0f28020f284a100f1181b06c00000f2842200f1189c06c00000f284a300f1181d06c00000f1189e06c0000c3',
    0x5B7630: 'c781d48b0a0001000000c3',
}
OWNERSHIP = 0xD5F60          # app+: cGcPlayerShipOwnership
RESPAWN_MATRIX = 0x6CB0      # ownership+: 4 rows read by 0x5AFFB0
RESPAWN_FLAG = 0xA8BD4       # player state+: 1 = respawn the primary ship landed at RESPAWN_MATRIX
SHIP_PHYSICS, BODY, BODY_STATE, STATE_ROWS = 0x6248, 0x60, 0x290, 0x10
PLACE_TIMEOUT = 5.           # s for the native respawn to take the flag


def _q(read, address):return struct.unpack('<Q', read(address, 8))[0]


def stored_rows(raw):
    """The 4-row respawn matrix from the body's 5 rows (3 axes, position low, position high), or None."""
    if len(raw) != 20 or not all(math.isfinite(v) for v in raw):return None
    if any(abs(math.sqrt(sum(v*v for v in raw[4*i:4*i+3]))-1.) > .1 for i in range(3)):return None
    position = [raw[12+i]+raw[16+i] for i in range(3)]
    if sum(v*v for v in position) < 1.:return None
    return list(raw[:12])+position+[1.]


def ship_rows(read, ship):
    """The ship's own 4-row matrix from its rigid body, or None."""
    if not ship:return None
    physics = _q(read, ship+SHIP_PHYSICS)
    if physics < 0x10000:return None
    state = _q(read, physics+BODY+BODY_STATE)
    if state < 0x10000:return None
    return stored_rows(list(struct.unpack('<20f', read(state+STATE_ROWS, 80))))


class Wreck:
    """One ditched ship to put down landed. place(now) once, right after the eject; step(now) then returns
    'landed', 'failed' or None while the game has not taken the flag yet."""

    def __init__(self, base, ship, rows, read=C.string_at, write=ship_damage.memory_write):
        self.base = int(base); self.ship = ship; self.rows = rows; self.read = read; self.write = write
        if rows is None or len(rows) != 16:raise ValueError('no ship matrix')
        for rva, expected in PINS.items():
            if read(self.base+rva, len(expected)//2).hex() != expected:raise RuntimeError(f'native bytes changed at {rva:#x}')
        self.state = 'new'; self.since = 0.; self.damaged = []

    def _app(self):
        return _q(self.read, self.base+ship_damage.APP_GLOBAL)

    def place(self, now):
        app = self._app()
        if not app:raise RuntimeError('no application')
        self.damaged = ship_damage.damage_essentials(self.read, self.write, self.base, ship=self.ship)
        self.write(app+OWNERSHIP+RESPAWN_MATRIX, struct.pack('<16f', *self.rows))
        self.write(app+ship_damage.PLAYER_STATE+RESPAWN_FLAG, struct.pack('<I', 1))  # after the matrix
        self.state = 'placing'; self.since = now

    def step(self, now):
        if self.state != 'placing':return None
        app = self._app()
        flag = app+ship_damage.PLAYER_STATE+RESPAWN_FLAG
        if app and struct.unpack('<I', self.read(flag, 4))[0] == 0:
            self.state = 'landed';return 'landed'
        if now-self.since < PLACE_TIMEOUT:return None
        if app:self.write(flag, struct.pack('<I', 0))
        self.state = 'failed';return 'failed'
