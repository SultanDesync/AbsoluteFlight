"""Digital bindings (T58): action catalog, binding validation and the lane-agnostic press router.

Follows docs/INPUT-ARCHITECTURE.md. Users bind *actions*, never raw devices to keys. The router
turns bus digital state into per-action down/edge state, and delivery lanes consume that state:
- native action substitution (T59);
- SendInput keys and mouse (T60);
- the mod's own state, such as the parking brake.
Nothing here touches the game, the OS or a device, so it is tested with plain values.

Press rules (INPUT-ARCHITECTURE §4):
- Edges come from bus press/release counters, so a tap shorter than one tick still fires.
- Holds are ref-counted across tokens.
- A press is held for at least MIN_HOLD seconds and one published step.
- Everything held is released on disarm, rebinding or a layer/context change. A token still held
  must be released before it can fire again (re-arm).
- Toggled mod states survive a disarm; toggled key/native holds do not.

Additions agreed 2026-10-16 (docs/BUTTON-BINDINGS.md):
- Behaviours tap, toggle and long press.
- A modifier (shift) layer, the architecture's "shift layer is just another context".
- User-defined keyboard/mouse actions (key.*, mouse.*), because the user asked to bind buttons to
  keystrokes and mouse input directly.
"""
from dataclasses import dataclass
import re

DEVICE=r'(\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}|virtual:[a-z0-9][a-z0-9-]*)'
DIGITAL_TOKEN=re.compile(DEVICE+r'@(?:button:(\d{1,3})|pov:([0-3]):(up|right|down|left))',re.I)
POV_DIRECTIONS=('up','right','down','left')
BUTTON_COUNT=128
MIN_HOLD=.05  # seconds; also the length of a tap
LONG_PRESS=.5  # seconds
MAX_EDGES=8  # per token per step; more than this is treated as noise and only the level is used
BEHAVIOURS=('press','tap','toggle','long')
LAYERS=('normal','shift')
CONTEXTS=('any',)  # only 'any' has a qualified signal (INPUT-ARCHITECTURE §2, §5)


@dataclass(frozen=True)
class Action:
    id:str
    label:str
    group:str  # flight | ship | custom
    lane:str  # mod | native | key | mouse
    behaviours:tuple=BEHAVIOURS
    native:int=None  # cGcInputActions value, NMSpy 181442.0 (LIKELY for build 180836; T59 qualifies)
    help:str=''


def _ship(id_,label,native,help_=''):return Action(f'ship.{id_}',label,'ship','native',('press','tap','toggle','long'),native,help_)


ACTIONS=(
    Action('mod.boost','Sultan boost','flight','mod',('press',),help='Hold for extra acceleration, limited by heat. Enable boost in the ship class tuning; the speed cap stays the same.'),
    Action('mod.parking-brake','Parking brake','flight','mod',('toggle','press'),help='Thrust the ship to a stop and hold it (Sultan\'s Flight Tuning).'),
    Action('mod.gear','Landing gear','flight','mod',('toggle','press'),help='Gear down: hover mode for landing in Sultan atmosphere flight (Manual landing on the Atmosphere Flight page). Low speed cap, gravity held, no boost, and the game no longer pushes you off the ground. A two-position switch can hold it down (Press).'),
    Action('mod.trim-up','Trim up','flight','mod',('press',),help='Hold to add lift trim: a steady upward thrust that stays set after you let go (Sultan atmosphere flight). A tap nudges it.'),
    Action('mod.trim-down','Trim down','flight','mod',('press',),help='Hold to take lift trim away, or trim downward.'),
    Action('mod.trim-hover','Trim to hover','flight','mod',('tap','long'),help='Set the lift trim to exactly hold the ship against gravity at its current attitude.'),
    Action('mod.trim-reset','Trim reset','flight','mod',('tap','long'),help='Clear the lift trim. It also clears on landing.'),
    Action('mod.flight-model-next','Next flight model','flight','mod',('tap','long'),help='Cycle Vanilla, Vanilla + strafe and Sultan\'s Flight Tuning.'),
    Action('mod.flight-model-toggle','Flight model toggle','flight','mod',('tap','long'),help='Switch between your two chosen flight models.'),
    Action('mod.lever-mode-toggle','Lever: speed / thrust','flight','mod',('tap','long'),help='Switch the HOTAS lever between speed target and thrust.'),
    Action('mod.pip-toggle','Switch lead / lag pip','flight','mod',('tap','long'),help='Switch the aiming pip between Lead and Lag. Visibility is set separately on Combat. Lag initially supports manual aiming in Sultan space flight.'),
    Action('mod.head-tracking-toggle','Head tracking on/off','flight','mod',('tap','long'),help='Turn head tracking on or off.'),
    Action('mod.head-recentre','Recentre head tracking','flight','mod',('tap','long'),help='Take the current head pose as centre.'),
    Action('mod.head-tracking-on','Head tracking on','flight','mod',('tap','long'),help='Turn head tracking on.'),
    Action('mod.head-tracking-off','Head tracking off','flight','mod',('tap','long'),help='Turn head tracking off.'),
    Action('mod.eject','Eject (hold)','flight','mod',('press',),help='Hold to leave the ship in flight, even where the game refuses (Sultan flight only). Hold time is set below. Bind it somewhere you will not press by accident.'),
    # T59 static (180836): Land 0x21 only requests landing; native launch reads Ship_Thrust 0x1B (or scalar 0x8A).
    _ship('boost','Boost',0x1D),_ship('land','Land',0x21),
    _ship('takeoff','Take off',0x1B,'Native thrust action: launches when landed or docked at an outpost.'),_ship('exit','Exit ship',0x20),
    _ship('shoot','Fire weapon',0x22),_ship('change-weapon','Change weapon',0x23),_ship('scan','Scan',0x24),
    _ship('pulse','Pulse drive',0x25),_ship('galactic-map','Galactic map',0x26),_ship('free-look','Free look',0x29),
    _ship('auto-follow-toggle','Combat follow (toggle)',0x2A),_ship('auto-follow-hold','Combat follow (hold)',0x2B),
    _ship('cycle-power','Cycle power',0x2C),_ship('next-target','Next target',0x81),_ship('previous-target','Previous target',0x82),
    _ship('closest-target','Closest target',0x83),
)
BY_ID={a.id:a for a in ACTIONS}

