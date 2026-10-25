"""Renderer-agnostic HOTAS menu: routes, native-style rows, meters and binding capture.

The NMS page (T40 F1b) maps each Row to one cGcOptionsPageUI call and each meter to the U3
in-flow drawing; nothing here touches the game. Layout follows NMS Options: a list of category
buttons, each opening one scrolling page of rows with Headers, native Back between levels,
help in the native description panel, values applied as they change and saved on leave.
"""
from dataclasses import dataclass,field
import math
from hotas_settings import SULTAN_TUNING,AIR_CONTROLS,LANDING_CONTROLS,DAMAGE_CONTROLS,VISUAL_CONTROLS,AXIS_CHANNELS,BY_ID,CONTROLS,HEAD_CONTROLS,PROFILE_CONTROLS,SHIP_CLASSES,LEVER_BOUNDARIES,binding_conflict,describe_binding,is_visible,strafe_enabled
from flight_shaping import bipolar,lever_thrust,shape_axis
import hotas_buttons
import crash_damage
import manual_landing
from planetary_flight import AIR_CLASSES

ROOT='root'
# Placement rule (user, 2026-10-17; docs/RELEASE-RUNTIME.md "Menu pages"): a setting lives on the one
# page named for what it affects. route_controls() is the single map; tests fail if a setting is
# on no page or on two.
# Order rule (docs/RELEASE-RUNTIME.md "Order on a page"): root pages in three groups (flight model,
# controls, ship and pilot); on a page, page-wide settings before per-section ones, each section's
# switch before its knobs, a binding beside what it acts on, live readouts last, then page actions.
ROUTES=(('flight-model','Flight Model',None),('flight-tuning','Flight Tuning',None),
        ('atmosphere','Atmosphere Flight',None),
        ('flight-axes','Flight Axes',None),('buttons','Buttons',None),
        ('head-tracking','Head Tracking',None),
        ('hud','HUD',None),('g-force','G-Force',None),('combat','Combat',None),('damage','Ship Damage',None),
        ('visuals','Visuals',None),
        ('devices','Devices & Calibration','Waits on the input bus (T4/T35).'),
        ('status','Status','Not built yet.'))
SULTAN_ROUTES=('flight-tuning','atmosphere','damage','hud','g-force')
# T88 Atmosphere Flight page (user, 2026-10-19: its own mechanics, its own page): the choice, the
# planet settings shared by every class, then the edited class's air style.
AIR_STYLE_GROUPS=(('SPEED AND ENGINES',('air-terminal-speed','air-engine-loss','air-boost','air-mav-lift','air-damping')),
                  ('LIFT AND GLIDE',('air-lift','air-induced-drag','air-wing-support','air-glide-ratio','air-omni')),
                  ('HANDLING',('air-nose-authority','air-slip-authority','air-side-drag','air-weathervane','air-sway-damping',
                               'air-buffet','air-overspeed-penalty')))
AIR_STYLE_KEYS=tuple(k for _,keys in AIR_STYLE_GROUPS for k in keys)
HUD_GROUPS=((None,('hud-scale','hud-font')),
            ('FLIGHT READOUTS',('hud-mode-tag','hud-speed-readout','hud-target-relative','hud-velocity-marker','hud-boost-gauge','hud-g-meter')),
            ('PLANETARY',('hud-planetary','hud-altitude','hud-vertical-speed','hud-atmosphere','hud-trim','hud-landing')))
HUD_CONTROLS=tuple(k for _,keys in HUD_GROUPS for k in keys)
ROUTE_TITLES={ROOT:'Absolute Flight',**{r:t for r,t,_ in ROUTES}}
SUPPORT_HELP=('Save your settings and recent logs as one zip to attach to a bug report. It goes in the support folder '
              'under %LOCALAPPDATA%\\AbsoluteHOTAS (config\\support in a dev install). Nothing is sent anywhere.')
CAPTURE_PROMPT='Move an axis... (Delete clears, Back cancels)'
BUTTON_PROMPT='Press a button or hat... (Delete clears, Back cancels)'
KEY_PROMPT='Press a keyboard key... (Back to cancel)'
# Mod actions the release runtime acts on (same list as button_actions.LIVE_MOD_ACTIONS).
LIVE_MOD_ACTIONS=('mod.parking-brake','mod.gear','mod.boost','mod.trim-up','mod.trim-down','mod.trim-hover','mod.trim-reset',
                  'mod.flight-model-next','mod.flight-model-toggle','mod.lever-mode-toggle',
                  'mod.pip-toggle',
                  'mod.head-tracking-toggle','mod.head-recentre','mod.head-tracking-on','mod.head-tracking-off','mod.eject')
# Bound on the Head Tracking page (user, 2026-10-16), not in the Buttons page's FLIGHT list.
HEAD_ACTIONS=('mod.head-recentre','mod.head-tracking-toggle','mod.head-tracking-on','mod.head-tracking-off')
# Feature-specific bindings live beside their tuning, as on Head Tracking.
COMBAT_ACTIONS=('mod.pip-toggle',)
PIP_CONTROLS=('pip-visible','pip-mode')  # the Lead/Lag binding follows these
AIM_SLOWDOWN_CONTROLS=('aim-slowdown-enabled','aim-slowdown-strength','aim-slowdown-radius','aim-slowdown-escape')
AIMING_CONTROLS=PIP_CONTROLS+AIM_SLOWDOWN_CONTROLS
# Enemy pilot sections: (header id, title, controls). Header ids are kept from the earlier layout.
AI_SECTIONS=(('ai-header','ENEMY PILOTS',('ai-pilots','ai-readability','ai-lateral-grip','ai-turn-rate','ai-thrust-fade',
                                          'ai-attack-delay','ai-evasive-commitment','ai-boost-alignment')),
             ('ai-aim-tuning-enabled-header','GUN ACCURACY',('ai-aim-tuning-enabled','ai-aim-precision','ai-evasion-loss',
                                                            'ai-evasion-acceleration','ai-aim-recovery')),
             ('ai-low-health-beams-header','LOW-HEALTH BEAMS',('ai-low-health-beams','ai-beam-switch-health')),
             ('ai-shield-recharge-header','NPC SHIELDS',('ai-shield-recharge','ai-shield-delay-scale','ai-shield-refill-scale')))
AI_CONTROLS=tuple(k for _,_,keys in AI_SECTIONS for k in keys)
PENDING='pending live test'
G_CONTROLS=('g-effects-enabled','g-blackout-controls','g-tolerance-up','g-tolerance-down','g-tolerance-lateral',
            'g-tolerance-forward','g-onset','g-recovery','g-unit-scale')
