"""Unscaled native cockpit forward-speed readout, NMS 183877 / Steam 26295378.

Only four display scalars change. Native HUD code still measures signed forward
speed; physics, thrust, speed caps and boost authority are untouched. Exact HUD
consumer instructions pin every field independently of NMSpy's struct version.
"""
import ctypes as C
import struct
import threading


# name: (direct data RVA, stock value, unscaled value)
FIELDS = {
    'SpaceSpeedReadoutMultiplier': (0x5331608, 2., 1.),
    'SpeedReadoutMultiplier': (0x5331634, 1., 1.),
    'CombatSpeedReadoutMultiplier': (0x5331088, 1.5, 1.),
    'HUDBoostUpgradeMultiplier': (0x52F6474, 10., 0.),
}
PINS = {
    0x9E8EE5: bytes.fromhex('f30f10351b879404'),
    0x9E8EED: bytes.fromhex('f30f5c353f879404'),
    0x9E8F05: bytes.fromhex('f30f583527879404'),
    0x9E8F28: bytes.fromhex('f30f593558819404'),
    0x9E8FAF: bytes.fromhex('f30f5905bdd49004'),
}


class CockpitSpeedReadout:
    def __init__(self, base, read=C.string_at, write=C.memmove):
        self.base, self.read, self.write = int(base), read, write
        self.lock = threading.Lock()
        self.verified = False
        self.closed = False
        self.originals = {}
        self.latest = {'state': 'waiting'}

    def _write(self, name, raw):
        address = self.base + FIELDS[name][0]
        self.write(address, raw, 4)
        if self.read(address, 4) != raw:
            raise RuntimeError('Cockpit display readback failed: ' + name)

    def update(self):
        """Worker-only poll, also handling native globals reset after loading.

        Recognized stock resets can be reapplied. Unexpected external values
        relinquish ownership instead of repeatedly overwriting another mod.
        All failures stay local to this display feature.
        """
        with self.lock:
            if self.closed:
                return self.status()
            try:
                if not self.verified:
                    if not all(self.read(self.base + rva, len(raw)) == raw
                               for rva, raw in PINS.items()):
                        self.closed = True
                        self.latest = {'state': 'code signature mismatch'}
                        return self.status()
                    self.verified = True
                current = {name: self.read(self.base + spec[0], 4)
                           for name, spec in FIELDS.items()}
                if not self.originals and all(raw == bytes(4) for raw in current.values()):
                    self.latest = {'state': 'waiting for display globals'}
                    return self.status()
                for name, (_, stock, target) in FIELDS.items():
                    if current[name] not in (struct.pack('<f', stock), struct.pack('<f', target)):
                        self._restore()
                        self.latest = {'state': 'external display settings changed', 'field': name}
                        return self.status()
                for name, (_, stock, target) in FIELDS.items():
                    desired = struct.pack('<f', target)
                    if current[name] == desired:
                        continue
                    # Save before attempting the write, including partial failures.
                    self.originals[name] = current[name]
                    self._write(name, desired)
                self.latest = {'state': 'actual forward speed'}
            except BaseException as error:
                restored = self._restore()
                self.latest = {'state': 'disabled', 'error': repr(error), 'restored': restored}
            return self.status()

    def _restore(self):
        # Permanent before the first read/write: even a late worker may not reapply.
        self.closed = True
        restored = {}
        for name, original in list(self.originals.items()):
            try:
                desired = struct.pack('<f', FIELDS[name][2])
                if self.read(self.base + FIELDS[name][0], 4) == desired:
                    self._write(name, original)
                # Preserve a newer native or third-party value. This is a data
                # read/write, not an atomic transaction with external writers.
                restored[name] = True
                del self.originals[name]
            except BaseException as error:
                restored[name] = repr(error)
        return restored

    def restore(self):
        with self.lock:
            result = self._restore()
            state = 'restore failed' if self.originals else 'restored'
            self.latest = {'state': state, 'restored': result}
            return result

    def status(self):
        return {**self.latest, 'verified': self.verified, 'owned': list(self.originals)}
