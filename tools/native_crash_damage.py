"""T94 crash damage: four small caves in the game's own collision damage, build 183791 / Steam 26271400.

Static research: research/re-log/2026-10-19_claude_collision-damage.md (build 180836; every site, register, stack
slot and field used here re-checked on 183791 against the aligned code, 2026-10-28). The native collision handler
0x17951D0 already damages the ship on terrain (IMPACTGROUND, table damage 10) and placed objects (IMPACTINSTANCE, 30)
through the ship's damage callback 0x1797960, at a flat rate. These caves make it scale with the impact speed and let
a hard crash reach the hull, all through the game's own damage path (shields, hull, difficulty, launch-thruster
break, HUD):

- A, 0x1797D1F `call 0xB1ED80` (the collision hit entering the damage route) becomes `call A`. A acts only for the
  player's ship, for the collision caller (0x1797960 returns to 0x17963B5) and the two IDs; anything else tail-jumps
  to the original with its native return address. It measures the approach speed into the surface from the collision
  frame (speed = caller xmm8, saved at [rsp+0xD0]; unit direction [callerRBP+0x60]; hit normal hit+0x40):
  v_eff = speed*|cos| + glancing*speed*sin. Above the safe speed it sets the multiplier (the 5th argument) to
  (table + per_us*(v_eff-safe))/table, marks the hit hull-first and sets the launch-thruster break chance; it marks
  every collision hit for the never-kill cap. It then calls the original, clears the marks and logs the hit in a
  ring the runtime reads.
- B, 0x14902D6 in TakeDamage (`test rbx,rbx; jne`, the shield block): for a marked hull-first hit, jump straight to
  the hull block 0x149054C with r13d = 0, the native "shields already down" path. Shields are left as they are.
- C, 0x1490C5F in the tech-break roll (`movss xmm4,[rsi+0x108]`, TechDamageChance): for that hit's damage definition,
  load the speed-scaled chance instead. RFLAGS are live here (comiss at 0x1490C47, jbe at 0x1490C78): saved.
- D, 0x1490574 in the hull block (`cmp byte [r15+0x10E],0`, the game's own non-lethal clamp test): for a marked hit,
  always clamp the hull damage to hull-1 (0x591980 reads the hull), so terrain never empties the hull, and flag it.
- Submerged (user 2026-10-20: crash damage inactive while the ditch at sea runs): A sets the multiplier and the break
  chance to 0 and marks only SUBMERGED_HIT; B hands C that hit's damage definition so the launch-thruster roll uses 0.
  The sea floor scraping a sinking ship does nothing; the purge's hull meter is the hazard.

Every cave matches the hit by the damage-ID pointer TakeDamage keeps at [rbp+0x248] (the same pointer A passed) and
by thread. Nothing here attaches to a process; bytes are checked against the recorded hash first. Allocations are
kept until process exit (a game thread may be inside a cave).
"""
import ctypes as C
import hashlib
import math
import struct

from native_roll_patch import Code

APP_GLOBAL = 0x6F60828
HULL = 0xB940+0x898  # PlayerState hull (int), application+0xC1D8; 0x591980 reads [PlayerState+0x898]
ROUTE = 0xB1ED80      # player damage route (player, id, source, direction, multiplier)
COLLISION_RETURN = 0x17963B5
HULL_GETTER = 0x591980
SITES = {  # name: (rva, original bytes)
    'multiplier': (0x1797D1F, bytes.fromhex('e85c7038ff')),
    'hull_first': (0x14902D6, bytes.fromhex('4885db7509')),
    'break_chance': (0x1490C5F, bytes.fromhex('f30f10a608010000')),
    'never_kill': (0x1490574, bytes.fromhex('4180bf0e01000000')),
}
PIN_RANGES = ((0x1795214, 121), (0x1796399, 33), (0x1797960, 19), (0x17979B9, 61), (0x1797D0D, 29),
              (0x1490287, 174), (0x14904DB, 229), (0x1490C08, 120), (0x591980, 45))
PIN_SHA256 = 'a0368ba7c588196528ee494473c489586671f3818bfd83bd3f9b3b0aee2a21d5'
IDS = {1: b'IMPACTGROUND', 2: b'IMPACTINSTANCE'}
KINDS = {1: 'ground', 2: 'object'}

