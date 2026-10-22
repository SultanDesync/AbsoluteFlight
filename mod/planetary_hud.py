"""T87 planetary HUD: native-flight velocity source and planetary layer, NMS 180836 / Steam 25732212.

Two read-only sources:
- the Newtonian final cave's published record (tools/native_newtonian.py). The cave captures the
  rigid-body state before any admission test and publishes a record every flight invocation, so it
  carries velocity, ship axes and world position in native fallback too (low height, combat,
  landing). The HUD no longer depends on Sultan being armed;
- the solar system's planet array (T86 R3, docs/PLANETARY-FLIGHT.md 6.1), read through
  ReadProcessMemory on our own process, so a planet freed during a warp cannot crash the game.

The planetary layer shows altitude above sea level, vertical speed and ATM/VAC near a planet,
plus a mode tag saying who flies the ship. User 2026-10-19: no pitch ladder or horizon (the vanilla
atmospheric HUD has one); the prograde marker serves as the flight-path marker. ship+0x671C is
height above the base sphere, not radar, so no radar altitude is shown (no terrain source yet).
"""
import ctypes as C
import math
import struct

# Bridge allocation layout (tools/native_newtonian.py: RING, SLOT, CAPACITY; latest sequence +136).
RING, SLOT, CAPACITY, LATEST = 0x1000, 0x800, 1024, 136
WORK = 16  # work snapshot offset inside a ring slot
# Work snapshot fields used here (decode_record in tools/native_newtonian.py).
W_SHIP, W_TICK, W_REASON, W_APPLIED = 16, 48, 60, 64
W_VELOCITY, W_RIGHT, W_UP, W_FORWARD = 112, 144, 160, 176
W_CONTEXT_ID, W_LANDING, W_HEIGHT_MODE = 0x110, 0x13C, 0x168
W_POSITION_LOW, W_POSITION_HIGH = 0x2E0, 0x2F0  # S+0x40 / S+0x50 split world position
REASON_IDENTITY = 3  # identity gates failed: this frame's state was not captured
REASON_APPLIED = 9  # the helper's acceleration was written this frame

# T86 R3: M=[module+0x6E8D708], solar=[M+0x72AFB0], planets at solar+0x2E30, stride 0xD9170.
APP_GLOBAL, SOLAR = 0x6E8D708, 0x72AFB0
PLANET_ARRAY, PLANET_STRIDE, PLANET_COUNT = 0x2E30, 0xD9170, 0x2544
P_CENTRE, P_RADIUS, P_SEA, P_ATMOSPHERE = 0xD74D0, 0x3CA0, 0x31D8, 0x1E90
MAX_PLANETS = 6
PLANET_REFRESH_MS = 250

# Layer fade by height above the nearest base sphere (GUESS, tune live). The native low-height
# gate flipped near 5000 on Udermin; the layer is fully up well before that.
LAYER_FULL, LAYER_TOP = 12000., 20000.
MIN_VS = 1.  # u/s; below this the trend arrow is hidden


def _finite(values):return all(math.isfinite(v) for v in values)


def _dot(a, b):return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def motion_from_slot(raw):
    """Motion dict from one ring slot, or None (torn, empty, identity failure or not primary context)."""
    if len(raw) != SLOT:return None
    seq = struct.unpack_from('<Q', raw, 0)[0]
    if seq == 0 or seq & 1 or seq != struct.unpack_from('<Q', raw, SLOT-8)[0]:return None
    def f3(offset):return struct.unpack_from('<3f', raw, WORK+offset)
    reason = struct.unpack_from('<I', raw, WORK+W_REASON)[0]
    if reason == REASON_IDENTITY or struct.unpack_from('<Q', raw, WORK+W_CONTEXT_ID)[0] != 1:return None
    velocity = f3(W_VELOCITY); basis = (f3(W_RIGHT), f3(W_UP), f3(W_FORWARD))
    low, high = f3(W_POSITION_LOW), f3(W_POSITION_HIGH)
    position = tuple(a+b for a, b in zip(low, high))
    if not _finite(velocity+basis[0]+basis[1]+basis[2]+position):return None
    if any(abs(_dot(a, a)-1) > .02 for a in basis):return None
    return {'sequence': seq//2, 'ship': struct.unpack_from('<Q', raw, WORK+W_SHIP)[0],
            'tick': struct.unpack_from('<Q', raw, WORK+W_TICK)[0], 'reason': reason,
            'applied': bool(struct.unpack_from('<I', raw, WORK+W_APPLIED)[0]),
            'landing': struct.unpack_from('<I', raw, WORK+W_LANDING)[0],
            'low_height': bool(raw[WORK+W_HEIGHT_MODE]),
            'velocity': velocity, 'basis': basis, 'position': position}


class MotionReader:
    """Latest published record from the bridge's own allocation, which is never freed."""
    def __init__(self, read=C.string_at):
        self.read = read

    def latest(self, data):
        if not data:return None
        seq = int.from_bytes(self.read(data+LATEST, 8), 'little')
        if not seq:return None
        motion = motion_from_slot(self.read(data+RING+(seq & (CAPACITY-1))*SLOT, SLOT))
        return motion if motion is not None and motion['sequence'] == seq else None


