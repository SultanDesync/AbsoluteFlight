"""Terrain look-ahead (Visuals page, default Off), NMS 183877 / Steam 26295378. T107.

The hazards a low pilot meets are rocks and trees (user, 2026-10-25), and they are real, collidable objects
only within 3 regions of the terrain focus (MaxRegionRadius 3 for every collidable rock, tree and cactus
sampled; research/re-log/2026-10-24_claude_terrain-lookahead.md). Three parts, all live:
- look-ahead: the region map update 0x12A4720 gets the planet update's stack copy of the observer matrix.
  Moving its position forward along the ship's velocity moves only the terrain focus (and with it the
  object window), never the camera. Capped at 2 regions, so the ship's own region keeps its objects;
- build speed: more terrain generator and polygonise calls per frame (GcTerrainGlobals);
- fade-in: plants (and maybe rocks) fade in over 0.6-2.25 s (GcEnvironmentGlobals FloraFadeTime);
  shorter makes a new hazard solid sooner.
The terrain range itself stays the game's own: a bigger RegionLODRadius overflows a fixed 1500-region
buffer and crashes at planet load (0.2.1-dev.3 live check).
Each part checks this build's code and data before writing; a mismatch leaves that part alone.
"""
import math
import struct
import threading

TERRAIN_GLOBALS = 0x5005C80   # cGcTerrainGlobals
BUDGETS = 0x264               # NumGeneratorCalls, NumPolygoniseCalls, NumPostPolygoniseCalls (int each)
VANILLA_BUDGETS = (8, 8, 8)
ENVIRONMENT_GLOBALS = 0x5331820
FADE = 0x5C4                  # FloraFadeTimeMax, FloraFadeTimeMin (float each)
VANILLA_FADE = (2.25, 0.6)
MAP_CELL = 0x10               # region map: focus grid cell, int[3]
MAP_FOCUS = 0x20              # region map: focus on the planet surface, planet space, float[3]
OBSERVER_POSITION = 0x30      # observer matrix translation, float[3]
CELL = 32.                    # focus grid step: floor(p/32+0.5)<<5 at 0x12A4994 (taken as the region size)
OBJECT_REGIONS = 3            # MaxRegionRadius of collidable rocks and trees
CAP = (OBJECT_REGIONS - 1)*CELL  # the ship's own region stays inside the object window
MIN_SPEED = 30.               # u/s: no look-ahead below this (landing, hovering)
FADE_SPEED = 60.              # u/s: full look-ahead from here
PLANET_SLOTS = 6

PINS = {
    # SetupRegionMap stores pointers to the three terrain budgets (read live).
    0x16A93EB: bytes.fromhex('837b5000488d05eeca9503488983b03c0000488d05e4ca9503488983b83c0000488d05daca9503488983c03c0000'),
    # Region map update: its observer position read and its 32-unit grid snap. Not its prologue: our own
    # hook patches a jump there (0.2.1-dev.2 live check read that as a mismatch); BUILD_HASH covers it.
    0x12A46FB: bytes.fromhex('410f105f300f5c9bc0340d00'),
    0x12A48A4: bytes.fromhex('f30f10357c5294030f28c1f30f59c60f28f90f11'),
    # Planet update: map update called with the stack observer copy (rdx = rsp+0x80), result unused.
    0x16A220F: bytes.fromhex('4d8bc6488d942480000000488d8f703c0000e80a24c0ff'),
}


def scaled_budgets(scale):
    return tuple(max(b, round(b*scale)) for b in VANILLA_BUDGETS)


def fade_times(longest):
    """(max, min) fade-in seconds: the game's spread scaled so its longest fade is `longest`, never longer."""
    longest = min(VANILLA_FADE[0], max(0., longest))
    return (longest, VANILLA_FADE[1]*longest/VANILLA_FADE[0])


def look_ahead(velocity, lead, cap=CAP):
    """World offset for the terrain focus: velocity*lead, faded in above MIN_SPEED, at most cap long."""
    speed = math.sqrt(sum(v*v for v in velocity))
    if speed <= MIN_SPEED or lead <= 0. or cap <= 0.:
        return None
    fade = min(1., (speed - MIN_SPEED)/(FADE_SPEED - MIN_SPEED))
    distance = min(speed*lead*fade, cap)
    return tuple(v/speed*distance for v in velocity)


