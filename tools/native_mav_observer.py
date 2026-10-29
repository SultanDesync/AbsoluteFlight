"""Read-only native MAV coefficient/basis snapshots, build 183877/26295378 (remapped from 180836).

Writes only owned observer storage. Neither cave changes any native force,
input, camera or game-data value. No force-injection mode exists in this module.
"""
import json,struct
from pathlib import Path
from native_roll_patch import Code,K,allocate_near,replace_code

# 183791: the capture site moved past the coefficient's last write (no MOVAPS from XMM6 any more: the
# coefficient is scalar XMM0, the modifier broadcast XMM1), and 7.10 swapped XMM7/XMM8, so the blended
# force lane is XMM8 and the signed forward speed XMM7. Both sites are 8 bytes.
CAPTURE_SITE,FORCE_SITE=0x178F296,0x178F583
SITES={CAPTURE_SITE:bytes.fromhex('0fc6c000440f59c0'),FORCE_SITE:bytes.fromhex('440f58c6440f2fe0')}
ADDPS=4  # native ADDPS XMM8,XMM6 at the start of the force site; COMISS follows
SIZE=256
# UpdateControlled stack slots copied into each snapshot (rbp offset, snapshot offset). 183791 frame:
# 0x90->0x7C, 0x170->0x1B0, 0x430 same, 0x4170->0x4180, basis 0x1CB0/0x1CC0/0x1CD0->0x1E60/0x1E70/0x1E80.
STACK=((0x7C,108),(0x1B0,112),(0x430,128),(0x4180,180),(0x1E60,192),(0x1E70,208),(0x1E80,224))


def capture_bytes(cave,site,data):
    c=Code(cave);c.emit(b'\x9c')
    c.relative(b'\x83\x3d',data,b'\x01');c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x89\x35',data+240);c.relative(b'\x48\xff\x05',data+248)
    c.relative(b'\x48\x3b\x35',data+8);c.relative(b'\x0f\x85','out')
    c.relative(b'\x48\x89\x2d',data+16) # frame identity
    c.relative(b'\x48\xff\x05',data+24)
    c.relative(b'\xf3\x0f\x11\x05',data+64) # XMM0 base proportional coefficient
    c.relative(b'\xf3\x0f\x11\x0d',data+68) # XMM1 native modifier, already broadcast
    c.label('out');c.emit(b'\x9d');c.emit(SITES[CAPTURE_SITE]);c.relative(b'\xe9',site+len(SITES[CAPTURE_SITE]))
    return c.finish()


def blend_bytes(cave,site,data):
    c=Code(cave)
    c.emit(SITES[FORCE_SITE][:ADDPS]) # native add XMM8,XMM6 exactly once
    c.emit(bytes.fromhex('9c 50 48 8d 64 24 f0 0f 11 04 24')) # save flags/RAX/XMM0
    for prefix,target,suffix in ((b'\x83\x3d',data,b'\x01'),(b'\x48\x3b\x35',data+8,b''),(b'\x48\x3b\x2d',data+16,b'')):
        c.relative(prefix,target,suffix);c.relative(b'\x0f\x85','skip')
    c.relative(b'\x48\x8b\x05',data+24);c.relative(b'\x48\x3b\x05',data+32);c.relative(b'\x0f\x84','skip')
    c.relative(b'\x48\xff\x05',data+40) # odd: publishing a new snapshot
    c.relative(b'\x48\x89\x05',data+32);c.relative(b'\x48\x89\x05',data+80)
    c.relative(b'\x48\x89\x2d',data+88)
    c.relative(b'\xf3\x0f\x7e\x05',data+64);c.relative(b'\x66\x0f\xd6\x05',data+96)
    # Current RSP is 32 bytes below original; native RSP+0x60 (183791; was +0x64) is now RSP+0x80.
    c.emit(bytes.fromhex('f3 0f 10 84 24 80 00 00 00'));c.relative(b'\xf3\x0f\x11\x05',data+104)
    for offset,target in STACK:
        vector=offset in (0x1B0,0x430,0x1E60,0x1E70,0x1E80)
        c.emit((b'\x0f\x10\x85' if vector else b'\xf3\x0f\x10\x85')+struct.pack('<i',offset))
        c.relative(b'\x0f\x11\x05' if vector else b'\xf3\x0f\x11\x05',data+target)
    c.relative(b'\x0f\x11\x35',data+144);c.relative(b'\x44\x0f\x11\x05',data+160) # XMM6, force lane XMM8
    c.relative(b'\xf3\x0f\x11\x3d',data+176) # native signed forward speed XMM7
    c.relative(b'\x48\xff\x05',data+40) # even: fully copied
    c.relative(b'\xe9','out')
    c.label('skip');c.relative(b'\x48\xff\x05',data+48)
    c.label('out');c.emit(bytes.fromhex('0f 10 04 24 48 8d 64 24 10 58 9d'))
    c.emit(SITES[FORCE_SITE][ADDPS:]);c.relative(b'\xe9',site+len(SITES[FORCE_SITE])) # native COMISS flags
    result=c.finish()
    if len(result)>0x300:raise ValueError('Observer cave too large')
    return result