def owner(motion):
    """'SULTAN' or 'NATIVE' with the native reason the pilot can act on."""
    if motion['applied']:return 'SULTAN'  # set for applied and zero (coast) commands alike
    if motion['landing']:return 'NATIVE  LANDING'
    if motion['low_height']:return 'NATIVE  LOW HEIGHT'
    return 'NATIVE'


def process_read(address, size, _buffer={}):
    """ReadProcessMemory on our own process: raises OSError instead of faulting on freed memory."""
    k = C.windll.kernel32
    if not _buffer:
        k.ReadProcessMemory.argtypes = (C.c_void_p, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t))
        k.ReadProcessMemory.restype = C.c_int; k.GetCurrentProcess.restype = C.c_void_p
        _buffer['process'] = k.GetCurrentProcess()
    out = C.create_string_buffer(size); n = C.c_size_t()
    if not k.ReadProcessMemory(_buffer['process'], C.c_void_p(address), out, size, C.byref(n)) or n.value != size:
        raise OSError(f'read {address:#x}+{size} failed')
    return out.raw


# The rigid body the final cave snapshots (its identity chain): B=[ship+0x6248]+0x60, S=[B+0x290],
# C=[B+0x328]; context id C+0xA38 (1 = primary). S+0x10/20/30 axes, S+0x40/50 position halves, S+0x60 velocity.
SHIP_PHYSICS, BODY, BODY_STATE, BODY_CONTEXT, CONTEXT_ID = 0x6248, 0x60, 0x290, 0x328, 0xA38
S_AXES, S_POSITION, S_VELOCITY = 0x10, 0x40, 0x60
SHIP_LOW_HEIGHT, SHIP_LANDING = 0x6720, 0x7308


class BodyReader:
    """Direct read of the same rigid-body fields, for frames whose cave record is unusable: after a
    landing the body moves and records report identity_changed until Sultan requalifies in space."""
    def __init__(self, read=process_read):
        self.read = read

    def _q(self, address):return int.from_bytes(self.read(address, 8), 'little')

    def motion(self, ship, now):
        """Motion dict like motion_from_slot's (never Sultan-applied), or None."""
        if ship < 0x10000:return None
        try:
            physics = self._q(ship+SHIP_PHYSICS)
            if physics < 0x10000:return None
            body = physics+BODY; state = self._q(body+BODY_STATE); context = self._q(body+BODY_CONTEXT)
            if state < 0x10000 or context < 0x10000 or self._q(context+CONTEXT_ID) != 1:return None
            raw = self.read(state+S_AXES, 0x60)  # axes, position halves, velocity: one read
            flags = self.read(ship+SHIP_LOW_HEIGHT, 1)[0]; landing = int.from_bytes(self.read(ship+SHIP_LANDING, 4), 'little')
        except OSError:
            return None
        basis = tuple(struct.unpack_from('<3f', raw, 16*i) for i in range(3))
        low, high = struct.unpack_from('<3f', raw, S_POSITION-S_AXES), struct.unpack_from('<3f', raw, S_POSITION-S_AXES+16)
        velocity = struct.unpack_from('<3f', raw, S_VELOCITY-S_AXES)
        position = tuple(a+b for a, b in zip(low, high))
        if not _finite(velocity+basis[0]+basis[1]+basis[2]+position):return None
        if any(abs(_dot(a, a)-1) > .02 for a in basis):return None
        return {'sequence': None, 'ship': ship, 'tick': now, 'reason': None, 'applied': False, 'landing': landing,
                'low_height': bool(flags), 'velocity': velocity, 'basis': basis, 'position': position}


class PlanetReader:
    """Planet centres, radii, sea levels and atmosphere flags of the current system (T86 R3/R4)."""
    def __init__(self, base, read=process_read):
        self.base = int(base); self.read = read

    def _q(self, address):return int.from_bytes(self.read(address, 8), 'little')

    def planets(self):
        """[{'centre','radius','sea','atmosphere'}], or [] when there is no valid system."""
        try:
            manager = self._q(self.base+APP_GLOBAL)
            if manager < 0x10000:return []
            solar = self._q(manager+SOLAR)
            if solar < 0x10000:return []
            count = int.from_bytes(self.read(solar+PLANET_COUNT, 4), 'little', signed=True)
            if not 1 <= count <= MAX_PLANETS:return []
            out = []
            for i in range(count):
                p = solar+PLANET_ARRAY+i*PLANET_STRIDE
                centre = struct.unpack('<3f', self.read(p+P_CENTRE, 12))
                radius, = struct.unpack('<f', self.read(p+P_RADIUS, 4)); sea, = struct.unpack('<f', self.read(p+P_SEA, 4))
                atmosphere = int.from_bytes(self.read(p+P_ATMOSPHERE, 4), 'little', signed=True)
                if not _finite(centre+(radius, sea)) or not 0 < radius < 1e7 or abs(sea) >= radius:continue
                out.append({'index': i, 'centre': centre, 'radius': radius, 'sea': sea,
                            'atmosphere': atmosphere if atmosphere in (0, 1) else None})
            if self._q(manager+SOLAR) != solar:return []  # system changed while reading
            return out
        except OSError:
            return []


