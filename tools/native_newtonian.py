"""T55 qualified acceleration seam, exact build 183877 / Steam 26295378 (remapped from 180836 on 2026-10-28).

Only final stack XYZ is replaced, immediately before native mass scaling. Native
rotation, collisions, gravity, integration and all fallback paths remain native.
An invocation-synchronous Windows x64 helper receives (work, command), returns
1 to apply command float[3], 0 to coast, or 2 for explicit native fallback. The
native cave independently enforces the publisher deadline before AND after the
helper; expiry writes isolated gravity compensation, never an old command or
the native speed follower. All geometry gates still apply during expired coast.
Construction and code generation never attach to a process or patch game code.
Owned allocations remain inert until process exit after restoration.
"""
import ctypes as C
import json
import math
from pathlib import Path
import struct
from native_roll_patch import Code, K, MemoryInfo, replace_code

MANIFEST=Path(__file__).with_name('native_newtonian_sites.json')
EXPECTED_SHA='d393c6782d29e1915854c0d5cf42505c8093740c05dc53705dbed7e0d1d4ac37'
CONTROL=0x000; WORK=0x400; WORK_SIZE=0x400; RING=0x1000
SLOT=0x800; CAPACITY=1024; ALLOCATION=RING+SLOT*CAPACITY
NEAR_DISTANCE=0x10000000 # one contiguous block within256MiB of final site
COMMAND=0x800; MAX_LEASE_MS=250
# T88: runtime switch read by caves built with allow_planetary_flight. While it is 1 the native
# low-height gate (ship+0x6720) and a nonzero ground-clearance term are tolerated, and that term
# is added to the command as a safety net. 0 (the initial value) keeps the space-only admission.
PLANETARY=0x3F0
LOW_HEIGHT='80 be 30 67 00 00 00'
# T88 integrator clamp. The radial velocity clamp is 2*[ship+0x6650] (BoostMaxSpeed; 0x6640 on 180836), loaded at
# 0x179383F and doubled at 0x179384F (183791; 0x176043D/0x176044C on 180836) (research/re-log/2026-10-16_codex_newtonian-gates.md, CONFIRMED
# static). In air native blends that field toward the Planet engine block, which capped a Sultan
# Fighter at 305.6 u/s (2*152.8) on 2026-10-19. Planetary caves only; signature unique in the exe.
CLAMP_HALF=0x3F4
CLAMP_SITE={'rva':0x179365f,'size':8,'original':'f30f109650660000',
            'signature':'f30f1096506600004533c9440f284d50f30f58d20fc6c000',
            'instructions':['0x179383f: movss xmm2, dword ptr [rsi + 0x6650]']}
# T93 ditch at sea (research/re-log/2026-10-19_claude_ditching.md). The native ground-clearance term treats
# the sea as ground, and under water it grows to thousands of u/s^2: one frame of it flung a ditched ship
# from 15 to 306 u/s (user flight 2026-10-19). While WATER is 1 and the ship is over water (ship+0x62E0),
# planetary caves never add that term to a written command (applied or coast), and native-fallback frames
# keep it capped at WATER_PUSH (u/s^2), so native can only lift a submerged ship gently. Not while landing.
WATER=0x3FC
WATER_PUSH=0x3EC
DEFAULT_WATER_PUSH=50.
# T101 manual landing (research/re-log/2026-10-20_claude_manual-landing-m1-m2.md). While GEAR is 1 the planetary
# caves never add the ground-clearance term to a written command, so Sultan can fly down to touchdown; native
# fallback frames keep it. The planetary clearance cave (CLEARANCE_SITE) copies UpdateControlled's terrain clearance
# (min of height above sea and above the terrain ray hit) into the work record at CLEARANCE.
GEAR=0x3E8
CLEARANCE=0x330
# Corvette (10): 2026-10-22 (user): Corvette flies Sultan on its baseline, marked incomplete. Only the normal piloted caller
# (return 0x1784121 on 183791, 0x1751001 on 180836) is latched, so its idle route stays native (T65 static map).
SUPPORTED_CLASSES=frozenset((1,2,3,4,6,7,8,9,10))
CORVETTE=10
CORVETTE_AUTOPILOT=3  # ship+0x6430 (0x6420 on 180836) alignment selector: the Corvette autopilot flies (hand back to native)
REASONS={0:'idle',1:'native_disabled',2:'expired_coast',3:'identity_changed',
         4:'native_mode',5:'physics_unqualified',6:'missing_same_invocation_terms',
         7:'height_safety_active',8:'nonfinite_velocity_or_gravity',9:'applied',
         10:'helper_coast',11:'helper_native_fallback',12:'invalid_acceleration',
         13:'helper_result_invalid'}
TERMS=('forward','boost','overspeed_subtrahend','track','relative_track',
       'proportional_unblended','minimum_forward','alignment_unblended',
       'direction_blend','after_reverse','gravity','height','avoidance',
       'accum100','control_without_gravity','before_height',
       'proportional_weighted','alignment_weighted')
VECTORS={name:0x180+16*i for i,name in enumerate(TERMS)}
BITS={name:1<<i for i,name in enumerate(TERMS)}
REQUIRED=BITS['gravity']|BITS['height']
FIELDS={'enabled':0,'override':4,'ship':8,'body':16,'state':24,'context':32,
        'frame':40,'invocation':48,'thread':56,'visits':60,'deadline':64,'lease':72,
        'application':80,'backend':88,'body_record':96,'motion':104,'handle':112,
        'motion_id':120,'body_flags':124,'write_sequence':128,'published':136,
        'r4_deadline':144,'r4_generation':152,'physics':160,'controller_wrapper':168,
        'controller':176,'gravity_cached_bits':184,'gravity_factor_word':188,
        'custom_flag':192,'custom_vector':196,'allow_nonzero_gravity':212,
        'r4_applied':216,'baseline_records':224,'body_pages':256,
        'body_page_entry':264,'body_page':272,'motion_pages':280,
        'motion_page_entry':288,'motion_page':296,'player_attachment':304,
        'class_owner':312,'ship_class':320,'modifier_manager':328,
        'callback_count':336,'callback_mask':340,'callbacks':344}

def site_manifest():
    from native_autobank import AUTOBANK_SITE
    m=json.loads(MANIFEST.read_text(encoding='utf8'))
    if m['build']['sha256']!=EXPECTED_SHA:raise ValueError('Wrong exact-build manifest')
    m['sites']['autobank']=dict(AUTOBANK_SITE)
    return m

def _save(c):
    # Every selected native RSP is aligned16. Ten pushes preserve alignment.
    c.emit(bytes.fromhex('9c 50 51 52 41 50 41 51 41 52 41 53 56 57 48 81 ec 90 00 00 00'))
    for r in range(6):c.emit(bytes([0xf3,0x0f,0x7f,0x44+8*r,0x24,0x20+16*r]))
    c.emit(bytes.fromhex('0f ae 9c 24 80 00 00 00')) # stmxcsr, own arithmetic restored

def _restore(c):
    c.emit(bytes.fromhex('0f ae 94 24 80 00 00 00'))
    for r in range(6):c.emit(bytes([0xf3,0x0f,0x6f,0x44+8*r,0x24,0x20+16*r]))
    c.emit(bytes.fromhex('48 8d a4 24 90 00 00 00 5f 5e 41 5b 41 5a 41 59 41 58 5a 59 58 9d'))

def _check(c,prefix,address,suffix=b'',reject='out',condition=b'\x0f\x85'):
    c.relative(prefix,address,suffix);c.relative(condition,reject)

def _store_xmm(c,r,address):
    c.relative((b'\x44' if r>=8 else b'')+bytes([0x0f,0x11,0x05+8*(r%8)]),address)

def _load_xmm(c,r,address):
    c.relative((b'\x44' if r>=8 else b'')+bytes([0x0f,0x10,0x05+8*(r%8)]),address)

def _stack_vector(c,offset,address):
    c.emit(b'\x0f\x10\x85'+struct.pack('<i',offset));_store_xmm(c,0,address)

def _snapshot_scalar(c,source_reg,offset,target,width=4):
    # source_reg: RAX0, RCX1, RDX2, RBX3, RBP5, RSI6, RDI7, R8=8...
    rex=(0x48 if width==8 else 0x40)|(1 if source_reg>=8 else 0)
    if rex!=0x40:c.emit(bytes([rex]))
    c.emit(bytes([0x8b,0x80+(source_reg%8)])+struct.pack('<i',offset))
    c.relative(b'\x48\x89\x05' if width==8 else b'\x89\x05',target)

def _owner_guard(c,data,avoidance=False):
    _check(c,b'\x83\x3d',data,b'\x01')
    if avoidance:
        _check(c,b'\x48\x3b\x0d',data+16) # explicit RCX native body
        c.relative(b'\x48\x8b\x05',data+8)
        c.emit(bytes.fromhex('48 05 b0 70 00 00 4c 39 f8'));c.relative(b'\x0f\x85','out')
    else:
        _check(c,b'\x48\x3b\x35',data+8)
        _check(c,b'\x48\x3b\x2d',data+40)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
    _check(c,b'\x3b\x05',data+56)

def low_height_gate(c,data,reject,label):
    """ship+0x6720 clear, or the T88 planetary switch on; otherwise jump to reject."""
    c.emit(bytes.fromhex(LOW_HEIGHT));c.relative(b'\x0f\x84',label)
    c.relative(b'\x83\x3d',data+PLANETARY,b'\x01');c.relative(b'\x0f\x85',reject)
    c.label(label)


def planetary_skip(c,data,label):
    """Jump to label while the T88 planetary switch is on."""
    c.relative(b'\x83\x3d',data+PLANETARY,b'\x01');c.relative(b'\x0f\x84',label)