# Data block (offsets from data).
ENABLED, CAP, SAFE, PER_US, GLANCING, BREAK, DISABLE, GROUND, INSTANCE, NATIVE_CHANCE = (
    0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24)
RETURN, SHIP, ONE, TINY, ID_GROUND, ID_INSTANCE = 0x28, 0x30, 0x38, 0x3C, 0x40, 0x50
ACTIVE_ID, ACTIVE_THREAD, ACTIVE_FLAGS, ACTIVE_CHANCE, ACTIVE_HULL, ACTIVE_DEF, SAVED_RETURN = (
    0x60, 0x68, 0x6C, 0x70, 0x74, 0x78, 0x80)
SEQ, APPLIED, VISITS, MAX_SPEED = 0x88, 0x8C, 0x90, 0x98
ACT_KIND, ACT_SPEED, ACT_VN, ACT_VT, ACT_VEFF, ACT_MULT = 0x9C, 0xA0, 0xA4, 0xA8, 0xAC, 0xB0
SUBMERGED = 0xB4  # u32: a ditch at sea is running, every collision hit does nothing
RING, RECORD, RECORDS = 0x100, 0x30, 8
DATA_SIZE = RING+RECORD*RECORDS
# Cave placement inside one 4 KiB allocation.
CAVE_OFFSETS = {'multiplier': 0x000, 'hull_first': 0x500, 'break_chance': 0x600, 'never_kill': 0x680}
DATA_OFFSET = 0x800
ALLOCATION = 0x1000

# Hit flags (record +0x1C): set by A, then applied bits by B/C/D.
HULL_FIRST, CAP_REQUESTED, DISABLING, CAPPED, HULL_APPLIED, CHANCE_APPLIED, SUBMERGED_HIT = 1, 2, 4, 8, 16, 32, 64

# Rel32 Jcc opcodes.
JB, JAE, JE, JNE, JBE, JS, JNS, JP, JL, JLE = (b'\x0f\x82', b'\x0f\x83', b'\x0f\x84', b'\x0f\x85', b'\x0f\x86',
                                               b'\x0f\x88', b'\x0f\x89', b'\x0f\x8a', b'\x0f\x8c', b'\x0f\x8e')
GS_THREAD = bytes.fromhex('65 8b 04 25 48 00 00 00')  # mov eax, gs:[0x48] (TEB client thread id)


def verify(read, base):
    """True only when every pinned range matches build 183791 and each site holds its original bytes."""
    h = hashlib.sha256()
    for rva, size in PIN_RANGES: h.update(read(base+rva, size))
    if h.hexdigest() != PIN_SHA256: return False
    return all(read(base+rva, len(original)) == original for rva, original in SITES.values())


