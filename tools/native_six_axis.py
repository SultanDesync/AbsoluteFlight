"""D12 native frame latch/provenance, exact build 183877/26295378 (remapped from 180836).

The external publisher never writes legacy channel blocks. One native pinned
mailbox copy at the start of each invocation supplies every consumer. Native
input is retained separately from injected values. Allocations stay inert until
process exit after restoration, so no executing cave can be freed underneath it.
"""
import struct
from native_roll_patch import Code, K, allocate_near, replace_code, ORIGINAL as ROLL_ORIGINAL
from native_throttle_patch import ORIGINAL as THROTTLE_ORIGINAL
from command_mailbox import reader_code, STRICT_ROTATION

SIZE = 512
PAYLOAD = 64
DEADLINE, MASK, VALUES, GENERATION = 64, 72, 80, 104
SITES = {0x178af4d:bytes.fromhex('0f118690760000'),
         0x178c23a:bytes.fromhex('488d8ec0600000'),
         0x178d78b:THROTTLE_ORIGINAL, 0x1790ee8:ROLL_ORIGINAL}


def save(c):
    # At every selected site RSP is 16-byte aligned. 8 pushes = 64 bytes.
    c.emit(bytes.fromhex('9c 50 51 52 41 50 41 51 41 52 41 53 48 81 ec 80 00 00 00'))
    for r in range(6):
        c.emit(bytes([0xf3,0x0f,0x7f,0x44+8*r,0x24,0x20+16*r]))


def restore(c):
    for r in range(6):
        c.emit(bytes([0xf3,0x0f,0x6f,0x44+8*r,0x24,0x20+16*r]))
    c.emit(bytes.fromhex('48 8d a4 24 80 00 00 00 41 5b 41 5a 41 59 41 58 5a 59 58 9d'))


def check(c, prefix, address, suffix=b'', reject='out', condition=b'\x0f\x85'):
    c.relative(prefix,address,suffix); c.relative(condition,reject)


def frame_guard(c, frame, mailbox, tick, pilot, global_slot, reject='out', allow_planetary_input=False,
                allow_combat_input=False, allow_power_input=False):
    """No required mask: provenance is also collected on disabled frames.

    Release physical inputs may continue under native planetary/combat physics
    and the four native power selections. Research callers remain strict by
    default; these options never admit Newtonian final-force overrides.
    """
    check(c,b'\x48\x83\x3d',frame,b'\x01',reject)
    check(c,b'\x48\x3b\x35',frame+8,reject=reject)
    check(c,b'\x48\x3b\x2d',frame+16,reject=reject)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
    check(c,b'\x3b\x05',frame+32,reject=reject)
    check(c,b'\x83\x3d',frame+56,b'\x00',reject)
    c.emit(b'\x48\xb8'+struct.pack('<Q',mailbox))
    c.emit(bytes.fromhex('48 83 78 08 00'));c.relative(b'\x0f\x85',reject)
    c.emit(b'\x48\xb8'+struct.pack('<Q',tick)+b'\xff\xd0')
    for address in (frame+40,frame+DEADLINE,pilot):
        # The menu-clock mapping can be far from the cave.
        c.emit(b'\x49\xbb'+struct.pack('<Q',address)+bytes.fromhex('49 3b 03'))
        c.relative(b'\x0f\x83',reject)
    modes=(('80 be c8 60 00 00 01',b'\x0f\x85'),
           ('83 be 5c 67 00 00 '+('03' if allow_power_input else '00'),
            b'\x0f\x87' if allow_power_input else b'\x0f\x85'))
    if not allow_planetary_input:modes+=(('80 be 30 67 00 00 00',b'\x0f\x85'),)
    for instruction,condition in modes:
        c.emit(bytes.fromhex(instruction));c.relative(condition,reject)
    c.emit(b'\x48\xa1'+struct.pack('<Q',global_slot)+bytes.fromhex('48 85 c0'))
    c.relative(b'\x0f\x84',reject)
    for instruction,condition in (('83 b8 98 bd 89 00 00',b'\x0f\x85'),
            ('80 b8 6d aa 57 00 '+('01' if allow_combat_input else '00'),
             b'\x0f\x87' if allow_combat_input else b'\x0f\x85')):
        c.emit(bytes.fromhex(instruction));c.relative(condition,reject)
    if allow_planetary_input or allow_combat_input or allow_power_input:
        # Release physical commands keep their existing permissive envelope.
        # Tagged test commands retain strict D10 checks at EVERY native guard,
        # using the invocation's atomic payload rather than a mutable mode flag.
        # MAV calls frame_guard twice in one Code object: labels must be unique.
        done = 'strict_rotation_done_' + str(len(c.data))
        check(c,b'\xf7\x05',frame+MASK,struct.pack('<I',STRICT_ROTATION),
              reject=done,condition=b'\x0f\x84')
        # An independently atomic revocation survives an already latched test
        # payload and cannot be undone by an in-flight/stale publication.
        c.emit(b'\x49\xbb'+struct.pack('<Q',mailbox))
        c.emit(bytes.fromhex('49 83 7b 10 00'));c.relative(b'\x0f\x85',reject)
        for instruction in ('83 be 5c 67 00 00 00', '80 be 30 67 00 00 00',
                            '80 b8 6d aa 57 00 00'):
            c.emit(bytes.fromhex(instruction));c.relative(b'\x0f\x85',reject)
        check(c,b'\xf7\x05',frame+MASK,struct.pack('<I',0x7ffffff8),reject=reject)
        for index in range(6):
            c.relative(b'\x8b\x05',frame+VALUES+index*4)
            c.emit(bytes.fromhex('25 ff ff ff 7f') + b'\x3d' +
                   struct.pack('<f',.25 if index < 3 else 0.))
            # Positive IEEE bits compare monotonically; NaN/inf reject too.
            c.relative(b'\x0f\x87',reject)
        c.label(done)