KEY_NAMES={**{0x41+i:chr(0x41+i) for i in range(26)},**{0x30+i:str(i) for i in range(10)},**{0x70+i:f'F{i+1}' for i in range(24)},
    **{0x60+i:f'Num {i}' for i in range(10)},0x08:'Backspace',0x09:'Tab',0x0D:'Enter',0x10:'Shift',0x11:'Ctrl',0x12:'Alt',0x1B:'Esc',
    0x20:'Space',0x21:'Page Up',0x22:'Page Down',0x23:'End',0x24:'Home',0x25:'Left',0x26:'Up',0x27:'Right',0x28:'Down',0x2D:'Insert',
    0x2E:'Delete',0x6A:'Num *',0x6B:'Num +',0x6D:'Num -',0x6E:'Num .',0x6F:'Num /',0xA0:'Left Shift',0xA1:'Right Shift',0xA2:'Left Ctrl',
    0xA3:'Right Ctrl',0xA4:'Left Alt',0xA5:'Right Alt',0xBA:';',0xBB:'=',0xBC:',',0xBD:'-',0xBE:'.',0xBF:'/',0xC0:'`',0xDB:'[',0xDC:'\\',
    0xDD:']',0xDE:"'"}
MOUSE_BUTTONS={'left':'Left click','right':'Right click','middle':'Middle click','x1':'Mouse 4','x2':'Mouse 5',
               'wheel-up':'Wheel up','wheel-down':'Wheel down'}
