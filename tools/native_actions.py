"""T59 native digital actions: HOTAS buttons bound straight to the game's own actions, build 183877 / Steam 26295378.

Static research: research/re-log/2026-10-16_codex_native-actions-static.md (build 180836) and
research/re-log/2026-10-28_claude_native-actions-183791.md (this build). Every digital query reaches the input
manager's slot +0x20, the wrapper 0x2CA55C0 (`bool Digital(manager, action, validation, port)`: RCX, EDX, R8D, R9D,
result AL): gameplay code through the controller getter 0x1445760, which first returns false unless the frontend
state app+0x89BEA8 is 0 or 2, and the quick menu and other UI by calling the slot directly (0x15548BF: Quick_Left,
Pressed, port -1; 0.2.3-dev.2 live: a cave on the getter never saw them). The wrapper refreshes the action from Steam
and tail-jumps the core 0x2C97870, which resolves the port (-1 = manager+0x18) and hands the port block
manager+port*0x470+0x5B0 to 0x185BE10: Held, Pressed (down this frame), HeldConfirm (held 50 frames), Released (up this
frame), HeldOver, Canceled, from the action's record and the block's frame counter (+0x20).

One cave at the wrapper's entry (its first 10 bytes become a jmp). It calls the original first, so every native
read and its stateful records still run once. Only a false native answer can become true, and only when the cave is
enabled, the action is in the table and the query is for the manager's active port. The frontend gate stays the
game's: gameplay queries that fail it never reach the wrapper. The answer comes from a per-action table the runtime
writes (held level, press and release counters) and the game's own frame counter: a new press is stamped with the
frame of the first query that sees it, so Pressed is true for exactly that frame however many consumers ask, and
HeldConfirm counts 50 frames from it. Canceled stays native.

Construction never attaches to a process; bytes are verified against the recorded hashes first.
"""
import ctypes as C
import hashlib
import struct

from native_roll_patch import Code

APP_GLOBAL = 0x6F6C288
GETTER = 0x1449D00                 # the controller getter (pinned: its frontend gate and slot +0x20 jump)
GETTER_BYTES = bytes.fromhex('4c8bc9488b0d7e25b2058b81a8be890085c0740883f802740332c0c3488b89b8af9500458b4908488b0148ff6020')
WRAPPER = 0x2CAA370                # manager slot +0x20: refresh, then jmp CORE
WRAPPER_BYTES = bytes.fromhex('48895c240848896c24104889742418574883ec20418bf8418bd9458bc18bf2488be9e849200000448bcb448bc78bd6'
                              '488bcd488b5c2430488b6c2438488b7424404883c4205fe96522ffff')
REFRESH, CORE = 0x2CAC3E0, 0x2C9C620
SITE_SIZE = 10                     # mov [rsp+8], rbx; mov [rsp+0x10], rbp
CONTINUE = WRAPPER+SITE_SIZE
FRONTEND, MANAGER = 0x89BEA8, 0x95AFB8
DEFAULT_PORT, PORT_STRIDE, PORT_BLOCK, BLOCK_FRAME, PORT_MAX = 0x18, 0x470, 0x5B0, 0x20, 5
# The core and validation helper the cave's port and frame reads come from (pinned, not called).
PIN_RANGES = ((GETTER, len(GETTER_BYTES)), (WRAPPER, len(WRAPPER_BYTES)), (CORE, 0x8D), (0x18600E0, 0x50))
PIN_SHA256 = '7a56567e3b9fd6bfe1fc677ccc8d142d3f65e61ada611df1129cfabb2f4d4ebb'  # sha256 over PIN_RANGES of the exe (research/re-log/2026-10-28_claude_native-actions-183791.md)

HELD, PRESSED, HELD_CONFIRM, RELEASED, HELD_OVER, CANCELED = range(6)  # cTkInputValidation
CONFIRM_FRAMES = 50                # 0x185BE10: HeldConfirm after 0x32 frames
COUNT = 0x110                      # action ids 0..0x10F (cGcInputActions up to Player_InteractSecondary 0x103)
ENTRY = 0x20                       # held, press seq, release seq, seen press, seen release, down frame, up frame
ENABLED, HITS, TABLE = 0x00, 0x08, 0x40
# T125: in VR a bound fire button did not fire (user 2026-11-13). ANY_PORT lets the cave answer whichever port asks
# (set only with Experimental VR on); the counters say what the game asked for: MISSES = a held action asked for on
# a port that is not the active one (with that port and the active port), entry+ASKED = asked for while held.
MISSES, MISS_PORT, MISS_ACTIVE, ANY_PORT, ASKED = 0x10, 0x18, 0x1C, 0x20, 0x1C
DATA_OFFSET = 0x1000
DATA_SIZE = TABLE+COUNT*ENTRY
ALLOCATION = DATA_OFFSET+((DATA_SIZE+0xFFF) & ~0xFFF)
NEVER = 0xFFFFFFFF                 # down/up frame before the first edge

JE, JNE, JA, JAE, JZ = b'\x0f\x84', b'\x0f\x85', b'\x0f\x87', b'\x0f\x83', b'\x0f\x84'