class TerrainLookahead:
    def __init__(self, base, read, write, raw_read=None, raw_write=None):
        """read/write: process memory that must fail, never fault. raw_*: the hook's own stack copy."""
        self.base = int(base)
        self.read, self.write = read, write
        self.raw_read = raw_read or read
        self.raw_write = raw_write or write
        self.lock = threading.Lock()
        self.code = None        # pinned code: None not checked, else bool
        self.owned = {}         # address -> (vanilla raw, written raw)
        self.closed = False
        self.enabled = False
        self.lead = 0.
        self.pending = {}       # observer address -> (original raw, written raw), one hook call
        self.applied = 0
        self.last_offset = 0.
        self.cells = {}         # map -> (cell raw, focus, changes, steps)
        self.latest = {'state': 'off'}

    # ---------- worker ----------
    def _own(self, address, vanilla, wanted):
        """Write `wanted` over the game's `vanilla`; 'waiting' before the game loads it, 'unavailable' if
        another mod or build holds something else."""
        if address in self.owned:
            original, written = self.owned[address]
            if written == wanted:
                return 'on'
            if self.read(address, len(written)) != written:
                return 'unavailable'
            self.write(address, wanted)
            self.owned[address] = (original, wanted)
            return 'on'
        current = self.read(address, len(vanilla))
        if not any(current):
            return 'waiting'
        if current != vanilla:
            return 'unavailable'
        self.write(address, wanted)
        self.owned[address] = (vanilla, wanted)
        return 'on'

    def _restore(self):
        for address, (vanilla, written) in list(self.owned.items()):
            try:
                if self.read(address, len(written)) == written:  # keep a newer value someone else wrote
                    self.write(address, vanilla)
                del self.owned[address]
            except BaseException:
                pass

    def update(self, values):
        """Worker poll. Failures switch this feature off and stay local to it."""
        with self.lock:
            if self.closed:
                return self.status()
            enabled = bool(values.get('terrain-lookahead'))
            try:
                if self.code is None:
                    self.code = all(self.read(self.base + rva, len(raw)) == raw for rva, raw in PINS.items())
                if not self.code:
                    self.enabled = False
                    self.latest = {'state': 'unavailable', 'reason': 'code signature mismatch'}
                    return self.status()
                if not enabled:
                    self.enabled = False
                    self._restore()
                    self.latest = {'state': 'off'}
                    return self.status()
                budgets = self._own(self.base + TERRAIN_GLOBALS + BUDGETS, struct.pack('<3i', *VANILLA_BUDGETS),
                                    struct.pack('<3i', *scaled_budgets(float(values.get('terrain-build-speed', 2.)))))
                fade = self._own(self.base + ENVIRONMENT_GLOBALS + FADE, struct.pack('<2f', *VANILLA_FADE),
                                 struct.pack('<2f', *fade_times(float(values.get('terrain-fade', .5)))))
                self.lead = float(values.get('terrain-lead', 1.))
                self.enabled = True
                best = max(self.cells.values(), key=lambda c: c[2], default=None)
                steps = sorted(best[3]) if best and best[3] else []
                self.latest = {'state': 'on', 'budgets': budgets, 'fade': fade, 'cap_m': CAP,
                               'applied': self.applied, 'last_offset_m': round(self.last_offset, 1),
                               'cell_changes': best[2] if best else 0,
                               'cell_step_m': round(steps[len(steps)//2], 1) if steps else None}
            except BaseException as error:
                self.enabled = False
                self._restore()
                self.latest = {'state': 'error', 'reason': type(error).__name__ + ': ' + str(error)}
            return self.status()

    # ---------- region map update hook (game thread) ----------
    def focus_before(self, observer, velocity):
        """Move the observer copy's position forward. velocity None: not piloting, leave it."""
        if not self.enabled or not observer or velocity is None:
            return None
        offset = look_ahead(velocity, self.lead)
        if offset is None:
            return None
        address = observer + OBSERVER_POSITION
        original = self.raw_read(address, 12)
        position = struct.unpack('<3f', original)
        if not all(math.isfinite(p) for p in position):
            return None
        written = struct.pack('<3f', *(p + o for p, o in zip(position, offset)))
        self.raw_write(address, written)
        self.pending[observer] = (original, written)
        self.applied += 1
        self.last_offset = math.sqrt(sum(o*o for o in offset))
        return offset

    def focus_after(self, region_map, observer):
        """Put the observer copy back, and count focus grid changes (live check of the cell size)."""
        pending = self.pending.pop(observer, None)
        if pending is not None and self.raw_read(observer + OBSERVER_POSITION, 12) == pending[1]:
            self.raw_write(observer + OBSERVER_POSITION, pending[0])
        if self.enabled and region_map:
            cell = self.raw_read(region_map + MAP_CELL, 12)
            focus = struct.unpack('<3f', self.raw_read(region_map + MAP_FOCUS, 12))
            before = self.cells.get(region_map)
            if before is None:
                if len(self.cells) < PLANET_SLOTS:
                    self.cells[region_map] = (cell, focus, 0, [])
            elif before[0] != cell:
                step = math.sqrt(sum((a - b)**2 for a, b in zip(focus, before[1])))
                steps = (before[3] + [step])[-64:]
                self.cells[region_map] = (cell, focus, before[2] + 1, steps)

    def fail(self, error):
        self.enabled = False
        self.latest = {'state': 'error', 'reason': 'hook: ' + type(error).__name__ + ': ' + str(error)}

    def restore(self):
        """Shutdown: the game's budgets and fade times back."""
        with self.lock:
            self.closed = True
            self.enabled = False
            self._restore()
            self.latest = {'state': 'off'}
            return {'remaining': len(self.owned)}

    def status(self):
        return dict(self.latest)
