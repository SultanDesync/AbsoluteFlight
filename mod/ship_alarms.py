"""T95 ship alarms: the ship computer's voice, alarm sirens, red cockpit alarm lights and the engine sound when
the ship is in trouble: a ditch at sea (T93 thruster purge) and, when T94 lands, a crash. Pure cue logic; no
game memory (tools/native_alarms.py carries the commands out on the flight thread).

The worker thread reports moments (ditch, wake, begin, stage, boost, miss, relit, wrecked, crash hits, end) and
the hull share; tick() turns due cues into commands on a queue the flight thread drains:
('post', object, event id, name) and ('engine', on). `light` is the alarm light pulse rate (per second) the
flight thread holds each frame, or None when the lights are the game's own.

T99: with the rendered purge lines loaded (use_voice, mod/purge_voice.py), Voice and alarms speaks those during a
ditch instead of the game's lines: ('say', clip, offset s) and ('hush',) on `speech`, which the worker plays
itself (mod/voice_player.py). Countdown lines start at a stage change, timed so "one" opens the band.

Events are the game's own Wwise events by name (ID = FNV-1 32 of the lower-case name), all in the always-loaded
banks NMS_Audio_Persistent and Vocal_Localised (soundbanksinfo.xml, build 180836). Spoken wording is a GUESS
from file names until heard (research/re-log/2026-10-19_claude_ditch-ambience.md). The game has no lines for
purge, thrusters, throttle or boost.
"""
from collections import deque

import purge_voice

SHIP, HUD = 'ship', 'hud'  # the ship's 3D audio object [ship+0x6268]; the HUD/UI object [M+0x7330F8] (183791)
VOICE, ALARMS, OFF = 'voice', 'alarms', 'off'
SOUND_CHOICES = ((VOICE, 'Voice and alarms'), (ALARMS, 'Alarms only'), (OFF, 'Off'))


def wwise_id(name):
    """Wwise short ID of an event name: FNV-1 32-bit over the lower-case name."""
    h = 2166136261
    for byte in name.lower().encode('ascii'):
        h = (h*16777619) & 0xFFFFFFFF
        h ^= byte
    return h


LOOPS = {'siren': ('UI_Alert_Damage_Warning_Critical_Lp', 'UI_Alert_Damage_Warning_Critical_Lp_Stop'),
         'low': ('Pl_Damage_Warning_Low_Health_Lp', 'Pl_Damage_Warning_Low_Health_Lp_Stop')}
CHIRP = 'UI_Next_Message'
# Engines back on the way vanilla takeoff does it (0x17A275A..0x17A27FF, 183791): Pl_Ship_Takeoff on the ship plays the engine
# loop again (and the launch sound), then the engine switch posts Pl_Ship_Engines_Start. Engines_Start alone only resets
# the loop's pitch and plays the start-up: Engines_Stop had stopped the loop (bank NMS_Audio_Persistent, v154 HIRC;
# 2026-10-20 flight: no engine sound after a relight).
ENGINE_LOOP = 'Pl_Ship_Takeoff'