def multiplier_code(cave, base, data):
    """Cave A: a Win64 callee standing in for the damage route at the collision call."""
    c = Code(cave)
    def d(offset): return data+offset
    c.relative(b'\x83\x3d', d(ENABLED), b'\x01'); c.relative(JNE, 'pass')
    c.emit(bytes.fromhex('48 8b 84 24 20 01 00 00'))       # rax = return address of 0x1797960
    c.relative(b'\x48\x3b\x05', d(RETURN)); c.relative(JNE, 'pass')
    c.relative(b'\x48\x3b\x3d', d(SHIP)); c.relative(JNE, 'pass')  # rdi = the damaged ship
    c.relative(b'\x48\x83\x3d', d(ACTIVE_ID), b'\x00'); c.relative(JNE, 'pass')  # never nested
    c.emit(bytes.fromhex('48 8b 02 4c 8b 52 08'))           # rax, r10 = 16-byte damage id at [rdx]
    c.emit(bytes.fromhex('41 bb 01 00 00 00'))              # r11d = 1 (ground)
    c.relative(b'\x48\x3b\x05', d(ID_GROUND)); c.relative(JNE, 'instance')
    c.relative(b'\x4c\x3b\x15', d(ID_GROUND+8)); c.relative(JE, 'kind')
    c.label('instance')
    c.emit(bytes.fromhex('41 bb 02 00 00 00'))              # r11d = 2 (placed object)
    c.relative(b'\x48\x3b\x05', d(ID_INSTANCE)); c.relative(JNE, 'pass')
    c.relative(b'\x4c\x3b\x15', d(ID_INSTANCE+8)); c.relative(JNE, 'pass')
    c.label('kind')
    c.relative(b'\x48\xff\x05', d(VISITS))
    c.emit(bytes.fromhex('4c 8b 94 24 30 01 00 00'))       # r10 = collision frame RBP
    c.emit(bytes.fromhex('4d 85 d2')); c.relative(JE, 'pass')
    c.emit(bytes.fromhex('f3 0f 10 84 24 d8 00 00 00'))    # xmm0 = speed (caller xmm8)
    c.emit(bytes.fromhex(
        'f3 41 0f 10 4a 60 f3 0f 59 4b 40'                  # xmm1 = dx*nx
        'f3 41 0f 10 52 64 f3 0f 59 53 44 f3 0f 58 ca'      #      + dy*ny
        'f3 41 0f 10 52 68 f3 0f 59 53 48 f3 0f 58 ca'      #      + dz*nz
        'f3 0f 10 53 40 f3 0f 59 d2'                        # xmm2 = |n|^2
        'f3 0f 10 5b 44 f3 0f 59 db f3 0f 58 d3'
        'f3 0f 10 5b 48 f3 0f 59 db f3 0f 58 d3'
        'f3 41 0f 10 5a 60 f3 0f 59 db'                     # xmm3 = |d|^2
        'f3 41 0f 10 62 64 f3 0f 59 e4 f3 0f 58 dc'
        'f3 41 0f 10 62 68 f3 0f 59 e4 f3 0f 58 dc'
        'f3 0f 59 d3'))                                     # xmm2 = |n|^2 |d|^2
    c.relative(b'\x0f\x2f\x15', d(TINY)); c.relative(JBE, 'pass')  # degenerate or NaN
    c.relative(b'\x0f\x2f\x05', d(MAX_SPEED)); c.relative(JP, 'pass'); c.relative(JAE, 'pass')
    c.emit(bytes.fromhex('0f 57 ed 0f 2f c5')); c.relative(JB, 'pass')  # xmm5 = 0; speed >= 0
    c.emit(bytes.fromhex(
        'f3 0f 51 d2'                                       # xmm2 = sqrt
        '66 0f 7e c8 25 ff ff ff 7f 66 0f 6e c8'            # xmm1 = |dot|
        'f3 0f 5e ca 0f 2e c9'))                            # cos = |dot|/sqrt
    c.relative(JP, 'pass')
    c.relative(b'\xf3\x0f\x5d\x0d', d(ONE))                 # cos <= 1
    c.emit(bytes.fromhex('0f 28 d1 f3 0f 59 d0 f3 0f 59 c9'))  # xmm2 = v_n = speed*cos; xmm1 = cos^2
    c.relative(b'\xf3\x0f\x10\x1d', d(ONE))
    c.emit(bytes.fromhex('f3 0f 5c d9 f3 0f 5f dd f3 0f 51 db f3 0f 59 d8'))  # xmm3 = v_t = speed*sin
    c.relative(b'\xf3\x0f\x10\x25', d(GLANCING))
    c.emit(bytes.fromhex('f3 0f 59 e3 f3 0f 58 e2'))      # xmm4 = v_eff = v_n + glancing*v_t
    c.relative(b'\xf3\x0f\x11\x05', d(ACT_SPEED)); c.relative(b'\xf3\x0f\x11\x15', d(ACT_VN))
    c.relative(b'\xf3\x0f\x11\x1d', d(ACT_VT)); c.relative(b'\xf3\x0f\x11\x25', d(ACT_VEFF))
    c.relative(b'\x44\x89\x1d', d(ACT_KIND))
    c.relative(b'\x8b\x05', d(NATIVE_CHANCE)); c.relative(b'\x89\x05', d(ACTIVE_CHANCE))
    c.emit(bytes.fromhex('31 c0'))                          # eax = flags
    c.relative(b'\x83\x3d', d(SUBMERGED), b'\x00'); c.relative(JE, 'dry')
    c.emit(bytes.fromhex('83 c8 40'))                       # submerged: no hull-first, cap or disabling
    c.emit(bytes.fromhex('c7 44 24 28 00 00 00 00'))        # the 5th argument: multiplier 0
    c.relative(b'\xc7\x05', d(ACTIVE_CHANCE), bytes(4))     # launch-thruster break chance 0
    c.relative(b'\xe9', 'native')
    c.label('dry')
    c.relative(b'\x83\x3d', d(CAP), b'\x01'); c.relative(JNE, 'nocap')
    c.emit(bytes.fromhex('83 c8 02'))
    c.label('nocap')
    c.relative(b'\x0f\x2f\x25', d(DISABLE)); c.relative(JB, 'enabled_below')
    c.emit(bytes.fromhex('83 c8 04'))
    c.label('enabled_below')
    c.emit(bytes.fromhex('0f 28 cc'))                       # xmm1 = v_eff - safe
    c.relative(b'\xf3\x0f\x5c\x0d', d(SAFE))
    c.emit(bytes.fromhex('0f 57 ed 0f 2f cd')); c.relative(JBE, 'native')
    c.relative(b'\xf3\x0f\x10\x15', d(GROUND))
    c.emit(bytes.fromhex('41 83 fb 01')); c.relative(JE, 'table')
    c.relative(b'\xf3\x0f\x10\x15', d(INSTANCE))
    c.label('table')
    c.emit(bytes.fromhex('0f 2f d5')); c.relative(JBE, 'native')  # table damage > 0
    c.emit(bytes.fromhex('83 c8 01'))                       # hull first
    c.relative(b'\xf3\x0f\x10\x1d', d(PER_US))
    c.emit(bytes.fromhex('f3 0f 59 d9 f3 0f 58 da f3 0f 5e da'))  # (table + per_us*excess)/table
    c.emit(bytes.fromhex('f3 0f 11 5c 24 28'))              # the 5th argument
    c.relative(b'\xf3\x0f\x10\x15', d(BREAK))
    c.relative(b'\xf3\x0f\x5c\x15', d(SAFE))                # xmm2 = break - safe
    c.relative(b'\xf3\x0f\x10\x1d', d(ONE))                 # t = 1 when break <= safe
    c.emit(bytes.fromhex('0f 2f d5')); c.relative(JBE, 't')
    c.emit(bytes.fromhex('0f 28 d9 f3 0f 5e da'))           # t = excess/(break - safe)
    c.relative(b'\xf3\x0f\x5d\x1d', d(ONE))
    c.emit(bytes.fromhex('f3 0f 5f dd'))
    c.label('t')
    c.relative(b'\xf3\x0f\x10\x15', d(ONE))
    c.relative(b'\xf3\x0f\x5c\x15', d(NATIVE_CHANCE))
    c.emit(bytes.fromhex('f3 0f 59 d3'))
    c.relative(b'\xf3\x0f\x58\x15', d(NATIVE_CHANCE))       # chance = native + (1-native)*t
    c.relative(b'\xf3\x0f\x11\x15', d(ACTIVE_CHANCE))
    c.label('native')
    c.emit(bytes.fromhex('f3 0f 10 5c 24 28'))
    c.relative(b'\xf3\x0f\x11\x1d', d(ACT_MULT))
    c.relative(b'\x89\x05', d(ACTIVE_FLAGS))
    # Hull before the hit. RCX, RDX, R8 and R9 still hold the route's arguments: only R10/R11 here.
    c.relative(b'\x4c\x8b\x15', base+APP_GLOBAL)
    c.emit(bytes.fromhex('41 bb ff ff ff ff 4d 85 d2'))     # r11d = -1 without an application
    c.relative(JE, 'hull')
    c.emit(bytes.fromhex('45 8b 9a')+struct.pack('<I', HULL))
    c.label('hull')
    c.relative(b'\x44\x89\x1d', d(ACTIVE_HULL))
    c.emit(GS_THREAD); c.relative(b'\x89\x05', d(ACTIVE_THREAD))
    c.relative(b'\xc7\x05', d(APPLIED), bytes(4))
    c.relative(b'\x48\xc7\x05', d(ACTIVE_DEF), bytes(4))
    c.relative(b'\x48\x89\x15', d(ACTIVE_ID))               # published last: B, C and D match on it
    c.emit(b'\x58'); c.relative(b'\x48\x89\x05', d(SAVED_RETURN))  # own return; the stack is the site's again
    c.relative(b'\xe8', base+ROUTE)
    c.relative(b'\x48\xc7\x05', d(ACTIVE_ID), bytes(4))
    c.relative(b'\x48\xc7\x05', d(ACTIVE_DEF), bytes(4))
    c.relative(b'\x8b\x05', d(SEQ))
    c.emit(bytes.fromhex('ff c0 41 89 c2 41 83 e2 07 45 6b d2 30'))  # r10d = (seq % 8) * 0x30
    c.relative(b'\x4c\x8d\x1d', d(RING))
    c.emit(bytes.fromhex('4d 01 d3'))
    for source, offset in ((ACT_KIND, 4), (ACT_SPEED, 8), (ACT_VN, 0xC), (ACT_VT, 0x10), (ACT_VEFF, 0x14),
                           (ACT_MULT, 0x18), (ACTIVE_HULL, 0x20), (ACTIVE_CHANCE, 0x28)):
        c.relative(b'\x44\x8b\x15', d(source)); c.emit(bytes.fromhex('45 89 53')+bytes([offset]))
    c.relative(b'\x44\x8b\x15', d(ACTIVE_FLAGS)); c.relative(b'\x44\x0b\x15', d(APPLIED))
    c.emit(bytes.fromhex('45 89 53 1c'))
    c.relative(b'\x4c\x8b\x15', base+APP_GLOBAL)
    c.emit(bytes.fromhex('41 b9 ff ff ff ff 4d 85 d2'))     # r9d = -1 without an application
    c.relative(JE, 'after')
    c.emit(bytes.fromhex('45 8b 8a')+struct.pack('<I', HULL))
    c.label('after')
    c.emit(bytes.fromhex('45 89 4b 24'))                    # hull after
    c.emit(bytes.fromhex('41 89 03'))                       # record seq
    c.relative(b'\x89\x05', d(SEQ))
    c.relative(b'\xff\x35', d(SAVED_RETURN))
    c.emit(b'\xc3')
    c.label('pass')
    c.relative(b'\xe9', base+ROUTE)
    return c.finish()