def capture_code(kind,cave,site,data):
    """Replay branch/RIP-free original bytes; captures actual native addends."""
    original=bytes.fromhex(site_manifest()['sites'][kind]['original'])
    c=Code(cave)
    after=kind in ('boost','overspeed','proportional')
    if after:c.emit(original)
    _save(c);_owner_guard(c,data,kind=='avoidance')
    w=data+WORK
    source={'forward':('forward',2),'boost':('boost',0),
            'overspeed':('overspeed_subtrahend',1),'track':('track',1),
            'relative_track':('relative_track',0),
            'proportional':('proportional_unblended',7),
            'minimum_forward':('minimum_forward',6),
            'alignment':('alignment_unblended',6),'blend':('direction_blend',7),
            'reverse':('after_reverse',7),'gravity':('gravity',1),
            'avoidance':('avoidance',12)}
    if kind=='blend':
        _store_xmm(c,7,w+VECTORS['proportional_weighted'])
        _store_xmm(c,6,w+VECTORS['alignment_weighted'])
        c.emit(bytes.fromhex('0f 28 c7 0f 58 c6'))
        _store_xmm(c,0,w+VECTORS['direction_blend'])
        bit=BITS['direction_blend']|BITS['proportional_weighted']|BITS['alignment_weighted']
    elif kind=='height':
        _stack_vector(c,0x520,w+VECTORS['height'])
        # Original XMM0 saved at our RSP+20; PRE-add is prior full correction.
        c.emit(bytes.fromhex('0f 10 44 24 20'));_store_xmm(c,0,w+VECTORS['before_height'])
        bit=BITS['height']|BITS['before_height']
    else:
        name,r=source[kind]
        # XMM0..5 are saved before guards; RAX use never alters their value.
        _store_xmm(c,r,w+VECTORS[name]);bit=BITS[name]
        # 183791: the control sum without gravity is XMM9 at the gravity site (was XMM7).
        if kind=='gravity':_store_xmm(c,9,w+VECTORS['control_without_gravity'])
        if kind=='reverse':_stack_vector(c,0x50,w+VECTORS['accum100'])  # 183791: accumulator [rbp+0x50] (was +0x100)
    c.relative(b'\x81\x0d',data+60,struct.pack('<I',bit))
    c.label('out');_restore(c)
    if not after:c.emit(original)
    c.relative(b'\xe9',site['address']+len(original))
    result=c.finish()
    if len(result)>0xf00:raise ValueError('Capture cave overflow')
    return result

