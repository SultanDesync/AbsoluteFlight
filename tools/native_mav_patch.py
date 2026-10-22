"""D8 candidate, build180836/25732212; NOT authorized or installed by import.

Only adds a bounded perpendicular correction to the native blended force lane.
Two native sites share same-invocation coefficient capture. No velocity write.
"""
import json,struct
from pathlib import Path
from native_roll_patch import Code,K,allocate_near,replace_code
from native_mav_observer import SITES,capture_bytes,decode_snapshot

SIZE=640;FORCE=256;CONSTANTS=512
LIMIT=.0499999 # rounding margin below first-proof norm0.05


def bounded_limit(bound):
    if bound not in (.05,3,5,30,60):raise ValueError('Unqualified MAV bound')
    return bound*(1-2e-6)


def initial_state(ship,lease,bound=.05):
    state=bytearray(SIZE);struct.pack_into('<I',state,0,1);struct.pack_into('<Q',state,8,ship)
    struct.pack_into('<Q',state,FORCE+32,lease)
    struct.pack_into('<9f',state,CONSTANTS,bounded_limit(bound),bound,0,1,.999,1.001,16,.0001,.00001)
    struct.pack_into('<4I',state,576,0xffffffff,0xffffffff,0xffffffff,0)
    struct.pack_into('<4I',state,592,0,0,0,0xffffffff)
    return bytes(state)


