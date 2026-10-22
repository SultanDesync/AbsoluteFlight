"""HOTAS settings schema, validation and live-apply store with atomic JSON persistence.

One schema drives the native rows, the instruments, validation and the flight consumer. IDs
reuse the AbsoluteHOTAS source control IDs where the meaning carries over (research/14), so
a later profile importer (research/15) maps them directly; NEW marks NMS-only settings.
Fractions are stored 0..1 and shown as percent. Edits apply immediately, as native NMS
options do; save() runs when the menu is left, revert() restores the saved file.
"""
from dataclasses import dataclass,field
import json
import math
import os
from pathlib import Path
import re
import hotas_buttons
from ai_pilots import KNOBS as AI_KNOBS
from ai_evasion_model import AIM_KNOBS
from ai_combat import COMBAT_KNOBS

SCHEMA_VERSION=4
SCHEMA_VERSIONS=(2,3,SCHEMA_VERSION)
# Schema 4 (2026-10-20) negated the lateral strafe command so Invert off follows the stick. Older files saved
# Invert on to fix a backwards default and saved off by default, so both load as off.
LATERAL_SIGN_SCHEMA=4
# Canonical input-bus token: hardware instance GUID or the reserved D21 agent source.
AXIS_TOKEN=re.compile(r'^(\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}|(?i:virtual:agent-0))@axis:0x(3[0-7])$')
AXIS_USAGE_NAMES=('X','Y','Z','Rx','Ry','Rz','Slider 1','Slider 2')
AXIS_CHANNELS=(('pitch','Pitch'),('yaw','Yaw'),('roll','Roll'),('strafe-lateral','Lateral strafe'),('strafe-vertical','Vertical strafe'))
MIN_GAP=.05
# Flight-model profiles (D19 addendum). Sultan's Flight Tuning is the Newtonian model with the
# parking brake (docs/FLIGHT-MODEL-BRIEF.md, T55); it stays out of the menu until that build exists.
SULTAN_FLIGHT_AVAILABLE=True
FLIGHT_MODELS=(('vanilla','Vanilla'),('vanilla-strafe','Vanilla + strafe'))+((('sultan',"Sultan's Flight Tuning"),) if SULTAN_FLIGHT_AVAILABLE else ())
STRAFE_MODELS=('vanilla-strafe','sultan')
SULTAN_ONLY=(('flight-model','sultan'),)
AIR_ONLY=SULTAN_ONLY+(('atmosphere-model','sultan'),)
VANILLA_ONLY=(('flight-model',('vanilla','vanilla-strafe')),)
SHIP_CLASSES={1:'Hauler',2:'Fighter',3:'Explorer',4:'Shuttle',6:'Exotic',7:'Living ship',8:'Solar',9:'Sentinel interceptor',10:'Corvette'}
ZONE_MIN_GAP=.005


@dataclass(frozen=True)
class Control:
    id:str
    kind:str  # bool | float | choice | axis-binding
    label:str
    help:str
    default:object=None
    low:float=0.
    high:float=1.
    step:float=.01
    unit:str='percent'  # percent | multiplier | per-second | speed | degrees
    choices:tuple=()  # ((value,label),...)
    visible:tuple=()  # ((control_id,value or tuple of allowed values),...), all must match
    new:bool=False  # not a source AbsoluteHOTAS control


def _axis_controls(channel,title):
    dz=.05 if channel.startswith('strafe') else 0.
    out=[Control(f'bind-{channel}-axis','axis-binding','Binding',f'Physical axis that drives {title.lower()}. Select to rebind, then move the axis.'),
         Control(f'{channel}-inverted','bool','Invert',f'Reverse the direction of {title.lower()}.',False)]
    if channel!='strafe-vertical':
        help_text='Output multiplier after the deadzone.'+(' Shared by vertical strafe.' if channel=='strafe-lateral' else '')
        out.append(Control(f'{channel}-sensitivity','float','Sensitivity',help_text,1.,.1,3.,.05,'multiplier'))
    out+=[Control(f'{channel}-deadzone','float','Deadzone','Centre travel that produces no output.',dz,0.,.5),
          Control(f'{channel}-saturation','float','Saturation','Deflection that reaches full output.',1.,.05,1.)]
    return out


