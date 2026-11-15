"""Mouse motion for VR menus (T125), read from a low-level mouse hook.

0.2.7-dev.15 (2026-11-15) moved the pause menu's pointer by the cursor's distance from the middle of the game window,
putting the cursor back there each tick. The pointer ran away (y to 1,621 on a 1,080 card): the cursor did not stay
where the mod put it, so the same offset was added tick after tick. Motion is now taken where Windows reports it:
a WH_MOUSE_LL hook sees each move before the cursor goes there, so the move is the event's point minus the cursor
position at that moment, whatever else moves or clips the cursor. Moves the mod or anything else injects are left
out. Raw input is not used: registering for it in the game's process would take the game's own raw mouse input.

The hook runs on its own thread with a message loop, only while a VR game menu is open (start()/stop()), so the
rest of the time no mouse event in Windows waits on Python.
"""
import ctypes as C
import ctypes.wintypes as W
import threading

WH_MOUSE_LL, WM_MOUSEMOVE, WM_QUIT, LLMHF_INJECTED = 14, 0x0200, 0x0012, 0x01
HOOKPROC = C.WINFUNCTYPE(C.c_ssize_t, C.c_int, W.WPARAM, W.LPARAM)


class MSLLHOOKSTRUCT(C.Structure):
    _fields_ = [('pt', W.POINT), ('mouseData', W.DWORD), ('flags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


def _user32():
    user = C.WinDLL('user32', use_last_error=True)
    user.SetWindowsHookExW.argtypes = [C.c_int, HOOKPROC, W.HINSTANCE, W.DWORD]; user.SetWindowsHookExW.restype = W.HHOOK
    user.CallNextHookEx.argtypes = [W.HHOOK, C.c_int, W.WPARAM, W.LPARAM]; user.CallNextHookEx.restype = C.c_ssize_t
    user.UnhookWindowsHookEx.argtypes = [W.HHOOK]; user.UnhookWindowsHookEx.restype = W.BOOL
    user.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]; user.GetMessageW.restype = W.BOOL
    user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]; user.PostThreadMessageW.restype = W.BOOL
    user.GetCursorPos.argtypes = [C.POINTER(W.POINT)]; user.GetCursorPos.restype = W.BOOL
    return user


class MouseMotion:
    """Accumulates real mouse movement in pixels between take() calls."""
    def __init__(self, user=None, kernel=None):
        self.user = user; self.kernel = kernel
        self.lock = threading.Lock(); self.dx = self.dy = 0
        self.thread = None; self.thread_id = 0; self.hook = None; self.error = None; self.events = 0
        self.proc = HOOKPROC(self._callback)

    def _callback(self, code, wparam, lparam):
        try:
            if code >= 0 and wparam == WM_MOUSEMOVE:
                info = MSLLHOOKSTRUCT.from_address(lparam)
                if not info.flags & LLMHF_INJECTED:
                    now = W.POINT()
                    if self.user.GetCursorPos(C.byref(now)):
                        with self.lock:
                            self.dx += info.pt.x-now.x; self.dy += info.pt.y-now.y; self.events += 1
        except BaseException as error:
            self.error = repr(error)
        return self.user.CallNextHookEx(None, code, wparam, lparam)

    def _run(self, ready):
        try:
            self.thread_id = self.kernel.GetCurrentThreadId()
            self.hook = self.user.SetWindowsHookExW(WH_MOUSE_LL, self.proc, self.kernel.GetModuleHandleW(None), 0)
            if not self.hook:self.error = f'SetWindowsHookExW failed ({C.get_last_error()})'
        except BaseException as error:
            self.error = repr(error)
        ready.set()
        if not self.hook:return
        message = W.MSG()
        try:
            while self.user.GetMessageW(C.byref(message), None, 0, 0) > 0:pass
        finally:
            self.user.UnhookWindowsHookEx(self.hook); self.hook = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        """Install the hook on its own thread (idempotent). False when Windows refused it; see `error`."""
        if self.running:return self.hook is not None
        if self.user is None:self.user = _user32()
        if self.kernel is None:
            self.kernel = C.WinDLL('kernel32', use_last_error=True); self.kernel.GetModuleHandleW.restype = W.HMODULE
        self.take(); ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), name='AbsoluteHOTAS VR mouse', daemon=True)
        self.thread.start(); ready.wait(2.)
        return self.hook is not None

    def stop(self):
        if self.running and self.thread_id:self.user.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if self.thread is not None:self.thread.join(1.)
        self.thread = None; self.thread_id = 0; self.take()

    def take(self):
        """(dx, dy) since the last call, screen pixels, y down."""
        with self.lock:
            moved = (self.dx, self.dy); self.dx = self.dy = 0
        return moved
