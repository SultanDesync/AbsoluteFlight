"""Keyboard layout diagram for keyboard flight (T112): which key does what, drawn on the Keyboard page and as an SVG.

Pure data, no drawing: the Keyboard page draws it row by row with NanoVG (hotas_draw.KeyboardPainter), and
tools/render_keyboard_layout.py writes the same model as docs/images/keyboard-layout.svg. User 2026-10-30: "render an
image of the keyboard layout showing the bindings ... and where we transpose the vanilla inputs to color coded",
"and then put it in the keyboard menu".

Categories:
- ours: an Absolute Flight flight key (Sultan thrust, strafe, boost, parking brake, Vanilla mode)
- press: one of our keys that presses the game's own action (roll, land, pulse, scan, exit)
- moved: a game ship key moved out of the layout's way (tools/native_rebind.MOVES)
- game: a game ship key left where it is
- clash: one of our keys that is still a game ship key (move the game's keys)
"""
from hotas_buttons import GAME_KEY_NAMES,GAME_SHIP_DEFAULTS,KEY_NAMES,action,game_ship_keys

# A US ANSI keyboard in key units: per row, (vk or None, width, gap before). The navigation block starts at NAV_X.
NAV_X=15.25
ROWS=(
    ((0x1B,1,0),(0x70,1,1),(0x71,1,0),(0x72,1,0),(0x73,1,0),(0x74,1,.5),(0x75,1,0),(0x76,1,0),(0x77,1,0),
     (0x78,1,.5),(0x79,1,0),(0x7A,1,0),(0x7B,1,0)),
    ((0xC0,1,0),*((0x30+(d+1)%10,1,0) for d in range(10)),(0xBD,1,0),(0xBB,1,0),(0x08,2,0)),
    ((0x09,1.5,0),*((ord(c),1,0) for c in 'QWERTYUIOP'),(0xDB,1,0),(0xDD,1,0),(0xDC,1.5,0)),
    ((0x14,1.75,0),*((ord(c),1,0) for c in 'ASDFGHJKL'),(0xBA,1,0),(0xDE,1,0),(0x0D,2.25,0)),
    ((0xA0,2.25,0),*((ord(c),1,0) for c in 'ZXCVBNM'),(0xBC,1,0),(0xBE,1,0),(0xBF,1,0),(0xA1,2.75,0)),
    ((0xA2,1.25,0),(None,1.25,0),(0xA4,1.25,0),(0x20,6.25,0),(0xA5,1.25,0),(None,1.25,0),(None,1.25,0),(0xA3,1.25,0)),
)
NAV=({},{0:0x2D,1:0x24,2:0x21},{0:0x2E,1:0x23,2:0x22},{},{1:0x26},{0:0x25,1:0x28,2:0x27})  # per row: column -> vk
WIDTH=NAV_X+3
SHORT_NAMES={0x14:'Caps',0x1B:'Esc',0x08:'Bksp',0x09:'Tab',0x0D:'Enter',0xA0:'Shift',0xA1:'Shift',0xA2:'Ctrl',0xA3:'Ctrl',0xA4:'Alt',
             0xA5:'Alt',0x20:'Space',0x2D:'Ins',0x24:'Home',0x21:'PgUp',0x2E:'Del',0x23:'End',0x22:'PgDn',0x26:'Up',
             0x28:'Down',0x25:'Left',0x27:'Right'}
# Short function names, by our action or the game's action name.
FUNCTIONS={'mod.thrust-forward':'Thrust','mod.thrust-back':'Reverse','mod.strafe-left':'Strafe L','mod.strafe-right':'Strafe R',
           'mod.strafe-up':'Strafe up','mod.strafe-down':'Strafe dn','ship.roll-left':'Roll L','ship.roll-right':'Roll R',
           'mod.boost':'Boost','mod.parking-brake':'P.brake','key.0x45:land':'Land','mod.gear':'Gear','mod.vanilla-mode':'Vanilla',
           'mod.trim-up':'Trim up','mod.trim-down':'Trim down','mod.trim-hover':'Trim hover','mod.trim-reset':'Trim reset',
           'mod.flight-model-next':'Next model','mod.flight-model-toggle':'Model swap','mod.lever-mode-toggle':'Lever mode',
           'mod.pip-toggle':'Lead/lag','mod.head-tracking-toggle':'Head on/off','mod.head-recentre':'Head centre',
           'mod.head-tracking-on':'Head on','mod.head-tracking-off':'Head off','mod.eject':'Eject',
           'key.0x20':'Pulse','key.0x43':'Scan','key.0x45':'Interact','key.0x45:exit':'Exit','key.0x25':'QM left','key.0x27':'QM right',
           'Ship_Thrust':'Thrust','Ship_Brake':'Brake','Ship_RollLeft':'Roll L','Ship_RollRight':'Roll R','Ship_Boost':'Boost',
           'Ship_PulseJump':'Pulse','Ship_Land':'Land','Ship_Exit':'Exit','Ship_AutoFollow_Toggle':'Follow','Ship_AutoFollow_Hold':'Follow (hold)',
           'Player_TagMarker':'Tag','Ship_Scan':'Scan','Quick_Left':'QM left','Quick_Right':'QM right','Quick_Action':'QM select',
           'Quick_Menu':'Q.menu','Ship_ChangeWeapon':'Weapon','Ship_ClosestTarget':'Target','Ship_NextTarget':'Next tgt',
           'Ship_PreviousTarget':'Prev tgt','Ship_GalacticMap':'Map','Ship_FreeLook':'Free look','Ship_CyclePower':'Power',
           'Inventory':'Inventory','Fe_Options':'Options','SolarSystemMap_ToggleMap':'System','Player_ShowHUD':'HUD',
           'TextChatOpenClose':'Chat'}
