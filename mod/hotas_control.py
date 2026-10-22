"""Settings + device table -> the six D12 mailbox values, with per-channel neutral arming.

No hotkeys. A channel drives the ship only while the whole runtime is eligible (master switch
on, piloting, game focused, no menu or capture open) and that channel has an armed, fresh
input. A channel disarms the moment its input becomes unusable or eligibility drops. Stick channels
(rotation, strafe) re-arm from wherever the stick is, ramped in over ARM_RAMP seconds, so nothing
jolts. They wait for centre only after NEEDS_CENTRE events, where the axis changes meaning: a rebind
or inversion, or strafe switched on. Live 2026-10-16: the old rule (arm only at centre) left a held
or slightly off-centre pitch dead at load and after every menu, which also stops the flight clock
('not piloting'). Live 2026-10-19: the same rule after a player-ship reacquire (launch, station
exit) left a held pull-up dead until the stick passed centre; reacquires now ramp in too.
The throttle keeps its own neutral rules (ThrottleChannel), and yields to the game while native
throttle is active (launch/station auto-boost holds native u=1). Unarmed channels publish 0, which the native caves treat as
"leave this axis to the game", so one missing device never takes the others down.
"""
import math
from flight_shaping import LATERAL_COMMAND_SIGN,ThrottleChannel,shape_axis
from hotas_settings import strafe_enabled

MAILBOX_ORDER=('yaw','pitch','roll','throttle','strafe-lateral','strafe-vertical')
ROTATION=('yaw','pitch','roll')
STRAFE=('strafe-lateral','strafe-vertical')
NEUTRAL=.02
# Disarm reasons after which a stick channel re-arms only at centre: the axis changed meaning
# (rebind, inversion, strafe switched on). A ship change or reacquire does not: the absolute stick
# means the same thing in the new ship, and the ARM_RAMP keeps the resume smooth (user, 2026-10-19).
NEEDS_CENTRE=frozenset(('binding changed','vanilla flight model'))
NATIVE_THROTTLE=.0001  # |native u| above this: the game is driving throttle (matches the cave's idle limit)
STALE_SECONDS=.25  # native receipts older than this mean the game is not consuming our axes
ARM_RAMP=.3  # seconds from 0 to the full deflection when a stick channel arms off-centre
NATIVE_LIMITS=(1.,1.,60.)  # mailbox authority qualified in the D16 full-native amendment
# Never publish throttle u=1 exactly: the native speed smoothing time is (1-u)*20 (E5), and at 0 it
# leaves a stale smooth-damp velocity at ship+0x6700 that launches the speed demand far past the
# target on the next partial throttle (T52 telemetry 2026-10-16: 651 and -873 against target 101).
# 0.995 keeps the smoothing time at 0.1 s and costs about 0.5% of top speed.
THROTTLE_MAX_U=.995
# The smoothing time (1-u)*20 is stiff near full and slack at partial throttle. Dropping u suddenly
# leaves the velocity built under the stiff spring to a slack one, and the demand overshoots by
# thousands (T52 verification flight: flicks to full and back gave 4638, -10662, 71064). Rises pass
# straight through; on a fall (1-u), and so the smoothing time, may grow at most e^RATE per second.
# Offline model of the native helper: overshoot gone at 30/60/144 fps; full->50% takes 0.93 s.
THROTTLE_RELEASE_RATE=5.


class StallMonitor:
    """Classifies native receipt stalls and logs each in-flight one with its context.

    A game menu or dialog (frontend flag nonzero) pauses the flight update by design, so it is
    'game menu' and not logged. Any other stall is 'native receipts stale': logged once when it
    ends, with the context sampled when it began (combat, zone and power flags, flight state), so
    in-flight causes (combat start, speed zones, boost, pulse) can be told apart (user, 2026-10-16).
    Stick channels re-arm off-centre with a ramp either way.
    """
    def __init__(self):self.since=None;self.kind=None;self.context=None;self.lines=[]

    def check(self,now,seq_time,armed,frontend,context=lambda:{}):
        """Disarm reason for this tick, or None. frontend: callable returning the flag or None."""
        stale=now-seq_time>STALE_SECONDS
        if not stale:
            if self.since is not None and self.kind=='native receipts stale':
                self.lines.append(f'receipts stalled in flight for {now-self.since:.2f} s; at start: {self.context}')
            self.since=self.kind=self.context=None
            return None
        if not armed and self.since is None:return None
        flag=frontend()
        kind='game menu' if flag else 'native receipts stale'
        if self.since is None or kind!=self.kind:
            if self.since is not None and self.kind=='native receipts stale':
                self.lines.append(f'receipts stalled in flight for {now-self.since:.2f} s, then a game menu; at start: {self.context}')
            self.since=seq_time;self.kind=kind
            try:self.context=context() if kind=='native receipts stale' else None
            except Exception as error:self.context={'context_error':repr(error)}
        return kind

    def drain(self):
        lines,self.lines=self.lines,[]
        return lines