# A key or mouse action may name a function after a colon (key.0x45:land): it sends the same key as the bare id but
# has its own binding, so several buttons can send one key, each for what it means to the pilot (user 2026-10-25:
# "it's more about the pilot's mental map of function than the actual output"). The bare id is the key's first row.
FUNCTION=r'(?::([a-z0-9][a-z0-9-]*))?'
KEY_ACTION=re.compile(r'key\.0x([0-9A-F]{2})'+FUNCTION,re.I)
MOUSE_ACTION=re.compile(r'mouse\.([a-z0-9-]+)'+FUNCTION)
# Ready-made keyboard/mouse rows (T61), one per function: rows may share a key (FUNCTION ids). Keys are the game's own PC defaults for build 180836, read from
# metadata/input/bindings/gcinputbindings_win_{keyboard,mouse}.mbin (research/20-nms-default-bindings.md).
# Left out on purpose: S/A/D (Ship_Brake/RollLeft/RollRight), because the HOTAS flight model owns thrust and roll.
# W (Ship_Thrust) is in only as Launch (user 2026-10-25, with manual landing off): held, it takes off when landed.
# Key bindings act whenever NMS has focus, so it works while the flight controls are not armed (landed is native);
# in flight a synthetic W thrusts natively, so bind it apart from flight buttons. Boost (Left Shift) is native boost until the mod's own
# boost exists (docs/FLIGHT-ENERGY-MODEL.md); Sultan flight falls back to native while it is held.
KEY_PRESETS=(
    ('COMBAT',(('mouse.left','Fire weapon (left click)'),('key.0x47','Change weapon (G)'),('key.0x54','Closest target (T)'),
               ('key.0xBE','Next target (.)'),('key.0xBC','Previous target (,)'))),
    ('SHIP',(('key.0x57','Launch: take off when landed (hold W)'),('key.0x45:land','Land (E)'),('key.0x45:exit','Exit ship (hold E)'),
             ('key.0x45','Interact / hold prompts (E)'),('key.0xA0','Boost (Left Shift)'),('key.0x43','Scan (C)'),('key.0x20','Pulse drive (Space)'),
             ('mouse.right:zoom','Zoom (right click)'),('key.0xA4','Free look (Left Alt)'),('key.0x4D','Galactic map (M)'),('key.0xBF','Cycle power (/)'),
             ('key.0x09','Inventory (Tab)'),('key.0x1B','Options / back (Esc)'))),
    ('QUICK MENU',(('key.0x58','Open quick menu (X)'),('key.0x25','Quick menu left (Left arrow)'),('key.0x27','Quick menu right (Right arrow)'),
                   ('key.0x26','Quick menu up (Up arrow)'),('key.0x28','Quick menu down (Down arrow)'),('key.0x46','Quick menu select (F)'))),
    # Frontend pages (dialogs, menus): the stick moves the cursor (mod/stick_cursor.py), left click selects, right
    # click is Fe_Back. In flight the same clicks are Fire and ShipZoom: separate rows for separate buttons.
    ('DIALOGS & MENUS',(('mouse.left:select','Select (left click)'),('mouse.right','Back (right click)'))),
)
PRESET_LABELS={a:label for _,group in KEY_PRESETS for a,label in group}


def action(action_id):
    """Catalog entry for action_id, including user-defined key.0xNN / mouse.<button> actions, or None."""
    if action_id in BY_ID:return BY_ID[action_id]
    m=KEY_ACTION.fullmatch(action_id) if isinstance(action_id,str) else None
    if m and int(m[1],16) in KEY_NAMES:
        vk=int(m[1],16);id_=f'key.0x{vk:02X}'+(':'+m[2].lower() if m[2] else '')
        return Action(id_,PRESET_LABELS[id_] if m[2] and id_ in PRESET_LABELS else f'Key {KEY_NAMES[vk]}','custom','key',('press','tap','toggle','long'))
    m=MOUSE_ACTION.fullmatch(action_id) if isinstance(action_id,str) else None
    if m and m[1] in MOUSE_BUTTONS:
        wheel=m[1].startswith('wheel')
        return Action(action_id,PRESET_LABELS[action_id] if m[2] and action_id in PRESET_LABELS else MOUSE_BUTTONS[m[1]],'custom','mouse',('tap','long') if wheel else ('press','tap','toggle','long'))
    return None


def output(action_id):
    """The key or mouse button an action sends, without its function name: 'key.0x45:land' -> 'key.0x45'."""
    return action_id.split(':',1)[0] if isinstance(action_id,str) else action_id


def parse_token(token):
    """(device id, digital channel 0..143) for a button/hat token, or raise ValueError.

    The channel is the bus DeviceSnapshotV1 digital index: buttons 1..128 -> 0..127, POV p
    direction d -> 128 + 4p + d (up, right, down, left).
    """
    m=DIGITAL_TOKEN.fullmatch(token.strip()) if isinstance(token,str) else None
    if not m:raise ValueError(f'not a button or hat token: {token!r}')
    device=m[1].upper() if m[1].startswith('{') else m[1].lower()
    if m[2] is not None:
        n=int(m[2])
        if not 1<=n<=BUTTON_COUNT:raise ValueError(f'button must be 1..{BUTTON_COUNT}: {token!r}')
        return device,n-1
    return device,BUTTON_COUNT+int(m[3])*4+POV_DIRECTIONS.index(m[4].lower())


def canonical(token):
    device,channel=parse_token(token)
    if channel<BUTTON_COUNT:return f'{device}@button:{channel+1}'
    p,d=divmod(channel-BUTTON_COUNT,4);return f'{device}@pov:{p}:{POV_DIRECTIONS[d]}'