def latch_code(cave,site,data,base):
    c=Code(cave);c.emit(bytes.fromhex(site['original']));_save(c)
    _check(c,b'\x83\x3d',data,b'\x01')
    _check(c,b'\x48\x3b\x35',data+8)
    c.emit(b'\x48\xb8'+struct.pack('<Q',base+0x1783F41))
    c.emit(bytes.fromhex('48 39 85 68 41 00 00'));c.relative(b'\x0f\x85','out')
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
    c.relative(b'\x83\x3d',data+56,b'\x00');c.relative(b'\x0f\x84','set_thread')
    _check(c,b'\x3b\x05',data+56)
    c.label('set_thread');c.relative(b'\x89\x05',data+56)
    c.relative(b'\x48\x89\x2d',data+40)
    c.relative(b'\x48\xff\x05',data+48)
    c.relative(b'\xc7\x05',data+60,bytes(4))
    c.relative(b'\x48\xc7\x05',data+240,bytes(4))
    # Clear every optional term and record field. No previous invocation survives.
    c.emit(b'\x48\xbf'+struct.pack('<Q',data+WORK))
    c.emit(b'\xb9'+struct.pack('<I',WORK_SIZE//8)+bytes.fromhex('31 c0 fc f3 48 ab'))
    c.relative(b'\x48\x8b\x05',data+48);c.relative(b'\x48\x89\x05',data+WORK)
    c.relative(b'\x48\x89\x2d',data+WORK+8)
    c.label('out');_restore(c);c.relative(b'\xe9',site['address']+len(bytes.fromhex(site['original'])))
    return c.finish()

def final_code(cave,site,data,base,get_tick,helper,*,allow_combat_flight=False,allow_planetary_flight=False):
    from native_autobank import AUTOBANK_COUNT,AUTOBANK_INVOCATION
    c=Code(cave);_save(c);_owner_guard(c,data)
    c.emit(b'\x48\xb8'+struct.pack('<Q',base+0x1783F41))
    c.emit(bytes.fromhex('48 39 85 68 41 00 00'));c.relative(b'\x0f\x85','out')
    w=data+WORK
    def reason(n):c.relative(b'\xc7\x05',w+60,struct.pack('<I',n))
    def fail(n):reason(n)
    def ne(prefix,address,suffix=b''): _check(c,prefix,address,suffix,'publish')
    def ins_ne(code):c.emit(bytes.fromhex(code));c.relative(b'\x0f\x85','publish')
    def zero_bits(reg,offset,width=4):
        rex=0x41 if reg>=8 else b''
        if rex:c.emit(bytes([rex]))
        c.emit(bytes([0x8b,0x80+(reg%8)])+struct.pack('<i',offset))
        c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0'));c.relative(b'\x0f\x85','publish')
    # Capture before any admission/rejection. Only pinned own-body memory is read.
    for reg,target in ((6,16),(13,24)):
        c.relative((b'\x4c' if reg>=8 else b'\x48')+bytes([0x89,0x05+8*(reg%8)]),w+target)
    _stack_vector(c,0x50,w+80);_stack_vector(c,0x50,w+96)
    c.relative(b'\x8b\x05',data+60);c.relative(b'\x89\x05',w+56)
    c.emit(b'\x48\xb8'+struct.pack('<Q',get_tick)+b'\xff\xd0')
    c.relative(b'\x48\x89\x05',w+48)
    fail(3)
    # Identity chain must still match the originally qualified native player body.
    c.emit(bytes.fromhex('48 8b 86 58 62 00 00'));ne(b'\x48\x3b\x05',data+160)
    c.emit(bytes.fromhex('48 83 c0 50'));ne(b'\x48\x3b\x05',data+16)
    ne(b'\x4c\x3b\x2d',data+16)
    c.emit(bytes.fromhex('49 8b 85 90 02 00 00'));ne(b'\x48\x3b\x05',data+24)
    c.emit(bytes.fromhex('48 89 c2')) # RDX=S after all native volatile values saved
    c.emit(bytes.fromhex('48 8b 42 08'));ne(b'\x48\x3b\x05',data+32)
    c.emit(bytes.fromhex('49 8b 85 28 03 00 00'));ne(b'\x48\x3b\x05',data+32)
    c.emit(bytes.fromhex('49 89 c0')) # R8=C
    c.relative(b'\x48\x89\x15',w+32);c.relative(b'\x4c\x89\x05',w+40)
    _snapshot_scalar(c,2,0x88,w+0x2d0,8)
    ne(b'\x48\x3b\x05',data+112)
    # 56AF70..83 proves basis is S10/20/30; S0 is flags, not attitude.
    for offset,target in ((0x60,112),(0x70,128),(0x10,144),(0x20,160),(0x30,176),(0x40,0x2e0),(0x50,0x2f0)):
        c.emit(b'\x0f\x10\x82'+struct.pack('<i',offset));_store_xmm(c,0,w+target)
    for offset,target in ((0x390,192),(0x3a0,208),(0x3b0,224),(0xa40,240),(0xa50,256)):
        c.emit(b'\x41\x0f\x10\x80'+struct.pack('<i',offset));_store_xmm(c,0,w+target)
    _snapshot_scalar(c,8,0xa38,w+0x110,8)
    _snapshot_scalar(c,13,0x260,w+0x118)
    for offset,target in ((0x270,0x11c),(0x274,0x120),(0x27c,0x124),(0x338,0x128)):
        _snapshot_scalar(c,13,offset,w+target)
    _snapshot_scalar(c,2,0,w+0x12c)
    for offset,target in ((0x6650,0x130),(0x66a4,0x134),(0x6654,0x138),(0x7318,0x13c),
                          (0x6430,0x140),(0x76ed,0x144),(0x6ce4,0x148),(0x716c,0x14c)):
        _snapshot_scalar(c,6,offset,w+target)
    _snapshot_scalar(c,5,0x4180,w+0x150)
    for offset,target in ((0x18,0x154),(0x24,0x158),(0x30,0x15c)):
        c.relative(b'\x8b\x05',base+0x7232e78+offset);c.relative(b'\x89\x05',w+target)
    c.relative(b'\x8b\x05',base+0x517e478);c.relative(b'\x89\x05',w+0x160)
    for offset,target in ((0x675c,0x164),(0x6730,0x168),(0x7740,0x16c),(0x73e0,0x170)):
        _snapshot_scalar(c,6,offset,w+target)
    # T101 clearance: 7.10 reuses its stack slot before this site, so clearance_code captures it where it's written.
    # Effective backend identity is checked before R3 reads pinned motion values.
    fail(3)
    c.relative(b'\x48\x8b\x05',base+0x6f5f868);ne(b'\x48\x3b\x05',data+80)
    c.emit(bytes.fromhex('48 8b 80 48 cf 72 00'));ne(b'\x48\x3b\x05',data+304)
    c.emit(bytes.fromhex('49 8b 80 80 03 00 00'));ne(b'\x48\x3b\x05',data+88)
    c.emit(bytes.fromhex('48 89 c7 48 8b 40 40'));ne(b'\x48\x3b\x05',data+256)
    c.relative(b'\x48\x8b\x05',data+264);c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+272)
    c.emit(bytes.fromhex('48 8b 87 38 07 00 00'));ne(b'\x48\x3b\x05',data+280)
    c.relative(b'\x48\x8b\x05',data+288);c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+296)
    c.relative(b'\x48\x8b\x0d',data+96)
    c.emit(bytes.fromhex('48 8b 81 b8 00 00 00'));ne(b'\x48\x3b\x05',data+16)
    c.emit(bytes.fromhex('8b 81 80 00 00 00'));ne(b'\x3b\x05',data+120)
    # Effective backend properties for R3 as well as R4.
    c.relative(b'\x48\x8b\x0d',data+104)
    for offset,target in ((0x74,0x2b0),(0x76,0x2b4),(0x78,0x2b8)):
        c.emit(bytes.fromhex('0f b7 81')+struct.pack('<i',offset));c.relative(b'\x89\x05',w+target)
    # R3 never changes the native stack; records still identify failed qualification.
    fail(1);c.relative(b'\x83\x3d',data+4,b'\x01');c.relative(b'\x0f\x85','publish')
    fail(4)
    c.emit(b'\x48\xb8'+struct.pack('<Q',base+0x1783F41))
    ins_ne('48 39 85 68 41 00 00')
    for code in ('80 be c8 60 00 00 01','83 be 5c 67 00 00 00','80 be 30 67 00 00 00',
                 '83 be 18 73 00 00 00','83 be 30 64 00 00 00','80 be e0 73 00 00 00',
                 '80 be ed 76 00 00 00','80 be e4 6c 00 00 00','80 be 40 77 00 00 00'):
        if allow_planetary_flight and code==LOW_HEIGHT:low_height_gate(c,data,'publish','low_height_ok')
        else:ins_ne(code)
    c.emit(bytes.fromhex('f6 42 01 18'));c.relative(b'\x0f\x85','publish')
    c.emit(bytes.fromhex('49 83 b8 38 0a 00 00 01'));c.relative(b'\x0f\x85','publish')
    c.relative(b'\x48\x8b\x05',base+0x6f5f868);ne(b'\x48\x3b\x05',data+80)
    for code in ('83 b8 98 bd 89 00 00','80 b8 6d aa 57 00 00','83 b8 dc b6 72 00 00','83 b8 cc b6 72 00 00'):
        if allow_combat_flight and code=='80 b8 6d aa 57 00 00':
            # Known manual-combat byte only. Unknown unsigned values stay native.
            c.emit(bytes.fromhex('80 b8 6d aa 57 00 01'));c.relative(b'\x0f\x87','publish')
        else:ins_ne(code)
    c.emit(bytes.fromhex('48 8b 86 c0 60 00 00'));ne(b'\x48\x3b\x05',data+168)
    c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+176)
    # The class owner and its actual cGcSpaceshipClasses enum are pinned.
    c.emit(bytes.fromhex('48 8b 46 30'));ne(b'\x48\x3b\x05',data+312)
    c.emit(bytes.fromhex('8b 80 d0 00 00 00'));ne(b'\x3b\x05',data+320)
    # Backend gravity callback registration is an independent effective property.
    c.emit(bytes.fromhex('48 8b 87 b0 0b 00 00'));ne(b'\x48\x3b\x05',data+328)
    c.emit(bytes.fromhex('48 89 c1 8b 81 7c 0f 00 00'));ne(b'\x3b\x05',data+336)
    c.emit(bytes.fromhex('8b 81 78 0f 00 00'));ne(b'\x3b\x05',data+340)
    for index in range(16):
        c.relative(b'\x83\x3d',data+336,bytes([index]));c.relative(b'\x0f\x86','callbacks_checked')
        c.emit(b'\x48\x8b\x81'+struct.pack('<I',0xf80+16*index));ne(b'\x48\x3b\x05',data+344+16*index)
        c.emit(b'\x48\x8b\x81'+struct.pack('<I',0xf88+16*index));ne(b'\x48\x3b\x05',data+352+16*index)
    c.label('callbacks_checked')
    fail(5)
    # Finite positive mass and effective native time. Bit ordering rejects NaN/Inf.
    for address in (w+0x118,w+0x154,w+0x158):
        c.relative(b'\x8b\x05',address);c.emit(bytes.fromhex('85 c0'));c.relative(b'\x0f\x8e','publish')
        c.emit(bytes.fromhex('3d 00 00 80 7f'));c.relative(b'\x0f\x83','publish')
    c.relative(b'\xf3\x0f\x10\x05',w+0x154);c.relative(b'\xf3\x0f\x59\x05',w+0x158)
    c.emit(bytes.fromhex('66 0f 7e c0 85 c0'));c.relative(b'\x0f\x8e','publish')
    c.emit(bytes.fromhex('3d 00 00 80 7f'));c.relative(b'\x0f\x83','publish')
    c.relative(b'\x8b\x05',w+0x15c);c.emit(bytes.fromhex('f7 c0 00 ff 00 00'));c.relative(b'\x0f\x84','clock_running')
    c.emit(bytes.fromhex('f7 c0 00 00 ff 00'));c.relative(b'\x0f\x84','publish')
    c.label('clock_running')
    # Actual backend identity and properties. Pinned motion records cannot be replaced.
    c.emit(bytes.fromhex('49 8b 80 80 03 00 00'));ne(b'\x48\x3b\x05',data+88)
    c.emit(bytes.fromhex('48 89 c7 48 8b 40 40'));ne(b'\x48\x3b\x05',data+256)
    c.relative(b'\x48\x8b\x05',data+264);c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+272)
    c.emit(bytes.fromhex('48 8b 87 38 07 00 00'));ne(b'\x48\x3b\x05',data+280)
    c.relative(b'\x48\x8b\x05',data+288);c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+296)
    c.relative(b'\x48\x8b\x0d',data+96)
    c.emit(bytes.fromhex('48 8b 81 b8 00 00 00'));ne(b'\x48\x3b\x05',data+16)
    c.emit(bytes.fromhex('8b 81 80 00 00 00'));ne(b'\x3b\x05',data+120)
    c.emit(bytes.fromhex('8b 41 54'));ne(b'\x3b\x05',data+124)
    c.relative(b'\x48\x8b\x0d',data+104)
    for off in (0x74,0x76):
        c.emit(bytes.fromhex('66 83 b9')+struct.pack('<i',off)+b'\x00');c.relative(b'\x0f\x85','publish')
    for off in (0x270,0x274):zero_bits(13,off)
    # Pin gravity; default all-zero requirement. Nonzero requires explicit R3 qualification.
    c.emit(bytes.fromhex('0f b7 41 78'));ne(b'\x3b\x05',data+188)
    c.emit(bytes.fromhex('41 8b 85 7c 02 00 00'));ne(b'\x3b\x05',data+184)
    c.emit(bytes.fromhex('41 8b 85 38 03 00 00'));ne(b'\x3b\x05',data+192)
    for i in range(3):
        c.emit(bytes.fromhex('41 8b 85')+struct.pack('<I',0x340+4*i));ne(b'\x3b\x05',data+196+4*i)
    c.relative(b'\x83\x3d',data+212,b'\x01');c.relative(b'\x0f\x84','gravity_qualified')
    zero_bits(13,0x27c)
    c.emit(bytes.fromhex('66 83 79 78 00'));c.relative(b'\x0f\x85','publish')
    c.emit(bytes.fromhex('41 83 bd 38 03 00 00 00'));c.relative(b'\x0f\x85','publish')
    c.label('gravity_qualified')
    fail(6);c.relative(b'\x8b\x05',data+60)
    c.emit(b'\x25'+struct.pack('<I',REQUIRED)+b'\x3d'+struct.pack('<I',REQUIRED))
    c.relative(b'\x0f\x85','publish')
    fail(7)
    # Keep physical avoidance outside this seam. Its receipt is captured, but
    # an avoidance impulse must not re-enable the ordinary speed follower.
    if allow_planetary_flight:planetary_skip(c,data,'height_tolerated')
    for term in ('height',):
        for i in range(3):
            c.relative(b'\x8b\x05',w+VECTORS[term]+4*i)
            c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0'));c.relative(b'\x0f\x85','publish')
    if allow_planetary_flight:c.label('height_tolerated')
    fail(8)
    # Production admits finite inherited overspeed; research-only R4 speed
    # bound must not re-enable the native follower and bleed existing momentum.
    for i in range(3):
        c.relative(b'\x8b\x05',w+112+4*i);c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 7f'))
        c.relative(b'\x0f\x83','publish')
        c.relative(b'\x8b\x05',w+VECTORS['gravity']+4*i);c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 7f'))
        c.relative(b'\x0f\x83','publish')
    # The native clock, not the Python publisher, decides stale-command coast.
    # Clear helper output every invocation so a partial/no-write callback is safe.
    for i in range(3):c.relative(b'\xc7\x05',data+COMMAND+4*i,bytes(4))
    c.emit(b'\x48\xb8'+struct.pack('<Q',get_tick)+b'\xff\xd0')
    c.relative(b'\x48\x3b\x05',data+72);c.relative(b'\x0f\x83','expired')
    c.emit(b'\x48\xb9'+struct.pack('<Q',w))
    c.emit(b'\x48\xba'+struct.pack('<Q',data+COMMAND))
    c.emit(b'\x48\xb8'+struct.pack('<Q',helper)+b'\xff\xd0')
    c.emit(bytes.fromhex('83 f8 02'));c.relative(b'\x0f\x84','helper_native')
    c.emit(bytes.fromhex('83 f8 01'));c.relative(b'\x0f\x84','helper_apply')
    c.emit(bytes.fromhex('85 c0'));c.relative(b'\x0f\x85','helper_invalid')
    reason(10);c.relative(b'\xe9','zero_command')
    c.label('helper_native');reason(11);c.relative(b'\xe9','publish')
    c.label('helper_invalid');reason(13);c.relative(b'\xe9','publish')
    c.label('helper_apply');reason(9)
    for i in range(3):
        c.relative(b'\x8b\x05',data+COMMAND+4*i)
        c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 7f'));c.relative(b'\x0f\x83','acceleration_invalid')
        c.relative(b'\x3b\x05',data+COMMAND+16);c.relative(b'\x0f\x87','acceleration_invalid')
    c.relative(b'\xe9','deadline_recheck')
    c.label('acceleration_invalid');reason(12);c.relative(b'\xe9','zero_command')
    c.label('deadline_recheck')
    c.emit(b'\x48\xb8'+struct.pack('<Q',get_tick)+b'\xff\xd0')
    c.relative(b'\x48\x3b\x05',data+72);c.relative(b'\x0f\x82','write_gate')
    c.label('expired');reason(2)
    c.label('zero_command')
    for i in range(3):c.relative(b'\xc7\x05',data+COMMAND+4*i,bytes(4))
    c.label('write_gate')
    c.relative(b'\x8b\x05',w+60);c.relative(b'\x89\x05',w+68)
    reason(1)
    ne(b'\x83\x3d',data,b'\x01');ne(b'\x83\x3d',data+4,b'\x01')
    # Recheck live own identity/mode immediately before stack XYZ writes.
    reason(3)
    c.emit(bytes.fromhex('48 8b 86 58 62 00 00'));ne(b'\x48\x3b\x05',data+160)
    c.emit(bytes.fromhex('49 8b 85 90 02 00 00'));ne(b'\x48\x3b\x05',data+24)
    c.emit(bytes.fromhex('48 8b 40 08'));ne(b'\x48\x3b\x05',data+32)
    c.emit(bytes.fromhex('49 8b 85 28 03 00 00'));ne(b'\x48\x3b\x05',data+32)
    c.relative(b'\x48\x8b\x05',base+0x6f5f868);ne(b'\x48\x3b\x05',data+80)
    c.emit(bytes.fromhex('48 8b 80 48 cf 72 00'));ne(b'\x48\x3b\x05',data+304)
    c.emit(bytes.fromhex('48 8b 46 30'));ne(b'\x48\x3b\x05',data+312)
    c.emit(bytes.fromhex('8b 80 d0 00 00 00'));ne(b'\x3b\x05',data+320)
    reason(4)
    if allow_combat_flight:
        # Re-read every protected mode after the helper; an earlier C1 snapshot
        # must not authorize writes after a power/planet/pulse/frontend change.
        for code in ('80 be c8 60 00 00 01','83 be 5c 67 00 00 00','80 be 30 67 00 00 00',
                     '83 be 18 73 00 00 00','83 be 30 64 00 00 00','80 be e0 73 00 00 00',
                     '80 be ed 76 00 00 00','80 be e4 6c 00 00 00','80 be 40 77 00 00 00'):
            if allow_planetary_flight and code==LOW_HEIGHT:low_height_gate(c,data,'publish','low_height_recheck_ok')
            else:ins_ne(code)
        c.emit(bytes.fromhex('49 8b 95 90 02 00 00 f6 42 01 18'));c.relative(b'\x0f\x85','publish')
        c.emit(bytes.fromhex('4d 8b 85 28 03 00 00 49 83 b8 38 0a 00 00 01'));c.relative(b'\x0f\x85','publish')
        c.relative(b'\x48\x8b\x05',base+0x6f5f868);ne(b'\x48\x3b\x05',data+80)
        ins_ne('83 b8 98 bd 89 00 00')
        c.emit(bytes.fromhex('80 b8 6d aa 57 00 01'));c.relative(b'\x0f\x87','publish')
        ins_ne('83 b8 dc b6 72 00 00');ins_ne('83 b8 cc b6 72 00 00')
    else:
        # Preserve the research/default cave bytes and its original admission.
        for code in ('83 be 18 73 00 00 00','83 be 30 64 00 00 00','80 be e4 6c 00 00 00',
                     '80 be ed 76 00 00 00','80 be 40 77 00 00 00'):
            ins_ne(code)
    # Three scalar stores: no native W, velocity or angular state is written.
    def stores(height):
        for i in range(3):
            c.relative(b'\xf3\x0f\x10\x05',data+COMMAND+4*i)
            c.relative(b'\xf3\x0f\x58\x05',w+VECTORS['gravity']+4*i)
            # T88: the native ground-clearance push stays as a safety net (zero unless tolerated above).
            if height:c.relative(b'\xf3\x0f\x58\x05',w+VECTORS['height']+4*i)
            c.emit(b'\xf3\x0f\x11\x85'+struct.pack('<i',0x50+4*i))
    if allow_planetary_flight:
        # T101: gear down, Sultan flies to touchdown; the ground push stays out.
        c.relative(b'\x83\x3d',data+GEAR,b'\x01');c.relative(b'\x0f\x84','without_height')
        # T93: over water with WATER on the sea is Sultan's; the height term (sea as ground) stays out.
        c.relative(b'\x83\x3d',data+WATER,b'\x01');c.relative(b'\x0f\x85','with_height')
        c.emit(bytes.fromhex('80 be f0 62 00 00 00'));c.relative(b'\x0f\x84','with_height')
        c.label('without_height');stores(False);c.relative(b'\xe9','stored')
        c.label('with_height');stores(True);c.label('stored')
    else:stores(False)
    c.relative(b'\x8b\x05',w+68);c.relative(b'\x89\x05',w+60)
    _stack_vector(c,0x50,w+96)
    c.relative(b'\xc7\x05',w+64,struct.pack('<I',1))
    c.relative(b'\x48\xff\x05',data+216)
    c.label('publish')
    if allow_planetary_flight:
        # T93: a native-fallback frame over water keeps the native command with its height term capped at
        # WATER_PUSH. Applied frames (WORK+64) and landing stay as they are.
        h=w+VECTORS['height']
        c.relative(b'\x83\x3d',w+64,b'\x01');c.relative(b'\x0f\x84','water_done')
        c.relative(b'\x83\x3d',data+WATER,b'\x01');c.relative(b'\x0f\x85','water_done')
        c.emit(bytes.fromhex('80 be f0 62 00 00 00'));c.relative(b'\x0f\x84','water_done')
        c.emit(bytes.fromhex('83 be 18 73 00 00 00'));c.relative(b'\x0f\x85','water_done')
        c.relative(b'\xf3\x0f\x10\x05',h);c.emit(bytes.fromhex('f3 0f 59 c0'))  # xmm0 = hx*hx
        for i in (1,2):  # xmm0 += h[i]*h[i]
            c.relative(b'\xf3\x0f\x10\x0d',h+4*i);c.emit(bytes.fromhex('f3 0f 59 c9 f3 0f 58 c1'))
        c.relative(b'\xf3\x0f\x10\x0d',data+WATER_PUSH);c.emit(bytes.fromhex('f3 0f 59 c9'))  # xmm1 = cap^2
        c.emit(bytes.fromhex('0f 2f c1'));c.relative(b'\x0f\x86','water_done')  # |h|^2 <= cap^2, or NaN
        c.emit(bytes.fromhex('f3 0f 51 c0'))  # |h|
        c.relative(b'\xf3\x0f\x10\x0d',data+WATER_PUSH);c.emit(bytes.fromhex('f3 0f 5e c8'))  # k = cap/|h|
        c.emit(bytes.fromhex('b8 00 00 80 3f 66 0f 6e d0 f3 0f 5c ca'))  # k-1
        for i in range(3):  # stack += h*(k-1): the native command keeps a capped height term
            c.relative(b'\xf3\x0f\x10\x05',h+4*i);c.emit(bytes.fromhex('f3 0f 59 c1'))
            c.emit(b'\xf3\x0f\x58\x85'+struct.pack('<i',0x50+4*i))
            c.emit(b'\xf3\x0f\x11\x85'+struct.pack('<i',0x50+4*i))
        c.label('water_done')
    # Associate assist telemetry with this sealed invocation, not a later live
    # counter sample which could already belong to the next flight frame.
    c.relative(b'\x48\x8b\x05',data+AUTOBANK_COUNT);c.relative(b'\x48\x89\x05',w+0x318)
    c.relative(b'\x48\x8b\x05',data+AUTOBANK_INVOCATION);c.relative(b'\x48\x89\x05',w+0x320)
    # Publish one immutable completed record under per-slot seqlock, ring overwrite detectable.
    c.relative(b'\x48\xff\x05',data+128)
    c.relative(b'\x48\x8b\x05',data+128)
    c.emit(bytes.fromhex('48 89 c2 48 25')+struct.pack('<I',CAPACITY-1)+bytes.fromhex('48 c1 e0 0b'))
    c.emit(b'\x48\xbf'+struct.pack('<Q',data+RING)+bytes.fromhex('48 01 c7 48 89 d0 48 d1 e0 48 ff c8 48 89 07'))
    c.emit(bytes.fromhex('48 89 87 f8 07 00 00 48 83 c7 10'))
    c.emit(b'\x48\xbe'+struct.pack('<Q',w)+b'\xb9'+struct.pack('<I',WORK_SIZE//8)+bytes.fromhex('fc f3 48 a5'))
    c.emit(bytes.fromhex('48 ff c0 48 89 87 e8 03 00 00 48 89 87 f0 fb ff ff'))
    # RDI now slot+410: end at+7F8 => +3E8; start offset -410 => -410.
    # Publish the sealed command receipt immediately, including failed identity
    # gates. A later valid after-integrator hook upgrades it under the seqlock.
    # Otherwise a body migration could leave the reader showing an old success.
    c.relative(b'\x48\x89\x15',data+136)
    c.relative(b'\x48\x8b\x05',data+48);c.relative(b'\x48\x89\x05',data+240)
    c.label('out');_restore(c)
    c.emit(bytes.fromhex(site['original']));c.relative(b'\xe9',site['address']+len(bytes.fromhex(site['original'])))
    result=c.finish()
    if len(result)>0x1f00:raise ValueError(f'Final cave overflow: {len(result)}')
    return result

# T88 auto-level. With raw roll input inside a 1.5e-5 band (0x175D678..0x175D69F) native computes its
# own roll, -[rbp+0x4178]*gain (0x175DB8A..0x175DB9D, near-planet gain via 0x1675810), into the XMM7 that
# the roll target reads at 0x175DCE9: with the stick centred it rolls the ship upright, fighting a held
# bank. Static 2026-10-19 (claude), build 180836. The site is `test al,al; jne 0x175DC98`; taking that
# jump is native's own pilot-is-rolling path. Planetary caves only; signature unique, no jump into it.
AUTO_LEVEL_OFF=0x3F8
LEVEL_SITE={'rva':0x179089b,'size':8,'original':'84c00f85f4050000',
            'signature':'84c00f85f4050000f30f1005c55eb603488d9510380000f3',
            'instructions':['0x1790a7b: test al, al','0x1790a7d: jne 0x1791077']}
LEVEL_SKIP=0x1790E97-0x179089b
# Water auto-level: with ship+0x6720 (low height) and ship+0x62E0 (over water within AutoLevelWaterMargin
# 90 of the sea surface, set at 0x1756F50..0x1756FB8) native measures tilt from planet up and past
# AutoLevelWaterAngle 45 (globals+0x1288, 0x175E974) torques the ship back (0x1055790, AutoLevelWaterTorque
# globals+0x1290): the 45-degree bank wall at sea level. Site `cmp byte [rsi+0x62E0],0; je 0x175E9E9`;
# taking the jump is native's own not-over-water path. Same switch as LEVEL_SITE. Static 2026-10-19 (claude).
WATER_SITE={'rva':0x1791943,'size':13,'original':'80bef0620000000f84bc020000',
            'signature':'80bef0620000000f84bc020000488d95a0390000498bcde8',
            'instructions':['0x1791b23: cmp byte ptr [rsi + 0x62f0], 0','0x1791b2a: je 0x1791dec']}
WATER_SKIP=0x1791c0c-0x1791943
# T101 terrain clearance, 183791: UpdateControlled stores min(height above sea, above the terrain ray hit) to
# [rbp-0x60] at 0x178A0BB ([rbp+0x18] at 0x1756F9C on 180836). 7.10 reuses that slot for a damping lerp
# (0x17932D2) on some frames before the final site, so the value is copied into the work record here, after
# the latch has cleared it. Planetary caves only; signature unique, no jump into it.
CLEARANCE_SITE={'rva':0x1789edb,'size':5,'original':'f30f1175a0',
                'signature':'f30f1175a07213f3410f108424d83100000f2fc17204b001',
                'instructions':['0x178a0bb: movss dword ptr [rbp - 0x60], xmm6']}

def _skip_gates(c,data):
    """Sultan planetary flight owns this ship: switch, auto-level off, bridge enabled and overriding,
    qualified ship, not landing. Falls through when all hold; otherwise jumps to 'native'."""
    c.relative(b'\x83\x3d',data+PLANETARY,b'\x01');c.relative(b'\x0f\x85','native')
    c.relative(b'\x83\x3d',data+AUTO_LEVEL_OFF,b'\x01');c.relative(b'\x0f\x85','native')
    c.relative(b'\x83\x3d',data,b'\x01');c.relative(b'\x0f\x85','native')
    c.relative(b'\x83\x3d',data+4,b'\x01');c.relative(b'\x0f\x85','native')
    c.relative(b'\x48\x3b\x35',data+8);c.relative(b'\x0f\x85','native')
    c.emit(bytes.fromhex('83 be 18 73 00 00 00'));c.relative(b'\x0f\x85','native')  # landing state native

def level_code(cave,site,data):
    """Skip native roll auto-level under _skip_gates; otherwise the original test and branch."""
    c=Code(cave);skip=site['address']+LEVEL_SKIP
    _skip_gates(c,data)
    c.relative(b'\xe9',skip)
    c.label('native')
    c.emit(bytes.fromhex('84 c0'));c.relative(b'\x0f\x85',skip)
    c.relative(b'\xe9',site['address']+site['size'])
    return c.finish()

def water_code(cave,site,data):
    """Skip native water auto-level under _skip_gates; otherwise the original compare and branch."""
    c=Code(cave);skip=site['address']+WATER_SKIP
    _skip_gates(c,data)
    c.relative(b'\xe9',skip)
    c.label('native')
    c.emit(bytes.fromhex('80 be f0 62 00 00 00'));c.relative(b'\x0f\x84',skip)
    c.relative(b'\xe9',site['address']+site['size'])
    return c.finish()

def clearance_code(cave,site,data):
    """Copy native terrain clearance XMM6 into the work record for the qualified ship and frame with the planetary
    switch on; then the native store. Flags are live into the next JB, so they're saved first."""
    c=Code(cave)
    c.emit(b'\x9c')  # pushfq: the native JB after this site reads COMISS flags
    c.relative(b'\x83\x3d',data+PLANETARY,b'\x01');c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x3b\x35',data+8);c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x3b\x2d',data+40);c.relative(b'\x0f\x85','out')
    c.relative(b'\xf3\x0f\x11\x35',data+WORK+CLEARANCE)  # movss [work+CLEARANCE],xmm6
    c.label('out');c.emit(b'\x9d')
    c.emit(bytes.fromhex(site['original']))
    c.relative(b'\xe9',site['address']+site['size'])
    return c.finish()

def clamp_code(cave,site,data):
    """Native load first; then, only on a frame whose Sultan command this invocation applied (latch-cleared
    WORK+64) for the qualified ship and frame with the planetary switch on, raise it to CLAMP_HALF."""
    c=Code(cave)
    c.emit(bytes.fromhex(site['original']))
    c.emit(bytes.fromhex('9c 50'))  # pushfq; push rax: flags and RAX untouched for native code
    c.relative(b'\x83\x3d',data+PLANETARY,b'\x01');c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x3b\x35',data+8);c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x3b\x2d',data+40);c.relative(b'\x0f\x85','out')
    c.relative(b'\x83\x3d',data+WORK+64,b'\x01');c.relative(b'\x0f\x85','out')
    c.relative(b'\xf3\x0f\x5f\x15',data+CLAMP_HALF)  # maxss xmm2,[CLAMP_HALF]
    c.label('out');c.emit(bytes.fromhex('58 9d'))
    c.relative(b'\xe9',site['address']+site['size'])
    return c.finish()