HOTAS_ONLY=(('throttle-style','hotas'),)
HOSAS_ONLY=(('throttle-style','hosas'),)
NATIVE_HOTAS=HOTAS_ONLY+VANILLA_ONLY
NATIVE_HOSAS=HOSAS_ONLY+VANILLA_ONLY
CONTROLS=(
    Control('flight-controls-enabled','bool','HOTAS flight control','Master switch. Off returns every flight axis to native NMS input.',True),
    Control('flight-model','choice','Flight model','Vanilla: the native NMS flight model on your HOTAS axes. Vanilla + strafe: the same, with lateral and vertical strafe on their own axes.'+(" Sultan's Flight Tuning: Newtonian flight; the ship keeps its momentum until you thrust, with a parking brake to stop." if SULTAN_FLIGHT_AVAILABLE else ''),'vanilla-strafe',choices=FLIGHT_MODELS,new=True),
    Control('atmosphere-model','choice','Atmosphere flight',"Native: near a planet the game flies the ship, as before. Sultan: Sultan flight carries on down to the landing, with planet gravity, air that thickens toward the ground, drag and, for sleek hulls, lift. Each class flies its own air style: the Fighter carves like a jet, the Exotic, Living, Solar and Interceptor slide and lift in any direction, the Explorer sits between, and the Hauler and Shuttle are bricks on weak lift thrusters and strong mains. Corvette keeps native flight. Landing, takeoff and pulse stay native.",'sultan',choices=(('native','Native'),('sultan','Sultan')),visible=SULTAN_ONLY,new=True),
    Control('turn-rate-scale','float','Turn rate','Pitch and yaw rate at full deflection, after the game input curve. Applies on the next flight entry.',.85,.5,1.,.05,new=True),
    Control('strafe-authority','float','Strafe strength','Maximum native strafe correction at full deflection.',60.,10.,60.,5.,'speed',visible=(('flight-model','vanilla-strafe'),),new=True),
    Control('pip-visible','bool','Show aiming pip','Show the selected aiming pip. Lag mode initially supports manual aiming in Sultan space flight.',True,new=True),
    Control('pip-mode','choice','Aiming pip','Lead: put the reticle on the pip. Lag: put the pip on the target. Lag mode initially supports manual aiming in Sultan space flight.','lead',choices=(('lead','Lead'),('lag','Lag')),new=True),
    Control('aim-slowdown-enabled','bool','Aim slowdown','Gently reduce pitch and yaw input near the firing solution, without steering for you. Initially supports manual aiming in Sultan space flight.',False,new=True),
    Control('aim-slowdown-strength','float','Maximum input reduction','Largest reduction in pitch and yaw input at the centre of the aiming bubble. The reduction fades smoothly towards its edge.',.20,0.,.50,.01,visible=(('aim-slowdown-enabled',True),),new=True),
    Control('aim-slowdown-radius','float','Slowdown radius','Angular distance from the firing solution where pitch and yaw input begin to soften. Degrees keep the bubble consistent across field of view and resolution.',1.5,.25,5.,.25,'degrees',visible=(('aim-slowdown-enabled',True),),new=True),
    Control('aim-slowdown-escape','float','Full-stick escape starts','Stick deflection where the slowdown starts fading. Full deflection always restores full pitch and yaw authority.',.75,.25,1.,.05,visible=(('aim-slowdown-enabled',True),),new=True),
    Control('ai-pilots','choice','Enemy pilots','How pirates and police fly in space combat. Readable: they keep momentum and slide through turns like a Newtonian ship, turn slower, bank before they turn and commit to their moves, so you can read them and take position. Vanilla: the native NMS pilots. Match flight model: readable with Sultan flight, vanilla otherwise. Applies to every space dogfight, not only yours.','match',choices=(('match','Match flight model'),('readable','Readable'),('vanilla','Vanilla')),new=True),
    Control('ai-readability','float','Pilot readability','Blend from vanilla towards your pilot tuning below. 0% is vanilla; 100% uses the full tuning. Match flight model uses it only with Sultan flight.',1.,0.,1.,.05,visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-lateral-grip','float','Lateral grip','How quickly pilots remove sideways drift. Lower lets them slide wider through turns; higher helps them hold your tail. The accepted feel is 0.8 /s.',*AI_KNOBS['ai-lateral-grip'],.1,'per-second',visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-turn-rate','float','Pilot turn rate','Scale minimum and maximum turn rates together for easy and hard space pilots. Lower gives you more time to read a turn; 1x keeps the accepted rates.',*AI_KNOBS['ai-turn-rate'],.05,'multiplier',visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-thrust-fade','float','Thrust fade starts','Share of top speed where pilot thrust starts fading. Lower makes speed build more gradually; higher keeps acceleration strong until nearer top speed.',*AI_KNOBS['ai-thrust-fade'],.05,visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-attack-delay','float','Attack delay','Seconds between lining up an attack and being ready to fire. Higher gives you more time to react.',*AI_KNOBS['ai-attack-delay'],.05,'seconds',visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-evasive-commitment','float','Evasive commitment','Seconds pilots commit to an urgent evasive break. Higher makes their movement easier to follow.',*AI_KNOBS['ai-evasive-commitment'],.25,'seconds',visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-boost-alignment','float','Boost alignment','Largest angle from the attack heading where pilots may boost. Lower keeps boosting closer to straight runs; higher allows boost through wider turns.',*AI_KNOBS['ai-boost-alignment'],1.,'degrees',visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-aim-tuning-enabled','bool','Maneuvers disrupt enemy aim','Tune enemy projectile gun precision and let measured acceleration disrupt shots aimed at you in Sultan space flight. S-turns and a barrel roll that changes your flight path count; spinning during steady drift does not. Beams use separate controls.',True,visible=SULTAN_ONLY+(('ai-pilots',('match','readable')),),new=True),
    Control('ai-aim-precision','float','Base aim precision','100% keeps the native gun spread; 50% doubles it. This controls shot spread rather than a fixed percentage of hits. Applies to projectile guns aimed at your ship.',*AIM_KNOBS['ai-aim-precision'],.05,visible=SULTAN_ONLY+(('ai-pilots',('match','readable')),('ai-aim-tuning-enabled',True)),new=True),
    Control('ai-evasion-loss','float','Maneuver accuracy loss','Extra gun spread at full disruption. 2x adds twice the base spread, for three times total spread. Measured acceleration on all three axes contributes; stronger combined maneuvers disrupt more.',*AIM_KNOBS['ai-evasion-loss'],.25,'multiplier',visible=SULTAN_ONLY+(('ai-pilots',('match','readable')),('ai-aim-tuning-enabled',True)),new=True),
    Control('ai-evasion-acceleration','float','Acceleration for full disruption','Measured acceleration that reaches maximum aim disruption. Lower makes gentle evasive maneuvers more effective. Actual velocity changes count, so held thrust at the speed cap does not keep disrupting aim.',*AIM_KNOBS['ai-evasion-acceleration'],1.,'acceleration',visible=SULTAN_ONLY+(('ai-pilots',('match','readable')),('ai-aim-tuning-enabled',True)),new=True),
    Control('ai-aim-recovery','float','Aim recovery time','Seconds for about two thirds of the extra spread to recover after your acceleration stops. About three times this interval removes most disruption.',*AIM_KNOBS['ai-aim-recovery'],.05,'seconds',visible=SULTAN_ONLY+(('ai-pilots',('match','readable')),('ai-aim-tuning-enabled',True)),new=True),
    Control('ai-low-health-beams','bool','Low-health beams','Allow space pilots to switch to beams when their own hull health falls below the threshold. Off prevents new low-health beam requests whenever readable tuning is active; an already active beam may finish its native one-second tail.',True,visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-beam-switch-health','float','Beam switch health','Enemy hull health at or below which low-health beams may be requested. Native is 50%. Pilot readability blends this threshold; the switch above turns the behavior off outright.',*COMBAT_KNOBS['ai-beam-switch-health'],.05,visible=(('ai-pilots',('match','readable')),('ai-low-health-beams',True)),new=True),
    Control('ai-shield-recharge','bool','NPC shield recharge','Allow the five ordinary NPC shield profiles to recharge, including when depleted. Shared by enemies, squadron allies, traders, some planetary ships and swarm drones. Freighter and generator shields are unchanged. Off prevents recharge while readable tuning is active.',True,visible=(('ai-pilots',('match','readable')),),new=True),
    Control('ai-shield-delay-scale','float','NPC shield recharge delay','Multiplier on each shield profile delay after a hit. Higher gives you more time before shields restart. 1x keeps native delays: 3 to 12 seconds. These profiles are also used by some allied and planetary NPCs.',*COMBAT_KNOBS['ai-shield-delay-scale'],.25,'multiplier',visible=(('ai-pilots',('match','readable')),('ai-shield-recharge',True)),new=True),
    Control('ai-shield-refill-scale','float','NPC shield refill time','Multiplier on native shield refill duration. Higher refills more slowly; 1x keeps native durations of 1 to 10 seconds. Native per-frame rounding can stop very slow refill on low-health profiles. Shared NPC profiles also affect allies.',*COMBAT_KNOBS['ai-shield-refill-scale'],.25,'multiplier',visible=(('ai-pilots',('match','readable')),('ai-shield-recharge',True)),new=True),
    Control('hud-speed-readout','bool','Total speed readout','Show total speed beside the reticle, with forward speed, lever target and sideways drift. The cockpit number shows forward speed only.',True,visible=SULTAN_ONLY,new=True),
    Control('hud-velocity-marker','bool','Velocity marker','Show where the ship is actually moving (prograde), where it is moving away from (retrograde), and an edge arrow when neither is on screen.',True,visible=SULTAN_ONLY,new=True),
    Control('g-effects-enabled','bool','G-force effects','Sustained G drains your vision to monochrome, darkens the edges, then fades you to black, as with G-safe off in Star Citizen. Short spikes are fine; pulse hard manoeuvres. Sultan flight, flat first-person cockpit.',True,visible=SULTAN_ONLY,new=True),
    Control('g-blackout-controls','bool','Blackout takes control','You keep full control until you black out; then your stick inputs drop out and the ship coasts until you recover.',True,visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-tolerance-up','float','Tolerance: up (+G)','Sustained G you can hold accelerating up through the canopy, as when pulling a hard loop. Beyond it strain builds towards blackout.',9.,1.,40.,.5,'g',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-tolerance-down','float','Tolerance: down (-G)','Sustained G accelerating down, as when pushing the nose over hard (redout side). Usually the lowest.',4.,1.,40.,.5,'g',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-tolerance-lateral','float','Tolerance: sideways','Sustained G strafing or skidding sideways.',6.,1.,40.,.5,'g',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-tolerance-forward','float','Tolerance: forward/back','Sustained G thrusting or braking along the nose. Usually the highest.',15.,1.,60.,.5,'g',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-onset','float','Strain onset','Seconds at twice your tolerance from clear vision to blackout. Higher overload gets there faster.',4.,.5,20.,.25,'seconds',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-recovery','float','Recovery','Seconds to recover fully once under tolerance, after a half-second delay.',4.,.5,20.,.25,'seconds',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('g-unit-scale','float','Metres per game unit','Converts game acceleration to G. Unknown for NMS; 1 assumes a unit is a metre. Raise it to make every manoeuvre count as more G.',1.,.1,5.,.05,'multiplier',visible=SULTAN_ONLY+(('g-effects-enabled',True),),new=True),
    Control('hud-g-meter','bool','G meter','Show G below the reticle: total G, a ring sized to your tolerance with a dot where the force pushes your body, a fore/aft bar and strain while it builds. Uses the G-FORCE tolerances.',True,visible=SULTAN_ONLY,new=True),
    Control('hud-target-relative','bool','Target relative','With a target locked: range, closing speed (positive while closing) and relative speed under the speed readout, and a violet diamond where you drift relative to the target.',True,visible=SULTAN_ONLY,new=True),
    Control('hud-boost-gauge','bool','Boost heat gauge','Show Sultan boost heat beside the reticle: the hot zone where boost is strongest, the relight mark and overheat. Only for ship classes with Sultan boost enabled.',True,visible=SULTAN_ONLY,new=True),
    Control('hud-planetary','bool','Planetary layer',"Near a planet: altitude, vertical speed and atmosphere right of the reticle. Fades in on approach; also shown when the game takes over at low height. The game's own pitch ladder stays.",True,visible=SULTAN_ONLY,new=True),
    Control('hud-altitude','bool','Altitude',"Height above the planet's sea level, in game units. Not height above the ground: hills and valleys are not included.",True,visible=SULTAN_ONLY+(('hud-planetary',True),),new=True),
    Control('hud-vertical-speed','bool','Vertical speed','Climb (+) or sink (-) rate away from the planet centre, with an up or down arrow.',True,visible=SULTAN_ONLY+(('hud-planetary',True),),new=True),
    Control('hud-atmosphere','bool','Atmosphere','ATM on a planet with air, VAC on an airless one.',True,visible=SULTAN_ONLY+(('hud-planetary',True),),new=True),
    Control('hud-trim','bool','Lift trim','The lift trim you have set, as TRIM +62% under ATM/VAC. Shown only while a trim is set.',True,visible=SULTAN_ONLY+(('hud-planetary',True),),new=True),
    Control('hud-landing','bool','Landing guide',"With the gear down near the ground (Manual landing): a level bubble with your drift over the ground, a ground-height tape with the touchdown band, your sink rate and what the landing is waiting for.",True,visible=SULTAN_ONLY,new=True),
    Control('hud-mode-tag','bool','Flight mode tag','Who flies the ship, above the speed readout: SULTAN, or NATIVE with the reason (low height, landing) when the game takes over. Shown near planets and whenever the game flies.',True,visible=SULTAN_ONLY,new=True),
    Control('hud-scale','float','HUD size','Size of the speed readout and velocity markers.',1.,.5,2.,.05,'multiplier',visible=SULTAN_ONLY,new=True),
    Control('hud-font','choice','HUD font','Game font used for the speed readout. Compare shows a sample line in each game font.','GAMEFONT',choices=(('GAMEFONT','Game font 1'),('GAMEFONT2','Game font 2'),('GAMEFONT3','Game font 3'),('GAMEFONT4','Game font 4'),('compare','Compare all')),visible=SULTAN_ONLY+(('hud-speed-readout',True),),new=True),
    *[c for channel,title in AXIS_CHANNELS for c in _axis_controls(channel,title)],
    Control('throttle-style','choice','Throttle style','HOTAS: a lever that stays where you leave it. HOSAS: a self-centring stick.','hosas',choices=(('hotas','HOTAS'),('hosas','HOSAS')),new=True),
    Control('bind-throttle-axis','axis-binding','Binding','Physical axis that drives throttle. Select to rebind, then move the axis.'),
    Control('throttle-inverted','bool','Invert','Reverse the throttle axis direction.',False),
    Control('throttle-idle-plateau','float','Idle zone','Lever travel at the bottom that stays at idle.',.05,0.,.3,visible=NATIVE_HOTAS),
    Control('throttle-saturation','float','Full-thrust saturation','Share of travel above idle that reaches full thrust.',1.,.05,1.,visible=NATIVE_HOTAS),
    Control('cruise-detent-enabled','bool','Cruise detent','Hold a steady speed while the lever sits in its detent.',False,visible=NATIVE_HOTAS,new=True),
    Control('throttle-detent-center','float','Detent position','Lever position of the cruise detent.',.6,.1,.95,visible=NATIVE_HOTAS+(('cruise-detent-enabled',True),)),
    Control('throttle-detent-width','float','Detent width','Travel either side of the detent that holds cruise speed.',.02,.005,.1,.005,visible=NATIVE_HOTAS+(('cruise-detent-enabled',True),)),
    Control('hosas-stick-mode','choice','Stick behaviour','Direct: centre is stop, push for thrust, pull to brake. Rate: push to change speed; it holds when you let go.','direct',choices=(('direct','Direct'),('rate','Rate')),visible=NATIVE_HOSAS,new=True),
    Control('throttle-deadzone','float','Centre deadzone','Stick travel around centre that does nothing.',.05,0.,.5,visible=HOSAS_ONLY),
    Control('hosas-saturation','float','Saturation','Deflection that reaches full signed thrust under Sultan; full thrust or brake in native Direct mode.',.95,.05,1.,visible=HOSAS_ONLY,new=True),
    Control('hosas-brake-authority','float','Brake strength','Brake demand at full back deflection. Until the brake lane is qualified, pulling back holds idle thrust.',1.,0.,1.,visible=NATIVE_HOSAS+(('hosas-stick-mode','direct'),),new=True),
    Control('rate-throttle-ramp','float','Ramp rate','Speed setting change per second at full deflection.',1.,.1,3.,.05,'per-second',visible=NATIVE_HOSAS+(('hosas-stick-mode','rate'),)),
    Control('rate-throttle-decay','float','Return to idle','Speed setting lost per second while centred. 0 holds speed.',0.,0.,1.,.05,'per-second',visible=NATIVE_HOSAS+(('hosas-stick-mode','rate'),)),
    Control('lever-controls','choice','Lever controls','Speed holds a forward speed along the nose, without braking lateral drift. Thrust commands acceleration. Coast disables either controller.','speed',choices=(('speed','Speed'),('thrust','Thrust')),visible=HOTAS_ONLY+SULTAN_ONLY,new=True),
    Control('lever-reverse-mode','choice','Reverse zone','Full gives full retro demand throughout the zone. Proportional increases smoothly towards the bottom.','full',choices=(('full','Full'),('proportional','Proportional')),visible=HOTAS_ONLY+SULTAN_ONLY,new=True),
    *[Control('lever-'+key,'float',label,'Lever position of this zone boundary. Boundaries stay ordered.',value,.005 if key=='reverse-end' else 0.,.995 if key=='boost-start' else 1.,.005,visible=HOTAS_ONLY+SULTAN_ONLY,new=True) for key,label,value in (
        ('reverse-end','Reverse ends',.10),('parking-end','Parking ends',.18),('coast-end','Coast ends',.22),
        ('cruise-start','Cruise starts',.52),('cruise-end','Cruise ends',.58),('full-start','Full starts',.85),('boost-start','Boost starts',.90))],
    Control('lever-cruise-enabled','bool','Cruise zone','Hold the cruise level in the cruise zone. Off joins this range to the forward ramp.',True,visible=HOTAS_ONLY+SULTAN_ONLY,new=True),
    Control('lever-cruise-level','float','Cruise level','Forward demand while in the cruise zone, as a fraction of the speed cap or full thrust.',.5,0.,1.,.01,visible=HOTAS_ONLY+SULTAN_ONLY+(('lever-cruise-enabled',True),),new=True),
    Control('key-output-enabled','bool','Keyboard & mouse output','Send bound keyboard keys and mouse buttons to NMS (D20). First live test: off until you turn it on. Keys go out only while NMS has focus and are always released.',False,new=True),
    Control('cursor-stick-enabled','bool','Stick moves cursor','While a dialog or menu is open, pitch and yaw move the mouse cursor; bind left click (Fire) to select. Needs Keyboard & mouse output. Never active in flight.',True,new=True),
    Control('cursor-speed','float','Cursor speed','Cursor speed at full stick deflection. Small deflections move much slower, for fine aiming.',1400.,200.,4000.,50.,'pixels',visible=(('cursor-stick-enabled',True),),new=True),
    Control('cursor-invert-y','bool','Invert cursor vertical','Reverse the cursor direction for pitch, without changing flight.',False,visible=(('cursor-stick-enabled',True),),new=True),
    Control('eject-hold-time','float','Eject hold time','How long the Eject button must be held before you leave the ship. The HUD counts down while you hold it.',3.,1.,10.,.5,'seconds',new=True),
    Control('lever-boost-enabled','bool','Boost zone','Request Sultan boost above the boost boundary. Enable boost for the flown class in Flight Tuning; boost adds acceleration while leaving the speed cap unchanged.',False,visible=HOTAS_ONLY+SULTAN_ONLY,new=True),
)

# Head tracking (OpenTrack FreeTrack -> cockpit camera, mod/head_tracking.py). Smoothing runs per
# rendered frame in the camera hook (live 2026-10-16: the unsmoothed path juddered).
HEAD_CONTROLS=(
    Control('head-tracking-enabled','bool','Head tracking','Turn the cockpit camera with your head through OpenTrack (FreeTrack 2.0 Enhanced output), for example with a Tobii tracker. First-person cockpit only; the camera is native in third person, photo mode, the galaxy map and menus.',False,new=True),
    *[c for axis,limit in (('yaw',90),('pitch',45),('roll',20)) for c in (
        Control(f'head-{axis}-scale','float',f'{axis.title()} scale',f'Camera {axis} per degree of head {axis}. 0 turns this axis off. The camera stops at {limit} degrees either way.',1.,0.,3.,.05,'multiplier',new=True),
        Control(f'head-{axis}-inverted','bool',f'Invert {axis}',f'Reverse the camera {axis} direction.',False,new=True))],
    Control('head-deadzone','float','Deadzone','Head movement around centre, in degrees on each axis, that leaves the camera still.',0.,0.,10.,.5,'degrees',new=True),
    Control('head-smoothing','float','Smoothing','Smooths the camera every frame. Strong when your head is nearly still (removes jitter), light when you turn quickly (keeps it responsive). 0 turns it off.',.5,0.,1.,.05,new=True),
)
CONTROLS+=HEAD_CONTROLS

# T88 planet settings shared by every class (Flight Tuning > PLANETS). Density knots: docs/PLANETARY-FLIGHT.md 5.1a.
AIR_CONTROLS=(
    Control('air-gravity','float','Planet gravity','Pull toward the planet near its surface. It weakens with distance as the game gravity does. 0 turns gravity off.',20.,0.,60.,1.,'acceleration',visible=AIR_ONLY,new=True),
    Control('air-gravity-compensation','choice','Gravity compensation','While thrusting: the engines hold altitude while you thrust or strafe, paid from thrust; with the lever in coast you glide on the wings, or drop in a ship without them. Always: the engines hold altitude even when coasting. Off (default): gravity acts in full; hold altitude with thrust or the lift trim buttons (Buttons page). The parking brake always holds position.','off',choices=(('thrusting','While thrusting'),('always','Always'),('off','Off')),visible=AIR_ONLY,new=True),
    Control('air-thick-top','float','Thick air up to','Altitude above sea level where the air starts to thin. Below it, drag and engine loss are at full strength.',1000.,0.,5000.,100.,'distance',visible=AIR_ONLY,new=True),
    Control('air-auto-level','bool','Auto-level near planets',"On: the game levels the ship, as in native planet flight: it rolls upright when the roll stick is centred, and over water near the surface it stops bank and pitch at 45 degrees. Off: Sultan holds your bank, so you can bank, centre the stick and pull through the turn, at any angle.",False,visible=AIR_ONLY,new=True),
    Control('air-top','float','Atmosphere top','Altitude above sea level where the air ends. Drag and engine loss fade smoothly to nothing between the thick air and here. Planet gravity ends here too, easing out over the last 2000, on planets with or without air: think of it as reaching orbit.',12000.,2000.,30000.,500.,'distance',visible=AIR_ONLY,new=True),
)
CONTROLS+=AIR_CONTROLS

# T101 manual landing (Atmosphere Flight page, docs/MANUAL-LANDING.md). Stage 1: the gear and hover mode.
LANDING_CONTROLS=(
    Control('landing-manual','bool','Manual landing',"On: the Landing gear button puts the ship in hover mode, so you can fly it down to the ground yourself: low speed cap, gravity held, no boost, and the game no longer pushes you up off the ground. Gear up flies as before. Off: the game keeps you off the ground and lands for you, as before.",True,visible=AIR_ONLY,new=True),
    Control('landing-touchdown-height','float','Touchdown height',"With the gear down, once the ship is this close to the ground, nearly stopped, roughly level and not climbing for half a second, the mod presses the game's Land for you and the game finishes the landing. No push into the ground is needed: ease down onto it. It is the game's height of the ship above the ground, so a landed ship reads its gear height, not 0 (about 2 on a Fighter).",5.,1.,30.,.5,'distance',visible=AIR_ONLY+(('landing-manual',True),),new=True),
    Control('landing-vertical-rate','float','Hover climb and sink rate',"With the gear down the vertical stick sets how fast you climb or sink, and centred holds your height. Full stick is this rate; close to the ground the sink is eased down to 1.5 u/s so the ship settles onto its gear.",10.,2.,40.,1.,'speed',visible=AIR_ONLY+(('landing-manual',True),),new=True),
    Control('landing-hover-cap','float','Hover speed cap','Top speed with the gear down. Lowering the gear faster than this slows you down to it over about two seconds.',40.,10.,120.,5.,'speed',visible=AIR_ONLY+(('landing-manual',True),),new=True),
)
CONTROLS+=LANDING_CONTROLS

# Ship Damage page (user 2026-10-19: damage has its own page). T93 ditch at sea; T94 crash damage joins here.
DAMAGE_CONTROLS=(
    Control('air-ditch','bool','Ditch at sea',"On: the sea is a real surface. The game's push that keeps ships off the water is off over the sea, so a gentle touch floats and bobs, and hitting the water too fast or too steeply wipes you out: you black out, the thrusters flood and the ship sinks while the hull fails under the pressure. Relight them in the emergency purge before the hull gives out: purge mains, retro, up, down, starboard and port by holding each one's control inside the HUD band, then hold boost to blast out of the water. A miss costs hull and time; staying shallow saves the hull. Off: the game keeps you above the water, as before.",True,visible=AIR_ONLY,new=True),
    Control('air-ditch-speed','float','Ditch speed','Touching the water faster than this, in any direction, wipes you out.',120.,20.,600.,10.,'speed',visible=AIR_ONLY+(('air-ditch',True),),new=True),
    Control('air-ditch-sink-rate','float','Ditch sink rate','Touching the water while descending faster than this wipes you out, even below the ditch speed.',30.,5.,200.,5.,'speed',visible=AIR_ONLY+(('air-ditch',True),),new=True),
    Control('air-sink-speed','float','Sink speed','How fast a ditched ship sinks toward the sea floor.',10.,2.,40.,1.,'speed',visible=AIR_ONLY+(('air-ditch',True),),new=True),
    Control('air-purge-difficulty','choice','Thruster purge','How wide the band is in the emergency purge after a ditch. Hold the control of each thruster group (throttle forward and reverse, strafe up, down, right, left) inside its band to advance; leave it and the hull takes a hit and the purge takes longer. The last stage is boost, which relights the thrusters and blasts you out of the water.','normal',choices=(('easy','Easy (wide band)'),('normal','Normal'),('hard','Hard (narrow band)')),visible=AIR_ONLY+(('air-ditch',True),),new=True),
    Control('crash-damage','bool','Crash damage','On: terrain and planet-object hits scale with impact speed into the surface instead of dealing flat damage. Above the safe speed, hits go straight to the hull, the launch-thruster break chance rises with speed, and a crash never kills: a disabling hit blacks you out, ejects you and leaves the ship essentials damaged until repaired. Off: native collision damage.',True,visible=SULTAN_ONLY,new=True),
    Control('crash-safe-speed','float','Safe impact speed','Below this impact speed into the surface, scrapes stay native and shields absorb them. Above it, the hit goes straight to the hull.',25.,5.,150.,5.,'speed',visible=SULTAN_ONLY+(('crash-damage',True),),new=True),
    Control('crash-damage-per-us','float','Hull damage per u/s','Extra damage added to the native hit (10 terrain, 30 object) for each u/s above the safe impact speed, before the game difficulty multiplier.',4.,.5,20.,.5,'number',visible=SULTAN_ONLY+(('crash-damage',True),),new=True),
    Control('crash-glancing','float','Glancing speed share','How much sideways sliding speed along the surface counts toward the impact speed, on top of speed straight into the surface.',.10,0.,.50,.05,'percent',visible=SULTAN_ONLY+(('crash-damage',True),),new=True),
    Control('crash-break-speed','float','Launcher break speed','Impact speed where the chance to break the launch thrusters reaches 100% (rises from the native 10% at the safe speed).',60.,20.,300.,5.,'speed',visible=SULTAN_ONLY+(('crash-damage',True),),new=True),
    Control('crash-disable-speed','float','Disabling speed','Impact speed that triggers a disabling crash: blackout, eject and damaged essentials (launch thruster, pulse engine, shield). Any hit that would empty the hull is also disabling.',60.,20.,300.,5.,'speed',visible=SULTAN_ONLY+(('crash-damage',True),),new=True),
    Control('alarm-sounds','choice','Alarm sounds',"What you hear when the ship is in trouble (a ditch at sea, a crash): the ship computer voice, the critical-damage siren, thruster ignition and the engines dying and relighting. In a ditch the computer talks you through the thruster purge, counting each stage in (3, 2, 1); in a crash it uses the game's own lines. Alarms only keeps the sounds without the voice.",'voice',choices=(('voice','Voice and alarms'),('alarms','Alarms only'),('off','Off')),visible=SULTAN_ONLY,new=True),
    Control('alarm-voice-volume','float','Voice volume','Loudness of the ship computer lines in a ditch (the purge countdown). They play outside the game mix, so the game volume sliders do not reach them.',.7,0.,1.,.05,visible=SULTAN_ONLY+(('alarm-sounds','voice'),),new=True),
    Control('alarm-lights','bool','Red alarm lights','The cockpit alarm lamps pulse red while the ship is in trouble, faster as the hull fails, slowly once it is wrecked. Off: the game keeps its own alarm lights.',True,visible=SULTAN_ONLY,new=True),
)
CONTROLS+=DAMAGE_CONTROLS

# Active-class tuning rows are views into profiles, never a second persisted source of truth.
PROFILE_CONTROLS=(
    Control('tuning-acceleration-scale','float','Acceleration scale','Multiplier on the ship effective native ThrustForce, including its class-tier (C/B/A/S) bonus.',1.,.05,3.,.05,'multiplier',new=True),
    *[Control('tuning-'+key,'float',label,'Directional authority as a fraction of forward native authority.',value,.05,2.,.05,'multiplier',new=True) for key,label,value in (
        ('forward-ratio','Forward authority',1.),('retro-ratio','Retro authority',.6),('lateral-ratio','Lateral authority',.4),('up-ratio','Up authority',.5),('down-ratio','Down authority',.4))],
    Control('tuning-speed-cap','float','Speed cap','Total speed cap. Outward thrust is removed at the cap. Optional acceleration fade eases the approach, and overspeed bleed returns excess speed to this cap.',1200.,10.,2000.,10.,'speed',new=True),
    Control('tuning-speed-cap-multiplier','float','Class speed multiplier','Class multiplier on the speed cap.',1.,.1,2.,.05,'multiplier',new=True),
    Control('tuning-speed-hold-gain','float','Lever speed response','Forward acceleration per unit of speed error in lever Speed mode.',1.,.1,10.,.1,'per-second',new=True),
    Control('tuning-accel-fade-exponent','float','Acceleration fade','Ease outward acceleration as total speed approaches the cap. 0 turns this off; 2 fades gradually, while 4 or more keeps thrust strong until nearer the cap. Steering thrust is unchanged.',0.,0.,10.,.25,'number',new=True),
    Control('tuning-brake-floor','float','Brake floor','Minimum share of inward thrust at high speed. 100% keeps braking unchanged; lower values ease braking above one quarter of the cap. Parking brake is unchanged.',1.,0.,1.,.05,new=True),
    Control('tuning-momentum','float','Momentum','How much slowing down and turning inherit the cost of building your speed. At 100%, braking mirrors the acceleration fade (so a flip-and-burn reversal takes twice the build-up) and sideways thrust is capped at the average acceleration it took to reach your speed (no stacking strafe and nose-across burns at speed). Low speed keeps full agility. 0 turns it off.',0.,0.,1.,.05,new=True),
    Control('tuning-overspeed-bleed-time','float','Overspeed bleed time','Automatically ease speed above the cap back towards it, while steering and retro remain available. 0 turns this off; after about three times this interval most excess speed is gone.',0.,0.,30.,.25,'seconds',new=True),
    Control('tuning-boost-enabled','bool','Sultan boost','Enable the bound Sultan boost button and the optional lever boost zone for this class. Boost multiplies acceleration, keeps the same speed cap, and locks out when overheated.',False,new=True),
    Control('tuning-boost-source','choice','Boost multiplier source','Use the ship effective native boost-to-thrust ratio, including upgrades, or a fixed multiplier. Only Sultan acceleration is boosted.','native',choices=(('native','Native class ratio'),('fixed','Fixed override')),visible=(('tuning-boost-enabled',True),),new=True),
    Control('tuning-boost-multiplier','float','Boost multiplier','Fixed acceleration multiplier while Sultan boost is active.',2.,1.,10.,.1,'multiplier',visible=(('tuning-boost-enabled',True),('tuning-boost-source','fixed')),new=True),
    Control('tuning-boost-heat-time','float','Time to overheat','Seconds of continuous Sultan boost from cold to overheat. Overheating locks out boost until the relight level is reached.',4.,.1,30.,.1,'seconds',visible=(('tuning-boost-enabled',True),),new=True),
    Control('tuning-boost-cool-time','float','Cooling time','Seconds to cool from full heat to cold after a short delay without boost.',6.,.1,60.,.1,'seconds',visible=(('tuning-boost-enabled',True),),new=True),
    Control('tuning-boost-cold-strength','float','Cold boost strength','Share of the boost multiplier you get from cold. Boost grows stronger as it heats, reaching full strength at the hot zone. 100% is flat boost at any heat.',.4,0.,1.,.05,visible=(('tuning-boost-enabled',True),),new=True),
    Control('tuning-boost-hot-zone','float','Hot zone starts','Heat where boost reaches full strength. Keep heat between here and overheat for the strongest boost.',.7,.1,.95,.05,visible=(('tuning-boost-enabled',True),),new=True),
    Control('tuning-boost-relight-level','float','Boost relight heat','Heat level that releases the overheat lockout. A held boost button can start boosting again once heat falls to this level.',.3,0.,.95,.05,visible=(('tuning-boost-enabled',True),),new=True),
    Control('tuning-brake-strength','float','Brake strength','Fraction of directional authority available to the parking brake.',1.,.05,1.,.05,new=True),
    Control('tuning-settle-time','float','Settle time','Parking brake response time near zero speed.',1.,.1,5.,.1,'seconds',new=True),
    Control('tuning-hold-threshold','float','Hold threshold','Near-zero speed used by the parking brake hold state.',.1,.001,5.,.01,'speed',new=True),
    Control('tuning-jerk-limit','float','Jerk limit','Maximum acceleration change per second. Zero disables smoothing.',0.,0.,5000.,25.,'jerk',new=True),
    Control('tuning-additive-pitch-yaw','bool','Additive pitch/yaw','Give Fighter pitch and yaw their full independent authority together. Centre both axes after enabling. Other ship classes keep native handling. Native torque limits and Auto-bank on yaw still apply.',False,new=True),
    *[Control('tuning-'+axis+'-rate','float',axis.title()+' rate',f'Multiplier on this class top {axis} rate. 1.0 is native. Centre the stick after changing. Native torque limits still apply; Corvette stays native.',1.,.5,2.,.05,'multiplier',new=True) for axis in ('pitch','yaw','roll')],
    Control('tuning-turn-response','float','Turn response','How quickly pitch, yaw and roll reach their rate and settle: a multiplier on the class engine TurnStrength and RollForce. 1.0 is native. Native torque limits still apply.',1.,.5,2.,.05,'multiplier',new=True),
    Control('tuning-auto-bank-on-yaw','bool','Auto-bank on yaw','Allow the native RudderToRoll assist to bank the ship during yaw.',False,new=True),
    # T88 air style per class (planetary_flight.AirStyle).
    Control('tuning-air-terminal-speed','float','Terminal speed in thick air','Top speed at full forward thrust in thick air. Thinner air higher up lets the ship go faster.',400.,50.,1500.,10.,'speed',visible=AIR_ONLY,new=True),
    Control('tuning-air-engine-loss','float','Engine loss in thick air','Share of thrust the engines lose in thick air. It comes back as the air thins.',.2,0.,.8,.05,visible=AIR_ONLY,new=True),
    Control('tuning-air-lift','float','Lift','How quickly the air turns your velocity toward the nose in thick air, so bank-and-pull turns carve like a jet. Fades below 150 u/s and as you slide sideways. 0 turns it off.',0.,0.,4.,.1,'per-second',visible=AIR_ONLY,new=True),
    Control('tuning-air-side-drag','float','Side drag','Drag on sideways and vertical motion, as a multiple of nose-on drag. Above 1 the hull slices forward and resists sliding.',1.,.5,8.,.25,'multiplier',visible=AIR_ONLY,new=True),
    Control('tuning-air-nose-authority','float','Nose authority at speed','Share of the pitch and yaw rate left at terminal speed in thick air. It falls as air pressure builds with speed, so a blocky hull is hard to point fast and turns freely when slow or high up. 1.0 is off. Roll is unchanged.',1.,.25,1.,.05,'multiplier',visible=AIR_ONLY,new=True),
    Control('tuning-air-induced-drag','float','Turn drag','Speed the air takes for lift: a plain roll is free, a hard pull in the bank bleeds speed, and the harder you pull the more you lose. 0 turns it off.',0.,0.,2.,.05,'multiplier',visible=AIR_ONLY,new=True),
    Control('tuning-air-slip-authority','float','Nose authority in a slide','Share of pitch and yaw rate left when strafing or yawing puts the nose well off the flight path at speed. Thrust forward along the nose to get it back. 100% turns it off.',1.,.25,1.,.05,visible=AIR_ONLY,new=True),
    Control('tuning-air-damping','float','Air damping','How much of the air drag already acts at low speed. Higher feels held by the air from the first u/s; lower feels free until near top speed. Top speed stays at the terminal speed either way.',0.,0.,1.,.05,visible=AIR_ONLY,new=True),
    Control('tuning-air-boost','float','Boost in thick air','Share of the boost you keep in thick air. It comes back as the air thins, so boost pays off most up high.',1.,0.,1.,.05,visible=AIR_ONLY,new=True),
    Control('tuning-air-buffet','float','Buffet','How hard the nose shakes when you hold it well off the flight path in fast, thick air, so a bad angle is work to hold. 0 turns it off.',0.,0.,1.,.05,'per-second',visible=AIR_ONLY,new=True),
    Control('tuning-air-overspeed-penalty','float','Overspeed penalty','Above the terminal speed (boosting or diving), how much harder the air makes turning: stiffer nose, stronger pull and buffet, more speed bled in a pull. 0 turns it off.',0.,0.,3.,.1,'multiplier',visible=AIR_ONLY,new=True),
    Control('tuning-air-wing-support','float','Wing support','How much of the ship’s weight the wings carry when you coast: full at half the terminal speed and above, less as you slow. Lets you glide and trade height for speed. 0: no wings, the ship drops without thrust.',0.,0.,2.,.05,'multiplier',visible=AIR_ONLY,new=True),
    Control('tuning-air-glide-ratio','float','Glide ratio','With the power off, how far the ship glides for each unit of height it loses (4 = 4:1). The air drag along the nose eases as you cut forward thrust and comes back as you add it, so full thrust still tops out at the terminal speed. Needs Wing support. 0 turns gliding off: full drag at any power.',0.,0.,10.,.25,'number',visible=AIR_ONLY,new=True),
    Control('tuning-air-weathervane','float','Weathervane','How hard the air swings the nose back onto the flight path in a slide, as a sway. 0 turns it off.',0.,0.,5.,.1,'per-second',visible=AIR_ONLY,new=True),
    Control('tuning-air-sway-damping','float','Sway damping','How quickly the weathervane sway settles. Low values wallow back and forth; high values snap straight.',.5,.05,1.,.05,visible=AIR_ONLY,new=True),
    Control('tuning-air-omni','float','All-direction aero','How much of the lift and wing support also works when you slide sideways or backward. At 100% a strafe keeps its slide and still lifts and glides; at 0% only flight along the nose does.',0.,0.,1.,.05,visible=AIR_ONLY,new=True),
    Control('tuning-air-mav-lift','float','MAV lift',"Lift of the manoeuvring thrusters in thick air, in ship weights, whatever the engine tier: 1.25 only just lifts the ship, so you get up, point the nose and fire the mains. Strafe and down thrust scale with it. The ship's own MAVs return as the air thins. 0 turns it off.",0.,0.,3.,.05,'multiplier',visible=AIR_ONLY,new=True),
)
CONTROLS+=PROFILE_CONTROLS
PROFILE_KEYS={c.id:c.id[len('tuning-'):].replace('-','_') for c in PROFILE_CONTROLS}
ROTATION_STYLE_KEYS=('pitch_rate','yaw_rate','roll_rate','turn_response')
BY_ID={c.id:c for c in CONTROLS}
# (lower, upper): lower must stay MIN_GAP below upper, as the U3 boundaries do.
ORDERED_PAIRS=tuple((f'{ch}-deadzone',f'{ch}-saturation') for ch,_ in AXIS_CHANNELS)+(
    ('throttle-deadzone','hosas-saturation'),('throttle-idle-plateau','throttle-detent-center'))
LEVER_BOUNDARIES=('reverse-end','parking-end','coast-end','cruise-start','cruise-end','full-start','boost-start')
ZONE_PAIRS=tuple(('lever-'+a,'lever-'+b) for a,b in zip(LEVER_BOUNDARIES,LEVER_BOUNDARIES[1:]))


# Sultan baselines (T89, restyled T90; docs/SHIP-CLASS-BASELINES.md). Each class is a flight style
# you feel at once, not a few percent: Fighter flies forward, rolls and pulls (little strafe, weak
# yaw); the specials fly in more dimensions (strafe and vertical at speed, flat yaw turns). Rotation
# rates multiply native top rates; turn response multiplies TurnStrength/RollForce (how fast the rate
# arrives). Native engine stats still set absolute strength: Fighter/Explorer/Exotic/Solar use the
# Light block (ThrustForce 80, TurnStrength 1.1), the rest start at 40 and 0.6/1.0, and the C/B/A/S
# roll adds to both. Corvette values are provisional: it flies native until T65 qualifies it.
_SHARED=dict(forward_ratio=.85,speed_cap=1100.,boost_enabled=True,boost_source='native',auto_bank_on_yaw=False)
SULTAN_BASELINES={
    # Fighter: point, roll, pull. Strongest forward punch, strafe only for fine aim, momentum keeps it
    # from sliding at speed. Fast roll and pitch, deliberately weak yaw.
    2:dict(acceleration_scale=1.,retro_ratio=.4,lateral_ratio=.2,up_ratio=.45,down_ratio=.25,speed_cap_multiplier=.9,
           accel_fade_exponent=3.25,brake_floor=1.,momentum=1.,overspeed_bleed_time=2.,jerk_limit=250.,settle_time=1.,
           boost_heat_time=6.,boost_cool_time=7.,boost_cold_strength=.5,
           pitch_rate=1.15,yaw_rate=.6,roll_rate=1.5,turn_response=1.,
           # Air (T88): powerful mains that keep most thrust, low nose-on drag, a hull that carves.
           # Flight 2026-10-19: air felt punchy and boost too strong; more engine loss, linear damping, half boost.
           air_terminal_speed=450.,air_engine_loss=.35,air_lift=1.2,air_side_drag=3.,air_damping=.3,air_boost=.5,
           # Sideslip (user design 2026-10-19): hard pulls bleed speed; a slide costs half the nose and
           # the air snaps it back onto the flight path.
           air_induced_drag=.3,air_slip_authority=.5,air_weathervane=2.,air_sway_damping=.6,air_buffet=.25,
           air_overspeed_penalty=1.5,air_wing_support=1.,air_glide_ratio=4.),  # glides about 4:1 power off
    # Exotic: drifter. Strafe as strong as forward and kept at speed; flat yaw turns, lazy roll.
    6:dict(acceleration_scale=.9,retro_ratio=.9,lateral_ratio=1.,up_ratio=.9,down_ratio=.8,speed_cap_multiplier=.85,
           accel_fade_exponent=3.5,brake_floor=1.,momentum=.25,overspeed_bleed_time=1.5,jerk_limit=300.,settle_time=.8,
           boost_heat_time=5.,boost_cool_time=6.,boost_cold_strength=.55,
           pitch_rate=1.,yaw_rate=1.4,roll_rate=.8,turn_response=1.3,
           # Air (T98, GUESS): slides and lifts in any direction. Drag alike on every axis, a slide keeps the
           # nose and the air barely fights it; strafing keeps lifting and gliding.
           air_terminal_speed=420.,air_engine_loss=.25,air_lift=.9,air_side_drag=1.,air_damping=.25,air_boost=.6,
           air_induced_drag=.15,air_slip_authority=.95,air_weathervane=.4,air_sway_damping=.8,
           air_overspeed_penalty=.8,air_wing_support=.9,air_glide_ratio=3.5,air_omni=.8),
    # Living: swimmer. Vertical is its strongest axis (lift and dive), smooth onset, everything flows.
    7:dict(acceleration_scale=1.05,retro_ratio=.8,lateral_ratio=.7,up_ratio=1.2,down_ratio=1.,speed_cap_multiplier=.8,
           accel_fade_exponent=3.,brake_floor=1.,momentum=.5,overspeed_bleed_time=2.,jerk_limit=150.,settle_time=1.,
           boost_heat_time=6.,boost_cool_time=4.5,boost_cold_strength=.6,
           pitch_rate=1.3,yaw_rate=1.15,roll_rate=1.2,turn_response=1.4,
           # Air (T98, GUESS): swims. Held by the air like water, glides long, a slide swings gently back.
           air_terminal_speed=400.,air_engine_loss=.25,air_lift=1.,air_side_drag=1.25,air_damping=.4,air_boost=.6,
           air_induced_drag=.2,air_slip_authority=.9,air_weathervane=.6,air_sway_damping=.5,
           air_overspeed_penalty=1.,air_wing_support=1.1,air_glide_ratio=4.5,air_omni=.6),
    # Solar: skirmisher. Closest special to the Fighter: some strafe at speed, long boost, even turns.
    8:dict(acceleration_scale=.9,retro_ratio=.7,lateral_ratio=.6,up_ratio=.6,down_ratio=.5,speed_cap_multiplier=.85,
           accel_fade_exponent=3.5,brake_floor=1.,momentum=.6,overspeed_bleed_time=2.,jerk_limit=225.,settle_time=1.,
           boost_heat_time=9.,boost_cool_time=8.,boost_cold_strength=.45,
           pitch_rate=1.05,yaw_rate=1.1,roll_rate=1.1,turn_response=1.1,
           # Air (T98, GUESS): the special nearest the Fighter. Carves, slides a little, and the sail glides furthest.
           air_terminal_speed=440.,air_engine_loss=.3,air_lift=1.1,air_side_drag=1.75,air_damping=.3,air_boost=.6,
           air_induced_drag=.25,air_slip_authority=.75,air_weathervane=1.,air_sway_damping=.65,air_buffet=.1,
           air_overspeed_penalty=1.2,air_wing_support=1.,air_glide_ratio=5.,air_omni=.4),
    # Interceptor: gunship. Full six-axis thrust at any speed, nose stays on target while it circles.
    9:dict(acceleration_scale=1.1,retro_ratio=1.1,lateral_ratio=1.1,up_ratio=1.1,down_ratio=1.,speed_cap_multiplier=.8,
           accel_fade_exponent=3.5,brake_floor=1.,momentum=.15,overspeed_bleed_time=1.5,jerk_limit=325.,settle_time=.8,
           boost_heat_time=5.,boost_cool_time=5.5,boost_cold_strength=.5,
           pitch_rate=1.25,yaw_rate=1.35,roll_rate=1.,turn_response=1.9,
           # Air (T98, GUESS): fully all-direction. Strafes and backs at full lift, the nose stays where you
           # put it, lower top speed and a short glide.
           air_terminal_speed=380.,air_engine_loss=.2,air_lift=.7,air_side_drag=1.,air_damping=.4,air_boost=.5,
           air_induced_drag=.1,air_slip_authority=1.,air_weathervane=.2,air_sway_damping=.9,
           air_overspeed_penalty=1.,air_wing_support=.7,air_glide_ratio=2.5,air_omni=1.),
    # Explorer: cruiser. Straight lines at top speed, gentle turns, little strafe.
    3:dict(acceleration_scale=.8,retro_ratio=.45,lateral_ratio=.3,up_ratio=.35,down_ratio=.3,speed_cap_multiplier=1.05,
           accel_fade_exponent=3.,brake_floor=.9,momentum=1.,overspeed_bleed_time=3.,jerk_limit=175.,settle_time=1.2,
           boost_heat_time=10.,boost_cool_time=9.,boost_cold_strength=.4,
           pitch_rate=.8,yaw_rate=.8,roll_rate=.8,turn_response=.9,
           # Air (T98, GUESS): balanced. Fastest in air, near-Fighter lift and glide, slides a little more.
           # Its weak space MAVs could not hold a C-class ship up in thick air, so they lift 1.6 weights at any
           # tier: more than the bricks, less than an upgraded Fighter.
           air_terminal_speed=500.,air_engine_loss=.3,air_lift=1.,air_side_drag=2.,air_damping=.25,air_boost=.5,
           air_induced_drag=.25,air_slip_authority=.7,air_weathervane=1.2,air_sway_damping=.55,air_buffet=.15,
           air_overspeed_penalty=1.2,air_wing_support=1.,air_glide_ratio=4.5,air_omni=.2,air_mav_lift=1.6),
    # Corvette: gunboat. Heavy, but real thrusters on every axis for a fight. Provisional (T65).
    10:dict(acceleration_scale=1.,retro_ratio=.6,lateral_ratio=.6,up_ratio=.7,down_ratio=.5,speed_cap_multiplier=.85,
            accel_fade_exponent=2.5,brake_floor=.8,momentum=.7,overspeed_bleed_time=3.,jerk_limit=125.,settle_time=1.5,
            boost_heat_time=7.,boost_cool_time=9.,boost_cold_strength=.5,
            pitch_rate=.8,yaw_rate=.8,roll_rate=.7,turn_response=1.2,
            # Air (T98, GUESS): balanced and heavier. Ready for when T65 lets Sultan fly it; native until then.
            air_terminal_speed=380.,air_engine_loss=.3,air_lift=.8,air_side_drag=2.,air_damping=.35,air_boost=.5,
            air_nose_authority=.8,air_induced_drag=.25,air_slip_authority=.7,air_weathervane=1.,air_sway_damping=.5,
            air_buffet=.15,air_overspeed_penalty=1.2,air_wing_support=.8,air_glide_ratio=3.,air_omni=.2),
    # Hauler: barge. Weak thrust, lift favoured over strafe, slow to turn and slower to roll.
    1:dict(acceleration_scale=.7,retro_ratio=.6,lateral_ratio=.3,up_ratio=.7,down_ratio=.5,speed_cap_multiplier=.85,
           accel_fade_exponent=2.,brake_floor=.7,momentum=1.,overspeed_bleed_time=4.,jerk_limit=75.,settle_time=1.8,
           boost_heat_time=8.,boost_cool_time=10.,boost_cold_strength=.35,
           pitch_rate=.7,yaw_rate=.7,roll_rate=.6,turn_response=1.,
           # Air (T92, restyled T98, GUESS): a brick. MAVs only just lift it (1.25 weights, any tier) to get up
           # and point the mains, which keep their power. No lift and little glide, drag alike on every axis,
           # the nose stiffens and shakes as speed builds, slides wallow.
           air_terminal_speed=300.,air_engine_loss=.2,air_damping=.5,air_boost=.6,air_lift=0.,air_side_drag=1.,air_nose_authority=.4,
           air_slip_authority=.4,air_weathervane=.5,air_sway_damping=.15,air_buffet=.3,air_overspeed_penalty=2.,
           air_wing_support=.15,air_mav_lift=1.25),
    # Shuttle: utility hopper. Weakest and slowest, but even on every axis and native turning.
    4:dict(acceleration_scale=.75,retro_ratio=.6,lateral_ratio=.5,up_ratio=.6,down_ratio=.5,speed_cap_multiplier=.75,
           accel_fade_exponent=2.5,brake_floor=.9,momentum=.8,overspeed_bleed_time=3.,jerk_limit=125.,settle_time=1.2,
           boost_heat_time=4.,boost_cool_time=8.,boost_cold_strength=.4,
           # Air (T92, restyled T98, GUESS): slower brick than the Hauler, a little more nose and MAV lift left.
           air_terminal_speed=260.,air_engine_loss=.2,air_damping=.5,air_boost=.6,air_lift=0.,air_side_drag=1.,air_nose_authority=.5,
           air_slip_authority=.5,air_weathervane=.6,air_sway_damping=.2,air_buffet=.25,air_overspeed_penalty=2.,
           air_wing_support=.2,air_mav_lift=1.3),
}


AIR_KEYS=tuple(k for k in PROFILE_KEYS.values() if k.startswith('air_'))
# Air styles shipped before T98, recognised on load so a class nobody retuned picks up its new style.
_RETIRED_AIR={1:dict(air_terminal_speed=300.,air_engine_loss=.4,air_damping=.4,air_boost=.5,air_nose_authority=.4,
                     air_slip_authority=.4,air_weathervane=.5,air_sway_damping=.15),
              4:dict(air_terminal_speed=260.,air_engine_loss=.35,air_damping=.4,air_boost=.5,air_nose_authority=.5,
                     air_slip_authority=.5,air_weathervane=.6,air_sway_damping=.2)}


def neutral_profile():
    """The plain law with every class character and energy term off (control defaults)."""
    return {PROFILE_KEYS[c.id]:c.default for c in PROFILE_CONTROLS}


def _stock_air(class_id,raw_profile):
    """Whether a class saved before T98 still has untouched air values: all neutral (its class had no air
    style yet) or the style shipped then. Those take the class air style; tuned values stay."""
    if 'air_omni' in raw_profile:return False  # saved since T98
    neutral=neutral_profile()
    def same(saved,stock):
        return not isinstance(saved,bool) and isinstance(saved,(int,float)) and abs(float(saved)-stock)<1e-9
    return any(all(same(raw_profile[k],style[k]) for k in AIR_KEYS if k in raw_profile)
               for style in (neutral,{**neutral,**_RETIRED_AIR.get(class_id,{})}))


def profile_defaults(class_id=4):
    p=neutral_profile()
    p.update(_SHARED);p.update(SULTAN_BASELINES.get(class_id,{}));return p


def flight_profile(source,class_id=4,native_thrust=1.,native_boost_thrust=None):
    """One class profile for FlightProfile.from_mapping; native thrust is applied by the law.

    The optional engine values are accepted for callers that share one native snapshot;
    the flight consumer resolves native thrust and boost authority from that snapshot.
    """
    if hasattr(source,'profile'):return source.profile(class_id)
    p=profile_defaults(class_id)
    for control_id,key in PROFILE_KEYS.items():
        if control_id in source:p[key]=source[control_id]
    return p


def lever_zone_settings(values):
    return {key.replace('-','_'):values['lever-'+key] for key in (*LEVER_BOUNDARIES,'cruise-level','cruise-enabled','boost-enabled','reverse-mode')}


def defaults():return {c.id:c.default for c in CONTROLS}


def is_visible(control,values):
    return all(values.get(k) in v if isinstance(v,tuple) else values.get(k)==v for k,v in control.visible)


def strafe_enabled(values):return values['flight-model'] in STRAFE_MODELS


def _validate_binding(value):
    if value is None:return None
    match=AXIS_TOKEN.fullmatch(value) if isinstance(value,str) else None
    if not match:raise ValueError('Binding must be a {GUID} or virtual:agent-0 @axis:0x3N token or None')
    device=match[1].upper() if match[1].startswith('{') else match[1].lower()
    return f'{device}@axis:0x{match[2]}'


def parse_token(token):
    """(device identity, axis index 0..7) for a canonical binding token."""
    guid,usage=AXIS_TOKEN.fullmatch(token).groups()
    return guid,int(usage,16)-0x30


def describe_binding(token,device_names=None):
    if token is None:return 'Unbound'
    guid,index=parse_token(token)
    name=(device_names or {}).get(guid) or ('Agent agent-0' if guid=='virtual:agent-0' else 'Missing device '+guid[1:9])
    return f'{name} - {AXIS_USAGE_NAMES[index]}'


def coerce(control,value,values=None):
    """Validated value for control; floats are clamped to bounds and ordered pairs."""
    if control.kind=='bool':
        if not isinstance(value,bool):raise ValueError(f'{control.id}: expected bool')
        return value
    if control.kind=='choice':
        if value not in [v for v,_ in control.choices]:raise ValueError(f'{control.id}: unknown choice {value!r}')
        return value
    if control.kind=='axis-binding':return _validate_binding(value)
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError(f'{control.id}: expected finite number')
    value=min(control.high,max(control.low,float(value)))
    if values is not None:
        for lower,upper in ORDERED_PAIRS:
            if control.id==lower:value=min(value,values[upper]-MIN_GAP)
            elif control.id==upper:value=max(value,values[lower]+MIN_GAP)
        for lower,upper in ZONE_PAIRS:
            if control.id==lower:value=min(value,values[upper]-ZONE_MIN_GAP)
            elif control.id==upper:value=max(value,values[lower]+ZONE_MIN_GAP)
        value=min(control.high,max(control.low,value))
    return round(value,6)


def binding_conflict(values,binding_id,token):
    """Other binding currently holding token, or None."""
    for c in CONTROLS:
        if c.kind=='axis-binding' and c.id!=binding_id and values.get(c.id)==token:return c.id
    return None


@dataclass
class LoadReport:
    issues:list=field(default_factory=list)


class SettingsStore:
    def __init__(self,path):
        self.path=Path(path);self.values=defaults();self.saved=dict(self.values);self.listeners=[]
        self.current_class=4
        self.profiles={str(i):profile_defaults(i) for i in SHIP_CLASSES}
        self.saved_profiles={i:dict(p) for i,p in self.profiles.items()}
        self._sync_profile();self.saved=dict(self.values)
        self.buttons=[];self.saved_buttons=[]  # canonical Binding.to_json() dicts (T61)
        self.button_modifier=None;self.saved_modifier=None  # shift-layer button token

    @property
    def dirty(self):
        return (any(self.values[k]!=self.saved[k] for k in self.values if k not in PROFILE_KEYS)
                or self.profiles!=self.saved_profiles or self.buttons!=self.saved_buttons or self.button_modifier!=self.saved_modifier)

    def _notify(self,changed):
        for listener in list(self.listeners):listener(dict(self.values),changed)

    def load(self):
        report=LoadReport();values=defaults()
        profiles={str(i):profile_defaults(i) for i in SHIP_CLASSES}
        try:data=json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:data=None
        except (OSError,ValueError) as e:data=None;report.issues.append(f'unreadable settings file: {e}')
        if data is not None:
            if not isinstance(data,dict) or data.get('schema') not in SCHEMA_VERSIONS or not isinstance(data.get('values'),dict):
                report.issues.append('unsupported settings file; using defaults')
            else:
                raw=data['values']
                for key in raw:
                    if key not in BY_ID:report.issues.append(f'ignored unknown setting {key}')
                for c in CONTROLS:  # schema order puts each ordered pair's members before use
                    if c.id not in raw or c.id in PROFILE_KEYS:continue
                    try:values[c.id]=coerce(c,raw[c.id])
                    except ValueError as e:report.issues.append(f'{e}; using default')
                if data['schema']<LATERAL_SIGN_SCHEMA:values['strafe-lateral-inverted']=False
                for lower,upper in ORDERED_PAIRS:
                    if values[lower]>values[upper]-MIN_GAP:
                        report.issues.append(f'{lower}/{upper} overlap; using defaults')
                        values[lower]=BY_ID[lower].default;values[upper]=BY_ID[upper].default
                if any(values[a]>values[b]-ZONE_MIN_GAP+1e-9 for a,b in ZONE_PAIRS):
                    report.issues.append('lever zones overlap; using defaults')
                    for key in LEVER_BOUNDARIES:values['lever-'+key]=BY_ID['lever-'+key].default
                raw_profiles=data.get('profiles',{})
                if not isinstance(raw_profiles,dict):report.issues.append('invalid class profiles; using defaults')
                else:
                    for class_key,raw_profile in raw_profiles.items():
                        if class_key not in profiles or not isinstance(raw_profile,dict):
                            report.issues.append(f'ignored invalid class profile {class_key}');continue
                        allowed=set(PROFILE_KEYS.values())
                        for key in raw_profile:
                            if key not in allowed:report.issues.append(f'ignored unknown class field {class_key}/{key}')
                        # A class saved before T90 keeps native rotation until Reset class loads its style.
                        for key in ROTATION_STYLE_KEYS:
                            if key not in raw_profile:profiles[class_key][key]=1.
                        stock_air=_stock_air(int(class_key),raw_profile)
                        for c in PROFILE_CONTROLS:
                            key=PROFILE_KEYS[c.id]
                            if key not in raw_profile or stock_air and key in AIR_KEYS:continue
                            try:profiles[class_key][key]=coerce(c,raw_profile[key])
                            except ValueError as e:report.issues.append(f'class {class_key}: {e}; using default')
        buttons=[];modifier=None
        if isinstance(data,dict) and isinstance(data.get('values'),dict) and data.get('schema') in SCHEMA_VERSIONS and 'buttons' in data:
            raw_buttons=data['buttons'] if isinstance(data['buttons'],dict) else {}
            loaded,modifier,issues=hotas_buttons.load_bindings(raw_buttons.get('bindings'),raw_buttons.get('modifier'))
            seen=set()
            for b in loaded:  # the menu keeps one binding per action
                if b.action in seen:issues.append(f'{b.action}: extra binding on {b.token} dropped');continue
                seen.add(b.action);buttons.append(b.to_json())
            report.issues.extend('buttons: '+i for i in issues)
        self.values=values;self.profiles=profiles;self.buttons=buttons;self.saved_buttons=[dict(b) for b in buttons]
        self.button_modifier=self.saved_modifier=modifier
        self._sync_profile();self.saved=dict(values)
        self.saved_profiles={i:dict(p) for i,p in profiles.items()};self._notify(set(values)|{'buttons'})
        return report

    def _sync_profile(self):
        profile=self.profiles[str(self.current_class)]
        for control_id,key in PROFILE_KEYS.items():self.values[control_id]=profile[key]

    def profile(self,class_id):
        """A copy: consumers cannot bypass validation or mutate another class accidentally."""
        return dict(self.profiles.get(str(class_id),profile_defaults(class_id)))

    def select_class(self,class_id):
        """Select the flown class for menu editing; selection itself is not a dirty edit."""
        if type(class_id) is not int or class_id not in SHIP_CLASSES or class_id==self.current_class:return False
        self.current_class=class_id;self._sync_profile();return True

    def reset_class(self):
        self.profiles[str(self.current_class)]=profile_defaults(self.current_class)
        self._sync_profile();self._notify(set(PROFILE_KEYS))

    def reset_air_style(self):
        """The edited class's air style back to its baseline; space tuning and planet settings stay."""
        self.reset([c for c,k in PROFILE_KEYS.items() if k in AIR_KEYS])

    def copy_class_to_all(self):
        current=self.profile(self.current_class)
        self.profiles={str(i):dict(current) for i in SHIP_CLASSES}
        self._notify(set(PROFILE_KEYS))

    def set(self,control_id,value):
        """Apply immediately; returns the stored (possibly clamped) value."""
        control=BY_ID[control_id]
        value=coerce(control,value,self.values)
        if self.values[control_id]!=value:
            self.values[control_id]=value
            if control_id in PROFILE_KEYS:self.profiles[str(self.current_class)][PROFILE_KEYS[control_id]]=value
            self._notify({control_id})
        return value

    def assign_binding(self,binding_id,token,steal_from=None):
        changed={binding_id}
        if steal_from is not None:self.values[steal_from]=None;changed.add(steal_from)
        self.values[binding_id]=_validate_binding(token);self._notify(changed)

    def reset(self,control_ids):
        """Back to defaults; class tuning rows return to the edited class's baseline."""
        baseline=profile_defaults(self.current_class)
        target={i:baseline[PROFILE_KEYS[i]] if i in PROFILE_KEYS else BY_ID[i].default for i in control_ids}
        changed={i for i in control_ids if self.values[i]!=target[i]}
        for i in control_ids:
            self.values[i]=target[i]
            if i in PROFILE_KEYS:self.profiles[str(self.current_class)][PROFILE_KEYS[i]]=target[i]
        if changed:self._notify(changed)

    # ----- button bindings (T61): one binding per action, on the normal or shift layer -----
    def button_bindings(self):
        return hotas_buttons.load_bindings(self.buttons,self.button_modifier)[0]

    def button_for(self,action_id):
        return next((b for b in self.buttons if b['action']==action_id),None)

    def button_conflict(self,action_id,token,layer=None):
        """What already uses token on this action's layer: an action id, 'modifier', or None.

        layer defaults to the action's current layer (normal when unbound)."""
        token=hotas_buttons.canonical(token)
        if token==self.button_modifier:return 'modifier'
        if layer is None:layer=(self.button_for(action_id) or {}).get('layer','normal')
        return next((b['action'] for b in self.buttons if b['token']==token and b['layer']==layer and b['action']!=action_id),None)

    def assign_button(self,action_id,token,steal_from=None,layer=None):
        """Bind action_id to token, keeping its behaviour (and its layer unless layer is given).
        steal_from: the action (or 'modifier') that held the token, which is cleared."""
        current=self.button_for(action_id) or {}
        binding=hotas_buttons.coerce_binding({'action':action_id,'token':token,'behaviour':current.get('behaviour'),'layer':layer or current.get('layer','normal')})
        if steal_from=='modifier':self.button_modifier=None
        self.buttons=[b for b in self.buttons if b['action'] not in (action_id,steal_from)]
        self.buttons.append(binding.to_json());self._notify({'buttons'})

    def clear_button(self,action_id):
        kept=[b for b in self.buttons if b['action']!=action_id]
        if kept!=self.buttons:self.buttons=kept;self._notify({'buttons'})

    def _replace(self,current,**change):
        candidate=hotas_buttons.coerce_binding({**current,**change})
        others=[hotas_buttons.coerce_binding(b) for b in self.buttons if b is not current]
        if hotas_buttons.find_conflict(others,candidate,self.button_modifier) is not None:return False
        self.buttons=[candidate.to_json() if b is current else b for b in self.buttons];self._notify({'buttons'})
        return True

    def cycle_button_behaviour(self,action_id):
        current=self.button_for(action_id)
        if current is None:return None
        allowed=hotas_buttons.action(action_id).behaviours
        nxt=allowed[(allowed.index(current['behaviour'])+1)%len(allowed)] if current['behaviour'] in allowed else allowed[0]
        return nxt if self._replace(current,behaviour=nxt) else current['behaviour']  # e.g. two long presses on one button

    def cycle_button_layer(self,action_id):
        """Normal <-> shift (with the modifier held). Refused when the other layer's slot is taken."""
        current=self.button_for(action_id)
        if current is None:return None
        nxt='normal' if current['layer']=='shift' else 'shift'
        return nxt if self._replace(current,layer=nxt) else current['layer']

    def modifier_conflict(self,token):
        """Action bound to token on any layer: the modifier button carries nothing else."""
        token=hotas_buttons.canonical(token)
        return next((b['action'] for b in self.buttons if b['token']==token),None)

    def set_modifier(self,token,steal_from=None):
        token=hotas_buttons.canonical(token)
        self.buttons=[b for b in self.buttons if b['token']!=token or b['action']!=steal_from]
        self.button_modifier=token;self._notify({'buttons'})

    def clear_modifier(self):
        if self.button_modifier is not None:self.button_modifier=None;self._notify({'buttons'})

    def clear_buttons(self):
        if self.buttons or self.button_modifier:self.buttons=[];self.button_modifier=None;self._notify({'buttons'})

    def revert(self):
        changed={k for k in self.values if self.values[k]!=self.saved[k]}
        if self.profiles!=self.saved_profiles:changed.update(PROFILE_KEYS)
        if self.buttons!=self.saved_buttons or self.button_modifier!=self.saved_modifier:
            changed.add('buttons');self.buttons=[dict(b) for b in self.saved_buttons];self.button_modifier=self.saved_modifier
        self.values=dict(self.saved)
        self.profiles={i:dict(p) for i,p in self.saved_profiles.items()};self._sync_profile()
        if changed:self._notify(changed)

    def save(self):
        if not self.dirty and self.path.exists():return False
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_name(self.path.name+'.tmp')
        base={k:v for k,v in self.values.items() if k not in PROFILE_KEYS}
        tmp.write_text(json.dumps({'schema':SCHEMA_VERSION,'values':base,'profiles':self.profiles,'buttons':{'bindings':self.buttons,'modifier':self.button_modifier}},indent=1,sort_keys=True),encoding='utf-8')
        os.replace(tmp,self.path)
        self.saved=dict(self.values);self.saved_buttons=[dict(b) for b in self.buttons];self.saved_modifier=self.button_modifier
        self.saved_profiles={i:dict(p) for i,p in self.profiles.items()}
        return True
