"""In-flow meters on our HOTAS pages, using the U3-accepted NanoVG path (build 180836).

Geometry: each meter has a spacer Header row; its native root bounds (captured from GetRow while
we emit rows) are projected with the UI-thread TLS layout scale and the active NanoVG matrix,
and the whole widget is skipped when its clip parent would cut it (U3 rules). Drawing happens
at nvgEndFrame inside nvgSave/nvgRestore, and the native state block must be byte-identical
afterwards. Read-only meters: no pointer capture, so U3's input ownership is not needed.
"""
import ctypes as C,struct
import keyboard_layout
from ui_range_policy import project

P,I,F=C.c_void_p,C.c_int32,C.c_float
NVG={'nvgSave':(None,[P]),'nvgRestore':(None,[P]),'nvgReset':(None,[P]),'nvgBeginPath':(None,[P]),
     'nvgRect':(None,[P,F,F,F,F]),'nvgFill':(None,[P]),'nvgStroke':(None,[P]),'nvgMoveTo':(None,[P,F,F]),
     'nvgLineTo':(None,[P,F,F]),'nvgFillColor':(None,[P,P]),'nvgStrokeColor':(None,[P,P]),'nvgStrokeWidth':(None,[P,F]),
     'nvgFontSize':(None,[P,F]),'nvgFontFaceId':(None,[P,I]),'nvgTextAlign':(None,[P,I]),'nvgText':(F,[P,F,F,P,P])}
# Muted NMS palette: panel, track, zones (dim/mid/bright), accent, text.
COLORS={'panel':(.04,.05,.07,.92),'dim':(.23,.24,.26,1),'mid':(.55,.54,.52,1),'bright':(.78,.76,.71,1),
        'accent':(.95,.69,.20,1),'text':(.91,.90,.88,1),'shadow':(0,0,0,1),
        'pane':(.04,.05,.07,1),  # solid: a pane hides the game's description panel under it (T122)
        # Keyboard layout (T112, mod/keyboard_layout.py categories); tools/render_keyboard_layout.py uses the same as hex.
        'key-ours':(.95,.69,.20,1),'key-press':(.25,.72,.66,1),'key-moved':(.36,.55,.94,1),'key-game':(.54,.53,.52,1),
        'key-clash':(.89,.33,.30,1),'key-overlap':(.80,.47,.78,1),'key-none':(.16,.17,.19,1),'key-ink':(.08,.09,.10,1),'key-faint':(.60,.60,.56,1)}
ZONE_TONE={'DEAD':'dim','STOP':'dim','IDLE':'dim','HOLD':'dim','ACTIVE':'mid','THRUST':'mid','FORWARD':'mid','FASTER':'mid',
           'SLOWER':'mid','BRAKE':'mid','FULL BRAKE':'bright','SAT':'bright','FULL':'bright','CRUISE':'bright'}


def panel_geometry(row,clip):
    """Panel and track inside a projected spacer row; None when clipped (never spill past the list)."""
    x,y,r,b=row;h=b-y
    if not 10<=h<=250 or r<=x:return None
    if y<clip[1] or b>clip[3] or x<clip[0] or r>clip[2]:return None
    pad=.35*h
    return {'panel':row,'left':x+pad,'right':r-pad,'top':y+.38*h,'bottom':y+.62*h,'h':h}


def keyboard_area(clip,size,margin=.04):
    """(left, top, width, height) for the keyboard panel beside the options list: the wider side of the screen, inside a
    margin, from the list's top. None when neither side has room for a readable keyboard."""
    if not size:return None
    w,h=size;x,y,r,b=clip;m=margin*w
    right=(r+m,w-m);left=(m,x-m)
    side=max((right,left),key=lambda s:s[1]-s[0])
    width=side[1]-side[0]
    if width<.18*w or width<keyboard_layout.WIDTH*12:return None
    return side[0],y,width,max(b,h*.8)-y


# The pane (T122) goes over the game's own description panel, which starts at the right edge of the options list, and
# uses that panel's text size. Measured on the user's screenshots (2026-11-13, 3440x1440, 0.2.7-dev.3, build 185034):
# description text about 30 px with lines 37.5 px apart, the panel's left border 29 px left of the list's clip edge.
PANE_FONT=1/48.   # body text, as a share of the frame height
PANE_INSET=.02    # the pane starts this share of the frame height left of the list's clip edge
PANE_CHARS=46     # the pane's text width, in characters of its body size
PANE_CHAR=.47     # a character's width as a share of the font size (the menu font: 26 characters ran 10.7 sizes wide)
PANE_CAPS=.56     # the same for capitals
PANE_TONE={'conflict':'key-clash','capture':'accent'}  # by the pane's 'state'; anything else is plain


def pane_area(clip,size):
    """(left, top, width, height, body size) for a pane over the game's description panel, right of the options list
    from the list's top; None when the frame has no room there for a readable column. clip: the list's clip, projected;
    size: the menu's NanoVG frame."""
    if not size or size[0]<=0 or size[1]<=0:return None
    w,h=size;x,y,r,b=clip;fs=h*PANE_FONT;pad=.8*fs
    left=r-PANE_INSET*h;width=min(PANE_CHARS*PANE_CHAR*fs+2*pad,.99*w-left)
    if width<24*PANE_CHAR*fs+2*pad or b<=y:return None
    return left,y,width,max(b,h*.8)-y,fs


