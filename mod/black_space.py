"""Black space (Visuals page, default Off), NMS 183791 / Steam 26271400.

Turns the space nebula, space fog and the space colour palettes to black, so space is black and the
stars, suns and planets stay as they are. Data writes only: no code patches or hooks.

How the game builds a system's space sky (static, research/re-log/2026-10-23_claude_black-space.md):
the generator at NMS.exe+0x1682F00 copies cGcSkyGlobals.SpaceSkyMin into the solar system's sky
properties (system+0x2020), draws each nebula and fog value between SpaceSkyMin and SpaceSkyMax from
the system seed, and picks a colour palette: common or rare (system+0x2044) and an entry (+0x2040).
The palettes are spaceskycolours.mbin and spacerareskycolours.mbin as loaded, two list pointers at
generator+0x6C0, and hold the sky's colours (top, middle, bottom, fog, nebula). So three parts:
- sky: SpaceSkyMax/Min nebula and fog values, for every system generated from now on;
- palettes: every palette entry's colours except the system light colour (ships stay lit);
- system: the current system's nebula and fog values, so the nebula goes without a warp.
Each part is checked against this build's shipped data before anything is written (another build or
another sky mod leaves that part alone). Off and shutdown put back every value still ours.
183791 (static, 2026-10-28): cGcSkyGlobals, cGcSpaceSkyProperties, cGcSolarSystemData and
cGcSolarSystemSkyColourData keep their 180836 reflection GUIDs and sizes; the 7.10 globals and palette
MBINs still match VANILLA, SYSTEM_TAIL and PALETTES. Only the app and system member offsets moved.
"""
import hashlib
import struct
import threading

SKY_GLOBALS = 0x53031A0          # NMS.exe RVA of cGcSkyGlobals (generator reads SpaceSkyMin at +0x19A0, 183791)
BLOCKS = {'SpaceSkyMax': 0x1900, 'SpaceSkyMin': 0x19A0}  # cGcSpaceSkyProperties, 0xA0 each
BLOCK_SIZE = 0xA0
# Turned to 0. The two fog colour strengths already ship at 0.
FIELDS = {
    'NebulaBrightness': 0x3C, 'NebulaCloudStrength': 0x40, 'NebulaCloudStrength1': 0x44,
    'NebulaFogAmount': 0x54, 'NebulaTendrilStrength': 0x68,
    'SpaceFogColour2Strength': 0x7C, 'SpaceFogColourStrength': 0x80, 'SpaceFogMax': 0x84, 'SpaceFogStrength': 0x8C,
}
BLACK = struct.pack('<f', 0.)
# SpaceSkyMax then SpaceSkyMin as loaded from gcskyglobals.globals.mbin (the struct sits 0x20 into the file).
VANILLA = bytes.fromhex(
    '0000803f0000803f0000803f0000803f6891ed3e295ccf3e31082c3e0000803f0000000000000000'
    '295c8f3e000020400000003f0000803f0000803f00004040cdcc8c3fcdcc0c3fcdcc4c3e0000803f'
    '0000803f0000003f000020403333333f0000c8426666663f0000904000000040a4707d3f0000803f'
    '0000204000000000000000003333333f9a99193f0ad7233d85eb513f0ad7a33b9a99193f00000000'
    'c9761e3fac1c5a3f3f357e3f0000803f48e1ba3e0ad7033fa4703d3f0000803f0000000000000000'
    '295c8f3e000020400000803e0000803f0000803f0000803f00000000cdcc0c3f0ad7233c0000803e'
    '0000803f000000000000e03f0000003f000000003333333f000000000000803ecdcc4c3e0000803f'
    '0000204100000000000000003333333f9a99193f0ad7233d85eb513f0ad7a33b9a99193f00000000')
assert len(VANILLA) == 2 * BLOCK_SIZE

