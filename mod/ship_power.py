"""T121: pick the ship's power mode from a button (user 2026-11-13: "a cycle back/forward switch or direct to each setting").

Build 185034 / Steam 26540391, static (research/re-log/2026-11-13_claude_power-modes.md). The game's Cycle Power
handler (0x1790D59..0x1790E93, in the ship's controlled update) does three things on a press of action 0x2C:
- ship+0x675C = (value + 1) % 4. Names from its own table: 0 BALANCED, 1 GUNS, 2 ENGINE, 3 SHIELD.
- ship+0x6760 = the game time float at module+0x723F904; the HUD (0x9AB0A6) shows the power indicator while
  now - that time is under a constant.
- posts a text notice (UI_POWER_SETTING_*).
Every consumer (engines 0x1794069, shields and weapons 0x1494341, 0x17EB4A9, 0x17F05B8, 0x17F0DED) reads the
field each time, so writing the first two is the whole state change. The text notice is not posted here.
"""
import ctypes as C

POWER_OFFSET = 0x675C  # ship: power selection (dword)
POWER_TIME_OFFSET = 0x6760  # ship: game time of the last change (float)
GAME_TIME = 0x723F904  # module: the game time the handler stamps (float)
MODES = ('balanced', 'weapons', 'engines', 'shields')
# Button action -> what it asks for: a step round the game's own order, or one mode.
ACTIONS = {'mod.power-next': 'next', 'mod.power-previous': 'previous',
           **{'mod.power-'+name: index for index, name in enumerate(MODES)}}


def wanted(current, request):
    """The power selection a request leads to, or None when it changes nothing or the state is unknown."""
    if type(current) is not int or not 0 <= current <= 3:
        return None
    if request == 'next': target = (current+1) % 4
    elif request == 'previous': target = (current-1) % 4
    elif type(request) is int and 0 <= request <= 3: target = request
    else: return None
    return None if target == current else target


def select(ship, base, request):
    """Write the selection and its timestamp on the piloted ship; returns the new selection or None."""
    if not ship or not base:
        return None
    field = C.c_uint32.from_address(ship+POWER_OFFSET)
    target = wanted(field.value, request)
    if target is None:
        return None
    C.c_float.from_address(ship+POWER_TIME_OFFSET).value = C.c_float.from_address(base+GAME_TIME).value
    field.value = target
    return target