CRASH_CONTROLS=('crash-damage','crash-safe-speed','crash-damage-per-us','crash-glancing','crash-break-speed','crash-disable-speed')
ALARM_CONTROLS=('alarm-sounds','alarm-voice-volume','alarm-lights')
# Sultan HOTAS lever, bottom to top: each zone's switch sits with its boundaries.
LEVER_ORDER=('lever-controls','lever-reverse-mode','lever-reverse-end','lever-parking-end','lever-coast-end',
             'lever-cruise-enabled','lever-cruise-start','lever-cruise-end','lever-cruise-level','lever-full-start',
             'lever-boost-enabled','lever-boost-start')


@dataclass
class Row:
    kind:str  # header | button | bool | float | choice | binding | meter | select (choice not backed by a setting)
    id:str
    label:str
    help:str=''
    enabled:bool=True
    value:object=None  # display value: bool, number in display units, choice index, text
    low:float=0.
    high:float=1.
    step:float=.01
    suffix:str=''
    options:tuple=()
    meter:dict=None


@dataclass
class Page:
    route:str
    title:str
    rows:list
    modal:dict=None  # {'kind': 'capture'|'conflict', 'text': ..., ...}


def _scale(control):return 100. if control.unit=='percent' else 1.


def _suffix(control):return {'percent':' %','multiplier':'x','per-second':' /s','speed':' u/s','acceleration':' u/s²','seconds':' s','jerk':' u/s³','pixels':' px/s','degrees':' deg','number':'','g':' g','distance':' u'}[control.unit]


def control_row(control,values,device_names=None):
    v=values[control.id]
    if control.kind=='bool':return Row('bool',control.id,control.label,control.help,value=v)
    if control.kind=='choice':
        keys=[k for k,_ in control.choices]
        return Row('choice',control.id,control.label,control.help,value=keys.index(v),options=tuple(l for _,l in control.choices))
    if control.kind=='axis-binding':
        return Row('binding',control.id,control.label,control.help,value=describe_binding(v,device_names))
    s=_scale(control)
    return Row('float',control.id,control.label,control.help,value=round(v*s,4),low=control.low*s,high=control.high*s,step=control.step*s,suffix=_suffix(control))


def _footer(route):
    return [Row('header',f'{route}-footer-header',''),
            Row('button',f'{route}-revert','Revert to saved','Undo every change since this menu was opened.'),
            Row('button',f'{route}-reset','Reset page to defaults',"Restore this page's settings to their defaults.")]


# --- meters: zone lists in instrument coordinates (axes -1..1, throttle 0..1) ---

def axis_meter(channel,values,live=None,focused=None):
    dz,sat=values[f'{channel}-deadzone'],values[f'{channel}-saturation']
    zones=[(-1.,-sat,'SAT'),(-sat,-dz,'ACTIVE'),(-dz,dz,'DEAD'),(dz,sat,'ACTIVE'),(sat,1.,'SAT')]
    highlight={f'{channel}-deadzone':(-dz,dz),f'{channel}-saturation':(-sat,sat)}.get(focused)
    raw=live.get(channel) if live else None
    markers=None if raw is None else {'input':bipolar(raw,values[f'{channel}-inverted']),'output':shape_axis(values,channel,raw)}
    return {'range':(-1.,1.),'zones':[z for z in zones if z[0]<z[1]],'highlight':highlight,'markers':markers}


def throttle_meter(values,live=None,focused=None):
    raw=live.get('throttle') if live else None
    sultan=values['flight-model']=='sultan'
    if values['throttle-style']=='hotas':
        if sultan:
            r,p,c,cs,ce,f,b=(values['lever-'+key] for key in LEVER_BOUNDARIES)
            zones=[(0.,r,'REVERSE'),(r,p,'PARK'),(p,c,'COAST')]
            if values['lever-cruise-enabled']:zones.extend(((c,cs,'FORWARD'),(cs,ce,'CRUISE'),(ce,f,'FORWARD')))
            else:zones.append((c,f,'FORWARD'))
            if values['lever-boost-enabled']:zones.extend(((f,b,'FULL'),(b,1.,'BOOST')))
            else:zones.append((f,1.,'FULL'))
            highlight=(values[focused],) if focused in ('lever-'+k for k in LEVER_BOUNDARIES) else None
            markers=None if raw is None else {'input':1.-raw if values['throttle-inverted'] else raw}
            return {'range':(0.,1.),'zones':[z for z in zones if z[0]<z[1]],'highlight':highlight,'markers':markers}
        idle,sat=values['throttle-idle-plateau'],values['throttle-saturation']
        full=idle+sat*(1.-idle)
        zones=[(0.,idle,'IDLE'),(idle,full,'FORWARD'),(full,1.,'FULL')]
        if values['cruise-detent-enabled']:
            c,w=values['throttle-detent-center'],values['throttle-detent-width']
            zones.append((max(0.,c-w),min(1.,c+w),'CRUISE'))
        highlight={'throttle-idle-plateau':(idle,),'throttle-saturation':(full,),
                   'throttle-detent-center':(values['throttle-detent-center'],),
                   'throttle-detent-width':(values['throttle-detent-center']-values['throttle-detent-width'],values['throttle-detent-center']+values['throttle-detent-width'])}.get(focused)
        markers=None
        if raw is not None:
            lever=1.-raw if values['throttle-inverted'] else raw
            detent=(values['throttle-detent-center'],values['throttle-detent-width']) if values['cruise-detent-enabled'] else None
            markers={'input':lever,'output':lever_thrust(lever,idle,sat,detent)}
        return {'range':(0.,1.),'zones':[z for z in zones if z[0]<z[1]],'highlight':highlight,'markers':markers}
    dz=values['throttle-deadzone']
    if values['hosas-stick-mode']=='direct' or sultan:
        sat=values['hosas-saturation']
        zones=[(-1.,-sat,'FULL RETRO' if sultan else 'FULL BRAKE'),(-sat,-dz,'RETRO' if sultan else 'BRAKE'),(-dz,dz,'COAST' if sultan else 'STOP'),(dz,sat,'THRUST'),(sat,1.,'FULL')]
        highlight={'throttle-deadzone':(-dz,dz),'hosas-saturation':(-sat,sat)}.get(focused)
    else:
        zones=[(-1.,-dz,'SLOWER'),(-dz,dz,'HOLD'),(dz,1.,'FASTER')]
        highlight={'throttle-deadzone':(-dz,dz)}.get(focused)
    markers=None if raw is None else {'input':bipolar(raw,values['throttle-inverted'])}
    if markers is not None and live.get('throttle-target') is not None:markers['target']=live['throttle-target']
    return {'range':(-1.,1.),'zones':[z for z in zones if z[0]<z[1]],'highlight':highlight,'markers':markers}


