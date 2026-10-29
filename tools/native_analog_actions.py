"""Exocraft controls: HOTAS / wheel axes and buttons answered as the game's own analog actions, build 183877 / Steam 26295378.

Research: research/re-log/2026-10-28_claude_exocraft-input-183791.md. Every analog query reaches the input manager's slot
+0x28, the wrapper 0x2CA5580 (`float Analog(manager, action, port)`: RCX, EDX, R8D, result XMM0): gameplay through the
controller's scalar getter 0x1445790 (which first returns 0 unless the frontend state app+0x89BD98 is 0 or 2), and some
vehicle code by calling the slot directly (0x182E3B8: VehicleThrust, port -1). The wrapper refreshes the action from Steam
and tail-jumps the core 0x2C97460, which resolves the port (-1 = manager+0x18) and reads this frame's record.

The exocraft (cGcVehicleComponent) builds its steering and throttle from VehicleSteer 0x8D, VehicleThrust 0x8E and
VehicleBrake 0x8F plus the keyboard's Vehicle_* and Player_* actions, all through this slot; Vehicle_Dive 0x37 is read
here too. With keyboard driving those three are natively 0 in every action set (live 2026-10-28), so the keys keep
working beside an answer here.

One cave at the wrapper's entry (its first 10 bytes become a jmp), the same shape as tools/native_actions.py on slot
+0x20. It calls the original first, so the native read and its records run once, then answers from a per-action table
the runtime writes, only when the cave is enabled, the query is for the manager's active port and the port's current
action set (the first entry of the list at block+0x68, block = manager+0x5B0+port*0x470, with no forced set at
block+0x80) is the entry's set. The exocraft's set is VehicleMode 16; the ship and on foot read other sets, so an answer
never reaches flight (0x180DF70 is shared by the ship and vehicle updates and reads VehicleThrust).

Construction never attaches to a process; bytes are verified against the recorded hashes first.
"""
import ctypes as C
import hashlib
import struct

from native_roll_patch import Code

APP_GLOBAL = 0x6F5F868
WRAPPER = 0x2CA5340                # manager slot +0x28: refresh, then jmp CORE
WRAPPER_BYTES = bytes.fromhex('48895c24084889742410574883ec20418bd88bfa488bf1e894200000448bc38bd7488bce488b5c2430488b7424'
                              '384883c4205fe9a81effff')
CORE, SET_HEAD, SET_FORCE, GETTER = 0x2C97220, 0x2C97120, 0x2C9D210, 0x14456A0
SITE_SIZE = 10                     # mov [rsp+8], rbx; mov [rsp+0x10], rsi
CONTINUE = WRAPPER+SITE_SIZE
MANAGER = 0x95AEA8
DEFAULT_PORT, PORT_STRIDE, PORT_BLOCK, PORT_MAX = 0x18, 0x470, 0x5B0, 5
SET_FORCED, SET_COUNT, SET_LIST = 0x80, 0x64, 0x68  # in the port block (0x2C97360, 0x2C9D450)
# The core, the set reader and setter the cave's port and set reads come from (pinned, not called). Not the scalar getter:
# the release mod hooks it for ship pitch/yaw (AbsoluteHOTASScalar), so its first bytes are a jump live (0.2.3-dev.7).
PIN_RANGES = ((WRAPPER, len(WRAPPER_BYTES)), (CORE, 0xD1), (SET_HEAD, 0x2A), (SET_FORCE, 0x66))
PIN_SHA256 = 'dbfe5d77019f3d72ce9c6394124eb33895a2700d70076217eccef79ea3f05a15'  # sha256 over PIN_RANGES of the exe

VEHICLE_MODE = 16                  # cGcActionSetType.VehicleMode
STEER, THRUST, BRAKE = 0x8D, 0x8E, 0x8F
LOOK_X, LOOK_Y, MOVE_X, MOVE_Y = 0x85, 0x86, 0x88, 0x89  # CameraLookX/Y, PlayerMoveX/Y
REVERSE = 0x2E                     # Vehicle_Reverse
COUNT = 0x110                      # action ids 0..0x10F
ENTRY = 8                          # u32 action set (0 = no answer), f32 value
ENABLED, HITS, TABLE = 0x00, 0x08, 0x40
DATA_OFFSET = 0x1000
DATA_SIZE = TABLE+COUNT*ENTRY
ALLOCATION = DATA_OFFSET+((DATA_SIZE+0xFFF) & ~0xFFF)