def after_integrator_code(cave,site,data,base):
    c=Code(cave);_save(c);_owner_guard(c,data)
    c.emit(b'\x48\xb8'+struct.pack('<Q',base+0x1783F41))
    c.emit(bytes.fromhex('48 39 85 68 41 00 00'));c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x8b\x05',data+48)
    _check(c,b'\x48\x3b\x05',data+240)
    c.emit(bytes.fromhex('49 8b 85 90 02 00 00'));_check(c,b'\x48\x3b\x05',data+24)
    c.emit(bytes.fromhex('48 89 c1')) # current S
    c.emit(bytes.fromhex('48 8b 41 08'));_check(c,b'\x48\x3b\x05',data+32)
    c.emit(bytes.fromhex('49 8b 85 28 03 00 00'));_check(c,b'\x48\x3b\x05',data+32)
    c.relative(b'\x48\x8b\x15',data+128)
    c.emit(bytes.fromhex('48 89 d0 48 25')+struct.pack('<I',CAPACITY-1)+bytes.fromhex('48 c1 e0 0b'))
    c.emit(b'\x48\xbf'+struct.pack('<Q',data+RING)+bytes.fromhex('48 01 c7'))
    c.emit(bytes.fromhex('48 89 d0 48 d1 e0 48 ff c8 48 89 07 48 89 87 f8 07 00 00'))
    c.emit(bytes.fromhex('0f 10 41 60 0f 11 87 10 03 00 00')) # payload+300
    c.emit(bytes.fromhex('c7 87 20 03 00 00 01 00 00 00')) # payload+310 post_valid
    c.emit(bytes.fromhex('48 ff c0 48 89 87 f8 07 00 00 48 89 07'))
    c.relative(b'\x48\x89\x15',data+136)
    c.relative(b'\x48\xff\x05',data+224)
    c.label('out');_restore(c)
    # Displaced MOV RCX,[RIP+57CCFB6] explicitly relocated, target module 6F60828 (183791).
    c.relative(b'\x48\x8b\x0d',base+0x6f5f868)
    c.relative(b'\xe9',site['address']+site['size'])
    return c.finish()

