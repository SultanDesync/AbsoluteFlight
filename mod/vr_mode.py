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

May fire (candidate; the reason fire still failed in 0.2.7-dev.6). Every firing decision in the weapon update calls
0x17F04C0. In VR, when the input pad of port 0 is the hand controllers (`*[input+0x5F8] == 4`, tested inline
here, not through 0x50F340) and the byte app+0x30E991 is set, it returns false unless the hand gesture query
`0x14B3BD0(hands, 3)` is true or ship+0x7740 is set: the weapon fires only with a hand on the in-world stick.
The 'mayfire' site turns its `jne` on "not VR" (0x17F04EA) into `jmp`, so the function goes straight to its
last test (no firing while the pulse drive is charging or running) as it does on a flat screen.

That inline test is the general lesson: the "VR hands are the active input" test exists as the function 0x50F340
(22 callers) and as about 70 inline copies all over the game, so no single code switch covers it. pad() reads
what they all read, for the log.

Quick menu (candidate, not flown). User 2026-11-14: "we will have to solve opening and navigating the VR quick
menu". In the dev.7 boot the Quick menu action was asked for and answered, but Quick menu select and down were
held and never asked for. Three quick-menu functions carry their own inline VR-hands test: the menu's update
0x1558D26 jumps over the block that reads Quick_Left and the other navigation actions (0x1558D84..0x1558D87),
0x1554380 takes a far branch (0x15543E3), and 0x1558B10 skips a countdown (0x1558B9D). The three 'quick-*' sites
change the 4 those tests compare with into 0x7F, so each takes its flat-screen path. Held with 'hands', only in
flight. What the flat quick menu looks like in a headset, if it draws at all, is not known.

Flat-screen input in flight (candidate; installed and released correctly in dev.6, effect unseen). User 2026-11-13: "we actually do want to revert to full
flatscreen control in flight because the user can't juggle vr controllers and joysticks". The game asks one small
function, 0x50F340, "are the VR hand controllers the active input?" from 22 places: ship (steering, throttle,
leaving the ship by gesture), the quick menu, exocraft, the player. The 'hands' site makes it answer no, so all of
them take their flat-screen handling at once. It is global, so the runtime holds it only while the player is
piloting, out of every menu, with Experimental VR and "Flat-screen input in flight" on, and puts the game's bytes
back the moment any of that stops (the VR menus are worked with the hands).

The mod's HUD in VR (T129; candidate, not seen in a headset). In flat play the ship HUD's GUI is drawn straight to
the screen (the frame call at 0x9F0BF7, the return address the HUD hook has always matched). When the game's
flag at app+0x92CCE4 is set, the same GUI object is drawn into a texture instead, through the wrapper 0x1DDD70
(called at 0x9F0BF0), and the frame call comes from inside that wrapper (0x1DDE26). That is why the mod's HUD drew
nothing in VR: wrong return address. hud_frame() accepts a frame whose return address is the wrapper's and whose
GUI object is the ship HUD's (app+0x7330E0+0x112D90+0x27C40), so the mod's HUD is drawn into the texture the game
shows on its in-world HUD panel. No code is changed for this; HUD_PINS only prove the offsets. Where the mod's
elements land on the panel, and whether its velocity marker lines up, has not been seen.