def release_limited(previous,wanted,dt):
    """Throttle u to publish: wanted when rising, else the slowest fall the rule allows."""
    if wanted>=previous:return wanted
    return max(wanted,1.-(1.-previous)*math.exp(THROTTLE_RELEASE_RATE*dt))


class FlightControl:
    def __init__(self,values):
        self.values=dict(values);self.armed={c:False for c in MAILBOX_ORDER}
        self.throttle=ThrottleChannel(self.values);self.reasons={};self.throttle_u=0.
        self.centre=set()  # stick channels that must pass centre before arming (NEEDS_CENTRE)
        self.ramp={}  # channel -> ramp-in progress 0..1 after arming off-centre

    def reconfigure(self,values,changed=()):
        if 'flight-model' in changed:self.disarm_all('flight model changed')
        rebound={c for c in MAILBOX_ORDER if f'bind-{c}-axis' in changed or f'{c}-inverted' in changed}
        self.values=dict(values);self.throttle.reconfigure(self.values)
        for c in rebound:self._disarm(c,'binding changed')

    def _disarm(self,channel,reason):
        if channel=='throttle':self.throttle.disarm();self.throttle_u=0.
        if reason in NEEDS_CENTRE:self.centre.add(channel)
        self.armed[channel]=False;self.reasons[channel]=reason;self.ramp.pop(channel,None)

    def disarm_all(self,reason):
        for c in MAILBOX_ORDER:self._disarm(c,reason)

    def step(self,table,eligible,dt,native_u=None,reason='not eligible'):
        """Returns the six mailbox values in MAILBOX_ORDER.

        native_u: the game's own throttle demand this frame (receipt, before substitution). While it
        is nonzero the game owns throttle (auto-boost on launch or station exit, keyboard thrust):
        throttle publishes 0 without disarming its channel, and the stick axes stay live.
        """
        v=self.values
        if not eligible or not v['flight-controls-enabled']:
            self.disarm_all(reason if v['flight-controls-enabled'] else 'switched off')
            return (0.,)*6
        out=dict.fromkeys(MAILBOX_ORDER,0.)
        strafing=strafe_enabled(v)
        for channel in ROTATION+STRAFE:
            if channel in STRAFE and not strafing:
                self._disarm(channel,'vanilla flight model');continue
            unit=table.unit(v[f'bind-{channel}-axis'])
            if unit is None:
                self._disarm(channel,'unbound' if v[f'bind-{channel}-axis'] is None else 'device missing or stale');continue
            shaped=shape_axis(v,channel,unit)
            if not self.armed[channel]:
                if channel in self.centre:
                    if abs(shaped)>NEUTRAL:self.reasons[channel]='waiting for centre';continue
                    self.centre.discard(channel)
                self.armed[channel]=True
                if abs(shaped)>NEUTRAL:self.ramp[channel]=0.;self.reasons[channel]='arming'
                else:self.reasons[channel]='armed'
            k=1.
            if channel in self.ramp:
                k=self.ramp[channel]=min(1.,self.ramp[channel]+max(0.,dt)/ARM_RAMP)
                if k>=1.:del self.ramp[channel];self.reasons[channel]='armed'
            out[channel]=shaped*k*(v['strafe-authority'] if channel in STRAFE else 1.)*(LATERAL_COMMAND_SIGN if channel=='strafe-lateral' else 1.)
        norm=math.hypot(out['strafe-lateral'],out['strafe-vertical'])
        if norm>v['strafe-authority']:
            for c in STRAFE:out[c]*=v['strafe-authority']/norm
        unit=table.unit(v['bind-throttle-axis'])
        if native_u is not None and math.isfinite(native_u) and abs(native_u)>NATIVE_THROTTLE:
            # Hand over without disarming; resume from the game's u so release_limited eases the
            # fall from the auto-boost's u=1 instead of dropping into a slack smoothing spring.
            self.armed['throttle']=False;self.reasons['throttle']='game throttle active'
            self.throttle_u=min(THROTTLE_MAX_U,max(0.,native_u))
            return self._finish(out)
        cmd=self.throttle.step(unit,dt,native_u)
        if cmd is None:
            self.armed['throttle']=False;self.throttle_u=0.
            self.reasons['throttle']=('unbound' if v['bind-throttle-axis'] is None else 'device missing or stale') if unit is None else 'waiting for neutral'
        else:
            self.armed['throttle']=True;self.reasons['throttle']='armed'
            wanted=min(THROTTLE_MAX_U,max(0.,cmd.thrust))  # brake lane not qualified: back half holds idle
            self.throttle_u=out['throttle']=release_limited(self.throttle_u,wanted,dt)
        return self._finish(out)

    def _finish(self,out):
        values=tuple(float(out[c]) for c in MAILBOX_ORDER)
        if not all(math.isfinite(x) for x in values):
            self.disarm_all('non-finite output');return (0.,)*6
        return values
