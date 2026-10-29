"""F13: suppress only RudderToRoll's yaw-derived demand on build 183877/26295378 (remapped from 180836).

Static source: research/re-log/2026-10-16_codex_rotation-additive.{md,json}.
Verified executable SHA256 d393c6782d29e1915854c0d5cf42505c8093740c05dc53705dbed7e0d1d4ac37 (183877);
the 24-byte signature is unique in its .text section. No attachment or game write
occurs here. The qualified assist load is replaced with zero; native yaw, explicit
roll input, angular feedback and separate combat-follow roll paths stay native.

Uses the translation bridge's positively pinned identity and same-invocation
gravity/height/avoidance receipts. Those capture sites precede this assist. All
mode/property/identity/time gates must still pass at the assist itself. No previous
frame's admission or Python callback result can qualify this operation.
"""
import struct

AUTOBANK_SUPPRESS=0x300
AUTOBANK_COUNT=0x308
AUTOBANK_INVOCATION=0x310
AUTOBANK_STAGE=0x318
AUTOBANK_SITE={
    'rva':0x1790d89,'size':8,'original':'f30f108588410000',
    'signature':'f30f1085884100000f5705684b4603f30f59c30f28f8e9f3',
    'instructions':['movss xmm0, dword ptr [rbp + 0x4188]'],
    'disposition':'zero yaw-derived RudderToRoll demand only under qualified Sultan flight',
}


