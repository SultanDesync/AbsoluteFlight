"""T66 strict late-rotation gates; source fork of the qualified F13 checks.

Build 183877/Steam 26295378 only (remapped from 180836). These are emitted at the actual target seam,
not inferred from a Python boolean or the later translation application receipt.
No process attachment, allocation, or game mutation occurs in this module.
"""
import struct


def emit(c, data, base, tick, *, reject='out', prefix='', check_clock=True, allow_planetary_flight=False):
    """Require pinned current Newtonian invocation and complete strict fixture.

    Caller saves volatile state/MXCSR and supplies an aligned Win64 stack.
    No successful prior-frame receipt is read. Strict combat/power/planet
    exclusions apply even when the release translation cave opts into combat.
    allow_planetary_flight (T92): while the Newtonian cave's T88 PLANETARY switch
    is on, tolerate the low-height gate and a nonzero ground-clearance term, as
    the final cave does. Off, the bytes are the strict space-only gates.
    """
    from native_newtonian import WORK, VECTORS, REQUIRED, LOW_HEIGHT, low_height_gate, planetary_skip

    def ne(prefix, address, suffix=b''):
        c.relative(prefix, address, suffix); c.relative(b'\x0f\x85', reject)

    def ins(raw):
        c.emit(bytes.fromhex(raw)); c.relative(b'\x0f\x85', reject)

    def zero(reg, offset):
        c.emit((b'\x41' if reg >= 8 else b'') + bytes([0x8b, 0x80 + reg % 8]) + struct.pack('<i', offset))
        c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0')); c.relative(b'\x0f\x85', reject)

    ne(b'\x83\x3d', data, b'\x01'); ne(b'\x83\x3d', data + 4, b'\x01')
    ne(b'\x48\x3b\x35', data + 8); ne(b'\x48\x3b\x2d', data + 40)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00')); ne(b'\x3b\x05', data + 56)
    c.emit(b'\x48\xb8' + struct.pack('<Q', base + 0x1783F41)); ins('48 39 85 68 41 00 00')
    if check_clock:
        c.emit(b'\x48\xb8' + struct.pack('<Q', tick) + b'\xff\xd0')
        c.relative(b'\x48\x3b\x05', data + 72); c.relative(b'\x0f\x83', reject)
    c.emit(bytes.fromhex('48 8b 86 58 62 00 00')); ne(b'\x48\x3b\x05', data + 160)
    c.emit(bytes.fromhex('48 83 c0 50')); ne(b'\x48\x3b\x05', data + 16)
    ne(b'\x4c\x3b\x2d', data + 16)
    c.emit(bytes.fromhex('49 8b 85 90 02 00 00')); ne(b'\x48\x3b\x05', data + 24)
    c.emit(bytes.fromhex('48 89 c2 48 8b 42 08')); ne(b'\x48\x3b\x05', data + 32)
    c.emit(bytes.fromhex('49 8b 85 28 03 00 00')); ne(b'\x48\x3b\x05', data + 32)
    c.emit(bytes.fromhex('49 89 c0 48 8b 82 88 00 00 00')); ne(b'\x48\x3b\x05', data + 112)
    c.relative(b'\x48\x8b\x05', base + 0x6f5f868); ne(b'\x48\x3b\x05', data + 80)
    c.emit(bytes.fromhex('48 8b 80 48 cf 72 00')); ne(b'\x48\x3b\x05', data + 304)
    c.emit(bytes.fromhex('48 8b 86 c0 60 00 00')); ne(b'\x48\x3b\x05', data + 168)
    c.emit(bytes.fromhex('48 8b 00')); ne(b'\x48\x3b\x05', data + 176)
    c.emit(bytes.fromhex('48 8b 46 30')); ne(b'\x48\x3b\x05', data + 312)
    c.emit(bytes.fromhex('8b 80 d0 00 00 00')); ne(b'\x3b\x05', data + 320)
    for raw in ('80 be c8 60 00 00 01', '83 be 5c 67 00 00 00', '80 be 30 67 00 00 00',
                '83 be 18 73 00 00 00', '83 be 30 64 00 00 00', '80 be e0 73 00 00 00',
                '80 be ed 76 00 00 00', '80 be e4 6c 00 00 00', '80 be 40 77 00 00 00',
                '83 be 18 67 00 00 00'):
        if allow_planetary_flight and raw == LOW_HEIGHT: low_height_gate(c, data, reject, prefix + 'low_height_ok')
        else: ins(raw)
    c.emit(bytes.fromhex('f6 42 01 18')); c.relative(b'\x0f\x85', reject)
    ins('49 83 b8 38 0a 00 00 01')
    c.relative(b'\x48\x8b\x05', base + 0x6f5f868)
    for raw in ('83 b8 98 bd 89 00 00', '80 b8 6d aa 57 00 00', '83 b8 dc b6 72 00 00', '83 b8 cc b6 72 00 00'):
        ins(raw)
    # A native camera-look condition changes pitch formation. Do not override it.
    c.emit(bytes.fromhex('48 8b 80 30 cf 72 00 48 85 c0')); c.relative(b'\x0f\x84', reject)
    ins('80 b8 c9 01 00 00 00')
    c.emit(bytes.fromhex('49 8b 80 80 03 00 00')); ne(b'\x48\x3b\x05', data + 88)
    c.emit(bytes.fromhex('48 89 c7 48 8b 40 40')); ne(b'\x48\x3b\x05', data + 256)
    c.relative(b'\x48\x8b\x05', data + 264); c.emit(bytes.fromhex('48 8b 00')); ne(b'\x48\x3b\x05', data + 272)
    c.emit(bytes.fromhex('48 8b 87 38 07 00 00')); ne(b'\x48\x3b\x05', data + 280)
    c.relative(b'\x48\x8b\x05', data + 288); c.emit(bytes.fromhex('48 8b 00')); ne(b'\x48\x3b\x05', data + 296)
    c.relative(b'\x48\x8b\x0d', data + 96)
    c.emit(bytes.fromhex('48 8b 81 b8 00 00 00')); ne(b'\x48\x3b\x05', data + 16)
    c.emit(bytes.fromhex('8b 81 80 00 00 00')); ne(b'\x3b\x05', data + 120)
    c.emit(bytes.fromhex('8b 41 54')); ne(b'\x3b\x05', data + 124)
    c.emit(bytes.fromhex('48 8b 87 b0 0b 00 00')); ne(b'\x48\x3b\x05', data + 328)
    c.emit(bytes.fromhex('48 89 c1 8b 81 7c 0f 00 00')); ne(b'\x3b\x05', data + 336)
    c.emit(bytes.fromhex('8b 81 78 0f 00 00')); ne(b'\x3b\x05', data + 340)
    for index in range(16):
        c.relative(b'\x83\x3d', data + 336, bytes([index])); c.relative(b'\x0f\x86', prefix + 'callbacks_checked')
        c.emit(b'\x48\x8b\x81' + struct.pack('<I', 0xf80 + 16 * index)); ne(b'\x48\x3b\x05', data + 344 + 16 * index)
        c.emit(b'\x48\x8b\x81' + struct.pack('<I', 0xf88 + 16 * index)); ne(b'\x48\x3b\x05', data + 352 + 16 * index)
    c.label(prefix + 'callbacks_checked')
    c.relative(b'\x48\x8b\x0d', data + 104)
    for off in (0x74, 0x76):
        c.emit(bytes.fromhex('66 83 b9') + struct.pack('<i', off) + b'\x00'); c.relative(b'\x0f\x85', reject)
    for off in (0x270, 0x274): zero(13, off)
    c.emit(bytes.fromhex('0f b7 41 78')); ne(b'\x3b\x05', data + 188)
    c.emit(bytes.fromhex('41 8b 85 7c 02 00 00')); ne(b'\x3b\x05', data + 184)
    c.emit(bytes.fromhex('41 8b 85 38 03 00 00')); ne(b'\x3b\x05', data + 192)
    for i in range(3):
        c.emit(bytes.fromhex('41 8b 85') + struct.pack('<I', 0x340 + 4 * i)); ne(b'\x3b\x05', data + 196 + 4 * i)
    # This trial stays at zero-gravity, even if a translation fixture was extended.
    zero(13, 0x27c); ins('66 83 79 78 00'); ins('41 83 bd 38 03 00 00 00')
    c.emit(bytes.fromhex('41 8b 85 60 02 00 00 85 c0')); c.relative(b'\x0f\x8e', reject)
    c.emit(bytes.fromhex('3d 00 00 80 7f')); c.relative(b'\x0f\x83', reject)
    for address in (base + 0x7232e78 + 0x18, base + 0x7232e78 + 0x24):
        c.relative(b'\x8b\x05', address); c.emit(bytes.fromhex('85 c0')); c.relative(b'\x0f\x8e', reject)
        c.emit(bytes.fromhex('3d 00 00 80 7f')); c.relative(b'\x0f\x83', reject)
    c.relative(b'\xf3\x0f\x10\x05', base + 0x7232e78 + 0x18)
    c.relative(b'\xf3\x0f\x59\x05', base + 0x7232e78 + 0x24)
    c.emit(bytes.fromhex('66 0f 7e c0 85 c0')); c.relative(b'\x0f\x8e', reject)
    c.emit(bytes.fromhex('3d 00 00 80 7f')); c.relative(b'\x0f\x83', reject)
    c.relative(b'\x8b\x05', base + 0x7232e78 + 0x30)
    c.emit(bytes.fromhex('f7 c0 00 ff 00 00')); c.relative(b'\x0f\x84', prefix + 'clock_running')
    c.emit(bytes.fromhex('f7 c0 00 00 ff 00')); c.relative(b'\x0f\x84', reject)
    c.label(prefix + 'clock_running')
    c.relative(b'\x8b\x05', data + 60)
    c.emit(b'\x25' + struct.pack('<I', REQUIRED) + b'\x3d' + struct.pack('<I', REQUIRED)); c.relative(b'\x0f\x85', reject)
    if allow_planetary_flight: planetary_skip(c, data, prefix + 'height_tolerated')
    for i in range(3):
        c.relative(b'\x8b\x05', data + WORK + VECTORS['height'] + 4 * i)
        c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0')); c.relative(b'\x0f\x85', reject)
    if allow_planetary_flight: c.label(prefix + 'height_tolerated')
    for i in range(3):
        for address in (None, data + WORK + VECTORS['gravity'] + 4 * i):
            if address is None: c.emit(b'\x8b\x82' + struct.pack('<i', 0x60 + 4 * i))
            else: c.relative(b'\x8b\x05', address)
            c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 7f')); c.relative(b'\x0f\x83', reject)