def decode_record(raw):
    if len(raw)!=SLOT:raise ValueError('Wrong ring slot size')
    seq=struct.unpack_from('<Q',raw,0)[0]
    if seq==0 or seq&1 or seq!=struct.unpack_from('<Q',raw,SLOT-8)[0]:return None
    p=raw[16:16+WORK_SIZE]
    out={name:struct.unpack_from(fmt,p,offset)[0] for name,fmt,offset in
         (('invocation','<Q',0),('frame','<Q',8),('ship','<Q',16),('body','<Q',24),
          ('state','<Q',32),('context','<Q',40),('tick','<Q',48),('visits','<I',56),('reason','<I',60),('applied','<I',64),
          ('context_id','<Q',0x110),('mass','<f',0x118),('linear_damping','<f',0x120),
          ('angular_damping','<f',0x11c),('gravity_cached','<f',0x124),
          ('state_flags_raw','<I',0x12c),('boost_max_speed','<f',0x130),
          ('thrust_force','<f',0x134),('boost_thrust_force','<f',0x138),
          ('landing','<I',0x13c),('alignment','<I',0x140),('native_u','<f',0x150),
          ('time_step','<f',0x154),('slow_factor','<f',0x158),('clock_flags','<I',0x15c))}
    for name,off in (('final_before',80),('final_after',96),('stored_velocity',112),
                     ('angular_velocity',128),('right',144),('up',160),('forward',176),
                     ('context_q0',192),('context_q1',208),('context_q2',224),
                     ('frame_linear',240),('frame_angular',256)):
        out[name]=list(struct.unpack_from('<4f',p,off))
    out['terms']={name:list(struct.unpack_from('<4f',p,off)) for name,off in VECTORS.items()}
    # Production never detours the shared avoidance helper. The native per-ship
    # flag below reports whether it fired; an absent vector receipt is unknown.
    if not out['visits']&BITS['avoidance']:out['terms']['avoidance']=None
    out['terms']['overspeed']=[-v for v in out['terms']['overspeed_subtrahend']]
    out['terms']['reverse']=[a-b for a,b in zip(out['terms']['after_reverse'],out['terms']['direction_blend'])]
    for name,offset in (('debug',0x144),('boost',0x148),('avoidance_flag',0x14c),
                        ('height_mode',0x168),('combat_follow',0x16c),('crash',0x170)):
        out[name]=p[offset]
    out['normal_674c']=struct.unpack_from('<I',p,0x164)[0]
    for name,offset in (('backend_linear_damping',0x2b0),('backend_angular_damping',0x2b4),('backend_gravity_factor',0x2b8)):
        word=struct.unpack_from('<I',p,offset)[0];out[name]=struct.unpack('<f',struct.pack('<I',word<<16))[0]
    flags=out['clock_flags'];paused=(flags>>8)&255;realtime=(flags>>16)&255
    out['dt_eff']=0. if paused and not realtime else out['time_step']*out['slow_factor']
    out['outer_velocity']=out['stored_velocity'][:3] if out['context_id']==1 else None
    out['post_valid']=bool(struct.unpack_from('<I',p,0x310)[0])
    out['stored_velocity_after_integrator']=list(struct.unpack_from('<4f',p,0x300))
    out['outer_velocity_after_integrator']=(out['stored_velocity_after_integrator'][:3]
        if out['context_id']==1 and out['post_valid'] else None)
    out['auto_bank_suppression_count']=struct.unpack_from('<Q',p,0x318)[0]
    assist_invocation=struct.unpack_from('<Q',p,0x320)[0]
    out['auto_bank_suppressed']=bool(out['auto_bank_suppression_count'] and assist_invocation==out['invocation'])
    low,high=struct.unpack_from('<3f',p,0x2e0),struct.unpack_from('<3f',p,0x2f0)
    out['position']=[a+b for a,b in zip(low,high)]  # S+0x40/0x50 split world position
    out['clearance']=struct.unpack_from('<f',p,CLEARANCE)[0]  # T101, planetary caves only (0 otherwise)
    out['record_sequence']=seq//2
    return out