def assist_code(cave,site,data,base,get_tick,*,allow_combat_flight=False,allow_planetary_flight=False):
    # Late import avoids a module cycle when Bridge.prepare imports this generator.
    from native_newtonian import (Code,WORK,VECTORS,REQUIRED,LOW_HEIGHT,_save,_restore,_owner_guard,_check,
                                  low_height_gate,planetary_skip)
    c=Code(cave);_save(c)
    def stage(value):c.relative(b'\xc7\x05',data+AUTOBANK_STAGE,struct.pack('<I',value))
    stage(0);_owner_guard(c,data)
    w=data+WORK
    def ne(prefix,address,suffix=b''):_check(c,prefix,address,suffix)
    def ins_ne(code):c.emit(bytes.fromhex(code));c.relative(b'\x0f\x85','out')
    def zero(reg,offset):
        c.emit((b'\x41' if reg>=8 else b'')+bytes([0x8b,0x80+(reg%8)])+struct.pack('<i',offset))
        c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0'));c.relative(b'\x0f\x85','out')
    ne(b'\x83\x3d',data+4,b'\x01')
    ne(b'\x83\x3d',data+AUTOBANK_SUPPRESS,b'\x01')
    stage(1)
    c.emit(b'\x48\xb8'+struct.pack('<Q',base+0x1783F41));ins_ne('48 39 85 68 41 00 00')
    # Native clock is checked here, independently of a previous translation call.
    c.emit(b'\x48\xb8'+struct.pack('<Q',get_tick)+b'\xff\xd0')
    c.relative(b'\x48\x3b\x05',data+72);c.relative(b'\x0f\x83','out')
    # Ship, controller and body must still be this positive local-player fixture.
    c.emit(bytes.fromhex('48 8b 86 58 62 00 00'));ne(b'\x48\x3b\x05',data+160)
    c.emit(bytes.fromhex('48 83 c0 50'));ne(b'\x48\x3b\x05',data+16)
    ne(b'\x4c\x3b\x2d',data+16)
    c.emit(bytes.fromhex('49 8b 85 90 02 00 00'));ne(b'\x48\x3b\x05',data+24)
    c.emit(bytes.fromhex('48 89 c2 48 8b 42 08'));ne(b'\x48\x3b\x05',data+32)
    c.emit(bytes.fromhex('49 8b 85 28 03 00 00'));ne(b'\x48\x3b\x05',data+32)
    c.emit(bytes.fromhex('49 89 c0')) # R8 pinned context, RDX pinned state
    c.emit(bytes.fromhex('48 8b 82 88 00 00 00'));ne(b'\x48\x3b\x05',data+112)
    c.relative(b'\x48\x8b\x05',base+0x6f5f868);ne(b'\x48\x3b\x05',data+80)
    c.emit(bytes.fromhex('48 8b 80 48 cf 72 00'));ne(b'\x48\x3b\x05',data+304)
    c.emit(bytes.fromhex('48 8b 86 c0 60 00 00'));ne(b'\x48\x3b\x05',data+168)
    c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+176)
    c.emit(bytes.fromhex('48 8b 46 30'));ne(b'\x48\x3b\x05',data+312)
    c.emit(bytes.fromhex('8b 80 d0 00 00 00'));ne(b'\x3b\x05',data+320)
    stage(2)
    # Native flight mode checks match the qualified acceleration seam. Own-ship
    # obstacle avoidance remains native and does not reject Newtonian admission.
    for code in ('80 be c8 60 00 00 01','83 be 5c 67 00 00 00','80 be 30 67 00 00 00',
                 '83 be 18 73 00 00 00','83 be 30 64 00 00 00','80 be e0 73 00 00 00',
                 '80 be ed 76 00 00 00','80 be e4 6c 00 00 00','80 be 40 77 00 00 00'):
        if allow_planetary_flight and code==LOW_HEIGHT:low_height_gate(c,data,'out','low_height_ok')
        else:ins_ne(code)
    c.emit(bytes.fromhex('f6 42 01 18'));c.relative(b'\x0f\x85','out')
    c.emit(bytes.fromhex('49 83 b8 38 0a 00 00 01'));c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x8b\x05',base+0x6f5f868)
    for code in ('83 b8 98 bd 89 00 00','80 b8 6d aa 57 00 00','83 b8 dc b6 72 00 00','83 b8 cc b6 72 00 00'):
        if allow_combat_flight and code=='80 b8 6d aa 57 00 00':
            # Clear-space manual combat shares this assist; all other guards stay.
            c.emit(bytes.fromhex('80 b8 6d aa 57 00 01'));c.relative(b'\x0f\x87','out')
        else:ins_ne(code)
    stage(3)
    # Backend pages, body generation, packed damping/gravity and callback list.
    c.emit(bytes.fromhex('49 8b 80 80 03 00 00'));ne(b'\x48\x3b\x05',data+88)
    c.emit(bytes.fromhex('48 89 c7 48 8b 40 40'));ne(b'\x48\x3b\x05',data+256)
    c.relative(b'\x48\x8b\x05',data+264);c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+272)
    c.emit(bytes.fromhex('48 8b 87 38 07 00 00'));ne(b'\x48\x3b\x05',data+280)
    c.relative(b'\x48\x8b\x05',data+288);c.emit(bytes.fromhex('48 8b 00'));ne(b'\x48\x3b\x05',data+296)
    c.relative(b'\x48\x8b\x0d',data+96)
    c.emit(bytes.fromhex('48 8b 81 b8 00 00 00'));ne(b'\x48\x3b\x05',data+16)
    c.emit(bytes.fromhex('8b 81 80 00 00 00'));ne(b'\x3b\x05',data+120)
    c.emit(bytes.fromhex('8b 41 54'));ne(b'\x3b\x05',data+124)
    c.emit(bytes.fromhex('48 8b 87 b0 0b 00 00'));ne(b'\x48\x3b\x05',data+328)
    c.emit(bytes.fromhex('48 89 c1 8b 81 7c 0f 00 00'));ne(b'\x3b\x05',data+336)
    c.emit(bytes.fromhex('8b 81 78 0f 00 00'));ne(b'\x3b\x05',data+340)
    for index in range(16):
        c.relative(b'\x83\x3d',data+336,bytes([index]));c.relative(b'\x0f\x86','callbacks_checked')
        c.emit(b'\x48\x8b\x81'+struct.pack('<I',0xf80+16*index));ne(b'\x48\x3b\x05',data+344+16*index)
        c.emit(b'\x48\x8b\x81'+struct.pack('<I',0xf88+16*index));ne(b'\x48\x3b\x05',data+352+16*index)
    c.label('callbacks_checked')
    stage(4)
    c.relative(b'\x48\x8b\x0d',data+104)
    for off in (0x74,0x76):
        c.emit(bytes.fromhex('66 83 b9')+struct.pack('<i',off)+b'\x00');c.relative(b'\x0f\x85','out')
    for off in (0x270,0x274):zero(13,off)
    c.emit(bytes.fromhex('0f b7 41 78'));ne(b'\x3b\x05',data+188)
    c.emit(bytes.fromhex('41 8b 85 7c 02 00 00'));ne(b'\x3b\x05',data+184)
    c.emit(bytes.fromhex('41 8b 85 38 03 00 00'));ne(b'\x3b\x05',data+192)
    for i in range(3):
        c.emit(bytes.fromhex('41 8b 85')+struct.pack('<I',0x340+4*i));ne(b'\x3b\x05',data+196+4*i)
    c.relative(b'\x83\x3d',data+212,b'\x01');c.relative(b'\x0f\x84','gravity_qualified')
    zero(13,0x27c)
    c.emit(bytes.fromhex('66 83 79 78 00'));c.relative(b'\x0f\x85','out')
    ins_ne('41 83 bd 38 03 00 00 00')
    c.label('gravity_qualified')
    stage(5)
    # Zero effective time would make this an unqualified paused physics frame.
    c.emit(bytes.fromhex('41 8b 85 60 02 00 00 85 c0'));c.relative(b'\x0f\x8e','out')
    c.emit(bytes.fromhex('3d 00 00 80 7f'));c.relative(b'\x0f\x83','out')
    for address in (base+0x7232e78+0x18,base+0x7232e78+0x24):
        c.relative(b'\x8b\x05',address);c.emit(bytes.fromhex('85 c0'));c.relative(b'\x0f\x8e','out')
        c.emit(bytes.fromhex('3d 00 00 80 7f'));c.relative(b'\x0f\x83','out')
    c.relative(b'\xf3\x0f\x10\x05',base+0x7232e78+0x18)
    c.relative(b'\xf3\x0f\x59\x05',base+0x7232e78+0x24)
    c.emit(bytes.fromhex('66 0f 7e c0 85 c0'));c.relative(b'\x0f\x8e','out')
    c.emit(bytes.fromhex('3d 00 00 80 7f'));c.relative(b'\x0f\x83','out')
    c.relative(b'\x8b\x05',base+0x7232e78+0x30);c.emit(bytes.fromhex('f7 c0 00 ff 00 00'));c.relative(b'\x0f\x84','clock_running')
    c.emit(bytes.fromhex('f7 c0 00 00 ff 00'));c.relative(b'\x0f\x84','out')
    c.label('clock_running')
    stage(6)
    # Actual gravity/height/avoidance capture belongs to this frame, never a cache.
    c.relative(b'\x8b\x05',data+60)
    c.emit(b'\x25'+struct.pack('<I',REQUIRED)+b'\x3d'+struct.pack('<I',REQUIRED));c.relative(b'\x0f\x85','out')
    if allow_planetary_flight:planetary_skip(c,data,'height_tolerated')
    for term in ('height',):
        for i in range(3):
            c.relative(b'\x8b\x05',w+VECTORS[term]+4*i)
            c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0'));c.relative(b'\x0f\x85','out')
    if allow_planetary_flight:c.label('height_tolerated')
    stage(7)
    # Overspeed is still qualified Newtonian coasting; reject invalid values,
    # never a finite speed above our cap or the research fixture's speed bound.
    for i in range(3):
        c.emit(b'\x8b\x82'+struct.pack('<i',0x60+4*i))
        c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 7f'));c.relative(b'\x0f\x83','out')
        c.relative(b'\x8b\x05',w+VECTORS['gravity']+4*i)
        c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 7f'));c.relative(b'\x0f\x83','out')
    stage(8)
    c.relative(b'\x48\xff\x05',data+AUTOBANK_COUNT)
    c.relative(b'\x48\x8b\x05',data+48);c.relative(b'\x48\x89\x05',data+AUTOBANK_INVOCATION)
    _restore(c)
    c.emit(bytes.fromhex('0f 57 c0')) # equivalent MOVSS upper lanes, zero yaw assist
    c.relative(b'\xe9',site['address']+site['size'])
    c.label('out');_restore(c)
    c.emit(bytes.fromhex(site['original']));c.relative(b'\xe9',site['address']+site['size'])
    code=c.finish()
    if len(code)>0x2000:raise ValueError('Auto-bank cave exceeds reserved pages')
    return code
