"""T95 native side of the ship alarms (mod/ship_alarms.py), build 183877 / Steam 26295378.

Three pieces, each pinned by its bytes; any mismatch leaves the alarms off (silence, native lights) and the
ditch and crash unchanged. Static research: research/re-log/2026-10-19_claude_ditch-ambience.md (build 180836;
re-checked on 183791 against the aligned code, 2026-10-28).

- Sound: the exported AK::SoundEngine::PostEvent (resolved by its export name, entry bytes pinned), the same
  call cTkAudioManager::Play 0x2EA5A10 makes, on the ship's audio object [ship+0x6268] (eject 0x178740E) or the
  HUD object [M+0x7330F8] (0x40CD6D). Wwise queues it to the audio thread; the runtime posts from the flight
  thread like the game does, and only loop stops from elsewhere (shutdown).
- Engine sound: the game's own switch 0x1797D60(ship, on), which posts Pl_Ship_Engines_Start/Stop and keeps its
  state in ship+0x6728, so the landing machine's later calls stay consistent. Flight thread only.
- Cockpit alarm lights: byte ship+0x6F5F (pinned by the only native writer, 0x1796740..0x179676B, inside Update)
  held on from the Update after-hook each frame; the light pass (0x180AB40.. pinned, it reads the globals below
  by RIP) then pulses AlarmLightColour at AlarmRate. Red = the game's own hostile colour copied over the normal
  light blue while ours, with the hostile intensity; originals restored where they still hold our values.
"""
import ctypes as C
import math
import struct

POST_EVENT_EXPORT = (b'?PostEvent@SoundEngine@AK@@YAII_KIP6AXW4AkCallbackType@@PEAUAkCallbackInfo@@@ZPEAXIPEAU'
                     b'AkExternalSourceInfo@@I@Z')
POST_EVENT_RVA = 0x320FF40
ENGINE_SOUND_RVA = 0x1797B80
PINS = {  # rva -> bytes at that rva (exe on disk, build 183791)
    POST_EVENT_RVA: '488bc44889580848896810488970184c89702041574883ec704533d2498bf141',
    ENGINE_SOUND_RVA: '48895c2408574883ec300fb681286700000fb6fa488bd984d2744b84c00f859f',
    0x1796560: '389f5f6f00007506899f606f00004488af5f6f0000eb14389f5f6f00007506899f606f0000889f5f6f0000',
    0x180A930: ('488b05314f7505b301f30f10358fb7ae03410f28c380b86daa5700007418f30f103576b7ae03eb0ef30f103568b7ae03'
                '32db410f28c380be6f02000000741ef30f59351d85a2050f28c6e8111dc801f30f5cf0b2030f28c6e8e3575d0184db74'
                '0af30f101d2bb7ae03eb08f30f101d1db7ae0384db488d0d04a5ae03488d05eda4ae03480f45c10f1030'),
}
APP_GLOBAL = 0x6F5F868   # cGcApplication::Data
HUD_OBJECT = 0x7330F8    # [M+...]: the audio object of 137 native UI sounds
SHIP_OBJECT = 0x6268     # [ship+...]: the ship's 3D audio object
ALARM_FLAG = 0x6F5F      # ship byte: cockpit alarm lights on
GLOBALS = 0x52F4E50      # gcspaceshipglobals (light pass RIP refs 0x180AB49..0x180ABBC)
COLOUR, COLOUR_HOSTILE = 0x50, 0x60          # 4 floats each: light blue, red
INTENSITY, INTENSITY_HOSTILE, RATE = 0x1270, 0x1274, 0x1278  # 183791: +0x30 (light pass RIP refs)

K = C.WinDLL('kernel32', use_last_error=True)
K.GetProcAddress.argtypes = [C.c_void_p, C.c_char_p]
K.GetProcAddress.restype = C.c_void_p
PostEvent = C.CFUNCTYPE(C.c_uint32, C.c_uint32, C.c_uint64, C.c_uint32, C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32)
EngineSound = C.CFUNCTYPE(None, C.c_void_p, C.c_bool)


def _q(read, address):return struct.unpack('<Q', read(address, 8))[0]


def _f(read, address, n=1):return struct.unpack(f'<{n}f', read(address, 4*n))


