"""D21 bus-only test routing, isolated from the release runtime and user settings.

Only explicit virtual:agent-0 bindings and frames are accepted. This adapter runs
the ordinary FlightControl and ButtonRouter on an in-memory settings dictionary;
it cannot emit input, write settings, install hooks, or deliver a native action.
L2 descriptions describe the catalog, never a qualified live delivery lane.
"""
import json
import math
from pathlib import Path
import time

from hotas_buttons import ButtonRouter, action as catalog_action, coerce_binding, find_conflict, canonical as digital_token
from hotas_control import FlightControl, MAILBOX_ORDER
from hotas_input import DeviceTable
from hotas_settings import BY_ID, coerce, defaults, parse_token

DEVICE='virtual:agent-0'


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError(f'duplicate test binding field: {key}')
        result[key]=value
    return result


def _axis_token(token):
    value=coerce(BY_ID['bind-pitch-axis'],token)
    if value is None or parse_token(value)[0]!=DEVICE:
        raise ValueError('agent test axes must use virtual:agent-0')
    return value


def _button_token(token):
    value=digital_token(token)
    if value.split('@',1)[0]!=DEVICE:
        raise ValueError('agent test buttons must use virtual:agent-0')
    return value


class AgentRouting:
    """A dry consumer of copied bus frames. No reference to the user's store exists.

    ``resolve`` returns a token for a configured semantic alias/action or an
    explicit agent token. ``describe`` is the pure L2 metadata API. ``step``
    exercises real shaping/router logic; ``observe`` supplies the clock/step.
    ``neutral`` discards all arming, counters, pulses and toggle latches.
    """
    def __init__(self,bindings,now=time.monotonic):
        if not isinstance(bindings,dict) or set(bindings)-{'axes','buttons'}:
            raise ValueError('test bindings must contain only axes and buttons')
        axes=bindings.get('axes',{})
        buttons=bindings.get('buttons',[])
        if not isinstance(axes,dict) or set(axes)-set(MAILBOX_ORDER):
            raise ValueError('unknown test axis action')
        if not isinstance(buttons,list):raise ValueError('test buttons must be a list')
        self.axes={name:_axis_token(token) for name,token in axes.items()}
        if len(set(self.axes.values()))!=len(self.axes):
            raise ValueError('test axis mapping is ambiguous: token reused')
        self.bindings=[];self.actions={};used=set()
        for raw in buttons:
            binding=coerce_binding(raw)
            token=_button_token(binding.token)
            if binding.action in self.actions or token in used or find_conflict(self.bindings,binding) is not None:
                raise ValueError('test button mapping is ambiguous')
            self.bindings.append(binding);self.actions[binding.action]=token;used.add(token)
        # The master switch enables only this dry model. Every output lane stays absent.
        self.values=defaults()
        self.values['flight-controls-enabled']=True
        self.values['key-output-enabled']=False
        self.values['cursor-stick-enabled']=False
        for name,token in self.axes.items():
            key=f'bind-{name}-axis'
            self.values[key]=coerce(BY_ID[key],token)
        self._now=now;self._clock=0.;self._last=None
        self.neutral()

    @classmethod
    def from_file(cls,path,now=time.monotonic):
        data=json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=_unique_object)
        return cls(data,now=now)

    def resolve(self,action):
        """Resolve an explicit token or unique configured action without delivery."""
        if not isinstance(action,str):raise ValueError('action must be text')
        if action in self.axes:return self.axes[action]
        catalog=catalog_action(action)
        if catalog is not None and catalog.id in self.actions:return self.actions[catalog.id]
        if '@axis:' in action:return _axis_token(action)
        if '@' in action:return _button_token(action)
        raise ValueError(f'unbound test action: {action}')

    def describe(self,action=None):
        """Pure L2 catalog metadata. Native catalog membership grants no authority."""
        if action is None:
            return {'level':'L2','delivery':False,'axes':dict(self.axes),
                    'buttons':[self.describe(name) for name in self.actions]}
        token=self.resolve(action)
        catalog=catalog_action(action)
        kind='axis' if '@axis:' in token else 'button'
        lane='axis' if kind=='axis' else (catalog.lane if catalog is not None else 'unresolved')
        return {'level':'L2','action':catalog.id if catalog is not None else action,
                'token':token,'kind':kind,'lane':lane,'qualified':False,'delivery':False,
                'state':'native unqualified' if lane=='native' else 'dry only'}

    def require_delivery(self,action):
        info=self.describe(action)
        if info['lane']=='native':raise ValueError('native test delivery is unqualified')
        raise ValueError('agent routing is bus-only; live delivery is disabled')

    def neutral(self):
        """Hard reset every test state, including toggled mod actions."""
        self.table=DeviceTable(now=lambda:self._clock)
        self.control=FlightControl(self.values)
        self.router=ButtonRouter(self.bindings)
        self._last=None
        self.latest={'dry_run':True,'delivery':False,
                     'axes':dict.fromkeys(MAILBOX_ORDER,0.),'armed':dict(self.control.armed),
                     'reasons':{},'buttons':{'held':[],'pressed':[],'released':[]}}
        return self.latest

    disarm=neutral

    def step(self,frames,now,dt):
        """Return JSON-safe dry outputs; unrelated hardware frames are excluded."""
        if (isinstance(now,bool) or isinstance(dt,bool) or not isinstance(now,(int,float))
                or not isinstance(dt,(int,float)) or not math.isfinite(now) or not math.isfinite(dt)
                or dt<0 or (self._last is not None and now<self._last)):
            raise ValueError('dry routing requires finite monotonic time and nonnegative dt')
        if not isinstance(frames,dict):raise ValueError('bus frames must be an object')
        self._clock=float(now);self._last=float(now)
        self.table.update({DEVICE:frames[DEVICE]} if DEVICE in frames else {})
        axes=self.control.step(self.table,True,float(dt))
        buttons=self.router.step(self.table.digital,float(now),eligible=True)
        self.latest={'dry_run':True,'delivery':False,
                     'axes':dict(zip(MAILBOX_ORDER,axes)),'armed':dict(self.control.armed),
                     'reasons':dict(self.control.reasons),
                     'buttons':{'held':sorted(buttons.held),'pressed':list(buttons.pressed),
                                'released':sorted(buttons.released)}}
        return self.latest

    def observe(self,frames,now=None,dt=None):
        now=self._now() if now is None else now
        if dt is None:dt=0. if self._last is None else max(0.,now-self._last)
        return self.step(frames,now,dt)
