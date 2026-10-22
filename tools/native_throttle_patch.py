"""D10 stack-u cave, build180836/25732212 only; disabled until explicitly armed."""
import json
from pathlib import Path
import struct
from native_roll_patch import Code,K,allocate_near,replace_code

SITE_RVA=0x175A6F4
ORIGINAL=bytes.fromhex('F3 0F 10 95 70 41 00 00')
GLOBAL_SLOT_RVA=0x6E8D708


def cave_bytes(cave,site,data,get_tick,global_slot):
    c=Code(cave)
    c.emit(b'\x9c')
    c.relative(b'\x83\x3d',data,b'\x00');c.relative(b'\x0f\x84','original')
    c.emit(bytes.fromhex('50 51 52 41 50 41 51 41 52 41 53 48 81 EC 80 00 00 00'))
    for reg in range(6):c.emit(bytes([0xf3,0x0f,0x7f,0x44+8*reg,0x24,0x20+16*reg]))
    def gate(prefix,target,suffix=b''):
        c.relative(prefix,target,suffix);c.relative(b'\x0f\x85','reject')
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',1))
    gate(b'\x48\x3b\x35',data+8) # exact RSI owner
    c.emit(bytes.fromhex('48 8D 86 74 66 00 00 48 39 C3'));c.relative(b'\x0f\x85','reject')
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',2))
    c.emit(bytes.fromhex('83 BE 4C 67 00 00 00'));c.relative(b'\x0f\x85','reject')
    c.emit(bytes.fromhex('80 BE 20 67 00 00 00'));c.relative(b'\x0f\x85','reject')
    # Original RSP+0x64 becomes current RSP+0x124 after flags/GPR/XMM saves.
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',3))
    c.emit(bytes.fromhex('83 BC 24 24 01 00 00 FF'));c.relative(b'\x0f\x85','reject')
    c.emit(b'\x48\xa1'+struct.pack('<Q',global_slot))
    c.emit(bytes.fromhex('48 85 C0'));c.relative(b'\x0f\x84','reject')
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',4))
    c.emit(bytes.fromhex('83 B8 98 54 87 00 00'));c.relative(b'\x0f\x85','reject')
    c.emit(bytes.fromhex('80 B8 0D A4 57 00 00'));c.relative(b'\x0f\x85','reject')
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',5))
    c.emit(bytes.fromhex('8B 85 70 41 00 00'));c.relative(b'\x89\x05',data+48)
    c.emit(bytes.fromhex('25 FF FF FF 7F 85 C0'));c.relative(b'\x0f\x85','reject')
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',6))
    c.relative(b'\x8b\x05',data+4)
    c.emit(b'\x3d'+struct.pack('<I',0x3f000000));c.relative(b'\x0f\x87','reject') # 0..0.5, also rejects NaN/negative
    c.relative(b'\xc7\x05',data+56,struct.pack('<I',7))
    c.emit(b'\x48\xb8'+struct.pack('<Q',get_tick)+b'\xff\xd0')
    c.relative(b'\x48\x3b\x05',data+16);c.relative(b'\x0f\x83','reject')
    c.relative(b'\x48\x3b\x05',data+24);c.relative(b'\x0f\x83','reject')
    c.relative(b'\x48\xff\x05',data+32)
    c.relative(b'\xc7\x05',data+56,bytes(4))
    c.relative(b'\x83\x3d',data,b'\x01');c.relative(b'\x0f\x85','restore') # mode2: observe eligibility only
    c.relative(b'\x8b\x05',data+4)
    c.emit(bytes.fromhex('89 85 70 41 00 00'));c.relative(b'\x89\x05',data+52)
    c.relative(b'\xff\x05',data+60)
    c.relative(b'\xe9','restore')
    c.label('reject');c.relative(b'\x48\xff\x05',data+40)
    c.label('restore')
    for reg in range(6):c.emit(bytes([0xf3,0x0f,0x6f,0x44+8*reg,0x24,0x20+16*reg]))
    c.emit(bytes.fromhex('48 8D A4 24 80 00 00 00 41 5B 41 5A 41 59 41 58 5A 59 58'))
    c.label('original');c.emit(b'\x9d');c.emit(ORIGINAL);c.relative(b'\xe9',site+len(ORIGINAL))
    result=c.finish()
    if len(result)>=0x300:raise ValueError('Cave overlaps block')
    return result


class ThrottlePatch:
    def __init__(self,pm,pid,ship,get_tick,manifest):
        self.pm,self.pid=pm,pid;self.site=pm.process_base.lpBaseOfDll+SITE_RVA
        if pm.read_bytes(self.site,8)!=ORIGINAL:raise RuntimeError('Throttle originals differ')
        self.cave=allocate_near(pm.process_handle,self.site);self.block=self.cave+0x300
        self.patch=b'\xe9'+struct.pack('<i',self.cave-self.site-5)+b'\x90'*3
        code=cave_bytes(self.cave,self.site,self.block,get_tick,pm.process_base.lpBaseOfDll+GLOBAL_SLOT_RVA)
        pm.write_bytes(self.cave,code,len(code));pm.write_bytes(self.block,struct.pack('<IfQQQ',0,0,ship,0,0)+bytes(32),64)
        if pm.read_bytes(self.cave,len(code))!=code:raise RuntimeError('Cave readback failed')
        if not K.FlushInstructionCache(pm.process_handle,self.cave,len(code)):raise OSError('Cache flush failed')
        self.metadata={'pid':pid,'site':hex(self.site),'site_rva':hex(SITE_RVA),'cave':hex(self.cave),'block':hex(self.block),
                       'original':ORIGINAL.hex(),'patch':self.patch.hex(),'code':code.hex(),
                       'retention':'Allocation retained until process exit, even after original site restored'}
        with Path(manifest).open('x') as f:json.dump(self.metadata,f,indent=2)
    def install(self):return replace_code(self.pm,self.pid,self.site,ORIGINAL,self.patch)
    def clear(self):
        self.pm.write_bytes(self.block,bytes(4),4);self.pm.write_bytes(self.block+16,bytes(8),8)
    def restore(self):
        self.clear();current=self.pm.read_bytes(self.site,8)
        if current==self.patch:replace_code(self.pm,self.pid,self.site,self.patch,ORIGINAL)
        elif current!=ORIGINAL:raise RuntimeError('Foreign throttle code; restore refused')
        if self.pm.read_bytes(self.site,8)!=ORIGINAL:raise RuntimeError('Throttle restoration failed')
        return True