def _match(c, data, rbp_slot=True):
    """Shared head of B and D: the in-progress collision hit on this thread (eax, rax clobbered)."""
    c.relative(b'\x48\x8b\x05', data+ACTIVE_ID)
    c.emit(bytes.fromhex('48 85 c0')); c.relative(JE, 'orig')
    if rbp_slot:c.emit(bytes.fromhex('48 3b 85 48 02 00 00')); c.relative(JNE, 'orig')
    c.emit(GS_THREAD); c.relative(b'\x3b\x05', data+ACTIVE_THREAD); c.relative(JNE, 'orig')


def hull_first_code(cave, base, data):
    rva = SITES['hull_first'][0]
    c = Code(cave); _match(c, data)
    c.relative(b'\xf7\x05', data+ACTIVE_FLAGS, struct.pack('<I', SUBMERGED_HIT)); c.relative(JE, 'dry')
    c.emit(bytes.fromhex('48 85 db')); c.relative(JE, 'orig')
    c.relative(b'\x4c\x89\x3d', data+ACTIVE_DEF)            # C rolls the 0 chance; shields take the 0 damage
    c.relative(b'\xe9', 'orig')
    c.label('dry')
    c.relative(b'\xf7\x05', data+ACTIVE_FLAGS, struct.pack('<I', HULL_FIRST)); c.relative(JE, 'orig')
    c.emit(bytes.fromhex('48 85 db')); c.relative(JE, 'orig')  # ship context only
    c.relative(b'\x4c\x89\x3d', data+ACTIVE_DEF)            # r15 = damage definition, for C
    c.relative(b'\x83\x0d', data+APPLIED, bytes([HULL_APPLIED]))
    c.emit(bytes.fromhex('45 31 ed'))                       # r13d = 0: shields already down
    c.relative(b'\xe9', base+0x149054C)
    c.label('orig')
    c.emit(bytes.fromhex('48 85 db')); c.relative(JNE, base+0x14902E4)
    c.relative(b'\xe9', base+rva+5)
    return c.finish()