# Moment -> cues (delay s, kind, what). Kinds: 'sound' (an alarm one-shot on the HUD object), 'ship' (a one-shot
# on the ship's object), 'voice' (a ship computer line, queued so lines never overlap), 'say' (T99: our rendered
# line, replacing the game's 'voice' lines of a ditch when loaded; COUNT = the countdown of the stage starting),
# 'loop'/'unloop' (a looping alarm in LOOPS and its stop), 'engine' (the game's engine sound switch, on/off).
# User proposal 2026-10-19 (T95).
COUNT = 'count'
CUES = {
    'ditch': ((0., 'engine', False),),                                      # impact, blackout: engines die
    'wake': ((0., 'sound', 'Pl_Damage_Warning_Shield_Loss'), (0., 'voice', 'NOTIFY_SHIELD_DOWN'),
             (1.5, 'loop', 'siren'), (0., 'voice', 'VO_Tech_Offline'), (0., 'say', 'warning')),  # pilot comes to: warning
    'begin': ((0., 'sound', CHIRP), (0., 'voice', 'NOTIFY_WARNING'), (0., 'say', COUNT)),  # MAINS countdown
    'stage': ((0., 'ship', 'Pl_Ship_Engine_Ignition_Start'), (0., 'say', COUNT)),  # a group purged, next countdown
    'boost': ((0., 'ship', 'Pl_Ship_Engine_Ignition_Start'), (.5, 'ship', 'Pl_Ship_Boost_PowerUp'), (0., 'say', COUNT)),
    'go': ((.45, 'sound', CHIRP),),                                          # countdown over, the window opens (after "one")
    'ignite': ((0., 'ship', 'Pl_Ship_Engine_Ignition_Start'),),              # thrust rose into the band in the window
    'miss': ((0., 'ship', 'Pl_Ship_Engine_Ignition_Fail'), (0., 'sound', 'Pl_Hazard_Critical_Hit'),
             (0., 'voice', 'NOTIFY_DamageSustained'), (0., 'say', 'miss')),
    'hull_half': ((0., 'voice', 'Notify_TechnologyCriticallyDamaged'), (0., 'say', 'hull_half')),
    'hull_quarter': ((0., 'loop', 'low'), (0., 'say', 'hull_quarter')),
    'relit': ((0., 'unloop', 'siren'), (0., 'unloop', 'low'), (0., 'engine', True), (.3, 'voice', 'VO_Tech_Repaired'),
              (.3, 'say', 'relit')),
    'wrecked': ((0., 'unloop', 'siren'), (0., 'unloop', 'low'), (0., 'ship', 'Pl_Ship_Engine_Ignition_Fail'),
                (.4, 'voice', 'EQUIP_DAMAGED'), (.4, 'say', 'wrecked')),
    # T94 crashes (wired when T94 lands): the game already plays the impact itself.
    'crash_hit': ((0., 'voice', 'NOTIFY_DamageSustained'),),
    'crash_disabled': ((0., 'sound', 'Pl_Hazard_Critical_Hit'), (0., 'voice', 'EQUIP_DAMAGED'), (0., 'say', 'crash_disabled')),
}
RENDERED_CRASH = frozenset(['crash_disabled'])  # crash moments with a rendered line of their own
VOICE_GAP = 2.6       # s between ship computer lines (the longest native lines run about 2 s)
VOICE_BACKLOG = 2     # lines waiting beyond this are dropped: late lines describe a moment already gone
REPEAT_GAP = {'NOTIFY_DamageSustained': 5., 'miss': 5.}  # s before the same line may play again (user proposal: at most every 5 s)
SAY_TAIL = .3         # s after one of our lines before another line may follow
OUTCOMES = ('relit', 'wrecked', 'crash_disabled')  # their lines replace any waiting line: the news is out of date
# Alarm light pulses per second (native AlarmRate 1.0): steady 1 while the purge runs, toward 2 as the hull goes,
# slow once wrecked, a short fast burst for a crash hit.
LIGHT_RATE, LIGHT_FAST, LIGHT_WRECKED = 1., 2., .5
CRASH_LIGHT_S = 3.
HULL_HALF, HULL_QUARTER = .5, .25


