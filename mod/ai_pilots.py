"""Readable enemy pilots: Newtonian-compatible space combat AI tuning, NMS 183791 / Steam 26271400.

NPC ships fly by force (research/23-combat-ai-newtonian.md). Each frame the shared AI follower
0x17BE2F0 reads one cGcSpaceshipTravelData engine row and applies, as mass * acceleration:
thrust Force along the desired heading, faded linearly from Falloff*MaxSpeed to MaxSpeed, plus
-DirectionBrake * (sideways velocity). DirectionBrake is an uncapped "grip": 3-5 per second
kills sideways drift in 0.2-0.33 s, so vanilla pirates fly on rails while a Newtonian player
slides. This tuning only changes numbers in the loaded cGcShipAIAttackDataTable rows that
space combat uses; no code is patched and nothing is hooked.

Pointer chain, every link pinned below: cGcApplication::Data = [NMS+0x6F60828];
cGcSimulation = Data+0x4CD580; cGcAISpaceshipManager = Sim+0x200990; its loaded attack table
pointer is at manager+0x60, so table = [Data+0x6CDF70]. The table is a heap buffer loaded once
by the manager's Construct; a reconstruct loads a fresh vanilla buffer, which is re-tuned.
"""
import math
import struct
import threading

APP_GLOBAL = 0x6F60828
TABLE_FROM_DATA = 0x4CD580 + 0x200990 + 0x60
PINS = {
    # mov rcx,[NMS+0x6F60828]; add rcx,0x4CD580; call cGcSimulation::Construct
    0x1B4067: bytes.fromhex('488b0dbac7da064881c180d54c00e816942201'),
    # lea rcx,[rdi+0x200990]; call cGcAISpaceshipManager::Construct
    0x13DD82A: bytes.fromhex('488d8f90092000e8dafd3600'),
    # call LoadAttackTable; xor r12d,r12d; mov [rbx+0x60],rax
    0x174D63A: bytes.fromhex('e8b1ad00004533e448894360'),
    # follower reads MaxSpeed +0x24, MaxSpeedBrake +0x28, DirectionBrake +0x18
    0x17BE384: bytes.fromhex('f3450f105624450f2fc20f5900f3450f107628f3450f107e18'),
    0x17BE3D5: bytes.fromhex('f3410f10561c'),  # Falloff +0x1C
    0x17BE44E: bytes.fromhex('f3410f596620'),  # Force +0x20
}
# cGcShipAIAttackDataTable dynamic arrays: (pointer offset, element stride). Count is i32 at +8.
ARRAYS = {'behaviour': (0x40, 0xD0), 'engine': (0x60, 0x48)}
FIELDS = {
    'engine': {'DirectionBrake': 0x18, 'Falloff': 0x1C, 'Roll': 0x38, 'TurnMax': 0x3C, 'TurnMin': 0x40},
    'behaviour': {'AttackBoostAngle': 0x20, 'AttackReadyTime': 0x38, 'AttackTurnMultiplier': 0x68,
                  'FleeRepositionUrgentTime': 0xA0, 'FleeUrgentBrake': 0xA8},
}
# (array, row Id, field): (vanilla, readable). Vanilla is the shipped AISPACESHIPATTACKDATATABLE
# on this build; a different value means another mod or a patch owns it, so we never write.
# Only space dogfight rows: planet, raid, player squadron and swarm rows stay native.
TUNING = {
    # Grip 3-5/s -> ~1/s: velocity lags the nose ~1 s, they slide out of turns like you do.
    ('engine', 'SPACE_EASY', 'DirectionBrake'): (5.0, 0.8),
    ('engine', 'SPACE_HARD', 'DirectionBrake'): (3.0, 0.8),
    # Thrust fades over the top half of speed, not the top 20%: speed builds visibly.
    ('engine', 'SPACE_EASY', 'Falloff'): (0.8, 0.5),
    ('engine', 'SPACE_HARD', 'Falloff'): (0.8, 0.5),
    # Lower peak turn rate; bank into every turn so the roll telegraphs it.
    ('engine', 'SPACE_EASY', 'TurnMax'): (1.2, 0.9),
    ('engine', 'SPACE_HARD', 'TurnMax'): (1.6, 0.95),
    ('engine', 'SPACE_EASY', 'TurnMin'): (0.6, 0.5),
    ('engine', 'SPACE_HARD', 'TurnMin'): (0.7, 0.6),
    ('engine', 'SPACE_HARD', 'Roll'): (0.6, 1.0),
    # No extra turn rate on attack runs; boost only when nearly lined up (straight, readable).
    ('behaviour', 'SPACE', 'AttackTurnMultiplier'): (1.15, 1.0),
    ('behaviour', 'SPACE', 'AttackBoostAngle'): (15.0, 8.0),
    # A beat between lining up and firing; commit longer to an evasive break; brake less hard.
    ('behaviour', 'SPACE', 'AttackReadyTime'): (0.5, 0.9),
    ('behaviour', 'SPACE', 'FleeRepositionUrgentTime'): (3.0, 4.5),
    ('behaviour', 'SPACE', 'FleeUrgentBrake'): (2.0, 1.0),
}
MAX_ROWS = 64
# Readable endpoints, with bounded menu ranges. Defaults retain the user-accepted v1 feel.
# These are shared by the settings schema and the worker so malformed input cannot escape them.
KNOBS = {
    'ai-lateral-grip': (0.8, 0.2, 3.0),
    'ai-turn-rate': (1.0, 0.5, 1.5),
    'ai-thrust-fade': (0.5, 0.1, 0.9),  # follower divides by 1 - Falloff
    'ai-attack-delay': (0.9, 0.25, 2.0),
    'ai-evasive-commitment': (4.5, 1.0, 8.0),
    'ai-boost-alignment': (8.0, 2.0, 30.0),
}