# Chain to the generator and the current system, every link in the pinned code below.
APP_GLOBAL = 0x6F60828      # cGcApplication::Data (same global as ai_pilots)
SYSTEM_FROM_APP = 0x72B610  # current solar system
GENERATOR_FROM_SYSTEM = 0x522390
PALETTE_LISTS = 0x6C0       # generator+0x6C0: common list, +0x6C8: rare list; list = {data ptr, count}
SYSTEM_SKY = 0x2020         # system sky properties (cGcSpaceSkyProperties copy)
SYSTEM_SEED = 0x2480        # 16 bytes; a new seed means a new system was generated
ENTRY_SIZE = 0xE0           # cGcSolarSystemSkyColourData
# Colour slots darkened (RGB; alpha kept): bottom, bottom (planet), cloud, fog, fog 2, mid, mid (planet),
# nebula 1-3, top, top (planet). LightColour (0x50) stays: it lights ships and stations.
PALETTE_SLOTS = (0x00, 0x10, 0x20, 0x30, 0x40, 0x60, 0x70, 0x80, 0x90, 0xA0, 0xB0, 0xC0)
RGB_BLACK = bytes(12)
# Entry count and SHA256 of the entries as shipped (the files' data after their 0x30-byte header).
PALETTES = (('common', 10, 'afef1e3b3f29bdd77075577170ce732e25c32ac1e6932dcab9836da000a862f0'),
            ('rare', 48, 'b3ae7c524c5e7f1816166ff6ea055dadff9ca977b557d7cd3821001a8f4ec6ae'))
# StarVisibility, SunSize, SunStrength, pad: copied from SpaceSkyMin as is, so a real system sky has them.
SYSTEM_TAIL = bytes.fromhex('85eb513f0ad7a33b9a99193f00000000')
PINS = {
    # mov rax,[NMS+0x6F60828]; ... mov rcx,[rax+0x72B610]; ... seed to rcx+0x2480; add rcx,0x522390;
    # call the sky generator 0x1682F00.
    0x1B535A: bytes.fromhex('488b05c7b4da060f104580488b8810b6720048894da848894db0488d81e02500000f1181802400004881c190235200488945b8e86edb4c01'),
    # Generator: system sky properties at system+0x2020.
    0x1682F9A: bytes.fromhex('498b0b4881c120200000'),
    # Generator: palette list = [generator + set*8 + 0x6C0], entry count at list+8.
    0x16830EC: bytes.fromhex('4863824420000049c1eb20498b8cc1c006000048634108480fafc748c1e820'),
}


def _field_addresses(globals_address):
    """(label, address) for every globals field written, SpaceSkyMax first."""
    return [(f'{block}.{name}', globals_address + block_offset + offset)
            for block, block_offset in BLOCKS.items() for name, offset in FIELDS.items()]


def _pointer(value):
    return 0x10000 <= value < 0x7FFFFFFFFFFF


