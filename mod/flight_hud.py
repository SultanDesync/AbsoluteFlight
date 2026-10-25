"""T73 flight HUD: total-speed readout and velocity-vector markers, NMS 180836 / Steam 25732212.

The native cockpit readout shows signed speed along the nose only, which hides sideways drift
in Newtonian flight. This draws, in the ship HUD's own full-surface NanoVG frame (T64:
RenderHeadsUp calls NGui_RenderFrame at 0x9D7E97):
- total speed |v| large, with forward speed, lever target and lateral/vertical drift small;
- a prograde marker where the ship is moving, a retrograde marker where it is moving away
  from, and an edge arrow towards prograde when neither is on screen;
- a Sultan boost heat gauge right of the reticle: hot zone band (boost strongest), relight tick,
  fill that turns amber in the hot zone and red when overheated;
- a G meter below the reticle: total G, and a ring sized to the pilot's tolerance with a dot where
  the force pushes the body (each axis scaled by its own tolerance, so outside the ring is overload),
  plus a fore/aft bar and the strain while it builds;
- target-relative instruments (T79 read-only sample from weapon_lead): range, closing speed and
  relative speed under the speed readout, and a violet diamond along -dv, where the ship drifts
  relative to the target (dv = target velocity - own velocity);
- T87 planetary layer (planetary_hud): altitude above sea level, vertical speed with a trend arrow
  and ATM/VAC right of the reticle, fading in near a planet, and a mode tag (SULTAN / NATIVE).

Projection is T56's exact world-direction formula through the selected cGcCameraManager pose
(research/re-log/2026-10-16_codex_hud-projection.md). Read-only: no game memory is written.
Drawing is bracketed by nvgSave/nvgRestore, as the menu meters are.
"""
import ctypes as C
import math

from planetary_hud import MIN_VS,format_altitude

APP_GLOBAL=0x6E8D708
CAMERA_MANAGER=0x905B20  # embedded in the application object
POSE_SELECT,POSE_PRIMARY,POSE_ALTERNATE=0x330,0x130,0x230
POSE_FOV=0xF0
FOV_MULTIPLIER=0x6E8F88C  # runtime global; no disk initializer, so it is read every frame
GRAPHICS=0x5924740  # pointer; int width +0x34, int height +0x38
HUD_FRAME_RETURN=0x9D7E9C  # return address of RenderHeadsUp's NGui_RenderFrame call
# Exact-build code this module's reads depend on; any mismatch disables the HUD.
PINS={
    0x3175A0:bytes.fromhex('80b93003000000488d9130020000488d8130010000480f45c2c3'),  # pose = +0x330 ? +0x230 : +0x130
    0x694177:bytes.fromhex('f3440f1080f0000000'),  # FOV field pose+0xF0
    0x694196:bytes.fromhex('f3440f590dedb67f06'),  # times global 0x6E8F88C
    0x6941DE:bytes.fromhex('488b0d5b052905'),  # graphics object *0x5924740
    0x2D7E77A:bytes.fromhex('f30f10593c0f57d2f30f2a5134'),  # graphics width +0x34
    0x2D957E6:bytes.fromhex('f30f590d1a3bd901'),  # second half-angle multiply
    0x9D7E97:bytes.fromhex('e8942180ff'),  # RenderHeadsUp -> NGui_RenderFrame(ShipHUD+0x27C00)
    0x2C6F00:bytes.fromhex('488d8f205b9000'),  # application+0x905B20 camera manager
    0x196BA3E:bytes.fromhex('488b97c8420000'),  # nvgText: fontstash at ctx+0x42C8
    0x19643B7:bytes.fromhex('3b4174'),  # fonsTextIterInit: font count at fs+0x74
    0x19643C3:bytes.fromhex('488b4760'),  # font pointer array at fs+0x60
    0x19643CF:bytes.fromhex('4983797800'),  # font data at +0x78, so name[64] at +0x38
}
FONTSTASH,FONT_ARRAY,FONT_COUNT,FONT_NAME=0x42C8,0x60,0x74,0x38
# Game fonts registered in the UI NanoVG context (observed live on 180836: ids 0,1,4,5).
GAME_FONTS=(('GAMEFONT','Game font 1'),('GAMEFONT2','Game font 2'),('GAMEFONT3','Game font 3'),('GAMEFONT4','Game font 4'))
SAMPLE_MAX_AGE_MS=150
MIN_MARKER_SPEED=.5  # u/s; below this the direction is noise
NEAR_W=2.**-16
EDGE_MARGIN=.07  # of frame height, keeps markers clear of the frame edge
# Close to the native cockpit HUD: soft white text, NMS amber accent, faint dark halo.
COLORS={'text':(.94,.95,.93,.92),'dim':(.85,.88,.86,.62),'prograde':(.62,.96,.78,.9),
        'retrograde':(.98,.72,.24,.92),'shadow':(0,0,0,.35),
        'target':(.78,.64,1.,.92),'track':(.85,.88,.86,.16),'hotzone':(.98,.72,.24,.26),'heat':(.98,.72,.24,.92),'overheat':(1.,.3,.22,.95),
        'band':(.62,.96,.78,.22),'window':(.98,.72,.24,.55),'go':(.22,.85,.54,.95)}  # T93 purge: burn band, ignition window, forward arrow


DAZE_RED=(.55,.02,.02)  # crash daze periphery colour (user 2026-10-25: a redout rather than a blackout)
DAZE_WASH=.16  # alpha of the red wash over the whole frame at full daze
PURGE_SPAN=5.  # s across the purge graph: 2 s of trail behind now, 3 s of band ahead
PURGE_NOW=.4   # share of the graph width left of now
PURGE_STEP=.05  # s per requirement trace sample (planetary_hud.purge_plan)
PURGE_HEAD_Z=4.2  # depth of the forward arrow's head; its bands run from 1 to just short of it
PURGE_BACK_Z=1.45  # depth of the back arrow's head base; its bands run from the far end to it
# Strafe cues: direction out from the reticle and reach (share of HUD height). Above stays under the purge block;
# sideways reaches further, clear of the speed and altitude readouts.
PURGE_STRAFE={'up':((0.,-1.),.115),'down':((0.,1.),.15),'right':((1.,0.),.15),'left':((-1.,0.),.15)}
PURGE_GATES_SHIP=.2  # share of the gates view left of the ship
GATE_T,SHIP_W=6.,10.  # gates course units (ditch_purge.GATE_T, SHIP_W)
TUNNEL_LAYERS=32
TUNNEL_SEGMENTS=48
PERIPHERY_DARK=.94  # peak darkness at the frame edge with the periphery full
PERIPHERY_CLEAR=.2  # clear centre (vertical) radius at full periphery, of frame height
PERIPHERY_REACH=.8  # radius where the periphery reaches PERIPHERY_DARK, of frame height