def bindable(action_id):
    """Actions the release runtime delivers today: live mod actions and keyboard/mouse output."""
    a=hotas_buttons.action(action_id)
    if a is not None and a.id=='mod.gear' and not manual_landing.ENABLED:return False  # manual landing off (user 2026-10-25)
    return a is not None and (a.id in LIVE_MOD_ACTIONS or a.lane in ('key','mouse'))


def _action_rows(action_id,label,help_text,b,device_names,has_modifier=False):
    """One row per action (user 2026-10-16: less clutter). Behaviour is the action's default; the
    layer comes from capture (shift held while pressing), and pressing the bound button again clears."""
    value=hotas_buttons.describe_input(b['token'] if b else None,device_names)
    if b and b['layer']=='shift':value+='  + shift'
    how=' Select, then press a button or hat direction'+(' (hold shift for the shift layer)' if has_modifier else '')+'. Press the bound button again to clear.'
    return [Row('binding','button-bind:'+action_id,label,help_text+how,value=value)]


CURSOR_STATES={'on':'STICK','flight':'IN FLIGHT, OFF','off':'OFF','key output off':'OFF','unfocused':'OFF',
               'hotas menu':'OFF IN THIS MENU','no frontend flag':'NO MENU SIGNAL'}
KEY_OUTPUT_STATES={'unqualified':'OFF: TURN ON THE SWITCH ABOVE','unfocused':'WAITING: NMS NOT FOCUSED','active':'ON','idle':'ON'}


def button_rows(buttons,values,device_names=None,live=None):
    """Buttons page (T61). buttons: {'bindings': [...], 'modifier': token or None}."""
    bound={b['action']:b for b in buttons.get('bindings',())}
    modifier=buttons.get('modifier')
    # Shift first: it is held while binding everything below.
    rows=[Row('header','buttons-modifier-header','SHIFT BUTTON'),
          Row('binding','button-modifier','Shift button','Hold it while binding to put a binding on the shift layer, so one button or hat can carry two actions. Press the bound button again to clear.',
              value=hotas_buttons.describe_input(modifier,device_names)),
          Row('header','buttons-flight-header','FLIGHT')]
    for a in hotas_buttons.ACTIONS:
        if a.group!='flight' or a.id in HEAD_ACTIONS or a.id in COMBAT_ACTIONS:continue
        if a.id=='mod.gear' and not manual_landing.ENABLED:continue  # manual landing off (user 2026-10-25)
        if a.id not in LIVE_MOD_ACTIONS:
            rows.append(Row('button','button-bind:'+a.id,a.label,a.help+' Not part of this runtime yet.',enabled=False));continue
        rows+=_action_rows(a.id,a.label,a.help,bound.get(a.id),device_names,modifier is not None)
        if a.id=='mod.parking-brake':rows+=_visible_rows(('parking-brake-mode',),values)  # T106: latched or hold
        if a.id=='mod.eject':rows+=_visible_rows(('eject-hold-time',),values)  # T93: this action's hold
    rows.append(Row('header','buttons-key-header','KEYBOARD & MOUSE'))
    rows.append(control_row(BY_ID['key-output-enabled'],values))
    status=(live or {}).get('key-output')
    if status:
        text='KEY OUTPUT: '+KEY_OUTPUT_STATES.get(status['output'],str(status['output']).upper())
        if status.get('cursor'):text+='  CURSOR: '+CURSOR_STATES.get(status['cursor'],str(status['cursor']).upper())
        if status.get('held'):text+='  SENDING: '+', '.join(_action_title(a) for a in status['held'])
        if status.get('send_failures'):text+=f"  SENDINPUT FAILURES: {status['send_failures']}"
        rows.append(Row('header','buttons-key-status',text))
    for group,presets in hotas_buttons.KEY_PRESETS:
        rows.append(Row('header','buttons-preset-'+group.lower().replace(' ','-'),group))
        if group=='DIALOGS & MENUS':rows+=_visible_rows(('cursor-stick-enabled','cursor-speed','cursor-invert-y'),values)
        for action_id,label in presets:
            rows+=_action_rows(action_id,label,'Sends this key to NMS while NMS has focus.',bound.get(action_id),device_names,modifier is not None)
    rows.append(Row('header','buttons-custom-header','CUSTOM KEYS'))
    for b in buttons.get('bindings',()):
        if b['action'].startswith(('key.','mouse.')) and b['action'] not in hotas_buttons.PRESET_LABELS:
            rows+=_action_rows(b['action'],_action_title(b['action']),'Sends this key to NMS while NMS has focus.',b,device_names,modifier is not None)
    rows.append(Row('button','button-add-key','Add custom key','Press a keyboard key, then the joystick button that should send it.'))
    rows.append(Row('header','buttons-ship-header','SHIP: WAITS FOR LIVE TEST (T67)'))
    for a in hotas_buttons.ACTIONS:
        if a.group=='ship':rows.append(Row('button','button-bind:'+a.id,a.label,(a.help+' ' if a.help else '')+'Native action injection is qualified in the T67 live session.',enabled=False,suffix=PENDING))
    return rows


HEAD_SOURCE_STATES={'off':'OFF','waiting':'WAITING FOR OPENTRACK (FREETRACK 2.0 ENHANCED OUTPUT)','connected':'OPENTRACK CONNECTED',
                    'stale':'OPENTRACK STOPPED SENDING','invalid':'OPENTRACK DATA INVALID'}
HEAD_CAMERA_STATES={'off':'NATIVE (HEAD TRACKING OFF)','applied':'HEAD TRACKING','frontend':'NATIVE WHILE A MENU OR MAP IS OPEN',
                    'pilot_stale':'NATIVE, NOT FLYING','pilot_inactive':'NATIVE, NOT FLYING','behaviour':'NATIVE, NOT COCKPIT VIEW',
                    'config':'NATIVE, NOT COCKPIT VIEW','no_pose':'NATIVE, WAITING FOR A HEAD POSE','expired':'NATIVE, HEAD POSE TOO OLD',
                    'stale':'NATIVE, HEAD POSE TOO OLD','ramp':'RETURNING TO HEAD TRACKING','changed':'HEAD TRACKING',
                    'output_alias':'NATIVE, UNEXPECTED CAMERA CALL','error':'NATIVE AFTER A CAMERA ERROR','faulted':'OFF AFTER REPEATED ERRORS',
                    'waiting':'NATIVE, WAITING FOR COCKPIT VIEW'}