Only while Experimental VR is on. Each change is at most three bytes inside one instruction (launch: 14 bytes
over three whole instructions, keeping their starts), written by the
runtime's code writers and put back on restore. Any byte that differs from this build refuses that patch and
the game keeps its own behaviour there.
"""
import ctypes as C
import re
import struct

MODE_GLOBAL = 0x6EF5660  # display device mode (dword): field +0x10 of the static device object at module+0x6EF0150
VR_MODE = 5              # the value the game itself tests for
APP_GLOBAL = 0x6F71768
HANDS = 0x72CE80         # application: the VR hand-control object (the ship code passes app+0x4CD580+0x25F860)
HAND_STATES, HANDS_MODE = 0x5580, 0x59EC  # hands: two per-hand states (dwords); the field the fire test compares with 3
# Exact code of this build. 0x2CAA75C: the game's VR test. The blocks: the tests around each site.
PINS = {0x2CAE1BC: '833d9d74240405',
        0x1795AED: ('488d0d3cbc7d05e8077fb4fe488bc8e8ff9bd7fe84c0', '0f84be000000',
                    '488d0d20bc7d05e8ab7eb4fe488d88b86d3000e81f28d2ff84c00f849e000000'),
        0x1795EB1: ('488d0d78b87d05e8437bb4fe488bc8e83b98d7fe84c0', '745a',
                    '488d0d60b87d05e8eb7ab4fe488d88b86d3000e85f24d2ff84c0743e'),
        0x17EE1F2: ('488b1d6f3578058885e00a000083bb6c28730003', '7d16',
                    '4183bc24805500000275194183bc248455000002750e4088bde80a00004088bde00a0000'),
        0x17F3580: ('40534883ec20488b0ddbe177054881c180ce7200e837f1cbff488bd84885c07475e88a849dfe83781005', '7548',
                    '488b15b5e17705488b8a98b09500488b81f80500004885c07435833804753080baf1e93000007427488d8a80ce72004533c0ba03000000'
                    'e83825ccff84c075083883407700007422488b156de177058b827cb7720083f801741083c0fe83f8017608b0014883c4205bc332c04883c4205bc3'),
        0x50F700: ('488b81f80500004885c074098b0083f804', '0f94c0', 'c383f8040f94c0c3'),
        0x155AC21: ('498b8098b09500488b88f80500004885c974098339', '04', '0f8437040000'),
        0x155626E: ('e8bd57c7fe837810057523488b05e8b4a105488b8898b09500488b81f80500004885c074098338', '04', '0f8470020000'),
        0x155AA33: ('488b052e6da105488b8898b09500488b81f80500004885c074058338', '04', '7423'),
        0x156EA42: ('488b051f2da005488b8898b09500488b81f80500004885c0742f8338', '04', '752a'),
        0x156EBB2: ('488b05af2ba005488bd9488b8898b09500488b91f80500004885d20f8465150000833a', '04', '0f855c150000'),
        0x17B4190: ('488b81c0600000488bd14885c07453488b084885c9744b4c8b05bad57b05498b8098b095004c8b88f80500004d85c97422418339'
                    '04', '751c', '4180b8f1e93000007412f30f108aa47600000f57c00f2fc80f97c0c3488b014533c0ba1b00000048ff600832c0c3'),
        0x90F860: ('83bfdcc69200ff4c897c2470', '0f845c010000', ''),
        0x94FB2D: ('488b8898b09500488b81f80500004885c00f84890600008338', '04', '0f8580060000'),
        0x93112F: ('4438bb841900000f8487000000e8efa889ff83781005757c488b0d1a066406488b93c01d00004881c1d0f78d00e87f300100488b8bd0210000e883c48aff488b0df4056406488b93c01d00004881c1d0f78d00e819410100488b0dda056406ba070000004881c1d0f78d00e8a12f0100488b8bd8210000',
                   'e845c48aff', '488b0db6056406ba070000004881c1d0f78d00e86d400100')}
# name: (rva, original bytes, replacement). steering: je -> nop; jmp, same rel32. throttle: jz short -> jmp short,
# same rel8. fire: jge short (to the discard) -> jmp short past the hand test and the discard (0x17EB16C).
# hands: sete al -> xor al, al; nop: the test answers false.
# mayfire: jne short (not VR) -> jmp short, same target: the hand-on-stick rule is never reached.
SITES = {'steering': (0x1795B03, '0f84', '90e9'), 'throttle': (0x1795EC7, '74', 'eb'), 'fire': (0x17EE206, '7d16', 'eb24'),
         'mayfire': (0x17F35AA, '75', 'eb'),
         # quick-*: the immediate of an inline `cmp dword [pad], 4`: 4 -> 0x7F, the pad is never "VR hands" there.
         'quick-nav': (0x155AC36, '04', '7f'), 'quick-branch': (0x1556295, '04', '7f'), 'quick-timer': (0x155AA4F, '04', '7f'),
         # dev.11, 2026-11-14: the open press was answered and the flat radial still never drew (the user: "in VR the quick
         # menu is attached to the left hand"; "we want to force the flatscreen menu to open in VR essentially"). Two more
         # inline tests in the quick menu's input code: the select helper 0x156CB82 (VR hands: hand states; else the
         # Quick_Select read at 0x156CBDB) and 0x156CCF0, whose body is the VR-hands path (else 0x156E288).
         'quick-select': (0x156EA5E, '04', '7f'), 'quick-vrpath': (0x156EBD5, '04', '7f'),
         'hands': (0x50F711, '0f94c0', '32c090'),
         # dev.13, 2026-11-14: the user landed, pressed launch, and the game never asked for Ship_Thrust 0x1B. The launch
         # helper 0x17B10E0 has the same VR rule as fire: with the hand controllers as the input and the hand rule byte
         # app+0x30E991 set, it answers from the in-world throttle (ship+0x76A4 > 0) instead of the action. jne -> jmp.
         # 0.3.1 (Discord, 0.3.0, Index + X56: "not even using the motion controls let's me take off from a landing"):
         # that jne -> jmp also took the in-world throttle away, so with no Launch button bound nothing launched. Now
         # either launches: the in-world throttle test stays, and when it is not forward the helper asks for the action.
         # movss xmm1,[rdx+0x76A4]; xorps; comiss -> cmp dword [rdx+0x76A4],0; nop; jle action; nop; mov al,1; ret.
         # A positive float is a positive dword, so the test is the game's `> 0`. Every instruction start of the old
         # bytes (0x17B1121, 0x17B1129, 0x17B112C) is one in the new, so a thread stopped there resumes on whole code.
         'launch': (0x17B41D1, 'f30f108aa47600000f57c00f2fc8', '83baa476000000907e0890b001c3'),
         # dev.13, user: "the pause menu pointer ... fixed in the pause menu card but can't be controlled". The stick
         # moved the front end's pointer (+0x2360, 9,802 answers), then 0x90EC50 overwrote its position each frame from
         # the VR hand ray (0x90ED10..0x90EE37, the controllers lying on the desk). je (no ray) -> nop; jmp, same rel32:
         # the ray never places the pointer, so the stick does. Held only while a game menu is up.
         'menu-ray': (0x90F86C, '0f84', '90e9'),
         # User 2026-11-15: "we need to solve how to take the quick menu from the hand mount in flight and render where we
         # want it" and "we almost need to run a duped copy of the quick menu on the target gui panel". In VR the quick
         # menu is the GUI at *(app+0x4DC790+0x21D8), drawn by the HUD manager (0x930590, VR only) at 0x930656 into GUI
         # layer 7, which 0x943940 pins to a scene node every frame (0x1511775: the quick menu's node; the left hand).
         # (dev.15 duplicated the wrong GUI, the 700x300 panel at app+0x731090: it was never drawn while the menu was
         # open.) The runtime draws the real one again right after the HUD's own frame (the card fixed in front of the
         # cockpit) and this site skips the hand copy: call -> 5-byte nop. Held only once that has drawn.
         'quick-hand': (0x9311A6, 'e845c48aff', '0f1f440000'),
         # dev.19 2026-11-15, found by a Fable 5.2 agent and checked here: the VR quick menu is GUI layer 2, the game's
         # "QuickMenuUIGroup" node ([layers+0x4CF30], model UI_QUICKMENU.SCENE.MBIN). Each frame 0x94CD40 places it with
         # 0x94EF80(layers, &xf, kind 2), which at 0x94F054 bases the pose on the hand (pad type 4) or, otherwise, on
         # the game's own non-hands placement: a cockpit locator of the ship (node hash 0xB159171A), or head-anchored
         # if there is none. 4 -> 0x7F: the non-hands placement. Held only while the quick menu is open in VR and the
         # HOTAS binding opened it (user: "when the quick menu is called by the hotas binding").
         'quick-front': (0x94FB46, '04', '7f')}
_BLOCK_OF = {'steering': 0x1795AED, 'throttle': 0x1795EB1, 'fire': 0x17EE1F2, 'hands': 0x50F700, 'mayfire': 0x17F3580,
             'quick-nav': 0x155AC21, 'quick-branch': 0x155626E, 'quick-timer': 0x155AA33,
             'quick-select': 0x156EA42, 'quick-vrpath': 0x156EBB2, 'launch': 0x17B4190, 'menu-ray': 0x90F860,
             'quick-hand': 0x93112F, 'quick-front': 0x94FB2D}
# The mod's HUD in VR goes on the HUD's own main GUI, the layer the game fixes to the ship's nose (weapon heat, overheat
# notice). Named by the wide test card survey and the user's screenshots, 0.2.7-dev.10, 2026-11-14: the card labelled
# `0X9A715C GUI +0X7531C0` (1920x1080) sat in front of the cockpit. 0x9A7150: lea rcx, [rbx+0x20040]; call 0x1DD8C0, with
# rbx the HUD object app+0x733180 (the identity the release already pins).
HUD_LAYER_RETURN = 0x9A84BC
# The floating crosshair (user 2026-11-15: "an optional floating vr crosshair at the third hud layer 840x"): the HUD
# object's 840x460 GUI (HUD+0x204C0, app+0x753640), drawn at 0x9AA2B2 in 0x9A9DA9 every frame in VR; the size of
# VR UI layer 4, the game's "CrosshairParent" group (lab note 40). The mod draws its crosshair at that frame's centre.
CROSSHAIR_RETURN = 0x9AB617
CROSSHAIR_GUI = 0x733220+0x204C0       # application: the HUD object, its 840x460 GUI (= 0x7536E0)
HUD_LAYER_GUI = 0x733220+0x20040       # application: the HUD object, its main GUI (= 0x753260)
# The ship HUD's texture path (the planet card in VR): kept for the survey's labels and as proof of the offsets.
HUD_FRAME_RETURN_VR = 0x1DDB5B           # after the frame call inside the render-to-texture wrapper 0x1DDD70
HUD_GUI = 0x733180+0x112D90+0x27C40      # application: HUD manager, its ship HUD, that HUD's GUI object (= 0x86DB50)
HUD_PINS = {0x9318B4: '488d8da0000000',  # lea rcx, [rbp+0xA0]: rbp is the HUD manager (app+0x7330E0)
            0x9318C0: '488d8d902d1100',  # lea rcx, [rbp+0x112D90]: the ship HUD, passed to its draw 0x9EF770
            0x9F1EFC: '488b1565f8570680bac4cd920000488d8e407c0200',  # app; cmp byte [app+0x92CDC4], 0; lea rcx, [rsi+0x27C40]
            0x9F1F64: '7411488b96c08002004533c0e82bbb7effeb05e874b67eff',  # je flat; texture [rsi+0x280C0]; call wrapper; jmp; call frame
            0x1DDB45: '0fb6be7d040000488bcec6867d04000000e895faffff',      # the wrapper's own frame call, same GUI object in rcx
            0x9A84B0: '488d8b40000200e8345183ff',                           # lea rcx, [rbx+0x20040]; call frame: the ship-forward layer
            0x9AB60B: '488d8ec0040200e8d91f83ff'}                           # lea rcx, [rsi+0x204C0]; call frame: the crosshair layer
FLIGHT = ('steering', 'throttle')  # inside the piloted ship's update: the flight-thread writer serves them
OTHER = ('fire', 'mayfire', 'launch')  # other functions: written with every thread held (the runtime's external writer)
GLOBAL = ('hands', 'quick-nav', 'quick-branch', 'quick-timer', 'quick-select', 'quick-vrpath')  # outside the ship's own code: held only in flight, by FlatInput.hold()
MENU = ('menu-ray',)               # the front end's pointer: held only while a game menu is up, by FlatInput.hold()
QUICK_DRAW = ('quick-hand',)       # the quick menu's hand copy: held only while the runtime draws it on the HUD layer
QUICK_FRONT = ('quick-front',)     # the quick menu's non-hands placement: held only while it is open from the HOTAS


def _pin(rva):
    value = PINS[rva]
    return bytes.fromhex(value if isinstance(value, str) else ''.join(value))


def verify_mode(read, base):
    """The game's own VR test still reads the field we read."""
    return read(base+0x2CAE1BC, 7) == _pin(0x2CAE1BC)