def force_bytes(cave,site,data,get_tick,global_slot,bound=.05,integration=None):
    bounded_limit(bound)
    # Only the release's integrated physical input path opts in. Standalone
    # research and the separate Newtonian final-force gate remain strict.
    allow_planetary_input=bool(integration and integration.get('allow_planetary_input',False))
    allow_combat_input=bool(integration and integration.get('allow_combat_input',False))
    allow_power_input=bool(integration and integration.get('allow_power_input',False))
    c=Code(cave);f=data+FORCE;k=data+CONSTANTS
    command_target = (integration['frame']+96) if integration else f+4
    generation = (integration['frame']+104) if integration else f+56
    def admission():
        if integration:
            c.relative(b'\xf7\x05',integration['frame']+72,struct.pack('<I',48));jump(b'\x0f\x84')
        else:
            c.relative(b'\x83\x3d',f,b'\x01');jump()
    def ss(op,d,s):c.emit(bytes([0xf3,0x0f,op,0xc0+8*d+s]))
    def ps(op,d,s):c.emit(bytes([0x0f,op,0xc0+8*d+s]))
    def ld(d,address):c.relative(bytes([0xf3,0x0f,0x10,0x05+8*d]),address)
    def st(s,address):c.relative(bytes([0xf3,0x0f,0x11,0x05+8*s]),address)
    def vp(d,offset):c.emit(bytes([0x0f,0x10,0x85+8*d])+struct.pack('<i',offset))
    def storeps(s,address):c.relative(bytes([0x0f,0x11,0x05+8*s]),address)
    def broadcast(r):c.emit(bytes([0x0f,0xc6,0xc0+9*r,0]))
    def sum_xyz():
        ps(0x28,5,4);c.emit(bytes.fromhex('0f c6 ec a9'));ss(0x58,5,4)
        ps(0x28,4,5);c.emit(bytes.fromhex('0f c6 e5 01'));ss(0x58,4,5)
    def length_squared():ps(0x28,4,2);ps(0x59,4,2);sum_xyz()
    def reason(n):c.relative(b'\xc7\x05',f+12,struct.pack('<I',n))
    def jump(op=b'\x0f\x85'):c.relative(op,'publish')
    def clamp(label):
        length_squared();ss(0x51,4,4)
        c.relative(b'\x0f\x2e\x25',k+8);jump(b'\x0f\x8a');jump(b'\x0f\x86') # unordered or zero
        c.relative(b'\x0f\x2e\x25',k);c.relative(b'\x0f\x86',label)
        ld(5,k);ss(0x5e,5,4);broadcast(5);ps(0x59,2,5);c.label(label)
    def clock():
        c.emit(b'\x48\xb8'+struct.pack('<Q',get_tick)+b'\xff\xd0')
        c.relative(b'\x48\x89\x05',f+144)
        c.relative(b'\x48\x3b\x05',integration['frame']+64 if integration else f+16);jump(b'\x0f\x83')
        c.relative(b'\x48\x3b\x05',f+32);jump(b'\x0f\x83')

    c.emit(SITES[0x175C435][:3]) # native ADDPS, exactly once before any guard
    c.emit(bytes.fromhex('9c 50 51 52 41 50 41 51 41 52 41 53 48 81 ec 80 00 00 00'))
    for r in range(6):c.emit(bytes([0xf3,0x0f,0x7f,0x44+8*r,0x24,0x20+16*r]))
    for prefix,address,suffix in ((b'\x83\x3d',data,b'\x01'),(b'\x48\x3b\x35',data+8,b''),(b'\x48\x3b\x2d',data+16,b'')):
        c.relative(prefix,address,suffix);c.relative(b'\x0f\x85','skip')
    c.relative(b'\x48\x8b\x05',data+24);c.relative(b'\x48\x3b\x05',data+32);c.relative(b'\x0f\x84','skip')
    c.relative(b'\x48\xff\x05',data+40)
    c.relative(b'\x48\x89\x05',data+32);c.relative(b'\x48\x89\x05',data+80);c.relative(b'\x48\x89\x2d',data+88)
    c.relative(b'\xf3\x0f\x7e\x05',data+64);c.relative(b'\x66\x0f\xd6\x05',data+96)
    c.emit(bytes.fromhex('f3 0f 10 84 24 24 01 00 00'));st(0,data+104)
    for offset,target,vector in ((0x90,108,False),(0x170,112,True),(0x430,128,True),(0x4170,180,False),(0x1cb0,192,True),(0x1cc0,208,True),(0x1cd0,224,True)):
        if vector:vp(0,offset);storeps(0,data+target)
        else:c.emit(b'\xf3\x0f\x10\x85'+struct.pack('<i',offset));st(0,data+target)
    storeps(6,data+144);storeps(7,data+160);c.relative(b'\xf3\x44\x0f\x11\x05',data+176)
    storeps(7,f+112);storeps(7,f+128);ps(0x57,0,0);storeps(0,f+64);storeps(0,f+80)
    reason(0);admission()
    reason(2);clock()
    reason(3)
    modes=(('80 be c8 60 00 00 01',b'\x0f\x85'),
           ('83 be 4c 67 00 00 '+('03' if allow_power_input else '00'),
            b'\x0f\x87' if allow_power_input else b'\x0f\x85'))
    if not allow_planetary_input:modes+=(('80 be 20 67 00 00 00',b'\x0f\x85'),)
    for instruction,condition in modes:
        c.emit(bytes.fromhex(instruction));jump(condition)
    c.emit(b'\x48\xa1'+struct.pack('<Q',global_slot));c.emit(bytes.fromhex('48 85 c0'));jump(b'\x0f\x84')
    for instruction,condition in (('83 b8 98 54 87 00 00',b'\x0f\x85'),
            ('80 b8 0d a4 57 00 '+('01' if allow_combat_input else '00'),
             b'\x0f\x87' if allow_combat_input else b'\x0f\x85')):
        c.emit(bytes.fromhex(instruction));jump(condition)
    reason(4)
    if integration:
        from native_six_axis import mav_guard
        mav_guard(c,integration['frame'],integration['mailbox'],get_tick,integration['pilot'],global_slot,
                  allow_planetary_input=allow_planetary_input,
                  allow_combat_input=allow_combat_input,allow_power_input=allow_power_input)
    else:
        for offset,limit in ((0x4170,.0001),):
            c.emit(b'\x8b\x85'+struct.pack('<i',offset)+bytes.fromhex('25 ff ff ff 7f')+b'\x3d'+struct.pack('<f',limit));jump(b'\x0f\x87')
        for offset in (0x7680,0x7684):
            c.emit(b'\x8b\x86'+struct.pack('<i',offset)+bytes.fromhex('25 ff ff ff 7f')+b'\x3d'+struct.pack('<f',.00001));jump(b'\x0f\x87')
    reason(5)
    for address in (command_target,command_target+4):
        c.relative(b'\x8b\x05',address);c.emit(bytes.fromhex('25 ff ff ff 7f')+b'\x3d'+struct.pack('<f',bound));jump(b'\x0f\x87')
    c.relative(b'\x8b\x05',command_target);c.relative(b'\x0b\x05',command_target+4);c.emit(bytes.fromhex('25 ff ff ff 7f'));jump(b'\x0f\x84')
    c.relative(b'\x48\x8b\x05',generation);c.relative(b'\x48\x89\x05',f+152)
    reason(6)
    for address,maximum in ((data+96,16),(data+100,16),(data+104,16),(data+108,1)):
        c.relative(b'\x8b\x05',address);c.emit(b'\x3d'+struct.pack('<f',maximum));jump(b'\x0f\x87')
    reason(7);vp(3,0x170);ps(0x28,4,3);ps(0x59,4,3);sum_xyz();st(4,f+100)
    c.relative(b'\x0f\x2e\x25',k+16);jump(b'\x0f\x8a');jump(b'\x0f\x82')
    c.relative(b'\x0f\x2e\x25',k+20);jump(b'\x0f\x87');ps(0x28,1,4)
    ld(0,command_target);broadcast(0);vp(2,0x1cb0);ps(0x59,2,0)
    ld(0,command_target+4);broadcast(0);vp(4,0x1cc0);ps(0x59,4,0);ps(0x58,2,4)
    c.relative(b'\x0f\x54\x15',data+576)
    ps(0x28,4,2);ps(0x59,4,3);sum_xyz();ss(0x5e,4,1);broadcast(4);ps(0x59,4,3);ps(0x5c,2,4)
    c.relative(b'\x0f\x54\x15',data+576);reason(8);clamp('target_bounded');storeps(2,f+80)
    ld(0,data+96);ld(1,data+100);ss(0x59,0,1);ld(1,data+104);ss(0x5c,0,1)
    ld(4,data+108);ss(0x59,0,4);ss(0x58,0,1);st(0,f+96)
    c.relative(b'\x8b\x05',f+96);c.emit(b'\x3d'+struct.pack('<f',16));reason(6);jump(b'\x0f\x87')
    broadcast(0);ps(0x59,2,0);reason(8);clamp('acceleration_bounded');storeps(2,f+64)
    # Re-check after all arithmetic; GetTickCount64 may clobber volatile XMMs.
    reason(2);clock();admission()
    if integration:
        mav_guard(c,integration['frame'],integration['mailbox'],get_tick,integration['pilot'],global_slot,
                  allow_planetary_input=allow_planetary_input,
                  allow_combat_input=allow_combat_input,allow_power_input=allow_power_input)
    c.relative(b'\x48\x8b\x05',generation);c.relative(b'\x48\x3b\x05',f+152);jump()
    c.relative(b'\x0f\x10\x15',f+64);ps(0x58,7,2)
    c.relative(b'\x0f\x54\x3d',data+576);c.relative(b'\x0f\x10\x05',f+112)
    c.relative(b'\x0f\x54\x05',data+592);ps(0x56,7,0) # original W lane bit-exact
    storeps(7,f+128);c.relative(b'\x48\xff\x05',f+40)
    if integration:c.relative(b'\x83\x0d',integration['frame']+200,b'\x30')
    if integration:
        c.relative(b'\x48\x8b\x05',integration['frame']+24);c.relative(b'\x48\x89\x05',f+160)
    c.relative(b'\x48\x8b\x05',data+24);c.relative(b'\x48\x89\x05',f+48);reason(9)
    c.label('publish');c.relative(b'\x48\xff\x05',data+40);c.relative(b'\xe9','restore')
    c.label('skip');c.relative(b'\x48\xff\x05',data+48)
    c.label('restore')
    for r in range(6):c.emit(bytes([0xf3,0x0f,0x6f,0x44+8*r,0x24,0x20+16*r]))
    c.emit(bytes.fromhex('48 8d a4 24 80 00 00 00 41 5b 41 5a 41 59 41 58 5a 59 58 9d'))
    c.emit(SITES[0x175C435][3:]);c.relative(b'\xe9',site+7)
    result=c.finish()
    if len(result)>(0x1800 if integration else 0xa00):raise ValueError('Force cave overlaps data')
    return result