def break_chance_code(cave, base, data):
    rva = SITES['break_chance'][0]
    c = Code(cave)
    c.emit(b'\x9c\x50')                                     # RFLAGS and RAX are live here
    c.relative(b'\x48\x8b\x05', data+ACTIVE_DEF)
    c.emit(bytes.fromhex('48 85 c0')); c.relative(JE, 'orig')
    c.emit(bytes.fromhex('48 39 f0')); c.relative(JNE, 'orig')  # rsi = this roll's damage definition
    c.emit(GS_THREAD); c.relative(b'\x3b\x05', data+ACTIVE_THREAD); c.relative(JNE, 'orig')
    c.relative(b'\xf3\x0f\x10\x25', data+ACTIVE_CHANCE)
    c.relative(b'\x83\x0d', data+APPLIED, bytes([CHANCE_APPLIED]))
    c.emit(b'\x58\x9d'); c.relative(b'\xe9', base+rva+8)
    c.label('orig')
    c.emit(b'\x58\x9d'); c.emit(SITES['break_chance'][1]); c.relative(b'\xe9', base+rva+8)
    return c.finish()


def never_kill_code(cave, base, data):
    c = Code(cave); _match(c, data)
    c.relative(b'\xf7\x05', data+ACTIVE_FLAGS, struct.pack('<I', CAP_REQUESTED)); c.relative(JE, 'orig')
    c.emit(bytes.fromhex('4c 89 f1')); c.relative(b'\xe8', base+HULL_GETTER)  # eax = hull
    c.emit(bytes.fromhex('ff c8 39 c3')); c.relative(JLE, 'done')   # damage <= hull-1: no cap
    c.emit(bytes.fromhex('89 c3 85 db')); c.relative(JNS, 'capped')
    c.emit(bytes.fromhex('31 db'))
    c.label('capped')
    c.relative(b'\x83\x0d', data+APPLIED, bytes([CAPPED]))
    c.label('done')
    c.relative(b'\xe9', base+0x14905A0)
    c.label('orig')
    c.emit(SITES['never_kill'][1]); c.relative(JE, base+0x14905A0)
    c.relative(b'\xe9', base+0x149057E)
    return c.finish()


