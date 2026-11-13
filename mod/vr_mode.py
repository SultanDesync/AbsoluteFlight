"""VR display detection and the VR flight-input switch (T125), build 185034 / Steam 26540391.

Static and live notes: research/re-log/2026-11-13_claude_vr-first-look.md. Plan: docs/VR-SUPPORT.md.

Detection. VR is the same NMS.exe started with `-HmdEnable 1`. The game's own test for a VR display is
`cmp dword [module+0x6EF0160], 5` at 0x2CAA75C (the only direct reference to that field; it read 5 live on
2026-11-13). The display is VR when that field is 5 or the process command line asks for the headset; either is
enough, so the first frames of a VR boot (field not written yet) are not taken for flat play.

Flight input. With VR hand controllers the piloted ship's update takes two branches of its own:
- steering (0x1792A3D..0x1792A79): reads the in-world stick vector and skips the steering producer, so the
  scalar-getter receipts the axis caves need never arrive and every frame is refused;
- throttle (0x1792E01..0x1792E35): smooths the demand toward the in-world throttle.
Both are entered when `0x50F340(input)` and the user setting byte at +0x6D89 (LIKELY VRVehiclesUseWorldControls) are
true. FlatInput turns the first conditional jump of each into an unconditional one, so the game takes its
flat-screen branches and the HOTAS replaces the in-world stick and throttle. Flown by the user 2026-11-13.

Fire (candidate, not flown). The ship weapon update (0x17EA780) reads the fire action (0x22, pressed and held)
and then throws both answers away when the game's VR hand-control object (app+0x72CDE0) says so: its field
+0x59EC is 3 or more, or both of its per-hand states (+0x5580, +0x5584) are 2 (0x17EB13F..0x17EB16C). In VR a
bound fire button did not fire (user 2026-11-13: "I think a lot of ship inputs require your vr hands on the
virtual controls"). The 'fire' site jumps over that test, so the action alone decides. Which of the two
conditions holds in VR with the hands off the controls is not known; hands() reads the three fields for the log.

Flat-screen input in flight (candidate, not flown). User 2026-11-13: "we actually do want to revert to full
flatscreen control in flight because the user can't juggle vr controllers and joysticks". The game asks one small
function, 0x50F340, "are the VR hand controllers the active input?" from 22 places: ship (steering, throttle,
leaving the ship by gesture), the quick menu, exocraft, the player. The 'hands' site makes it answer no, so all of
them take their flat-screen handling at once. It is global, so the runtime holds it only while the player is
piloting, out of every menu, with Experimental VR and "Flat-screen input in flight" on, and puts the game's bytes
back the moment any of that stops (the VR menus are worked with the hands).

Only while Experimental VR is on. Each change is at most three bytes inside one instruction, written by the
runtime's code writers and put back on restore. Any byte that differs from this build refuses that patch and
the game keeps its own behaviour there.
"""
import ctypes as C
import re
import struct

MODE_GLOBAL = 0x6EF0160  # display device mode (dword): field +0x10 of the static device object at module+0x6EF0150
VR_MODE = 5              # the value the game itself tests for
APP_GLOBAL = 0x6F6C288
HANDS = 0x72CDE0         # application: the VR hand-control object (the ship code passes app+0x4CD580+0x25F860)
HAND_STATES, HANDS_MODE = 0x5580, 0x59EC  # hands: two per-hand states (dwords); the field the fire test compares with 3
# Exact code of this build. 0x2CAA75C: the game's VR test. The blocks: the tests around each site.
PINS = {0x2CAA75C: '833dfd59240405',
        0x1792A3D: ('488d0d0c987d05e8b7aeb4fe488bc8e8efc8d7fe84c0', '0f84be000000',
                    '488d0df0977d05e85baeb4fe488d88986d3000e87f39d2ff84c00f849e000000'),
        0x1792E01: ('488d0d48947d05e8f3aab4fe488bc8e82bc5d7fe84c0', '745a',
                    '488d0d30947d05e89baab4fe488d88986d3000e8bf35d2ff84c0743e'),
        0x17EB132: ('488b1d4f1178058885e00a000083bbcc27730003', '7d16',
                    '4183bc24805500000275194183bc248455000002750e4088bde80a00004088bde00a0000'),
        0x50F340: ('488b81f80500004885c074098b0083f804', '0f94c0', 'c383f8040f94c0c3')}
# name: (rva, original bytes, replacement). steering: je -> nop; jmp, same rel32. throttle: jz short -> jmp short,
# same rel8. fire: jge short (to the discard) -> jmp short past the hand test and the discard (0x17EB16C).
# hands: sete al -> xor al, al; nop: the test answers false.
SITES = {'steering': (0x1792A53, '0f84', '90e9'), 'throttle': (0x1792E17, '74', 'eb'), 'fire': (0x17EB146, '7d16', 'eb24'),
         'hands': (0x50F351, '0f94c0', '32c090')}
_BLOCK_OF = {'steering': 0x1792A3D, 'throttle': 0x1792E01, 'fire': 0x17EB132, 'hands': 0x50F340}
FLIGHT = ('steering', 'throttle')  # inside the piloted ship's update: the flight-thread writer serves them
OTHER = ('fire',)                  # another function: written with every thread held (the runtime's external writer)
GLOBAL = ('hands',)                # asked from all over the game: held only in flight, by FlatInput.hold()