def head_rows(values,live=None,buttons=None,device_names=None):
    """Head Tracking page: switch, recentre, stick bindings, shaping for all axes then each axis, then the live readout."""
    rows=_visible_rows(['head-tracking-enabled'],values)
    rows.append(Row('button','head-recentre','Recentre now','Take your current head pose as straight ahead.'))
    rows.append(Row('header','head-bindings-header','STICK BUTTONS'))
    buttons=buttons or {};bound={b['action']:b for b in buttons.get('bindings',())}
    for action_id in HEAD_ACTIONS:
        a=hotas_buttons.action(action_id)
        rows+=_action_rows(action_id,a.label,a.help,bound.get(action_id),device_names,buttons.get('modifier') is not None)
    rows.append(Row('header','head-all-header','ALL AXES'))
    rows+=_visible_rows(['head-deadzone','head-smoothing'],values)
    for axis in ('yaw','pitch','roll'):
        rows.append(Row('header',f'head-{axis}-header',axis.upper()))
        rows+=_visible_rows([f'head-{axis}-scale',f'head-{axis}-inverted'],values)
    rows.append(Row('header','head-live-header','LIVE'))
    head=(live or {}).get('head')
    if not head:
        rows.append(Row('header','head-no-live','HEAD TRACKING READOUT UNAVAILABLE'));return rows
    rows.append(Row('header','head-source','SOURCE: '+HEAD_SOURCE_STATES.get(head.get('source'),str(head.get('source')).upper())))
    rows.append(Row('header','head-camera','CAMERA: '+HEAD_CAMERA_STATES.get(head.get('camera'),str(head.get('camera')).upper())))
    command=head.get('command') or (0.,0.,0.)
    for axis,value in zip(('Yaw','Pitch','Roll'),command):
        rows.append(Row('readout','head-live-'+axis.lower(),'Head '+axis.lower(),value=value,suffix=' deg'))
    return rows


def _action_title(action_id):
    if action_id=='modifier':return 'the shift button'
    if action_id in hotas_buttons.PRESET_LABELS:return hotas_buttons.PRESET_LABELS[action_id]
    a=hotas_buttons.action(action_id)
    return a.label if a else action_id


def terrain_rows(values,live):
    """T107 status line under the terrain rows, only while the switch is on and something needs saying."""
    if not values.get('terrain-lookahead'):return []
    state=live.get('state')
    if state=='unavailable':return [Row('header','terrain-lookahead-live','TERRAIN LOOK-AHEAD: NOT AVAILABLE ON THIS GAME BUILD')]
    if state=='error':return [Row('header','terrain-lookahead-live','TERRAIN LOOK-AHEAD: OFF AFTER AN ERROR (SEE LOG)')]
    if 'unavailable' in (live.get('budgets'),live.get('fade')):
        return [Row('header','terrain-lookahead-live','TERRAIN LOOK-AHEAD: SOME VALUES CHANGED BY ANOTHER MOD')]
    return []


def _visible_rows(ids,values,device_names=None):
    return [control_row(BY_ID[i],values,device_names) for i in ids if is_visible(BY_ID[i],values)]


def throttle_controls():
    """The throttle section of Flight Axes (its own page until 2026-10-24: user found it confusing apart)."""
    return [c.id for c in CONTROLS if c.id in ('throttle-style','bind-throttle-axis','throttle-inverted','hosas-stick-mode','cruise-detent-enabled') or c.id.startswith(('throttle-','hosas-','rate-throttle-','lever-'))]


def route_controls(route):
    if route=='flight-model':return ['flight-controls-enabled','flight-model']
    if route=='flight-axes':
        return (['turn-rate-scale','strafe-authority']+[c.id for c in CONTROLS if any(c.id==f'bind-{ch}-axis' or c.id.startswith(ch+'-') for ch,_ in AXIS_CHANNELS)]
                +throttle_controls())
    if route=='hud':return list(HUD_CONTROLS)
    if route=='g-force':return list(G_CONTROLS)
    if route=='combat':return list(AIMING_CONTROLS+AI_CONTROLS)
    if route=='buttons':return ['key-output-enabled','cursor-stick-enabled','cursor-speed','cursor-invert-y','parking-brake-mode','eject-hold-time']
    if route=='flight-tuning':return [c.id for c in PROFILE_CONTROLS if c.id[len('tuning-'):] not in AIR_STYLE_KEYS]
    if route=='atmosphere':return ['atmosphere-model']+['tuning-'+k for k in AIR_STYLE_KEYS]+[c.id for c in AIR_CONTROLS+LANDING_CONTROLS]
    if route=='damage':return [c.id for c in DAMAGE_CONTROLS]
    if route=='visuals':return [c.id for c in VISUAL_CONTROLS]
    if route=='head-tracking':return [c.id for c in HEAD_CONTROLS]
    return []


TUNING_HELP=("Which tuning this class flies. Sultan's is built in and always there to go back to. Others are files named "
             "<Class>_<Name>.json in the flight-tunings folder; add one and it appears here. Your edits show as (edited) until saved.")


def tuning_rows(tuning):
    """Named flight tunings (2026-10-22): the Tuning choice for the edited class, plus a file error if one failed."""
    names=list(tuning['names']);selected=tuning['selected']
    if selected not in names:names.append(selected)  # its file was removed or renamed; the profile stays loaded
    labels=tuple(n+(' (edited)' if n==selected and tuning['edited'] else '')+('' if n in tuning['names'] else ' (file missing)')
                 for n in names)
    rows=[Row('select','flight-tuning-select','Tuning',TUNING_HELP,value=names.index(selected),options=labels)]
    if tuning.get('error'):rows.append(Row('header','flight-tuning-error','TUNING NOT LOADED: '+tuning['error'].upper()))
    return rows


def _throttle_rows(values,live,focused,device_names):
    """Flight Axes' THROTTLE section: style and binding, the meter, behaviour, then the lever zones."""
    rows=[Row('header','throttle-header','THROTTLE')]
    if values['flight-model']=='sultan' and values['throttle-style']=='hosas':
        rows.append(Row('header','signed-thrust-help','CENTRE COASTS / PULL FOR RETRO'))
    rows+=_visible_rows(['throttle-style','bind-throttle-axis'],values,device_names)
    rows.append(Row('meter','throttle-meter','',meter=throttle_meter(values,live,focused)))
    rows+=_visible_rows([i for i in throttle_controls() if i not in ('throttle-style','bind-throttle-axis') and i not in LEVER_ORDER],values)
    lever=_visible_rows(LEVER_ORDER,values)
    if lever:rows+=[lever[0],Row('header','lever-zones-header','LEVER ZONES')]+lever[1:]
    return rows