def describe_input(token,device_names=None):
    if token is None:return 'Unbound'
    device,channel=parse_token(token)
    name=(device_names or {}).get(device) or ('Agent '+device[8:] if device.startswith('virtual:') else 'Missing device '+device[1:9])
    if channel<BUTTON_COUNT:return f'{name} - Button {channel+1}'
    p,d=divmod(channel-BUTTON_COUNT,4);return f'{name} - Hat {p+1} {POV_DIRECTIONS[d].title()}'


@dataclass(frozen=True)
class Binding:
    action:str
    token:str
    behaviour:str='press'
    layer:str='normal'
    context:str='any'

    def to_json(self):return {'action':self.action,'token':self.token,'behaviour':self.behaviour,'layer':self.layer,'context':self.context}


def _slot(b):return (b.token,b.layer,b.context,b.behaviour=='long')


def coerce_binding(raw):
    """Validated Binding from a dict, or raise ValueError."""
    if not isinstance(raw,dict):raise ValueError('binding must be an object')
    a=action(raw.get('action'))
    if a is None:raise ValueError(f"unknown action {raw.get('action')!r}")
    behaviour=raw.get('behaviour') or a.behaviours[0]
    if behaviour not in a.behaviours:raise ValueError(f'{a.id}: behaviour {behaviour!r} not allowed (allowed: {", ".join(a.behaviours)})')
    layer=raw.get('layer','normal');context=raw.get('context','any')
    if layer not in LAYERS:raise ValueError(f'{a.id}: unknown layer {layer!r}')
    if context not in CONTEXTS:raise ValueError(f'{a.id}: context {context!r} has no qualified signal')
    return Binding(a.id,canonical(raw.get('token')),behaviour,layer,context)


def find_conflict(bindings,candidate,modifier=None):
    """What candidate collides with: 'modifier', an existing Binding, or None.

    One token carries one short binding and one long binding per layer and context. The modifier
    token carries nothing else.
    """
    if modifier is not None and canonical(modifier)==candidate.token:return 'modifier'
    for b in bindings:
        if b!=candidate and _slot(b)==_slot(candidate):return b
    return None


def load_bindings(raw_list,modifier=None):
    """(bindings, modifier, issues) from stored values. Bad or conflicting entries are dropped and reported."""
    issues=[];out=[]
    try:modifier=canonical(modifier) if modifier is not None else None
    except ValueError as e:issues.append(f'modifier: {e}; cleared');modifier=None
    for raw in raw_list if isinstance(raw_list,list) else []:
        try:b=coerce_binding(raw)
        except ValueError as e:issues.append(f'{e}; dropped');continue
        clash=find_conflict(out,b,modifier)
        if clash is not None:
            issues.append(f'{b.action} on {b.token} ({b.layer}) conflicts with {"the modifier" if clash=="modifier" else clash.action}; dropped');continue
        out.append(b)
    if not isinstance(raw_list,list) and raw_list is not None:issues.append('bindings must be a list; using none')
    return out,modifier,issues


@dataclass
class ButtonState:
    """One step of router output. held: actions down now. pressed: actions activated this step
    (tap, press-down, long, toggle-on). released: actions that went up this step."""
    held:frozenset
    pressed:tuple
    released:frozenset


class _Token:
    __slots__=('down','presses','releases','generation','since','layer','long_fired','blocked','holds')

    def __init__(self):
        self.down=False;self.presses=None;self.releases=None;self.generation=None;self.since=0.
        self.layer='normal';self.long_fired=False;self.blocked=True;self.holds=()