GENERATORS = {'multiplier': multiplier_code, 'hull_first': hull_first_code,
              'break_chance': break_chance_code, 'never_kill': never_kill_code}


def site_patch(name, site, cave):
    original = SITES[name][1]
    if name == 'multiplier':return b'\xe8'+struct.pack('<i', cave-site-5)
    return b'\xe9'+struct.pack('<i', cave-site-5)+b'\x90'*(len(original)-5)


def parse_record(raw):
    seq, kind = struct.unpack_from('<II', raw, 0)
    speed, vn, vt, veff, mult = struct.unpack_from('<5f', raw, 8)
    flags, before, after = struct.unpack_from('<Iii', raw, 0x1C)
    chance, = struct.unpack_from('<f', raw, 0x28)
    return {'seq': seq, 'kind': KINDS.get(kind, kind), 'speed': speed, 'into_surface': vn, 'sliding': vt,
            'impact': veff, 'multiplier': mult, 'hull_before': before, 'hull_after': after, 'break_chance': chance,
            'hull_first': bool(flags & HULL_APPLIED), 'disabling': bool(flags & DISABLING),
            'capped': bool(flags & CAPPED), 'chance_applied': bool(flags & CHANCE_APPLIED),
            'submerged': bool(flags & SUBMERGED_HIT), 'flags': flags}


