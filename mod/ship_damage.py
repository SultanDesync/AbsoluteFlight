"""T93: leave the essentials of an abandoned ditched ship damaged, repaired the vanilla way (user 2026-10-20:
"only the essentials: launch thruster, pulse engine and shields, saved with the game").

Build 180836 / Steam 25732212 (research/re-log/2026-10-19_claude_ditching.md, "damaged ship technology"):
- player state = [module+0x6E8D708] + 0xB940 (game state +0xE70, player state +0xAAD0): CONFIRMED static, the
  quick menu and the launcher-damaged check both use it;
- primary ship index at player state +0x182A0, ship tech-only inventories (12 x 0x248) at +0xAB28: CONFIRMED;
- inventory store: element count +0x8C, element array +0x90, 0x30 per element (GetElement 0x4C5B40): CONFIRMED;
- element: Id TkID0x10 inline at +0x0, DamageFactor float at +0x1C (nmspy cGcInventoryElement): layout
  CONFIRMED; that DamageFactor is what the takeoff refusal reads is LIKELY, the first live check decides.
The edit is in the live inventory, so the game saves it.
"""
import ctypes as C
import struct

APP_GLOBAL = 0x6E8D708
PLAYER_STATE = 0xB940
PRIMARY_SHIP = 0x182A0
TECH_INVENTORIES = 0xAB28
STORE_SIZE = 0x248
STORE_COUNT = 0x8C
STORE_ELEMENTS = 0x90
ELEMENT_SIZE = 0x30
ELEMENT_DAMAGE = 0x1C
SHIPS = 12
MAX_ELEMENTS = 512
# Launch thruster, pulse engine, deflector shield (product IDs, LIKELY; matched exactly).
ESSENTIALS = ('LAUNCHER', 'SHIPJUMP1', 'SHIPSHIELD')
DAMAGED = 1.
SMOKE_REFRESH = 0x7428  # T97: byte on cGcSpaceshipComponent; 1 makes Update 0x174A600 re-evaluate crash smoke


def _q(read, address):return struct.unpack('<Q', read(address, 8))[0]


def primary_index(read, base):
    """Index of the player's current ship (0..11), or None."""
    app = _q(read, base+APP_GLOBAL)
    if not app:return None
    index = struct.unpack('<i', read(app+PLAYER_STATE+PRIMARY_SHIP, 4))[0]
    return index if 0 <= index < SHIPS else None


def ship_tech(read, base):
    """[(element address, id)] of the primary ship's technology, or [] when anything is out of range."""
    index = primary_index(read, base)
    if index is None:return []
    state = _q(read, base+APP_GLOBAL)+PLAYER_STATE
    store = state+TECH_INVENTORIES+index*STORE_SIZE
    count = struct.unpack('<I', read(store+STORE_COUNT, 4))[0]
    elements = _q(read, store+STORE_ELEMENTS)
    if not elements or not 0 < count <= MAX_ELEMENTS:return []
    out = []
    for i in range(count):
        address = elements+i*ELEMENT_SIZE
        raw = read(address, 16).split(b'\0', 1)[0]
        out.append((address, raw.decode('ascii', 'replace').lstrip('^')))
    return out


def damage_essentials(read, write, base, factor=DAMAGED, ship=None):
    """Damage the primary ship's essentials, only if it is ship (an index) when given. Returns the IDs damaged
    (already-damaged ones included)."""
    if ship is not None and primary_index(read, base) != ship:return []
    done = []
    for address, ident in ship_tech(read, base):
        if ident in ESSENTIALS:
            write(address+ELEMENT_DAMAGE, struct.pack('<f', factor))
            done.append(ident)
    return done


def refresh_smoke(write, ship_component):
    """Ask the ship component's next Update to re-evaluate its crash smoke (research/re-log/2026-10-20_claude_wrecked-ship-state.md)."""
    if ship_component:
        write(int(ship_component)+SMOKE_REFRESH, b'\x01')


def memory_write(address, data):
    C.memmove(address, data, len(data))
