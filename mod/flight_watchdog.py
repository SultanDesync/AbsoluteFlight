"""T124 flight watchdog: notices a fail-safe that has stuck and asks the runtime to reset the flight model.

Every native cave hands the ship back to the game when one of its checks fails. That is right for a frame or for a
deliberate game regime (pulse, warp, landing, a menu, the Corvette autopilot). It is wrong when it lasts: the pilot
holds the stick over, the menu meters move, and the ship does nothing (Discord report 2026-11-13). This module is
the pure decision; the runtime gathers the inputs and performs the reset through the same detach and reinstall it
uses after a warp (`_request_pilot_recovery` / `_recover_pilot`).

Stuck means, while the runtime itself says flight is eligible and no deliberate regime is active:
  - a channel is armed and published off-centre, and fresh native receipts have not applied it for AXIS_SECONDS;
  - Sultan is selected and commanded, its cave runs every frame, and it has not applied for SULTAN_SECONDS;
  - the rotation host stopped itself after an error, or the Sultan install is parked for the session.

It never adds a condition to the enable path: a reset only re-runs the normal install. It cannot loop: resets back off
(BACKOFF), stop after MAX_RESETS for one ship, and the count clears only when the stuck thing is seen working again,
the ship changes, or GIVE_UP_SECONDS pass. A check that is working as written (not a latch) is not cleared by a reset;
the log line names it so a support bundle shows what to fix at the root.
"""
import dataclasses

NEUTRAL=.05          # published deflection that counts as the pilot asking for something
AXIS_SECONDS=2.      # commanded and unapplied this long on fresh receipts: stuck
SULTAN_SECONDS=3.    # Sultan selected, invoked and unapplied this long: stuck
LATCH_SECONDS=3.     # a parked install or a stopped rotation host seen this long: stuck
FORGET_SECONDS=5.    # an unapplied run not seen again for this long is dropped (the pilot let go)
ROTATION_OK_SECONDS=30. # a rebuilt rotation host that stays up this long has recovered
RECEIPT_GAP=.25      # longest receipt interval counted as unapplied time (matches the stall monitor)
BACKOFF=(10.,60.)    # earliest next reset after the first and after the second
MAX_RESETS=3
GIVE_UP_SECONDS=600. # after giving up, try again this much later

# Applied-mask bits in the six-axis receipt (frame+200). The MAV cave sets both strafe bits when either is commanded.
BITS={'yaw':1,'pitch':2,'roll':4,'throttle':8,'strafe-lateral':48,'strafe-vertical':48}
SULTAN_FLYING=('applied','helper_coast','expired_coast')
# Cave reasons that are the runtime's own doing or a deliberate safety, never a stuck fail-safe.
SULTAN_QUIET=('idle','native_disabled','height_safety_active','awaiting_invocation','unprepared')

REGIME_COLUMNS=('gate_autopilot','gate_landing','gate_73e0','gate_velocity_override','gate_native_boost','gate_7740',
                'gate_pulse','gate_warp')


def deliberate(state,planetary_on=False):
    """Names of the native regimes that own the ship on purpose, from raw field values keyed by their telemetry
    column (mod/flight_telemetry.py), so a telemetry row works too. A missing or blank key is not a regime.

    Known manual combat (1) and the four power selections are flown by the release, so they are not listed. Low
    height counts only while planetary flight is off, where Sultan hands back by design.
    """
    def value(key,default=0):
        v=state.get(key,default)
        return default if v in ('',None) else v
    out=[]
    if value('gate_active',1)!=1:out.append('inactive')
    out.extend(name[5:] for name in REGIME_COLUMNS if value(name))
    if value('frontend_state'):out.append('frontend')
    if value('combat_active')>1:out.append('combat')
    if value('gate_674c')>3:out.append('power')
    if value('gate_6720') and not planetary_on:out.append('low_height')
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class Action:
    kind:str     # 'reset' (detach and reinstall everything) or 'retry_sultan' (re-open the parked Sultan install)
    causes:tuple # what was stuck, for the pending set
    line:str     # the log line, without the 'watchdog: ' prefix