def initial_state(fixture,deadline,lease):
    """All pointer values must come from the independent native attachment/body snapshot."""
    b=bytearray(RING)
    for name in ('ship','body','state','context','application','backend','body_record',
                 'motion','handle','physics','controller_wrapper','controller','body_pages',
                 'body_page_entry','body_page','motion_pages','motion_page_entry','motion_page','player_attachment',
                 'class_owner','modifier_manager'):
        struct.pack_into('<Q',b,FIELDS[name],int(fixture[name]))
    for name in ('motion_id','body_flags','gravity_cached_bits','gravity_factor_word','custom_flag',
                 'ship_class','callback_count','callback_mask'):
        struct.pack_into('<I',b,FIELDS[name],int(fixture[name]))
    if 'custom_vector_bits' in fixture:struct.pack_into('<3I',b,196,*fixture['custom_vector_bits'][:3])
    else:struct.pack_into('<3f',b,196,*fixture['custom_vector'][:3])
    if not 0<fixture['speed_bound']<2*fixture['boost_max_speed']:raise ValueError('Unsafe native-clamp bound')
    struct.pack_into('<f',b,232,fixture['speed_bound']**2)
    struct.pack_into('<f',b,236,fixture['boost_max_speed'])
    struct.pack_into('<Q',b,64,deadline);struct.pack_into('<Q',b,72,lease)
    if fixture['ship_class'] not in SUPPORTED_CLASSES:raise ValueError('Class requires native flight')
    if not 0<=fixture['callback_count']==len(fixture['callbacks'])<=16:
        raise ValueError('Invalid bounded callback fixture')
    for index,row in enumerate(fixture['callbacks']):
        struct.pack_into('<QQ',b,344+16*index,*row)
    # Nonzero gravity must have been independently admitted by the builder.
    struct.pack_into('<I',b,212,int(bool(fixture.get('allow_nonzero_gravity',False))))
    struct.pack_into('<f',b,COMMAND+16,1e6)
    struct.pack_into('<f',b,WATER_PUSH,DEFAULT_WATER_PUSH)
    from native_autobank import AUTOBANK_SUPPRESS
    struct.pack_into('<I',b,AUTOBANK_SUPPRESS,1) # F13 default: yaw assist off
    return bytes(b)

