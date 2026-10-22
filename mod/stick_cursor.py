"""Stick cursor (T61 follow-up): pitch/yaw move the Windows mouse cursor in rate mode while NMS shows a frontend page.

NMS menus and dialogs (FRONTEND action set) use a mouse cursor and select with left click
(research/20-nms-default-bindings.md). While one is open the runtime already hands the flight axes
back to the game, so the stick is free; this turns its deflection into relative mouse moves.

Rules:
- Cursor mode needs the Keyboard & mouse output switch AND the stick-cursor setting AND NMS focused
  AND the HOTAS menu closed AND the frontend flag nonzero. Never while the flag is 0: in flight the
  mouse steers the ship, so a stray move would fight the stick.
- Rate mode: speed = deflection^CURVE * cursor-speed (pixels per second), per axis, sign kept.
  Deflection comes from flight_shaping.shape_axis, so the axis deadzone, saturation, sensitivity and
  invert of the flight binding apply. Positive yaw moves right, positive pitch moves up (cursor-invert-y flips it).
- Fractions of a pixel are carried to the next tick, so the speed is the same at any tick rate.
  Leaving cursor mode drops the remainder; a long tick is clamped to MAX_STEP_SECONDS.
"""
import math
from flight_shaping import shape_axis

CURVE=1.6
MAX_STEP_SECONDS=.1


def cursor_mode(key_output,stick_cursor,focused,hotas_menu_open,frontend):
    """True when stick deflection may move the cursor. frontend: the game's frontend flag, or None if unreadable."""
    return bool(key_output and stick_cursor and focused and not hotas_menu_open and frontend)


def cursor_reason(key_output,stick_cursor,focused,hotas_menu_open,frontend):
    """Short state for status and the Buttons page: 'on' or why not."""
    if not key_output:return 'key output off'
    if not stick_cursor:return 'off'
    if not focused:return 'unfocused'
    if hotas_menu_open:return 'hotas menu'
    if frontend is None:return 'no frontend flag'
    if not frontend:return 'flight'
    return 'on'


def rate(deflection,max_speed,curve=CURVE):
    """Signed pixels per second for a shaped deflection in -1..1."""
    if not deflection:return 0.
    d=min(1.,abs(deflection))
    return math.copysign(d**curve*max_speed,deflection)


class StickCursor:
    """Accumulates rate-mode cursor motion into whole-pixel relative moves."""
    def __init__(self):
        self.rx=self.ry=0.;self.state='off'

    def reset(self):self.rx=self.ry=0.

    def step(self,values,pitch_unit,yaw_unit,dt,qualified,reason=None):
        """(dx, dy) whole pixels to move now, screen axes (dy positive is down). (0, 0) when not qualified."""
        self.state=reason or ('on' if qualified else 'off')
        if not qualified or dt<=0:
            self.reset();return 0,0
        dt=min(dt,MAX_STEP_SECONDS);speed=values['cursor-speed']
        yaw=shape_axis(values,'yaw',yaw_unit) or 0.
        pitch=shape_axis(values,'pitch',pitch_unit) or 0.
        if values.get('cursor-invert-y'):pitch=-pitch
        self.rx+=rate(yaw,speed)*dt;self.ry-=rate(pitch,speed)*dt
        dx=int(self.rx);dy=int(self.ry)  # truncate toward zero: the remainder keeps its sign
        self.rx-=dx;self.ry-=dy
        if not yaw:self.rx=0.
        if not pitch:self.ry=0.
        return dx,dy