def build_page(route,values,live=None,focused=None,modal=None,device_names=None,editing_class=4,buttons=None,tuning=None):
    """Rows for route. live: calibrated units by channel ('pitch', ..., 'throttle'), None = no data."""
    if route==ROOT:
        rows=[Row('button',f'open-{r}',t,reason or '',enabled=reason is None) for r,t,reason in ROUTES if r not in SULTAN_ROUTES or values['flight-model']=='sultan']
        rows.append(Row('button','support-bundle','Save support bundle',SUPPORT_HELP))
        return Page(ROOT,ROUTE_TITLES[ROOT],rows,modal)
    rows=[]
    if route=='flight-model':
        rows+=_visible_rows(route_controls(route),values)
    elif route=='hud':
        for title,keys in HUD_GROUPS:
            section=_visible_rows(keys,values)
            if section and title:rows.append(Row('header','hud-'+keys[0]+'-header',title))
            rows+=section
    elif route=='g-force':
        rows+=_visible_rows(route_controls(route),values)
        g=(live or {}).get('g')
        if g and values['g-effects-enabled']:
            # Coarse steps: native labels are pooled strings, so a per-frame exact value would leak them.
            rows.append(Row('header','g-live','G: '+('BLACKED OUT' if g.get('out') else f"STRAIN {5*round(20*g.get('strain',0.))}%")+
                            ('' if g.get('installed') else '  (NATIVE EFFECT NOT INSTALLED: '+str(g.get('reason','')).upper()+')')))
    elif route=='combat':
        rows.append(Row('header','aiming-header','AIMING PIP'))
        rows+=_visible_rows(PIP_CONTROLS,values)
        bindings=buttons or {};bound={b['action']:b for b in bindings.get('bindings',())}
        for action_id in COMBAT_ACTIONS:
            a=hotas_buttons.action(action_id)
            rows+=_action_rows(action_id,a.label,a.help,bound.get(action_id),device_names,bindings.get('modifier') is not None)
        slowdown=_visible_rows(AIM_SLOWDOWN_CONTROLS,values)
        if slowdown:rows+=[Row('header','aim-slowdown-header','AIM SLOWDOWN')]+slowdown
        ai=(live or {}).get('ai-pilots');combat=(live or {}).get('npc-combat');aim=(live or {}).get('ai-aim')
        for header_id,title,keys in AI_SECTIONS:
            section=_visible_rows(keys,values)
            if section or header_id=='ai-header':rows+=[Row('header',header_id,title)]+section
            # Each section's live status closes it.
            if header_id=='ai-header' and ai:rows.append(Row('header','ai-live','PILOTS: '+str(ai.get('state','unknown')).upper()))
            if header_id=='ai-aim-tuning-enabled-header' and aim and values.get('ai-aim-tuning-enabled'):
                rows.append(Row('header','ai-aim-live','AIM: '+str(aim.get('state','unknown')).upper()))
                if 'disruption' in aim:
                    rows.append(Row('header','ai-aim-disruption','LAST GUN AIM DISRUPTION: '+str(5*round(20*aim['disruption']))+'%'))
        if combat:rows.append(Row('header','npc-combat-live','BEAMS/SHIELDS: '+str(combat.get('state','unknown')).upper()))
    elif route=='flight-axes':
        rows+=_visible_rows(['turn-rate-scale','strafe-authority'],values)
        ids=route_controls(route)
        # Stick axes, then the throttle, then the optional strafe axes.
        for channel,title in AXIS_CHANNELS:
            if channel=='strafe-lateral':rows+=_throttle_rows(values,live,focused,device_names)
            if channel.startswith('strafe') and not strafe_enabled(values):continue
            section=_visible_rows([i for i in ids if i==f'bind-{channel}-axis' or i.startswith(channel+'-')],values,device_names)
            rows.append(Row('header',f'{channel}-header',title.upper()))
            rows.append(section[0])  # binding row, then the meter directly under it
            rows.append(Row('meter',f'{channel}-meter','',meter=axis_meter(channel,values,live,focused)))
            rows+=section[1:]
    elif route=='flight-tuning':
        if values['flight-model']!='sultan':raise KeyError('Flight Tuning requires Sultan flight model')
        rows.append(Row('header','editing-class','Editing: '+SHIP_CLASSES[editing_class]))
        if editing_class==10:rows.append(Row('header','corvette-fallback','CORVETTE: WORK IN PROGRESS'))
        if tuning:
            # The tuning file's own actions sit with its picker, where "(edited)" shows.
            rows+=tuning_rows(tuning)
            builtin=tuning['selected']==SULTAN_TUNING
            rows.append(Row('button','flight-tuning-save','Save tuning',f"Write this class's profile to {tuning['selected']}'s file.",
                            enabled=not builtin,suffix="Sultan's is built in"))
            rows.append(Row('button','flight-tuning-save-new','Save as new tuning',
                            f"Write this class's profile to a new <Class>_Custom.json in {tuning['folder']} and select it. "
                            'Rename the file to rename the tuning; share the file to share the tuning.'))
        groups=(('THRUST',('acceleration-scale','forward-ratio','retro-ratio','lateral-ratio','up-ratio','down-ratio','jerk-limit')),
                ('SPEED',('speed-cap','speed-cap-multiplier','speed-hold-gain','accel-fade-exponent','brake-floor','momentum','overspeed-bleed-time')),
                ('ROTATION',('pitch-rate','yaw-rate','roll-rate','turn-response','additive-pitch-yaw','auto-bank-on-yaw')),
                ('BOOST',('boost-enabled','boost-source','boost-multiplier','boost-heat-time','boost-cool-time','boost-cold-strength','boost-hot-zone','boost-relight-level')),
                ('PARKING BRAKE',('brake-strength','settle-time','hold-threshold')))
        for group,keys in groups:
            rows.append(Row('header','tuning-group-'+keys[0],group))
            rows+=_visible_rows(['tuning-'+k for k in keys],values)
        rows.append(Row('header','tuning-readout-header','LIVE FLIGHT'))
        rotation=(live or {}).get('additive-rotation') or {}
        rotation_state=str(rotation.get('state','off')).upper()
        rotation_reason=str(rotation.get('reason','disabled for this ship class'))
        modes=str(rotation.get('mode') or '').split('+')
        rotation_title='CLASS ROTATION' if rotation.get('mode') and 'additive' not in modes else 'ADDITIVE PITCH/YAW'
        rows.append(Row('header','tuning-rotation-status',rotation_title+': '+rotation_state))
        rows.append(Row('header','tuning-rotation-reason',rotation_reason.upper()))
        native=rotation.get('native') or {}
        if 'override_applied' in native:
            applied=all(native.get(key)==1 for key in ('override_applied','admitted','fresh','qualified','complete'))
            applied_text='SHAPED' if 'additive' not in modes else 'ADDITIVE'
            rows.append(Row('header','tuning-rotation-native','NATIVE ROTATION: '+(applied_text if applied else 'FALLBACK')))
        state=(live or {}).get('newtonian') or {}
        if state:
            status='NEWTONIAN' if state.get('admitted') else 'NATIVE FALLBACK: '+str(state.get('reason','not admitted'))
            rows.append(Row('header','tuning-admission',status))
            rows.append(Row('header','tuning-command-state','COMMAND: '+str(state.get('command_state','unknown')).upper()))
            rows.append(Row('header','tuning-cap-state','SPEED CAP: '+('ACTIVE' if state.get('cap_active') else 'CLEAR')))
            if 'bleeding_overspeed' in state:
                rows.append(Row('header','tuning-bleed-state','OVERSPEED BLEED: '+('ACTIVE' if state['bleeding_overspeed'] else 'CLEAR')))
            if any(key in state for key in ('boost_active','boost_overheated','boost_heat')):
                boost_state=('OVERHEATED' if state.get('boost_overheated') else 'ACTIVE' if state.get('boost_active')
                             else 'READY' if values['tuning-boost-enabled'] else 'OFF')
                rows.append(Row('header','tuning-boost-state','BOOST: '+boost_state))
            if state.get('boost_heat') is not None:
                rows.append(Row('readout','tuning-live-boost-heat','Boost heat',value=100.*state['boost_heat'],suffix=' %'))
            if state.get('boost_multiplier') is not None:
                rows.append(Row('readout','tuning-live-boost-multiplier','Applied boost',value=state['boost_multiplier'],suffix='x'))
            rows.append(Row('header','tuning-parking-state','PARKING BRAKE: '+str(state.get('parking','off')).upper()))
            if state.get('speed') is not None:rows.append(Row('readout','tuning-live-speed','Actual speed',value=state['speed'],suffix=' u/s'))
            if state.get('forward_speed') is not None:rows.append(Row('readout','tuning-live-forward-speed','Actual forward speed',value=state['forward_speed'],suffix=' u/s'))
            if state.get('target_speed') is not None:rows.append(Row('readout','tuning-live-target','Forward target',value=state['target_speed'],suffix=' u/s'))
            port,up,forward=state.get('acceleration_body',(0.,0.,0.))  # row 0 is the pilot's left
            for axis,value in zip(('Lateral','Vertical','Forward'),(-port,up,forward)):
                rows.append(Row('readout','tuning-live-'+axis.lower(),axis+' acceleration',value=value,suffix=' u/s²'))
        else:rows.append(Row('header','tuning-no-live','FLIGHT READOUT UNAVAILABLE'))
        rows.append(Row('header','tuning-actions-header','CLASS PROFILE'))
        rows.append(Row('button','flight-tuning-copy-all','Copy to all classes','Copy the currently edited profile into every flyable class. Revert restores the saved profiles.'))
    elif route=='atmosphere':
        if values['flight-model']!='sultan':raise KeyError('Atmosphere Flight requires Sultan flight model')
        rows+=_visible_rows(['atmosphere-model'],values)
        if values['atmosphere-model']=='sultan':
            rows.append(Row('header','air-planets-header','PLANETS (ALL CLASSES)'))
            rows+=_visible_rows([c.id for c in AIR_CONTROLS],values)
            if manual_landing.ENABLED:  # off for release (user 2026-10-25)
                rows.append(Row('header','landing-header','LANDING (ALL CLASSES)'))  # T101; Landing gear binds on Buttons
                rows+=_visible_rows([c.id for c in LANDING_CONTROLS],values)
            rows.append(Row('header','air-style-header','AIR STYLE: '+SHIP_CLASSES[editing_class].upper()))
            if editing_class not in AIR_CLASSES:
                rows.append(Row('header','air-style-native','NATIVE ATMOSPHERE FLIGHT FOR THIS CLASS (NOT QUALIFIED FOR SULTAN YET)'))
            for group,keys in AIR_STYLE_GROUPS:
                rows.append(Row('header','air-group-'+keys[0],group))
                rows+=_visible_rows(['tuning-'+k for k in keys],values)
            rows.append(Row('button','air-style-reset','Reset class air style',"Restore this class's air style. Space tuning and the planet settings stay."))
            rows.append(Row('header','air-live-header','LIVE AIR'))
            air=((live or {}).get('newtonian') or {}).get('planetary')
            if air:
                rows.append(Row('readout','air-live-altitude','Altitude above sea level',value=air['altitude'],suffix=' u'))
                rows.append(Row('readout','air-live-density','Air density',value=100.*air['density'],suffix=' %'))
                rows.append(Row('readout','air-live-engines','Engine efficiency',value=100.*air['engine_efficiency'],suffix=' %'))
                rows.append(Row('readout','air-live-gravity','Gravity',value=air['gravity'],suffix=' u/s²'))
                rows.append(Row('readout','air-live-drag','Drag',value=air['drag'],suffix=' u/s²'))
                rows.append(Row('readout','air-live-lift','Lift',value=air['lift'],suffix=' u/s²'))
                if 'overspeed' in air:  # past the terminal speed for this air (boost or dive)
                    rows.append(Row('readout','air-live-overspeed','Over terminal',value=100.*air['overspeed'],suffix=' %'))
                    rows.append(Row('readout','air-live-penalty','Air penalty',value=air.get('overspeed_multiplier',1.),suffix='x'))
                if air.get('water_owned'):
                    rows.append(Row('readout','air-live-submerged','Hull under water',value=100.*air['submerged'],suffix=' %'))
                if air.get('clearance') is not None:  # T101: the game's height above ground or sea
                    rows.append(Row('readout','air-live-clearance','Height above ground',value=air['clearance'],suffix=' u'))
                if air.get('gear'):
                    touchdown=(live or {}).get('touchdown')
                    rows.append(Row('header','air-live-gear','GEAR DOWN: HOVER MODE'+(f': {touchdown.upper()}' if touchdown else '')))
            else:rows.append(Row('header','air-live-none','NOT IN SULTAN PLANETARY FLIGHT'))
            rotation=(live or {}).get('additive-rotation') or {}
            if rotation.get('air_authority') is not None:  # T92: pitch/yaw rate share left by the air
                rows.append(Row('header','air-live-nose',f"NOSE AUTHORITY: {round(100*rotation['air_authority'])}%"))
    elif route=='damage':
        # T94 crash damage (anywhere you can hit something), T93 ditch at sea (Sultan atmosphere flight only).
        rows.append(Row('header','damage-crash-header','CRASH DAMAGE'))
        rows+=_visible_rows(CRASH_CONTROLS,values)
        crash=(live or {}).get('crash-damage')
        if crash and values.get('crash-damage'):
            state='ACTIVE' if crash.get('enabled') else str(crash.get('state','waiting')).upper()
            rows.append(Row('header','damage-crash-live','CRASH DAMAGE: '+state))
            last_hit=crash_damage.format_last_hit(crash.get('last_hit'))
            if last_hit:rows.append(Row('header','damage-crash-last-hit',last_hit))
        rows.append(Row('header','damage-ditch-header','DITCH AT SEA'))
        ditch=_visible_rows(['air-ditch','air-ditch-speed','air-ditch-sink-rate','air-sink-speed','air-purge-style','air-purge-length','air-purge-difficulty'],values)
        rows+=ditch or [Row('header','damage-ditch-unavailable','NEEDS SULTAN ATMOSPHERE FLIGHT (ATMOSPHERE FLIGHT PAGE)')]
        # T95: what the ship sounds and looks like in trouble (ditch, crash).
        alarms=_visible_rows(ALARM_CONTROLS,values)
        if alarms:rows+=[Row('header','damage-alarms-header','ALARMS')]+alarms
        alarm_state=(live or {}).get('ship-alarms') or {}
        if alarm_state.get('unavailable'):rows.append(Row('header','damage-alarms-off','ALARMS UNAVAILABLE ON THIS BUILD'))
        # Ship condition (from a crash or a ditch) closes the page, as live readouts do elsewhere.
        air=((live or {}).get('newtonian') or {}).get('planetary') or {}
        condition=air.get('condition','intact');purge=air.get('purge')
        if condition=='wrecked':rows.append(Row('header','damage-live-condition','WRECKED: HULL FAILED, EJECT'))
        elif condition!='intact':
            remaining=air.get('restart_in')
            rows.append(Row('header','damage-live-condition','ENGINES OUT'+(f": PURGE {purge['stage']} {round(100*purge['progress'])}%" if purge
                                                                          else '' if remaining is None else f': RESTART IN {math.ceil(remaining)} S')))
        if air.get('hull') is not None and air['hull']<1.:
            rows.append(Row('header','damage-live-hull',f"HULL {round(100*air['hull'])}%"))
    elif route=='visuals':
        rows+=_visible_rows(['black-space'],values)
        sky=(live or {}).get('black-space') or {}
        if values.get('black-space') and sky.get('state') in ('waiting','unavailable','error'):
            rows.append(Row('header','black-space-live',{'waiting':'BLACK SPACE: WAITING FOR THE GAME',
                                                         'unavailable':'BLACK SPACE: SKY CHANGED BY ANOTHER MOD OR BUILD',
                                                         'error':'BLACK SPACE: OFF AFTER AN ERROR (SEE LOG)'}[sky['state']]))
        elif values.get('black-space') and sky.get('palettes')=='unavailable':
            rows.append(Row('header','black-space-live','BLACK SPACE: COLOURS CHANGED BY ANOTHER MOD OR BUILD'))
        rows+=_visible_rows([i for i in route_controls(route) if i!='black-space'],values)
        rows+=terrain_rows(values,(live or {}).get('terrain-lookahead') or {})
    elif route=='buttons':
        rows+=button_rows(buttons or {},values,device_names,live)
    elif route=='head-tracking':
        rows+=head_rows(values,live,buttons,device_names)
    else:
        raise KeyError(route)
    rows+=_footer(route)
    if route=='flight-tuning':rows[-1].label='Reset class';rows[-1].help='Restore the starting profile for the class you are flying.'
    if route=='buttons':rows[-1].label='Clear all button bindings';rows[-1].help='Remove every button binding. Revert restores the saved ones.'
    return Page(route,ROUTE_TITLES[route],rows,modal)


