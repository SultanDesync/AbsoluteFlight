"""D21 local-only Windows named pipe with a protected current-user DACL.

The transport never evaluates code and never touches game memory. One bounded
JSON line is one request. PIPE_NOWAIT keeps disconnect/kill independent of a
client which connects without sending a complete message.
"""
import ctypes as C
import json
import os
import threading
import time

MAX_MESSAGE = 65536
PROTOCOL = 1


def encode(value):
    data = (json.dumps(value, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(data) > MAX_MESSAGE:
        raise ValueError('pipe message exceeds 64 KiB')
    return data


def decode(line):
    if len(line) > MAX_MESSAGE:
        raise ValueError('pipe message exceeds 64 KiB')
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)
    value = json.loads(line.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('request must be a JSON object')
    return value


def validate_name(name):
    prefix = r'\\.\pipe\AbsoluteHOTAS-Agent-'
    suffix = name[len(prefix):] if isinstance(name, str) and name.startswith(prefix) else ''
    if not suffix or len(suffix) > 80 or any(not (c.isascii() and (c.isalnum() or c in '-_')) for c in suffix):
        raise ValueError('expected local AbsoluteHOTAS-Agent pipe name')
    return name


class WinPipe:
    """Small ctypes wrapper; construction is the first Windows API access."""
    def __init__(self):
        if os.name != 'nt':
            raise OSError('D21 named pipes require Windows')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        signatures = (
            (self.k, 'GetCurrentProcess', [], C.c_void_p),
            (self.k, 'CloseHandle', [C.c_void_p], C.c_int),
            (self.k, 'LocalFree', [C.c_void_p], C.c_void_p),
            (self.k, 'CreateNamedPipeW', [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_uint32,
                                       C.c_uint32, C.c_uint32, C.c_uint32, C.c_void_p], C.c_void_p),
            (self.k, 'ConnectNamedPipe', [C.c_void_p, C.c_void_p], C.c_int),
            (self.k, 'DisconnectNamedPipe', [C.c_void_p], C.c_int),
            (self.k, 'ReadFile', [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            (self.k, 'WriteFile', [C.c_void_p, C.c_void_p, C.c_uint32, C.POINTER(C.c_uint32), C.c_void_p], C.c_int),
            (self.k, 'CreateFileW', [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p,
                                  C.c_uint32, C.c_uint32, C.c_void_p], C.c_void_p),
            (self.k, 'SetNamedPipeHandleState', [C.c_void_p, C.POINTER(C.c_uint32), C.c_void_p, C.c_void_p], C.c_int),
            (self.a, 'OpenProcessToken', [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_int),
            (self.a, 'GetTokenInformation', [C.c_void_p, C.c_uint32, C.c_void_p,
                                          C.c_uint32, C.POINTER(C.c_uint32)], C.c_int),
            (self.a, 'ConvertSidToStringSidW', [C.c_void_p, C.POINTER(C.c_wchar_p)], C.c_int),
            (self.a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
             [C.c_wchar_p, C.c_uint32, C.POINTER(C.c_void_p), C.c_void_p], C.c_int),
        )
        for library, name, args, result in signatures:
            fn = getattr(library, name)
            fn.argtypes, fn.restype = args, result

    def _check(self, value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def user_sid(self):
        token = C.c_void_p()
        self._check(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)))
        try:
            size = C.c_uint32()
            self.a.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not 1 <= size.value <= 65536:
                raise OSError('invalid token information size')
            buffer = C.create_string_buffer(size.value)
            self._check(self.a.GetTokenInformation(token, 1, buffer, size.value, C.byref(size)))
            sid = C.c_void_p.from_buffer(buffer).value
            text = C.c_wchar_p()
            self._check(self.a.ConvertSidToStringSidW(sid, C.byref(text)))
            try:
                return text.value
            finally:
                self.k.LocalFree(C.cast(text, C.c_void_p))
        finally:
            self.k.CloseHandle(token)

    def create(self, name):
        class SecurityAttributes(C.Structure):
            _fields_ = [('length', C.c_uint32), ('descriptor', C.c_void_p), ('inherit', C.c_int)]
        descriptor = C.c_void_p()
        self._check(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(A;;GA;;;' + self.user_sid() + ')', 1, C.byref(descriptor), None))
        try:
            attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, 0)
            # Duplex, first-instance ownership, byte mode, nonblocking, reject remote clients.
            handle = self.k.CreateNamedPipeW(validate_name(name), 3 | 0x80000, 1 | 8,
                                            1, MAX_MESSAGE, MAX_MESSAGE, 0, C.byref(attributes))
            if handle in (None, C.c_void_p(-1).value):
                raise C.WinError(C.get_last_error())
            return handle
        finally:
            self.k.LocalFree(descriptor)

    def connect_client(self, name):
        # Identification-only SQOS prevents a server impersonating the client.
        handle = self.k.CreateFileW(validate_name(name), 0xC0000000, 0, None, 3,
                                    0x100000 | 0x10000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise C.WinError(C.get_last_error())
        mode = C.c_uint32(1)
        try:
            self._check(self.k.SetNamedPipeHandleState(handle, C.byref(mode), None, None))
            return handle
        except BaseException:
            self.k.CloseHandle(handle)
            raise

    def accept(self, handle):
        if self.k.ConnectNamedPipe(handle, None):
            # In NOWAIT mode this transition only puts the instance into LISTENING.
            return False
        error = C.get_last_error()
        if error == 535:  # ERROR_PIPE_CONNECTED
            return True
        if error in (536, 232):  # LISTENING / NO_DATA (previous peer left)
            if error == 232:
                self.k.DisconnectNamedPipe(handle)
            return False
        raise C.WinError(error)

    def read(self, handle):
        buffer = C.create_string_buffer(4096)
        count = C.c_uint32()
        if self.k.ReadFile(handle, buffer, len(buffer), C.byref(count), None):
            return buffer.raw[:count.value]
        error = C.get_last_error()
        if error == 232:  # NOWAIT pipe is empty
            return b''
        if error in (109, 233):
            raise EOFError('pipe disconnected')
        raise C.WinError(error)

    def write(self, handle, data, timeout=.2):
        deadline = time.monotonic() + timeout
        while data:
            count = C.c_uint32()
            buffer = C.create_string_buffer(data)
            if not self.k.WriteFile(handle, buffer, len(data), C.byref(count), None):
                error = C.get_last_error()
                if error in (109, 232, 233):
                    raise EOFError('pipe disconnected')
                raise C.WinError(error)
            data = data[count.value:]
            if data and time.monotonic() >= deadline:
                raise TimeoutError('pipe output stalled')
            if not count.value:
                time.sleep(.005)


class PipeServer:
    def __init__(self, name, handler, *, tick=lambda: None, disconnected=lambda: None):
        self.name = validate_name(name)
        self.handler, self.tick, self.disconnected = handler, tick, disconnected
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError('pipe already started')
        self.thread = threading.Thread(target=self._run, name='D21Pipe', daemon=True)
        self.thread.start()
        if not self.ready.wait(3):
            self.stop()
            raise TimeoutError('pipe startup timeout')
        if self.error:
            raise self.error
        return self

    def _run(self):
        native, handle, connected, pending = None, None, False, b''
        try:
            native = WinPipe()
            handle = native.create(self.name)
            self.ready.set()
            while not self.stopping.is_set():
                self.tick()
                try:
                    if not connected:
                        connected = native.accept(handle)
                    if connected:
                        pending += native.read(handle)
                        if len(pending) > MAX_MESSAGE:
                            raise ValueError('pipe message exceeds 64 KiB')
                        # Limit requests per tick so a flood cannot starve lease/kill checks.
                        for _ in range(8):
                            if b'\n' not in pending:
                                break
                            line, pending = pending.split(b'\n', 1)
                            try:
                                reply = self.handler(decode(line))
                            except Exception as error:
                                # A protocol error cancels active work before replying.
                                self.disconnected()
                                reply = {'ok': False, 'error': str(error)}
                            native.write(handle, encode(reply))
                except (OSError, EOFError, ValueError, TimeoutError):
                    self.disconnected()
                    native.k.DisconnectNamedPipe(handle)
                    connected, pending = False, b''
                self.stopping.wait(.005)
        except BaseException as error:
            self.error = error
            self.ready.set()
        finally:
            try:
                self.disconnected()
            finally:
                if handle is not None:
                    native.k.DisconnectNamedPipe(handle)
                    native.k.CloseHandle(handle)

    def stop(self):
        self.stopping.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(3)
            if self.thread.is_alive():
                raise RuntimeError('pipe did not stop')


class PipeClient:
    def __init__(self, name, timeout=3.):
        self.native = WinPipe()
        self.handle = self.native.connect_client(name)
        self.timeout, self.pending = timeout, b''

    def request(self, request):
        self.native.write(self.handle, encode(request))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.pending += self.native.read(self.handle)
            if len(self.pending) > MAX_MESSAGE:
                raise ValueError('pipe response exceeds 64 KiB')
            if b'\n' in self.pending:
                line, self.pending = self.pending.split(b'\n', 1)
                return decode(line)
            time.sleep(.005)
        raise TimeoutError('pipe response timeout')

    def close(self):
        if self.handle is not None:
            self.native.k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