def memory_write(address, data):
    C.memmove(address, data, len(data))


def plausible_lights(colour, hostile, intensity, hostile_intensity, rate):
    """The globals are filled from the MBIN at load (zero on disk): refuse lights until they look loaded."""
    values = (*colour, *hostile, intensity, hostile_intensity, rate)
    if not all(math.isfinite(v) for v in values):return False
    return (all(0. <= v <= 4. for v in (*colour, *hostile)) and max(hostile[:3]) > 0.
            and 0. < intensity <= 10000. and 0. < hostile_intensity <= 10000. and 0. < rate <= 20.)


class NativeAlarms:
    def __init__(self, base, read=C.string_at, write=memory_write, resolve=None, functions=None):
        self.base = int(base); self.read = read; self.write = write
        for rva, expected in PINS.items():
            if read(self.base+rva, len(expected)//2).hex() != expected:raise RuntimeError(f'native bytes changed at {rva:#x}')
        address = (resolve or (lambda name: K.GetProcAddress(self.base, name)))(POST_EVENT_EXPORT)
        if address != self.base+POST_EVENT_RVA:raise RuntimeError('PostEvent export moved')
        if functions is not None:self.post_event, self.engine_sound = functions
        else:self.post_event, self.engine_sound = PostEvent(address), EngineSound(self.base+ENGINE_SOUND_RVA)
        self.saved = None   # original (colour, intensity, rate) bytes while the lights are ours
        self.ours = None    # what we wrote
        self.lights_refused = False

    def game_object(self, target, ship):
        if target == 'ship':return _q(self.read, ship+SHIP_OBJECT) if ship else 0
        manager = _q(self.read, self.base+APP_GLOBAL)
        return _q(self.read, manager+HUD_OBJECT) if manager else 0

    def post(self, target, event_id, ship=0):
        """Post a Wwise event; returns the playing ID (0 = not played)."""
        obj = self.game_object(target, ship)
        if target == 'ship' and not obj:return 0
        return self.post_event(int(event_id) & 0xFFFFFFFF, obj, 0, None, None, 0, None, 0)

    def engine(self, ship, on):
        """The game's engine sound switch. Flight thread only."""
        if ship:self.engine_sound(ship, bool(on))

    @property
    def holding(self):
        return self.saved is not None

    def set_lights(self, rate):
        """rate (pulses per second) makes the alarm red and ours; None gives it back. True while ours."""
        if rate is None:
            self.release();return False
        g = self.base+GLOBALS
        if self.saved is None:
            if self.lights_refused:return False
            colour, hostile = _f(self.read, g+COLOUR, 4), _f(self.read, g+COLOUR_HOSTILE, 4)
            intensity, hostile_intensity, native_rate = _f(self.read, g+INTENSITY, 3)
            if not plausible_lights(colour, hostile, intensity, hostile_intensity, native_rate):
                self.lights_refused = True;return False
            self.saved = (self.read(g+COLOUR, 16), self.read(g+INTENSITY, 4), self.read(g+RATE, 4))
            red, bright = self.read(g+COLOUR_HOSTILE, 16), self.read(g+INTENSITY_HOSTILE, 4)
            self.write(g+COLOUR, red); self.write(g+INTENSITY, bright)
            self.ours = (red, bright, None)
        rate_bytes = struct.pack('<f', float(rate))
        if rate_bytes != self.ours[2]:
            self.write(g+RATE, rate_bytes); self.ours = (*self.ours[:2], rate_bytes)
        return True

    def hold_flag(self, ship):
        """Each frame after the ship Update (native rewrites the flag inside it)."""
        if self.saved is not None and ship:self.write(ship+ALARM_FLAG, b'\x01')

    def release(self, ship=0):
        """Give the lights back: restore each global that still holds what we wrote."""
        if self.saved is None:return
        g = self.base+GLOBALS
        for (offset, size), original, mine in zip(((COLOUR, 16), (INTENSITY, 4), (RATE, 4)), self.saved, self.ours):
            if mine is None or self.read(g+offset, size) == mine:self.write(g+offset, original)
        self.saved = self.ours = None
        if ship:self.write(ship+ALARM_FLAG, b'\x00')