def pane_layout(meter,width,fs):
    """A pane's text (T122) for a column this wide at body size fs: [(indent, text, size, colour, advance)], top to
    bottom. Pure, for tests. The pane is content only, so any page can have one: {'title', 'lines': [text],
    'options': [(what to do, what it does)], 'footer', 'state'}. Title, the lines, each option with its first part in the
    accent colour, then the footer."""
    wrap=keyboard_layout.wrap;out=[]
    def add(text,size,colour,indent=0.,char=PANE_CHAR,gap=0.):
        lines=wrap(text or '',width-indent,size,char)
        for line in lines:out.append([indent,line,size,colour,1.3*size])
        if lines:out[-1][4]+=gap
    add(meter.get('title'),1.2*fs,PANE_TONE.get(meter.get('state'),'text'),char=PANE_CAPS,gap=.6*fs)
    for text in meter.get('lines') or ():add(text,fs,'text',gap=.25*fs)
    if out:out[-1][4]+=.4*fs
    for key,text in meter.get('options') or ():
        add(key,fs,'accent',char=PANE_CAPS);add(text,fs,'text',indent=1.2*fs,gap=.5*fs)
    add(meter.get('footer'),.85*fs,'key-faint')
    return [tuple(line) for line in out]


def meter_marks(meter,left,right):
    """Pixel x for zones, highlight edges and markers; pure, for tests."""
    lo,hi=meter['range'];w=right-left
    px=lambda v:left+(min(hi,max(lo,v))-lo)/(hi-lo)*w
    zones=[(px(a),px(b),label) for a,b,label in meter['zones']]
    highlight=[px(v) for v in (meter.get('highlight') or ())]
    markers={k:px(v) for k,v in (meter.get('markers') or {}).items()}
    return zones,highlight,markers


