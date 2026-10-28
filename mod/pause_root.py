"""HOTAS button on the pause Options root (E-pause), build 183791 / Steam 26271400.

The pause root is DoGameSwitcher (0x6EE990) drawing UI/SWITCHERPAGENEW.MBIN. Its category buttons
are named NGui layers; tools/build_pause_root_ui.py adds BUTTON_HOTAS after BUTTON_CTR. For each
button DoGameSwitcher (research/re-log/2026-10-16_claude_pause-root-buttons.md):
  1. finds the layer with FindLayer 0x1E36D0(root=[this+0x14478], &{id16, hash}, 0),
  2. looks its WHITE child up in the layer's index map at +0x150 (no recursive find),
  3. polls PollButton 0x901CF0(this, white, 1, 0, &sound, 0, 1); true = activated, after which
     it calls RequestOptionsPage(category, true).
We do the same from an after-hook on DoGameSwitcher, on the UI thread, in the same frame.
183791 (static, 2026-10-28): the hash constants, root/map/node offsets and the sound key are unchanged in
aligned DoGameSwitcher code; only the busy flag moved.

mem is a small reader (u32/i32/u64 by address) so everything here is tested with a fake.
"""
M64=(1<<64)-1
M32=0xFFFFFFFF
K=0x9DDFEA08EB382D69
BUTTON_ID='BUTTON_HOTAS'
CHILD_ID='WHITE'
ANCHOR_ID='BUTTON_CTR'  # native button; finding it proves the hash and root before we trust a miss
ROOT_LAYER=0x14478       # DoGameSwitcher this -> root NGui layer
INDEX_MAP=0x150          # layer -> descendant index map
BUSY_FLAG=0x87FD5A       # [app]+flag != 0: DoGameSwitcher polls no buttons this frame
SOUND_HASH=0x18AECCF6    # native 5th arg {0, 0x18AECCF6, 1}: the click sound key


def id16(text):
    raw=text.encode('ascii')
    if len(raw)>15:raise ValueError('NGui IDs are 16 bytes with a terminator: '+text)
    return raw.ljust(16,b'\0')


def _rotr(x,n):return ((x>>n)|(x<<(32-n)))&M32


def id_hash(text):
    """Transcribed from DoGameSwitcher 0x6EE9AD..0x6EEAAD (identical at every button site)."""
    raw=id16(text);lo=int.from_bytes(raw[:8],'little');hi=int.from_bytes(raw[8:],'little')
    a=((hi^lo)*K)&M64
    b=(((a>>47)^lo^a)*K)&M64
    h=(((b>>47)^b)*K)&M64
    lo32,hi32=h&M32,h>>32
    c=((lo32&0xFFFF)+0x1B873593)&M32
    d=((lo32>>5)&0x7FFF800)^((c<<16)&M32)^c
    e=(d+(hi32&0xFFFF)+(d>>11))&M32
    f=((hi32>>5)&0x7FFF800)^((e<<16)&M32)^e
    x=(f+(f>>11))&M32
    x=((x<<3)&M32)^x
    x=(x+(x>>5))&M32
    x=((x<<4)&M32)^x
    x=(x+(x>>17))&M32
    x=((x<<25)&M32)^x
    x=(x+(x>>6))&M32
    x=_rotr((x*0xCC9E2D51)&M32,17)
    x=_rotr((x*0x1B873593)&M32,19)
    return (x*5+0xE6546B64)&M32


def key_bytes(text):
    """FindLayer's key: 16-byte ID then the 64-bit (zero-extended) hash."""
    return id16(text)+id_hash(text).to_bytes(8,'little')


def map_lookup(mem,layer,text):
    """Native index-map walk at 0x6EF4A8; returns the stored element or 0."""
    table=mem.u64(layer+INDEX_MAP)
    if not table or mem.i32(table+0x1C)<=0:return 0
    h=id_hash(text)
    signed=h-(1<<32) if h&0x80000000 else h
    slot=(signed&M64)&((mem.i32(table+0x20)-1)&M64)
    node=mem.u64(mem.u64(table+8)+slot*8)
    for _ in range(4096):
        if not node:return 0
        if mem.u64(node+0x20)==h and mem.u64(node+0x10)==h:return mem.u64(node+0x18)
        node=mem.u64(node+0x28)
    return 0


class PauseRootButton:
    """Finds and polls BUTTON_HOTAS once per DoGameSwitcher frame.

    find(root, key_address) and poll(this, element) are the native calls; key(text) returns a
    stable address holding key_bytes(text); is_layer(element) is the vtable[1]()==0 type check.
    """
    def __init__(self,mem,find,poll,key,is_layer,log=lambda text:None):
        self.mem=mem;self.find=find;self.poll=poll;self.key=key;self.is_layer=is_layer;self.log=log
        self.state='unchecked'  # unchecked -> ready | missing | broken

    def element(self,this):
        root=self.mem.u64(this+ROOT_LAYER)
        if not root:return 0
        if self.state=='unchecked':
            if not self.find(root,self.key(ANCHOR_ID)):
                self.state='broken';self.log('pause root: native '+ANCHOR_ID+' not found; ID hash or layout changed, entry off')
                return 0
        layer=self.find(root,self.key(BUTTON_ID))
        if not layer:
            if self.state=='unchecked':
                self.state='missing';self.log('pause root: '+BUTTON_ID+' not in layout (UI mod not installed); entry off')
            return 0
        white=map_lookup(self.mem,layer,CHILD_ID)
        if not white or not self.is_layer(white):
            if self.state!='broken':self.log('pause root: '+BUTTON_ID+' has no '+CHILD_ID+' layer; entry off')
            self.state='broken';return 0
        if self.state=='unchecked':
            self.state='ready';self.log('pause root: '+BUTTON_ID+' found')
        return white

    def frame(self,this,app):
        """True when the player activated our button this frame."""
        if self.state in ('missing','broken'):return False
        if app and self.mem.u8(app+BUSY_FLAG):return False
        white=self.element(this)
        return bool(white) and bool(self.poll(this,white))