JNE, JA, JAE, JLE, JE = b'\x0f\x85', b'\x0f\x87', b'\x0f\x83', b'\x0f\x8e', b'\x0f\x84'


def verify(read, base):
    """True only when every pinned range matches build 183791 and the site holds its original bytes."""
    h = hashlib.sha256()
    for rva, size in PIN_RANGES: h.update(read(base+rva, size))
    return h.hexdigest() == PIN_SHA256 and read(base+WRAPPER, SITE_SIZE) == WRAPPER_BYTES[:SITE_SIZE]


def cave_code(cave, base, data):
    c = Code(cave)
    def d(offset): return data+offset
    c.emit(bytes.fromhex('53 56 57 41 54 48 83 ec 28'))     # push rbx, rsi, rdi, r12; shadow space (RSP 16-aligned)
    c.emit(bytes.fromhex('48 89 ce 89 d7 45 89 c4'))        # rsi = manager, edi = action, r12d = port
    c.relative(b'\xe8', 'original')                          # the native answer (XMM0), all its reads once
    c.relative(b'\x83\x3d', d(ENABLED), b'\x01'); c.relative(JNE, 'done')
    c.emit(bytes.fromhex('81 ff')+struct.pack('<I', COUNT)); c.relative(JAE, 'done')
    c.relative(b'\x4c\x8d\x05', d(TABLE))
    c.emit(bytes.fromhex('4d 8d 04 f8'))                     # r8 = entry (lea r8, [r8+rdi*8])
    c.emit(bytes.fromhex('44 89 e1 8b 56')+bytes([DEFAULT_PORT]))  # ecx = port, edx = active port
    c.emit(bytes.fromhex('83 f9 ff')); c.relative(JNE, 'port')
    c.emit(bytes.fromhex('89 d1'))
    c.label('port')
    c.emit(bytes.fromhex('39 d1')); c.relative(JNE, 'done')   # only the active player's port
    c.emit(bytes.fromhex('83 f9')+bytes([PORT_MAX])); c.relative(JA, 'done')
    c.emit(bytes.fromhex('45 8b 18 45 85 db')); c.relative(JE, 'done')  # r11d = the entry's set; 0 = no answer
    c.emit(bytes.fromhex('48 69 c9')+struct.pack('<I', PORT_STRIDE))
    c.emit(bytes.fromhex('48 8d 84 0e')+struct.pack('<I', PORT_BLOCK))  # rax = the port block
    c.emit(bytes.fromhex('83 b8')+struct.pack('<I', SET_FORCED)+b'\xff'); c.relative(JNE, 'done')  # a forced set
    c.emit(bytes.fromhex('83 b8')+struct.pack('<I', SET_COUNT)+b'\x00'); c.relative(JLE, 'done')
    c.emit(bytes.fromhex('48 8b 80')+struct.pack('<I', SET_LIST))
    c.emit(bytes.fromhex('48 85 c0')); c.relative(JE, 'done')
    c.emit(bytes.fromhex('44 3b 18')); c.relative(JNE, 'done')   # the port's current set
    c.emit(bytes.fromhex('f3 41 0f 10 40 04'))              # movss xmm0, [r8+4]
    c.relative(b'\x48\xff\x05', d(HITS))
    c.label('done')
    c.emit(bytes.fromhex('48 83 c4 28 41 5c 5f 5e 5b c3'))
    c.label('original')                                      # the replaced 10 bytes, then the rest of the wrapper
    c.emit(WRAPPER_BYTES[:SITE_SIZE]); c.relative(b'\xe9', base+CONTINUE)
    return c.finish()


def site_patch(site, cave):
    return b'\xe9'+struct.pack('<i', cave-site-5)+b'\x90'*(SITE_SIZE-5)