def idle(c, load, reject='conflict', limit=.00001):
    c.emit(load+bytes.fromhex('25 ff ff ff 7f')+b'\x3d'+struct.pack('<f',limit))
    c.relative(b'\x0f\x87',reject)  # unsigned float bits also reject NaN/inf


def provenance(c, frame, reject='out'):
    """Processed steering and throttle must have been produced THIS invocation."""
    check(c,b'\x83\x3d',frame+60,b'\x03',reject)
    for marker in (136,152):
        c.relative(b'\x48\x8b\x05',frame+24)
        check(c,b'\x48\x3b\x05',frame+marker,reject=reject)
    for offset,address in ((0x7690,frame+128),(0x7694,frame+132)):
        c.emit(b'\x8b\x86'+struct.pack('<I',offset))
        check(c,b'\x3b\x05',address,reject=reject)
    c.emit(bytes.fromhex('8b 85 80 41 00 00'))
    check(c,b'\x3b\x05',frame+148,reject=reject)
    idle(c,bytes.fromhex('8b 85 a0 00 00 00'),reject,.0001)


def cave_bytes(kind,cave,site,frame,mailbox,tick,pilot,global_slot,steering_limit=.25,throttle_limit=.5,turn_scale=1.,
               allow_planetary_input=False,allow_combat_input=False,allow_power_input=False):
    if (steering_limit,throttle_limit) not in ((.25,.5),(1.,1.)):raise ValueError('Unknown native authority')
    if not 0<turn_scale<=1:raise ValueError('Turn rate scale')
    c=Code(cave)
    rva={'latch':0x178af4d,'processed':0x178c23a,'throttle':0x178d78b,'roll':0x1790ee8}[kind]
    original=SITES[rva]
    if kind=='latch':c.emit(original)
    save(c)
    check(c,b'\x48\x3b\x35',frame+8)
    if kind=='latch':
        # Sticky owner thread; a foreign or reentrant stack cannot reuse proof.
        c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
        c.relative(b'\x83\x3d',frame+32,b'\x00');c.relative(b'\x0f\x84','set_thread')
        check(c,b'\x3b\x05',frame+32)
        c.label('set_thread');c.relative(b'\x89\x05',frame+32)
        c.relative(b'\x48\x89\x2d',frame+16);c.relative(b'\x48\xff\x05',frame+24)
        for offset in (56,60,136,152,168,200,204):c.relative(b'\xc7\x05',frame+offset,bytes(4))
        c.emit(b'\x48\xb9'+struct.pack('<Q',mailbox))
        c.relative(b'\x48\x8d\x15',frame+PAYLOAD);c.relative(b'\xe8','reader')
        check(c,b'\x48\x3b\x05',frame+GENERATION)
        c.relative(b'\xe9','out')
    else:
        frame_guard(c,frame,mailbox,tick,pilot,global_slot,allow_planetary_input=allow_planetary_input,
                    allow_combat_input=allow_combat_input,allow_power_input=allow_power_input)
        if kind=='processed':
            check(c,b'\x83\x3d',frame+60,b'\x03',reject='conflict')
            # Scale our processed yaw/pitch after the game's nonlinear input curve.
            # Disabled/native input and uncommanded axes remain untouched.
            if turn_scale!=1:
                for index in (0,1):
                    label='turn_scale_done_'+str(index)
                    check(c,b'\xf7\x05',frame+MASK,struct.pack('<I',1<<index),reject=label,condition=b'\x0f\x84')
                    c.emit(bytes.fromhex('f3 0f 10 86')+struct.pack('<I',0x7690+index*4))
                    c.emit(b'\xb8'+struct.pack('<f',turn_scale)+bytes.fromhex('66 0f 6e c8 f3 0f 59 c1'))
                    c.emit(bytes.fromhex('f3 0f 11 86')+struct.pack('<I',0x7690+index*4))
                    c.label(label)
            c.emit(bytes.fromhex('f3 0f 7e 86 90 76 00 00'))
            c.relative(b'\x66\x0f\xd6\x05',frame+128)
            c.relative(b'\x48\x8b\x05',frame+24);c.relative(b'\x48\x89\x05',frame+136)
        elif kind=='throttle':
            # Exact normal target branch and endpoint ownership from D10.
            c.emit(bytes.fromhex('48 8d 86 84 66 00 00 48 39 c3'));c.relative(b'\x0f\x85','out')
            c.emit(bytes.fromhex('83 bc 24 20 01 00 00 ff'));c.relative(b'\x0f\x85','out')
            c.emit(bytes.fromhex('8b 85 80 41 00 00'));c.relative(b'\x89\x05',frame+144)
            # Native u is a conflict only on a frame that substitutes throttle. The game's own
            # u writers (launch/station auto-boost latch, 0x1759500 on 180836, holds u=1) otherwise pass
            # through, so roll, MAV and rotation keep their frame (live 2026-10-19).
            check(c,b'\xf7\x05',frame+MASK,struct.pack('<I',8),reject='throttle_native',condition=b'\x0f\x84')
            idle(c,bytes.fromhex('8b 85 80 41 00 00'),limit=.0001)
            c.relative(b'\x8b\x05',frame+VALUES+12)
            c.emit(b'\x3d'+struct.pack('<f',throttle_limit));c.relative(b'\x0f\x87','conflict')
            c.emit(bytes.fromhex('89 85 80 41 00 00'))
            c.relative(b'\x83\x0d',frame+200,b'\x08')
            c.label('throttle_native');c.emit(bytes.fromhex('8b 85 80 41 00 00'))
            c.relative(b'\x89\x05',frame+148)
            c.relative(b'\x48\x8b\x05',frame+24);c.relative(b'\x48\x89\x05',frame+152)
        else:
            provenance(c,frame)
            # XMM7 is already processed and includes native yaw-to-roll coupling.
            # provenance() checks the ORIGINAL roll input at [RBP+A0] instead.
            c.relative(b'\xf3\x0f\x11\x3d',frame+160)
            check(c,b'\xf7\x05',frame+MASK,struct.pack('<I',4),reject='out',condition=b'\x0f\x84')
            c.relative(b'\x8b\x05',frame+VALUES+8);c.emit(bytes.fromhex('25 ff ff ff 7f')+b'\x3d'+struct.pack('<f',steering_limit));c.relative(b'\x0f\x87','conflict')
            c.relative(b'\xf3\x0f\x10\x05',frame+VALUES+8);c.emit(bytes.fromhex('f3 0f 10 f8'))
            c.relative(b'\xf3\x0f\x11\x3d',frame+164)
            c.relative(b'\x48\x8b\x05',frame+24);c.relative(b'\x48\x89\x05',frame+168)
            c.relative(b'\x83\x0d',frame+200,b'\x04')
        c.relative(b'\xe9','out')
        c.label('conflict');c.relative(b'\xc7\x05',frame+56,struct.pack('<I',1))
    c.label('out')
    if kind=='roll':
        # A completed invocation receipt, published under a seqlock. XMM0 is
        # still saved; game registers/flags are restored below.
        check(c,b'\x48\x3b\x35',frame+8,reject='restore')
        check(c,b'\x48\x3b\x2d',frame+16,reject='restore')
        c.relative(b'\x48\xff\x05',frame+240)
        for offset in range(0,224,16):
            c.relative(b'\x0f\x10\x05',frame+offset)
            c.relative(b'\x0f\x11\x05',frame+256+offset)
        c.relative(b'\x48\xff\x05',frame+240)
        c.label('restore')
    restore(c)
    if kind!='latch':c.emit(original)
    c.relative(b'\xe9',site+len(original))
    if kind=='latch':c.label('reader');c.emit(reader_code())
    result=c.finish()
    if len(result)>0xf00:raise ValueError('Cave too large')
    return result