class FlightWatchdog:
    def __init__(self):
        self.context=None;self.resets=0;self.total=0;self.next_allowed=0.;self.gave_up=None;self.pending=set()
        self.lines=[];self.last_cause='';self.last_reset=None;self.rotation_ok=None
        self._clear()

    def _clear(self):
        self.unapplied={};self.last_seq=None;self.last_fresh=None
        self.sultan_since=None;self.sultan_reason='';self.sultan_invocation=None;self.sultan_seen=None
        self.latch_since={}

    def _recovered(self,cause):
        if cause in self.pending:
            self.pending.discard(cause)
            if not self.pending and self.resets:
                self.lines.append(f'recovered after reset {self.resets}: {cause} is working again')
                self.resets=0;self.next_allowed=0.;self.gave_up=None

    def step(self,now,*,eligible,context=None,regimes=(),receipt=None,published=None,sultan=None,rotation_latched=False):
        """One worker tick. Returns an Action or None.

        eligible: the runtime's own verdict (piloting, focused, no menu or capture, patches in, receipts fresh).
        context: the ship; a change clears the reset count.
        regimes: deliberate(); any entry means the game owns the ship and nothing is stuck.
        receipt: (sequence, applied mask, conflict) from the six-axis receipt, or None.
        published: {channel: value} for armed channels whose ramp has finished.
        sultan: None, or {'selected','installed','latched','invocation','reason'}.
        """
        if context!=self.context:
            self.context=context;self.resets=0;self.next_allowed=0.;self.gave_up=None;self.pending=set();self.rotation_ok=None;self._clear()
        if self.gave_up is not None and now-self.gave_up>=GIVE_UP_SECONDS:
            self.gave_up=None;self.resets=0;self.next_allowed=0.
        if not eligible or regimes:
            self._clear();return None
        stuck=[]
        if receipt is not None and receipt[0]!=self.last_seq:
            seq,mask,conflict=receipt
            dt=min(RECEIPT_GAP,now-self.last_fresh) if self.last_fresh is not None else 0.
            self.last_seq=seq;self.last_fresh=now
            if conflict:self.unapplied.clear()  # the runtime's own conflict handling owns this
            else:
                for channel,value in (published or {}).items():
                    if mask&BITS[channel]:
                        self.unapplied.pop(channel,None);self._recovered(channel)
                    elif abs(value)>NEUTRAL:
                        run=self.unapplied.setdefault(channel,[0.,now]);run[0]+=dt;run[1]=now
        for channel,(seconds,seen) in list(self.unapplied.items()):
            if now-seen>FORGET_SECONDS:del self.unapplied[channel]
            elif seconds>=AXIS_SECONDS:stuck.append((channel,f'{channel} commanded but not applied for {seconds:.1f} s'))
        latched=[]
        if sultan and sultan.get('selected'):
            if sultan.get('latched'):latched.append(('sultan','Sultan install parked after a failure'))
            elif sultan.get('installed'):
                invocation=sultan.get('invocation');reason=sultan.get('reason') or ''
                if invocation is not None and invocation!=self.sultan_invocation:
                    self.sultan_invocation=invocation;self.sultan_seen=now
                    if reason in SULTAN_FLYING:self.sultan_since=None;self._recovered('sultan')
                    elif reason in SULTAN_QUIET:self.sultan_since=None
                    else:
                        if self.sultan_since is None:self.sultan_since=now
                        self.sultan_reason=reason
                elif self.sultan_seen is None or now-self.sultan_seen>RECEIPT_GAP:
                    self.sultan_since=None  # the cave is not being invoked: the game is on another route
                if self.sultan_since is not None and now-self.sultan_since>=SULTAN_SECONDS:
                    stuck.append(('sultan',f'Sultan not applied for {now-self.sultan_since:.1f} s ({self.sultan_reason})'))
        else:self.sultan_since=None
        if rotation_latched:latched.append(('rotation','rotation style stopped after an error'))
        for cause in [c for c in self.latch_since if c not in dict(latched)]:del self.latch_since[cause]
        # A fresh rotation host is not proof it works: it counts as recovered only after staying up for a while.
        if rotation_latched:self.rotation_ok=None
        elif 'rotation' in self.pending:
            if self.rotation_ok is None:self.rotation_ok=now
            elif now-self.rotation_ok>=ROTATION_OK_SECONDS:self.rotation_ok=None;self._recovered('rotation')
        for cause,text in latched:
            since=self.latch_since.setdefault(cause,now)
            if now-since>=LATCH_SECONDS:stuck.append((cause,text))
        if not stuck or self.gave_up is not None or now<self.next_allowed:return None
        text='; '.join(t for _,t in stuck);causes=tuple(c for c,_ in stuck)
        if self.resets>=MAX_RESETS:
            self.gave_up=now
            self.lines.append(f'giving up after {self.resets} resets, still stuck: {text}. Flight carries on as it is; '
                              f'next try in {GIVE_UP_SECONDS/60:.0f} min or on a ship change')
            return None
        self.resets+=1;self.total+=1;self.last_cause=text;self.last_reset=now;self.pending.update(causes)
        self.next_allowed=now+BACKOFF[min(self.resets,len(BACKOFF))-1]
        kind='retry_sultan' if causes==('sultan',) and sultan and sultan.get('latched') else 'reset'
        self._clear()
        return Action(kind,causes,f'{text}; '+('retrying the Sultan install' if kind=='retry_sultan' else 'resetting the flight model')
                      +f' ({self.resets} of {MAX_RESETS})')

    def drain(self):
        lines,self.lines=self.lines,[]
        return lines

    def status(self):
        return {'resets':self.total,'resets_this_ship':self.resets,'last_cause':self.last_cause,
                'waiting_on':sorted(self.pending),'gave_up':self.gave_up is not None,
                'unapplied':{c:round(s,2) for c,(s,_) in self.unapplied.items()}}