def current_set(read, base):
    """The active port's current action set (cGcActionSetType), or None while it is unreadable, forced or empty.
    read(address, size) must fail safely (the runtime's process reader), never fault."""
    try:
        app = struct.unpack('<Q', read(base+APP_GLOBAL, 8))[0]
        if not app:return None
        manager = struct.unpack('<Q', read(app+MANAGER, 8))[0]
        if not manager:return None
        port = struct.unpack('<i', read(manager+DEFAULT_PORT, 4))[0]
        if not 0 <= port <= PORT_MAX:return None
        block = manager+PORT_BLOCK+port*PORT_STRIDE
        forced, count = struct.unpack('<i', read(block+SET_FORCED, 4))[0], struct.unpack('<i', read(block+SET_COUNT, 4))[0]
        if forced != -1 or count <= 0:return None
        head = struct.unpack('<Q', read(block+SET_LIST, 8))[0]
        return struct.unpack('<i', read(head, 4))[0] if head else None
    except (OSError, ValueError, struct.error):
        return None


class NativeAnalogActions:
    """Owns the cave and table for the life of the process. publish() is the only writer of the table;
    install()/restore() take the runtime's all-thread code writer (the wrapper runs on several game threads)."""

    def __init__(self, base, allocation, read=C.string_at, memmove=C.memmove):
        self.base = int(base); self.read = read; self.memmove = memmove
        if not verify(read, self.base):raise RuntimeError('Exocraft controls exact-build code mismatch; no patch applied')
        self.allocation = int(allocation); self.data = self.allocation+DATA_OFFSET
        self.code = cave_code(self.allocation, self.base, self.data)
        if len(self.code) > DATA_OFFSET:raise ValueError('exocraft controls cave overflow')
        self.site = self.base+WRAPPER; self.patch = site_patch(self.site, self.allocation)
        self.installed = False; self.answers = {}
        self._write(0, bytes(DATA_SIZE))
        self.memmove(self.allocation, self.code, len(self.code))
        if self.read(self.allocation, len(self.code)) != self.code:raise RuntimeError('exocraft controls cave readback failed')

    def _write(self, offset, raw):self.memmove(self.data+offset, raw, len(raw))

    def install(self, write_code, pm, pid):
        if self.installed:return True
        if not verify(self.read, self.base):raise RuntimeError('Exocraft controls code changed before installation')
        write_code(pm, pid, self.site, WRAPPER_BYTES[:SITE_SIZE], self.patch)
        self.installed = True
        return True

    def restore(self, write_code, pm, pid):
        self.disable()
        current = self.read(self.site, SITE_SIZE)
        if current == self.patch:write_code(pm, pid, self.site, self.patch, WRAPPER_BYTES[:SITE_SIZE])
        elif current != WRAPPER_BYTES[:SITE_SIZE]:raise RuntimeError('foreign bytes at the analog action wrapper')
        self.installed = False

    def enable(self):self._write(ENABLED, struct.pack('<I', 1))

    def disable(self):
        """Off, and every answer withdrawn (a disabled cave answers native only)."""
        self._write(ENABLED, bytes(4)); self.publish({})

    def publish(self, answers, action_set=VEHICLE_MODE):
        """answers: {action id: value} answered while action_set is current. Actions left out answer native.
        The value is written before its set, so a reader never sees a set with a stale value."""
        answers = {a: float(v) for a, v in answers.items() if 0 <= a < COUNT}
        for action in set(self.answers)-set(answers):self._write(TABLE+action*ENTRY, bytes(4))
        for action, value in answers.items():
            entry = TABLE+action*ENTRY
            self._write(entry+4, struct.pack('<f', value))
            if action not in self.answers:self._write(entry, struct.pack('<I', action_set))
        self.answers = answers

    def status(self):
        return {'installed': self.installed, 'enabled': bool(struct.unpack('<I', self.read(self.data+ENABLED, 4))[0]),
                'answers': {hex(a): round(v, 3) for a, v in sorted(self.answers.items())},
                'hits': struct.unpack('<Q', self.read(self.data+HITS, 8))[0]}