def tunnel_stretch(width,height):
    """Horizontal stretch of the periphery rings: halfway (geometrically) between a circle and the
    frame's own aspect, so a wide frame darkens at the top and bottom as well as the sides."""
    return math.sqrt(width/height)


def tunnel_layers(tunnel,width,height,fade=0.,layers=TUNNEL_LAYERS):
    """G-load vision (user, 2026-10-18): a very dark, well blended periphery that never closes the
    centre, then the whole view fading to black near the limit. The native lane does the monochrome.

    tunnel 0..1 brings the periphery in from the corners to a clear centre of PERIPHERY_CLEAR of the
    frame height, darkening through a wide smoothstep to PERIPHERY_DARK at PERIPHERY_REACH; fade 0..1
    is one uniform black layer on top. NanoVG has no gradients here, so the edge is a stack of
    layers: returns [(radius, alpha)], innermost first, each darkening everything outside its ring
    (radius 0: the whole frame). Radii are vertical; rings are tunnel_stretch() wider. Nothing to
    draw returns []."""
    t=min(1.,max(0.,float(tunnel)));f=min(1.,max(0.,float(fade)))
    if f>=.999:return [(0.,1.)]
    out=[]
    if t>.001:
        corner=math.hypot(width/2/tunnel_stretch(width,height),height/2)
        clear=corner+(PERIPHERY_CLEAR*height-corner)*t
        feather=(.3+(PERIPHERY_REACH-PERIPHERY_CLEAR-.3)*t)*height;peak=PERIPHERY_DARK*t;prev=0.
        for k in range(layers):
            u=(k+1)/layers;dark=peak*u*u*(3.-2.*u)
            out.append((clear+feather*k/(layers-1),1.-(1.-dark)/(1.-prev)));prev=dark
    if f>.001:out.append((0.,f))
    return out


def boost_gauge(status,profile):
    """Heat gauge plan from the flight consumer status and the flown class profile, or None."""
    if not profile or not profile.get('boost_enabled') or 'boost_heat' not in status:return None
    heat=float(status.get('boost_heat') or 0.)
    if not math.isfinite(heat):return None
    hot=float(profile.get('boost_hot_zone',.7));overheated=bool(status.get('boost_overheated'))
    state='OVERHEAT' if overheated else 'HOT' if heat>=hot else 'BOOST' if status.get('boost_active') else 'COLD' if heat<=.01 else 'WARM'
    return {'heat':min(1.,max(0.,heat)),'hot':hot,'relight':float(profile.get('boost_relight_level',.3)),
            'overheated':overheated,'active':bool(status.get('boost_active')),'state':state}


G_LEVEL_COLORS={'ok':'prograde','warn':'heat','over':'overheat'}


def g_meter(g,values,strain=0.):
    """G meter plan from per-axis G along basis rows 0..2 and the tolerance settings, or None.

    The dot shows where the force pushes the pilot, opposite the acceleration: accelerating up
    through the canopy presses you into the seat (dot down), a right strafe pushes you left.
    Row 0 is the pilot's left (flight_shaping.LATERAL_COMMAND_SIGN), so +G on it pushes the pilot right."""
    if g is None or len(g)!=3 or not _finite(g):return None
    lateral,up,forward=(float(v) for v in g)
    x=lateral/values['g-tolerance-lateral']
    y=up/(values['g-tolerance-up'] if up>=0 else values['g-tolerance-down'])
    fa=-forward/values['g-tolerance-forward']  # pushed back (negative) when thrusting forward
    load=math.sqrt(x*x+y*y+fa*fa)
    n=math.hypot(x,y)
    if n>1.4:x,y=x*1.4/n,y*1.4/n
    return {'total':math.sqrt(lateral*lateral+up*up+forward*forward),'load':load,'strain':max(0.,min(1.,float(strain))),
            'dot':(x,y),'fore_aft':max(-1.4,min(1.4,fa)),'level':'over' if load>=1. else 'warn' if load>=.7 else 'ok'}


def _dot(a,b):return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def _finite(values):return all(math.isfinite(v) for v in values)


def camera_ok(axes,fov,k,aspect):
    """Rigid camera with a usable field of view, or False (never repair unknown game data)."""
    if len(axes)!=3 or not all(len(a)>=3 and _finite(a[:3]) for a in axes):return False
    if not _finite((fov,k,aspect)) or aspect<=0:return False
    for i in range(3):
        if abs(_dot(axes[i],axes[i])-1)>.02:return False
        for j in range(i+1,3):
            if abs(_dot(axes[i],axes[j]))>.02:return False
    half=fov*k/4  # degrees: both native half multiplications (T56)
    return 1.<half<89.


def project(d,axes,fov,k,aspect):
    """T56 world-direction projection of unit d: (X, Y, w), screen-normalized, w>0 in front."""
    theta=fov*k*math.pi/720
    t=math.tan(theta)
    qx,qy,qz=(_dot(d,a) for a in axes)
    return qx/(aspect*t),qy/t,-qz


def to_frame(X,Y,w,width,height):
    return (1+X/w)/2*width,(1-Y/w)/2*height


def edge_point(dx,dy,width,height,margin):
    """Where a ray from the frame centre along (dx,dy) meets the safe rectangle."""
    cx,cy=width/2,height/2
    s=min(((cx-margin)/abs(dx)) if dx else math.inf,((cy-margin)/abs(dy)) if dy else math.inf)
    return cx+s*dx,cy+s*dy


