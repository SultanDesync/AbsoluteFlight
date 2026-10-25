"""Keyboard/mouse delivery lane (T60, D20): router action state -> SendInput, with focus gating and stuck-key safety.

Consumes the ButtonState of mod/hotas_buttons.py and delivers user-defined key.0xNN / mouse.* actions.
Catalog game actions can be added later through `resolved` (action -> VK from the native binding map),
once the resolver exists. Rules, from D20 and INPUT-ARCHITECTURE §4:

- Down events go out only while NMS is the foreground window and the lane is qualified.
- Releases always go out, even unfocused, so the OS never keeps a key we pressed.
- The lane tracks exactly what it pressed and releases only that: on focus loss, disarm,
  rebinding, shutdown and lease expiry (no step within LEASE seconds).
- Keys go as scan codes (KEYEVENTF_SCANCODE, extended flag where needed), because games read scan
  codes. Mouse buttons are down/up; a wheel step is one notch per activation and is never held.
- Within one step, releases are sent before presses.
- A held key repeats its key-down like a physical keyboard's auto-repeat (REPEAT_DELAY, then every
  REPEAT_INTERVAL). Live 2026-10-16 (build 180836): one key-down then silence was not seen as held by
  NMS hold actions (hold E, Left Shift boost), while held mouse buttons fired continuously. Mouse buttons
  never repeat.
- Relative cursor moves (move()) go out under the same focus and qualification gate. They are never
  held, so there is nothing to release; mod/stick_cursor.py decides when and how far.

The Win32 sender is separate from the logic, so everything above is tested with a fake sender.
"""
import atexit
import ctypes as C
from ctypes import wintypes as W
import os
import time
from hotas_buttons import KEY_NAMES,MOUSE_BUTTONS,action,output

LEASE=.5  # seconds without a step before everything held is released
REPEAT_DELAY=.25  # seconds before a held key starts repeating (Windows default is about 0.5 s)
REPEAT_INTERVAL=1/30  # seconds between repeats, the Windows default rate
WHEEL_DELTA=120
# VKs whose scan codes need KEYEVENTF_EXTENDEDKEY.
EXTENDED_VKS=frozenset((0x21,0x22,0x23,0x24,0x25,0x26,0x27,0x28,0x2D,0x2E,0x6F,0xA3,0xA5))
GENERIC_MODIFIERS={0x10:0xA0,0x11:0xA2,0x12:0xA4}  # Shift/Ctrl/Alt send as their left-hand keys


def target(action_id,resolved=None):
    """('key', vk) | ('mouse', button) | None for an action this lane can deliver."""
    if resolved and action_id in resolved:return ('key',resolved[action_id])
    a=action(action_id)
    if a is None:return None
    out=output(a.id)  # a function name (key.0x45:land) sends the bare key
    if a.lane=='key':return ('key',int(out[4:],16))
    if a.lane=='mouse':return ('mouse',out[6:])
    return None