def _pin(rva):
    value = PINS[rva]
    return bytes.fromhex(value if isinstance(value, str) else ''.join(value))


def verify_mode(read, base):
    """The game's own VR test still reads the field we read."""
    return read(base+0x2CAA75C, 7) == _pin(0x2CAA75C)


def verify_input(read, base, names=FLIGHT):
    """Each named block holds this build's bytes; its site may hold the original or our replacement."""
    for name in names:
        rva, original, patch = SITES[name]
        start = _BLOCK_OF[name]; want = _pin(start); got = read(base+start, len(want))
        at = rva-start; size = len(original)//2
        if got[:at] != want[:at] or got[at+size:] != want[at+size:]:return False
        if got[at:at+size] not in (bytes.fromhex(original), bytes.fromhex(patch)):return False
    return True


def verify(read, base):
    """Everything this module pins (tools/game_update.py verify calls this against the exe on disk)."""
    return verify_mode(read, base) and verify_input(read, base, FLIGHT+OTHER+GLOBAL)


def command_line_vr(text):
    """True when the command line asks for the headset: `-HmdEnable 1` (Steam's "No Man's Sky VR" option)."""
    match = re.search(r'-HmdEnable(?:\s+|=)(\S+)', text or '', re.I)
    return bool(match) and match.group(1).strip('"').lower() not in ('0', 'false')


def process_command_line():
    try:
        kernel = C.WinDLL('kernel32'); kernel.GetCommandLineW.restype = C.c_wchar_p
        return kernel.GetCommandLineW() or ''
    except (OSError, AttributeError):
        return ''


def hands(base, read=None):
    """(left state, right state, mode) of the game's VR hand-control object, or None without an application.
    Read-only, for the log: these are what the fire test looks at."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    if not app:return None
    return struct.unpack('<II', read(app+HANDS+HAND_STATES, 8))+struct.unpack('<I', read(app+HANDS+HANDS_MODE, 4))


class VrMode:
    """Is the game's display a headset? Read-only."""
    def __init__(self, base, read=None, command_line=None):
        self.base = int(base); self.read = read or C.string_at
        self.pinned = verify_mode(self.read, self.base)
        self.asked = command_line_vr(process_command_line() if command_line is None else command_line)

    def mode(self):
        """The game's display mode field, or None when this build's test for it is not where we expect."""
        return int.from_bytes(self.read(self.base+MODE_GLOBAL, 4), 'little') if self.pinned else None

    def vr(self):
        return self.asked or self.mode() == VR_MODE

    def status(self):
        return {'display': 'vr' if self.vr() else 'flat', 'mode': self.mode(), 'command_line': self.asked, 'pinned': self.pinned}


class FlatInput:
    """The switches described above. install()/restore() take the runtime's code writers
    (pm, pid, address, expected, replacement): `write` for the flight sites, `external` for the others.
    The flight sites go in together or not at all. The others are each on their own: one that is refused or
    fails is left native and named in `skipped`, and flight is unaffected."""
    def __init__(self, base, read=None):
        self.base = int(base); self.read = read = read or C.string_at; self.skipped = {}
        if not verify_input(read, self.base):raise RuntimeError('VR input code differs from this build; no patch applied')

    def _state(self, name):
        rva, original, patch = SITES[name]; original = bytes.fromhex(original); patch = bytes.fromhex(patch)
        return self.base+rva, original, patch, self.read(self.base+rva, len(original))

    def switched(self, name):
        site, original, patch, current = self._state(name)
        return current == patch

    @property
    def installed(self):
        return all(self.switched(name) for name in FLIGHT)

    def install(self, write, pm, pid, external=None):
        if not verify_input(self.read, self.base):raise RuntimeError('VR input code changed before installation')
        try:
            for name in FLIGHT:
                site, original, patch, current = self._state(name)
                if current != patch:write(pm, pid, site, original, patch)
        except BaseException:
            self.restore(write, pm, pid, external); raise
        self.skipped = {}
        for name in OTHER:
            try:
                if not verify_input(self.read, self.base, (name,)):self.skipped[name] = 'code differs from this build'; continue
                site, original, patch, current = self._state(name)
                if current != patch:(external or write)(pm, pid, site, original, patch)
            except BaseException as error:
                self.skipped[name] = repr(error)
        return True

    def hold(self, name, on, external, pm, pid):
        """Put a GLOBAL site in (on) or take it out, with the all-thread writer. True when the site now is as asked.
        A refusal or failure is recorded in `skipped` and not retried: the game keeps its own behaviour."""
        if name in self.skipped and on:return False
        try:
            if not verify_input(self.read, self.base, (name,)):raise RuntimeError('code differs from this build')
            site, original, patch, current = self._state(name)
            if on and current != patch:external(pm, pid, site, original, patch)
            elif not on and current == patch:external(pm, pid, site, patch, original)
        except BaseException as error:
            self.skipped[name] = repr(error) if not isinstance(error, RuntimeError) else str(error)
            return self.switched(name) == on
        return True

    def restore(self, write, pm, pid, external=None):
        """{site name: True when it holds the game's original bytes again}."""
        results = {}
        for name in FLIGHT+OTHER+GLOBAL:
            site, original, patch, current = self._state(name)
            try:
                if current == patch:(write if name in FLIGHT else external or write)(pm, pid, site, patch, original)
                results[name] = self.read(site, len(original)) != patch
            except BaseException as error:
                results[name] = repr(error)
        return results