class ShipAlarms:
    def __init__(self, sounds=VOICE, lights=True):
        self.commands = deque()  # worker appends, flight thread pops: ('post', object, id, name) | ('engine', on)
        self.speech = deque()    # T99, worker only: ('say', clip, offset s) | ('hush',)
        self.lengths = {}        # rendered line -> seconds; empty: the game's own lines
        self.sounds, self.lights = sounds, bool(lights)
        self.light = None
        self._reset()

    def _reset(self):
        self.pending = []          # (due s, order, kind, what)
        self.voices = deque()      # voice lines waiting for the voice to be free
        self.voice_free = 0.
        self.said = {}             # line -> last time spoken
        self.looping = set()
        self.episode = None        # 'ditch' | 'crash' while ours, else None
        self.woke = self.wrecked = False
        self.marks = set()         # hull thresholds already announced this episode
        self.hull_share = 1.
        self.burst_until = None
        self.engine_off = False
        self.order = 0
        self.light = None
        self.paused = False
        self.paused_at = (0., 0)   # (s, cue order) when the pause began: cues queued before it wait as long
        self.resume = set()        # loops to restart when a pause ends
        self.saying = None         # (clip, started s) of our line playing
        self.resume_say = None     # (clip, offset s) cut by a pause, to pick up where it stopped

    def use_voice(self, lengths):
        """The rendered lines that loaded ({clip: seconds}); None or {} goes back to the game's lines."""
        self.lengths = dict(lengths or {})

    def configure(self, sounds, lights):
        """Menu values. Turning sounds off stops any running alarm at once."""
        sounds = sounds if sounds in (VOICE, ALARMS, OFF) else VOICE
        if sounds == OFF and self.sounds != OFF:self._stop_loops()
        if sounds != VOICE:
            self.voices.clear(); self._hush()
        self.sounds, self.lights = sounds, bool(lights)

    @property
    def active(self):
        return self.episode is not None or bool(self.looping) or self.light is not None

    def moment(self, name, now, stage=None):
        """A moment of a ditch or crash: queue its cues and move the light state. stage: the purge stage starting
        (begin, stage, boost), for its countdown line."""
        if name == 'end':return self.end(now)
        if name == 'ditch':
            self.end(now, engine=False); self.episode = 'ditch'
        elif name == 'wake':
            if self.woke or self.episode != 'ditch':return
            self.woke = True
        elif name in ('crash_hit', 'crash_disabled'):
            if self.episode is None:self.episode = 'crash'
            self.burst_until = now+CRASH_LIGHT_S
        elif self.episode is None:return
        if name == 'wrecked':self.wrecked = True
        if name in OUTCOMES:self.voices.clear()
        # Our lines replace the game's in a ditch, and for a disabling crash (user 2026-10-25); other crash hits keep the game's.
        rendered = bool(self.lengths) and (self.episode == 'ditch' or name in RENDERED_CRASH)
        for delay, kind, what in CUES.get(name, ()):
            if kind == 'voice' and rendered or kind == 'say' and not rendered:continue
            if what == COUNT:
                if stage is None:continue
                what = purge_voice.countdown_clip(stage)
            self.order += 1; self.pending.append((now+delay, self.order, kind, what))
        if name == 'relit':self.episode = None; self.woke = False; self.marks.clear()

    def hull(self, share, now):
        """The purge hull meter (0..1): announces the half and quarter marks once per episode."""
        if self.episode != 'ditch' or not self.woke or self.wrecked:return
        self.hull_share = share
        for mark, name in ((HULL_HALF, 'hull_half'), (HULL_QUARTER, 'hull_quarter')):
            if share < mark and name not in self.marks:
                self.marks.add(name); self.moment(name, now)

    def pause(self, paused, now):
        """The purge is frozen (game menu, blackout): the loops stop and the lights go back to the game until it
        runs again; cues wait."""
        paused = bool(paused)
        if paused == self.paused:return
        self.paused = paused
        if paused:
            self.resume = set(self.looping); self._stop_loops(); self.paused_at = (now, self.order)
            if self.saying is not None:
                clip, started = self.saying
                if now-started < self.lengths.get(clip, 0.):self.resume_say = (clip, now-started)
                self._hush()
        else:
            for loop in sorted(self.resume):self._cue('loop', loop, now)
            self.resume = set()
            # The purge stood still, so the cues wait as long; a cut line picks up where it stopped (on time).
            held = max(0., now-self.paused_at[0])
            self.pending = [(due+held if order <= self.paused_at[1] else due, order, kind, what)
                            for due, order, kind, what in self.pending]
            self.voice_free += held
            for clip, last in list(self.said.items()):self.said[clip] = last+held
            if self.resume_say is not None and self.sounds == VOICE:
                clip, offset = self.resume_say
                self.speech.append(('say', clip, offset)); self.saying = (clip, now-offset)
            self.resume_say = None

    def end(self, now=0., engine=True):
        """Eject, leaving the ship, setting off, mod off: stop every loop and give the lights back. The engine
        sound comes back only if we cut it and the ship was not abandoned (engine=False on eject)."""
        self._stop_loops(); self._hush()
        if engine and self.engine_off and self.sounds != OFF:self._engines_on()
        self._reset()

    def _hush(self):
        if self.saying is not None:self.speech.append(('hush',))
        self.saying = None

    def _stop_loops(self):
        for loop in sorted(self.looping):
            name = LOOPS[loop][1]; self.commands.append(('post', HUD, wwise_id(name), name))
        self.looping.clear()

    def tick(self, now):
        """Release due cues as commands and update the light state."""
        if self.paused:
            self.light = None;return
        due = sorted(p for p in self.pending if p[0] <= now)
        if due:self.pending = [p for p in self.pending if p[0] > now]
        for _, _, kind, what in due:self._cue(kind, what, now)
        if self.voices and now >= self.voice_free and self.sounds == VOICE:
            name = self.voices.popleft(); self.said[name] = now; self.voice_free = now+VOICE_GAP
            self.commands.append(('post', HUD, wwise_id(name), name))
        self.light = self._light_rate(now)

    def _cue(self, kind, what, now):
        if kind == 'engine':
            # The engine switch is part of the sound: Off leaves the game's engine sound alone.
            if self.sounds == OFF:return
            if not what:
                self.engine_off = True; self.commands.append(('engine', False))
            elif self.engine_off:
                self.engine_off = False; self._engines_on()
            return
        if self.sounds == OFF:return
        if kind == 'say':
            self._say(what, now);return
        if kind == 'voice':
            if self.sounds != VOICE:return
            last = self.said.get(what)
            if last is not None and now-last < REPEAT_GAP.get(what, 0.):return
            if what in self.voices or len(self.voices) >= VOICE_BACKLOG:return
            self.voices.append(what);return
        if kind == 'loop':
            if what in self.looping:return
            self.looping.add(what); name = LOOPS[what][0]
        elif kind == 'unloop':
            if what not in self.looping:return
            self.looping.discard(what); name = LOOPS[what][1]
        else:name = what
        self.commands.append(('post', SHIP if kind == 'ship' else HUD, wwise_id(name), name))

    def _say(self, clip, now):
        """Our rendered line: countdowns and outcomes cut in at once; the rest only when the voice is free."""
        length = self.lengths.get(clip)
        if self.sounds != VOICE or length is None:return
        if clip not in purge_voice.INTERRUPTS:
            if now < self.voice_free:return
            last = self.said.get(clip)
            if last is not None and now-last < REPEAT_GAP.get(clip, 0.):return
        self.voices.clear(); self.said[clip] = now
        self.speech.append(('say', clip, 0.)); self.saying = (clip, now)
        self.voice_free = now+length+SAY_TAIL

    def _engines_on(self):
        self.commands.append(('post', SHIP, wwise_id(ENGINE_LOOP), ENGINE_LOOP)); self.commands.append(('engine', True))

    def _light_rate(self, now):
        if not self.lights:return None
        if self.burst_until is not None:
            if now < self.burst_until:return LIGHT_FAST
            self.burst_until = None
            if self.episode == 'crash':self.episode = None
        if self.episode != 'ditch' or not self.woke:return None
        if self.wrecked:return LIGHT_WRECKED
        share = min(1., max(0., self.hull_share))
        if share >= HULL_HALF:return LIGHT_RATE
        return LIGHT_RATE+(LIGHT_FAST-LIGHT_RATE)*(HULL_HALF-share)/HULL_HALF

    def status(self):
        return {'episode': self.episode, 'sounds': self.sounds, 'lights': self.lights, 'light_rate': self.light,
                'paused': self.paused, 'looping': sorted(self.looping), 'engine_off': self.engine_off, 'voices_waiting': len(self.voices),
                'voice': 'rendered' if self.lengths else 'game', 'saying': self.saying[0] if self.saying else None}