class KeyLane:
    """Diffs the router's held/pressed actions into key and mouse events."""
    def __init__(self,sender,foreground,now=time.monotonic,qualified=False,resolved=None):
        self.sender=sender;self.foreground=foreground;self.now=now;self.qualified=qualified;self.resolved=dict(resolved or {})
        self.down={}  # action id -> ('key', vk) | ('mouse', button), exactly what we pressed
        self.last_step=None;self.status='idle'
        self.next_repeat={}  # ('key', vk) -> time of the next repeated key-down

    def _send(self,events):
        if events:self.sender(events)

    def release_all(self,reason='released'):
        """Send key-up/button-up for everything this lane pressed. Safe to call any time."""
        events=[(kind,code,False) for kind,code in sorted(set(self.down.values()),key=str)]
        if events and hasattr(self.sender,'note'):self.sender.note('release all: '+reason)
        self.down={};self.next_repeat={};self._send(events);self.status=reason
        return events

    def expire(self):
        """Call from any thread or timer: releases if the router stopped stepping (lease)."""
        if self.down and self.last_step is not None and self.now()-self.last_step>LEASE:return self.release_all('lease expired')
        return []

    def step(self,state):
        """state: hotas_buttons.ButtonState. Returns the events sent this step."""
        self.last_step=self.now()
        if not self.qualified:return self.release_all('unqualified') if self.down else self._idle('unqualified')
        if not self.foreground():return self.release_all('unfocused') if self.down else self._idle('unfocused')
        want={}
        for a in state.held:
            t=target(a,self.resolved)
            if t is not None and not (t[0]=='mouse' and t[1].startswith('wheel')):want[a]=t
        before=set(self.down.values());after=set(want.values())  # two actions on one key press it once
        releases=[(k,c,False) for k,c in sorted(before-after,key=str)]
        presses=[(k,c,True) for k,c in sorted(after-before,key=str)]
        self.down=want
        now=self.now();repeats=[]
        for t in sorted(after,key=str):
            if t[0]!='key':continue
            due=self.next_repeat.get(t)
            if due is None or t not in before:self.next_repeat[t]=now+REPEAT_DELAY
            elif now>=due:
                repeats.append((t[0],t[1],True))
                self.next_repeat[t]=max(due+REPEAT_INTERVAL,now)  # never a burst after a stall
        for t in list(self.next_repeat):
            if t not in after:del self.next_repeat[t]
        presses+=repeats
        wheel=[]
        for a in state.pressed:
            t=target(a,self.resolved)
            if t is not None and t[0]=='mouse' and t[1].startswith('wheel'):wheel.append(('wheel',WHEEL_DELTA if t[1]=='wheel-up' else -WHEEL_DELTA,None))
        events=releases+presses+wheel;self._send(events);self.status='active'
        return events

    def move(self,dx,dy):
        """Relative mouse move, only while qualified and NMS is in the foreground. Returns the events sent."""
        if not (dx or dy) or not self.qualified or not self.foreground():return []
        events=[('move',int(dx),int(dy))];self._send(events)
        return events

    def _idle(self,reason):
        self.status=reason;return []


# --- Win32 ---

INPUT_MOUSE,INPUT_KEYBOARD=0,1
KEYEVENTF_EXTENDEDKEY,KEYEVENTF_KEYUP,KEYEVENTF_SCANCODE=0x1,0x2,0x8
MOUSE_FLAGS={'left':(0x2,0x4,0),'right':(0x8,0x10,0),'middle':(0x20,0x40,0),'x1':(0x80,0x100,1),'x2':(0x80,0x100,2)}
MOUSEEVENTF_MOVE,MOUSEEVENTF_WHEEL=0x1,0x800
MAPVK_VK_TO_VSC_EX=4
ULONG_PTR=C.c_size_t