def verify_input(read, base, names=FLIGHT):
    """Each named block holds this build's bytes; its site may hold the original or our replacement."""
    for name in names:
        rva, original, patch = SITES[name]
        start = _BLOCK_OF[name]; want = _pin(start); got = read(base+start, len(want))
        at = rva-start; size = len(original)//2
        if got[:at] != want[:at] or got[at+size:] != want[at+size:]:return False
        if got[at:at+size] not in (bytes.fromhex(original), bytes.fromhex(patch)):return False
    return True


def verify_hud(read, base):
    """The ship HUD's draw still chooses between the screen and the texture where this build does."""
    return all(read(base+rva, len(want)//2) == bytes.fromhex(want) for rva, want in HUD_PINS.items())


def hud_frame(base, this, caller, read=None):
    """True for the frame the mod's HUD is drawn into in VR: the HUD's main GUI, the ship-forward layer.
    this: the frame call's object; caller: its return address as an RVA. (Up to dev.10 this was the ship HUD's
    texture, which turned out to be the planet card.)"""
    if caller != HUD_LAYER_RETURN or not this:return False
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    return bool(app) and int(this) == app+HUD_LAYER_GUI


def crosshair_frame(base, this, caller, read=None):
    """True for the HUD's 840x460 crosshair-layer frame (CROSSHAIR_RETURN, CROSSHAIR_GUI)."""
    if caller != CROSSHAIR_RETURN or not this:return False
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    return bool(app) and int(this) == app+CROSSHAIR_GUI


def texture_gui(base, this, caller, read=None):
    """For the test card's survey: the application-relative offset of any GUI being drawn through the
    render-to-texture wrapper, or None. Live 2026-11-14 the ship HUD's texture turned out to be the planet
    card (the test card sat on it, at the planet); the game's cockpit HUD in VR is drawn somewhere else. The
    survey labels every such texture with its offset so one screenshot names the right one."""
    if caller != HUD_FRAME_RETURN_VR:return None
    return gui_offset(base, this, read)


def gui_offset(base, this, read=None):
    """The application-relative offset of a GUI object, or None when it is not inside the application object.
    For the wide survey (dev.8, 2026-11-14: the game's ship-forward VR elements, weapon heat and the overheat
    notice, carried no card, so they are not drawn through the render-to-texture wrapper): every GUI frame is
    labelled with the return address of its frame call and this offset."""
    if not this:return None
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    offset = int(this)-app if app else -1
    return offset if 0 <= offset < 0x4000000 else None


def verify(read, base):
    """Everything this module pins (tools/game_update.py verify calls this against the exe on disk)."""
    return (verify_mode(read, base) and verify_input(read, base, FLIGHT+OTHER+GLOBAL+MENU+QUICK_DRAW+QUICK_FRONT) and verify_hud(read, base)
            and verify_quick(read, base) and verify_pointer(read, base))


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


INPUT_MANAGER, PAD, HANDS_RULE, HUD_TO_TEXTURE = 0x95B098, 0x5F8, 0x30E9F1, 0x92CDC4


def pad(base, read=None):
    """{'type': port 0's pad type (4 = VR hand controllers, what ~90 tests compare), 'hands_rule': the byte that turns
    the hand-on-stick rule on, 'hud_to_texture': the byte that sends the ship HUD to a texture}, or None. Read-only."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    if not app:return None
    manager = struct.unpack('<Q', read(app+INPUT_MANAGER, 8))[0]
    pointer = struct.unpack('<Q', read(manager+PAD, 8))[0] if manager else 0
    return {'type': struct.unpack('<I', read(pointer, 4))[0] if pointer else None,
            'hands_rule': read(app+HANDS_RULE, 1)[0], 'hud_to_texture': read(app+HUD_TO_TEXTURE, 1)[0]}


# The quick menu, for the log (read-only; static, 185034, lab note section 27). The front-end input function 0x15708F0
# runs on the manager app+0x86EEE0. It opens the flat quick menu (mode 1, the object at manager+0xDC0) when the opener
# action is pressed or the request byte is set, the front end is free, and 0x1575820 says it may open. The menu is drawn
# by the HUD-side object app+0x7304F0 (radial GUI at +8 while the fade at +0xAB0 is above zero).
QUICK = 0x86EFA0
QUICK_REQUEST, QUICK_BUSY, QUICK_MODE = 0xDB0, 0xDB1, 0xDB4   # manager: open requested; changing; 0 closed, 1 quick menu, 2..4 other pages
QUICK_SWAP = 0x30E9DF          # application byte: set, the opener is not Quick_Menu 0x17 (0x1570C85, 0x142126B: 0x2C in a ship)
QUICK_GUI, QUICK_GUI_STATE, QUICK_GUI_FADE = 0x730590, 0xA68, 0xAB0
PLAYER_FLAG, PLAYER_STATE, PLAYER_HOLD = 0x732528, 0x72B76C, HANDS+0x2C0   # 0x1575820: any of these set, the menu may not open
GAME_STATE, HANDLES = 0x6F71774, 0x547BE10                      # module: game state (and +4); the handle table 0xA37F50 checks
FRONTEND, FRONTEND_FLAG, FRONTEND_PAGE = 0x89BF68, 0x87FE1A, 0x896B28   # 0x1570CEC: the front end's own block
QUICK_PINS = {0x33CC9C: '4881c1a0ef8600e8f85a2301',          # add rcx, 0x86EFA0; call 0x15727A0
              0x1572CCA: '4438a7b00d0000',                    # cmp byte [rdi+0xDB0], r12b: the request byte
              0x1572D12: '488d8fc00d00004489a7f0ea0000c787b40d000001000000c687b10d000001',  # the quick menu, mode = 1, changing = 1
              0x1572B35: '4438a1dfe93000',                    # cmp byte [app+0x30E9DF], r12b
              0x15776D9: '488b0d88a09f0580b92825730000756e83b96cb77200007565',            # the first two tests of "may open"
              0x1513D65: '8b8e680a0000',                      # mov ecx, [rsi+0xA68]: the drawing object's state
              0x151CB04: '498d5608f3410f1096b00a0000e8ca090000498d4e08'}                   # radial GUI at +8, fade at +0xAB0


def verify_quick(read, base):
    """The quick menu code still reads the fields quick_menu() reads."""
    return all(read(base+rva, len(want)//2) == bytes.fromhex(want) for rva, want in QUICK_PINS.items())


def quick_menu(base, read=None):
    """What the game's quick menu is doing, or None without an application. Read-only, for the log:
    mode (0 closed, 1 the flat quick menu's logic is open), request and changing bytes, the opener swap byte, the
    drawing object's state and whether its radial is being drawn, and which of the game's own conditions would stop
    it opening right now ('blocked'; the one test this cannot evaluate, 0x6A2290, is not in the list)."""
    read = read or C.string_at; base = int(base)
    u8 = lambda a: read(a, 1)[0]
    u32 = lambda a: struct.unpack('<I', read(a, 4))[0]
    u64 = lambda a: struct.unpack('<Q', read(a, 8))[0]
    app = u64(base+APP_GLOBAL)
    if not app:return None
    blocked = []
    if u8(app+PLAYER_FLAG):blocked.append('player flag')
    if u32(app+PLAYER_STATE):blocked.append('player state')
    state = u32(base+GAME_STATE)
    if state == 6:state = u32(base+GAME_STATE+4)
    if state in (0, 4):blocked.append('game state')
    handle = u64(app+PLAYER_HOLD); index = handle >> 32
    if handle and index <= 0xFEFF and u64(base+HANDLES+index*0x58+0x50) == handle:blocked.append('player handle')
    if (u32(app+FRONTEND) or u8(app+FRONTEND_FLAG)) and u32(app+FRONTEND_PAGE) != 0x32:blocked.append('front end')
    return {'mode': u32(app+QUICK+QUICK_MODE), 'request': u8(app+QUICK+QUICK_REQUEST), 'changing': u8(app+QUICK+QUICK_BUSY),
            'swap': u8(app+QUICK_SWAP), 'gui_state': u32(app+QUICK_GUI+QUICK_GUI_STATE),
            'drawn': struct.unpack('<f', read(app+QUICK_GUI+QUICK_GUI_FADE, 4))[0] > 0, 'blocked': tuple(blocked)}


# The menus' pointer (static, 185034, lab note section 28). Every GUI frame (0x1DD8C0) hands the GUI's own input
# object (gui+0x168, when the byte gui+0x468 is set) to 0x204530, which reads UI_CursorX/Y (0xD1, 0xD2) through the
# input manager's analog slot, turns them into a velocity and adds it to the pointer position (input+0xC, +0x10), and
# reads Fe_Select 0x3B and Fe_Back 0x3E into input+0x34 and +0x38. Two tests stand in front of the axes in a headset:
# with the byte app+0x30E96B set and input+0x58 clear the axes are zeroed (0x204620), and with the byte
# [module+0x6EFE7E8]+0xCEC set the velocity step is skipped (0x2046C2).
UI_CURSOR_X, UI_CURSOR_Y = 0xD1, 0xD2
POINTER_VR_GATE, POINTER_SKIP_GLOBAL, POINTER_SKIP = 0x30E9CB, 0x6F03CE8, 0xCEC
INPUT_GLOBAL = 0x53758D0       # module: the input object the GUI code asks (expected: the manager at app+0x95AFB8)
GUI_INPUT, GUI_OWN_INPUT = 0x168, 0x468
POINTER_PINS = {0x1DD613: '488db1680100004438b968040000',   # lea rsi, [rcx+0x168]; cmp byte [rcx+0x468], r15b
                0x20425F: '488b0d6a161705',                 # mov rcx, [module+0x5370350]
                0x2042A4: 'bad1000000', 0x2042C3: '488b01ff5028', 0x2042D3: 'bad2000000',   # UI_CursorX, slot +0x28, UI_CursorY
                0x204330: '83781005751d488b052bd4d60680b8cbe9300000740d807b580075070f57ff450f57d2',  # the headset test that zeroes the axes
                0x2043B0: '488b0531f9cf06', 0x2043D2: '80b8ec0c000000',                      # the skip byte
                0x2053AC: 'f30f58430cf30f584b10', 0x2053CD: 'f30f11430cf30f114b10',          # position += velocity
                0x205550: 'ba3b000000', 0x20556F: '488b01ff5020',                            # Fe_Select through slot +0x20
                0x1B7AED: '4881c1e0f98700',                  # add rcx, 0x87F9E0: the front end, its render 0x917D50
                0x917E2B: '4c8dbf60230000'}                  # lea r15, [rdi+0x2360]: the front end's pointer input


QUICK_PANEL, QUICK_PANEL_FADE = QUICK_GUI+0xBA0, QUICK_GUI+0x15F0   # a 700x300 panel GUI and its fade (not the VR menu)
QUICK_VR, QUICK_VR_SHOWN, QUICK_VR_GUI = 0x4DC830, 0x1984, 0x21D8      # application: the HUD manager's VR menu block;
                                                                       # its draw flag; the quick menu GUI pointer
GUI_FRAME = 0x1DD5F0           # NGui render frame(gui): what draws the panel at 0x151B53C and the HUD at 0x9A7157
FRONTEND, FRONTEND_POINTER = 0x87F9E0, 0x2360                 # application: the front end; its pointer input object
GUI_MANAGER, DEFAULT_POINTER, CURRENT_POINTER = 0x95C910, 0xA0, 0x6617270   # app, the manager's desktop pointer; module
CURSOR_SPEEDS, TIME_SCALES = (0x30E910, 0x30E914), (0x7244DD8, 0x7244DE4)   # application ints; module floats (0x204530)
MENU_CARD = (1920., 1080.)     # the pause menu card's pointer space: it opens at the centre, (960, 540) (dev.15 probe)


VR_UI = 0x92CDC4                 # application byte: the VR interface is up (0x93761A, 0x9A4FB0)


def quick_menu_state(base, read=None):
    """For the log: (quick menu mode, the HUD manager's VR draw flag, its quick menu GUI pointer is set), or None."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    if not app:return None
    return (struct.unpack('<I', read(app+QUICK+QUICK_MODE, 4))[0], read(app+QUICK_VR+QUICK_VR_SHOWN, 1)[0],
            bool(struct.unpack('<Q', read(app+QUICK_VR+QUICK_VR_GUI, 8))[0]))


# dev.16 2026-11-15: the copy never drew and the log could not say why. These frame calls draw GUIs that live outside
# the application object, so the test card survey labels them by return address alone: 0x93061D and 0x93065B are the
# HUD manager's VR draws (a card, and layer 7: the quick menu), 0x12EA447 and 0x132CBF4 two others.
HEAP_GUI_FRAMES = frozenset((0x93116D, 0x9311AB, 0x12EC2E7, 0x132EA94))


LAYERS, LAYER_COUNT, LAYER_SIZE = 0x8DF7D0, 12, 0x490   # application: the VR UI layer records (0x943670 begins one)


def layer_records(base, read=None):
    """The twelve fixed VR UI layer records, as bytes, or None without an application. Read-only."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    return read(app+LAYERS, LAYER_COUNT*LAYER_SIZE) if app else None


def layer_differences(snapshots):
    """snapshots: {state: [bytes, bytes]}, two samples per state. The (layer, offset) dwords that hold still within
    every state and differ between states: {(layer, offset): {state: value}}."""
    states = sorted(snapshots)
    out = {}
    size = min(len(s) for samples in snapshots.values() for s in samples)
    for at in range(0, size-3, 4):
        values = {}
        for state in states:
            a, b = (struct.unpack_from('<I', s, at)[0] for s in snapshots[state][:2])
            if a != b:break
            values[state] = a
        else:
            if len(set(values.values())) > 1:out[(at//LAYER_SIZE, at % LAYER_SIZE)] = values
    return out


def quick_menu_gui(base, read=None):
    """The VR quick menu's GUI while it is open (the front end's quick menu mode 1, the HUD manager's draw flag set,
    0x9305DF), else None. That is the GUI the game draws into the hand-mounted layer 7 at 0x930656."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    if not app:return None
    if struct.unpack('<I', read(app+QUICK+QUICK_MODE, 4))[0] != 1 or not read(app+QUICK_VR+QUICK_VR_SHOWN, 1)[0]:return None
    return struct.unpack('<Q', read(app+QUICK_VR+QUICK_VR_GUI, 8))[0] or None


def frontend_pointer(base, read=None):
    """Address of the front end's pointer input object (position at +0xC, +0x10), or None without an application."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    return app+FRONTEND+FRONTEND_POINTER if app else None


def pointer_probe(base, read=None):
    """Read-only, for the log while a VR game menu is up: every candidate for the pause menu's pointer and what scales
    its velocity in 0x204530, so one boot names the object the menu draws and why the stick did not move it."""
    read = read or C.string_at; base = int(base)
    f32 = lambda a: round(struct.unpack('<f', read(a, 4))[0], 2)
    u32 = lambda a: struct.unpack('<I', read(a, 4))[0]
    app = struct.unpack('<Q', read(base+APP_GLOBAL, 8))[0]
    if not app:return None
    fe = app+FRONTEND+FRONTEND_POINTER; default = app+GUI_MANAGER+DEFAULT_POINTER
    current = struct.unpack('<Q', read(base+CURRENT_POINTER, 8))[0]
    manager = struct.unpack('<Q', read(app+INPUT_MANAGER, 8))[0]
    pad = struct.unpack('<Q', read(manager+PAD, 8))[0] if manager else 0
    return {'fe': (f32(fe+0xC), f32(fe+0x10)), 'fe_velocity': (f32(fe+0x14), f32(fe+0x18)), 'fe_speed': (f32(fe+0x1C), f32(fe+0x20)),
            'fe_flags': (read(fe+0x54, 1)[0], read(fe+0x56, 1)[0], read(fe+0x58, 1)[0]), 'fe_select': u32(fe+0x34),
            'default': (f32(default+0xC), f32(default+0x10)),
            'current': current-app if current and 0 <= current-app < 0x4000000 else (None if not current else 'outside'),
            'speeds': tuple(u32(app+o) for o in CURSOR_SPEEDS), 'times': tuple(f32(base+o) for o in TIME_SCALES),
            'pad_subtype': u32(pad+4) if pad else None}


def verify_pointer(read, base):
    """The GUI input update still reads the pointer the way menu_pointer() and the runtime assume."""
    return all(read(base+rva, len(want)//2) == bytes.fromhex(want) for rva, want in POINTER_PINS.items())


def menu_pointer(base, read=None):
    """{'vr_gate': the byte that (with input+0x58 clear) zeroes the pointer axes in a headset, 'skip': the byte that
    skips the velocity step (None unread), 'same_manager': the GUI code asks the input manager the lanes sit on}, or
    None without an application. Read-only, for the log."""
    read = read or C.string_at; base = int(base)
    app = struct.unpack('<Q', read(base+APP_GLOBAL, 8))[0]
    if not app:return None
    holder = struct.unpack('<Q', read(base+POINTER_SKIP_GLOBAL, 8))[0]
    return {'vr_gate': read(app+POINTER_VR_GATE, 1)[0], 'skip': read(holder+POINTER_SKIP, 1)[0] if holder else None,
            'same_manager': struct.unpack('<Q', read(base+INPUT_GLOBAL, 8))[0] == struct.unpack('<Q', read(app+INPUT_MANAGER, 8))[0]}


def gui_input(base, offset, read=None):
    """For the survey's log line: a GUI's pointer fields. offset: the GUI, application-relative. 'own': the GUI has
    its own input object; then the three flag bytes 0x204530 and the hand pointer use, and the pointer position."""
    read = read or C.string_at
    app = struct.unpack('<Q', read(int(base)+APP_GLOBAL, 8))[0]
    if not app:return None
    gui = app+int(offset); block = gui+GUI_INPUT
    x, y = struct.unpack('<ff', read(block+0xC, 8))
    return {'own': read(gui+GUI_OWN_INPUT, 1)[0], 'f54': read(block+0x54, 1)[0], 'f56': read(block+0x56, 1)[0],
            'f58': read(block+0x58, 1)[0], 'x': round(x, 1), 'y': round(y, 1)}


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
        self.install_other(external or write, pm, pid)
        return True

    def install_other(self, external, pm, pid):
        """The OTHER sites alone (fire, mayfire, launch), with the all-thread writer: they need no flight thread, so the
        runtime puts them in as soon as the pilot is seated in VR, landed included (user 2026-11-15: no HOTAS launch
        "until you've flown then landed. loading a landed save or mounting from on foot won't give launch authority").
        Each site stands alone; a refused one is named in `skipped`."""
        for name in OTHER:
            try:
                if not verify_input(self.read, self.base, (name,)):self.skipped[name] = 'code differs from this build'; continue
                site, original, patch, current = self._state(name)
                if current != patch:external(pm, pid, site, original, patch)
                self.skipped.pop(name, None)
            except BaseException as error:
                self.skipped[name] = repr(error)
        return all(self.switched(name) for name in OTHER)

    def hold(self, name, on, external, pm, pid):
        """Put a GLOBAL, MENU or QUICK_DRAW site in (on) or take it out, with the all-thread writer. True when the site now is as asked.
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
        for name in FLIGHT+OTHER+GLOBAL+MENU+QUICK_DRAW+QUICK_FRONT:
            site, original, patch, current = self._state(name)
            try:
                if current == patch:(write if name in FLIGHT else external or write)(pm, pid, site, patch, original)
                results[name] = self.read(site, len(original)) != patch
            except BaseException as error:
                results[name] = repr(error)
        return results