def verify(read, base):
    """True only when every pinned range matches build 183791 and the site holds its original bytes."""
    h = hashlib.sha256()
    for rva, size in PIN_RANGES: h.update(read(base+rva, size))
    return h.hexdigest() == PIN_SHA256 and read(base+WRAPPER, SITE_SIZE) == WRAPPER_BYTES[:SITE_SIZE]


def cave_code(cave, base, data):
    c = Code(cave)
    def d(offset): return data+offset
    c.emit(bytes.fromhex('53 56 57 41 54 48 83 ec 28'))     # push rbx, rsi, rdi, r12; shadow space (RSP 16-aligned)
    c.emit(bytes.fromhex('48 89 ce 89 d7 44 89 c3 45 89 cc'))  # rsi = manager, edi = action, ebx = validation, r12d = port
    c.relative(b'\xe8', 'original')                          # the native answer, all its reads once
    c.emit(bytes.fromhex('84 c0')); c.relative(JNE, 'done')  # native true stays true
    c.relative(b'\x83\x3d', d(ENABLED), b'\x01'); c.relative(JNE, 'done')
    c.emit(bytes.fromhex('81 ff')+struct.pack('<I', COUNT)); c.relative(JAE, 'done')
    c.emit(bytes.fromhex('44 89 e1 8b 56')+bytes([DEFAULT_PORT]))  # ecx = port, edx = active port
    c.emit(bytes.fromhex('83 f9 ff')); c.relative(JNE, 'port')
    c.emit(bytes.fromhex('89 d1'))
    c.label('port')
    c.emit(bytes.fromhex('39 d1')); c.relative(JE, 'port-ok')  # the active player's port
    c.relative(b'\x83\x3d', d(ANY_PORT), b'\x01'); c.relative(JE, 'port-ok')  # T125: or any port, while switched on
    # Another port asked. If we hold that action, count it and note both ports; the answer stays false.
    c.relative(b'\x4c\x8d\x05', d(TABLE))
    c.emit(bytes.fromhex('89 f8 48 c1 e0 05 49 01 c0'))      # r8 = entry (action * 0x20)
    c.emit(bytes.fromhex('41 83 38 00')); c.relative(JE, 'false')
    c.relative(b'\x48\xff\x05', d(MISSES)); c.relative(b'\x89\x0d', d(MISS_PORT)); c.relative(b'\x89\x15', d(MISS_ACTIVE))
    c.relative(b'\xe9', 'false')
    c.label('port-ok')
    c.emit(bytes.fromhex('83 f9')+bytes([PORT_MAX])); c.relative(JA, 'false')
    c.emit(bytes.fromhex('48 69 c9')+struct.pack('<I', PORT_STRIDE))
    c.emit(bytes.fromhex('44 8b 9c 0e')+struct.pack('<I', PORT_BLOCK+BLOCK_FRAME))  # r11d = this frame
    c.relative(b'\x4c\x8d\x05', d(TABLE))
    c.emit(bytes.fromhex('89 f8 48 c1 e0 05 49 01 c0'))      # r8 = entry (action * 0x20)
    c.emit(bytes.fromhex('41 83 38 00')); c.relative(JE, 'not-held')
    c.emit(bytes.fromhex('41 ff 40')+bytes([ASKED]))          # asked for while held
    c.label('not-held')
    # A new press or release takes the frame of the first query that sees it.
    c.emit(bytes.fromhex('41 8b 40 04 41 3b 40 0c')); c.relative(JE, 'no-press')
    c.emit(bytes.fromhex('41 89 40 0c 45 89 58 14'))
    c.label('no-press')
    c.emit(bytes.fromhex('41 8b 40 08 41 3b 40 10')); c.relative(JE, 'no-release')
    c.emit(bytes.fromhex('41 89 40 10 45 89 58 18'))
    c.label('no-release')
    c.emit(bytes.fromhex('31 c0'))
    for validation, label in ((HELD, 'held'), (PRESSED, 'pressed'), (HELD_CONFIRM, 'confirm'),
                              (RELEASED, 'released'), (HELD_OVER, 'held')):
        c.emit(bytes.fromhex('83 fb')+bytes([validation])); c.relative(JE, label)
    c.relative(b'\xe9', 'done')                              # Canceled and anything else: native
    c.label('held')
    c.emit(bytes.fromhex('41 83 38 00 0f 95 c0')); c.relative(b'\xe9', 'count')
    c.label('pressed')
    c.emit(bytes.fromhex('45 3b 58 14 0f 94 c0')); c.relative(b'\xe9', 'count')
    c.label('released')
    c.emit(bytes.fromhex('45 3b 58 18 0f 94 c0')); c.relative(b'\xe9', 'count')
    c.label('confirm')
    c.emit(bytes.fromhex('41 83 38 00')); c.relative(JE, 'done')
    c.emit(bytes.fromhex('44 89 d9 41 2b 48 14 83 f9')+bytes([CONFIRM_FRAMES])+bytes.fromhex('0f 93 c0'))
    c.label('count')
    c.emit(bytes.fromhex('84 c0')); c.relative(JE, 'done')
    c.relative(b'\x48\xff\x05', d(HITS)); c.relative(b'\xe9', 'done')
    c.label('false')                                         # AL may hold anything from here on: answer false
    c.emit(bytes.fromhex('31 c0'))
    c.label('done')
    c.emit(bytes.fromhex('48 83 c4 28 41 5c 5f 5e 5b c3'))
    c.label('original')                                      # the replaced 10 bytes, then the rest of the wrapper
    c.emit(WRAPPER_BYTES[:SITE_SIZE]); c.relative(b'\xe9', base+CONTINUE)
    return c.finish()