class MOUSEINPUT(C.Structure):
    _fields_=[('dx',W.LONG),('dy',W.LONG),('mouseData',W.DWORD),('dwFlags',W.DWORD),('time',W.DWORD),('dwExtraInfo',ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_=[('wVk',W.WORD),('wScan',W.WORD),('dwFlags',W.DWORD),('time',W.DWORD),('dwExtraInfo',ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_=[('uMsg',W.DWORD),('wParamL',W.WORD),('wParamH',W.WORD)]


class _INPUTUNION(C.Union):
    _fields_=[('mi',MOUSEINPUT),('ki',KEYBDINPUT),('hi',HARDWAREINPUT)]


class INPUT(C.Structure):
    _fields_=[('type',W.DWORD),('u',_INPUTUNION)]


# Tag on every event we inject, so a future hook can tell our input from the user's.
EXTRA_INFO=0x41484B4C  # 'AHKL'


def build_inputs(events,scan_of):
    """INPUT array for events. scan_of(vk) -> scan code (MapVirtualKey)."""
    out=(INPUT*len(events))()
    for i,(kind,code,down) in enumerate(events):
        item=out[i]
        if kind=='key':
            vk=GENERIC_MODIFIERS.get(code,code);scan=scan_of(vk)&0xFF
            flags=KEYEVENTF_SCANCODE|(0 if down else KEYEVENTF_KEYUP)
            if vk in EXTENDED_VKS or scan_of(vk)>>8 in (0xE0,0xE1):flags|=KEYEVENTF_EXTENDEDKEY
            item.type=INPUT_KEYBOARD;item.u.ki=KEYBDINPUT(0,scan,flags,0,EXTRA_INFO)
        elif kind=='mouse':
            press,release,xbutton=MOUSE_FLAGS[code]
            item.type=INPUT_MOUSE;item.u.mi=MOUSEINPUT(0,0,xbutton,press if down else release,0,EXTRA_INFO)
        elif kind=='move':  # (move, dx, dy): relative, so the OS pointer speed setting applies
            item.type=INPUT_MOUSE;item.u.mi=MOUSEINPUT(code,down,0,MOUSEEVENTF_MOVE,0,EXTRA_INFO)
        elif kind=='wheel':
            item.type=INPUT_MOUSE;item.u.mi=MOUSEINPUT(0,0,code&0xFFFFFFFF,MOUSEEVENTF_WHEEL,0,EXTRA_INFO)
        else:raise ValueError(kind)
    return out


class Win32Sender:
    def __init__(self):
        self.user32=C.WinDLL('user32',use_last_error=True)
        self.user32.SendInput.argtypes=[W.UINT,C.POINTER(INPUT),C.c_int];self.user32.SendInput.restype=W.UINT
        self.user32.MapVirtualKeyW.argtypes=[W.UINT,W.UINT];self.user32.MapVirtualKeyW.restype=W.UINT
        self.failures=0

    def scan(self,vk):return self.user32.MapVirtualKeyW(vk,MAPVK_VK_TO_VSC_EX)

    def __call__(self,events):
        array=build_inputs(events,self.scan)
        if self.user32.SendInput(len(array),array,C.sizeof(INPUT))!=len(array):self.failures+=1


class TraceSender:
    """Wraps a sender and logs every event with the OS key state read right after SendInput (diagnosis of
    held keys that NMS does not see as held, 2026-10-16). Bounded, so a long session cannot fill the disk."""
    def __init__(self,sender,path,key_down=None,now=time.monotonic,limit=20000):
        self.sender=sender;self.path=path;self.key_down=key_down;self.now=now;self.limit=limit;self.lines=0;self.t0=now()

    @property
    def failures(self):return getattr(self.sender,'failures',0)

    def _write(self,text):
        if self.lines>=self.limit:return
        self.lines+=1
        try:
            with open(self.path,'a',encoding='utf-8') as f:f.write(f'{(self.now()-self.t0)*1000:10.1f} {text}\n')
        except OSError:pass

    def note(self,text):self._write(text)

    def __call__(self,events):
        self.sender(events)
        for kind,code,down in events:
            if kind=='key':
                vk=GENERIC_MODIFIERS.get(code,code)
                state='' if self.key_down is None else ' os='+('down' if self.key_down(vk) else 'up')
                self._write(f"key {KEY_NAMES.get(code,hex(code))} {'down' if down else 'up'}{state}")
            elif kind=='mouse':self._write(f"mouse {code} {'down' if down else 'up'}")
            elif kind=='wheel':self._write(f'wheel {code}')
            # cursor moves are too frequent to log


def process_foreground(pid=None):
    """Callable: True while a window of this process (NMS, when run in-process) is in the foreground."""
    user32=C.WinDLL('user32');pid=os.getpid() if pid is None else pid
    user32.GetForegroundWindow.restype=W.HWND
    user32.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
    def check():
        hwnd=user32.GetForegroundWindow()
        if not hwnd:return False
        owner=W.DWORD();user32.GetWindowThreadProcessId(hwnd,C.byref(owner))
        return owner.value==pid
    return check


def win32_lane(qualified=False,resolved=None,trace_path=None):
    """A KeyLane on the real Win32 sender and this process's focus, released at interpreter exit.
    trace_path: also log every event and the OS key state after it (TraceSender)."""
    sender=Win32Sender()
    if trace_path is not None:
        user32=C.WinDLL('user32');user32.GetAsyncKeyState.restype=C.c_short
        sender=TraceSender(sender,trace_path,lambda vk:bool(user32.GetAsyncKeyState(vk)&0x8000))
    lane=KeyLane(sender,process_foreground(),qualified=qualified,resolved=resolved)
    atexit.register(lane.release_all,'shutdown')
    return lane


def describe(action_id):
    t=target(action_id)
    if t is None:return None
    return KEY_NAMES.get(t[1],hex(t[1])) if t[0]=='key' else MOUSE_BUTTONS[t[1]]
