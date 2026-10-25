"""Absolute Flight for No Man's Sky (code name AbsoluteHOTAS): release runtime (pyMHF mod), build 180836 / Steam 25732212.

Loads automatically. A separately enabled D21 developer pipe is disabled by default.
Devices come from the input bus (D11) loaded
in-process, bindings and tuning from the menu-owned settings file (D15), and commands reach
the game only through the D12 mailbox and the validated native caves (D10/D12/D16):
pitch/yaw scalar substitution, roll lane, throttle stack-u and MAV strafe. Head tracking
(OpenTrack FreeTrack -> first-person cockpit camera) uses the qualified H3 camera hook (head_tracking.py).

Safety model, unchanged from the validated flights:
- Every cave checks build-pinned guards (pilot ship, flight clock, menu flags, thread, lease).
- Native axes expire after 100 ms. In qualified Sultan flight, an expired
  translation command instead writes zero acceleration plus isolated gravity
  compensation, so a publisher stall coasts without resurrecting native braking.
- A channel arms only while piloting with the game focused; every axis ramps in from where it is and
  none waits for centre (hotas_control, user 2026-10-22).
- Native input on a substituted axis (e.g. Steam Input still mapping the HOTAS) is a conflict:
  the cave rejects the frame and the runtime reports it. The game's own throttle (launch/station
  auto-boost) is not: throttle hands over to it and the stick axes stay live.
Reloading the mod shuts the previous instance down first; the game keeps running.
"""
import ctypes as C
import dataclasses
import json
import math
import os
import struct
import sys
import threading
import time
import traceback
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
for _p in (ROOT/'mod',ROOT/'tools'):
    if str(_p) not in sys.path:sys.path.insert(0,str(_p))

from pymhf import Mod
from pymhf.core import _internal
from pymhf.core._types import FUNCDEF
from pymhf.core.hooking import NOOP,get_caller,hook_manager,manual_hook

from command_mailbox import CommandMailbox,PAYLOAD,STRICT_ROTATION
from hotas_control import FlightControl,MAILBOX_ORDER,NATIVE_LIMITS,StallMonitor
from hotas_input import AxisCapture,BusReader,ButtonCapture,ClearableCapture,DeviceTable,KeyCapture
from key_lane import win32_lane
from button_actions import ButtonActions,flight_buttons_live
from g_effects import GEffects
from flight_telemetry import SHIP_FIELDS,TelemetryWriter,build_row,decode_receipt
from hotas_settings import SettingsStore
from hotas_settings import lever_zone_settings
from newtonian_control import TranslationPublisher
from pilot_boundary import PilotBoundary,local_player_ship
from weapon_lead import ShipLeadCorrection
from combat_aiming import CombatAiming,slowdown_gain
from pip_presentation import PipPresentation
from cockpit_speed import CockpitSpeedReadout
from head_tracking import HeadTracker,ProcessMemory
from ai_pilots import AIPilotTuning,wanted_mode
from ai_accuracy import EvasiveAccuracy,enabled as aim_tuning_enabled
from ai_combat import NPCCombatTuning
from black_space import BlackSpace
from terrain_lookahead import TerrainLookahead
from newtonian_runtime import FlightConsumer, SUPPORTED_CLASSES
CRASH_BLACKOUT_HOLD=2.  # T93: seconds a ditched pilot stays out beyond the normal G recovery
WRECK_CAUSES=('ditch','collision')  # ejecting from these has the game put the wreck down landed (user 2026-10-21)
EJECT_REQUEST_MS=500    # T93: a held-eject request the flight thread has not taken by then is dropped
from hotas_draw import NVG,MeterPainter,layout_from_tls,panel_geometry,row_geometry
import flight_hud
import planetary_hud
import manual_landing
import planetary_flight
import ditch_purge
import ship_condition
import ship_alarms
import crash_damage
from lift_trim import LiftTrim
from hotas_page import CONTROLS_PAGE,NativeMenu,NativeOptions,Strings
from pause_root import SOUND_HASH,PauseRootButton,key_bytes
import runtime_config
import code_patch
import support_bundle
import flight_tunings
import rotation_validation
import additive_rotation

BUILD_HASH='d7e4c0b20e2fa555af2b7e1b358e05cebf8fabc8'
DATA=runtime_config.data_dir(ROOT)  # dev checkout: config/; release: %LOCALAPPDATA%\AbsoluteHOTAS (T100)
SETTINGS=DATA/'hotas-settings.json'
TUNINGS=DATA/'flight-tunings'  # named per-class flight tunings, <Class>_<Name>.json
GAME_BUILD='180836'  # NMS.exe FileVersion of BUILD_HASH, written into saved tunings
LOGS=DATA/'logs'
LEASE_MS=1000
TICK_SECONDS=.004
CODE_WRITE_SECONDS=1.  # flight thread must take a queued code write within this, else fall back
FRAME_SIZE=512
MAV_FORCE=256

K=C.WinDLL('kernel32',use_last_error=True)
K.GetTickCount64.restype=C.c_uint64
K.GetCurrentThreadId.restype=C.c_uint32
U=C.WinDLL('user32')
U.GetForegroundWindow.restype=C.c_void_p
U.GetWindowThreadProcessId.argtypes=[C.c_void_p,C.POINTER(C.c_ulong)]


def q(address):return C.c_uint64.from_address(address).value


class Mem:
    u8=staticmethod(lambda a:C.c_uint8.from_address(a).value)
    i32=staticmethod(lambda a:C.c_int32.from_address(a).value)
    u64=staticmethod(q)


# E-pause natives (research/re-log/2026-10-16_claude_pause-root-buttons.md): rva, entry32, ABI.
PAUSE_ROOT_NATIVE={
    'FindLayer':(0x1DFC30,'40555741574883ec204c8b8950010000458bf8488bea488bf94d85c90f844302',
                 C.c_uint64,[C.c_void_p,C.c_void_p,C.c_uint32]),
    'PollButton':(0x8F29D0,'48896c24204154415641574883ec4080b9444c010000450fb6f1450fb6f84c8b',
                  C.c_bool,[C.c_void_p,C.c_void_p,C.c_uint8,C.c_uint8,C.c_void_p,C.c_uint8,C.c_uint8]),
}
APP_GLOBAL=0x6E8D708


class Block(C.Structure):
    """Same layout as the validated menu_probe bridge: caves read pilot_until at +24."""
    _fields_=[('reserved',C.c_uint64*3),('pilot_until',C.c_uint64),('ship',C.c_uint64)]