def layout(sample,camera,width,height,show_readout=True,show_marker=True):
    """Everything to draw for one frame; pure, for tests.

    sample: (velocity_world, (right, up, forward), target_speed or None).
    camera: (axes B0..B2, fov field, multiplier k, graphics aspect) or None when unusable.
    """
    velocity,basis,target=sample
    speed=math.sqrt(_dot(velocity,velocity))
    out={'readout':None,'markers':[]}
    if show_readout:
        port,up,forward=basis  # row 0 is the pilot's left; LAT reads + drifting right
        out['readout']={'speed':speed,'forward':_dot(velocity,forward),'lateral':-_dot(velocity,port),
                        'vertical':_dot(velocity,up),'target':target}
    if not show_marker or camera is None or speed<MIN_MARKER_SPEED:return out
    m=direction_marker(tuple(v/speed for v in velocity),camera,width,height)
    if m is not None:out['markers'].append({'front':('prograde',),'behind':('retrograde',),'edge':('edge',)}[m[0]]+m[1:])
    return out


def direction_marker(d,camera,width,height):
    """Screen marker for unit world direction d: ('front', x, y) where it is on screen ahead,
    ('behind', x, y) where -d is on screen ahead, else ('edge', x, y, angle) pointing the way to
    turn towards d; None when no direction is defined."""
    axes,fov,k,aspect=camera
    X,Y,w=project(d,axes,fov,k,aspect)
    margin=EDGE_MARGIN*height
    def inside(p):return margin<=p[0]<=width-margin and margin<=p[1]<=height-margin
    # d in front at (X/w, Y/w); -d in front at the same point.
    if w>NEAR_W:
        p=to_frame(X,Y,w,width,height)
        if inside(p):return ('front',)+p
    elif w<-NEAR_W:
        r=to_frame(-X,-Y,-w,width,height)
        if inside(r):return ('behind',)+r
    # Off screen, behind or at the horizon: point the way to turn towards d. The turn
    # direction on screen is (X, -Y) in every hemisphere, so no division by a near-zero w.
    dx,dy=X*width/2,-Y*height/2
    if math.hypot(dx,dy)<1e-6:return None
    ex,ey=edge_point(dx,dy,width,height,margin)
    return ('edge',ex,ey,math.atan2(dy,dx))


TARGET_MARKERS={'front':'target','behind':'target-behind','edge':'target-edge'}


def target_readout(sample,camera,width,height,max_age_ms=SAMPLE_MAX_AGE_MS):
    """Target-relative plan from a weapon_lead.target_sample() record, or None (no, stale or bad sample).

    range = |displacement| (target minus own position); dv = target velocity - own velocity;
    closing = -dot(displacement, dv) / range, positive while the gap shrinks; relative = |dv|.
    The marker is the direction of OUR motion relative to the target, -dv: where the ship drifts
    as seen from the target's frame. camera None skips the marker."""
    if sample is None:return None
    age=sample.get('age_ms')
    if age is None or not 0<=age<=max_age_ms:return None
    disp=tuple(float(v) for v in sample['displacement_xyz'])
    target_v=tuple(float(v) for v in sample['velocity_xyz']);own_v=tuple(float(v) for v in sample['shooter_velocity_xyz'])
    if len(disp)!=3 or len(target_v)!=3 or len(own_v)!=3 or not _finite(disp+target_v+own_v):return None
    dv=tuple(t-o for t,o in zip(target_v,own_v))
    rng=math.sqrt(_dot(disp,disp));relative=math.sqrt(_dot(dv,dv))
    closing=-_dot(disp,dv)/rng if rng>1e-3 else 0.  # on top of the target the sign is undefined
    out={'range':rng,'closing':closing,'relative':relative,'marker':None}
    if camera is not None and relative>=MIN_MARKER_SPEED:
        drift=tuple(-v/relative for v in dv)  # our motion relative to the target
        m=direction_marker(drift,camera,width,height)
        if m is not None:out['marker']=(TARGET_MARKERS[m[0]],)+m[1:]
    return out


def format_range(r):
    """Native units below 1 km, km with one decimal above."""
    return f"{r:.0f} u" if r<1000 else f"{r/1000:.1f} km"


def format_signed(v):
    n=round(v);return f"{n if n else 0:+d}"  # no "-0"


class HudReader:
    """In-process reads of the selected camera pose and graphics aspect (render thread)."""
    def __init__(self,base,read=C.string_at):
        self.base=int(base);self.read=read;self.verified=None

    def verify(self):
        self.verified=all(self.read(self.base+rva,len(raw))==raw for rva,raw in PINS.items())
        return self.verified

    def _q(self,address):return int.from_bytes(self.read(address,8),'little')

    def _floats(self,address,n):return tuple(memoryview(bytearray(self.read(address,4*n))).cast('f'))

    def fonts(self,ctx):
        """{name: font id} from the context's fontstash; {} when it looks malformed."""
        fs=self._q(ctx+FONTSTASH)
        if fs<0x10000:return {}
        array=self._q(fs+FONT_ARRAY);n=int.from_bytes(self.read(fs+FONT_COUNT,4),'little',signed=True)
        if array<0x10000 or not 1<=n<=64:return {}
        out={}
        for i in range(n):
            font=self._q(array+8*i)
            if font<0x10000:continue
            name=self.read(font+FONT_NAME,64).split(b'\0')[0]
            if name and all(32<=c<127 for c in name):out.setdefault(name.decode('ascii'),i)
        return out

    def camera(self):
        app=self._q(self.base+APP_GLOBAL)
        graphics=self._q(self.base+GRAPHICS)
        if app<0x10000 or graphics<0x10000:return None
        manager=app+CAMERA_MANAGER
        pose=manager+(POSE_ALTERNATE if self.read(manager+POSE_SELECT,1)!=b'\0' else POSE_PRIMARY)
        axes=tuple(self._floats(pose+16*i,3) for i in range(3))
        fov=self._floats(pose+POSE_FOV,1)[0];k=self._floats(self.base+FOV_MULTIPLIER,1)[0]
        w,h=memoryview(bytearray(self.read(graphics+0x34,8))).cast('i')
        if w<=0 or h<=0:return None
        aspect=w/h
        return (axes,fov,k,aspect) if camera_ok(axes,fov,k,aspect) else None