def site_patch(site, cave):
    return b'\xe9'+struct.pack('<i', cave-site-5)+b'\x90'*(SITE_SIZE-5)


class NativeActions:
    """Owns the cave and table for the life of the process. step() is the only writer of the table's inputs;
    install()/restore() take the runtime's all-thread code writer (the wrapper runs on several game threads)."""
    EDGE_MS = 150  # a press or release no query took by then is dropped (opening a menu later must not replay it)

    def __init__(self, base, allocation, read=C.string_at, memmove=C.memmove):
        self.base = int(base); self.read = read; self.memmove = memmove
        if not verify(read, self.base):raise RuntimeError('Native actions exact-build code mismatch; no patch applied')
        self.allocation = int(allocation); self.data = self.allocation+DATA_OFFSET
        self.code = cave_code(self.allocation, self.base, self.data)
        if len(self.code) > DATA_OFFSET:raise ValueError('native actions cave overflow')
        self.site = self.base+WRAPPER; self.patch = site_patch(self.site, self.allocation)
        self.installed = False; self.held = set(); self.edges = {}; self.ever = set()
        self._write(0, bytes(DATA_SIZE))
        for action in range(COUNT):self._write(TABLE+action*ENTRY+0x14, struct.pack('<II', NEVER, NEVER))
        self.memmove(self.allocation, self.code, len(self.code))
        if self.read(self.allocation, len(self.code)) != self.code:raise RuntimeError('native actions cave readback failed')

    def _write(self, offset, raw):self.memmove(self.data+offset, raw, len(raw))

    def _u32(self, offset):return struct.unpack('<I', self.read(self.data+offset, 4))[0]

    def install(self, write_code, pm, pid):
        if self.installed:return True
        if not verify(self.read, self.base):raise RuntimeError('Native actions code changed before installation')
        write_code(pm, pid, self.site, WRAPPER_BYTES[:SITE_SIZE], self.patch)
        self.installed = True
        return True

    def restore(self, write_code, pm, pid):
        self.disable()
        current = self.read(self.site, SITE_SIZE)
        if current == self.patch:write_code(pm, pid, self.site, self.patch, WRAPPER_BYTES[:SITE_SIZE])
        elif current != WRAPPER_BYTES[:SITE_SIZE]:raise RuntimeError('foreign bytes at the digital action wrapper')
        self.installed = False

    def enable(self):self._write(ENABLED, struct.pack('<I', 1))

    def set_any_port(self, on):
        """T125: answer whichever port asks (VR), not only the manager's active one."""
        self._write(ANY_PORT, struct.pack('<I', 1 if on else 0))

    def disable(self):
        """Off, and every synthetic hold let go (a disabled cave answers native only)."""
        self._write(ENABLED, bytes(4)); self.step(())

    def step(self, held, now_ms=0):
        """held: native action ids (ints) held this tick. Writes level changes and edge counters; drops edges no
        query took within EDGE_MS."""
        held = {a for a in held if 0 <= a < COUNT}
        for action in held ^ self.held:
            entry = TABLE+action*ENTRY
            down = action in held
            seq = (self._u32(entry+(4 if down else 8))+1) & 0xFFFFFFFF
            self._write(entry, struct.pack('<I', 1 if down else 0))
            self._write(entry+(4 if down else 8), struct.pack('<I', seq))
            self.edges[(action, down)] = now_ms
        self.held = held; self.ever |= held
        for (action, down), since in list(self.edges.items()):
            entry = TABLE+action*ENTRY; seq, seen = (4, 0xC) if down else (8, 0x10)
            if self._u32(entry+seq) == self._u32(entry+seen):del self.edges[(action, down)]
            elif now_ms-since > self.EDGE_MS:
                self._write(entry+seen, struct.pack('<I', self._u32(entry+seq)));del self.edges[(action, down)]

    def status(self):
        return {'installed': self.installed, 'enabled': bool(self._u32(ENABLED)), 'held': sorted(self.held),
                'hits': struct.unpack('<Q', self.read(self.data+HITS, 8))[0],
                # T125 diagnostics: did the game ask for what we held, and on which port.
                'any_port': bool(self._u32(ANY_PORT)), 'misses': struct.unpack('<Q', self.read(self.data+MISSES, 8))[0],
                'miss_port': self._u32(MISS_PORT), 'miss_active': self._u32(MISS_ACTIVE),
                'asked': {f'{a:#x}': self._u32(TABLE+a*ENTRY+ASKED) for a in sorted(self.ever)}}