class AbsoluteHOTAS(Mod):
    __author__='AbsoluteHOTAS-NMS'
    __description__='Absolute 1:1 HOTAS/HOSAS flight axes'

    def __init__(self):
        # Refuse before pyMHF can register hooks at offsets that only exist on this build.
        if str(_internal.BINARY_HASH)!=BUILD_HASH:raise RuntimeError('Absolute Flight: unsupported game build; nothing hooked')
        super().__init__()
        previous=getattr(sys,'_absolute_hotas_runtime',None)
        if previous is not None:
            try:previous.shutdown('reloaded')
            except BaseException:pass
        sys._absolute_hotas_runtime=self
        self.active=False;self.stopping=threading.Event();self.lock=threading.Lock()
        self.block=Block();self.rigid=0;self.controller=0;self.ship_class=None
        self.pilot_boundary=PilotBoundary()
        self.pilot_recovery=None;self.pilot_recovery_retry=0.;self.pilot_recoveries=0
        self.frame=0;self.box=None;self.pm=None;self.patches=None;self.mav=None;self.installed_ship=0;self.installed_turn_scale=None
        self.bus=None;self.reader=None;self.table=DeviceTable();self.capture=None;self.capture_done=None
        self.agent_test=None;self.agent_test_owns=False
        self.errors=0;self.hook_errors=0;self.conflicts=0;self.last_receipt=0;self.status={'state':'starting'}
        self.menu_open=False
        self.code_lock=threading.Lock();self.code_writes=[];self.code_patcher=False  # False: not loaded yet
        self.uninstall_lock=threading.Lock()
        LOGS.mkdir(parents=True,exist_ok=True)
        self.log_path=LOGS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-runtime.log')
        self.cockpit_speed=CockpitSpeedReadout(int(_internal.BASE_ADDRESS))
        memory=ProcessMemory()  # AI tables are heap data: a stale pointer must fail, not fault
        self.ai_pilots=AIPilotTuning(int(_internal.BASE_ADDRESS),memory.read,memory.write)
        self.ai_combat=NPCCombatTuning(int(_internal.BASE_ADDRESS),memory.read,memory.write)
        self.black_space=BlackSpace(int(_internal.BASE_ADDRESS),memory.read,memory.write)
        # T107: the hook moves the region map's own stack copy of the observer, so it reads and writes it directly.
        self.terrain=TerrainLookahead(int(_internal.BASE_ADDRESS),memory.read,memory.write,
                                      raw_read=C.string_at,raw_write=lambda address,data:C.memmove(address,data,len(data)))
        self.terrain_motion=(0,None)
        self.ai_aim=EvasiveAccuracy(int(_internal.BASE_ADDRESS),memory.read,K.GetTickCount64)
        try:self.ai_aim.verify()
        except Exception as error:self.log('enemy aim disabled: '+repr(error))
        self.weapon_lead=ShipLeadCorrection(int(_internal.BASE_ADDRESS),clock=K.GetTickCount64)
        try:
            self.weapon_lead.verify()
            self.log('weapon lead: '+json.dumps(self.weapon_lead.status()))
        except BaseException as error:
            self.weapon_lead.verified=False
            self.weapon_lead.latest={'state':'verification failed: '+repr(error),'inheritance_multiplier':None}
            self.log('weapon lead disabled: '+repr(error))
        self.store=SettingsStore(SETTINGS);report=self.store.load()
        for issue in report.issues:self.log('settings: '+issue)
        self.store.tuning_files=flight_tunings.FlightTunings(TUNINGS,build=GAME_BUILD)
        self.control=FlightControl(self.store.values);self.stall=StallMonitor();self.geffects=GEffects(log=self.log);self.buttons=ButtonActions(self.store,win32_lane(trace_path=self.log_path.with_name(self.log_path.name.replace('-runtime.log','-keys.log'))))
        self.translation=TranslationPublisher();self.translation_command=None;self.lift_trim=LiftTrim()
        self.translation_profiles={c:self.store.profile(c) for c in SUPPORTED_CLASSES}
        self.flight_consumer=FlightConsumer();self.newtonian=None;self.newtonian_retry=0.
        self.newtonian_failure='not selected';self.newtonian_last_sequence=0
        self.newtonian_receipt_path=None;self.newtonian_last_drain=0.;self.newtonian_transition=None
        self.newtonian_failed=[]
        self.newtonian_refresh_requested=None;self.newtonian_refresh_count=0
        self.newtonian_lock=threading.Lock() # metadata only; never held across code-write waits
        self.translation_callback=C.CFUNCTYPE(C.c_int,C.c_void_p,C.c_void_p)(self._translation_helper)
        # Native allocations/callbacks outlive a hot reload, including an invocation
        # already inside a cave. Retain them until the process exits.
        retained=getattr(sys,'_absolute_hotas_translation_callbacks',None)
        if retained is None:retained=sys._absolute_hotas_translation_callbacks=[]
        retained.append((self,self.translation_callback))
        self.store.listeners.append(self._settings_changed)
        self.head=None
        try:
            self.head=HeadTracker(int(_internal.BASE_ADDRESS),self.store.values,log=self.log)
            self.store.listeners.append(self.head.settings_changed)
            self.buttons.on_head_recentre=self.head.recentre
        except BaseException as error:self.log('head tracking disabled: '+repr(error))
        self.box=CommandMailbox(limits=NATIVE_LIMITS)
        self.aiming=CombatAiming(int(_internal.BASE_ADDRESS),self.weapon_lead,clock=K.GetTickCount64)
        self.pip_presentation=PipPresentation(int(_internal.BASE_ADDRESS),K.GetTickCount64)
        self.aim_slowdown=None;self.aim_slowdown_failed=False;self.aim_pip_visible=None
        self.crash_damage=None;self.crash_damage_failed=False;self.crash_last_hit=None
        # T95: ship alarms (cues on the worker, native sound/engine/lights on the flight thread).
        self.alarms=ship_alarms.ShipAlarms();self.native_alarms=None;self.native_alarms_failed=False
        self.alarms_drained=0;self.alarm_eject=False
        self.voice_player=None;self.voice_player_failed=False  # T99: rendered purge lines (mod/voice_player.py)
        self.aim_draw=threading.local()
        for name,adapter in (('aim solution',self.aiming),('pip presentation',self.pip_presentation)):
            try:self.log(name+': '+str(adapter.verify()))
            except BaseException as error:self.log(name+' unavailable: '+repr(error))
        self.telemetry=self._telemetry_writer()
        self.box.publish((0.,)*6,K.GetTickCount64(),enabled=False)
        self.ui=threading.local();self.menu=None;self.pause_root=None;self.ui_api={};self.originals={};self.roots={};self.ui_layout=None;self.draw_errors=0
        self.meters_enabled=True;self.last_options=0.;self.trace_key=None;self.trace_seen=set()
        self.counters={'options':0,'emits':0,'draws':0,'draw_skips':0,'pushes':0,'hud_draws':0}
        # T73 flight HUD: the flight thread publishes one immutable sample, the render thread draws it.
        self.hud_sample=None;self.hud_painter=None;self.hud_errors=0;self.hud_state='not verified'
        self.hud_reader=flight_hud.HudReader(int(_internal.BASE_ADDRESS))
        # T87: native-flight velocity from the cave's published record; planets via ReadProcessMemory.
        self.hud_motion_reader=planetary_hud.MotionReader();self.hud_planet_reader=planetary_hud.PlanetReader(int(_internal.BASE_ADDRESS))
        self.hud_body_reader=planetary_hud.BodyReader()
        self.hud_planets=(0,[]);self.hud_live={}
        # T88 planetary flight: worker-published planet list and settings, read by the flight-thread helper.
        self.air_planets=(0.,[]);self.air_env=None;self.planetary_on=False;self.planetary_bridge=None;self.engine_blocks=None
        try:self.hud_state='ready' if self.hud_reader.verify() else 'code signature mismatch'
        except BaseException as error:self.hud_state='verification failed: '+repr(error)
        try:self._build_menu()
        except BaseException as e:self.menu=None;self.log('menu disabled: '+repr(e))
        self.active=True
        self.thread=threading.Thread(target=self._run,name='AbsoluteHOTAS',daemon=True);self.thread.start()
        if self.head is not None:self.head.start()

    # ---------- logging/status ----------
    def log(self,message):
        line=f'{datetime.now(timezone.utc).isoformat()} {message}\n'
        try:
            with self.log_path.open('a',encoding='utf-8') as f:f.write(line)
        except OSError:pass

    def trace(self,message):
        """Menu trace for live diagnosis; bounded so a stuck screen can't flood the log."""
        self.trace_lines=getattr(self,'trace_lines',0)+1
        if self.trace_lines<=2000:self.log('menu: '+message)

    def _set_state(self,state,**extra):
        if self.status.get('state')!=state:self.log('state: '+state)
        self.status={'state':state,**extra}

    def _settings_changed(self,values,changed):
        with self.lock:
            try:additive_rotation.disarm(self,'settings changed')
            except BaseException as error:self.log('additive rotation disable failed: '+repr(error))
            self.control.reconfigure(values,changed)
            if any(k in changed for k in ('tuning-additive-pitch-yaw','tuning-pitch-rate','tuning-yaw-rate','tuning-roll-rate','tuning-turn-response')):
                for axis in ('pitch','yaw'):self.control._disarm(axis,'rotation tuning changed')  # re-arms with the ramp
            if 'buttons' in changed or 'parking-brake-mode' in changed:self.buttons.reload()
            self.translation_profiles={c:self.store.profile(c) for c in SUPPORTED_CLASSES}
            if any(key.startswith('ai-') or key in ('flight-model','flight-controls-enabled') for key in changed):
                aim=getattr(self,'ai_aim',None)
                if aim is not None:aim.reset()
            if any(key.startswith(('pip-','aim-slowdown-')) for key in changed):
                self.aim_pip_visible=None
                if getattr(self,'aiming',None) is not None:self.aiming.reset('settings changed')
                if getattr(self,'pip_presentation',None) is not None:self.pip_presentation.clear()
                if getattr(self,'aim_slowdown',None) is not None:self.aim_slowdown.disable()
                elif 'aim-slowdown-enabled' in changed and not values.get('aim-slowdown-enabled',False):
                    self.aim_slowdown_failed=False
            if any(key.startswith('crash-') for key in changed):
                if getattr(self,'crash_damage',None) is not None and not values.get('crash-damage',False):
                    self.crash_damage.disable()
                elif 'crash-damage' in changed and not values.get('crash-damage',False):
                    self.crash_damage_failed=False

    # NPC guns receive a private behavior row only for their positively matched player target.
    @manual_hook('AbsoluteHOTASAIGun',offset=0x1714510,
                 func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='before')
    def ai_gun_before(self,this,point,target,opaque):
        aim=getattr(self,'ai_aim',None)
        if aim is None:return None
        values=dict(self.store.values);eligible=False
        try:
            eligible=bool(self.active and values['flight-controls-enabled'] and aim_tuning_enabled(values)
                          and self._focused() and not self.menu_open and self.capture is None
                          and self._eligible_native(K.GetTickCount64(),allow_combat_flight=True))
        except Exception:pass
        aim.begin(this,target,values,self.block.ship,eligible)
        return None

    @manual_hook('AbsoluteHOTASAIGun',offset=0x1714510,
                 func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='after')
    def ai_gun_after(self,this,point,target,opaque):
        aim=getattr(self,'ai_aim',None)
        if aim is not None:aim.end(this)
        return None

    @get_caller
    @manual_hook('AbsoluteHOTASAIBehavior',offset=0x171E340,
                 func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='after')
    def ai_gun_behavior(self,manager,position,role,_result_):
        aim=getattr(self,'ai_aim',None)
        if aim is None:return None
        try:
            if (not self.active or not self.store.values['flight-controls-enabled']
                    or not aim_tuning_enabled(self.store.values) or not self._focused()
                    or self.menu_open or self.capture is not None
                    or not self._eligible_native(K.GetTickCount64(),allow_combat_flight=True)):return None
            return aim.row(manager,role,_result_,self.ai_gun_behavior.caller_address())
        except Exception:return None


    def _aiming_requested(self):
        values=self.store.values
        return values.get('pip-mode','lead')=='lag' or values.get('aim-slowdown-enabled',False)

    def _aiming_eligible(self):
        return (self.active and self._aiming_requested() and self.store.values.get('flight-controls-enabled',False)
                and self.store.values.get('flight-model')=='sultan'
                and self._eligible_native(K.GetTickCount64(),allow_combat_flight=True))

    # ---------- hooks (validated in D12/D16; build-pinned callers) ----------
    def _lead_begin(self,lane,this):
        lead=self.weapon_lead
        frame=lead.begin(lane,this)  # false sentinel precedes all admission reads
        try:
            eligible=bool(self.active and self.store.values['flight-controls-enabled']
                and self.store.values['flight-model']=='sultan'
                and self._eligible_native(K.GetTickCount64(),allow_combat_flight=True))
            lead.qualify(frame,self.block.ship,eligible)
        except BaseException as error:
            frame['allowed']=False
            lead.latest={'state':'scope rejected: '+repr(error),'inheritance_multiplier':None}

    @manual_hook('AbsoluteHOTASLeadHUD',offset=0x984340,func_def=FUNCDEF(None,[C.c_void_p,C.c_float]),detour_time='before')
    def lead_hud_before(self,this,dt):
        self._lead_begin('pip',this)
        aiming=getattr(self,'aiming',None)
        if aiming is not None:
            self.aim_pip_visible=None
            try:
                aiming.begin_hud(this,self.block.ship,self._aiming_eligible())
                presenter=getattr(self,'pip_presentation',None)
                if presenter is not None:presenter.clear()
            except BaseException:aiming.reset('HUD admission failed')

    @manual_hook('AbsoluteHOTASLeadHUD',offset=0x984340,func_def=FUNCDEF(None,[C.c_void_p,C.c_float]),detour_time='after')
    def lead_hud_after(self,this,dt):
        try:
            aiming=getattr(self,'aiming',None)
            if aiming is not None:
                aiming.end_hud(this)
                self._update_pip(this)
        except BaseException as error:
            self.aim_pip_visible=None
            if getattr(self,'aiming',None) is not None:self.aiming.reset('HUD solution rejected: '+repr(error))
        finally:self.weapon_lead.end('pip',this)

    @manual_hook('AbsoluteHOTASLeadAim',offset=0x13F3330,func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_float]),detour_time='before')
    def lead_aim_before(self,this,target,speed):
        self._lead_begin('aim_assist',this)

    @manual_hook('AbsoluteHOTASLeadAim',offset=0x13F3330,func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_float]),detour_time='after')
    def lead_aim_after(self,this,target,speed):
        self.weapon_lead.end('aim_assist',this)

    @manual_hook('AbsoluteHOTASWeaponLead',offset=0x158D930,
                 func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_float]),detour_time='before')
    def weapon_prediction(self,target,out,position,velocity,speed):
        eligible=False;result=None
        try:
            eligible=(self.active and self.store.values['flight-model']=='sultan'
                      and self.store.values['flight-controls-enabled']
                      and self._eligible_native(K.GetTickCount64(),allow_combat_flight=True))
            if eligible:result=self.weapon_lead.arguments(target,out,position,velocity,speed)
        except BaseException as error:
            self.weapon_lead.latest={'state':'prediction rejected: '+repr(error),'inheritance_multiplier':None}
            eligible=False
        aiming=getattr(self,'aiming',None)
        if aiming is not None:
            try:aiming.prediction_begin(target,out,position,velocity,speed,eligible=eligible and self._aiming_requested())
            except BaseException:aiming.reset('prediction observation failed')
        return result

    @manual_hook('AbsoluteHOTASWeaponLead',offset=0x158D930,
                 func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_float]),detour_time='after')
    def weapon_prediction_after(self,target,out,position,velocity,speed,_result_):
        aiming=getattr(self,'aiming',None)
        if aiming is not None:
            try:aiming.prediction_end(target,out)
            except BaseException:aiming.reset('prediction completion failed')

    @get_caller
    @manual_hook('AbsoluteHOTASAimSolver',offset=0x158DC80,
                 func_def=FUNCDEF(C.c_float,[C.c_void_p,C.c_void_p,C.c_float]),detour_time='after')
    def aim_solver_after(self,r,u,speed,_result_):
        aiming=getattr(self,'aiming',None)
        if aiming is not None:
            try:aiming.solver_after(r,u,speed,_result_,caller=self.aim_solver_after.caller_address())
            except BaseException:aiming.reset('solver observation failed')

    @manual_hook('AbsoluteHOTASManualAim',offset=0x13F4D20,
                 func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p,C.c_float]),detour_time='after')
    def manual_aim_after(self,this,out,distance,_result_):
        aiming=getattr(self,'aiming',None)
        if aiming is not None:
            try:
                aiming.observe_manual(this,out if _result_==out else 0,self.block.ship,self._aiming_eligible())
            except BaseException:aiming.reset('manual reference rejected')

    def _update_pip(self,hud):
        presenter=getattr(self,'pip_presentation',None)
        epoch=presenter.clear() if presenter is not None else None
        eligible=self._aiming_eligible()
        solution=self.aiming.sample(ship=self.block.ship,eligible=eligible,mode=self.store.values.get('pip-mode','lead'))
        self.aim_pip_visible=None
        app=q(int(_internal.BASE_ADDRESS)+0x6E8D708)
        if (eligible and app and hud==app+0x7329C0 and solution
                and C.c_ubyte.from_address(hud+0xD8C15).value):
            self.aim_pip_visible=(solution['ship'],solution['target'],solution['tick'])
        if presenter is not None:
            shown=presenter.update(hud,self.block.ship,self.store.values,solution,eligible,epoch=epoch)
            if self.store.values.get('pip-mode','lead')=='lag' and not shown:self.aim_pip_visible=None

    @manual_hook('AbsoluteHOTASPipDrawScope',offset=0x996700,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='before')
    def pip_draw_before(self,this):
        local=getattr(self,'aim_draw',None)
        if local is None:return
        stack=getattr(local,'stack',None)
        if stack is None:stack=local.stack=[]
        stack.append(int(this or 0))

    @manual_hook('AbsoluteHOTASPipDrawScope',offset=0x996700,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='after')
    def pip_draw_after(self,this):
        local=getattr(self,'aim_draw',None)
        stack=getattr(local,'stack',None)
        if stack and stack[-1]==int(this or 0):stack.pop()
        elif stack:stack.clear()

    @NOOP
    @manual_hook('AbsoluteHOTASPipWidget',offset=0x2DC9A20,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='before')
    def pip_widget(self,this):
        try:
            stack=getattr(getattr(self,'aim_draw',None),'stack',None)
            presenter=getattr(self,'pip_presentation',None)
            if (self.active and stack and this==stack[-1]+0xD87E0 and presenter is not None
                    and presenter.owns(this)):
                if presenter.suppress(this,self.store.values,self.block.ship):return None
                if self.store.values.get('pip-mode','lead')=='lag':
                    aiming=getattr(self,'aiming',None)
                    eligible=(self.store.values.get('flight-model')=='sultan'
                              and self.store.values.get('flight-controls-enabled',False))
                    if aiming is None or aiming.sample(ship=self.block.ship,eligible=eligible,mode='lag') is None:return None
        except BaseException:pass
        try:return self._original(0x2DC9A20)(this)
        except BaseException as error:
            self.hook_errors+=1;self.log('native pip draw failed: '+repr(error));return None

    # Keep UpdateControlled's native return address intact for its exact caller gate.
    # pyMHF calls a hooked original through ctypes, so hook the outer Update instead.
    @manual_hook('AbsoluteHOTASPilot',offset=0x174A600,func_def=FUNCDEF(None,[C.c_void_p,C.c_float]),detour_time='after')
    def pilot(self,this,dt):
        try:
            ship=C.cast(this,C.c_void_p).value
            thread=K.GetCurrentThreadId()
            if getattr(self,'salvage',None) is not None and thread==getattr(self,'salvage_thread',None):self._step_salvage()
            frame_thread=C.c_uint32.from_address(self.frame+32).value if self.frame else 0
            # During a lifecycle handover this callback is maintenance-only.
            # Never renew a clock against the old installed native thread pins.
            pending=getattr(self,'pilot_recovery',None)
            if pending is not None:
                retry=getattr(self,'pilot_recovery_retry',0.)
                if retry and time.monotonic()>=retry and self._player_context(ship) is not None:
                    # A second reload can replace the maintenance owner while
                    # restoration is parked. Delivery remains disabled throughout.
                    self.pilot_recovery=(ship,thread);self.pilot_recovery_retry=0.
                    pending=self.pilot_recovery
                if (ship,thread)==pending and self._player_context(ship) is not None:
                    self._drain_code_writes()
                return None
            if not self.pilot_boundary.admit(C.string_at,int(_internal.BASE_ADDRESS),ship,
                                             thread,frame_thread):
                if self.block.pilot_until<=K.GetTickCount64() and self._player_context(ship) is not None:
                    self._request_pilot_recovery(ship,thread)
                    self._drain_code_writes()
                return None
            if self.active and self.patches is not None:
                context=self._player_context(ship)
                if context is None:return None
                rigid,controller=context
                if (ship!=self.installed_ship or rigid!=self.rigid or controller!=self.controller):
                    self._request_pilot_recovery(ship,thread)
                    self._drain_code_writes()
                    return None
            # Only the positive player's pinned flight thread may drain; retain
            # this path while inactive so shutdown can restore owned code.
            self._drain_code_writes()
            if not self.active:return None
            if C.c_ubyte.from_address(ship+0x60C8).value!=1:return None
            if self.block.ship!=ship:self.block.pilot_until=0;self.block.ship=ship
            self.rigid=q(ship+0x6248)+0x60
            class_owner=q(ship+0x28)
            self.ship_class=C.c_int32.from_address(class_owner+0xD0).value if class_owner else None
            # Controlled has returned: the pinned flight thread can now replace
            # qualified metadata without racing any of its native consumers.
            self._refresh_newtonian_fixture(ship)
            self._alarms_frame(ship)
            if getattr(self,'ship_damage_pending',None)=='away':self._damage_abandoned_ship()
            # T93: a held Eject runs the native eject here, on the flight thread outside UpdateControlled.
            request=getattr(self,'eject_request',None)
            if request is not None:
                self.eject_request=None
                if K.GetTickCount64()-request<=EJECT_REQUEST_MS:self._eject()
        except BaseException:self.hook_errors+=1;self.block.pilot_until=0
        return None

    # ---------- head tracking: H3 camera composition (build 180836; caller 0x656C43 only) ----------
    @get_caller
    @manual_hook('AbsoluteHOTASHeadPose',offset=0x1422A50,
                 func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_float,C.c_void_p,C.c_float]),detour_time='after')
    def head_pose(self,this,output,base,parameter4,limits,dt,_result_):
        """All gates, the lease and the write live in HeadTracker.camera; this only supplies the flight clock."""
        head=getattr(self,'head',None)
        if head is None or not head.enabled or not self.active:return None
        try:
            recovering=getattr(self,'pilot_recovery',None) is not None
            head.camera(this,output,base,_result_,self.head_pose.caller_address(),
                        0 if recovering else self.block.ship,self.block.pilot_until)
        except BaseException:self.hook_errors+=1
        return None

    def _player_context(self,ship):
        """Fresh positive player identity; never dereference the discarded ship."""
        base=int(_internal.BASE_ADDRESS)
        if not ship or local_player_ship(C.string_at,base)!=ship:return None
        if C.c_ubyte.from_address(ship+0x60c8).value!=1:return None
        physics=q(ship+0x6248);wrapper=q(ship+0x60c0)
        if not physics or not wrapper:return None
        controller=q(wrapper)
        if not controller or local_player_ship(C.string_at,base)!=ship:return None
        return physics+0x60,controller

    def _request_pilot_recovery(self,ship,thread):
        self.block.pilot_until=0
        aim=getattr(self,'ai_aim',None)
        if aim is not None:aim.reset()
        self.pilot_recovery=(ship,thread)
        self.log(f'player lifecycle changed: ship={ship:#x} thread={thread}; reacquiring')

    def _recover_pilot(self,now):
        """Worker, under self.lock: detach before resetting any lifetime pin."""
        if self.pilot_recovery is None or now<self.pilot_recovery_retry:return False
        self.pilot_recovery_retry=0.
        self.box.disable();self.block.pilot_until=0
        self.control.disarm_all('reacquiring player ship')
        self.translation.disarm('reacquiring player ship');self.translation_command=None
        self.buttons.release('reacquiring player ship')
        try:
            self._uninstall()
        except BaseException as error:
            self.pilot_recovery_retry=now+2.
            self.log('player reacquisition waiting for restoration: '+repr(error))
            return False
        # No owned native consumer remains installed. Its invocation, receipts
        # and sticky thread can now be discarded together, never individually.
        if self.frame:C.memset(self.frame,0,FRAME_SIZE)
        self.pilot_boundary=PilotBoundary()
        self.block.ship=0;self.rigid=0;self.controller=0;self.ship_class=None
        self.flight_consumer=FlightConsumer(boost=self.flight_consumer.boost)
        self.newtonian_retry=0.;self.newtonian_failure='reacquiring player ship'
        self.newtonian_last_sequence=0;self.newtonian_transition=None
        self.conflicts=0
        self.box.publish((0.,)*6,K.GetTickCount64(),enabled=False)
        if self.active and not self.stopping.is_set():self.box.rearm()
        self.pilot_recoveries+=1
        self.pilot_recovery_retry=0.;self.pilot_recovery=None
        self.log('player lifecycle detached; waiting for fresh player flight and neutral inputs')
        return True

    @get_caller
    @manual_hook('AbsoluteHOTASFlightClock',offset=0x2CC09A0,func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p]),detour_time='after')
    def flight_clock(self,this,out,_result_):
        try:
            if getattr(self,'pilot_recovery',None) is not None:return None
            if not self.active or this!=self.rigid or self.flight_clock.caller_address()!=0x175BFC3:return None
            if K.GetCurrentThreadId()!=self.pilot_boundary.thread:return None
            if C.c_ubyte.from_address(self.block.ship+0x60C8).value!=1:return None
            self.block.pilot_until=K.GetTickCount64()+400
        except BaseException:self.hook_errors+=1;self.block.pilot_until=0
        return None

    @get_caller
    @manual_hook('AbsoluteHOTASScalar',offset=0x141EBC0,func_def=FUNCDEF(C.c_float,[C.c_void_p,C.c_uint32]),detour_time='after')
    def scalar(self,this,action,_result_):
        """Pitch/yaw substitution from the frame's pinned mailbox copy (six_axis_trial, D12)."""
        try:
            f=self.frame
            if not self.active or not f or this!=self.controller or action not in (0x97,0x98):return None
            if self.scalar.caller_address()!={0x97:0x141FFB4,0x98:0x141FFA5}[action]:return None
            now=K.GetTickCount64()
            if C.c_uint32.from_address(f+72).value&STRICT_ROTATION:
                if self.box.agent_stopped() or not self._eligible_native(now):return None
            if C.c_uint32.from_address(f+32).value!=K.GetCurrentThreadId():return None
            if not C.c_uint64.from_address(f+24).value:return None
            index=0 if action==0x97 else 1;bit=1<<index
            native=float(_result_)
            C.c_float.from_address(f+176+4*index).value=native
            if native!=native or abs(native)>.00001:C.c_uint32.from_address(f+56).value=1
            C.c_uint32.from_address(f+60).value|=bit
            deadline,mask,*fields=PAYLOAD.unpack(C.string_at(f+64,64))
            if mask&STRICT_ROTATION:
                tagged=fields[:6]
                if (mask&~(STRICT_ROTATION|7) or any(not math.isfinite(v) or abs(v)>.25 for v in tagged[:3])
                        or any(v!=0. for v in tagged[3:])):return None
            value=fields[index]
            admitted=(C.c_uint64.from_address(f).value==1 and not C.c_uint32.from_address(f+56).value
                      and self.box.native.load(self.box.address+8)==0 and now<min(deadline,C.c_uint64.from_address(f+40).value)
                      and self._eligible_input(now) and value==value and abs(value)<=1. and bool(mask&bit))
            result=value if admitted else native
            C.c_float.from_address(f+184+4*index).value=result
            if admitted:
                C.c_uint32.from_address(f+200).value|=bit;return result
        except BaseException:
            self.hook_errors+=1
            try:self.box.disable()
            except BaseException:pass
        return None

    def _eligible_input(self,now):
        """Physical axes remain available under native planet/combat/power handling."""
        if getattr(self,'pilot_recovery',None) is not None:return False
        ship=self.block.ship;manager=q(_internal.BASE_ADDRESS+0x6e8d708)
        return bool(self.block.pilot_until>now and manager and ship
            and C.c_ubyte.from_address(ship+0x60c8).value==1 and C.c_uint32.from_address(ship+0x674c).value<=3
            and C.c_uint32.from_address(manager+0x875498).value==0
            and C.c_ubyte.from_address(manager+0x57a40d).value<=1)

    def _stall_context(self):
        """What the game is doing when receipts stop in flight (read-only; same fields as telemetry)."""
        manager=q(_internal.BASE_ADDRESS+APP_GLOBAL);ship=self.block.ship;out={}
        if manager:out['combat']=C.c_ubyte.from_address(manager+0x57a40d).value
        if ship:
            out['gate_6720']=C.c_ubyte.from_address(ship+0x6720).value
            out['power_674c']=C.c_uint32.from_address(ship+0x674c).value
        ns=self._translation_status() if hasattr(self,'_translation_status') else {}
        for k in ('reason','native_reason','speed'):
            if ns.get(k) not in (None,''):out['newtonian_'+k]=round(ns[k],1) if isinstance(ns[k],float) else ns[k]
        out['keys_held']=sorted(self.buttons.key_state.held) if getattr(self,'buttons',None) else []
        return out

    def _frontend(self):
        """The frontend flag _eligible_input reads (nonzero while a menu/dialog page is up), or None without a manager."""
        manager=q(_internal.BASE_ADDRESS+APP_GLOBAL)
        return C.c_uint32.from_address(manager+0x875498).value if manager else None

    def _eligible_native(self,now,*,allow_combat_flight=False,allow_low_height=False):
        """Clear-space preflight; research callers remain combat-strict by default.

        Release force/assist caves independently enforce the same known-combat
        opt-in plus their complete ownership, physics and protected-mode gates.
        """
        if getattr(self,'pilot_recovery',None) is not None:return False
        ship=self.block.ship;manager=q(_internal.BASE_ADDRESS+0x6e8d708)
        return bool(self.block.pilot_until>now and manager and ship
            and C.c_ubyte.from_address(ship+0x60c8).value==1 and C.c_uint32.from_address(ship+0x674c).value==0
            and (allow_low_height or C.c_ubyte.from_address(ship+0x6720).value==0) and C.c_uint32.from_address(manager+0x875498).value==0
            and C.c_ubyte.from_address(manager+0x57a40d).value<=(1 if allow_combat_flight else 0))

    # ---------- native patches ----------
    def _install(self,ship):
        import pymem
        from native_roll_patch import allocate_near
        from native_six_axis import SixAxisPatches
        from native_mav_patch import MavPatch
        turn_scale=float(self.store.values['turn-rate-scale'])
        pid=os.getpid()
        if self.pm is None:self.pm=pymem.Pymem(pid)
        pm=self.pm;base=pm.process_base.lpBaseOfDll
        tick=C.cast(K.GetTickCount64,C.c_void_p).value;pilot=C.addressof(self.block)+24
        lease=K.GetTickCount64()+LEASE_MS
        if not self.frame:self.frame=allocate_near(pm.process_handle,base+0x1758007)
        state=bytearray(FRAME_SIZE);struct.pack_into('<Q',state,8,ship);struct.pack_into('<Q',state,40,lease)
        C.memmove(self.frame,bytes(state),FRAME_SIZE)
        self.controller=q(q(ship+0x60c0))
        patches=SixAxisPatches(pm,pid,self.frame,self.box.address,tick,pilot,1.,1.,turn_scale,
                               allow_planetary_input=True,allow_combat_input=True,allow_power_input=True)
        mav=MavPatch(pm,pid,ship,tick,lease,LOGS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-mav.json'),
                     NATIVE_LIMITS[2],{'frame':self.frame,'mailbox':self.box.address,'pilot':pilot,'allow_planetary_input':True,
                                       'allow_combat_input':True,'allow_power_input':True})
        patches.install(self._write_code)
        try:mav.install(self._write_code)
        except BaseException:patches.restore(self._write_code);raise
        self.patches,self.mav,self.installed_ship,self.installed_turn_scale=patches,mav,ship,turn_scale
        C.c_uint64.from_address(self.frame).value=1
        self.log(f'patches installed ship={ship:#x} turn_scale={turn_scale}')

    def _uninstall(self):
        # Shutdown can outlive its bounded worker join; restoration still has
        # exactly one owner. The pilot maintenance drain never takes this lock.
        lock=getattr(self,'uninstall_lock',None)
        if lock is None:lock=self.uninstall_lock=threading.Lock()
        with lock:return self._uninstall_owned()

    def _uninstall_owned(self):
        import additive_rotation
        self.newtonian_refresh_requested=None
        if self.frame:C.c_uint64.from_address(self.frame).value=0
        results={}
        additive_error=None
        try:
            additive_rotation.shutdown(self)
            # A restored owner remains terminal; a later ship/fixture install
            # gets a fresh controller and must centre its inputs again.
            self.additive_rotation=None
        except BaseException as error:
            additive_error=error;results['additive_rotation']=repr(error)
        rotation_error=None
        try:rotation_validation.shutdown(self)
        except BaseException as error:
            rotation_error=error;results['rotation_validation']=repr(error)
        def restored(result):return isinstance(result,dict) and all(value is True for value in result.values())
        aim=getattr(self,'aim_slowdown',None)
        if aim is not None:
            aim.stop()
            try:
                results['aim_slowdown']={'site':aim.restore(self._external_write_code)}
                if restored(results['aim_slowdown']):self.aim_slowdown=None;self.aim_slowdown_failed=False
            except BaseException as error:results['aim_slowdown']=repr(error)
        crash=getattr(self,'crash_damage',None)
        if crash is not None:
            crash.disable()
            try:
                results['crash_damage']=crash.restore(self._external_write_code,self.pm,os.getpid())
                if restored(results['crash_damage']):self.crash_damage=None;self.crash_damage_failed=False
            except BaseException as error:results['crash_damage']=repr(error)
        if getattr(self,'aiming',None) is not None:self.aiming.reset('flight detached')
        if getattr(self,'pip_presentation',None) is not None:self.pip_presentation.clear('flight detached')
        self.aim_pip_visible=None
        # Detach while metadata/publication is excluded. Code restoration may
        # wait for this flight thread, so it MUST run after releasing this lock.
        with self.newtonian_lock:
            bridge=self.newtonian;self.newtonian=None
            if bridge:
                try:bridge.disable()
                except BaseException as error:results['newtonian_disable']=repr(error)
        if bridge:
            try:
                results['newtonian']=bridge.restore(self._write_code)
                if not restored(results['newtonian']):raise RuntimeError(str(results['newtonian']))
            except BaseException as error:
                self.newtonian_failed.append(bridge);results['newtonian']=repr(error)
        failed=[]
        for index,bridge in enumerate(self.newtonian_failed):
            try:
                results['newtonian_failed_'+str(index)]=bridge.restore(self._write_code)
                if not restored(results['newtonian_failed_'+str(index)]):raise RuntimeError(str(results['newtonian_failed_'+str(index)]))
            except BaseException as error:
                failed.append(bridge);results['newtonian_failed_'+str(index)]=repr(error)
        self.newtonian_failed=failed
        geffects=getattr(self,'geffects',None)
        if geffects is not None and geffects.lanes:
            results['g_effects']=geffects.uninstall(self.pm,os.getpid(),self._external_write_code)
        if self.mav:
            try:results['mav']=self.mav.restore(self._write_code)
            except BaseException as e:results['mav']=repr(e)
        if self.patches:results['axes']=self.patches.restore(self._write_code)
        if 'mav' in results and restored(results['mav']):self.mav=None
        if 'axes' in results and restored(results['axes']):self.patches=None
        self.log('patches restored: '+json.dumps(results,default=str))
        if rotation_error is not None:
            raise RuntimeError('T66 restoration failed: '+repr(rotation_error)) from rotation_error
        if additive_error is not None:
            raise RuntimeError('Additive rotation restoration failed: '+repr(additive_error)) from additive_error
        if isinstance(results.get('newtonian'),str):raise RuntimeError('Newtonian restoration failed: '+results['newtonian'])
        if (self.newtonian_failed or self.mav is not None or self.patches is not None
                or getattr(self,'aim_slowdown',None) is not None or getattr(self,'crash_damage',None) is not None):
            raise RuntimeError('Owned patches remain unrestored: '+str(results))
        self.installed_ship=0
        return results

    # ---------- Newtonian translation (T55; no velocity or W writes) ----------
    def _translation_helper(self,work,command):
        """Called synchronously by the qualified final acceleration cave.

        Do not acquire self.lock: code installation waits for the flight thread
        while the publisher owns it. Publication below replaces immutable objects.
        Native code checks identity, mode and the deadline again after this call.
        """
        try:
            from native_newtonian import work_snapshot
            bridge=self.newtonian
            if (not self.active or bridge is None or not self._focused()
                    or getattr(self,'pilot_recovery',None) is not None):
                self._observe_ai_aim(None)
                return 2
            current=work_snapshot(work)
            # T65: the Corvette autopilot (alignment selector 3) also uses the normal caller; let it fly.
            autopilot=bool(bridge.fixture['ship_class']==10 and current.get('alignment')==3)
            if autopilot!=getattr(self,'corvette_autopilot',False):
                self.corvette_autopilot=autopilot;self.log('Corvette autopilot '+('engaged: native flight' if autopilot else 'off: Sultan resumes'))
            native={'ship':current['ship'],'body':current['body'],
                    'class_id':bridge.fixture['ship_class'],
                    'basis':tuple(tuple(current[k][:3]) for k in ('right','up','forward')),
                    'velocity':current['outer_velocity'],
                    'thrust_force':current['thrust_force'],
                    'boost_thrust_force':current.get('boost_thrust_force'),'dt':current['dt_eff']}
            if autopilot:native.update(admitted=False,reason='Corvette autopilot')
            env=getattr(self,'air_env',None)
            if env is not None and native['class_id'] in planetary_flight.AIR_CLASSES:
                planet=planetary_flight.planet_state(current['position'],env[1],env[0]) if env[1] else None
                if planet is not None:
                    native.update(planet=planet,air=env[0],space_thrust_force=self._space_thrust(current),water=env[2],
                                  gear=env[3],clearance=current['clearance'],
                                  # T93: native over-water flag, set earlier in this UpdateControlled (0x1756FB8)
                                  over_water=bool(env[2].ditch and C.c_ubyte.from_address(current['ship']+0x62e0).value))
                elif current['height_mode']:
                    # The cave admitted low height, but there is no planet frame to fly it with.
                    self._observe_ai_aim(None)
                    self.flight_consumer.fallback('planet frame unavailable')
                    return 2
            mode,result=self.flight_consumer.consume(self.translation_command,self.translation_profiles,native,K.GetTickCount64())
            self._observe_ai_aim(current if mode==1 else None)
            velocity=native['velocity']
            if velocity is not None and len(velocity)==3 and all(math.isfinite(v) for v in velocity):
                self.hud_sample=(K.GetTickCount64(),(tuple(velocity),native['basis'],self.flight_consumer.latest.get('target_speed')))
            if mode==1:C.memmove(command,struct.pack('<3f',*result.acceleration_world),12)
            return mode
        except BaseException as error:
            self._observe_ai_aim(None)
            # A bad snapshot/tuning/callback cannot retain the previous command.
            self.hook_errors+=1
            self.flight_consumer.previous=(0.,0.,0.)
            self.flight_consumer.latest={'admitted':True,'reason':'translation error: '+repr(error),'command_state':'expired',
                                         **self.flight_consumer.boost.status()}
            return 0

    def _space_thrust(self,current):
        """ThrustForce without the native Planet-block blend (ship+0x62C0); Sultan's engine loss replaces it."""
        blocks=self.engine_blocks
        if blocks is None or blocks[0]!=current['ship']:return current['thrust_force']
        blend=C.c_float.from_address(current['ship']+0x62c0).value
        return planetary_flight.space_thrust(current['thrust_force'],blocks[1],blocks[2],blend)

    def _update_planetary(self,now):
        """T88: publish planets and settings for the helper, then switch the cave's low-height admission."""
        bridge=self.newtonian;v=self.store.values
        if bridge is not self.planetary_bridge:
            self.planetary_bridge=bridge;self.planetary_on=False;self.planetary_clamp=self.planetary_level=None
            self.planetary_water=self.planetary_gear=None
        on=bool(v['flight-model']=='sultan' and v['atmosphere-model']=='sultan'
                and bridge.fixture['ship_class'] in planetary_flight.AIR_CLASSES)
        if on:
            if now-self.air_planets[0]>=planetary_hud.PLANET_REFRESH_MS/1000.:self.air_planets=(now,self.hud_planet_reader.planets())
            ship=bridge.fixture['ship']
            if self.engine_blocks is None or self.engine_blocks[0]!=ship:
                try:
                    control=int.from_bytes(planetary_hud.process_read(ship+0x66a8,8),'little')
                    # Engine blocks are 0x74 bytes, ThrustForce at +0x64 (effective copy ship+0x6630, +0x6694).
                    planet_tf,=struct.unpack('<f',planetary_hud.process_read(control+0xe8+0x64,4))
                    space_tf,=struct.unpack('<f',planetary_hud.process_read(control+0x15c+0x64,4))
                except OSError:planet_tf=space_tf=float('nan')
                self.engine_blocks=(ship,planet_tf,space_tf)
                self.log(f'planetary engine blocks: Planet ThrustForce {planet_tf:.1f}, Space {space_tf:.1f}')
            self.air_env=(planetary_flight.Air.from_values(v),tuple(self.air_planets[1]),
                          dataclasses.replace(planetary_flight.Water.from_values(v),ditch=self._ditch_active(v)),
                          self._gear(v))
            # The game's integrator clamp (2*BoostMaxSpeed) follows the Planet engine block in air; lift it
            # to the class speed cap so drag, boost and Sultan's own cap decide top speed instead.
            profile=self.translation_profiles[bridge.fixture['ship_class']]
            half=float(profile['speed_cap'])*float(profile['speed_cap_multiplier'])
            if half!=getattr(self,'planetary_clamp',None):bridge.set_clamp(half);self.planetary_clamp=half
            level=bool(v['air-auto-level'])
            if level!=getattr(self,'planetary_level',None):bridge.set_auto_level(level);self.planetary_level=level
        if on!=self.planetary_on:
            bridge.set_planetary(on);self.planetary_on=on
            self.log('planetary flight '+('on: Sultan flies down to landing' if on else 'off: native below the low-height gate'))
        if not on:self.air_env=None
        # T101: gear down, the caves keep the native ground push out of Sultan's commands. So does a falling wreck
        # (user 2026-10-24: the push held crashed wrecks up, some rose over 100 u before the eject), and normal flight
        # with Terrain push off (user 2026-10-25: it kicked the ship up where the ground rose suddenly; D5's safety
        # net, kept until hover mode and crash damage existed).
        condition=getattr(getattr(self,'flight_consumer',None),'condition',None)
        falling=bool(condition is not None and condition.falling);gear_down=bool(on and self.air_env[3] is not None)
        gear=bool(on and (gear_down or falling or not v.get('air-ground-push',False)))
        if gear!=getattr(self,'planetary_gear',None):
            bridge.set_gear(gear);self.planetary_gear=gear
            self.log(('landing gear down: hover mode, no ground push' if gear_down else 'wreck falling: no ground push'
                      if falling else 'terrain push off: no ground push') if gear else 'ground push on')
        # T93: the cave's water switch (no sea-as-ground push over water, a capped push in native frames).
        water=self._ditch_active(v)
        if water!=getattr(self,'planetary_water',None):
            bridge.set_water(water);self.planetary_water=water
            self.log('ditch at sea '+("on: the sea is Sultan's" if water else 'off: native keeps ships above water'))

    def _touchdown(self,now,flying):
        """T101 stage 2: gear down and resting on (or settling onto) the ground, tap the game's Land on the key lane."""
        consumer=getattr(self,'flight_consumer',None)
        if consumer is None:return
        detector=getattr(self,'touchdown',None)
        if detector is None:detector=self.touchdown=manual_landing.TouchdownDetector()
        v=self.store.values;latest=consumer.latest;command=getattr(self,'translation_command',None)
        planetary=latest.get('planetary') if latest.get('admitted') and not latest.get('reason') else None
        vertical=command.vertical if command is not None and command.enabled else 0.
        if detector.step(now,manual_landing.Touchdown.from_values(v),planetary,vertical,latest.get('speed'),
                         bool(flying and manual_landing.ENABLED and v.get('landing-manual') and v['flight-model']=='sultan'
                              and v['atmosphere-model']=='sultan')):
            air=planetary or {}
            self.log(f"touchdown: pressing Land at {air.get('clearance',float('nan')):.1f} u above ground, "
                     f"{latest.get('speed',0.):.1f} u/s, tilt {air.get('tilt',float('nan')):.0f} deg"
                     +('' if v.get('key-output-enabled') else ' (keyboard output is off: the press cannot go out)'))
        self.buttons.synthetic_keys=frozenset((manual_landing.LAND_KEY,)) if detector.holding(now) else frozenset()
        # The game's takeoff hands back with the gear still down; raise it so the ship leaves in normal air flight.
        takeoff=getattr(self,'takeoff_watch',None)
        if takeoff is None:takeoff=self.takeoff_watch=manual_landing.TakeoffWatch()
        if takeoff.step(now,bool(getattr(self,'air_env',None) and self.air_env[3]),flying,
                        (planetary or {}).get('clearance'),detector.pressed_at):
            self.log('takeoff: gear up, normal air flight'+('' if self.buttons.raise_gear() else ' (gear binding still held: release it)'))

    def _gear(self,v):
        """T101: the hover settings while Manual landing is on and the Landing gear binding is down, else None.
        Never while manual landing is off for release (manual_landing.ENABLED, user 2026-10-25)."""
        if not (manual_landing.ENABLED and v.get('landing-manual') and getattr(getattr(self,'buttons',None),'gear',False)):return None
        return planetary_flight.Gear.from_values(v)

    def _eject_hold(self,now,tick,flying):
        """T93 Eject (hold): while Sultan flies the ship, holding the bound button for the hold time asks the
        flight thread to eject the pilot (once per hold). eject_hint is the HUD countdown."""
        v=self.store.values;consumer=getattr(self,'flight_consumer',None)
        block=getattr(self,'block',None)
        if getattr(self,'ship_damage_pending',None)=='ejected' and block is not None and block.pilot_until<=tick:
            self.ship_damage_pending='away'  # out of the ship: the next boarding damages it
        held=bool(flying and getattr(self.buttons,'eject',False) and getattr(self,'newtonian',None) is not None
                  and v.get('flight-model')=='sultan' and consumer is not None and consumer.latest.get('admitted'))
        if not held:
            self.eject_since=self.eject_hint=None;return
        if getattr(self,'eject_since',None) is None:self.eject_since=now;self.eject_fired=False
        left=float(v.get('eject-hold-time',3.))-(now-self.eject_since)
        if left>0.:self.eject_hint=f'EJECT  HOLD {left:.1f}s';return
        self.eject_hint='EJECTING'
        if not self.eject_fired:
            self.eject_fired=True;self.eject_request=tick;self.log('eject requested (held)')

    def _eject(self):
        """The native quick-menu eject block (tools/native_eject.py), built on first use."""
        try:
            if getattr(self,'ejector',None) is None:
                from native_eject import Ejector
                ejector=Ejector(int(_internal.BASE_ADDRESS));ejector.install(self.pm.process_handle);self.ejector=ejector
                self.log(f'eject stub installed at {ejector.stub:#x}')
            condition=self.flight_consumer.condition
            rows=None
            if condition.engines_out and condition.cause in WRECK_CAUSES:
                import ship_salvage  # the ship's own matrix, read while it is still ours
                try:rows=ship_salvage.ship_rows(planetary_hud.process_read,self.block.ship)
                except OSError:rows=None
            result=self.ejector.eject()
            self.log('eject: '+('native eject called' if result else 'no player ship, nothing done'))
            if result and condition.engines_out:
                # Leaving a ditched or wrecked ship: its essentials are damaged when you next board it (always
                # landed, e.g. after Summon Starship), so the ship cannot just fly away (user 2026-10-20).
                import ship_damage
                try:self.ship_damage_index=ship_damage.primary_index(planetary_hud.process_read,int(_internal.BASE_ADDRESS))
                except OSError:self.ship_damage_index=None
                self.ship_damage_pending='ejected' if self.ship_damage_index is not None else None
                if condition.cause in WRECK_CAUSES and self.ship_damage_index is not None:self._land_wreck(rows)
                self.alarm_eject=True;condition.repair()
        except BaseException as error:
            self.hook_errors+=1;self.log('eject failed: '+repr(error))

    def _land_wreck(self,rows):
        """Ditch or crash eject (user 2026-10-21): an ejected ship stays in flight mode, so boarding it again needs no
        launch. Damage the essentials now and have the game respawn it landed where it lies (mod/ship_salvage.py).
        Without it the old path stays: damage on boarding."""
        self.salvage=None
        try:
            import ship_salvage
            wreck=ship_salvage.Wreck(int(_internal.BASE_ADDRESS),self.ship_damage_index,rows)
            wreck.place(time.monotonic())
            self.salvage=wreck;self.salvage_thread=K.GetCurrentThreadId();self.ship_damage_pending=None
            self.log('wreck: damaged '+(', '.join(wreck.damaged) or 'nothing')+'; landed respawn requested where it lies')
        except BaseException as error:
            self.log('wreck: landed respawn unavailable: '+repr(error))

    def _step_salvage(self):
        """Game thread (ship Update after-hook): watch the game take the landed-respawn request."""
        wreck=self.salvage
        try:result=wreck.step(time.monotonic())
        except BaseException as error:
            self.hook_errors+=1;self.salvage=None;self.log('wreck: landed respawn failed: '+repr(error));return
        if result=='landed':
            self.salvage=None;self.log('wreck: the game respawned it landed (crash smoke re-evaluated)')
        elif result=='failed':
            self.salvage=None;self.log('wreck: the game did not take the landed respawn within 5 s')

    def _damage_abandoned_ship(self):
        """T93: back aboard the ditched ship: damage the launch thruster, pulse engine and shields in the live
        inventory (saved with the game, repaired the vanilla way)."""
        self.ship_damage_pending=None
        try:
            import ship_damage
            done=ship_damage.damage_essentials(planetary_hud.process_read,ship_damage.memory_write,int(_internal.BASE_ADDRESS),
                                               ship=getattr(self,'ship_damage_index',None))
            self.log('ship boarded after a ditch eject: damaged '+(', '.join(done) if done else 'nothing (another ship, or essentials not found)'))
        except BaseException as error:
            self.hook_errors+=1;self.log('ship damage failed: '+repr(error))

    def _step_crash_damage(self,tick,piloting,recovering):
        """T94 crash damage: install once when enabled, publish settings and player ship, drain hit records and
        run the disabling crash sequence (blackout, eject, essentials damaged)."""
        lock=getattr(self,'uninstall_lock',None)
        if lock is None:lock=self.uninstall_lock=threading.Lock()
        with lock:
            patch=getattr(self,'crash_damage',None)
            if not self.active or self.stopping.is_set():
                if patch is not None:patch.disable()
                return
            self._step_crash_damage_owned(tick,piloting,recovering)

    def _step_crash_damage_owned(self,tick,piloting,recovering):
        v=self.store.values
        patch=getattr(self,'crash_damage',None)
        wanted=bool(v.get('flight-controls-enabled',False) and v.get('flight-model')=='sultan'
                    and v.get('crash-damage',False) and piloting and not recovering
                    and self.patches is not None and self.installed_ship==self.block.ship and self.block.ship)
        ditching=self._crash_ditching()
        if not wanted:
            if patch is not None:
                # A ditch keeps the caves on in their do-nothing mode: native scrapes on the sea floor would
                # otherwise empty the hull while the pilot purges (2026-10-20 flight).
                if ditching and self.block.ship and self.active and not self.stopping.is_set():
                    patch.publish(crash_damage.Settings.from_values(v),self.block.ship,submerged=True)
                else:patch.disable()
                for hit in patch.hits():self._handle_crash_hit(hit,tick)
            return
        if getattr(self,'crash_damage_failed',False):return
        if patch is None:
            from native_crash_damage import CrashDamagePatch
            from native_eject import allocate_after_image
            base=int(_internal.BASE_ADDRESS)
            allocation=getattr(sys,'_absolute_hotas_crash_allocation',0)
            if not allocation:
                allocation=allocate_after_image(self.pm.process_handle,base)
                sys._absolute_hotas_crash_allocation=allocation
            try:
                patch=CrashDamagePatch(base,allocation)
                self.crash_damage=patch
                patch.install(self._external_write_code,self.pm,os.getpid())
                self.log(f'crash damage caves installed at {allocation:#x}')
            except BaseException as error:
                self.crash_damage_failed=True
                self.log('crash damage unavailable: '+repr(error))
                if patch is not None:
                    patch.disable()
                    try:
                        restored=patch.restore(self._external_write_code,self.pm,os.getpid())
                        if isinstance(restored,dict) and all(val is True for val in restored.values()):
                            self.crash_damage=None
                    except BaseException as restore_error:
                        self.active=False;self.stopping.set();self.box.disable()
                        self.log('crash damage restoration failed; runtime stopped: '+repr(restore_error))
                        raise RuntimeError('Crash damage restoration failed') from restore_error
                return
        if not self.active or self.stopping.is_set():
            patch.disable();return
        patch.publish(crash_damage.Settings.from_values(v),self.block.ship,submerged=ditching)
        for hit in patch.hits():self._handle_crash_hit(hit,tick)

    def _crash_ditching(self):
        """The ditch at sea is running (user 2026-10-20: crash damage inactive meanwhile): the ship is in the water
        with the ditch on, or its engines are out from a ditch (purge, wrecked, until the eject)."""
        consumer=getattr(self,'flight_consumer',None);condition=getattr(consumer,'condition',None)
        if condition is None:return False
        if condition.engines_out and condition.cause=='ditch':return True
        planetary=(getattr(consumer,'latest',None) or {}).get('planetary') or {}
        return bool(getattr(self,'ditch_was_active',False) and (planetary.get('submerged') or 0.)>0.)

    def _handle_crash_hit(self,hit,tick):
        if hit.get('submerged'):return  # a ditch is running: the hit did nothing
        consumer=getattr(self,'flight_consumer',None);condition=getattr(consumer,'condition',None)
        if condition is not None and condition.falling:
            self.log(f"crash hit during the wreck fall: {hit['kind']} impact={hit['impact']:.1f} u/s (ignored)");return
        self.crash_last_hit=hit
        self.log(f"crash hit: {hit['kind']} impact={hit['impact']:.1f} u/s (into={hit['into_surface']:.1f}, slide={hit['sliding']:.1f}) "
                 f"mult={hit['multiplier']:.2f} hull={hit['hull_before']}->{hit['hull_after']} flags={hit['flags']:#x}")
        if self._crash_ditching():  # it landed as the ditch began: the purge decides, not a crash
            self.log('crash hit during a ditch: no crash sequence');return
        self._alarm_crash(hit,tick)  # T95
        if not crash_damage.is_disabling(hit):return
        geffects=getattr(self,'geffects',None)
        if condition is not None:
            if self.block.ship:condition.select_ship(self.block.ship)
            condition.knock_out('collision',impact=hit['impact'])
            condition.wreck('collision')
            # User 2026-10-24: the wreck falls to a resting place before the eject (_step_wreck_fall). User
            # 2026-10-25: the pilot stays conscious through it, dazed and red, and passes out at the clamp.
            condition.start_fall(tick);self.wreck_fall_from=self._wreck_fall_sample()
            if geffects is not None:geffects.strain.daze(ship_condition.FALL_MAX_MS/1000.)
            self.log(f"disabling crash at {hit['impact']:.1f} u/s (hull {hit['hull_before']}->{hit['hull_after']}, capped={hit['capped']}): "
                     f"daze and wreck; the wreck falls (speed {self.wreck_fall_from[0]}, altitude {self.wreck_fall_from[1]})")
            return
        if geffects is not None:geffects.strain.knock_out(CRASH_BLACKOUT_HOLD)
        self.eject_request=tick
        self.log(f"disabling crash at {hit['impact']:.1f} u/s (hull {hit['hull_before']}->{hit['hull_after']}, capped={hit['capped']}): "
                 'blackout, eject and wreck')

    def _wreck_fall_sample(self):
        """(speed, altitude, clearance) of the wreck now, rounded for the log; None where unknown."""
        latest=getattr(getattr(self,'flight_consumer',None),'latest',None) or {};planetary=latest.get('planetary') or {}
        def r(x):return None if not isinstance(x,(int,float)) or not math.isfinite(x) else round(x,1)
        return r(latest.get('speed')),r(planetary.get('altitude')),r(planetary.get('clearance'))

    def _step_wreck_fall(self,tick):
        """Worker: eject once the falling wreck has come to rest (or the fall timed out), then the landed respawn
        puts it down where it lies. The dazed pilot passes out here (the jump is never seen) and wakes
        CRASH_BLACKOUT_HOLD after."""
        condition=getattr(getattr(self,'flight_consumer',None),'condition',None)
        if condition is None or not condition.falling:return
        speed,altitude,clearance=self._wreck_fall_sample()
        since=condition.fall_since_ms;reason=condition.fall_done(tick,speed)
        if reason is None:return
        geffects=getattr(self,'geffects',None)
        if geffects is not None:geffects.strain.knock_out(CRASH_BLACKOUT_HOLD)
        self.eject_request=tick
        start=getattr(self,'wreck_fall_from',(None,None,None))
        fell=None if start[1] is None or altitude is None else round(start[1]-altitude,1)
        self.log(f'wreck fall over ({reason}) after {(tick-since)/1000.:.1f} s: speed {speed}, altitude {altitude}, '
                 f'clearance {clearance}, fell {fell} u; eject')

    def _crash_damage_status(self):
        patch=getattr(self,'crash_damage',None)
        state=('failed' if getattr(self,'crash_damage_failed',False)
               else 'active' if (patch is not None and patch.status().get('enabled'))
               else 'installed' if patch is not None else 'off')
        out={'state':state,'failed':getattr(self,'crash_damage_failed',False),'last_hit':getattr(self,'crash_last_hit',None)}
        if patch is not None:out.update(patch.status())
        else:out.update(installed=[],enabled=False,collisions=0,hits=0)
        return out

    def _step_purge(self,condition,dt,tick,live):
        """T93 thruster purge on the worker thread: lever and depth in, relight or wreck out. Frozen while
        not piloting, in a menu or blacked out."""
        purge=condition.purge
        if purge is None or condition.condition!='engines_out' or not live:return []
        altitude=(self.flight_consumer.latest.get('planetary') or {}).get('altitude')
        depth=-altitude if isinstance(altitude,(int,float)) and math.isfinite(altitude) else 0.
        events=purge.step(dt,getattr(self,'purge_inputs',{}),depth)
        condition.hull=purge.hull
        for event in events:
            if event=='begin':self.log(f'thruster purge begins (hull {100*purge.hull:.0f}%)')
            elif event=='miss':self.log(f'thruster purge: miss in {purge.stage.name} (hull {100*purge.hull:.0f}%)')
            elif event=='relit':
                condition.relight(tick);condition.blast_until_ms=tick+planetary_flight.BLAST_MS  # boost blasts the ship out
                self.log(f'thruster purge done: thrusters relit, boost blast (hull {100*purge.hull:.0f}%, {purge.misses} misses)')
            elif event=='wrecked':
                # User 2026-10-21: hull failure ejects the pilot like a disabling crash; the wreck is put down landed.
                condition.wreck('ditch')
                geffects=getattr(self,'geffects',None)
                if geffects is not None:geffects.strain.knock_out(CRASH_BLACKOUT_HOLD)
                self.eject_request=tick
                self.log('thruster purge lost: hull failed, ship wrecked: blackout and eject')
        return events

    # ---------- T95 ship alarms: voice, sirens, engine sound, red cockpit lights (mod/ship_alarms.py) ----------
    def _alarms_step(self,condition,purge,events,live,tick):
        """Worker: turn the ditch's moments into cues, hold the light globals, and post loop stops ourselves if the
        flight thread stopped draining (out of the ship, game closing)."""
        alarms=getattr(self,'alarms',None);v=self.store.values;now=tick/1000.
        if alarms is None:return
        alarms.configure(v.get('alarm-sounds','voice'),v.get('alarm-lights',True))
        if alarms.episode=='ditch':
            self._purge_voice(alarms)
            if purge is not None and live:alarms.moment('wake',now)
            for event in events:  # begin/stage start a stage's countdown: its line is the new stage's (T99)
                alarms.moment('boost' if event=='stage' and purge.stage.input=='boost' else event,now,
                              stage=purge.stage.voice if event in ('begin','stage') else None)
            if purge is not None and not purge.result:alarms.hull(purge.hull,now)
            if not condition.engines_out and alarms.episode=='ditch':  # ejected (engine stays off), setting off, another ship
                alarms.end(now,engine=not self.alarm_eject);self.alarm_eject=False
            alarms.pause(alarms.episode=='ditch' and alarms.woke and not live and not condition.wrecked,now)
        alarms.tick(now)
        if alarms.speech:self._purge_speech(alarms,v)
        native=self._alarm_native() if alarms.active or alarms.commands else self.native_alarms
        if native is not None:
            try:native.set_lights(alarms.light)
            except BaseException as error:
                self.hook_errors+=1;self.log('ship alarms off: lights failed: '+repr(error))
                self.native_alarms=None;self.native_alarms_failed=True;native.saved=None
        if alarms.commands and tick-self.alarms_drained>500:self._alarm_stops_only(native)

    def _alarm_native(self):
        if self.native_alarms is None and not self.native_alarms_failed:
            try:
                from native_alarms import NativeAlarms
                self.native_alarms=NativeAlarms(int(_internal.BASE_ADDRESS))
            except BaseException as error:
                self.native_alarms_failed=True;self.log('ship alarms unavailable: '+repr(error))
        return self.native_alarms

    def _purge_voice(self,alarms):
        """T99: load the rendered purge lines once a ditch could speak them; any failure keeps the game's lines."""
        if self.voice_player is not None or self.voice_player_failed or alarms.sounds!='voice':return
        try:
            from voice_player import VoicePlayer
            self.voice_player=VoicePlayer();alarms.use_voice(self.voice_player.lengths)
        except BaseException as error:
            self.voice_player_failed=True;alarms.use_voice(None);self.log('purge voice unavailable: '+repr(error))

    def _purge_speech(self,alarms,v):
        """Worker: play or cut our lines at once (countdowns are timed to the purge); a failure falls back to the
        game's lines."""
        while alarms.speech:
            command=alarms.speech.popleft();player=self.voice_player
            if player is None:continue
            try:
                if command[0]=='say':player.say(command[1],command[2],v.get('alarm-voice-volume',.7))
                else:player.hush()
            except BaseException as error:
                self.hook_errors+=1;self.log('purge voice failed: '+repr(error))
                self.voice_player=None;self.voice_player_failed=True;alarms.use_voice(None)

    def _alarm_stops_only(self,native):
        """Nobody is flying to carry the queue out: stop any loop from here (Wwise queues the call), drop the rest."""
        commands=self.alarms.commands
        while commands:
            command=commands.popleft()
            if native is not None and command[0]=='post' and command[3].endswith('_Stop'):
                try:native.post(command[1],command[2])
                except BaseException as error:self.hook_errors+=1;self.log('alarm stop failed: '+repr(error))

    def _alarms_frame(self,ship):
        """Flight thread, after the ship Update: post the queued alarm sounds, switch the engine sound, keep the
        cockpit alarm lamps on (native clears the flag inside Update)."""
        alarms=getattr(self,'alarms',None)
        if alarms is None:return
        self.alarms_drained=K.GetTickCount64()
        native=self.native_alarms;commands=alarms.commands
        if native is None:
            if commands and self.native_alarms_failed:commands.clear()
            return
        try:
            while commands:
                command=commands.popleft()
                if command[0]=='post':native.post(command[1],command[2],ship)
                elif command[0]=='engine':native.engine(ship,command[1])
            native.hold_flag(ship)
        except BaseException as error:
            self.hook_errors+=1;self.log('ship alarm failed: '+repr(error))

    def _alarm_crash(self,hit,tick):
        """T94 crash hits: a hull hit gets the computer's damage call and a burst of red; a disabling one the
        critical sting and our 'ship disabled, ejection sequence initiated' line as the wreck tumbles."""
        alarms=getattr(self,'alarms',None)
        if alarms is None:return
        if crash_damage.is_disabling(hit):
            self._purge_voice(alarms)  # the rendered 'ship disabled' line (user 2026-10-25); the game's line if unavailable
            alarms.moment('crash_disabled',tick/1000.)
        elif hit.get('hull_first'):alarms.moment('crash_hit',tick/1000.)

    def _alarms_shutdown(self):
        """Stop any siren and give the lights back. The engine sound is the game's again at its next switch."""
        try:
            self.alarms.end(engine=False);self._alarm_stops_only(self.native_alarms)
            if self.native_alarms is not None:self.native_alarms.release()
            if getattr(self,'voice_player',None) is not None:self.voice_player.hush()
        except BaseException as error:self.log('ship alarms shutdown failed: '+repr(error))

    def _ditch_active(self,v):
        """Ditch at sea on, or turned off while the ship is still wet: the native sea-as-ground push would
        fling a submerged ship (2026-10-19 flight), so the water stays ours until the ship is out of it."""
        consumer=self.flight_consumer;planetary=consumer.latest.get('planetary') or {}
        wet=consumer.condition.engines_out or planetary.get('submerged',0.)>0.
        active=(bool(v['flight-model']=='sultan' and v['atmosphere-model']=='sultan' and v['air-ditch'])
                or (wet and getattr(self,'ditch_was_active',False)))
        self.ditch_was_active=active
        return active

    def _observe_ai_aim(self,work):
        """Aim observation cannot throw into the translation command or its fallback."""
        aim=getattr(self,'ai_aim',None)
        if aim is None:return
        try:
            if work is None:aim.reset()
            else:aim.observe(work,K.GetTickCount64(),self.store.values)
        except Exception:
            try:aim.reset()
            except Exception:pass

    def _aim_deflection(self):
        # Escape follows calibrated physical travel, before sensitivity/ramp or G effects.
        units=[self.table.unit(self.store.values.get('bind-'+axis+'-axis')) for axis in ('yaw','pitch')]
        if any(unit is not None and not math.isfinite(unit) for unit in units):return 1.
        return min(1.,math.hypot(*(0. if unit is None else 2.*unit-1. for unit in units)))

    def _aiming_tick(self,tick,deflection,eligible):
        # Shutdown can outlive its worker join. Serialize creation and publication
        # with teardown so a late constructor cannot install an unowned patch.
        lock=getattr(self,'uninstall_lock',None)
        if lock is None:lock=self.uninstall_lock=threading.Lock()
        with lock:
            if not self.active or self.stopping.is_set():
                bridge=getattr(self,'aim_slowdown',None)
                if bridge is not None:bridge.stop()
                return
            self._aiming_tick_owned(tick,deflection,eligible)

    def _aiming_tick_owned(self,tick,deflection,eligible):
        aiming=getattr(self,'aiming',None)
        if aiming is None:return
        bridge=getattr(self,'aim_slowdown',None)
        enabled=self.store.values.get('aim-slowdown-enabled',False)
        if not enabled or not eligible or not self._aiming_eligible() or self.patches is None:
            if bridge is not None:bridge.disable()
            return
        solution=aiming.sample(ship=self.block.ship,eligible=True,mode=self.store.values.get('pip-mode','lead'))
        visible=getattr(self,'aim_pip_visible',None)
        if (solution is None or visible!=(solution['ship'],solution['target'],solution['tick'])):
            if bridge is not None:bridge.disable()
            return
        if getattr(self,'aim_slowdown_failed',False):return
        if bridge is None:
            from native_aim_slowdown import AimSlowdownPatch
            bridge=AimSlowdownPatch(self.pm,os.getpid(),int(_internal.BASE_ADDRESS),self.frame,
                                    self.box.address,C.cast(K.GetTickCount64,C.c_void_p).value,
                                    C.addressof(self.block)+24)
            self.aim_slowdown=bridge  # retain ownership even if installation fails
            try:
                bridge.install(self._external_write_code)
                self.log('owned pitch/yaw slowdown installed')
            except BaseException as error:
                bridge.stop();self.aim_slowdown_failed=True
                self.log('aim slowdown unavailable: '+repr(error))
                try:
                    bridge.restore(self._external_write_code)
                    self.aim_slowdown=None
                except BaseException as restore_error:
                    self.active=False;self.stopping.set();self.box.disable()
                    self.log('aim slowdown restoration failed; runtime stopped: '+repr(restore_error))
                    raise RuntimeError('Aim slowdown restoration failed') from restore_error
                return
        if not self.active or self.stopping.is_set():
            bridge.stop();return
        gain=slowdown_gain(solution,self.store.values,deflection)
        bridge.publish(gain,tick,enabled=True,ttl_ms=50)

    def _aiming_status(self):
        aiming=getattr(self,'aiming',None)
        presenter=getattr(self,'pip_presentation',None)
        bridge=getattr(self,'aim_slowdown',None)
        return {'solution':aiming.status() if aiming is not None else {'state':'not initialized'},
                'pip':presenter.status() if presenter is not None else {'state':'not initialized'},
                'slowdown':bridge.snapshot() if bridge is not None else {'state':'off'},
                'slowdown_failed':getattr(self,'aim_slowdown_failed',False)}

    def _ensure_newtonian(self,now,piloting):
        if not piloting or self.patches is None or now<self.newtonian_retry:return
        if self.newtonian is not None:
            # The worker only requests work. Metadata may change only after the
            # pinned flight thread returns from Controlled, never under this lock.
            if self.store.values['flight-model']!='sultan':return
            if getattr(self,'newtonian_refresh_requested',None) is not None:return
            self.newtonian_retry=now+2.
            if self.newtonian.fixture['ship']!=self.block.ship:return
            if not self._eligible_native(K.GetTickCount64(),allow_combat_flight=True,allow_low_height=getattr(self,'planetary_on',False)):return
            if self.newtonian.sample().get('reason_name')!='identity_changed':return
            self.newtonian_refresh_requested=(self.newtonian,self.block.ship)
            return
        if self.store.values['flight-model']!='sultan':return
        self.newtonian_retry=now+2.
        bridge=None
        try:
            from native_newtonian import Bridge,fixture_from_ship
            fixture=fixture_from_ship(self.pm,self.pm.process_base.lpBaseOfDll,self.block.ship)
            bridge=Bridge(self.pm,os.getpid(),self.pm.process_base.lpBaseOfDll,fixture,
                          C.cast(K.GetTickCount64,C.c_void_p).value,
                          C.cast(self.translation_callback,C.c_void_p).value,allow_combat_flight=True,
                          allow_planetary_flight=True)
            metadata=bridge.prepare()
            (LOGS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-newtonian-sites.json')).write_text(
                json.dumps(metadata,indent=1),encoding='utf-8')
            bridge.install(self._write_code)
            self.newtonian=bridge;self.newtonian_failure=''
            self.newtonian_last_sequence=0;self.newtonian_transition=None
            self.newtonian_receipt_path=LOGS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-newtonian-transitions.jsonl')
            self.log('Newtonian seam installed; class '+str(fixture['ship_class']))
        except BaseException as error:
            if bridge is not None and bridge.data:
                # Caves must stay allocated until exit. Do not leak another full
                # native ring on each retry, or lose a failed restoration handle.
                self.newtonian_failed.append(bridge)
                self.newtonian_retry=float('inf')
            reason=str(error)
            if self.newtonian_failure!=reason:self.log('Newtonian native fallback: '+reason)
            self.newtonian_failure=reason

    def _refresh_newtonian_fixture(self,ship):
        """Requalify the same player's physics on its idle native flight boundary."""
        request=getattr(self,'newtonian_refresh_requested',None)
        if request is None:return
        self.newtonian_refresh_requested=None
        bridge,expected_ship=request
        # This lock excludes the worker's lease/enable writes while gates and
        # metadata are verified. It never waits for code writes or self.lock.
        with self.newtonian_lock:
            if (not self.active or self.stopping.is_set() or self.newtonian is not bridge
                or ship!=expected_ship or self.block.ship!=ship or bridge.fixture['ship']!=ship
                or self.store.values['flight-model']!='sultan'
                or not self._eligible_native(K.GetTickCount64(),allow_combat_flight=True,allow_low_height=getattr(self,'planetary_on',False))):return
            try:
                from native_newtonian import fixture_from_ship
                # Re-run positive local-player, class, callback, body and zero-gravity
                # qualification. An arbitrary new pointer is never enough to resume.
                fixture=fixture_from_ship(self.pm,self.pm.process_base.lpBaseOfDll,ship)
                changed=[name for name in fixture if fixture.get(name)!=bridge.fixture.get(name)]
                bridge.refresh_fixture(fixture)
                # A requalified same ship keeps its boost heat and ship condition (T93: still ditched).
                self.flight_consumer=FlightConsumer(boost=self.flight_consumer.boost,condition=self.flight_consumer.condition);self.translation_command=None
                self.newtonian_refresh_count+=1;self.newtonian_failure='';self.newtonian_transition=None
                self.log('Newtonian physics requalified; class '+str(fixture['ship_class'])+'; changed '+','.join(changed))
            except BaseException as error:
                reason='physics requalification: '+str(error)
                if self.newtonian_failure!=reason:self.log('Newtonian native fallback: '+reason)
                self.newtonian_failure=reason

    def _translation_status(self):
        status=dict(self.flight_consumer.latest)
        if self.newtonian is not None:
            native=self.newtonian.sample()
            status['native_reason']=native.get('reason_name','awaiting invocation')
            status['native_invocation']=native.get('invocation')
            status['native_tick']=native.get('tick')
            status['avoidance']=native.get('terms',{}).get('avoidance')
            status['auto_bank_suppressed']=native.get('auto_bank_suppressed',False)
            if native.get('native_fallback',True):
                velocity=native.get('outer_velocity')
                valid=bool(velocity is not None and len(velocity)==3 and all(math.isfinite(v) for v in velocity))
                forward=native.get('forward')
                forward_valid=bool(forward is not None and len(forward)>=3 and all(math.isfinite(v) for v in forward[:3]))
                status.update(admitted=False,reason=status['native_reason'],command_state='native fallback',
                              generation=None,age_ms=None,acceleration_body=(0.,0.,0.),acceleration_world=(0.,0.,0.),
                              cap_active=False,target_speed=None,parking=False,boost_active=False,boost_multiplier=1.,bleeding_overspeed=False,
                              speed=math.hypot(*velocity) if valid else None,
                              forward_speed=sum(a*b for a,b in zip(velocity,forward[:3])) if valid and forward_valid else None)
            elif native.get('command_expired'):
                status.update(admitted=True,reason='expired',command_state='expired',
                              parking=False,cap_active=False,target_speed=None,boost_active=False,boost_multiplier=1.,bleeding_overspeed=False,
                              acceleration_body=(0.,0.,0.),acceleration_world=(0.,0.,0.))
            if native.get('tick') is not None and K.GetTickCount64()-native['tick']>=100:
                status.update(admitted=False,reason='native receipts stale',command_state='no current flight receipt',
                              generation=None,age_ms=None,speed=None,forward_speed=None,target_speed=None,
                              acceleration_body=(0.,0.,0.),acceleration_world=(0.,0.,0.),cap_active=False,parking=False)
        if self.newtonian is None:
            status.update(admitted=False,reason=self.newtonian_failure)
        elif self.store.values['flight-model']!='sultan':
            status.update(admitted=False,reason='native flight model')
        if not status.get('admitted'):
            status.update(boost_active=False,boost_multiplier=1.,bleeding_overspeed=False)
        status['requalification_count']=getattr(self,'newtonian_refresh_count',0)
        status['requalification_reason']=self.newtonian_failure
        return status

    def _drain_translation_receipts(self,now):
        """Export state changes from the bounded native ring, including a worker stall.

        Reading only the latest receipt after a stall would hide its expired frames.
        Generation/state marked *_now describe the resumed Python publisher, whereas
        each native tick and reason belongs to that completed flight invocation.
        """
        if self.newtonian is None or self.newtonian_receipt_path is None or now-self.newtonian_last_drain<.1:return
        self.newtonian_last_drain=now
        rows,sequence,dropped=self.newtonian.read_records(self.newtonian_last_sequence)
        self.newtonian_last_sequence=sequence
        events=[]
        if dropped:events.append({'ring_records_dropped':dropped,'at':now})
        for row in rows:
            transition=(row['reason'],row['applied'])
            if transition==self.newtonian_transition:continue
            self.newtonian_transition=transition
            events.append({k:row.get(k) for k in ('invocation','tick','reason_name','applied','avoidance_flag',
                          'final_before','final_after','outer_velocity','outer_velocity_after_integrator')})
            events[-1].update(publisher_generation_now=getattr(self.translation_command,'generation',None),
                              consumer_state_now=self.flight_consumer.latest.get('command_state'))
        if events and self.newtonian_receipt_path:
            with self.newtonian_receipt_path.open('a',encoding='utf-8') as output:
                for event in events:output.write(json.dumps(event)+'\n')

    # ---------- code writes on the flight thread (no thread suspension in-process) ----------
    def _external_write_code(self,pm,pid,site,expected,replacement):
        """Swap a call on another game thread. Python-side thread suspension can deadlock on the interpreter
        lock, so the bus DLL does the suspend-write-resume natively (T100, code_patch). A DLL built before
        that export falls back to the dev venv helper (tools/patch_call.py, the T77 live-proof method)."""
        if getattr(self,'code_patcher',False) is False:
            self.code_patcher=code_patch.load(runtime_config.load(ROOT)['bus'])
            if self.code_patcher is None:self.log('bus DLL has no native code patch; using the dev venv helper')
        if self.code_patcher is not None and pid==os.getpid():return self.code_patcher(site,expected,replacement)
        import subprocess
        python=ROOT/'.venv'/'Scripts'/'python.exe'
        result=subprocess.run([str(python),'-I',str(ROOT/'tools'/'patch_call.py'),str(pid),f'{site:x}',expected.hex(),replacement.hex()],
                              capture_output=True,text=True,timeout=20,creationflags=0x08000000)  # CREATE_NO_WINDOW
        line=(result.stdout.strip().splitlines() or [''])[-1]
        if result.returncode!=0:raise RuntimeError(f'patch_call {site:#x} failed: {line or result.stderr.strip()[-300:]}')
        return line

    def _write_code(self,pm,pid,site,expected,replacement):
        """Writer for the patch classes. Every patched site is inside UpdateControlled, so the
        flight thread applies the write from the outer Update after-detour, after Controlled returned.
        When that drain is idle (paused, on foot or unloading), the existing external helper
        owns suspension. Suspending another Python thread in-process can deadlock on its GIL."""
        item={'args':(pm,pid,site,expected,replacement),'done':threading.Event(),'error':None}
        with self.code_lock:self.code_writes.append(item)
        if not item['done'].wait(CODE_WRITE_SECONDS):
            with self.code_lock:
                queued=any(i is item for i in self.code_writes)
                if queued:self.code_writes.remove(item)
            if queued:
                if getattr(self,'pilot_recovery',None) is not None:
                    raise RuntimeError('Player recovery boundary unavailable; native delivery remains disabled')
                self.log(f'code write at {site:#x}: flight thread idle, using external patch helper')
                return self._external_write_code(pm,pid,site,expected,replacement)
            item['done'].wait()
        if item['error'] is not None:raise item['error']

    def _drain_code_writes(self):
        """Flight thread, top of pilot(): apply queued code writes."""
        if not self.code_writes:return
        from native_roll_patch import write_code_in_thread
        while True:
            with self.code_lock:
                if not self.code_writes:return
                item=self.code_writes.pop(0)
            try:write_code_in_thread(*item['args'])
            except BaseException as e:item['error']=e
            finally:item['done'].set()

    def _renew_lease(self,now,ttl_ms=None):
        if ttl_ms is not None and (type(ttl_ms) is not int or not 1<=ttl_ms<=100):
            raise ValueError('Agent native lease must be <=100ms')
        until=now+(LEASE_MS if ttl_ms is None else ttl_ms)
        C.c_uint64.from_address(self.frame+40).value=until
        C.c_uint64.from_address(self.mav.block+MAV_FORCE+32).value=until

    def _receipt_raw(self):
        """(sequence, 224-byte frame copy) from the seqlocked native receipt, or None."""
        f=self.frame;before=q(f+240)
        if not before or before&1:return None
        raw=C.string_at(f+256,224)
        if q(f+240)!=before:return None
        return before,raw

    def _receipt(self):
        """(sequence, conflict, native throttle u) from the seqlocked native receipt, or None."""
        receipt=self._receipt_raw()
        if receipt is None:return None
        seq,raw=receipt
        return seq,struct.unpack_from('<I',raw,56)[0],struct.unpack_from('<f',raw,144)[0]

    # ---------- flight telemetry (read-only; flight_telemetry.py) ----------
    def _telemetry_writer(self):
        """runtime.json "telemetry_hz" (default 50; 0 turns recording off)."""
        try:hz=float(runtime_config.load(ROOT).get('telemetry_hz',50))
        except (OSError,ValueError):hz=50.
        if not hz>0:return None
        return TelemetryWriter(LOGS/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-telemetry.csv'),hz)

    def _record(self,reason,values):
        v=self.store.values;ship=self.block.ship
        fields={name:struct.unpack(fmt,C.string_at(ship+offset,struct.calcsize(fmt)))[0] for name,offset,fmt in SHIP_FIELDS}
        receipt=self._receipt_raw() if self.patches is not None else None
        mav=self.mav.sample() if self.mav is not None else None
        state={'piloting':1,'reason':reason or 'armed','style':v['throttle-style'],'hosas_mode':v['hosas-stick-mode'],
               'lever_unit':self.table.unit(v['bind-throttle-axis']),'throttle_armed':int(self.control.armed['throttle']),
               'throttle_reason':self.control.reasons.get('throttle',''),'rate_target':self.control.throttle.rate.target,
               **{f'out_{c}':x for c,x in zip(MAILBOX_ORDER,values)}}
        if getattr(self,'flight_consumer',None) is not None:
            ns=self._translation_status()
            state.update(flight_model=v['flight-model'],newtonian_admitted=int(ns.get('admitted',False)),
                         newtonian_reason=ns.get('reason',''),command_state=ns.get('command_state',''),
                         command_generation=ns.get('generation',''),command_age_ms=ns.get('age_ms',''),
                         newtonian_native_reason=ns.get('native_reason',''),newtonian_invocation=ns.get('native_invocation',''),
                         newtonian_speed=ns.get('speed',''),newtonian_forward_speed=ns.get('forward_speed',''),
                         newtonian_target_speed=ns.get('target_speed',''),parking_brake=int(ns.get('parking',False)),
                         speed_cap_active=int(ns.get('cap_active',False)),
                         bleeding_overspeed=int(ns.get('bleeding_overspeed',False)),
                         boost_heat=ns.get('boost_heat',0.),boost_active=int(ns.get('boost_active',False)),
                         boost_overheated=int(ns.get('boost_overheated',False)),boost_multiplier=ns.get('boost_multiplier',1.))
            for axis,value in zip('xyz',ns.get('acceleration_body',(0.,0.,0.))):state['acceleration_body_'+axis]=value
            if self.newtonian is not None:
                native=self.newtonian.sample()
                for name in ('final_before','final_after','outer_velocity','outer_velocity_after_integrator'):
                    for axis,value in zip('xyz',native.get(name) or ()):
                        state['newtonian_'+name+'_'+axis]=value
                state['newtonian_avoidance_flag']=native.get('avoidance_flag','')
        # Read-only mode evidence: distinguish combat admission from a lost
        # command or a physics fallback in the next manual validation capture.
        pm=getattr(self,'pm',None)
        if pm is not None:
            try:
                manager=struct.unpack('<Q',pm.read_bytes(_internal.BASE_ADDRESS+APP_GLOBAL,8))[0]
                if manager:
                    state['combat_active']=pm.read_bytes(manager+0x57a40d,1)[0]
                    state['frontend_state']=struct.unpack('<I',pm.read_bytes(manager+0x875498,4))[0]
            except BaseException:pass # Missing diagnostic reads never affect controls.
        state['newtonian_requalifications']=getattr(self,'newtonian_refresh_count',0)
        state['parking_brake_requested']=int(bool(getattr(getattr(self,'buttons',None),'parking',False)))
        consumer=getattr(self,'flight_consumer',None);air=((consumer.latest.get('planetary') if consumer is not None else None) or {})
        state.update(landing_gear=int(bool(air.get('gear'))),ground_clearance=air.get('clearance',''),ground_tilt=air.get('tilt',''),
                     touchdown=getattr(getattr(self,'touchdown',None),'last','') or '')
        record=(self._flight_record(K.GetTickCount64())
                if getattr(self,'newtonian',None) is not None and getattr(self,'hud_motion_reader',None) is not None else None)
        if record:state['landing_state']=record['landing']
        self.telemetry.write(build_row(time.monotonic(),state,fields,receipt,mav))

    # ---------- runtime loop ----------
    def _start_bus(self):
        from hardware_bus_reader import DirectInputBus
        config=runtime_config.load(ROOT)['bus']
        bus=DirectInputBus(config);bus.start()
        self.bus=bus;self.reader=BusReader(bus)
        self._start_agent_test()

    def _start_agent_test(self):
        """Only the explicit per-machine developer flag creates a local pipe."""
        self.agent_test=None;self.agent_test_owns=False
        config=runtime_config.load(ROOT).get('agent_test',{})
        if not isinstance(config,dict) or config.get('enabled') is not True:return
        try:
            from agent_release_runtime import ReleaseIntegration
            path=Path(config['config'])
            if not path.is_absolute():path=ROOT/path
            self.agent_test=ReleaseIntegration(self.bus,path,lambda:dict(self.store.values),
                                              focused=self._focused,ticks=K.GetTickCount64,
                                              stop_agent=self.box.stop_agent).start()
            self.log('agent rotation validation pipe ready: '+self.agent_test.host.name)
        except Exception as error:
            if self.agent_test is not None:
                try:self.agent_test.close()
                except Exception:pass
            self.agent_test=None
            self.log('agent test disabled: '+repr(error))

    def _agent_receipt(self):
        """Decode only the existing seqlocked native receipt; no new game reads."""
        raw=self._receipt_raw() if self.patches is not None else None
        if raw is None:return None
        seq,data=raw
        result={'sequence':seq,'generation':struct.unpack_from('<Q',data,104)[0],
                'invocation':struct.unpack_from('<Q',data,24)[0],
                'deadline':struct.unpack_from('<Q',data,64)[0],
                'mask':struct.unpack_from('<I',data,72)[0],
                'values':list(struct.unpack_from('<6f',data,80)),**decode_receipt(data)}
        if not all(math.isfinite(v) for v in result['values']+[result['u_native'],result['u_applied']]):return None
        return result

    def _agent_select(self,values,command,reason,now,dt,tick,receipt_age):
        agent=getattr(self,'agent_test',None)
        if agent is None:return None,False
        try:
            held=set(self.buttons.state.held)|set(self.buttons.key_state.held)
            if self.buttons.parking:held.add('physical parking brake')
            if self.buttons.boost:held.add('physical boost')
            observed=self._agent_receipt()
            eligible=bool(reason is None and observed is not None and not observed['conflict']
                          and 0<=receipt_age<=.1 and self._eligible_native(tick)
                          and self.store.values['flight-model']=='vanilla')
            # Immutable copied frames from the pipe's native reader avoid a
            # hardware rescan delay and never make the release worker take its lock.
            selection=agent.select(agent.device.latest,values,command,eligible=eligible,reason=reason,
                                   now=now,dt=dt,native_tick=tick,physical_buttons=held,receipt=observed)
            handback=getattr(self,'agent_test_owns',False) and selection is None
            self.agent_test_owns=selection is not None
            if handback:
                self.control.disarm_all('agent test ended')  # physical inputs ramp back in from where they are
                self.translation.disarm('agent test ended')
            return selection,handback
        except Exception as error:
            agent.revoke('release selection error: '+repr(error))
            handback=getattr(self,'agent_test_owns',False);self.agent_test_owns=False
            if handback:
                self.control.disarm_all('binding changed');self.translation.disarm('agent test error')
            self.log('agent selection stopped: '+repr(error))
            return None,handback

    def _agent_publish(self,selection,tick):
        """Serialize epoch validation/rearm/publication; revocation has a native latch."""
        agent=self.agent_test
        def publish(current):
            now=K.GetTickCount64()
            ttl=max(0,min(current.ttl_ms,current.command.deadline-now))
            if not ttl:return None
            if getattr(self,'agent_test_published_epoch',None)!=current.epoch:
                self.box.publish((0.,)*6,now,enabled=False,ttl_ms=1)
                self.box.rearm_agent()
                self.agent_test_published_epoch=current.epoch
            self._renew_lease(now,ttl)
            generation=self.box.publish(current.values,now,enabled=any(v!=0 for v in current.values),
                                        ttl_ms=ttl,strict_rotation=True)
            agent.published_generation(generation)
            return generation
        return agent.owner.publish(selection,publish)

    def _log_axis_states(self):
        """One log line whenever any axis changes state: armed, arming, or why it is held (user 2026-10-22:
        dead-axis reports need this in support bundles). Native channels and Sultan translation separately."""
        native=getattr(self.control,'reasons',None)
        translation=getattr(getattr(self,'translation',None),'reasons',None)
        states=dict(native) if isinstance(native,dict) else {}
        if isinstance(translation,dict):states.update({'sultan '+c:r for c,r in translation.items()})
        last=getattr(self,'axis_states',{})
        changed={c:r for c,r in states.items() if last.get(c)!=r}
        self.axis_states=states
        if not changed:return
        self.axis_state_lines=getattr(self,'axis_state_lines',0)+1
        if self.axis_state_lines<=3000:self.log('axes: '+', '.join(f'{c} {r}' for c,r in sorted(changed.items())))

    def _focused(self):
        pid=C.c_ulong();U.GetWindowThreadProcessId(U.GetForegroundWindow(),C.byref(pid));return pid.value==os.getpid()

    def _run(self):
        last=time.monotonic();bus_retry=0.;fault_until=0.;last_status=0.;last_seq=None;seq_time=last
        while not self.stopping.is_set():
            now=time.monotonic();dt=now-last;last=now
            try:
                if self.bus is None and now>=bus_retry:
                    try:self._start_bus()
                    except BaseException as e:
                        bus_retry=now+5;self._set_state('input bus unavailable: '+str(e))
                if self.reader:self.table.update(self.reader.read())
                if self.menu is not None and (self.menu.depth_routes or self.menu.pending is not None):
                    # DoOptions stops running once the menu closes: notice a full exit here. Pops between
                    # our own screens are handled on the UI thread, never racing a push in flight.
                    depth,kind=self._menu_depth()
                    if depth==0 or (self.menu.depth_routes and depth<min(self.menu.depth_routes)):
                        self.trace(f'menu left (depth {depth}); saved');self.menu.frame(depth)
                    elif self.menu.depth_routes and now-self.last_options>2.:
                        # Watchdog: our screen is up but the options page stopped building. Release it.
                        self.trace(f'STALL: no DoOptions for {now-self.last_options:.1f}s at depth {depth} kind {kind}; '
                                   f'counters {self.counters}; meters off, HOTAS screens released')
                        self.meters_enabled=False;self.menu.close_all()
                self.menu_open=bool(self.menu and self.menu.open)
                self._poll_capture()
                with self.lock:
                    tick=K.GetTickCount64()
                    if getattr(self,'pilot_recovery',None) is not None and self._recover_pilot(now):
                        last_seq=0;seq_time=now;fault_until=0.
                    recovering=getattr(self,'pilot_recovery',None) is not None
                    piloting=self.block.pilot_until>tick and self.block.ship!=0
                    if piloting and self.patches is None and self.bus is not None and now>=fault_until:
                        try:self._install(self.block.ship)
                        except BaseException as e:
                            fault_until=now+10;self.errors+=1;self.log('install failed: '+''.join(traceback.format_exception(e)))
                    if not recovering and self.patches is not None and not any(self.control.armed.values()):
                        # Retarget a new ship or apply a changed turn rate only while nothing is armed, and
                        # only while piloting, so the flight thread is there to take the code writes.
                        if piloting and (self.block.ship!=self.installed_ship or self.installed_turn_scale!=self.store.values['turn-rate-scale']):
                            self._uninstall()
                    # Code writes release the GIL while waiting for Update.
                    # A lifecycle watcher may have invalidated ownership there.
                    recovering=getattr(self,'pilot_recovery',None) is not None
                    piloting=self.block.pilot_until>K.GetTickCount64() and self.block.ship!=0
                    receipt=self._receipt() if self.patches is not None and not recovering else None
                    if receipt and receipt[0]!=last_seq:last_seq,seq_time=receipt[0],now
                    native_u=receipt[2] if receipt else None
                    reason=('stopping' if self.stopping.is_set() else 'reacquiring player ship' if recovering else 'not piloting' if not piloting else 'game not focused' if not self._focused() else 'menu open' if self.menu_open
                            else 'binding capture' if self.capture is not None else 'starting' if self.patches is None
                            else 'player ship changed' if self.installed_ship!=self.block.ship
                            else 'waiting for flight receipt' if receipt is None
                            else 'native conflict' if now<fault_until else None)
                    geffects=getattr(self,'geffects',None)
                    if geffects is not None and geffects.ready_to_install(self.store.values,self.patches is not None and not recovering,
                                                                          self.pilot_boundary.thread,int(_internal.BASE_ADDRESS)):
                        from native_roll_patch import allocate_near
                        geffects.install(self.pm,os.getpid(),int(_internal.BASE_ADDRESS),C.cast(K.GetTickCount64,C.c_void_p).value,
                                         C.addressof(self.block)+24,self._external_write_code,allocate_near)
                    self._ensure_newtonian(now,piloting and not recovering and not self.stopping.is_set()
                                           and self.installed_ship==self.block.ship)
                    if getattr(self,'pilot_recovery',None) is not None:
                        recovering=True;reason='reacquiring player ship'
                    elif self.stopping.is_set():reason='stopping'
                    values=self.control.step(self.table,reason is None,dt,native_u,reason or '')
                    handed=self.control.reasons.get('throttle')=='game throttle active'
                    if handed!=getattr(self,'game_throttle',False):
                        self.game_throttle=handed
                        self.log(f'game throttle active (native u={native_u:.2f}, launch/station auto-boost or keyboard); HOTAS throttle handed over, stick axes stay live'
                                 if handed else 'game throttle released; HOTAS throttle resumes')
                    armed=any(self.control.armed.values())
                    if self.patches is not None and not recovering:
                        if getattr(self,'agent_test',None) is None:self._renew_lease(tick)
                        if armed and receipt and receipt[1]:
                            self.conflicts+=1
                            if self.conflicts>=25:
                                fault_until=now+2;self.conflicts=0
                                self.log('native input conflict: NMS is also reading a bound device (HOTAS bound in Steam Input?)')
                        elif receipt:self.conflicts=0
                        stall=self.stall.check(now,seq_time,armed,self._frontend,self._stall_context)
                        if stall:  # 'game menu' or 'native receipts stale'; stick channels re-arm off-centre with a ramp
                            self.control.disarm_all(stall)
                            values=(0.,)*6;armed=False;reason=stall
                        for line in self.stall.drain():self.log(line)
                    if self.store.values.get('aim-slowdown-enabled',False):
                        self._aiming_tick(tick,self._aim_deflection(),reason is None and armed)
                    elif getattr(self,'aim_slowdown',None) is not None:self.aim_slowdown.disable()
                    self._step_crash_damage(tick,piloting,recovering)
                    self._step_wreck_fall(tick)
                    # T93: a ditch knocks the engines out. The pilot blacks out once per crash (user 2026-10-19:
                    # the dead stop is never seen) and the inputs stay centred until the restart ramps them back.
                    condition=getattr(getattr(self,'flight_consumer',None),'condition',None);control_share=1.
                    if condition is not None:
                        knockout=(id(condition),condition.knockouts)
                        if knockout!=getattr(self,'condition_knockout',(id(condition),0)) and knockout[1]:
                            # A ditch blacks out here; a disabling crash dazes instead (_handle_crash_hit, user 2026-10-25:
                            # this knock-out blacked out most of the crash tumble).
                            if geffects is not None and condition.cause=='ditch':geffects.strain.knock_out(CRASH_BLACKOUT_HOLD)
                            if condition.cause=='ditch' and condition.restart_after is None:
                                water=planetary_flight.Water.from_values(self.store.values)
                                ratio=(condition.impact or water.ditch_speed)/max(1.,water.ditch_speed)
                                condition.purge=ditch_purge.make(water.purge_style,tick,ratio,water.purge_difficulty,condition.hull,water.purge_length)
                                self.log(f'ditched at sea at {condition.impact or 0:.0f} u/s: engines out, thruster purge'
                                         f' (hull {100*condition.hull:.0f}%)')
                                self.alarm_eject=False
                                if getattr(self,'alarms',None) is not None:self.alarms.moment('ditch',tick/1000.)
                                if getattr(self,'crash_damage',None) is not None:self.crash_damage.submerge(True)  # at once
                        self.condition_knockout=knockout
                        purge=condition.purge;purge_live=reason is None and not (geffects is not None and geffects.strain.out)
                        self._alarms_step(condition,purge,self._step_purge(condition,dt,tick,purge_live),purge_live,tick)
                        control_share=condition.control_share(tick)
                    g_scale=1.
                    if geffects is not None:
                        # G load: the commanded acceleration of admitted Sultan flight; anything else recovers.
                        # latest keeps the last command after a handover to native, so the current flight
                        # record must say the helper's command was applied (2026-10-19: a frozen 25.7 G
                        # brake command blacked the pilot out through a whole native atmosphere flight).
                        latest=self.flight_consumer.latest;record=self._flight_record(tick)
                        applied=record is not None and record['applied'] and record['reason']==planetary_hud.REASON_APPLIED
                        accel=latest.get('acceleration_body') if latest.get('admitted') and reason is None and applied else None
                        manager=q(_internal.BASE_ADDRESS+APP_GLOBAL)
                        g_scale=geffects.tick(self.store.values,dt,tick,accel,
                                              (manager,self.block.ship,q(manager+0x72c858) if manager else 0))['control']
                        if g_scale<1.:values=tuple(v if c=='throttle' else v*g_scale for c,v in zip(MAILBOX_ORDER,values))
                    if control_share<1.:values=tuple(v*control_share for v in values)
                    if condition is not None and condition.falling:  # the wreck tumbles as it falls (user 2026-10-25)
                        values=condition.tumble(self.flight_consumer.latest.get('speed'))+tuple(values[3:])
                    if getattr(self,'agent_test',None) is None:
                        self.box.publish(values,tick,enabled=armed and self.patches is not None,ttl_ms=100)
                    # Keys and mouse also serve the quick menu, dialogs and pause menu, so they need
                    # only focus and no HOTAS menu or capture; flight actions need active piloting.
                    try:self._touchdown(now,reason is None)
                    except BaseException as error:  # never costs the tick; the Land key stays up
                        self.buttons.synthetic_keys=frozenset()
                        if not getattr(self,'touchdown_failed',False):self.touchdown_failed=True;self.log('touchdown error: '+repr(error))
                    self._log_axis_states()
                    self.buttons.step(self.table.digital,now,flight_buttons_live(reason),
                                      self.capture is None and not self.menu_open and self._focused())
                    self._eject_hold(now,tick,reason is None)
                    # Dialogs and menus: the free stick drives the cursor (the frontend flag also releases flight axes).
                    v=self.store.values
                    self.buttons.step_cursor(self.table.unit(v['bind-pitch-axis']),self.table.unit(v['bind-yaw-axis']),dt,
                                             self._focused(),self.menu_open or self.capture is not None,self._frontend())
                    with self.newtonian_lock:
                        self.translation_command=self.translation.step(self.table,self.store.values,reason is None,tick,
                                                                       lever_zone_settings(self.store.values),self.buttons.parking,boost=self.buttons.boost)
                        # T93: raw controls for the thruster purge, before any engines-out scaling.
                        raw=self.translation_command
                        self.purge_inputs={'throttle':raw.throttle.value,'vertical':raw.vertical,'lateral':raw.lateral,'boost':self.buttons.boost}
                        # T88 lift trim: worker-held, cleared on landing and on a ship change.
                        lift_trim=getattr(self,'lift_trim',None);trim=0.
                        if lift_trim is not None:
                            record=self._flight_record(tick);planetary=(self.flight_consumer.latest.get('planetary') or {})
                            trim=lift_trim.step(dt,getattr(self.buttons,'trim_up',False),getattr(self.buttons,'trim_down',False),
                                                getattr(self.buttons,'trim_events',()),planetary.get('hover_trim'),self.block.ship,
                                                bool(record and record['landing']))
                        if trim and self.translation_command.enabled:
                            self.translation_command=dataclasses.replace(self.translation_command,trim=trim)
                        if getattr(self,'agent_test',None) is not None:
                            selection,handback=self._agent_select(values,self.translation_command,reason,now,dt,tick,now-seq_time)
                            if selection is not None:
                                values=selection.values;armed=any(v!=0 for v in values)
                                self.translation_command=selection.command
                            elif handback:
                                values=(0.,)*6;armed=False
                                self.translation_command=dataclasses.replace(self.translation_command,enabled=False)
                            ttl=selection.ttl_ms if selection is not None else 100
                            if selection is not None:
                                if self.patches is None or recovering or self._agent_publish(selection,tick) is None:
                                    values=(0.,)*6;armed=False
                                    self.translation_command=dataclasses.replace(self.translation_command,enabled=False)
                                    self.box.publish(values,K.GetTickCount64(),enabled=False)
                            else:
                                if self.patches is not None and not recovering:self._renew_lease(tick)
                                self.box.publish(values,tick,enabled=armed and self.patches is not None,ttl_ms=ttl)
                        command=self.translation_command
                        if control_share<1. and command.enabled:  # T93 restart ramp (engines out ignores the pilot)
                            command=self.translation_command=dataclasses.replace(command,lateral=command.lateral*control_share,vertical=command.vertical*control_share,
                                                                                 throttle=dataclasses.replace(command.throttle,value=command.throttle.value*control_share))
                        if g_scale<1. and command.enabled:  # blacked-out pilot: inputs fade, the ship coasts
                            self.translation_command=dataclasses.replace(command,lateral=command.lateral*g_scale,vertical=command.vertical*g_scale,
                                                                         throttle=dataclasses.replace(command.throttle,value=command.throttle.value*g_scale))
                        if self.newtonian:
                            self.newtonian.renew(100)
                            self.newtonian.set_auto_bank(self.translation_profiles[self.newtonian.fixture['ship_class']]['auto_bank_on_yaw'])
                            self.newtonian.set_enabled(self.translation_command.enabled)
                            try:self._update_planetary(now)
                            except BaseException as error:
                                self.air_env=None;self.log('planetary flight error: '+repr(error))
                                try:self.newtonian.set_planetary(False);self.planetary_on=False
                                except BaseException:pass
                                try:self.newtonian.set_gear(False);self.planetary_gear=False
                                except BaseException:pass
                # Button-requested settings apply outside self.lock: the settings listener takes it.
                if not additive_rotation.owns_sites(self):rotation_validation.worker_tick(self)
                additive_rotation.worker_tick(self,values,reason=reason,now_ms=K.GetTickCount64())
                changes=self.buttons.take_changes()
                for control_id,value in changes:
                    self.log(f'button: {control_id} -> {value}');self.store.set(control_id,value)
                if changes:self.store.save()
                try:self._drain_translation_receipts(now)
                except BaseException as error:
                    self.log('Newtonian receipt export off: '+repr(error));self.newtonian_receipt_path=None
                if now-last_status>=.5:
                    last_status=now
                    self._update_cockpit_speed();self._update_ai_pilots();self._update_ai_combat();self._update_black_space();self._update_terrain()
                    self.status={'state':'armed' if armed else (reason or 'waiting'),'channels':dict(self.control.reasons),
                                 'values':dict(zip(MAILBOX_ORDER,values)),'devices':self.table.names(),'errors':self.errors,'hook_errors':self.hook_errors,
                                 'newtonian':self._translation_status(),'translation_channels':dict(self.translation.reasons),
                                 'additive_rotation':additive_rotation.status(self),
                                 'weapon_lead':self.weapon_lead.status(),
                                 'aiming':self._aiming_status(),
                                 'flight_hud':{'state':self.hud_state,'draws':self.counters['hud_draws'],'errors':self.hud_errors,**self.hud_live},
                                 'cockpit_speed':self.cockpit_speed.status() if getattr(self,'cockpit_speed',None) else {'state':'not initialized'},
                                 'ai_pilots':self.ai_pilots.status() if getattr(self,'ai_pilots',None) else {'state':'not initialized'},
                                 'ai_aim':self.ai_aim.status() if getattr(self,'ai_aim',None) else {'state':'not initialized'},
                                 'npc_combat':self.ai_combat.status() if getattr(self,'ai_combat',None) else {'state':'not initialized'},
                                 'black_space':self.black_space.status() if getattr(self,'black_space',None) else {'state':'not initialized'},
                                 'terrain_lookahead':self.terrain.status() if getattr(self,'terrain',None) else {'state':'not initialized'},
                                 'player_recoveries':getattr(self,'pilot_recoveries',0),
                                 'agent_test':self.agent_test.status() if getattr(self,'agent_test',None) else {'enabled':False},
                                 'player_recovery':getattr(self,'pilot_recovery',None),
                                 'buttons':sorted(self.buttons.state.held),'key_output':self.buttons.key_status(),'g':self.geffects.status(),
                                 'crash_damage':self._crash_damage_status(),'ship_alarms':self.alarms.status() if getattr(self,'alarms',None) else None,
                                 'head_tracking':self.head.status() if self.head is not None else {'state':'not initialized'}}
                    try:(DATA/'hotas-status.json').write_text(json.dumps(self.status,indent=1),encoding='utf-8')
                    except OSError:pass
                if piloting and self.telemetry is not None and self.telemetry.due():
                    try:self._record(reason,values)
                    except BaseException as e:
                        # Telemetry never takes flight down: log once and stop recording.
                        self.log('telemetry off: '+''.join(traceback.format_exception(e)))
                        self.telemetry.close();self.telemetry=None
            except BaseException as e:
                # A terminal stop may latch the mailbox during an existing poll.
                # Leave its gates inert; shutdown must never enter recovery.
                if self.stopping.is_set():break
                self.errors+=1;self.log('runtime error: '+''.join(traceback.format_exception(e)))
                if getattr(self,'agent_test',None) is not None:self.agent_test.revoke('release runtime error')
                try:
                    # Latch stop, publish neutral, then re-open for the next armed frame.
                    self.control.disarm_all('runtime error');self.box.disable()
                    additive_rotation.disarm(self,'runtime error')
                    self.translation.disarm('runtime error');self.translation_command=None
                    if self.newtonian:self.newtonian.disable()
                    if getattr(self,'crash_damage',None) is not None:self.crash_damage.disable()
                    self.box.publish((0.,)*6,K.GetTickCount64(),enabled=False)
                    if getattr(self,'pilot_recovery',None) is None:self.box.rearm()
                except BaseException:pass
                try:self.buttons.release('runtime error')  # separate: must never block the recovery above
                except BaseException:pass
                time.sleep(.25)
            time.sleep(TICK_SECONDS)

    def _update_ai_pilots(self):
        """World data, not flight: applies in menus and native flight too. Errors stay local."""
        pilots=getattr(self,'ai_pilots',None)
        if pilots is None:return
        try:
            v=dict(self.store.values);before=pilots.status()['state']
            after=pilots.update(wanted_mode(v),v['ai-readability'],v)
            if after['state']!=before:self.log('enemy pilots: '+json.dumps(after))
        except BaseException as error:
            self.log('enemy pilots disabled: '+repr(error))
            try:pilots.restore()
            except BaseException:pass

    def _update_cockpit_speed(self):
        """Display errors must never enter the flight worker's recovery path."""
        display=getattr(self,'cockpit_speed',None)
        if display is None:return
        try:
            before=display.status()['state'];after=display.update()
            if after['state']!=before:self.log('cockpit speed: '+json.dumps(after))
        except BaseException as error:
            self.log('cockpit speed disabled: '+repr(error))
            try:display.restore()
            except BaseException:pass

    def _update_ai_combat(self):
        combat=getattr(self,'ai_combat',None)
        if combat is None:return
        try:
            values=dict(self.store.values);before=combat.status()['state']
            after=combat.update(wanted_mode(values),values['ai-readability'],values)
            if after['state']!=before:self.log('NPC beams/shields: '+json.dumps(after))
        except Exception as error:
            self.log('NPC beams/shields disabled: '+repr(error))
            try:combat.restore()
            except Exception:pass

    def _update_black_space(self):
        sky=getattr(self,'black_space',None)
        if sky is None:return
        try:
            before=sky.status()['state'];after=sky.update(bool(self.store.values.get('black-space')))
            if after['state']!=before:self.log('black space: '+json.dumps(after))
        except Exception as error:
            self.log('black space disabled: '+repr(error))
            try:sky.restore()
            except Exception:pass

    def _update_terrain(self):
        terrain=getattr(self,'terrain',None)
        if terrain is None:return
        try:
            before=terrain.status();after=terrain.update(self.store.values)
            # T107 live check: also every 20 s while the focus is being moved (offset, grid cell step).
            now=time.monotonic()
            if ((after['state'],after.get('budgets'),after.get('fade'))!=(before['state'],before.get('budgets'),before.get('fade'))
                    or after.get('applied',0)>before.get('applied',0) and now-getattr(self,'terrain_logged',0.)>=20.):
                self.terrain_logged=now;self.log('terrain look-ahead: '+json.dumps(after))
        except Exception as error:
            self.log('terrain look-ahead disabled: '+repr(error))
            try:terrain.restore()
            except Exception:pass

    def _terrain_velocity(self):
        """Ship velocity while piloting, read once per millisecond tick (the planet update runs once per planet)."""
        now=K.GetTickCount64()
        if self.terrain_motion[0]!=now:
            motion=self._hud_motion(now) if self.block.ship else None
            velocity=motion.get('velocity') if motion else None
            self.terrain_motion=(now,tuple(velocity) if velocity is not None and len(velocity)==3 and all(math.isfinite(v) for v in velocity) else None)
        return self.terrain_motion[1]

    # T107: the planet update passes the region map its stack copy of the observer matrix (rdx); moving that copy
    # moves only the terrain focus. The after hook puts it back.
    @manual_hook('AbsoluteHOTASTerrainFocus',offset=0x12801B0,func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='before')
    def terrain_focus_before(self,this,observer,extra):
        terrain=getattr(self,'terrain',None)
        if terrain is None or not terrain.enabled:return
        try:terrain.focus_before(C.cast(observer,C.c_void_p).value,self._terrain_velocity())
        except BaseException as error:terrain.fail(error);self.hook_errors+=1

    @manual_hook('AbsoluteHOTASTerrainFocus',offset=0x12801B0,func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='after')
    def terrain_focus_after(self,this,observer,extra):
        terrain=getattr(self,'terrain',None)
        if terrain is None or not (terrain.enabled or terrain.pending):return
        try:terrain.focus_after(C.cast(this,C.c_void_p).value,C.cast(observer,C.c_void_p).value)
        except BaseException as error:terrain.fail(error);self.hook_errors+=1

    # ---------- native menu (D15): rows on Controls-type pages, meters at nvgEndFrame ----------
    def _build_menu(self):
        """Resolve every native UI function from the exact-build manifests; refuse on any byte change.
        The manifests are copies of the research/re-log ones, shipped in mod/data (T100)."""
        base=int(_internal.BASE_ADDRESS);targets={}
        for name in ('2026-10-15_codex_options-ui-static-v4.json','2026-10-15_codex_ui-range-static.json'):
            meta=json.loads((ROOT/'mod'/'data'/name).read_text(encoding='utf-8'))
            for key,t in meta['targets'].items():
                if 'entry32' in t:targets.setdefault(key,t)
        P,I,F,B,U64=C.c_void_p,C.c_int32,C.c_float,C.c_uint8,C.c_uint64
        rows={'Header':(None,[P,U64,I]),'Boolean':(C.c_bool,[P,U64,U64,B,B,P]),'Float':(F,[P,U64,U64,F,F,F,F,P]),
              'Button':(C.c_bool,[P,U64,U64,P]),'RequestOptionsPage':(None,[I,C.c_bool])}
        for name,(result,args) in {**rows,**NVG}.items():
            t=targets[name];address=base+int(t['rva'],16)
            if C.string_at(address,32).hex()!=t['entry32']:raise RuntimeError('native entry changed: '+name)
            self.ui_api[name]=C.CFUNCTYPE(result,*args)(address)

        class CStr:
            def __init__(self,text):self.buf=C.create_string_buffer(text.encode('utf-8'));self.address=C.addressof(self.buf)

        class CBlock:
            def __init__(self,data):self.buf=C.create_string_buffer(data,max(len(data),1));self.address=C.addressof(self.buf)

        strings=Strings(CStr);options=NativeOptions(strings,CBlock)
        self.menu=NativeMenu(self.store,strings,options,self.start_capture,self.cancel_capture,on_action=self._menu_action)
        self.menu.trace=self.trace
        self.painter=MeterPainter(self.ui_api)
        self.hud_painter=flight_hud.HudPainter(self.ui_api)
        self.nt=C.WinDLL('ntdll')
        self.nt.NtQueryInformationThread.argtypes=[C.c_void_p,C.c_int32,C.c_void_p,C.c_ulong,C.c_void_p]
        self.nt.NtQueryInformationThread.restype=C.c_int32
        # The pause-root entry is optional: without it the Controls-page entry still works.
        try:self.pause_root=self._build_pause_root(base)
        except BaseException as e:self.pause_root=None;self.log('pause root entry off: '+repr(e))

    def _build_pause_root(self,base):
        """E-pause: poll the UI mod's BUTTON_HOTAS like DoGameSwitcher polls its own (pause_root.py)."""
        P,B=C.c_void_p,C.c_uint8
        fns={}
        for name,(rva,entry,result,args) in PAUSE_ROOT_NATIVE.items():
            if C.string_at(base+rva,32).hex()!=entry:raise RuntimeError('native entry changed: '+name)
            fns[name]=C.CFUNCTYPE(result,*args)(base+rva)
        class Aligned:
            """16-byte aligned copy: PollButton loads its 5th argument with movaps (live GP fault, 2026-10-16)."""
            def __init__(self,data):
                self.buf=C.create_string_buffer(len(data)+16);self.address=(C.addressof(self.buf)+15)&~15
                C.memmove(self.address,data,len(data))
        keys={};sound=Aligned(struct.pack('<QIB3x',0,SOUND_HASH,1))
        def key(text):
            if text not in keys:keys[text]=Aligned(key_bytes(text))
            return keys[text].address
        def is_layer(element):
            return C.CFUNCTYPE(C.c_int32,P)(q(q(element)+8))(element)==0
        self._pause_keep=(keys,sound,fns)
        return PauseRootButton(Mem,lambda root,k:fns['FindLayer'](root,k,0),
                               lambda this,white:fns['PollButton'](this,white,1,0,sound.address,0,1),
                               key,is_layer,self.trace)

    def _open_from_pause_root(self):
        """Our button was activated on the pause root: push a Controls page and own it as ROOT."""
        depth,kind=self._menu_depth()
        if depth!=0:
            self.trace(f'pause root press ignored at depth {depth}');return
        self.ui_api['RequestOptionsPage'](CONTROLS_PAGE,True)
        after,after_kind=self._menu_depth();self.counters['pushes']+=1
        self.trace(f'pause root push: depth {depth}->{after} kind {after_kind}')
        if after==1 and after_kind==CONTROLS_PAGE:
            # Start the stall watchdog's clock: no DoOptions may have run yet this session (live run 2).
            self.last_options=time.monotonic();self.meters_enabled=True;self.menu.root_pushed(after)
        else:self.trace('pause root push not observed; native Controls stays native')

    def _menu_depth(self):
        """(depth, menu type) of the native options stack; F0: menus[depth-1], depth 0 = closed."""
        depth=C.c_uint32.from_address(_internal.BASE_ADDRESS+0x6EB64E4).value
        if depth==0 or depth>32:return depth,None
        state=q(q(_internal.BASE_ADDRESS+0x6EB64E8)+8*(depth-1))
        return depth,C.c_int32.from_address(state).value

    def _original(self,rva):
        fn=self.originals.get(rva)
        if fn is None:
            fn=self.originals[rva]=next(h.original for h in hook_manager.hooks.values() if getattr(h,'_offset',None)==rva)
        return fn

    def _live(self):
        v=self.store.values;live={}
        for c in ('pitch','yaw','roll','strafe-lateral','strafe-vertical','throttle'):live[c]=self.table.unit(v[f'bind-{c}-axis'])
        if v['throttle-style']=='hosas' and v['hosas-stick-mode']=='rate':live['throttle-target']=self.control.throttle.rate.target
        if getattr(self,'ship_class',None) is not None:live['ship-class']=self.ship_class
        live['newtonian']=self._translation_status()
        if getattr(self,'touchdown',None) is not None:live['touchdown']=self.touchdown.last
        live['additive-rotation']=additive_rotation.status(self)
        live['key-output']=self.buttons.key_status()
        if getattr(self,'geffects',None) is not None:live['g']=self.geffects.status()
        if getattr(self,'ai_pilots',None) is not None:live['ai-pilots']=self.ai_pilots.status()
        if getattr(self,'ai_aim',None) is not None:live['ai-aim']=self.ai_aim.status()
        if getattr(self,'ai_combat',None) is not None:live['npc-combat']=self.ai_combat.status()
        if getattr(self,'black_space',None) is not None:live['black-space']=self.black_space.status()
        if getattr(self,'terrain',None) is not None:live['terrain-lookahead']=self.terrain.status()
        if getattr(self,'head',None) is not None:live['head']=self.head.status()
        live['crash-damage']=self._crash_damage_status()
        if getattr(self,'alarms',None) is not None:
            live['ship-alarms']={**self.alarms.status(),'unavailable':getattr(self,'native_alarms_failed',False),
                                 'voice_unavailable':getattr(self,'voice_player_failed',False)}
        return live

    def _ui_fail(self,error):
        self.hook_errors+=1;self.log('menu error: '+''.join(traceback.format_exception(error)))

    @manual_hook('AbsoluteHOTASOptions',offset=0x6E08B0,func_def=FUNCDEF(None,[]),detour_time='before')
    def options_enter(self):
        u=self.ui;u.page=0;u.inserted=False;u.ours=None;u.entry=False
        try:
            if not self.active or self.menu is None:return None
            depth,kind=self._menu_depth()
            route=self.menu.frame(depth)
            self.last_options=time.monotonic();self.counters['options']+=1
            key=(depth,kind,route)
            if key!=self.trace_key:
                self.trace_key=key;self.trace(f'options depth={depth} kind={kind} route={route} routes={dict(self.menu.depth_routes)}')
            if route is not None and getattr(u,'ctx',0):
                # U3: layout scale and active matrix are read here, on the UI thread mid-render.
                try:self.ui_layout=layout_from_tls(q,C.string_at,self.nt,u.ctx)
                except BaseException:self.ui_layout=None
            if kind==CONTROLS_PAGE:
                if route is not None:u.ours=route
                else:u.entry=True
            self.menu_open=self.menu.open
        except BaseException as e:self._ui_fail(e)
        return None

    @manual_hook('AbsoluteHOTASOptions',offset=0x6E08B0,func_def=FUNCDEF(None,[]),detour_time='after')
    def options_leave(self):
        self.ui.ours=None;self.ui.entry=False
        return None

    @manual_hook('AbsoluteHOTASSwitcher',offset=0x6E16A0,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='after')
    def switcher_leave(self,this):
        """Pause Options root (DoGameSwitcher): poll our BUTTON_HOTAS after the native buttons."""
        try:
            if not self.active or self.menu is None or self.pause_root is None or not this:return None
            if self.pause_root.frame(this,q(_internal.BASE_ADDRESS+APP_GLOBAL)):self._open_from_pause_root()
        except BaseException as e:
            self.pause_root=None;self._ui_fail(e)
        return None

    @manual_hook('AbsoluteHOTASBeginPage',offset=0x1B99A0,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='after')
    def page_begin(self,page):
        if getattr(self.ui,'ours',None) is not None or getattr(self.ui,'entry',False):self.ui.page=page
        return None

    @get_caller
    @manual_hook('AbsoluteHOTASFinalize',offset=0x1B9E90,func_def=FUNCDEF(C.c_uint64,[C.c_void_p,C.c_bool,C.c_bool]),detour_time='before')
    def page_finalize(self,page,a,b):
        """Emit our rows before the Controls builder finalizes (caller 0x6E684D, F0/F1)."""
        u=self.ui
        if getattr(u,'inserted',True) or (getattr(u,'ours',None) is None and not getattr(u,'entry',False)):return None
        try:
            if self.page_finalize.caller_address()!=0x6E684D or page!=u.page or C.c_int32.from_address(page+8).value!=0:return None
        except BaseException as e:
            self._ui_fail(e);return None
        u.inserted=True;dirty=C.string_at(page+0x10,1);u.emitting=True
        try:
            self.roots={}
            if u.ours is not None:
                count=self.menu.page_rows(page,self.ui_api,u.ours,self._live(),self.table.names());self.counters['emits']+=1
                if ('emit',u.ours) not in self.trace_seen:
                    self.trace_seen.add(('emit',u.ours));self.trace(f'first rows for {u.ours}: {count} rows, meters {list(self.menu.meters)}')
                if self.menu.meters and ('roots',u.ours) not in self.trace_seen and self.counters['emits']%60==0:
                    self.trace_seen.add(('roots',u.ours))
                    self.trace(f'meter rows for {u.ours}: roots {len(self.roots)}/{len(self.menu.meters)}, layout {self.ui_layout is not None}, '
                               f'draws {self.counters["draws"]}, skips {self.counters["draw_skips"]}')
            else:self.menu.entry_rows(page,self.ui_api)
        except BaseException as e:self._ui_fail(e)
        finally:
            u.emitting=False
            # Mod-owned values must never dirty the native Apply state (U3 rule).
            C.memmove(page+0x10,dirty,1)
        return None

    def _suppress(self):
        u=self.ui
        return getattr(u,'ours',None) is not None and not getattr(u,'emitting',False)

    # Native Controls rows are swallowed only on our own screens; everywhere else, and for our own
    # calls, the original runs. NOOP detours must never raise: pyMHF would drop the detour and then
    # skip the native function entirely, so every path falls through to the original.
    @NOOP
    @manual_hook('AbsoluteHOTASHeader',offset=0x1B68A0,func_def=FUNCDEF(None,[C.c_void_p,C.c_uint64,C.c_int32]),detour_time='before')
    def row_header(self,page,label,icon):
        try:
            if self._suppress():return None
        except BaseException:pass
        try:return self._original(0x1B68A0)(page,label,icon)
        except BaseException as e:
            self._ui_fail(e);return None

    @NOOP
    @manual_hook('AbsoluteHOTASBoolean',offset=0x1B6C30,func_def=FUNCDEF(C.c_bool,[C.c_void_p,C.c_uint64,C.c_uint64,C.c_uint8,C.c_uint8,C.c_void_p]),detour_time='before')
    def row_boolean(self,page,label,desc,value,default,options):
        try:
            if self._suppress():return bool(value)
        except BaseException:pass
        try:return self._original(0x1B6C30)(page,label,desc,value,default,options)
        except BaseException as e:
            self._ui_fail(e);return bool(value)

    @NOOP
    @manual_hook('AbsoluteHOTASInt',offset=0x1B6E20,func_def=FUNCDEF(C.c_int32,[C.c_void_p,C.c_uint64,C.c_uint64,C.c_int32,C.c_int32,C.c_int32,C.c_int32,C.c_void_p]),detour_time='before')
    def row_int(self,page,label,desc,value,default,low,high,options):
        try:
            if self._suppress():return value
        except BaseException:pass
        try:return self._original(0x1B6E20)(page,label,desc,value,default,low,high,options)
        except BaseException as e:
            self._ui_fail(e);return value

    @NOOP
    @manual_hook('AbsoluteHOTASFloat',offset=0x1B70A0,func_def=FUNCDEF(C.c_float,[C.c_void_p,C.c_uint64,C.c_uint64,C.c_float,C.c_float,C.c_float,C.c_float,C.c_void_p]),detour_time='before')
    def row_float(self,page,label,desc,value,default,low,high,options):
        try:
            if self._suppress():return value
        except BaseException:pass
        try:return self._original(0x1B70A0)(page,label,desc,value,default,low,high,options)
        except BaseException as e:
            self._ui_fail(e);return value

    @NOOP
    @manual_hook('AbsoluteHOTASButton',offset=0x1B6A80,func_def=FUNCDEF(C.c_bool,[C.c_void_p,C.c_uint64,C.c_uint64,C.c_void_p]),detour_time='before')
    def row_button(self,page,label,desc,options):
        try:
            if self._suppress():return False
        except BaseException:pass
        try:return self._original(0x1B6A80)(page,label,desc,options)
        except BaseException as e:
            self._ui_fail(e);return False

    @NOOP
    @manual_hook('AbsoluteHOTASBeginList',offset=0x1B7CD0,func_def=FUNCDEF(None,[C.c_void_p,C.c_uint64,C.c_uint64,C.c_int32,C.c_int32,C.c_void_p]),detour_time='before')
    def row_begin_list(self,page,label,desc,value,default,out):
        try:
            if self._suppress():return None
        except BaseException:pass
        try:return self._original(0x1B7CD0)(page,label,desc,value,default,out)
        except BaseException as e:
            self._ui_fail(e);return None

    @NOOP
    @manual_hook('AbsoluteHOTASListOption',offset=0x1B7DF0,func_def=FUNCDEF(None,[C.c_void_p,C.c_uint64,C.c_int32]),detour_time='before')
    def row_list_option(self,page,label,value):
        try:
            if self._suppress():return None
        except BaseException:pass
        try:return self._original(0x1B7DF0)(page,label,value)
        except BaseException as e:
            self._ui_fail(e);return None

    @manual_hook('AbsoluteHOTASGetRow',offset=0x1B6440,func_def=FUNCDEF(C.c_void_p,[C.c_void_p,C.c_void_p,C.c_void_p,C.c_int32]),detour_time='after')
    def row_root(self,page,out,label,kind,_result_):
        """Remember native row roots of meter spacers (U3 geometry source)."""
        try:
            if getattr(self.ui,'emitting',False) and getattr(self.ui,'ours',None) is not None and label and self.menu:
                text=C.string_at(label).decode('utf-8','replace')
                if text in self.menu.meters:
                    root=q(out+8)
                    if root:self.roots[text]=root
        except BaseException as e:self._ui_fail(e)
        return None

    @manual_hook('AbsoluteHOTASAppUpdate',offset=0x2D7580,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='after')
    def app_update(self,this):
        """Push a queued HOTAS screen on the application thread, as F0's navigator did."""
        try:
            if self.menu is None or self.menu.pending is None:return None
            depth,kind=self._menu_depth()
            if kind!=CONTROLS_PAGE or depth>=30:
                self.menu.pending=None;return None
            route=self.menu.take_pending()
            if route is None:return None
            self.ui_api['RequestOptionsPage'](CONTROLS_PAGE,True)
            after,after_kind=self._menu_depth();self.counters['pushes']+=1
            self.trace(f'push {route}: depth {depth}->{after} kind {after_kind}')
            if after==depth+1 and after_kind==CONTROLS_PAGE:self.menu.pushed(route,after)
            else:self.trace('push not observed; route dropped')
        except BaseException as e:self._ui_fail(e)
        return None

    @manual_hook('AbsoluteHOTASRender',offset=0x905610,func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='before')
    def render_enter(self,manager,a,b):
        self.ui.manager=manager if (self.menu is not None and self.menu.depth_routes) else 0;self.ui.ctx=0
        return None

    @manual_hook('AbsoluteHOTASRender',offset=0x905610,func_def=FUNCDEF(None,[C.c_void_p,C.c_void_p,C.c_void_p]),detour_time='after')
    def render_leave(self,manager,a,b):
        self.ui.manager=0;self.ui.ctx=0
        return None

    # ---------- T73 flight HUD: only inside RenderHeadsUp's own NGui frame (T64 seam) ----------
    @get_caller
    @manual_hook('AbsoluteHOTASHudFrame',offset=0x1DA030,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='before')
    def hud_frame_enter(self,this):
        """NGui_RenderFrame(this) reads only rcx; RenderHeadsUp calls it at 0x9D7E97."""
        try:
            stack=getattr(self.ui,'hud_stack',None)
            if stack is None or len(stack)>8:stack=self.ui.hud_stack=[]
            ours=self.hud_state=='ready' and self.hud_frame_enter.caller_address()==flight_hud.HUD_FRAME_RETURN
            stack.append(ours)
            if ours:
                self.ui.hud_ctx=0;self.ui.hud_size=None
                geffects=getattr(self,'geffects',None)
                if geffects is not None:geffects.hud_thread=K.GetCurrentThreadId()  # G effects install only on the flight thread
        except BaseException:self.hud_errors+=1
        return None

    @manual_hook('AbsoluteHOTASHudFrame',offset=0x1DA030,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='after')
    def hud_frame_leave(self,this):
        stack=getattr(self.ui,'hud_stack',None)
        if stack:stack.pop()
        return None

    def _in_hud_frame(self):
        stack=getattr(self.ui,'hud_stack',None)
        return bool(stack) and stack[-1]

    def _hud_fonts(self,ctx):
        """Font ids by name for this context, read once per fontstash and font count."""
        fs=C.c_uint64.from_address(ctx+flight_hud.FONTSTASH).value
        key=(ctx,fs,C.c_int32.from_address(fs+flight_hud.FONT_COUNT).value if fs>=0x10000 else 0)
        cached=getattr(self,'hud_font_cache',None)
        if cached is None or cached[0]!=key:cached=self.hud_font_cache=(key,self.hud_reader.fonts(ctx))
        return cached[1]

    def _flight_record(self,now):
        """Latest fresh record for our ship from the final cave (published in native fallback too), or None."""
        bridge=self.newtonian
        if bridge is None or not bridge.data:return None
        motion=self.hud_motion_reader.latest(bridge.data)
        if motion is None or motion['ship']!=self.block.ship or not 0<=now-motion['tick']<=flight_hud.SAMPLE_MAX_AGE_MS:return None
        return motion

    def _hud_motion(self,now):
        """Cave record, else the rigid body read directly while piloting (records stay identity_changed
        from a landing until Sultan requalifies in space)."""
        motion=self._flight_record(now)
        if motion is None and self.block.pilot_until>now:motion=self.hud_body_reader.motion(self.block.ship,now)
        return motion

    def _hud_planet_list(self,now):
        if now-self.hud_planets[0]>=planetary_hud.PLANET_REFRESH_MS:self.hud_planets=(now,self.hud_planet_reader.planets())
        return self.hud_planets[1]

    def _draw_hud(self,ctx):
        """Speed readout and velocity markers; NanoVG state must be byte-identical after restore."""
        try:
            if self.hud_errors>=3 or self.hud_painter is None or not self.active:return
            v=self.store.values
            if v['flight-model']!='sultan' or not v['flight-controls-enabled']:return
            geffects=getattr(self,'geffects',None)
            g=getattr(geffects,'last',None)
            # G-load vision: our dark periphery and fade to black over everything the mod draws.
            blackout=bool(g and g.get('blackout'));tunnel=g.get('tunnel',0.) if g else 0.;fade=g.get('fade',0.) if g else 0.
            red=g.get('red',0.) if g else 0.  # crash daze
            size=getattr(self.ui,'hud_size',None)
            if size is None or not (64<=size[0]<=16384 and 64<=size[1]<=16384):return
            layers=flight_hud.tunnel_layers(tunnel,size[0],size[1],fade)
            plan=gauge=gmeter=target=planet=mode=purge=landing=None
            if not blackout:
                readout,marker=v['hud-speed-readout'],v['hud-velocity-marker']
                latest=self.flight_consumer.latest
                gauge=flight_hud.boost_gauge(latest,self.translation_profiles.get(latest.get('ship_class'))) if v['hud-boost-gauge'] else None
                gmeter=flight_hud.g_meter(geffects.strain.g,v,geffects.strain.strain) if v['hud-g-meter'] and geffects is not None else None
                # T79: copied read-only target record, None when no lock or older than its 150 ms lifetime.
                tsample=self.weapon_lead.target_sample(ship=self.block.ship) if v['hud-target-relative'] and self.block.ship else None
                now=K.GetTickCount64();sample=self.hud_sample;source='sultan'
                motion=self._hud_motion(now)
                if sample is None or now-sample[0]>flight_hud.SAMPLE_MAX_AGE_MS:
                    # T87: Sultan disarmed (native fallback); the cave still records the rigid body.
                    sample=(motion['tick'],(motion['velocity'],motion['basis'],None)) if motion else None;source='native'
                frame=None
                if motion is not None and v['hud-planetary']:
                    frame=planetary_hud.planet_frame(motion['position'],self._hud_planet_list(now))
                    air=self.flight_consumer.latest.get('planetary') or {}
                    planet=planetary_hud.planetary_layout(motion,frame,v,getattr(getattr(self,'lift_trim',None),'value',0.),
                                                          air.get('clearance') if air.get('gear') else None)
                mode=(getattr(self,'eject_hint',None) or planetary_hud.condition_tag(self.flight_consumer.condition,v)
                      or planetary_hud.mode_tag(motion,planet,v))
                purge=planetary_hud.purge_plan(self.flight_consumer.condition)  # T93 ditch: warning block and purge graph
                landing=manual_landing.panel(self.flight_consumer.latest.get('planetary'),manual_landing.Touchdown.from_values(v),
                                             getattr(getattr(self,'touchdown',None),'last',None),v) if v.get('landing-manual') else None
                self.hud_live={'source':source if sample is not None else None,'mode':planetary_hud.owner(motion) if motion else None,
                               'planet':None if frame is None else {'index':frame['index'],'altitude':round(frame['altitude'],1),
                                                                     'height_base':round(frame['height_base'],1),'atmosphere':frame['atmosphere']}}
                if (readout or marker or gauge or gmeter or tsample or planet or mode or purge or landing) and sample is not None and now-sample[0]<=flight_hud.SAMPLE_MAX_AGE_MS:
                    camera=self.hud_reader.camera() if marker or tsample else None
                    plan=flight_hud.layout(sample[1],camera if marker else None,size[0],size[1],readout,marker)
                    target=flight_hud.target_readout(tsample,camera,size[0],size[1])
            if plan is None and not layers and not red>0.:return
            n=C.c_int32.from_address(ctx+0x42a8).value
            if not 1<=n<64:return
            state=ctx+0xa8+(n-1)*0x108;before=C.string_at(state,0x108);font=C.c_int32.from_address(state+0x104).value
            if not 0<=font<64:return
            api=self.ui_api;api['nvgSave'](ctx)
            try:
                api['nvgReset'](ctx);api['nvgFontFaceId'](ctx,font)
                fonts=self._hud_fonts(ctx);choice=v['hud-font']
                self.hud_painter.draw(ctx,plan,size[0],size[1],v['hud-scale'],fonts.get(choice,font),
                                      fonts if choice=='compare' else None,gauge,gmeter,target,planet,mode,purge,landing)
                if layers or red>0.:self.hud_painter.tunnel(ctx,layers,size[0],size[1],red=red)
            finally:
                api['nvgRestore'](ctx)
                if C.c_int32.from_address(ctx+0x42a8).value!=n or C.string_at(state,0x108)!=before:raise RuntimeError('NanoVG state differs after restore')
            self.counters['hud_draws']+=1
            if self.counters['hud_draws']==1:
                self.log(f'flight HUD first draw: frame {size[0]}x{size[1]}, state font {font}, fonts {self._hud_fonts(ctx)}, markers {[m[0] for m in plan["markers"]] if plan else []}')
        except BaseException as error:
            self.hud_errors+=1;self.log('flight HUD error: '+''.join(traceback.format_exception(error)))
            if self.hud_errors>=3:self.hud_state='disabled after repeated drawing errors';self.log('flight HUD disabled after repeated drawing errors')

    @manual_hook('AbsoluteHOTASNvgBegin',offset=0x1965EE0,func_def=FUNCDEF(None,[C.c_void_p,C.c_int32,C.c_int32,C.c_float]),detour_time='after')
    def nvg_begin(self,ctx,w,h,ratio):
        if getattr(self.ui,'manager',0):self.ui.ctx=ctx
        try:
            if self._in_hud_frame() and not getattr(self.ui,'hud_ctx',0):self.ui.hud_ctx=ctx;self.ui.hud_size=(w,h)
        except BaseException:self.hud_errors+=1
        return None

    @manual_hook('AbsoluteHOTASNvgEnd',offset=0x1965FA0,func_def=FUNCDEF(None,[C.c_void_p]),detour_time='before')
    def nvg_end(self,ctx):
        """Draw meters over their spacer rows; state must be byte-identical after nvgRestore (U3)."""
        try:
            if self._in_hud_frame() and ctx==getattr(self.ui,'hud_ctx',0):self._draw_hud(ctx)
        except BaseException:self.hud_errors+=1
        try:
            manager=getattr(self.ui,'manager',0)
            if not manager or ctx!=getattr(self.ui,'ctx',0) or self.draw_errors>=3 or not self.meters_enabled:return None
            if self.ui_layout is None:
                self.counters['draw_skips']+=1;return None
            if C.c_int32.from_address(manager+0x17028).value!=12:return None
            depth,kind=self._menu_depth()
            if kind!=CONTROLS_PAGE or depth not in self.menu.depth_routes or not self.menu.meters:return None
            n=C.c_int32.from_address(ctx+0x42a8).value
            if not 1<=n<64:return None
            state=ctx+0xa8+(n-1)*0x108;before=C.string_at(state,0x108);font=C.c_int32.from_address(state+0x104).value
            if not 0<=font<64:return None
            api=self.ui_api;api['nvgSave'](ctx)
            try:
                api['nvgReset'](ctx);api['nvgFontFaceId'](ctx,font);api['nvgTextAlign'](ctx,65)
                drawn=0
                for label,meter in list(self.menu.meters.items()):
                    root=self.roots.get(label)
                    geo=row_geometry(root,self.ui_layout,q,C.string_at) if root else None
                    g=panel_geometry(*geo) if geo else None
                    if g:self.painter.meter(ctx,g,meter);drawn+=1
                self.counters['draws']+=drawn
                if drawn and ('draw',depth) not in self.trace_seen:
                    self.trace_seen.add(('draw',depth));self.trace(f'first meters drawn at depth {depth}: {drawn}')
            finally:
                api['nvgRestore'](ctx)
                if C.c_int32.from_address(ctx+0x42a8).value!=n or C.string_at(state,0x108)!=before:raise RuntimeError('NanoVG state differs after restore')
        except BaseException as e:
            self.draw_errors+=1;self._ui_fail(e)
            if self.draw_errors>=3:self.log('meters disabled after repeated drawing errors; rows remain')
        return None

    # ---------- menu-facing API (wired by the native page) ----------
    def _poll_capture(self):
        """Finish a binding capture. done() must run outside self.lock: it takes the menu lock and
        assigns the binding, whose settings listener (_settings_changed) takes self.lock again.
        Lock order is always menu -> runtime (T51 rebind freeze, 2026-10-16)."""
        with self.lock:
            if self.capture is None:return
            result=self.capture.poll()
            if result is None:return
            done,self.capture,self.capture_done=self.capture_done,None,None
        self.trace(f'capture result: {result}')
        if done:done(result)

    def start_capture(self,done):
        kind=getattr(done,'kind','axis')
        with self.lock:
            key_down=lambda vk:bool(U.GetAsyncKeyState(vk)&0x8000)
            if kind=='key':self.capture=KeyCapture(key_down)
            elif kind=='button':self.capture=ClearableCapture(ButtonCapture(self.table,modifier=self.store.button_modifier),key_down)
            elif kind=='modifier':self.capture=ClearableCapture(ButtonCapture(self.table),key_down)
            else:self.capture=ClearableCapture(AxisCapture(self.table),key_down)
            self.capture_done=done
        self.trace(f'capture start: {kind}')

    def _menu_action(self,action):
        if action=='head-recentre' and getattr(self,'head',None) is not None:self.head.recentre()
        if action=='support-bundle':threading.Thread(target=self._save_support_bundle,name='AbsoluteHOTAS support bundle',daemon=True).start()

    def _save_support_bundle(self):
        """Menu button (T100): zip settings and recent logs off the game thread."""
        try:
            info={'game_build_hash':BUILD_HASH,'python':sys.version,'data':str(DATA),'state':self.status.get('state'),
                  'flight_model':self.store.values.get('flight-model')}
            path=support_bundle.save(DATA,info,ROOT/'config')
            self.log('support bundle saved: '+str(path))
        except Exception as error:self.log('support bundle failed: '+repr(error))

    def cancel_capture(self):
        with self.lock:self.capture=None;self.capture_done=None
        self.trace('capture cancelled')

    def shutdown(self,reason='shutdown'):
        import additive_rotation
        self.newtonian_refresh_requested=None
        self.stopping.set();self.active=False
        try:additive_rotation.disarm(self,reason)
        except BaseException as error:self.log('additive rotation disable failed: '+repr(error))
        agent=getattr(self,'agent_test',None)
        if agent is not None:
            try:agent.close()
            except Exception as error:self.log('agent shutdown failed: '+repr(error))
        aim=getattr(self,'ai_aim',None)
        if aim is not None:aim.close()
        if getattr(self,'aim_slowdown',None) is not None:self.aim_slowdown.stop()
        if getattr(self,'crash_damage',None) is not None:self.crash_damage.disable()
        if getattr(self,'alarms',None) is not None:self._alarms_shutdown()
        if getattr(self,'aiming',None) is not None:self.aiming.reset('shutdown')
        if getattr(self,'pip_presentation',None) is not None:self.pip_presentation.clear('shutdown')
        self.aim_pip_visible=None
        head=getattr(self,'head',None)
        if head is not None:
            try:head.stop()  # camera returns to native before anything else is torn down
            except BaseException as error:self.log('head tracking stop failed: '+repr(error))
        display=getattr(self,'cockpit_speed',None)
        if display is not None:
            try:self.log('cockpit speed restored: '+json.dumps(display.restore()))
            except BaseException as error:self.log('cockpit speed restore failed: '+repr(error))
        pilots=getattr(self,'ai_pilots',None)
        if pilots is not None:
            try:self.log('enemy pilots restored: '+json.dumps(pilots.restore()))
            except BaseException as error:self.log('enemy pilots restore failed: '+repr(error))
        combat=getattr(self,'ai_combat',None)
        if combat is not None:
            try:self.log('NPC beams/shields restored: '+json.dumps(combat.restore()))
            except Exception as error:self.log('NPC beams/shields restore failed: '+repr(error))
        sky=getattr(self,'black_space',None)
        if sky is not None:
            try:self.log('black space restored: '+json.dumps(sky.restore()))
            except Exception as error:self.log('black space restore failed: '+repr(error))
        terrain=getattr(self,'terrain',None)
        if terrain is not None:
            try:self.log('terrain look-ahead restored: '+json.dumps(terrain.restore()))
            except Exception as error:self.log('terrain look-ahead restore failed: '+repr(error))
        try:self.buttons.release(reason)  # never leave a key or mouse button down
        except BaseException:pass
        with self.newtonian_lock:
            self.translation_command=None
            if self.newtonian:
                try:self.newtonian.disable()
                except BaseException as error:self.log('shutdown disable failed: '+repr(error))
        try:
            if self.thread.is_alive() and threading.current_thread() is not self.thread:self.thread.join(1)
        except AttributeError:pass
        try:
            if self.box:self.box.disable()
        except BaseException:pass
        if (self.patches is not None or self.mav is not None or self.newtonian is not None
                or self.newtonian_failed or getattr(self,'aim_slowdown',None) is not None
                or getattr(self,'crash_damage',None) is not None
                or getattr(getattr(self,'rotation_validation',None),'state',None)=='restore failed'
                or getattr(getattr(self,'additive_rotation',None),'state',None)=='restore failed'):self._uninstall()
        if self.telemetry is not None:
            self.telemetry.close();self.log(f'telemetry: {self.telemetry.rows} rows in {self.telemetry.path.name}')
        if self.bus is not None:
            try:self.bus.stop()
            except BaseException:pass
        self.log('shutdown: '+reason)