class HudPainter:
    """Stroke and text drawing with the native NanoVG entries resolved for the menu (hotas_draw.NVG).

    Sizes are fractions of frame height times the user's HUD scale, so the HUD keeps its
    proportions at any resolution.
    """
    def __init__(self,api):
        self.api=api;self.colors={k:(C.c_float*4)(*v) for k,v in COLORS.items()};self.text=C.create_string_buffer(64)
        self.tunnel_color=(C.c_float*4)(0.,0.,0.,0.)
        self.faded={k:(C.c_float*4)(*v) for k,v in COLORS.items()}
        self.scale=1.;self.alpha=1.

    def _paint(self,name):
        """Address of a colour, faded by self.alpha (NanoVG copies it at the call)."""
        if self.alpha>=1.:return C.addressof(self.colors[name])
        c=self.faded[name];c[3]=COLORS[name][3]*self.alpha
        return C.addressof(c)

    def _stroke(self,ctx,color,width):
        a=self.api;a['nvgStrokeColor'](ctx,self._paint(color));a['nvgStrokeWidth'](ctx,width)

    def _poly(self,ctx,points):
        a=self.api;a['nvgMoveTo'](ctx,*points[0])
        for p in points[1:]:a['nvgLineTo'](ctx,*p)

    def _circle(self,ctx,x,y,r,n=24):
        self._poly(ctx,[(x+r*math.cos(2*math.pi*i/n),y+r*math.sin(2*math.pi*i/n)) for i in range(n+1)])

    def _shape(self,ctx,color,width,draw):
        a=self.api
        for c,wd in (('shadow',width*2.2),(color,width)):
            a['nvgBeginPath'](ctx);self._stroke(ctx,c,wd);draw();a['nvgStroke'](ctx)

    def _line(self,h):return max(1.,.0016*h*self.scale)

    def prograde(self,ctx,x,y,h):
        r=.0085*h*self.scale
        def draw():
            self._circle(ctx,x,y,r)
            self._poly(ctx,[(x-r,y),(x-2.2*r,y)]);self._poly(ctx,[(x+r,y),(x+2.2*r,y)]);self._poly(ctx,[(x,y-r),(x,y-1.9*r)])
        self._shape(ctx,'prograde',self._line(h),draw)

    def retrograde(self,ctx,x,y,h):
        r=.0085*h*self.scale;c=r*.7071
        def draw():
            self._circle(ctx,x,y,r)
            self._poly(ctx,[(x-c,y-c),(x+c,y+c)]);self._poly(ctx,[(x-c,y+c),(x+c,y-c)])
            for ang in (math.pi/2,math.pi*7/6,math.pi*11/6):
                self._poly(ctx,[(x+r*math.cos(ang),y-r*math.sin(ang)),(x+1.9*r*math.cos(ang),y-1.9*r*math.sin(ang))])
        self._shape(ctx,'retrograde',self._line(h),draw)

    def edge(self,ctx,x,y,angle,h,color='prograde'):
        s=.014*h*self.scale;ca,sa=math.cos(angle),math.sin(angle)
        tip=(x+s*ca,y+s*sa);left=(x-s*.6*ca-s*.6*sa,y-s*.6*sa+s*.6*ca);right=(x-s*.6*ca+s*.6*sa,y-s*.6*sa-s*.6*ca)
        self._shape(ctx,color,self._line(h)*1.2,lambda:self._poly(ctx,[left,tip,right,left]))

    def target_marker(self,ctx,x,y,h,behind=False):
        """Violet diamond where we drift relative to the target; crossed when that point is behind
        (the diamond then marks where we drift away from), as retrograde is to prograde."""
        r=.0105*h*self.scale;c=r*.5;d=.22*r
        def draw():
            self._poly(ctx,[(x,y-r),(x+r,y),(x,y+r),(x-r,y),(x,y-r)])
            if behind:self._poly(ctx,[(x-c,y-c),(x+c,y+c)]);self._poly(ctx,[(x-c,y+c),(x+c,y-c)])
            else:self._poly(ctx,[(x-d,y),(x+d,y)])
        self._shape(ctx,'target',self._line(h),draw)

    def target(self,ctx,t,width,height,font=None):
        """Two right-aligned lines under the speed readout: range and relative speed, then closing
        speed coloured amber while closing and green while opening."""
        h=height*self.scale;x=width/2-.2*h;y=height/2+.01*h+.086*h
        self.api['nvgTextAlign'](ctx,4|64)  # NVG_ALIGN_RIGHT | NVG_ALIGN_BASELINE
        self.label(ctx,x,y,f"TGT {format_range(t['range'])}  REL {t['relative']:.0f}",.016*h,'target',font)
        n=round(t['closing'])
        self.label(ctx,x,y+.02*h,f"CLS {format_signed(t['closing'])}",.016*h,'heat' if n>0 else 'prograde' if n<0 else 'dim',font)

    def label(self,ctx,x,y,text,size,color,font=None):
        a=self.api;self.text.value=text.encode('ascii','replace')[:63];a['nvgFontSize'](ctx,size)
        if font is not None:a['nvgFontFaceId'](ctx,font)
        for c,o in (('shadow',max(1.,size*.05)),(color,0.)):
            a['nvgFillColor'](ctx,self._paint(c));a['nvgText'](ctx,x+o,y+o,C.addressof(self.text),None)

    def _rect(self,ctx,x,y,w,h,color):
        a=self.api;a['nvgBeginPath'](ctx);a['nvgFillColor'](ctx,self._paint(color));a['nvgRect'](ctx,x,y,w,h);a['nvgFill'](ctx)

    def gauge(self,ctx,g,width,height,font=None):
        """Vertical heat bar right of the reticle, mirroring the speed readout on the left."""
        h=height*self.scale;w=.011*h;tall=.13*h;x=width/2+.2*h;bottom=height/2+.065*h;top=bottom-tall
        self._rect(ctx,x-w*.15,top-w*.15,w*1.3,tall+w*.3,'shadow')
        self._rect(ctx,x,top,w,tall,'track')
        self._rect(ctx,x,top,w,tall*(1-g['hot']),'hotzone')
        color='overheat' if g['overheated'] else 'heat' if g['heat']>=g['hot'] else 'dim'
        fill=tall*g['heat']
        if fill>0:self._rect(ctx,x,bottom-fill,w,fill,color)
        y=bottom-tall*g['relight']
        self._shape(ctx,'dim',self._line(height),lambda:self._poly(ctx,[(x-w*.6,y),(x,y)]))
        self.api['nvgTextAlign'](ctx,2|64)  # NVG_ALIGN_CENTER | NVG_ALIGN_BASELINE
        self.label(ctx,x+w/2,top-.012*h,g['state'],.014*h,'overheat' if g['overheated'] else 'heat' if g['state']=='HOT' else 'dim',font)
        self.label(ctx,x+w/2,bottom+.022*h,f"{g['heat']*100:.0f}%",.014*h,'dim',font)

    def _disc(self,ctx,x,y,r,color):
        a=self.api;a['nvgBeginPath'](ctx);self._circle(ctx,x,y,r,16)
        a['nvgFillColor'](ctx,self._paint(color));a['nvgFill'](ctx)

    def g_meter(self,ctx,m,width,height,font=None):
        """Below the reticle: total G, tolerance ring with the body-push dot, fore/aft bar, strain."""
        h=height*self.scale;R=.03*h;cx=width/2;cy=height/2+.15*h
        color=G_LEVEL_COLORS[m['level']]
        self._shape(ctx,'dim',self._line(height),lambda:self._circle(ctx,cx,cy,R,32))
        t=.18*R
        self._shape(ctx,'dim',self._line(height)*.8,lambda:(self._poly(ctx,[(cx-t,cy),(cx+t,cy)]),self._poly(ctx,[(cx,cy-t),(cx,cy+t)])))
        dx,dy=cx+m['dot'][0]*R,cy+m['dot'][1]*R
        if math.hypot(dx-cx,dy-cy)>t:self._shape(ctx,color,self._line(height),lambda:self._poly(ctx,[(cx,cy),(dx,dy)]))
        self._disc(ctx,dx,dy,.005*h,'shadow');self._disc(ctx,dx,dy,.0038*h,color)
        bw=.006*h;bx=cx-R-.02*h;top=cy-R
        self._rect(ctx,bx,top,bw,2*R,'track')
        fa=m['fore_aft']*R
        if abs(fa)>.5:self._rect(ctx,bx,cy-max(fa,0.),bw,abs(fa),'overheat' if abs(m['fore_aft'])>=1. else color)
        self._rect(ctx,bx-bw*.3,cy-.5,bw*1.6,1.,'dim')
        self.api['nvgTextAlign'](ctx,2|64)  # NVG_ALIGN_CENTER | NVG_ALIGN_BASELINE
        self.label(ctx,cx,cy-R-.012*h,f"{m['total']:.1f} G",.022*h,color,font)
        if m['strain']>.005:
            self.label(ctx,cx,cy+R+.024*h,f"STRAIN {m['strain']*100:.0f}%",.014*h,'overheat' if m['strain']>.5 else 'heat',font)

    def purge(self,ctx,p,width,height,font=None):
        """T93 ditch: warning lines, the purge view (the telemetry requirement trace with the lever trail, or the
        gates course), the purge and hull bars with the failure clock, centred above the reticle."""
        h=height*self.scale;cx=width/2;top=height/2-.33*h
        a=self.api;a['nvgTextAlign'](ctx,2|64)  # NVG_ALIGN_CENTER | NVG_ALIGN_BASELINE
        for i,line in enumerate(p['lines']):self.label(ctx,cx,top+i*.024*h,line,.018*h,'overheat',font)
        if p['wrecked']:return
        gw=.34*h;gx=cx-gw/2;gy=top+.06*h
        if p.get('kind')=='gates':gh=self._purge_gates(ctx,p,gx,gy,gw,h,font)
        else:
            gh=self._purge_telemetry(ctx,p,gx,gy,gw,h,font)
            if p.get('arrow') and p['warning']<=0.:self.purge_arrow(ctx,p['arrow'],p['flow'],cx,height/2,h)
        bw=gw;by=gy+gh+.018*h;bh=.008*h
        for i,(name,value,color) in enumerate((('PURGE',p['progress'],'prograde'),
                                              ('HULL',p['hull'],'overheat' if p['hull']<.35 else 'heat'))):
            y=by+i*.03*h
            self._rect(ctx,gx,y,bw,bh,'track')
            if value>0:self._rect(ctx,gx,y,bw*min(1.,value),bh,color)
            a['nvgTextAlign'](ctx,4|64)  # NVG_ALIGN_RIGHT
            self.label(ctx,gx-.008*h,y+bh,name,.014*h,'dim',font)
            a['nvgTextAlign'](ctx,1|64)  # NVG_ALIGN_LEFT
            self.label(ctx,gx+bw+.008*h,y+bh,f"{100*value:.0f}%",.014*h,'dim',font)
        fail=p.get('fail_in')  # user 2026-10-24: the hull clock is the pressure to go faster
        if fail is not None and fail<99.:
            self.label(ctx,gx+bw+.045*h,by+.03*h+bh,f"FAILURE {fail:.0f}s",.014*h,'overheat' if fail<10. else 'heat',font)

    def _purge_telemetry(self,ctx,p,gx,gy,gw,h,font):
        """The requirement trace (idle band, the steep rise with its ignition window, the burn band) a column per
        sample, the lever trail and the title. Returns the graph height."""
        gh=.09*h;span=PURGE_SPAN;now_x=gx+gw*PURGE_NOW;line=max(1.,.0016*h)
        def X(t):return now_x+t/span*gw
        def Y(v):return gy+gh*(1.-v)
        self._rect(ctx,gx,gy,gw,gh,'track')
        for t,low,high,kind in p['trace']:
            self._rect(ctx,X(t),Y(high),gw*PURGE_STEP/span+.5,max(1.,Y(low)-Y(high)),'window' if kind=='window' else 'band')
        trail=p['trail']
        if len(trail)>1:
            pts=[(X(t),Y(v)) for t,v,_ in trail]
            self._shape(ctx,'prograde' if p['inside'] else 'overheat',line,lambda:self._poly(ctx,pts))
        if p['value'] is not None:
            self._disc(ctx,now_x,Y(p['value']),.006*h,'shadow');self._disc(ctx,now_x,Y(p['value']),.0045*h,'prograde' if p['inside'] else 'overheat')
        self._shape(ctx,'dim',line*.7,lambda:self._poly(ctx,[(now_x,gy),(now_x,gy+gh)]))
        step,steps=p['step'];phase=p['phase'];head=f"{step}/{steps} {p['stage']}: {p['prompt']}"
        title=(f"PURGE SEQUENCE IN {math.ceil(p['warning'])}s - FIRST {p['stage']}: {p['prompt']}" if p['warning']>0.
               else head+' - BACK TO IDLE' if phase=='countdown' and p['early']
               else f"{head} - {p['count'] or 'STANDBY'}" if phase=='countdown'  # T99
               else head+' - IGNITION FAILED' if phase=='retry'
               else head+' - IGNITE' if phase=='window' else head+' - HOLD')
        self.api['nvgTextAlign'](ctx,2|64)
        self.label(ctx,gx+gw/2,gy-.008*h,title,.016*h,'heat',font)
        return gh

    def _purge_gates(self,ctx,p,gx,gy,gw,h,font):
        """The gates course: walls with their openings, the sea floor, the ship's trail and the ship, the title.
        Returns the view height."""
        gh=.14*h;k=gw*(1.-PURGE_GATES_SHIP)/p['ahead'];ship_x=gx+gw*PURGE_GATES_SHIP
        def X(d):return ship_x+d*k
        def Y(v):return gy+gh*(1.-v)
        self._rect(ctx,gx,gy,gw,gh,'track')
        self._rect(ctx,gx,Y(0.)-1.,gw,2.,'heat')  # the sea floor
        wall=GATE_T*k
        for d,low,high,state in p['gates']:
            x0,x1=max(gx,X(d)),min(gx+gw,X(d)+wall)
            if x1<=x0:continue
            color='overheat' if state=='ahead' else 'dim' if state=='hit' else 'track'
            self._rect(ctx,x0,gy,x1-x0,Y(high)-gy,color);self._rect(ctx,x0,Y(low),x1-x0,Y(0.)-Y(low),color)
        trail=[(X(d),Y(y)) for d,y in p['trail'] if X(d)>=gx]
        if len(trail)>1:self._shape(ctx,'prograde',max(1.,.0011*h),lambda:self._poly(ctx,trail))
        sw,sh=SHIP_W*k/2,max(2.,gh*.025);sy=Y(p['ship'])
        self._fill(ctx,[(ship_x-sw,sy-sh),(ship_x+sw,sy),(ship_x-sw,sy+sh)],'go')
        if p['warning']>0.:title=f"PURGE SEQUENCE IN {math.ceil(p['warning'])}s - {p['prompt']}"
        elif p['countdown']>0.:title=f"GATES: {p['prompt']} - {p['count'] or 'STANDBY'}"
        else:title=f"GATE {min(p['passed']+1,p['total'])}/{p['total']}  {p['prompt']}"
        self.api['nvgTextAlign'](ctx,2|64)
        self.label(ctx,gx+gw/2,gy-.008*h,title,.016*h,'heat',font)
        return gh

    def _fill(self,ctx,points,color):
        a=self.api;a['nvgBeginPath'](ctx);self._poly(ctx,points);a['nvgFillColor'](ctx,self._paint(color));a['nvgFill'](ctx)

    def _shadowed(self,ctx,points,color):
        self._fill(ctx,[(x+1.5,y+1.5) for x,y in points],'shadow');self._fill(ctx,points,color)

    def purge_arrow(self,ctx,kind,flow,cx,cy,h):
        """The purge stage's cue at the reticle (user 2026-10-24: from the seat, screen-up means both forward and
        up). forward / back: a green arrow lying flat below the reticle on a ground plane that converges onto it,
        its bands flowing away into the distance (forward) or toward the pilot with the head near (back). up, down,
        left, right: a red arrow standing on a deck line on that side of the reticle, chevrons moving out at a
        constant size. flow: the phase, faster with more thrust (prototype: tools/prototypes/purge_gates.html)."""
        alpha=self.alpha
        try:
            if kind in ('forward','back'):self._ground_arrow(ctx,kind=='forward',flow,cx,cy,h,alpha)
            elif kind in PURGE_STRAFE:self._strafe_arrow(ctx,PURGE_STRAFE[kind],flow,cx,cy,h,alpha)
        finally:self.alpha=alpha

    def _ground_arrow(self,ctx,away,flow,cx,cy,h,alpha):
        # A ground plane below the eye: screen y = vp + span/z at depth z, widths shrink by 1/z.
        vp=cy+.03*h;span=.2*h;W0=.055*h;D=.55
        def at(z):return vp+span/z
        def band(z0,z1):return [(cx-W0/z0,at(z0)),(cx+W0/z0,at(z0)),(cx+W0/z1,at(z1)),(cx-W0/z1,at(z1))]
        if away:  # bands from the near edge to just short of the far head, flowing out
            near,far=1.,PURGE_HEAD_Z-.15
            for i in range(8):
                z0=near+(i*D+flow*D)%(8*D)
                if z0>far:continue
                self.alpha=alpha*(.1+.9*min(1.,(z0-near)/.35))  # fade in at the near edge
                self._shadowed(ctx,band(z0,min(z0+.5*D,far)),'go')
            self.alpha=alpha;zh=PURGE_HEAD_Z
            self._shadowed(ctx,[(cx-2.3*W0/zh,at(zh)),(cx+2.3*W0/zh,at(zh)),(cx,at(10.))],'go')
        else:  # back: bands come out of the distance toward the pilot, the head near pointing down the screen
            near,far,zb=PURGE_BACK_Z,PURGE_HEAD_Z,PURGE_BACK_Z-.1
            for i in range(8):
                z1=far-(i*D+flow*D)%(8*D)
                if z1<near:continue
                self.alpha=alpha*(.1+.9*min(1.,(far-z1)/.6))  # fade in out of the distance
                self._shadowed(ctx,band(max(near,z1-.5*D),z1),'go')
            self.alpha=alpha
            self._shadowed(ctx,[(cx-1.6*W0/zb,at(zb)),(cx+1.6*W0/zb,at(zb)),(cx,at(1.))],'go')

    def _strafe_arrow(self,ctx,d,flow,cx,cy,h,alpha):
        """Red arrow out from the reticle along d: a deck line with feet toward the reticle, chevrons moving out
        from it, a fixed head at the end. u runs out along d, v across it."""
        (dx,dy),reach=d;reach*=h
        def P(u,v):return (cx+u*dx-v*dy,cy+u*dy+v*dx)
        base=.03*h;dw=.04*h;cw=.028*h;ct=.009*h;gap=.025*h;foot=.004*h;head=.022*h
        for u0,u1,v0,v1 in ((base-foot,base,-dw,dw),(base-.01*h,base,-dw,-dw+foot),(base-.01*h,base,dw-foot,dw)):
            self._fill(ctx,[P(u0,v0),P(u1,v0),P(u1,v1),P(u0,v1)],'overheat')
        start=base+.01*h;travel=reach-head-start
        for i in range(4):
            off=(i*gap+flow*gap)%(4*gap)
            if off>travel:continue
            self.alpha=alpha*max(0.,min(1.,off/(.012*h),(travel-off)/(.016*h)))
            u=start+off
            self._shadowed(ctx,[P(u,-cw),P(u+.7*cw,0.),P(u,cw),P(u-ct,cw),P(u+.7*cw-ct,0.),P(u-ct,-cw)],'overheat')
        self.alpha=alpha
        self._shadowed(ctx,[P(reach-head,-1.2*cw),P(reach,0.),P(reach-head,1.2*cw)],'overheat')

    def readout(self,ctx,r,width,height,font=None):
        """Right-aligned block left of the reticle, fighter-HUD style."""
        h=height*self.scale;x=width/2-.2*h;y=height/2+.01*h
        self.api['nvgTextAlign'](ctx,4|64)  # NVG_ALIGN_RIGHT | NVG_ALIGN_BASELINE
        self.label(ctx,x,y,f"{r['speed']:.0f}",.036*h,'text',font)
        self.label(ctx,x,y+.02*h,'u/s',.014*h,'dim')
        line=f"FWD {r['forward']:+.0f}"+(f"  SET {r['target']:.0f}" if r.get('target') is not None else '')
        self.label(ctx,x,y+.042*h,line,.016*h,'dim')
        self.label(ctx,x,y+.062*h,f"LAT {r['lateral']:+.0f}  VRT {r['vertical']:+.0f}",.016*h,'dim')

    def planetary(self,ctx,p,width,height,font=None):
        """Left-aligned block right of the reticle (outside the boost gauge), mirroring the speed
        readout: altitude above sea level large, then vertical speed with a trend arrow, then ATM/VAC."""
        h=height*self.scale;x=width/2+.235*h;y=height/2+.01*h
        self.alpha=p['alpha']
        try:
            self.api['nvgTextAlign'](ctx,1|64)  # NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE
            if p['altitude'] is not None:
                self.label(ctx,x,y,format_altitude(p['altitude']),.036*h,'text',font)
                self.label(ctx,x,y+.02*h,'ALT u',.014*h,'dim')
            if p['vertical'] is not None:
                vy=y+.042*h;vs=p['vertical']
                if abs(vs)>=MIN_VS:
                    s=.006*h;cx=x+s;top,bottom=vy-.011*h,vy-.001*h
                    tip,base=(top,bottom) if vs>0 else (bottom,top)
                    self._shape(ctx,'text' if vs>0 else 'heat',self._line(height),
                                lambda:self._poly(ctx,[(cx-s,base),(cx,tip),(cx+s,base),(cx-s,base)]))
                self.label(ctx,x+.018*h,vy,f"VS {format_signed(vs)}",.016*h,'dim',font)
            if p['atmosphere'] is not None:
                self.label(ctx,x,y+.062*h,p['atmosphere'],.016*h,'dim',font)
            if p.get('trim') is not None:  # T88 lift trim
                self.label(ctx,x,y+.082*h,f"TRIM {100*p['trim']:+.0f}%",.016*h,'text',font)
            if p.get('ground') is not None:  # T101 gear down: hover mode and the height above ground
                self.label(ctx,x,y+.102*h,f"GEAR  GND {format_altitude(max(0.,p['ground']))}",.016*h,'prograde',font)
        finally:self.alpha=1.

    def landing(self,ctx,p,width,height,font=None):
        """T101 landing guide below the G meter: level ring (bubble on the high side, green inside the tilt limit)
        with the drift-over-ground line, ground tape on the right with the touchdown band, sink rate and status."""
        h=height*self.scale;R=.04*h;cx=width/2;cy=height/2+.27*h;line=self._line(height)
        self._shape(ctx,'dim',line,lambda:self._circle(ctx,cx,cy,R,32))
        self._shape(ctx,'dim',line*.8,lambda:self._circle(ctx,cx,cy,.2*R,16))
        t=.12*R
        self._shape(ctx,'dim',line*.8,lambda:(self._poly(ctx,[(cx-R-t,cy),(cx-R+t,cy)]),self._poly(ctx,[(cx+R-t,cy),(cx+R+t,cy)]),
                                                 self._poly(ctx,[(cx,cy-R-t),(cx,cy-R+t)]),self._poly(ctx,[(cx,cy+R-t),(cx,cy+R+t)])))
        dx,dy=cx+p['drift'][0]*R,cy-p['drift'][1]*R  # forward drift points up the screen
        if math.hypot(dx-cx,dy-cy)>.05*R:
            self._shape(ctx,'prograde' if p['drift_ok'] else 'heat',line*1.3,lambda:self._poly(ctx,[(cx,cy),(dx,dy)]))
        bx,by=cx+p['level'][0]*R,cy-p['level'][1]*R
        color='prograde' if p['level_ok'] else 'heat'
        self._disc(ctx,bx,by,.011*h,'shadow');self._disc(ctx,bx,by,.0085*h,color)
        # Ground tape: 0 at the bottom, TAPE_TOP at the top, touchdown band filled.
        tw=.008*h;tx=cx+R+.022*h;bottom=cy+R;tall=2*R
        def Y(u):return bottom-tall*min(1.,max(0.,u/p['top']))
        self._rect(ctx,tx,bottom-tall,tw,tall,'track')
        self._rect(ctx,tx,Y(p['band']),tw,bottom-Y(p['band']),'hotzone' if p['ground']>p['band'] else 'prograde')
        gy=Y(p['ground']);ground_color='prograde' if p['ground']<=p['band'] else 'text'
        self._shape(ctx,ground_color,line*1.4,lambda:self._poly(ctx,[(tx-.006*h,gy),(tx+tw+.006*h,gy)]))
        self.api['nvgTextAlign'](ctx,1|64)  # NVG_ALIGN_LEFT | NVG_ALIGN_BASELINE
        self.label(ctx,tx+tw+.01*h,gy+.005*h,f"GND {p['ground']:.1f}",.014*h,ground_color,font)
        self.api['nvgTextAlign'](ctx,4|64)  # NVG_ALIGN_RIGHT | NVG_ALIGN_BASELINE
        vs=p['vertical_speed'] if abs(p['vertical_speed'])>=.05 else 0.  # no "-0.0"
        self.label(ctx,cx-R-.016*h,cy+.005*h,f"VS {vs:+.1f}",.014*h,'heat' if vs<-3. else 'dim',font)
        self.api['nvgTextAlign'](ctx,2|64)  # NVG_ALIGN_CENTER | NVG_ALIGN_BASELINE
        if p['status']:
            self.label(ctx,cx,cy+R+.026*h,p['status'],.016*h,'prograde' if p['status'] in ('SETTLING','LANDING') else 'text',font)

    def mode(self,ctx,text,width,height,font=None):
        """Right-aligned above the speed readout: green SULTAN, amber NATIVE and why."""
        h=height*self.scale;x=width/2-.2*h;y=height/2-.032*h
        self.api['nvgTextAlign'](ctx,4|64)  # NVG_ALIGN_RIGHT | NVG_ALIGN_BASELINE
        self.label(ctx,x,y,text,.014*h,'prograde' if text=='SULTAN' else 'heat',font)

    def compare(self,ctx,r,width,height,fonts):
        """One sample line per game font, labelled, so the user can pick by eye."""
        h=height*self.scale;x=width/2-.2*h;y=height/2-.06*h
        self.api['nvgTextAlign'](ctx,4|64)
        for i,(name,label) in enumerate(GAME_FONTS):
            if name in fonts:
                self.label(ctx,x,y+i*.04*h,f"{label}: {r['speed']:.0f} u/s  FWD {r['forward']:+.0f}",.026*h,'text',fonts[name])

    def tunnel(self,ctx,layers,width,height,blackout=False,red=0.):
        """tunnel_layers() plan, or the whole frame black. Each ring layer is one simple polygon: the
        frame (overdrawn so its edges stay off screen) with a slit below the ellipse into the ellipse
        traced the other way, so the hole needs no path winding call. The slit sits in the dark. red 0..1
        (crash daze, g_strain.daze): the periphery turns dark red and a faint red wash covers the frame."""
        a=self.api;color=self.tunnel_color
        if blackout:layers=[(0.,1.)]
        red=0. if blackout else min(1.,max(0.,float(red)))
        color[0]=DAZE_RED[0]*red;color[1]=DAZE_RED[1]*red;color[2]=DAZE_RED[2]*red
        if red>0.:layers=[(0.,DAZE_WASH*red)]+list(layers)
        pad=4.;cx,cy=width/2,height/2;n=TUNNEL_SEGMENTS;k=tunnel_stretch(width,height)
        for r,alpha in layers:
            color[3]=alpha;a['nvgBeginPath'](ctx);a['nvgFillColor'](ctx,C.addressof(color))
            if r<=.5:a['nvgRect'](ctx,-pad,-pad,width+2*pad,height+2*pad);a['nvgFill'](ctx);continue
            bottom=height+pad
            self._poly(ctx,[(cx,bottom),(width+pad,bottom),(width+pad,-pad),(-pad,-pad),(-pad,bottom),(cx,bottom)])
            # Bottom of the ellipse, then left, top, right: opposite to the frame's direction.
            for i in range(n+1):
                th=2*math.pi*i/n;a['nvgLineTo'](ctx,cx-k*r*math.sin(th),cy+r*math.cos(th))
            a['nvgLineTo'](ctx,cx,bottom);a['nvgFill'](ctx)

    def draw(self,ctx,plan,width,height,scale=1.,font=None,fonts=None,gauge=None,gmeter=None,target=None,
             planet=None,mode=None,purge=None,landing=None):
        """fonts (name -> id) switches the readout to the font comparison; gauge: boost_gauge() plan;
        gmeter: g_meter() plan; target: target_readout() plan; planet: planetary_layout() plan;
        mode: mode_tag() text; landing: manual_landing.panel() plan. plan None draws no instruments."""
        self.scale=scale
        if plan is None:return
        if planet is not None:self.planetary(ctx,planet,width,height,font)
        if purge is not None:self.purge(ctx,purge,width,height,font)
        if landing is not None:self.landing(ctx,landing,width,height,font)
        if mode is not None and fonts is None:self.mode(ctx,mode,width,height,font)
        if gauge is not None:self.gauge(ctx,gauge,width,height,font)
        if gmeter is not None and purge is None:self.g_meter(ctx,gmeter,width,height,font)  # the purge arrows sit there
        if target is not None:
            m=target['marker']
            if m is not None:
                if m[0]=='target-edge':self.edge(ctx,m[1],m[2],m[3],height,'target')
                else:self.target_marker(ctx,m[1],m[2],height,m[0]=='target-behind')
            if fonts is None:self.target(ctx,target,width,height,font)
        for marker in plan['markers']:
            kind=marker[0]
            if kind=='prograde':self.prograde(ctx,marker[1],marker[2],height)
            elif kind=='retrograde':self.retrograde(ctx,marker[1],marker[2],height)
            else:self.edge(ctx,marker[1],marker[2],marker[3],height)
        if plan['readout'] is not None:
            if fonts is not None:self.compare(ctx,plan['readout'],width,height,fonts)
            else:self.readout(ctx,plan['readout'],width,height,font)