def mav_guard(c, frame, mailbox, tick, pilot, global_slot, reject='publish', allow_planetary_input=False,
              allow_combat_input=False, allow_power_input=False):
    frame_guard(c,frame,mailbox,tick,pilot,global_slot,reject,allow_planetary_input,
                allow_combat_input,allow_power_input)
    provenance(c,frame,reject)


class SixAxisPatches:
    def __init__(self,pm,pid,frame,mailbox,tick,pilot,steering_limit=.25,throttle_limit=.5,turn_scale=1.,
                 allow_planetary_input=False,allow_combat_input=False,allow_power_input=False):
        self.pm,self.pid,self.items=pm,pid,{}
        base=pm.process_base.lpBaseOfDll
        for kind,rva in zip(('latch','processed','throttle','roll'),SITES):
            site=base+rva;original=SITES[rva]
            if pm.read_bytes(site,len(original))!=original:raise RuntimeError('Foreign/original site mismatch')
            cave=allocate_near(pm.process_handle,site)
            code=cave_bytes(kind,cave,site,frame,mailbox,tick,pilot,base+0x6f5f868,steering_limit,throttle_limit,turn_scale,
                            allow_planetary_input,allow_combat_input,allow_power_input)
            pm.write_bytes(cave,code,len(code))
            if pm.read_bytes(cave,len(code))!=code:raise RuntimeError('Cave readback failed')
            if not K.FlushInstructionCache(pm.process_handle,cave,len(code)):raise OSError('Cache flush')
            self.items[hex(rva)]={'site':site,'original':original.hex(),'patch':(b'\xe9'+struct.pack('<i',cave-site-5)+b'\x90'*(len(original)-5)).hex(),'code':code.hex(),'kind':kind,'cave':cave}

    def install(self,writer=replace_code):
        for item in self.items.values():writer(self.pm,self.pid,item['site'],bytes.fromhex(item['original']),bytes.fromhex(item['patch']))

    def restore(self,writer=replace_code):
        results={}
        for rva,item in reversed(list(self.items.items())):
            try:
                original,patch=bytes.fromhex(item['original']),bytes.fromhex(item['patch'])
                current=self.pm.read_bytes(item['site'],len(original))
                if current==patch:writer(self.pm,self.pid,item['site'],patch,original)
                elif current!=original:raise RuntimeError('Foreign patch; refused')
                results[rva]=self.pm.read_bytes(item['site'],len(original))==original
            except BaseException as error:results[rva]=repr(error)
        return results