def decode_snapshot(raw):
    if len(raw)!=SIZE:raise ValueError('Wrong observer size')
    seq=struct.unpack_from('<Q',raw,40)[0]
    if not seq or seq&1:return None
    values=struct.unpack_from('<36f',raw,96)
    return {'sequence':seq,'capture_count':struct.unpack_from('<Q',raw,80)[0],
            'frame':struct.unpack_from('<Q',raw,88)[0],'k_prop':values[0],'modifier':values[1],
            'k_min':values[2],'weight':values[3],'forward':list(values[4:8]),'perpendicular_velocity':list(values[8:12]),
            'minimum_vector':list(values[12:16]),'blended_vector':list(values[16:20]),
            'forward_speed':values[20],'native_u':values[21],
            'original_right':list(values[24:28]),'original_up':list(values[28:32]),'original_forward':list(values[32:36])}


class MavObserver:
    def __init__(self,pm,pid,ship,manifest):
        self.pm,self.pid=pm,pid;self.base=pm.process_base.lpBaseOfDll
        for rva,original in SITES.items():
            if pm.read_bytes(self.base+rva,len(original))!=original:raise RuntimeError('MAV observer originals differ')
        self.cave=allocate_near(pm.process_handle,self.base+CAPTURE_SITE);self.block=self.cave+0x600;self.patches={}
        for i,(rva,original) in enumerate(SITES.items()):
            site=self.base+rva;cave=self.cave+i*0x200
            code=(capture_bytes if i==0 else blend_bytes)(cave,site,self.block)
            if len(code)>=0x200:raise ValueError('Caves overlap')
            pm.write_bytes(cave,code,len(code))
            if pm.read_bytes(cave,len(code))!=code:raise RuntimeError('Observer code readback differs')
            patch=b'\xe9'+struct.pack('<i',cave-site-5)+b'\x90'*(len(original)-5)
            self.patches[rva]={'site':site,'original':original.hex(),'patch':patch.hex(),'code':code.hex()}
        state=bytearray(SIZE);struct.pack_into('<Q',state,8,ship);pm.write_bytes(self.block,bytes(state),SIZE)
        if not K.FlushInstructionCache(pm.process_handle,self.cave,0x1000):raise OSError('Cache flush failed')
        self.metadata={'pid':pid,'block':self.block,'cave':self.cave,'sites':self.patches,'game_data_writes':0,'retention':'Allocation retained inert until process exit'}
        with Path(manifest).open('x') as f:json.dump(self.metadata,f,indent=2)
    def install(self):
        for item in self.patches.values():replace_code(self.pm,self.pid,item['site'],bytes.fromhex(item['original']),bytes.fromhex(item['patch']))
        self.pm.write_bytes(self.block,struct.pack('<I',1),4)
    def sample(self):
        before=self.pm.read_ulonglong(self.block+40);raw=self.pm.read_bytes(self.block,SIZE);after=self.pm.read_ulonglong(self.block+40)
        if before!=after or before!=struct.unpack_from('<Q',raw,40)[0]:return None
        return decode_snapshot(raw)
    def counters(self):
        raw=self.pm.read_bytes(self.block,SIZE)
        return {name:struct.unpack_from('<Q',raw,offset)[0] for name,offset in
                (('captured_frame',16),('capture_count',24),('consumed_count',32),('snapshot_sequence',40),('blend_rejections',48),('last_capture_owner',240),('capture_visits_all_owners',248))}
    def restore(self):
        self.pm.write_bytes(self.block,bytes(4),4);results={}
        for rva,item in self.patches.items():
            try:
                original=bytes.fromhex(item['original']);patch=bytes.fromhex(item['patch']);site=item['site'];current=self.pm.read_bytes(site,len(original))
                if current==patch:replace_code(self.pm,self.pid,site,patch,original)
                elif current!=original:raise RuntimeError('Foreign code; restore refused')
                results[hex(rva)]=self.pm.read_bytes(site,len(original))==original
            except BaseException as error:results[hex(rva)]=repr(error)
        if any(v is not True for v in results.values()):raise RuntimeError(str(results))
        return results