class ButtonRouter:
    """Bus digital state -> per-action state. step() is called once per tick with fresh input."""
    def __init__(self,bindings=(),modifier=None):
        self.tokens={};self.latched=set();self.pulses={};self.last_held=frozenset();self.configure(bindings,modifier)

    def configure(self,bindings,modifier=None):
        """New bindings: releases everything except toggled mod states, which survive only while still bound."""
        self.bindings=list(bindings);self.modifier=modifier
        bound={b.action for b in self.bindings}
        self.latched={a for a in self.latched if a in bound and BY_ID.get(a) is not None and BY_ID[a].lane=='mod'}
        self.by_slot={}
        for b in self.bindings:self.by_slot.setdefault((b.token,b.layer),[]).append(b)
        self._release_all(keep_mod=True)

    def watched(self):
        """Tokens the router needs each step (bindings plus the modifier)."""
        return sorted({b.token for b in self.bindings}|({self.modifier} if self.modifier else set()))

    def _release_all(self,keep_mod):
        for t in self.tokens.values():t.holds=();t.blocked=True;t.long_fired=False
        self.pulses={}
        if not keep_mod:self.latched=set()
        else:self.latched={a for a in self.latched if BY_ID.get(a) is not None and BY_ID[a].lane=='mod'}

    def disarm(self):
        """Focus loss, menu, runtime disarm or settings reload: release everything we pressed."""
        self._release_all(keep_mod=True)

    # --- edge handling ---

    def _activate(self,b,now,pressed,short):
        """short: a deferred short press firing on release, so a press becomes a tap."""
        if b.behaviour=='toggle':
            if b.action in self.latched:self.latched.discard(b.action)
            else:self.latched.add(b.action);pressed.append(b.action)
        elif b.behaviour=='press' and not short:
            pressed.append(b.action);return b.action
        else:
            self.pulses[b.action]=max(self.pulses.get(b.action,0.),now+MIN_HOLD);pressed.append(b.action)
        return None

    def _bindings(self,token,layer):
        found=self.by_slot.get((token,layer))
        if found is None and layer=='shift':found=self.by_slot.get((token,'normal'))  # unbound shift falls through
        return found or []

    def _on_down(self,token,t,now,pressed):
        t.since=now;t.long_fired=False
        m=self.tokens.get(self.modifier) if self.modifier else None
        t.layer='shift' if (m is not None and m.down and not m.blocked and token!=self.modifier) else 'normal'
        bs=self._bindings(token,t.layer)
        if any(b.behaviour=='long' for b in bs):return  # short bindings wait for release
        t.holds=tuple(h for h in (self._activate(b,now,pressed,False) for b in bs) if h)

    def _on_up(self,token,t,now,pressed):
        bs=self._bindings(token,t.layer)
        if any(b.behaviour=='long' for b in bs) and not t.long_fired:
            for b in bs:
                if b.behaviour!='long':self._activate(b,now,pressed,True)
        for a in t.holds:  # honour the minimum hold
            if now-t.since<MIN_HOLD:self.pulses[a]=max(self.pulses.get(a,0.),t.since+MIN_HOLD)
        t.holds=()

    def _edges(self,t,state):
        """Edge list ('down'/'up') since the last step, from counters when they are consistent."""
        down,presses,releases,generation=state
        if t.generation!=generation or t.presses is None:
            t.generation=generation;t.presses=presses;t.releases=releases
            return None  # new device or reconnect: a level baseline, never a synthesized press
        dp=(presses-t.presses)&0xFFFFFFFF;dr=(releases-t.releases)&0xFFFFFFFF
        t.presses=presses;t.releases=releases
        n=dp+dr
        if n==0 or n>MAX_EDGES or abs(dp-dr)>1 or (t.down!=down)!=(n%2==1):
            return [] if t.down==down else ['down' if down else 'up']
        first='up' if t.down else 'down'
        return [first if i%2==0 else ('down' if first=='up' else 'up') for i in range(n)]

    def step(self,read,now,eligible=True):
        """read(token) -> (down, press_count, release_count, generation) or None when unusable."""
        if not eligible:self.disarm()
        pressed=[];released_before=self.last_held
        order=sorted(self.watched(),key=lambda tok:tok!=self.modifier)  # modifier first, so the layer is current
        for token in order:
            t=self.tokens.setdefault(token,_Token());state=read(token)
            if state is None or not eligible:
                if state is None:t.presses=None
                t.down=False if state is None else bool(state[0]);t.holds=();t.blocked=True
                if state is not None:self._edges(t,state)
                continue
            edges=self._edges(t,state)
            if edges is None and not t.blocked:  # baseline: a release is honoured, a press is not
                if t.down and not state[0]:edges=['up']
                elif not t.down and state[0]:t.blocked=True
            if t.blocked:
                t.down=bool(state[0])
                if not t.down:t.blocked=False
                continue
            for edge in edges or ():
                if edge==('up' if t.down else 'down'):
                    t.down=not t.down
                    if token==self.modifier:continue  # the modifier only selects the layer
                    (self._on_down if t.down else self._on_up)(token,t,now,pressed)
            if token!=self.modifier and t.down and not t.long_fired and now-t.since>=LONG_PRESS:
                longs=[b for b in self._bindings(token,t.layer) if b.behaviour=='long']
                if longs:
                    t.long_fired=True
                    for b in longs:self._activate(b,now,pressed,True)
        held=set(self.latched)
        for t in self.tokens.values():held.update(t.holds)
        for a,until in list(self.pulses.items()):
            if until>now or a in pressed:held.add(a)
            else:del self.pulses[a]
        held=frozenset(held);self.last_held=held
        return ButtonState(held,tuple(dict.fromkeys(pressed)),released_before-held)