class CrashDamagePatch:
    """Owns the caves and data block for the life of the process. publish() is the only writer of the settings;
    install()/restore() take the runtime's all-thread code writer (these sites run on game threads)."""
    def __init__(self, base, allocation, read=C.string_at, memmove=C.memmove):
        self.base = int(base); self.read = read; self.memmove = memmove
        if not verify(read, self.base):raise RuntimeError('Crash damage exact-build code mismatch; no patch applied')
        self.allocation = int(allocation); self.data = self.allocation+DATA_OFFSET
        self.caves = {name: self.allocation+offset for name, offset in CAVE_OFFSETS.items()}
        self.code = {name: GENERATORS[name](self.caves[name], self.base, self.data) for name in SITES}
        limits = dict(zip(sorted(CAVE_OFFSETS.values()), sorted(CAVE_OFFSETS.values())[1:]+[DATA_OFFSET]))
        for name, raw in self.code.items():
            if len(raw) > limits[CAVE_OFFSETS[name]]-CAVE_OFFSETS[name]:raise ValueError(f'{name} cave overflow')
        self.sites = {name: self.base+rva for name, (rva, _) in SITES.items()}
        self.patches = {name: site_patch(name, self.sites[name], self.caves[name]) for name in SITES}
        self.installed = set(); self.seen = 0
        self._write(0, bytes(DATA_SIZE))
        self._write(RETURN, struct.pack('<Q', self.base+COLLISION_RETURN))
        self._write(ONE, struct.pack('<ff', 1., 1e-12))
        self._write(MAX_SPEED, struct.pack('<f', 5000.))
        self._write(ID_GROUND, IDS[1].ljust(16, b'\0')); self._write(ID_INSTANCE, IDS[2].ljust(16, b'\0'))
        for name, raw in self.code.items():
            self.memmove(self.caves[name], raw, len(raw))
            if self.read(self.caves[name], len(raw)) != raw:raise RuntimeError('crash damage cave readback failed')

    def _write(self, offset, raw):self.memmove(self.data+offset, raw, len(raw))

    def _u32(self, offset):return struct.unpack('<I', self.read(self.data+offset, 4))[0]

    def install(self, write_code, pm, pid):
        """Switch every site; on any failure the ones already switched are put back."""
        if not verify(self.read, self.base) and not self.installed:
            raise RuntimeError('Crash damage code changed before installation')
        try:
            for name in SITES:
                if name in self.installed:continue
                write_code(pm, pid, self.sites[name], SITES[name][1], self.patches[name])
                self.installed.add(name)
        except BaseException:
            self.restore(write_code, pm, pid)
            raise
        return True

    def restore(self, write_code, pm, pid):
        self.disable()
        results = {}
        for name in list(SITES)[::-1]:
            current = self.read(self.sites[name], len(SITES[name][1]))
            try:
                if current == self.patches[name]:write_code(pm, pid, self.sites[name], self.patches[name], SITES[name][1])
                elif current != SITES[name][1]:raise RuntimeError('foreign bytes at '+name)
                self.installed.discard(name); results[name] = True
            except BaseException as error:results[name] = repr(error)
        return results

    def publish(self, settings, ship, submerged=False):
        """settings: crash_damage.Settings-like (safe, per_us, glancing, break_speed, disable_speed, never_kill,
        ground, instance, native_chance). ship: the player's ship component; 0 turns the caves off. submerged: a
        ditch at sea is running, so collision hits do nothing."""
        values = (settings.safe, settings.per_us, settings.glancing, settings.break_speed, settings.disable_speed,
                  settings.ground, settings.instance, settings.native_chance)
        if not all(math.isfinite(v) for v in values) or ship is None:raise ValueError('bad crash damage settings')
        self._write(SAFE, struct.pack('<8f', *values))
        self._write(CAP, struct.pack('<I', 1 if settings.never_kill else 0))
        self._write(SHIP, struct.pack('<Q', int(ship)))
        self.submerge(submerged)
        self._write(ENABLED, struct.pack('<I', 1 if ship else 0))

    def submerge(self, on):
        """Ditch at sea running: every collision hit does nothing (from the next hit on)."""
        self._write(SUBMERGED, struct.pack('<I', 1 if on else 0))

    def disable(self):self._write(ENABLED, bytes(4))

    def hits(self):
        """New hit records since the last call, oldest first (at most the ring size)."""
        seq = self._u32(SEQ); out = []
        first = max(self.seen+1, seq-RECORDS+1)
        for s in range(first, seq+1):
            raw = self.read(self.data+RING+(s % RECORDS)*RECORD, RECORD)
            record = parse_record(raw)
            if record['seq'] == s:out.append(record)
        self.seen = seq
        return out

    def status(self):
        return {'installed': sorted(self.installed), 'enabled': bool(self._u32(ENABLED)),
                'collisions': struct.unpack('<Q', self.read(self.data+VISITS, 8))[0], 'hits': self._u32(SEQ)}