@dataclass
class MenuController:
    """Route stack, edits, capture and conflict flow over a SettingsStore.

    capture_start/capture_cancel are the bus hooks (T35); the bus calls capture_result(token).
    """
    store:object
    capture_start:object=None
    capture_cancel:object=None
    stack:list=field(default_factory=lambda:[ROOT])
    capture:dict=None
    conflict:dict=None
    on_action:object=None  # runtime callback for one-shot actions ('head-recentre')

    @property
    def route(self):return self.stack[-1]

    def page(self,live=None,focused=None,device_names=None):
        if live and live.get('ship-class') in SHIP_CLASSES:self.store.select_class(live['ship-class'])
        modal=None
        if self.capture:
            c=self.capture
            text=KEY_PROMPT if c.get('key') else BUTTON_PROMPT if (c.get('action') or c.get('modifier')) else CAPTURE_PROMPT
            if c.get('action','').startswith(('key.','mouse.')):text=f'{_action_title(c["action"])}: '+text
            modal={'kind':'capture','text':text,'binding':c['id'],'clearable':self._bound(c)}
        elif self.conflict:
            c=self.conflict
            if c.get('modifier'):
                text=f"{hotas_buttons.describe_input(c['token'],device_names)} is bound to {_action_title(c['owner'])}. Make it the shift button instead?"
            elif c.get('action'):
                text=f"{hotas_buttons.describe_input(c['token'],device_names)} is bound to {_action_title(c['owner'])}. Move it to {_action_title(c['action'])}?"
            else:
                text=f"{describe_binding(c['token'],device_names)} is bound to {_binding_title(c['owner'])}. Move it to {_binding_title(c['id'])}?"
            modal={'kind':'conflict','binding':c['id'],'text':text,'options':('Move binding','Keep current')}
        buttons={'bindings':getattr(self.store,'buttons',[]),'modifier':getattr(self.store,'button_modifier',None)}
        return build_page(self.route,self.store.values,live,focused,modal,device_names,self.store.current_class,buttons,self._tuning())

    def _tuning(self):
        """Named tunings for the Flight Tuning page, or None when the runtime attached no tuning folder."""
        files=getattr(self.store,'tuning_files',None)
        if self.route!='flight-tuning' or files is None:return None
        c=self.store.current_class
        return {'names':files.names(c),'selected':self.store.tuning(c),'edited':self.store.tuning_edited(c),
                'folder':str(files.folder),'error':self.store.tuning_error}

    def choose(self,row_id,index):
        """A choice row not backed by a setting (the Tuning row) stepped to index."""
        if row_id!='flight-tuning-select' or self.capture:return 'ignored'
        tuning=self._tuning()
        names=list(tuning['names']) if tuning else []
        if not 0<=index<len(names):return 'ignored'
        return 'loaded' if self.store.select_tuning(names[index]) else 'failed'

    def change(self,row_id,display_value):
        """A native Float/Boolean/List row reported a new value; returns the display value to show."""
        control=BY_ID[row_id]
        if control.kind=='float':return round(self.store.set(row_id,display_value/_scale(control))*_scale(control),4)
        if control.kind=='choice':
            keys=[k for k,_ in control.choices]
            return keys.index(self.store.set(row_id,keys[display_value]))
        return self.store.set(row_id,display_value)

    def activate(self,row_id):
        if self.capture:return 'busy'  # a capture times out by itself (CAPTURE_TIMEOUT)
        # A pending conflict never blocks the page: choosing another row keeps the current binding
        # (alpha 2026-10-22: the prompt was off-screen on long pages and every row looked dead).
        if self.conflict:self.conflict=None
        if row_id.startswith('open-'):
            route=row_id[5:]
            if dict((r,reason) for r,_,reason in ROUTES).get(route,'missing') is not None:return 'unavailable'
            if route in SULTAN_ROUTES and self.store.values['flight-model']!='sultan':return 'unavailable'
            self.stack.append(route);return 'opened'
        if row_id==f'{self.route}-revert':self.store.revert();return 'reverted'
        if row_id=='head-recentre':
            if self.on_action is None:return 'unavailable'
            self.on_action('head-recentre');return 'recentred'
        if row_id=='support-bundle':
            if self.on_action is None:return 'unavailable'
            self.on_action('support-bundle');return 'saving'
        if row_id=='buttons-reset':self.store.clear_buttons();return 'reset'
        if row_id.startswith('button-bind:'):
            action_id=row_id[len('button-bind:'):]
            if not bindable(action_id) or self.capture_start is None:return 'unavailable'
            self.capture={'id':row_id,'action':action_id};self.capture_start(row_id);return 'capturing'
        if row_id in ('button-modifier','button-add-key'):
            if self.capture_start is None:return 'unavailable'
            self.capture={'id':row_id,'modifier' if row_id=='button-modifier' else 'key':True};self.capture_start(row_id);return 'capturing'
        if row_id=='flight-tuning-copy-all':self.store.copy_class_to_all();return 'copied'
        if row_id in ('flight-tuning-save','flight-tuning-save-new'):
            if getattr(self.store,'tuning_files',None) is None:return 'unavailable'
            try:self.store.save_tuning(new=row_id.endswith('new'));self.store.tuning_error=None;return 'saved'
            except (OSError,ValueError) as error:self.store.tuning_error=str(error);return 'failed'
        if row_id=='flight-tuning-reset':self.store.reset_class();return 'reset'
        if row_id=='air-style-reset':self.store.reset_air_style();return 'reset'
        if row_id==f'{self.route}-reset':self.store.reset([i for i in route_controls(self.route) if BY_ID[i].kind!='axis-binding']);return 'reset'
        if BY_ID.get(row_id) is not None and BY_ID[row_id].kind=='axis-binding':
            if self.capture_start is None:return 'unavailable'
            self.capture={'id':row_id};self.capture_start(row_id);return 'capturing'
        return 'ignored'

    def capture_result(self,token):
        if not self.capture:return 'stale'
        capture=self.capture;binding_id=capture['id'];action_id=capture.get('action');self.capture=None
        if capture.get('key'):
            # Step 1 of a custom key: the key chosen, now capture the joystick button that sends it.
            if not isinstance(token,str) or hotas_buttons.action(token) is None or hotas_buttons.action(token).lane!='key':return 'stale'
            self.capture={'id':'button-bind:'+token,'action':token};self.capture_start('button-bind:'+token);return 'key'
        token,_,suffix=token.partition('+') if isinstance(token,str) else (token,'','')
        layer='shift' if suffix=='shift' else 'normal'
        if capture.get('modifier'):
            try:owner=self.store.modifier_conflict(token)
            except ValueError:return 'stale'
            if self.store.button_modifier is not None and owner is None and hotas_buttons.canonical(token)==self.store.button_modifier:
                self.store.clear_modifier();return 'cleared'
            if owner is None:self.store.set_modifier(token);return 'bound'
            self.conflict={'id':binding_id,'modifier':True,'token':token,'owner':owner};return 'conflict'
        if action_id:
            try:owner=self.store.button_conflict(action_id,token,layer)
            except ValueError:return 'stale'  # not a button token
            current=self.store.button_for(action_id)
            if current and current['token']==hotas_buttons.canonical(token) and current['layer']==layer:
                self.store.clear_button(action_id);return 'cleared'
            if owner is None:self.store.assign_button(action_id,token,layer=layer);return 'bound'
            if owner=='mod.gear' and not manual_landing.ENABLED:
                # A hidden Landing gear binding gives its button up without asking (user 2026-10-25: binding Land
                # asked to move a binding they could no longer see).
                self.store.assign_button(action_id,token,steal_from=owner,layer=layer);return 'bound'
            self.conflict={'id':binding_id,'action':action_id,'token':token,'layer':layer,'owner':owner};return 'conflict'
        owner=binding_conflict(self.store.values,binding_id,token)
        if owner is None:
            self.store.assign_binding(binding_id,token);return 'bound'
        self.conflict={'id':binding_id,'token':token,'owner':owner};return 'conflict'

    def resolve_conflict(self,move):
        if not self.conflict:return 'stale'
        c=self.conflict;self.conflict=None
        if move and c.get('modifier'):self.store.set_modifier(c['token'],steal_from=c['owner']);return 'moved'
        if move and c.get('action'):self.store.assign_button(c['action'],c['token'],steal_from=c['owner'],layer=c.get('layer'));return 'moved'
        if move:self.store.assign_binding(c['id'],c['token'],steal_from=c['owner']);return 'moved'
        return 'kept'

    def _bound(self,capture):
        """Whether the row being captured currently has a binding to clear."""
        if capture.get('key'):return False
        if capture.get('modifier'):return getattr(self.store,'button_modifier',None) is not None
        if capture.get('action'):return self.store.button_for(capture['action']) is not None
        return self.store.values.get(capture['id']) is not None

    def clear_capture(self):
        """The capture prompt's Clear binding button: unbind the row and end the capture."""
        c=self.capture
        if not c or not self._bound(c):return 'stale'
        self.capture=None
        if self.capture_cancel:self.capture_cancel()
        if c.get('modifier'):self.store.clear_modifier()
        elif c.get('action'):self.store.clear_button(c['action'])
        else:self.store.assign_binding(c['id'],None)
        return 'cleared'

    def back(self):
        """Native Back. Returns False when the HOTAS menu itself closes (after saving)."""
        if self.capture:
            self.capture=None
            if self.capture_cancel:self.capture_cancel()
            return True
        if self.conflict:self.conflict=None;return True
        if len(self.stack)>1:self.stack.pop();self.store.save();return True
        self.store.save();return False


def _binding_title(binding_id):
    channel=binding_id[len('bind-'):-len('-axis')]
    return dict(AXIS_CHANNELS).get(channel,channel.title())
