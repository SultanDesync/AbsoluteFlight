"""D21 native virtual source; game delivery requires a separate release adapter."""
import ctypes as C
import re
import threading
import time

from hotas_input import BusReader

TOKEN = re.compile(r'^virtual:agent-0@(axis:0x3[0-7]|button:(?:[1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])|pov:[0-3]:(?:up|right|down|left))$')
OBSERVERS = ('context', 'device', 'axes', 'buttons', 'routing', 'axis_peak', 'button_presses')


class AgentDevice:
    def __init__(self, bus, routing, lease_ms=400):
        if type(lease_ms) is not int or not 1 <= lease_ms <= 450:
            raise ValueError('lease_ms must be 1..450')
        self.bus, self.routing, self.lease_ms = bus, routing, lease_ms
        self.reader = BusReader(bus)
        self.lock = threading.RLock()
        self.latest = {}
        self.routed = {}
        self.connected = False
        self.axis_peak = 0.
        self.button_presses = 0
        self.press_baseline = None
        self.functions = {}
        for suffix, args in (
            ('Enable', [C.c_uint32, C.c_uint32]), ('Heartbeat', []),
            ('SetAxis', [C.c_uint32, C.c_float]), ('SetButton', [C.c_uint32, C.c_uint32]),
            ('SetPov', [C.c_uint32, C.c_int32]), ('Neutral', []), ('Disconnect', []),
        ):
            fn = getattr(bus.dll, 'AbsoluteInputBus_Agent' + suffix)
            fn.argtypes, fn.restype = args, C.c_uint32
            self.functions[suffix] = fn

    def _call(self, name, *args):
        result = self.functions[name](*args)
        if result:
            raise RuntimeError('agent source ' + name + ' rejected: ' + str(result))

    def prepare(self):
        """Opt in before provider Start; no command is applied during startup."""
        self._call('Enable', 1, self.lease_ms)

    def connect(self):
        with self.lock:
            self._call('Enable', 1, self.lease_ms)
            self._call('Heartbeat')
            self.connected = True
            self.routing.neutral()
            # Connection generation is a baseline, never a button edge. Consume
            # a connected neutral frame before the first scripted command.
            try:
                self._neutral_baseline(renew=True)
            except BaseException:
                self.disconnect()
                raise

    def _neutral_baseline(self, renew=False):
        """Observe two new native neutral frames after resetting a router baseline."""
        old = self.latest.get('virtual:agent-0')
        previous = (old['sequence'], old['generation']) if old else None
        fresh = 0
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            if renew:
                self._call('Heartbeat')
            self.reader.scan()
            self.poll()
            frame = self.latest.get('virtual:agent-0')
            if frame:
                if not frame['connected'] and not renew:
                    self.connected = False
                    self.routing.neutral()
                    return
                identity = (frame['sequence'], frame['generation'])
                if identity != previous:
                    previous = identity
                    if (frame['connected'] and not any(frame['digital_down'])
                            and all(abs(raw - 32767.5) <= 1 for raw in frame['raw'])):
                        fresh += 1
                        if fresh >= 2:
                            return
                    else:
                        fresh = 0
            time.sleep(.003)
        raise TimeoutError('virtual source did not publish a fresh neutral baseline')

    def heartbeat(self):
        with self.lock:
            if not self.connected:
                raise RuntimeError('agent session is disconnected')
            self._call('Heartbeat')

    def resolve(self, action):
        return self.routing.resolve(action)

    @staticmethod
    def _control(token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError('only canonical virtual:agent-0 controls are allowed')
        return token.split('@', 1)[1].split(':')

    def axis(self, token, value):
        control = self._control(token)
        if control[0] != 'axis':
            raise ValueError('axis command needs an axis token')
        with self.lock:
            if not self.connected:
                raise RuntimeError('agent session is disconnected')
            self._call('SetAxis', int(control[1], 16) - 0x30, value)

    def button(self, token, down):
        control = self._control(token)
        if type(down) is not bool:
            raise ValueError('down must be boolean')
        with self.lock:
            if not self.connected:
                raise RuntimeError('agent session is disconnected')
            if control[0] == 'button':
                self._call('SetButton', int(control[1]), int(down))
            elif control[0] == 'pov':
                direction = ('up', 'right', 'down', 'left').index(control[2]) if down else -1
                self._call('SetPov', int(control[1]), direction)
            else:
                raise ValueError('button command needs a digital token')

    def poll(self):
        with self.lock:
            frames = self.reader.read(.05)
            self.latest = {key: value for key, value in frames.items() if key == 'virtual:agent-0'}
            self.routed = self.routing.observe(self.latest)
            frame = self.latest.get('virtual:agent-0')
            if frame and frame['connected']:
                self.axis_peak = max(self.axis_peak, max(abs(raw / 32767.5 - 1.) for raw in frame['raw']))
                if self.press_baseline is not None:
                    self.button_presses = sum((current - previous) & 0xFFFFFFFF
                                              for current, previous in zip(frame['press_count'], self.press_baseline))

    def begin_run(self):
        with self.lock:
            self.poll()
            frame = self.latest.get('virtual:agent-0')
            if not self.connected or not frame or not frame['connected']:
                raise RuntimeError('virtual source is not connected')
            self.press_baseline = list(frame['press_count'])
            self.axis_peak, self.button_presses = 0., 0

    def read(self, names):
        if any(name not in OBSERVERS for name in names):
            raise ValueError('unknown bus observer')
        self.poll()
        with self.lock:
            frame = self.latest.get('virtual:agent-0')
            values = {
                'context': {'mode': 'bus-only', 'game_delivery': False},
                'device': None if frame is None else {
                    'id': 'virtual:agent-0', 'connected': frame['connected'],
                    'sequence': frame['sequence'], 'generation': frame['generation']},
                'axes': None if frame is None else list(frame['raw']),
                'buttons': None if frame is None else {
                    'down': list(frame['digital_down']), 'press': list(frame['press_count']),
                    'release': list(frame['release_count'])},
                'routing': self.routed,
                'axis_peak': self.axis_peak,
                'button_presses': self.button_presses,
            }
            return {name: values[name] for name in names}

    def neutral(self):
        with self.lock:
            # Native neutral may return NotReady after its independent lease expired.
            try:
                result = self.functions['Neutral']()
                if result not in (0, 1):
                    raise RuntimeError('agent neutral rejected: ' + str(result))
            finally:
                self.routing.neutral()
            if self.connected:
                self._neutral_baseline()

    def disconnect(self):
        with self.lock:
            self.connected = False
            try:
                result = self.functions['Disconnect']()
                if result not in (0, 1):
                    raise RuntimeError('agent disconnect rejected: ' + str(result))
            finally:
                self.routing.neutral()

    def close(self):
        with self.lock:
            try:
                self.disconnect()
            finally:
                self._call('Enable', 0, self.lease_ms)