CATEGORIES=(('ours','Absolute Flight'),('press',"Presses the game's action"),('moved',"Game's key, moved here"),
            ('game',"Game's key"),('clash','Clash: still the game\'s'))


# Experimental, and why the game's keys are moved rather than removed (user 2026-10-30). Shown on the Keyboard page and
# in the diagram.
EXPERIMENTAL=("Keyboard flight is experimental.",
              "The game's own ship keys are moved, not removed: they stay on the right as a fallback that still works with "
              "the mod off or faulted, and the game's prompts show them. Reset to Default in the game's Controls puts them back. "
              "To launch when landed, hold the game's own Thrust key (I after the move): take-off is the game's own.")


def fit(text,width,size,char=.58,smallest=.8):
    """Lines and font size for a key's function name: one line, shrunk to no less than smallest*size, else two lines
    at a word break."""
    if not text:return [text],size
    one=min(size,width/(char*len(text)))
    if one>=smallest*size:return [text],one
    words=text.split(' ')
    if len(words)>1:
        cut=min(range(1,len(words)),key=lambda i:abs(len(' '.join(words[:i]))-len(' '.join(words[i:]))))
        lines=[' '.join(words[:cut]),' '.join(words[cut:])]
        return lines,min(size,width/(char*max(map(len,lines))))
    return [text],width/(char*len(text))


def wrap(text,width,size,char=.55):
    """Lines of text no wider than width at font size."""
    import textwrap
    return textwrap.wrap(text,max(10,int(width/(char*size))))


def thrust_key(remaps):
    """The key the game's own Thrust is on (its launch from landed reads it), with the player's remaps; None if unbound."""
    for vk,actions in game_keys(remaps).items():
        if any(name=='Ship_Thrust' for name,_ in actions):return KEY_NAMES.get(vk) or key_name(vk)
    return None


def key_name(vk):
    return SHORT_NAMES.get(vk) or KEY_NAMES.get(vk,'')


def game_keys(remaps):
    """{vk: [(game action name, moved)]} the game uses while you fly, with the player's remaps."""
    remapped={action_name:key for _,action_name,key in remaps}
    out={}
    for action_name,vk,_ in GAME_SHIP_DEFAULTS:
        moved=action_name in remapped
        key=GAME_KEY_NAMES.get(remapped[action_name]) if moved else vk
        if key is not None:out.setdefault(key,[]).append((action_name,moved))
    return out


def classify(keyboard,remaps):
    """{vk: (category, function)} for every key with a meaning. keyboard: stored keyboard bindings (dicts with action and
    token); remaps: the game's (action set, action, key name) remaps."""
    out={}
    for vk,actions in game_keys(remaps).items():
        name,moved=actions[0]
        out[vk]=('moved' if moved else 'game',FUNCTIONS.get(name,name))
    for b in keyboard:
        vk=int(b['token'][-2:],16);a=action(b['action'])
        if a is None:continue
        function=FUNCTIONS.get(b['action'],a.label)
        category='ours' if a.lane=='mod' else 'press'
        if vk in out:  # pressing the game's own action on the game's own key for it is fine; anything else clashes
            category='press' if category=='press' and out[vk][1]==function else 'clash'
        out[vk]=(category,function)
    return out


def rows(keyboard,remaps):
    """Per keyboard row: [(x, width, key name, function, category)] in key units (WIDTH wide)."""
    meaning=classify(keyboard,remaps);out=[]
    for r,row in enumerate(ROWS):
        x=0.;keys=[]
        for vk,width,gap in row:
            x+=gap
            category,function=meaning.get(vk,('none','')) if vk is not None else ('none','')
            keys.append((x,width,key_name(vk) if vk is not None else '',function,category))
            x+=width
        for column,vk in NAV[r].items():
            category,function=meaning.get(vk,('none',''))
            keys.append((NAV_X+column,1,key_name(vk),function,category))
        out.append(keys)
    return out