class MeterPainter:
    def __init__(self,api):
        self.api=api;self.colors={k:(F*4)(*v) for k,v in COLORS.items()};self.text=C.create_string_buffer(96)

    def fill(self,ctx,color):self.api['nvgFillColor'](ctx,C.addressof(self.colors[color]))

    def rect(self,ctx,x,y,w,h,color):
        a=self.api;a['nvgBeginPath'](ctx);self.fill(ctx,color);a['nvgRect'](ctx,x,y,w,h);a['nvgFill'](ctx)

    def triangle(self,ctx,x,tip,base,half,color):
        a=self.api;a['nvgBeginPath'](ctx);self.fill(ctx,color)
        a['nvgMoveTo'](ctx,x,tip);a['nvgLineTo'](ctx,x-half,base);a['nvgLineTo'](ctx,x+half,base);a['nvgFill'](ctx)

    def label(self,ctx,x,y,text,size,color='text'):
        self.text.value=text.encode('ascii','replace')[:95];self.fill(ctx,color)
        self.api['nvgFontSize'](ctx,size);self.api['nvgText'](ctx,x,y,C.addressof(self.text),None)

    def keyboard_panel(self,ctx,geo,size,meter):
        """The Keyboard page's layout diagram (T112), beside the options list (user 2026-10-30: a two-panel page; drawn in
        the rows it spilled past the list at 3440x1440). geo: the anchor row and the list's clip, projected; size: the
        menu's NanoVG frame. The anchor row shows a caption pointing at the panel."""
        row,clip=geo
        anchor=panel_geometry(row,clip)
        if anchor:
            x,y,r,b=anchor['panel'];h=anchor['h']
            self.rect(ctx,x,y,r-x,b-y,'panel');self.label(ctx,x+.3*h,y+.62*h,meter.get('caption',''),.36*h,'accent')
        area=keyboard_area(clip,size)
        if area is None:return False
        left,top,width,height=area;rows=meter['rows']
        unit=min(width/keyboard_layout.WIDTH,height/(len(rows)+5.2))
        x0=left;y0=top;pad=.3*unit
        self.rect(ctx,x0-pad,y0-pad,keyboard_layout.WIDTH*unit+2*pad,(len(rows)+.4+4.6)*unit+pad,'panel')
        gap=.08*unit
        for r,keys in enumerate(rows):
            y=y0+r*unit+(.4*unit if r else 0)
            for kx,w,name,function,category in keys:
                px=x0+kx*unit;pw=w*unit-gap;ph=unit-gap;ink='key-ink' if category!='none' else 'key-faint'
                self.rect(ctx,px,y,pw,ph,'key-'+category)
                if name:self.label(ctx,px+.1*unit,y+.3*unit,name,.24*unit,ink)
                if function:
                    lines,fs=keyboard_layout.fit(function,pw-.16*unit,.22*unit,char=.55)
                    for i,line in enumerate(lines):self.label(ctx,px+.1*unit,y+ph-.12*unit-(len(lines)-1-i)*fs,line,fs,ink)
        y=y0+(len(rows)+.4)*unit+.5*unit;columns=3;cw=keyboard_layout.WIDTH*unit/columns
        for i,(category,label) in enumerate(keyboard_layout.CATEGORIES):
            lx=x0+(i%columns)*cw;ly=y+(i//columns)*.55*unit
            self.rect(ctx,lx,ly,.32*unit,.32*unit,'key-'+category);self.label(ctx,lx+.45*unit,ly+.28*unit,label,.3*unit)
        y+=1.4*unit
        for line in keyboard_layout.wrap(' '.join(keyboard_layout.EXPERIMENTAL),keyboard_layout.WIDTH*unit,.26*unit):
            self.label(ctx,x0,y,line,.26*unit,'key-faint');y+=.36*unit
        return True

    def side_panel(self,ctx,geo,size,meter):
        """A page's pane (T122, user 2026-11-13): text over the game's own description panel, so what it says never
        moves the list's rows. The Buttons page uses it for binding prompts and button conflicts
        (hotas_menu.button_pane). State 'idle' draws nothing, so the game's description shows as usual (user
        2026-11-13, after an always-on pane: "they overlap existing descriptions"). Otherwise the pane is solid, at the
        description's text size, and covers that panel for as long as the prompt lasts. geo: the anchor row and the
        list's clip, projected; size: the menu's NanoVG frame. Text only: nothing in it is clickable. Returns whether
        there is room for the pane (False: the menu falls back to rows in the list)."""
        _,clip=geo
        area=pane_area(clip,size)
        if area is None:return False
        if meter.get('state','idle')=='idle':return True
        left,top,width,height,fs=area;pad=.8*fs
        self.rect(ctx,left,top,width,height,'pane')
        self.rect(ctx,left,top,.15*fs,height,PANE_TONE.get(meter.get('state'),'mid'))
        y=top+pad
        for indent,text,text_size,colour,advance in pane_layout(meter,width-2*pad,fs):
            if y+text_size>top+height:break
            self.label(ctx,left+pad+indent,y+.9*text_size,text,text_size,colour);y+=advance
        return True

    def meter(self,ctx,g,meter):
        x,y,r,b=g['panel'];h=g['h'];left,right,top,bottom=g['left'],g['right'],g['top'],g['bottom']
        self.rect(ctx,x,y,r-x,b-y,'panel')
        zones,highlight,markers=meter_marks(meter,left,right)
        for zx,zr,label in zones:
            self.rect(ctx,zx,top,max(1.,zr-zx),bottom-top,ZONE_TONE.get(label,'mid'))
            if zr-zx>len(label)*.11*h:self.label(ctx,zx+.05*h,top+.17*h,label,.15*h,'shadow')
        for hx in highlight:self.rect(ctx,hx-.02*h,top-.06*h,.04*h,bottom-top+.12*h,'accent')
        if 'input' in markers:self.triangle(ctx,markers['input'],top-.02*h,top-.2*h,.08*h,'text')
        for key in ('output','target'):
            if key in markers:self.triangle(ctx,markers[key],bottom+.02*h,bottom+.2*h,.08*h,'accent')
        lo,hi=meter['range'];m=meter.get('markers')
        if m is None:status='NO INPUT'
        else:
            status=f"IN {m['input']*100:+.0f}%" if lo<0 else f"IN {m['input']*100:.0f}%"
            if 'output' in m:status+=f"   OUT {m['output']*100:+.0f}%" if lo<0 else f"   OUT {m['output']*100:.0f}%"
            if 'target' in m:status+=f"   SET {m['target']*100:.0f}%"
        self.label(ctx,left,y+.26*h,status,.2*h)


def layout_from_tls(read_q,read_bytes,nt,ctx):
    """U3 native layout: UI-thread TLS scale (TEB+0x58 -> slot0 -> +0x1CD8) and active NanoVG matrix."""
    tbi=C.create_string_buffer(48)
    if nt.NtQueryInformationThread(P(-2),0,C.addressof(tbi),48,None)!=0:raise RuntimeError('Cannot read UI-thread TLS')
    teb=struct.unpack_from('<Q',tbi.raw,8)[0];tls_block=read_q(read_q(teb+0x58))
    scale=struct.unpack('<2f',read_bytes(tls_block+0x1cd8,8));n=struct.unpack('<i',read_bytes(ctx+0x42a8,4))[0]
    if not 1<=n<64:raise RuntimeError('Bad native state count')
    matrix=struct.unpack('<6f',read_bytes(ctx+0xa8+(n-1)*0x108+0xb4,24))
    return scale,matrix


def row_geometry(root,layout,read_q,read_bytes):
    """Projected row and clip rectangles for a native row root (U3 parent/clip chain), or None."""
    scale,matrix=layout
    bounds=struct.unpack('<4f',read_bytes(root+8,16))
    if bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:return None
    parent=read_q(root+0x40)
    if parent<0x10000:return None
    clipparent=read_q(parent+0x40)
    if clipparent<0x10000:return None
    clip=struct.unpack('<4f',read_bytes(clipparent+8,16))
    if clip[2]<=clip[0] or clip[3]<=clip[1]:return None
    return project(bounds,scale,matrix),project(clip,scale,matrix)