class BlackSpace:
    def __init__(self, base, read, write):
        """read(address, size) -> bytes and write(address, data) must fail, never fault."""
        self.base = int(base)
        self.address = self.base + SKY_GLOBALS
        self.read, self.write = read, write
        self.lock = threading.Lock()
        self.verified = None  # sky globals: None not loaded yet, False not this build's values
        self.code = None      # pinned chain code
        self.palettes = None  # None not loaded yet, False not the shipped palettes, else entry addresses
        self.system = None    # (system address, seed) the system part was written to
        self.closed = False
        self.owned = {}  # address -> (vanilla raw, written raw)
        self.parts = {}
        self.latest = {'state': 'off'}

    def _q(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def _verify(self):
        data = self.read(self.address + BLOCKS['SpaceSkyMax'], 2 * BLOCK_SIZE)
        if not any(data):
            return None  # globals not loaded yet
        return data == VANILLA

    def _own(self, address, data):
        if address in self.owned:
            return
        vanilla = self.read(address, len(data))
        self.write(address, data)
        self.owned[address] = (vanilla, data)

    def _system_address(self):
        system = self._q(self._q(self.base + APP_GLOBAL) + SYSTEM_FROM_APP)
        if not _pointer(system):
            raise ValueError('no current system')
        return system

    def _find_palettes(self):
        """Entry addresses of both palettes, None while not loaded, False if they are not the shipped ones."""
        generator = self._system_address() + GENERATOR_FROM_SYSTEM
        entries = []
        for i, (_, count, digest) in enumerate(PALETTES):
            palette = self._q(generator + PALETTE_LISTS + 8 * i)
            if not _pointer(palette):
                return None
            data = self._q(palette)
            loaded = struct.unpack('<i', self.read(palette + 8, 4))[0]
            if not _pointer(data) or loaded <= 0:
                return None
            if loaded != count or hashlib.sha256(self.read(data, count * ENTRY_SIZE)).hexdigest() != digest:
                return False
            entries += [data + n * ENTRY_SIZE for n in range(count)]
        return entries

    def _apply_sky(self):
        if not self.verified:  # checked every poll until it matches: at game start the globals may still be loading
            self.verified = self._verify()
        if self.verified is None:
            return 'waiting'
        if not self.verified:
            return 'unavailable'
        for _, address in _field_addresses(self.address):
            self._own(address, BLACK)
        return 'on'

    def _apply_palettes(self):
        if not self.palettes:
            self.palettes = self._find_palettes()
        if self.palettes is None:
            return 'waiting'
        if self.palettes is False:
            return 'unavailable'
        for entry in self.palettes:
            for slot in PALETTE_SLOTS:
                self._own(entry + slot, RGB_BLACK)
        return 'on'

    def _apply_system(self):
        """The current system's nebula and fog. A new system (new seed) was generated from the darkened
        globals already, so the old system's values are dropped, never written into it."""
        system = self._system_address()
        seed = self.read(system + SYSTEM_SEED, 16)
        if self.system and self.system != (system, seed):
            old = self.system[0] + SYSTEM_SKY
            for address in [a for a in self.owned if old <= a < old + BLOCK_SIZE]:
                del self.owned[address]
            self.system = None
        sky = system + SYSTEM_SKY
        if self.read(sky + 0x90, 16) != SYSTEM_TAIL or self.read(sky + 0x24, 4) not in (bytes(4), b'\1\0\0\0'):
            return 'unavailable'
        low, high = VANILLA[BLOCK_SIZE:], VANILLA[:BLOCK_SIZE]
        for offset in FIELDS.values():
            value = struct.unpack('<f', self.read(sky + offset, 4))[0]
            bounds = sorted(struct.unpack_from('<f', block, offset)[0] for block in (low, high))
            if value != 0. and not bounds[0] - 1e-4 <= value <= bounds[1] + 1e-4:
                return 'unavailable'
        for offset in FIELDS.values():
            self._own(sky + offset, BLACK)
        self.system = (system, seed)
        return 'on'

    def _restore(self):
        restored = 0
        for address, (vanilla, written) in list(self.owned.items()):
            try:
                if self.read(address, len(written)) == written:  # keep a newer native or third-party value
                    self.write(address, vanilla)
                restored += 1
                del self.owned[address]
            except BaseException:
                pass
        self.system = None
        return restored

    def update(self, enabled):
        """Worker poll: darken, keep, or put the game's values back. Failures stay local to this feature."""
        with self.lock:
            if self.closed:
                return self.status()
            try:
                if not enabled:
                    if self.owned:
                        self._restore()
                    self.latest = {'state': 'off'} if not self.owned else {'state': 'error', 'reason': 'restore incomplete'}
                    return self.status()
                parts = {'sky': self._apply_sky()}
                if self.code is None:
                    self.code = all(self.read(self.base + rva, len(raw)) == raw for rva, raw in PINS.items())
                if self.code:
                    for name, apply in (('palettes', self._apply_palettes), ('system', self._apply_system)):
                        try:
                            parts[name] = apply()
                        except (OSError, ValueError):
                            parts[name] = 'waiting'  # no system loaded yet (main menu, loading)
                else:
                    parts.update(palettes='unavailable', system='unavailable')
                states = set(parts.values())
                state = ('on' if 'on' in states else 'waiting' if 'waiting' in states else 'unavailable')
                self.latest = {'state': state, **parts, 'fields': len(self.owned)}
            except BaseException as error:
                self._restore()
                self.latest = {'state': 'error', 'reason': type(error).__name__ + ': ' + str(error)}
            return self.status()

    def restore(self):
        """Shutdown: put the game's values back and stop."""
        with self.lock:
            self.closed = True
            restored = self._restore()
            self.latest = {'state': 'off'}
            return {'restored': restored, 'remaining': len(self.owned)}

    def status(self):
        return dict(self.latest)