def planet_frame(position, planets):
    """Nearest planet by height above its base sphere (the native 0x2C4710 rule), or None."""
    best = None
    for p in planets:
        d = tuple(a-b for a, b in zip(position, p['centre']))
        r = math.sqrt(_dot(d, d))
        if r < 1.:continue
        h = r-p['radius']
        if best is None or h < best['height_base']:
            best = {'index': p['index'], 'height_base': h, 'altitude': h-p['sea'],
                    'up': tuple(v/r for v in d), 'atmosphere': p['atmosphere']}
    return best


def layer_alpha(height_base, full=LAYER_FULL, top=LAYER_TOP):
    """1 below full, 0 above top, smoothstep between, so the layer never pops in."""
    if height_base <= full:return 1.
    if height_base >= top:return 0.
    u = (top-height_base)/(top-full)
    return u*u*(3.-2.*u)


def planetary_layout(motion, frame, values, trim=0., ground=None):
    """Planetary layer plan, or None when it is off, out of range or has nothing to show. ground (T101) is the
    game's height above ground or sea while the gear is down (hover mode), else None."""
    if frame is None or not values.get('hud-planetary', True):return None
    alpha = layer_alpha(frame['height_base'])
    if alpha <= .01:return None
    vs = _dot(motion['velocity'], frame['up'])
    atmosphere = {1: 'ATM', 0: 'VAC'}.get(frame['atmosphere'])
    out = {'alpha': alpha,
           'altitude': frame['altitude'] if values.get('hud-altitude', True) else None,
           'vertical': vs if values.get('hud-vertical-speed', True) else None,
           'atmosphere': atmosphere if values.get('hud-atmosphere', True) else None,
           'trim': trim if trim and abs(trim) >= .005 and values.get('hud-trim', True) else None,
           'ground': ground if ground is not None and math.isfinite(ground) else None}
    if out['altitude'] is None and out['vertical'] is None and out['atmosphere'] is None:return None
    return out


def mode_tag(motion, layer, values):
    """Who flies the ship: shown with the planetary layer up, or whenever native owns the ship."""
    if motion is None or not values.get('hud-mode-tag', True):return None
    text = owner(motion)
    if layer is None and text == 'SULTAN':return None
    return text


def condition_tag(condition, values):
    """T93: replaces the mode tag while the engines are out, with the restart countdown."""
    if condition is None or not condition.engines_out or not values.get('hud-mode-tag', True):return None
    remaining = condition.remaining()
    return 'ENGINES OUT' if remaining is None else f'ENGINES OUT  {math.ceil(remaining)}s'


PURGE_LINES = ('THRUSTERS FLOODED - EMERGENCY PURGE IN PROCESS', 'HULL OVERPRESSURE - INTEGRITY FAILING')
WRECK_LINES = ('HULL FAILED - SHIP WRECKED', 'HOLD EJECT TO ABANDON SHIP')
PURGE_AHEAD = 3.  # s of band shown ahead of now
PURGE_BEHIND = 2.  # s of input trail shown behind now


def purge_plan(condition):
    """T93: the warning block, the stage's band graph and the purge / hull bars while ditched, or None.
    Graph times are seconds from now (negative behind); values are the stage input, 0..1."""
    if condition is None or not condition.engines_out:return None
    if condition.wrecked:return {'lines': WRECK_LINES, 'wrecked': True, 'hull': 0.}
    purge = condition.purge
    if purge is None:return None
    stage = purge.stage; now = purge.elapsed
    ahead = [(t, low, high) for t, low, high in purge.preview(PURGE_AHEAD, .1)
             if t <= max(.1, purge.countdown+stage.required-purge.done+.05)]  # this stage only: the next one uses another control
    trail = [(e-now, v, inside) for e, v, inside in purge.history if e >= now-PURGE_BEHIND]
    return {'lines': PURGE_LINES, 'wrecked': False, 'stage': stage.name, 'prompt': purge.prompt(),
            'step': (purge.index+1, len(purge.stages)), 'band': purge.band(), 'ahead': ahead, 'trail': trail,
            'value': trail[-1][1] if trail else None, 'inside': bool(trail and trail[-1][2]),
            'progress': purge.progress(), 'hull': purge.hull, 'warning': purge.warning, 'countdown': purge.countdown, 'count': purge.count(),
            'arrow': purge.arrow()}


def format_altitude(h):
    """Whole units below 10 km, km with one decimal above; never '-0'."""
    if abs(h) < 10000:
        n = round(h);return f"{n if n else 0}"
    return f"{h/1000:.1f}k"