def f32(value):
    return struct.pack('<f', value)


def _bounded(value, default, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(high, max(low, float(value)))


def tuning_values(values=None):
    """Copy just the AI knobs, filling old settings with the accepted defaults."""
    values = values or {}
    return {key: _bounded(values.get(key, default), default, low, high)
            for key, (default, low, high) in KNOBS.items()}


def target_value(key, readability, tuning=None):
    """Blend vanilla -> configured readable endpoint; 0 is always exactly vanilla."""
    vanilla, readable = TUNING[key]
    knobs = tuning_values(tuning)
    field = key[2]
    if field == 'DirectionBrake': readable = knobs['ai-lateral-grip']
    elif field in ('TurnMin', 'TurnMax'): readable *= knobs['ai-turn-rate']
    elif field == 'Falloff': readable = knobs['ai-thrust-fade']
    elif field == 'AttackReadyTime': readable = knobs['ai-attack-delay']
    elif field == 'FleeRepositionUrgentTime': readable = knobs['ai-evasive-commitment']
    elif field == 'AttackBoostAngle': readable = knobs['ai-boost-alignment']
    r = _bounded(readability, 0., 0., 1.)
    if r == 0.: return vanilla
    return readable if r == 1. else vanilla + (readable - vanilla) * r


def wanted_mode(values):
    """'readable' or 'vanilla' from the ai-pilots setting; 'match' follows the flight model."""
    mode = values.get('ai-pilots', 'match')
    if mode == 'match':
        return 'readable' if values.get('flight-model') == 'sultan' else 'vanilla'
    return mode


class AIPilotTuning:
    # Shared guarded ownership engine; combat settings use disjoint fields/rows.
    arrays, fields, targets, pins = ARRAYS, FIELDS, TUNING, PINS
    row_id_offsets = {}
    normalize = staticmethod(tuning_values)
    target = staticmethod(target_value)

    def __init__(self, base, read, write):
        """read(address, size) -> bytes and write(address, data) must fail, never fault."""
        self.base, self.read, self.write = int(base), read, write
        self.lock = threading.Lock()
        self.verified = None
        self.closed = False
        self.table = 0
        self.owned = {}  # address -> (vanilla raw, written raw)
        self.applied = 0.
        self.applied_tuning = self.normalize()
        self.latest = {'state': 'waiting'}

    def _q(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def _rows(self, table):
        """Field address for every TUNING key, or raise if the table is not the expected one."""
        found = {}
        for array, (offset, stride) in self.arrays.items():
            data = self._q(table + offset)
            count = struct.unpack('<i', self.read(table + offset + 8, 4))[0]
            if not data or not 0 < count <= MAX_ROWS:
                raise ValueError(f'{array} table not loaded')
            for i in range(count):
                row = data + i * stride
                name = self.read(row + self.row_id_offsets.get(array, 0), 16).split(b'\0', 1)[0].decode('ascii', 'replace')
                for field, field_offset in self.fields[array].items():
                    if (array, name, field) in self.targets:
                        found[(array, name, field)] = row + field_offset
        missing = [k for k in self.targets if k not in found]
        if missing:
            raise ValueError('rows missing: ' + ', '.join(f'{a}/{r}' for a, r, _ in missing[:3]))
        return found

    def _restore(self):
        restored = 0
        for address, (vanilla, written) in list(self.owned.items()):
            try:
                if self.read(address, 4) == written:  # keep a newer native or third-party value
                    self.write(address, vanilla)
                restored += 1
                del self.owned[address]
            except BaseException:
                pass
        return restored

    def update(self, mode, readability=1., tuning=None):
        """Worker poll: apply, retarget or restore. Failures stay local to this feature."""
        with self.lock:
            if self.closed:
                return self.status()
            try:
                if self.verified is None:
                    self.verified = all(self.read(self.base + rva, len(raw)) == raw for rva, raw in self.pins.items())
                if not self.verified:
                    self.closed = True
                    self.latest = {'state': 'code signature mismatch'}
                    return self.status()
                data = self._q(self.base + APP_GLOBAL)
                table = self._q(data + TABLE_FROM_DATA) if data else 0
                if table != self.table:
                    # A reconstruct freed the old buffer: forget it, never write there again.
                    self.owned.clear()
                    self.table = table
                if not table:
                    self.latest = {'state': 'waiting for AI tables'}
                    return self.status()
                addresses = self._rows(table)
                strength = _bounded(readability, 0., 0., 1.) if mode == 'readable' else 0.
                knobs = self.normalize(tuning)
                current = {key: self.read(address, 4) for key, address in addresses.items()}
                for key, address in addresses.items():
                    vanilla = f32(self.targets[key][0])
                    mine = self.owned.get(address, (None, None))[1]
                    if current[key] not in (vanilla, mine):
                        restored = self._restore()
                        self.closed = True
                        self.latest = {'state': 'AI table changed by something else; vanilla kept',
                                       'field': '/'.join(key[1:]), 'restored': restored}
                        return self.status()
                for key, address in addresses.items():
                    desired = f32(self.target(key, strength, knobs))
                    if current[key] == desired:
                        continue
                    vanilla = f32(self.targets[key][0])
                    # Own it before writing, so a partial failure is still restored.
                    self.owned[address] = (vanilla, desired)
                    self.write(address, desired)
                    if self.read(address, 4) != desired:
                        raise RuntimeError('AI table readback failed: ' + '/'.join(key[1:]))
                    if desired == vanilla:
                        del self.owned[address]
                self.applied = strength
                self.applied_tuning = knobs
                self.latest = {'state': 'readable' if strength > 0 else 'vanilla'}
            except BaseException as error:
                restored = self._restore()
                self.closed = True
                self.latest = {'state': 'disabled', 'error': repr(error), 'restored': restored}
            return self.status()

    def restore(self):
        with self.lock:
            self.closed = True
            restored = self._restore()
            self.latest = {'state': 'restore failed' if self.owned else 'restored', 'restored': restored}
            return self.status()

    def status(self):
        return {**self.latest, 'readability': round(self.applied, 3), 'owned_fields': len(self.owned),
                'tuning': dict(self.applied_tuning)}
