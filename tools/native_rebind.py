"""T112 (D26): move the game's own ship key bindings out of the way of keyboard flight, through the game's own Apply.

Build 183877 / Steam 26295378, static (research/re-log/2026-10-30_claude_game-rebind.md):
- The game's options pages edit a working copy of the user settings at [NMS+0x6F5F868]+0x30E998 (General page handler
  0x6F3280: add rbx,30E998h). The saved settings are app+0x307C08. Each page compares the two (0x1CC20F0) to offer
  Apply / Discard / Cancel (0x1BD530); Apply copies working over saved (0x1BFB90, field by field with each array's own
  copy-assign), starts WriteStateToStorageThread and applies the bindings (0x1848290). That's the "save changes" step.
- CustomBindingsPC is +0x10 of either copy: cTkDynamicArray<cGcInputActionMapping2> (pointer, int count, flags at +0xC);
  an entry is 0xA0 of fixed strings: Action 0x40, ActionSet 0x20, Axis 0x20, Button 0x20.
- 0x1CB6D0 is that array's copy-assign: frees the destination if it owns its buffer, resizes it (0x1CB610, the game's
  allocator) and copies the entries. The source is only read.

So the Move button stages the moves the way a page edit does: the working copy's CustomBindingsPC is copy-assigned from
a list the mod owns (the player's remaps with the moves merged in). The player then presses the game's Apply, and the
game copies, saves and applies them itself. Nothing else is written; the saved settings are left to the game.
History: 0.2.4-dev.5 called the Controls-page rebind (reverted on leaving options); 0.2.4-dev.6 resized both copies in
place and called Apply's bindings step (held in session, empty after a restart: likely a buffer the working copy shared
was freed). The way back is the game's own Reset to Default (Controls).
Names are cGcActionSetType, cGcInputActions and cTkInputEnum, checked against the game's name tables on 183877.
The game has no numpad keys (cTkInputEnum), so the moved keys go to the right-hand letters and the navigation block.
"""
import ctypes as C
import hashlib

APP_GLOBAL = 0x6F6B208
SAVED = 0x307C18                   # app + this: the saved settings' CustomBindingsPC (in effect)
WORKING = 0x30E9A8                 # app + this: the options pages' working copy (what Apply saves)
COPY, COPY_END = 0x1CB6E0, 0x1CB7D6
RESIZE, RESIZE_END = 0x1CB620, 0x1CB6DD
PAGE_SITE = 0x6F4A87               # mov rbx,[app]; mov rdi,rcx; add rbx,30E998h (General page's working copy)
APPLY_SITE = 0x6F53C6              # saved = app+307C08; compare; dialog; Apply: copy working over saved (0x1BFB90)
ENTRY = 0xA0
PIN_RANGES = ((COPY, COPY_END-COPY), (RESIZE, RESIZE_END-RESIZE), (PAGE_SITE, 0x11), (APPLY_SITE, 0x49))
PIN_SHA256 = '56488d326da60870a27d711e2ed99c3ebc1b3da8c412275fd763ae71088022b4'  # sha256 over PIN_RANGES, 184271

SHIP, QUICK = 'ShipControls', 'ShipQuickMenu'
# (action set, action, key): every stock ship binding on a key the keyboard layout uses, moved to the right side
# (user 2026-10-30: "just need them out of the way and maybe in a layout that feels somewhat familiar to native").
MOVES = ((SHIP, 'Ship_Thrust', 'KeyI'), (SHIP, 'Ship_Brake', 'KeyK'), (SHIP, 'Ship_RollLeft', 'KeyU'),
         (SHIP, 'Ship_RollRight', 'KeyO'), (SHIP, 'Ship_Boost', 'RShift'), (SHIP, 'Ship_PulseJump', 'KeyN'),
         (SHIP, 'Ship_Land', 'Home'), (SHIP, 'Ship_Exit', 'End'), (SHIP, 'Ship_AutoFollow_Toggle', 'PageUp'),
         (SHIP, 'Ship_AutoFollow_Hold', 'PageDown'), (SHIP, 'Player_TagMarker', 'Insert'), (SHIP, 'Ship_Scan', 'Delete'),
         (QUICK, 'Quick_Left', 'KeyZ'), (QUICK, 'Quick_Right', 'KeyC'))


def verify(read, base):
    """True only when the array copy, its resize and the options pages' working copy and Apply match build 183877."""
    h = hashlib.sha256()
    for rva, size in PIN_RANGES: h.update(read(base+rva, size))
    return h.hexdigest() == PIN_SHA256


def fixed(raw):
    return raw.split(b'\0', 1)[0].decode('ascii', 'replace')


def read_custom(read, base, offset=SAVED):
    """[(action set, action, key name)] from one copy of the game's remaps, or None when it can't be read."""
    app = int.from_bytes(read(base+APP_GLOBAL, 8), 'little')
    if not app:return None
    head = read(app+offset, 12)
    array, count = int.from_bytes(head[:8], 'little'), int.from_bytes(head[8:12], 'little', signed=True)
    if count < 0 or count > 512 or (count and not array):return None
    return [(fixed(e[0x40:0x60]), fixed(e[0:0x40]), fixed(e[0x80:0xA0]))
            for e in (read(array+i*ENTRY, ENTRY) for i in range(count))]


def pending(remaps):
    """MOVES not yet in remaps (by name)."""
    have = {(s, a): k for s, a, k in remaps or ()}
    return [m for m in MOVES if have.get(m[:2]) != m[2]]


def merged(remaps):
    """The player's own remaps with every move applied: other remaps kept, moved actions replaced."""
    moved = {m[:2] for m in MOVES}
    return [r for r in remaps if r[:2] not in moved]+list(MOVES)


def entry(action_set, action, key):
    raw = bytearray(ENTRY)
    for offset, text in ((0, action), (0x40, action_set), (0x60, 'None'), (0x80, key)):raw[offset:offset+len(text)] = text.encode('ascii')
    return bytes(raw)


class Rebinder:
    """Stages the moves in the options pages' working copy on the game thread (the HOTAS menu's callback, inside the
    options menu, where a page edit happens); the game's Apply saves them."""
    def __init__(self, base, read=C.string_at, copy=None):
        self.base = base; self.read = read
        self.verified = verify(read, base)
        self.copy = copy if copy is not None else (
            C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p)(base+COPY) if self.verified else None)

    def stage(self):
        """Copy-assign the merged remaps into the working copy; returns the moves still missing from it."""
        if not self.verified:raise RuntimeError('game bindings not verified on this build')
        app = int.from_bytes(self.read(self.base+APP_GLOBAL, 8), 'little')
        current = read_custom(self.read, self.base, WORKING)
        if not app or current is None:raise RuntimeError('game settings not ready')
        rows = merged(current)
        buffer = C.create_string_buffer(b''.join(entry(*row) for row in rows), len(rows)*ENTRY)
        source = C.create_string_buffer(16)  # a cTkDynamicArray the mod owns: pointer, count, flags (not the game's)
        C.memmove(source, C.addressof(buffer).to_bytes(8, 'little')+len(rows).to_bytes(4, 'little')+b'\x01\x00\x00\x00', 16)
        self.copy(app+WORKING, C.addressof(source))
        return pending(read_custom(self.read, self.base, WORKING))
