"""Positive player and thread ownership for the outer Update after-hook.

Exact build 183877 / Steam 26295378 (remapped from 180836). Native 0x14ABE60 selects the spaceship through
the player attachment handle and entity component mask, not a generic ship pool.
The heavier native Newtonian fixture still independently checks pool membership.
This helper never attaches, calls game functions, or writes game memory.
"""
import struct


def local_player_ship(read,base):
    def raw(address,size):
        if address<0x10000 or size>32:raise ValueError('Invalid bounded player identity read')
        value=bytes(read(address,size))
        if len(value)!=size:raise ValueError('Short player identity read')
        return value
    def q(address):return struct.unpack('<Q',raw(address,8))[0]
    manager=q(base+0x6f5f868)
    if not manager:return 0
    handle=q(manager+0x72cf48)
    if not handle or handle>>32>0xfeff:return 0
    entity=base+0x5469f80+0x58*(handle>>32)
    if q(entity+0x50)!=handle:return 0
    component=struct.unpack('<h',raw(base+0x517e568,2))[0]
    if not 0<=component<256:return 0
    mask_raw=raw(entity+0x30,32);mask=int.from_bytes(mask_raw,'little')
    if not mask&(1<<component):return 0
    components=q(entity+0x20)
    if not components:return 0
    rank=(mask&((1<<component)-1)).bit_count()
    ship=q(components+8*rank)
    if not ship:return 0
    if (q(base+0x6f5f868)!=manager or q(manager+0x72cf48)!=handle or
            q(entity+0x50)!=handle or raw(entity+0x30,32)!=mask_raw or
            q(entity+0x20)!=components or q(components+8*rank)!=ship):
        return 0
    return ship


class PilotBoundary:
    def __init__(self):self.thread=0

    def admit(self,read,base,ship,thread,frame_thread=0):
        """Pin only a positive player callback; a foreign callback cannot drain."""
        if not thread or self.thread not in (0,thread) or frame_thread not in (0,thread):return False
        if local_player_ship(read,base)!=ship:return False
        if not self.thread:self.thread=thread
        return True