class MavPatch:
    def __init__(self,pm,pid,ship,get_tick,lease,manifest,bound=.05,integration=None):
        limit=bounded_limit(bound)
        self.pm,self.pid=pm,pid;self.base=pm.process_base.lpBaseOfDll
        for rva,original in SITES.items():
            if pm.read_bytes(self.base+rva,7)!=original:raise RuntimeError('Original MAV bytes differ')
        self.cave=allocate_near(pm.process_handle,self.base+0x175C134)
        force_cave=allocate_near(pm.process_handle,self.base+0x175C435) if integration else self.cave+0x200
        self.block=self.cave+(0x400 if integration else 0xc00);self.force=self.block+FORCE;self.patches={}
        for i,(rva,original) in enumerate(SITES.items()):
            site=self.base+rva;cave=force_cave if i else self.cave
            code=capture_bytes(cave,site,self.block) if i==0 else force_bytes(cave,site,self.block,get_tick,self.base+0x6e8d708,bound,integration)
            pm.write_bytes(cave,code,len(code))
            if not K.FlushInstructionCache(pm.process_handle,cave,len(code)):raise OSError('Cache flush failed')
            if pm.read_bytes(cave,len(code))!=code:raise RuntimeError('MAV cave readback differs')
            patch=b'\xe9'+struct.pack('<i',cave-site-5)+b'\x90'*2
            self.patches[rva]={'site':site,'original':original.hex(),'patch':patch.hex(),'code':code.hex()}
        state=initial_state(ship,lease,bound);pm.write_bytes(self.block,state,len(state))
        if not K.FlushInstructionCache(pm.process_handle,self.cave,0x1000):raise OSError('Cache flush failed')
        self.metadata={'pid':pid,'block':self.block,'force':self.force,'cave':self.cave,'sites':self.patches,'build':'180836/25732212','bounds':{'target_norm':limit,'addition_norm':limit},'retention':'Allocation retained inert until process exit'}
        with Path(manifest).open('x') as f:json.dump(self.metadata,f,indent=2)
    def install(self,writer=replace_code):
        for item in self.patches.values():writer(self.pm,self.pid,item['site'],bytes.fromhex(item['original']),bytes.fromhex(item['patch']))
    def clear(self):
        self.pm.write_bytes(self.force,bytes(4),4);self.pm.write_bytes(self.force+16,bytes(8),8)
    def sample(self):
        before=self.pm.read_ulonglong(self.block+40);raw=self.pm.read_bytes(self.block,SIZE);after=self.pm.read_ulonglong(self.block+40)
        if before!=after or before!=struct.unpack_from('<Q',raw,40)[0]:return None
        result=decode_snapshot(raw[:256])
        if result is None:return None
        result.update(reason=struct.unpack_from('<I',raw,FORCE+12)[0],applied_count=struct.unpack_from('<Q',raw,FORCE+40)[0],
            addition=list(struct.unpack_from('<4f',raw,FORCE+64)),target=list(struct.unpack_from('<4f',raw,FORCE+80)),
            gain=struct.unpack_from('<f',raw,FORCE+96)[0],before=list(struct.unpack_from('<4f',raw,FORCE+112)),
            after=list(struct.unpack_from('<4f',raw,FORCE+128)),native_tick=struct.unpack_from('<Q',raw,FORCE+144)[0],
            command_generation=struct.unpack_from('<Q',raw,FORCE+152)[0],injected_invocation=struct.unpack_from('<Q',raw,FORCE+160)[0])
        return result
    def restore(self,writer=replace_code):
        self.clear();self.pm.write_bytes(self.block,bytes(4),4);results={}
        for rva,item in self.patches.items():
            try:
                site=item['site'];original=bytes.fromhex(item['original']);patch=bytes.fromhex(item['patch']);current=self.pm.read_bytes(site,7)
                if current==patch:writer(self.pm,self.pid,site,patch,original)
                elif current!=original:raise RuntimeError('Foreign code; refused')
                results[hex(rva)]=self.pm.read_bytes(site,7)==original
            except BaseException as error:results[hex(rva)]=repr(error)
        if any(v is not True for v in results.values()):raise RuntimeError(str(results))
        return results