def fixture_from_scene(scene,read_bytes,module_base,speed_bound):
    """Extend the independent local-player scene snapshot with exact pinned page fields.

    Read-only. Does not select a player from generic active pool membership.
    Caller should save both scene and this fixture and repeat before installation.
    """
    if scene.get('selection')!='native_local_player_ship':raise ValueError('No positively selected native local-player ship')
    p=scene.get('physics')
    if not isinstance(p,dict):raise ValueError('Effective physics snapshot missing')
    def ptr(v):return int(v,16) if isinstance(v,str) else int(v)
    def raw(address,size):
        b=read_bytes(address,size)
        if len(b)!=size:raise ValueError('Short fixture read')
        return b
    def q(a):return struct.unpack('<Q',raw(a,8))[0]
    def u(a):return struct.unpack('<I',raw(a,4))[0]
    b=ptr(p['rigid']);s=ptr(p['state']);c=ptr(p['context']);h=ptr(p['backend'])
    bodyid=p['body_id'];mid=p['motion_id'];bodypages=q(h+0x40);motionpages=q(h+0x738)
    be=bodypages+8*(bodyid//42);me=motionpages+8*(mid>>6)
    bp=q(be);mp=q(me);body=bp+0xc0*(bodyid%42);motion=mp+0x80*(mid&63)
    if body!=ptr(p['body_record']) or motion!=ptr(p['motion_record']) or q(body+0xb8)!=b:
        raise ValueError('Backend page/owner changed since independent snapshot')
    if q(s+8)!=c:raise ValueError('State context does not match rigid context')
    if p['context_id']!=1 or p['native_capped_integrator_rejects_state']:raise ValueError('Not primary dynamic fixture')
    for cb in p['gravity_callbacks']:
        if cb['body_mask']&2 and not cb['is_native_angular_factor_callback']:
            raise ValueError('Unknown matching gravity callback')
    identity=scene['identity'];ship=ptr(p['ship']);app=ptr(scene['manager'])
    attachment=ptr(scene['native_player_identity']['attachment_handle'])
    if q(app+0x72cf48)!=attachment:raise ValueError('Local player attachment changed')
    result=dict(ship=ship,body=b,state=s,context=c,application=app,backend=h,
                body_record=body,motion=motion,handle=ptr(p['native_handle']),motion_id=mid,
                body_flags=u(body+0x54),physics=ptr(p['physics']),
                controller_wrapper=ptr(identity['controller_wrapper']),controller=ptr(identity['controller']),
                body_pages=bodypages,body_page_entry=be,body_page=bp,motion_pages=motionpages,
                motion_page_entry=me,motion_page=mp,player_attachment=attachment,
                gravity_cached_bits=u(b+0x27c),gravity_factor_word=struct.unpack('<H',raw(motion+0x78,2))[0],
                custom_flag=u(b+0x338),custom_vector=list(struct.unpack('<4f',raw(b+0x340,16))),
                custom_vector_bits=list(struct.unpack('<3I',raw(b+0x340,12))),
                speed_bound=float(speed_bound),boost_max_speed=struct.unpack('<f',raw(ship+0x6650,4))[0])
    if q(ship+0x6258)!=result['physics'] or q(b+0x290)!=s or q(b+0x328)!=c or q(be)!=bp or q(me)!=mp:
        raise ValueError('Fixture identities changed while extending snapshot')
    return result

def _allocate_data(handle,site,size):
    lower=max(0x10000,(site-NEAR_DISTANCE)&~0xffff);upper=site+NEAR_DISTANCE;cursor=lower
    while cursor<upper:
        info=MemoryInfo()
        if not K.VirtualQueryEx(handle,cursor,C.byref(info),C.sizeof(info)):raise OSError('VirtualQueryEx')
        start=info.base or 0;end=start+info.region_size
        if end<=cursor:raise RuntimeError('Memory query did not advance')
        candidate=(max(start,cursor)+0xffff)&~0xffff
        if info.state==0x10000 and candidate+size<=min(end,upper):
            result=K.VirtualAllocEx(handle,candidate,size,0x3000,0x40)
            if result:return result
        cursor=end
    raise RuntimeError('No near allocation; nothing installed')

class IdentityError(RuntimeError):
    pass

def resolve_local_player_ship(read,module_base,*,max_pool_count=4096):
    def raw(a,n):
        if not isinstance(a,int) or a<0x10000 or n<0 or n>0x10000:raise IdentityError('invalid bounded read')
        b=bytes(read(a,n))
        if len(b)!=n:raise IdentityError('short read')
        return b
    def q(a):return struct.unpack('<Q',raw(a,8))[0]
    def u(a):return struct.unpack('<I',raw(a,4))[0]
    manager=q(module_base+0x6f5f868)
    player=manager+0x72cde0
    handle=q(player+0x168)
    if handle==0:raise IdentityError('native player has no attached spaceship handle')
    index=handle>>32
    if index>0xfeff:raise IdentityError('native player attachment index out of bounds')
    entity=module_base+0x5469f80+0x58*index
    if q(entity+0x50)!=handle:raise IdentityError('player attachment generation mismatch')
    component_type=struct.unpack('<h',raw(module_base+0x517e568,2))[0]
    if not 0<=component_type<256:raise IdentityError('spaceship component type outside attachment bitmask')
    mask_bytes=raw(entity+0x30,32)
    mask=int.from_bytes(mask_bytes,'little')
    if not (mask>>component_type)&1:raise IdentityError('player attachment lacks spaceship component bit')
    rank=(mask&((1<<component_type)-1)).bit_count()
    components=q(entity+0x20)
    ship=q(components+8*rank)
    if not ship:raise IdentityError('native player spaceship component null')
    pool=q(module_base+0x517e8f0)
    count=u(pool+4);array=q(pool+8)
    if not 0<count<=max_pool_count:raise IdentityError('spaceship pool count outside bounded snapshot')
    # 512-pointer reads preserve a modest upper bound on any single RPM request.
    pointers=[]
    for start in range(0,count,512):
        n=min(512,count-start)
        pointers.extend(struct.unpack('<'+'Q'*n,raw(array+8*start,8*n)))
    matches=[i for i,p in enumerate(pointers) if p==ship]
    if len(matches)!=1:raise IdentityError('native player ship not uniquely present in current spaceship pool')
    active=raw(ship+0x60c8,1)[0]
    wrapper=q(ship+0x60c0)
    controller=q(wrapper) if wrapper else 0
    if active!=1 or controller==0:raise IdentityError('native player ship controller inactive or invalid')
    vtable=q(controller)
    if not vtable:raise IdentityError('concrete controller vtable null')
    if (q(module_base+0x6f5f868)!=manager or q(player+0x168)!=handle or q(entity+0x50)!=handle
        or raw(entity+0x30,32)!=mask_bytes or q(entity+0x20)!=components
        or q(components+8*rank)!=ship or q(module_base+0x517e8f0)!=pool
        or u(pool+4)!=count or q(pool+8)!=array or q(array+8*matches[0])!=ship
        or q(ship+0x60c0)!=wrapper or q(wrapper)!=controller or raw(ship+0x60c8,1)[0]!=1):
        raise IdentityError('native player/attachment/pool identity changed during snapshot')
    return {'ship':hex(ship),'manager':hex(manager),'player':hex(player),'attachment_handle':hex(handle),
            'attachment_entry':hex(entity),'attachment_index':index,'spaceship_component_type':component_type,
            'component_rank':rank,'component_array':hex(components),'spaceship_pool':hex(pool),
            'pool_count':count,'pool_array':hex(array),'selected_pool_index':matches[0],
            'controller_active':active,'controller_wrapper':hex(wrapper),'concrete_controller':hex(controller),
            'concrete_controller_vtable':hex(vtable),'identity_method':'native14842c0 attachment component lookup + bounded pool cross-check',
            'build':'183877 / Steam26295378'}

class SnapshotError(RuntimeError):
    pass

def snapshot_physics(read, ship, module_base, *, max_hash_probes=256):
    def raw(addr,n):
        if not isinstance(addr,int) or addr<0x10000 or n<0 or n>0x1000:
            raise SnapshotError('invalid bounded read')
        data=bytes(read(addr,n))
        if len(data)!=n: raise SnapshotError('short read')
        return data
    def u64(a): return struct.unpack('<Q',raw(a,8))[0]
    def u32(a): return struct.unpack('<I',raw(a,4))[0]
    def f32(a): return struct.unpack('<f',raw(a,4))[0]
    def bf16(a): return struct.unpack('<f',struct.pack('<I',struct.unpack('<H',raw(a,2))[0]<<16))[0]
    def lookup(map_addr,key):
        head=raw(map_addr,24)
        buckets,mask,shift=struct.unpack_from('<QQI',head)
        if not buckets or mask>0x1000000 or shift>63:
            raise SnapshotError('invalid handle-map header')
        hashed=(key*0xff51afd7ed558ccd)&0xffffffffffffffff
        origin=hashed>>shift
        tag=(hashed>>56)&0xff
        for probe in range(min(mask+1,max_hash_probes)):
            addr=buckets+128*((origin+probe)&mask)
            data=raw(addr,128)
            occupied=struct.unpack_from('<Q',data,8)[0]
            for lane in range(1,8):
                if data[lane]!=tag or not ((occupied>>(8*lane))&1):continue
                k,v=struct.unpack_from('<QQ',data,16*lane)
                if k!=key:continue
                # Repeat the complete bounded bucket read to reject an unstable slot.
                if raw(addr,128)!=data:raise SnapshotError('handle-map bucket changed')
                return v
            ctrl=data[0]
            limit=ctrl if ctrl<0xcd else (1<<(ctrl-0xc5))-1
            if ctrl==0 or probe==limit:raise SnapshotError('native handle not found')
        raise SnapshotError('native handle lookup probe bound exceeded')
    physics=u64(ship+0x6258); rigid=physics+0x50
    state=u64(rigid+0x290); context=u64(rigid+0x328)
    backend=u64(context+0x380); manager=u64(backend+0xbb0)
    if u64(backend+0x20)!=module_base+0x3632618:
        raise SnapshotError('unrecognized backend body-accessor vtable')
    handle=u64(state+0x88)
    body_id=(handle if handle&(1<<63) else lookup(backend+0x1138,handle))&0xffffff
    if body_id==0xffffff:raise SnapshotError('invalid native body id')
    body_pages=u64(backend+0x40)
    body=u64(body_pages+8*(body_id//42))+0xc0*(body_id%42)
    wrapper=u64(body+0xb8)
    if wrapper!=rigid:raise SnapshotError('backend body user-data does not identify ship rigid wrapper')
    motion_id=u32(body+0x80)
    if motion_id==0xffffffff:raise SnapshotError('invalid native motion id')
    motion_pages=u64(backend+0x738)
    motion=u64(motion_pages+8*(motion_id>>6))+0x80*(motion_id&63)
    count=u32(manager+0xf7c)
    if count>16:raise SnapshotError('gravity callback count exceeds native bucket capacity16')
    callbacks=[]
    for i in range(count):
        obj=u64(manager+0xf88+16*i)
        callbacks.append({'index':i,'body_mask':u32(manager+0xf80+16*i),'object':hex(obj),
                          'vtable':hex(u64(obj)),
                          'is_native_angular_factor_callback':obj==module_base+0x50d9910 and u64(obj)==module_base+0x35d3a20})
    flags=raw(state,2)
    result={'ship':hex(ship),'physics':hex(physics),'rigid':hex(rigid),'state':hex(state),
            'context':hex(context),'context_id':u64(context+0xa38),'backend':hex(backend),
            'backend_interface_vtable':hex(u64(backend+0x20)),
            'backend_interface_expected':hex(module_base+0x3632618),
            'modifier_manager':hex(manager),'native_handle':hex(handle),'body_id':body_id,
            'body_record':hex(body),'body_wrapper':hex(wrapper),'body_flags54':u32(body+0x54),
            'motion_id':motion_id,'motion_record':hex(motion),'state_flags':list(flags),
            'native_capped_integrator_rejects_state':bool(flags[1]&0x18),
            'gravity_scalar_rigid27c':f32(rigid+0x27c),
            'linear_damping_rigid274':f32(rigid+0x274),'angular_damping_rigid270':f32(rigid+0x270),
            'packed_gravity_factor_motion78':bf16(motion+0x78),
            'packed_linear_damping_motion74':bf16(motion+0x74),'packed_angular_damping_motion76':bf16(motion+0x76),
            'custom_gravity_enabled':raw(rigid+0x338,1)[0],
            'custom_gravity_vector':list(struct.unpack('<4f',raw(rigid+0x340,16))),
            'gravity_callback_mask':u32(manager+0xf78),'gravity_callbacks':callbacks}
    if (u64(ship+0x6258)!=physics or u64(rigid+0x290)!=state or u64(rigid+0x328)!=context
        or u64(context+0x380)!=backend or u64(backend+0xbb0)!=manager
        or u64(state+0x88)!=handle or u32(body+0x80)!=motion_id or u64(body+0xb8)!=rigid
        or u32(manager+0xf7c)!=count):
        raise SnapshotError('ship body identity changed during snapshot')
    return result


def fixture_from_ship(pm, base, ship, *, speed_bound=None, allow_nonzero_gravity=False):
    """Read-only positive native-player identity and actual solver-body snapshot.

    Known native angular-factor callbacks are retained. Any unknown callback
    matching linear-body mask bit2 rejects qualification rather than assuming
    that rigid+27C describes all effective solver gravity.
    """
    read=pm.read_bytes if hasattr(pm,'read_bytes') else pm
    identity=resolve_local_player_ship(read,base)
    if int(identity['ship'],16)!=ship:raise IdentityError('Requested ship is not native player ship')
    p=snapshot_physics(read,ship,base)
    boost=struct.unpack('<f',read(ship+0x6650,4))[0]
    if not math.isfinite(boost) or boost<=0:raise SnapshotError('Invalid native speed bound')
    bound=1.9*boost if speed_bound is None else float(speed_bound)
    scene={'selection':'native_local_player_ship','physics':p,'identity':{
        'controller_wrapper':identity['controller_wrapper'],'controller':identity['concrete_controller']},
        'manager':identity['manager'],'native_player_identity':identity}
    result=fixture_from_scene(scene,read,base,bound)
    q=lambda address:struct.unpack('<Q',read(address,8))[0]
    u=lambda address:struct.unpack('<I',read(address,4))[0]
    class_owner=q(ship+0x30)
    if not class_owner:raise IdentityError('Ship class owner is null')
    ship_class=u(class_owner+0xd0)
    if ship_class not in SUPPORTED_CLASSES:raise IdentityError('Ship class requires native flight')
    manager=int(p['modifier_manager'],16)
    callbacks=[(q(manager+0xf80+16*i),q(manager+0xf88+16*i)) for i in range(len(p['gravity_callbacks']))]
    result.update(class_owner=class_owner,ship_class=ship_class,modifier_manager=manager,
                  callback_count=len(callbacks),callback_mask=u(manager+0xf78),callbacks=callbacks,
                  allow_nonzero_gravity=bool(allow_nonzero_gravity))
    if not allow_nonzero_gravity and (p['gravity_scalar_rigid27c']!=0 or
            p['packed_gravity_factor_motion78']!=0 or p['custom_gravity_enabled']):
        raise SnapshotError('Nonzero gravity needs explicit qualification')
    if (q(ship+0x30)!=class_owner or u(class_owner+0xd0)!=ship_class or
            u(manager+0xf7c)!=len(callbacks) or
            callbacks!=[(q(manager+0xf80+16*i),q(manager+0xf88+16*i)) for i in range(len(callbacks))]):
        raise SnapshotError('Class/gravity callback identity changed during snapshot')
    return result


def decode_work(raw):
    """Decode the immutable invocation snapshot passed to the helper."""
    if len(raw)!=WORK_SIZE:raise ValueError('Wrong work snapshot length')
    slot=bytearray(SLOT)
    struct.pack_into('<Q',slot,0,2);struct.pack_into('<Q',slot,SLOT-8,2)
    slot[16:16+WORK_SIZE]=raw
    return decode_record(slot)


def work_snapshot(address):
    return decode_work(C.string_at(address,WORK_SIZE))


class Bridge:
    """Prepare inert storage, install verified caves, publish a bounded lease.

    The caller owns executable-build qualification and the helper lifetime. The
    helper must remain strongly referenced until process exit; allocations are
    deliberately never freed underneath possibly executing native code.
    """
    def __init__(self,pm,pid,module_base,fixture,get_tick,helper_address,*,allow_combat_flight=False,
                 allow_planetary_flight=False):
        if not get_tick or not helper_address:raise ValueError('Native clock/helper required')
        self.pm,self.pid,self.base=pm,pid,module_base
        self.fixture,self.get_tick,self.helper=fixture,get_tick,helper_address
        self.allow_combat_flight=bool(allow_combat_flight)
        self.allow_planetary_flight=bool(allow_planetary_flight)
        self.items={};self.data=0;self.installed=False;self._last_sequence=0

    @staticmethod
    def _now():
        K.GetTickCount64.restype=C.c_uint64
        return int(K.GetTickCount64())

    def prepare(self):
        from native_autobank import assist_code
        if self.data:raise RuntimeError('Already prepared')
        manifest=site_manifest()
        if self.allow_planetary_flight:manifest['sites'].update(clamp=dict(CLAMP_SITE),level=dict(LEVEL_SITE),water=dict(WATER_SITE),
                                                                clearance=dict(CLEARANCE_SITE))
        for site in manifest['sites'].values():
            expected=bytes.fromhex(site['signature'])
            if self.pm.read_bytes(self.base+site['rva'],len(expected))!=expected:
                raise RuntimeError('Exact-build site signature mismatch')
        # Two pages per cave leave room for complete same-invocation gates.
        allocation_size=ALLOCATION+len(manifest['sites'])*0x2000
        self.data=_allocate_data(self.pm.process_handle,self.base+0x179364f,allocation_size)
        for index,(kind,v) in enumerate(manifest['sites'].items()):
            site=self.base+v['rva'];cave=self.data+ALLOCATION+index*0x2000
            patch=b'\xe9'+struct.pack('<i',cave-site-5)+b'\x90'*(v['size']-5)
            self.items[kind]=dict(v,address=site,cave=cave,patch=patch.hex())
        # Resolve all rel32 references before the first owned-memory write.
        codes={}
        for kind,site in self.items.items():
            cave=site['cave']
            code=(latch_code(cave,site,self.data,self.base) if kind=='latch' else
                  final_code(cave,site,self.data,self.base,self.get_tick,self.helper,
                             allow_combat_flight=self.allow_combat_flight,
                             allow_planetary_flight=self.allow_planetary_flight) if kind=='final' else
                  after_integrator_code(cave,site,self.data,self.base) if kind=='after_integrator' else
                  clamp_code(cave,site,self.data) if kind=='clamp' else
                  clearance_code(cave,site,self.data) if kind=='clearance' else
                  level_code(cave,site,self.data) if kind=='level' else
                  water_code(cave,site,self.data) if kind=='water' else
                  assist_code(cave,site,self.data,self.base,self.get_tick,
                              allow_combat_flight=self.allow_combat_flight,
                              allow_planetary_flight=self.allow_planetary_flight) if kind=='autobank' else
                  capture_code(kind,cave,site,self.data))
            if len(code)>0x2000:raise ValueError('Cave exceeds reserved pages')
            codes[kind]=code
        state=initial_state(self.fixture,0,0)
        self.pm.write_bytes(self.data,state,len(state))
        if self.pm.read_bytes(self.data,len(state))!=state:raise RuntimeError('Storage readback failed')
        for kind,code in codes.items():
            cave=self.items[kind]['cave'];self.pm.write_bytes(cave,code,len(code))
            if self.pm.read_bytes(cave,len(code))!=code:raise RuntimeError('Cave readback failed')
            if not K.FlushInstructionCache(self.pm.process_handle,cave,len(code)):raise OSError('FlushInstructionCache')
        return {'data':self.data,'build':manifest['build'],'allocation_size':allocation_size,
                'fixture':self.fixture,'sites':self.items,'retention':'until process exit'}

    def install(self,writer=replace_code):
        if not self.data:raise RuntimeError('Prepare first')
        try:
            for site in self.items.values():
                writer(self.pm,self.pid,site['address'],bytes.fromhex(site['original']),bytes.fromhex(site['patch']))
            self.pm.write_bytes(self.data,struct.pack('<I',1),4)
            self.installed=True
        except BaseException:
            self.restore(writer);raise

    def renew(self,lease_ms=MAX_LEASE_MS,*,now_ms=None):
        if not self.installed:raise RuntimeError('Install first')
        if not 1<=lease_ms<=MAX_LEASE_MS:raise ValueError('Lease must be 1..250 ms')
        now=self._now() if now_ms is None else int(now_ms)
        self.pm.write_bytes(self.data+72,struct.pack('<Q',now+lease_ms),8)

    def refresh_fixture(self,fixture):
        """Repin a fully qualified same-ship fixture between Controlled calls.

        The caller must use the pinned flight thread's outer Update AFTER hook
        and supply a fresh fixture_from_ship qualification. No cave, invocation,
        lease or receipt storage is recreated. A worker must publish a fresh
        command after success before the override can resume.
        """
        if not self.installed or not self.data:raise RuntimeError('Installed bridge required')
        candidate=dict(fixture)
        if candidate.get('ship')!=self.fixture.get('ship'):
            raise IdentityError('Fixture refresh requires the same ship')
        thread=struct.unpack('<I',self.pm.read_bytes(self.data+56,4))[0]
        if not thread or thread!=K.GetCurrentThreadId():
            raise IdentityError('Fixture refresh requires the pinned flight thread')
        state=initial_state(candidate,0,0)  # Validate every field before mutation.
        ranges=((8,40),(80,128),(160,216),(232,240),(256,600))
        try:
            self.pm.write_bytes(self.data+4,bytes(4),4)
            self.pm.write_bytes(self.data,bytes(4),4)
            if self.pm.read_bytes(self.data,8)!=bytes(8):
                raise RuntimeError('Fixture refresh gates did not become inert')
            for start,end in ranges:
                chunk=state[start:end]
                self.pm.write_bytes(self.data+start,chunk,len(chunk))
                if self.pm.read_bytes(self.data+start,len(chunk))!=chunk:
                    raise RuntimeError('Fixture metadata readback failed')
            self.pm.write_bytes(self.data,struct.pack('<I',1),4)
            if self.pm.read_bytes(self.data,8)!=struct.pack('<II',1,0):
                raise RuntimeError('Fixture refresh enable readback failed')
        except BaseException:
            # A transport error may follow a partial write, including enable.
            # Attempt both independent gates without masking the original error.
            for offset in (0,4):
                try:self.pm.write_bytes(self.data+offset,bytes(4),4)
                except BaseException:pass
            raise
        self.fixture=candidate

    def set_enabled(self,enabled):
        if not self.installed:raise RuntimeError('Install first')
        self.pm.write_bytes(self.data+4,struct.pack('<I',bool(enabled)),4)

    def set_planetary(self,enabled):
        """T88 switch: tolerate the low-height gate and ground-clearance term (planetary caves only)."""
        if not self.installed:raise RuntimeError('Install first')
        if enabled and not self.allow_planetary_flight:raise RuntimeError('Caves were built without planetary flight')
        self.pm.write_bytes(self.data+PLANETARY,struct.pack('<I',bool(enabled)),4)

    def set_auto_level(self,enabled):
        """False skips native roll and water auto-level while the planetary switch is on (Sultan holds the bank)."""
        if not self.installed:raise RuntimeError('Install first')
        self.pm.write_bytes(self.data+AUTO_LEVEL_OFF,struct.pack('<I',not bool(enabled)),4)

    def set_clamp(self,half):
        """Integrator clamp floor while Sultan flies a planet: the clamp radius becomes 2*max(native, half)."""
        if not self.installed:raise RuntimeError('Install first')
        if not (math.isfinite(half) and 0.<=half<=1e5):raise ValueError('Invalid integrator clamp')
        self.pm.write_bytes(self.data+CLAMP_HALF,struct.pack('<f',half),4)

    def set_water(self,enabled,push=DEFAULT_WATER_PUSH):
        """T93: over water the sea is Sultan's (no native height term in written commands, a capped push in
        native frames). Planetary caves only."""
        if not self.installed:raise RuntimeError('Install first')
        if enabled and not self.allow_planetary_flight:raise RuntimeError('Caves were built without planetary flight')
        if not (math.isfinite(push) and 0.<=push<=1e4):raise ValueError('Invalid water push')
        self.pm.write_bytes(self.data+WATER_PUSH,struct.pack('<f',push),4)
        self.pm.write_bytes(self.data+WATER,struct.pack('<I',bool(enabled)),4)

    def set_gear(self,enabled):
        """T101: gear down, no native ground-clearance push in written commands. Planetary caves only."""
        if not self.installed:raise RuntimeError('Install first')
        if enabled and not self.allow_planetary_flight:raise RuntimeError('Caves were built without planetary flight')
        self.pm.write_bytes(self.data+GEAR,struct.pack('<I',bool(enabled)),4)

    def set_auto_bank(self,enabled):
        """True retains native yaw-derived banking; False suppresses its assist."""
        from native_autobank import AUTOBANK_SUPPRESS
        if not self.installed:raise RuntimeError('Install first')
        self.pm.write_bytes(self.data+AUTOBANK_SUPPRESS,struct.pack('<I',not bool(enabled)),4)

    def disable(self):
        if self.data:self.pm.write_bytes(self.data+4,bytes(4),4)

    def read_records(self,after=0):
        latest=self.pm.read_ulonglong(self.data+136)
        first=max(after+1,latest-CAPACITY+1);out=[]
        for seq in range(first,latest+1):
            address=self.data+RING+(seq&(CAPACITY-1))*SLOT
            before=self.pm.read_ulonglong(address);raw=self.pm.read_bytes(address,SLOT)
            if before!=self.pm.read_ulonglong(address):continue
            row=decode_record(raw)
            if row and row['record_sequence']==seq:
                row['reason_name']=REASONS.get(row['reason'],'unknown')
                row['command_expired']=row['reason']==2
                row['native_fallback']=not bool(row['applied'])
                out.append(row)
        return out,latest,max(0,first-after-1)

    def sample(self):
        if not self.data:return {'reason_name':'unprepared','native_fallback':True}
        latest=self.pm.read_ulonglong(self.data+136)
        rows,_,_=self.read_records(max(0,latest-1))
        result=rows[-1] if rows else {'reason_name':'awaiting_invocation','native_fallback':True}
        return result

    def restore(self,writer=replace_code):
        if not self.data:return {}
        self.disable();self.pm.write_bytes(self.data,bytes(4),4)
        self.pm.write_bytes(self.data+72,bytes(8),8);results={}
        for kind,site in reversed(list(self.items.items())):
            original=bytes.fromhex(site['original']);patch=bytes.fromhex(site['patch'])
            try:
                current=self.pm.read_bytes(site['address'],len(original))
                if current==patch:writer(self.pm,self.pid,site['address'],patch,original)
                elif current!=original:raise RuntimeError('Foreign bytes; restore refused')
                results[kind]=self.pm.read_bytes(site['address'],len(original))==original
            except BaseException as error:results[kind]=repr(error)
        self.installed=False
        if any(result is not True for result in results.values()):raise RuntimeError(str(results))
        return results
