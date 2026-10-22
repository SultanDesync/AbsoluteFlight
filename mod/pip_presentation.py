"""Owned ship pip presentation, NMS 180836 / Steam 25732212.

Native prediction/smoothing/selection keep running. Lag changes only the owned
HUD's two projected coordinates, and render suppression targets its pip widget.
The native finite-point projector preserves camera position, FOV and parallax.
"""
import ctypes as C
import math
import struct
import threading

APP_GLOBAL = 0x6E8D708
HUD = 0x7329C0
WIDGET = 0xD87E0
PIP_XY = 0xD8C00
PIP_VISIBLE = 0xD8C15
CAMERA = 0x905B20
PROJECT = 0x694110
EXTENT = 0x2D84230
EXTENT_OBJECT = 0x9098D0
MAX_AGE_MS = 50
# Below hooked entries; valid with normal startup hooks and after hot reload.
PINS = {
    0x996A30: bytes.fromhex('e8eb2f4302'),
    0x2DC9A40: bytes.fromhex('8d80000000e8469aa7fe488945604885c0750a483945680f'),
    0x694150: bytes.fromhex('0f1040200f294424600f1048300f294c24700f104040488b'),
    0x694246: bytes.fromhex('e815a56e02'),
    0x986C38: bytes.fromhex('e8f3d53f02'),
    0x2D8423A: bytes.fromhex('488bda488bf9e88b4244fd837810050f84dd000000'),
    0x2D7AC0: bytes.fromhex('488b41384805d0989000c3'),
}


class PipPresentation:
    def __init__(self, base, clock, read=C.string_at, write=C.memmove, projector=None, extent=None):
        self.base, self.clock, self.read, self.write = int(base), clock, read, write
        self.projector = projector
        self.extent = extent
        self.verified = False
        self.lock = threading.Lock()
        self.epoch = 0
        self.ready = None
        self.state = 'not verified'
        self.updates = 0

    def verify(self):
        self.verified = all(self.read(self.base+rva, len(raw)) == raw for rva, raw in PINS.items())
        if self.verified and self.projector is None:
            self.projector = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)(self.base+PROJECT)
        if self.verified and self.extent is None:
            self.extent = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p)(self.base+EXTENT)
        self.clear('ready' if self.verified else 'presentation signature mismatch')
        return self.verified

    def clear(self, reason='waiting for current pip'):
        with self.lock:
            self.epoch += 1
            self.ready = None
            self.state = reason
            return self.epoch

    def _app(self):
        return struct.unpack('<Q', self.read(self.base+APP_GLOBAL, 8))[0]

    def owns(self, widget):
        return self.verified and bool(app := self._app()) and widget == app+HUD+WIDGET

    def update(self, hud, ship, values, solution, eligible, *, epoch=None):
        """Run after native own HUD update; never turns a native-hidden pip on."""
        if epoch is None:
            epoch = self.clear()
        if not self.verified or values.get('pip-mode', 'lead') != 'lag' or not eligible:
            return False
        app = self._app()
        if not app or hud != app+HUD or not ship or not solution or solution.get('ship') != ship:
            return False
        age = self.clock()-solution['tick']
        if not 0 <= age <= MAX_AGE_MS or not self.read(hud+PIP_VISIBLE, 1)[0]:
            return False
        point = solution.get('lag_split')
        if point is None or len(point) != 2 or not all(math.isfinite(v) for half in point for v in half):
            return False
        # Both native projector input and output must permit aligned SIMD loads.
        buffers = [C.create_string_buffer(size+15) for size in (32, 16)]
        src, out = ((C.addressof(buffer)+15) & ~15 for buffer in buffers)
        C.memmove(src, struct.pack('<4f4f', *point[0], 0., *point[1], 0.), 32)
        result = self.projector(app+CAMERA, out, src)
        if result != out:
            return False
        x, y, depth, front = struct.unpack('<4f', C.string_at(out, 16))
        if not all(math.isfinite(v) for v in (x, y, depth, front)) or front <= 0:
            return False
        size = (C.c_float*2)()
        if self.extent(app+EXTENT_OBJECT, C.addressof(size)) != C.addressof(size):
            return False
        width,height = size
        if not all(math.isfinite(v) and 64 <= v <= 32768 for v in size):
            return False
        xy = (x*width, y*height)
        if any(abs(v) > 1e7 for v in xy) or self._app() != app:
            return False
        with self.lock:
            if epoch != self.epoch or not 0 <= self.clock()-solution['tick'] <= MAX_AGE_MS:
                return False
            self.write(hud+PIP_XY, struct.pack('<2f', *xy), 8)
            self.ready = {'hud': hud, 'ship': ship, 'target': solution['target'], 'tick': solution['tick']}
            self.updates += 1
            self.state = 'lag'
        return True

    def suppress(self, widget, values, ship):
        """Only call for the exact native pip draw caller, or an owned draw TLS scope."""
        if not self.owns(widget):
            return False
        if not values.get('pip-visible', True):
            return True
        if values.get('pip-mode', 'lead') != 'lag':
            return False
        with self.lock:
            ready = self.ready
            return not (ready and ready['ship'] == ship and widget == ready['hud']+WIDGET
                        and 0 <= self.clock()-ready['tick'] <= MAX_AGE_MS)

    def status(self):
        with self.lock:
            return {'verified': self.verified, 'state': self.state, 'lag_updates': self.updates,
                    'ready': self.ready is not None and 0 <= self.clock()-self.ready['tick'] <= MAX_AGE_MS}
