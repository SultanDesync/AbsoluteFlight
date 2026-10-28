"""T66 rotation addons, exact build 183791/Steam 26271400 (remapped from 180836).

Owns only the previously unused late-target and torque-clamp detours. It reads
the existing Newtonian invocation/fixture and D12 frame without modifying them.
No process discovery, attachment, live install, menu edit, or automatic rearm.
Observe-only is the default. Explicit calibration supplies signed native full
targets and positive independent limits before bounded substitution is possible.
The separate production bridge compiles four independently measured endpoints
and renews only a <=50ms lease after the host verifies complete qualification.
Allocations are deliberately retained until process exit, including on failure.
"""
import ctypes as C
from collections.abc import Mapping
import math
import struct
import threading
from types import MappingProxyType

from native_roll_patch import Code, K, replace_code
from native_newtonian import _allocate_data, EXPECTED_SHA, planetary_skip
from native_additive_rotation import ORIGINAL, GLOBAL_RVA, SITE_RVA
from native_rotation_gates import emit as full_gates

from rotation_sustained_envelope import (freeze_windows, manifest as sustained_manifest, emit_window_gate,
                                        ORIGIN_OFFSET, ENVELOPE, PRODUCTION_ENVELOPE)

MAX_LEASE_MS = 50
STRICT_ROTATION = 0x80000000
TORQUE_RVA = 0x1791A9E
TORQUE_ORIGINAL = bytes.fromhex('0f5dd8410f14d3440f16c2440f5fc3')
WORK = 0x200
WORK_SIZE = 0x300
RING = 0x1000
SLOT = 0x400
CAPACITY = 128
ALLOCATION = RING + SLOT * CAPACITY + 0xc000
STACK = 0x110
SAVED_TOTAL = STACK + 80
AXES = ('pitch', 'yaw', 'roll')
ORDINARY_RVA = 0x17914a0
ORDINARY_ORIGINAL = bytes.fromhex('488b0de1187605')
SITES = {'ordinary': (ORDINARY_RVA, ORDINARY_ORIGINAL), 'target': (SITE_RVA, ORIGINAL), 'torque': (TORQUE_RVA, TORQUE_ORIGINAL)}
PREWORK = 0x500
MODE_FIELDS = {'q9ab670': (344, 4), 'route_flag': (348, 4), 'device_root': (352, 8),
               'device_index': (360, 4), 'device_descriptor': (368, 8), 'device_type0': (376, 4),
               'device_type4': (380, 4), 'pad_root': (384, 8), 'pad_index': (392, 4),
               'pad_descriptor': (400, 8), 'pad_type0': (408, 4), 'pad_type4': (412, 4)}
# T92 air terms, aligned floats the host writes each worker tick. The production air cave copies each
# once per frame, range-checks it (any value outside rejects to native) and applies the copy:
#   scale    pitch/yaw rate share left by the air (1.0 = none), AIR_RANGE
#   damping  share of the native rate controller kept in fast air (1.0 = native), DAMPING_RANGE
#   sway     pitch, yaw: observed-rate offsets the air swings the nose with (0 = none), |sway| <= SWAY_LIMIT
# Per axis: target' = response*(damping*(rate*scale*target + observed) - sway) - observed.
AIR_SCALE = 0x800
AIR_DAMPING = 0x804
AIR_SWAY = 0x808  # pitch, yaw
AIR_RANGE = (.25, 1.)
DAMPING_RANGE = (.1, 1.)
SWAY_LIMIT = 1.5  # rad/s observed rate; the host bounds its sway well inside this
AIR_RECEIPT = 736  # scale, damping, pitch sway, yaw sway
NATIVE_STAGES = {0: 'not_captured', 1: 'captured', 2: 'fixture_gates_passed', 3: 'freshness_passed',
                 4: 'source_provenance_passed', 5: 'ordinary_route_passed', 6: 'association_passed',
                 7: 'command_captured', 8: 'target_bounds_passed', 9: 'commit_freshness_passed',
                 10: 'command_authority_passed', 11: 'immutable_pins_passed', 12: 'commit_fixture_passed',
                 13: 'commit_provenance_passed', 14: 'applied'}


def read_engine_pins(pm, ship):
    """Read fixed effective engine bytes and selected-control clamp identity."""
    engine = pm.read_bytes(ship + 0x6640, 0x78)
    selected = struct.unpack('<Q', pm.read_bytes(ship + 0x66b8, 8))[0]
    if not selected: raise ValueError('No selected controls')
    torque = pm.read_bytes(selected + 0x1f0, 4)
    value = struct.unpack('<f', torque)[0]
    if not math.isfinite(value) or value <= 0: raise ValueError('Invalid native MaxTorque')
    return {'engine_snapshot_hex': engine.hex(), 'selected_control': selected,
            'max_torque_bits': struct.unpack('<I', torque)[0]}


def _pin_fixture(c, newtonian, fixture):
    from native_newtonian import initial_state
    state = initial_state(fixture, 0, 0)
    # Immutable geometry fields refreshed by native_newtonian.Bridge.refresh_fixture.
    # Comparing constants closes its same-allocation metadata replacement window.
    for start, end in ((8, 40), (80, 128), (160, 216), (256, 600)):
        for offset in range(start, end, 8):
            value = struct.unpack_from('<Q', state, offset)[0]
            c.emit(b'\x48\xb8' + struct.pack('<Q', value)); _ne(c, b'\x48\x3b\x05', newtonian + offset)


def _pin_engine(c, pins, newtonian=None):
    """Pin the effective engine snapshot, selected controls and native MaxTorque.

    With `newtonian` (T92) the snapshot compare is skipped while the T88 PLANETARY switch is on:
    in air native blends the whole block from the Planet engine (selectedControl+0xE8) toward Space
    (+0x15C) by ship+0x62C0 every frame, so TurnStrength and RollForce are legitimately live there,
    and shaping multiplies whatever native gain it finds. Combat and power engine contexts stay
    excluded by the strict gates; selected-control identity and MaxTorque are pinned either way.
    """
    if newtonian is not None: planetary_skip(c, newtonian, 'engine_blend_tolerated')
    for offset in range(0, 0x78, 4):
        value = bytes.fromhex(pins['engine_snapshot_hex'])[offset:offset + 4]
        c.emit(bytes.fromhex('81 be') + struct.pack('<I', 0x6640 + offset) + value); c.relative(b'\x0f\x85', 'out')
    if newtonian is not None: c.label('engine_blend_tolerated')
    c.emit(b'\x48\xb8' + struct.pack('<Q', pins['selected_control']) + bytes.fromhex('48 39 86 b8 66 00 00')); c.relative(b'\x0f\x85', 'out')
    c.emit(bytes.fromhex('81 b8 f0 01 00 00') + struct.pack('<I', pins['max_torque_bits'])); c.relative(b'\x0f\x85', 'out')


def read_mode_pins(pm, base):
    """Read-only bounded native route identities; caller fingerprints calibration."""
    def read(address, fmt): return struct.unpack(fmt, pm.read_bytes(address, struct.calcsize(fmt)))[0]
    app = read(base + 0x6f60828, '<Q')
    if not app: raise ValueError('Native application unavailable')
    out = {'q9ab670': read(app + 0x30e986, '<B'), 'route_flag': read(app + 0x30e930, '<I')}
    for name, root_address in (('device', app + 0x95aeb8), ('pad', base + 0x5364c40)):
        root = read(root_address, '<Q')
        if not root: raise ValueError('Missing native route root: ' + name)
        index = read(root + 0x18, '<i')
        if not 0 <= index <= 15: raise ValueError('Native device index outside trial bound')
        descriptor = read(root + 0x5f8 + 0x470*index, '<Q')
        if not descriptor: raise ValueError('Missing native descriptor: ' + name)
        out.update({name + '_root': root, name + '_index': index, name + '_descriptor': descriptor,
                    name + '_type0': read(descriptor, '<I'), name + '_type4': read(descriptor + 4, '<I')})
    return out


def _capture_modes(c, base, work):
    c.relative(b'\x48\x8b\x05', base + 0x6f60828)
    c.emit(bytes.fromhex('0f b6 88 86 e9 30 00')); c.relative(b'\x89\x0d', work + 344)
    c.emit(bytes.fromhex('8b 88 30 e9 30 00')); c.relative(b'\x89\x0d', work + 348)
    for name, offset in (('device', 352), ('pad', 384)):
        if name == 'device':
            c.relative(b'\x48\x8b\x05', base + 0x6f60828); c.emit(bytes.fromhex('48 8b 90 b8 ae 95 00'))
        else: c.relative(b'\x48\x8b\x15', base + 0x5364c40)
        c.relative(b'\x48\x89\x15', work + offset)
        c.emit(bytes.fromhex('48 85 d2')); c.relative(b'\x0f\x84', 'mode_capture_done')
        c.emit(bytes.fromhex('8b 42 18')); c.relative(b'\x89\x05', work + offset + 8)
        c.emit(bytes.fromhex('83 f8 0f')); c.relative(b'\x0f\x87', 'mode_capture_done')
        c.emit(bytes.fromhex('48 69 c0 70 04 00 00 48 8b 94 02 f8 05 00 00'))
        c.relative(b'\x48\x89\x15', work + offset + 16)
        c.emit(bytes.fromhex('48 85 d2')); c.relative(b'\x0f\x84', 'mode_capture_done')
        for source, target in ((0, offset + 24), (4, offset + 28)):
            c.emit(bytes([0x8b, 0x42, source])); c.relative(b'\x89\x05', work + target)
    c.relative(b'\xc7\x05', work + 420, struct.pack('<I', 1))
    c.label('mode_capture_done')


def _pin_modes(c, base, pins):
    c.relative(b'\x48\x8b\x05', base + 0x6f60828)
    for raw, expected in (('0f b6 88 86 e9 30 00', pins['q9ab670']), ('8b 88 30 e9 30 00', pins['route_flag'])):
        c.emit(bytes.fromhex(raw) + b'\x81\xf9' + struct.pack('<I', expected)); c.relative(b'\x0f\x85', 'out')
    for name in ('device', 'pad'):
        if name == 'device':
            c.relative(b'\x48\x8b\x05', base + 0x6f60828); c.emit(bytes.fromhex('48 8b 90 b8 ae 95 00'))
        else: c.relative(b'\x48\x8b\x15', base + 0x5364c40)
        c.emit(b'\x48\xb8' + struct.pack('<Q', pins[name + '_root']) + bytes.fromhex('48 39 c2')); c.relative(b'\x0f\x85', 'out')
        c.emit(bytes.fromhex('81 7a 18') + struct.pack('<I', pins[name + '_index'])); c.relative(b'\x0f\x85', 'out')
        c.emit(bytes.fromhex('48 8b 92') + struct.pack('<I', 0x5f8 + 0x470*pins[name + '_index']))
        c.emit(b'\x48\xb8' + struct.pack('<Q', pins[name + '_descriptor']) + bytes.fromhex('48 39 c2')); c.relative(b'\x0f\x85', 'out')
        for offset, key in ((0, 'type0'), (4, 'type4')):
            c.emit(bytes([0x81, 0x7a, offset]) + struct.pack('<I', pins[name + '_' + key])); c.relative(b'\x0f\x85', 'out')


def _save(c):
    c.emit(bytes.fromhex('9c 50 51 52 41 50 41 51 41 52 41 53 56 57 48 81 ec') + struct.pack('<I', STACK))
    for r in range(6): c.emit(bytes([0xf3, 0x0f, 0x7f, 0x44 + 8*r, 0x24, 0x20 + 16*r]))
    c.emit(bytes.fromhex('0f ae 9c 24 80 00 00 00'))


def _restore(c):
    c.emit(bytes.fromhex('0f ae 94 24 80 00 00 00'))
    for r in range(6): c.emit(bytes([0xf3, 0x0f, 0x6f, 0x44 + 8*r, 0x24, 0x20 + 16*r]))
    c.emit(bytes.fromhex('48 8d a4 24') + struct.pack('<I', STACK) + bytes.fromhex('5f 5e 41 5b 41 5a 41 59 41 58 5a 59 58 9d'))


def _ne(c, prefix, address, suffix=b'', reject='out', condition=b'\x0f\x85'):
    c.relative(prefix, address, suffix); c.relative(condition, reject)


def _scalar(c, register, address):
    c.relative(bytes.fromhex('f3') + (b'\x44' if register >= 8 else b'') + bytes([0x0f, 0x11, 0x05 + 8*(register % 8)]), address)


def _copy(c, source, target, *, width=8):
    c.relative(b'\x48\x8b\x05' if width == 8 else b'\x8b\x05', source)
    c.relative(b'\x48\x89\x05' if width == 8 else b'\x89\x05', target)


def _identity(c, newtonian, frame):
    _ne(c, b'\x48\x3b\x35', newtonian + 8)
    _ne(c, b'\x48\x3b\x2d', newtonian + 40)
    _ne(c, b'\x48\x3b\x35', frame + 8)
    _ne(c, b'\x48\x3b\x2d', frame + 16)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00'))
    _ne(c, b'\x3b\x05', newtonian + 56); _ne(c, b'\x3b\x05', frame + 32)


def _stops(c, own, frame, mailbox, reject='out', label='agent_stop_checked'):
    _ne(c, b'\x48\x83\x3d', own + 8, b'\x00', reject)
    # Mailbox can be farther than rel32 from the code allocation.
    c.emit(b'\x48\xb8' + struct.pack('<Q', mailbox))
    c.emit(bytes.fromhex('48 83 78 08 00')); c.relative(b'\x0f\x85', reject)
    c.relative(b'\xf7\x05', frame + 72, struct.pack('<I', STRICT_ROTATION)); c.relative(b'\x0f\x84', label)
    c.emit(bytes.fromhex('48 83 78 10 00')); c.relative(b'\x0f\x85', reject)
    c.label(label)


def _fresh(c, own, frame, mailbox, pilot, tick, *, reject='out', label='agent_stop_checked', newtonian=None,
           production=False, hard_deadline=None, observed_tick=None):
    _ne(c, b'\x83\x3d', own, b'\x01', reject)
    _stops(c, own, frame, mailbox, reject, label)
    if production:
        # Snapshot before the clock call. A concurrent renewal crossing the
        # system clock quantum must not look like a >50ms future publication.
        c.relative(b'\x4c\x8b\x1d', own + 16)
        c.emit(bytes.fromhex('4c 89 9c 24 d0 00 00 00'))
    c.emit(b'\x48\xb8' + struct.pack('<Q', tick) + b'\xff\xd0')
    c.emit(bytes.fromhex('48 89 84 24 f0 00 00 00'))
    if observed_tick is not None:
        c.relative(b'\x48\x89\x05', observed_tick)
    _ne(c, b'\x83\x3d', own, b'\x01', reject)
    _stops(c, own, frame, mailbox, reject, label + '_after_clock')
    c.emit(bytes.fromhex('48 8b 84 24 f0 00 00 00'))
    addresses = (frame + 40, frame + 64, pilot)
    if not production: addresses += (own + 16,)
    if not production or hard_deadline is not None: addresses += (own + 24,)
    if newtonian is not None: addresses += (newtonian + 72,)
    for address in addresses:
        c.emit(b'\x49\xbb' + struct.pack('<Q', address) + bytes.fromhex('49 3b 03'))
        c.relative(b'\x0f\x83', reject)
    if production:
        c.emit(bytes.fromhex('4c 8b 9c 24 d0 00 00 00 49 39 c3')); c.relative(b'\x0f\x86', reject)
        c.emit(bytes.fromhex('49 29 c3 49 83 fb 32')); c.relative(b'\x0f\x87', reject)
        if hard_deadline is not None:
            c.relative(b'\x4c\x8b\x1d', own + ORIGIN_OFFSET)
            c.emit(bytes.fromhex('4d 85 db')); c.relative(b'\x0f\x84', reject)
            c.emit(bytes.fromhex('49 81 c3') + struct.pack('<I', hard_deadline))
            _ne(c, b'\x4c\x3b\x1d', own + 24, reject=reject)


def _provenance(c, frame, reject='out', *, production=False):
    delivery_prefix = 'delivery_' + str(len(c.data)) + '_'
    _ne(c, b'\x48\x83\x3d', frame, b'\x01', reject)
    _ne(c, b'\x83\x3d', frame + 56, b'\x00', reject)
    _ne(c, b'\x83\x3d', frame + 60, b'\x03', reject)
    for marker in (136, 152):
        c.relative(b'\x48\x8b\x05', frame + 24); _ne(c, b'\x48\x3b\x05', frame + marker, reject=reject)
    for index in range(2):
        bit = struct.pack('<I', 1 << index)
        neutral, done = delivery_prefix + 'neutral_' + str(index), delivery_prefix + 'done_' + str(index)
        # The production mailbox masks only NONZERO commands. A singleton's
        # other axis is native, not an injected-zero delivery. Both getter calls
        # were observed above; each lane now proves its actual delivery path.
        c.relative(b'\x8b\x05', frame + 80 + 4 * index)
        c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0')); c.relative(b'\x0f\x84', neutral)
        for offset in (72, 200):
            c.relative(b'\xf7\x05', frame + offset, bit); c.relative(b'\x0f\x84', reject)
        c.relative(b'\x8b\x05', frame + 80 + 4 * index)
        _ne(c, b'\x3b\x05', frame + 184 + 4 * index, reject=reject)
        c.relative(b'\xe9', done)
        c.label(neutral)
        for offset in (72, 200):
            c.relative(b'\xf7\x05', frame + offset, bit); c.relative(b'\x0f\x85', reject)
        for offset in (176, 184):
            c.relative(b'\x8b\x05', frame + offset + 4 * index)
            c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0')); c.relative(b'\x0f\x85', reject)
        c.label(done)
    for offset, address in ((0x7690, frame + 128), (0x7694, frame + 132)):
        c.emit(b'\x8b\x86' + struct.pack('<I', offset)); _ne(c, b'\x3b\x05', address, reject=reject)
    c.emit(bytes.fromhex('8b 85 80 41 00 00')); _ne(c, b'\x3b\x05', frame + 148, reject=reject)
    for address in (frame + 80, frame + 84):
        c.relative(b'\x8b\x05', address); c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 3f')); c.relative(b'\x0f\x87', reject)
    # Trials retain their historical zero-roll restriction. Production accepts
    # an independent existing F13 roll lane only with its actual delivery proof.
    if production:
        done = delivery_prefix + 'roll_done'
        c.relative(b'\x8b\x05', frame + 88)
        c.emit(bytes.fromhex('25 ff ff ff 7f 3d 00 00 80 3f')); c.relative(b'\x0f\x87', reject)
        c.emit(bytes.fromhex('85 c0')); c.relative(b'\x0f\x84', delivery_prefix + 'roll_native')
        for offset in (72, 200):
            c.relative(b'\xf7\x05', frame + offset, struct.pack('<I', 4)); c.relative(b'\x0f\x84', reject)
        c.relative(b'\x48\x8b\x05', frame + 24); _ne(c, b'\x48\x3b\x05', frame + 168, reject=reject)
        c.relative(b'\x8b\x05', frame + 88); _ne(c, b'\x3b\x05', frame + 164, reject=reject)
        c.relative(b'\xe9', done)
        c.label(delivery_prefix + 'roll_native')
        for offset in (72, 200):
            c.relative(b'\xf7\x05', frame + offset, struct.pack('<I', 4)); c.relative(b'\x0f\x85', reject)
        c.label(done)
    else:
        c.relative(b'\x8b\x05', frame + 88); c.emit(bytes.fromhex('25 ff ff ff 7f 85 c0')); c.relative(b'\x0f\x85', reject)
    c.emit(bytes.fromhex('8b 85 a0 00 00 00 25 ff ff ff 7f 85 c0')); c.relative(b'\x0f\x85', reject)


def _observation(c, own, newtonian, frame, mailbox, pilot, w):
    """Capture evidence before admission; these fields confer no write authority.

    D12's downstream markers are gated on its own lease. Keep their raw values
    even when absent after expiry, independently of T66's strict association.
    """
    c.relative(b'\x48\x89\x2d', w + 488)
    _copy(c, newtonian + 40, w + 496); _copy(c, frame + 16, w + 504)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00')); c.relative(b'\x89\x05', w + 512)
    _copy(c, newtonian + 56, w + 516, width=4); _copy(c, frame + 32, w + 520, width=4)
    for source, target in ((newtonian + 48, 528), (frame + 24, 536),
                           (newtonian + 0x400, 544), (frame + 136, 552), (frame + 152, 560),
                           (frame + 168, 584), (own + 16, 592), (own + 24, 600),
                           (frame + 40, 608), (frame + 64, 616), (newtonian + 72, 624),
                           (own + 8, 640), (own + 40, 656)):
        _copy(c, source, w + target)
    for source, target in ((own, 632), (own + 48, 648)):
        _copy(c, source, w + target, width=4)
    # Pilot/mailbox mappings can lie outside rel32 range of this allocation.
    for source, target in ((pilot, 664), (mailbox + 8, 672), (mailbox + 16, 680)):
        c.emit(b'\x48\xb8' + struct.pack('<Q', source) + bytes.fromhex('48 8b 00'))
        c.relative(b'\x48\x89\x05', w + target)
    _copy(c, frame + 200, w + 336, width=4)
    _copy(c, own + 104, w + 424)
    # _identity ran before capture. Require nonzero current counters and the
    # native latch, but explicitly do not call missing D12 markers complete.
    for source, snapshot in ((newtonian + 48, w), (frame + 24, w + 8)):
        c.relative(b'\x48\x8b\x05', source); _ne(c, b'\x48\x3b\x05', snapshot, reject='observation_done')
        c.emit(bytes.fromhex('48 85 c0')); c.relative(b'\x0f\x84', 'observation_done')
    c.relative(b'\x48\x8b\x05', newtonian + 48)
    _ne(c, b'\x48\x3b\x05', newtonian + 0x400, reject='observation_done')
    c.relative(b'\xc7\x05', w + 568, struct.pack('<I', 1))
    c.label('observation_done')


def _stage(c, w, value):
    c.relative(b'\xc7\x05', w + 572, struct.pack('<I', value))


def target_code(cave, site, own, newtonian, frame, mailbox, pilot, base, tick, *, pitch_limit=None, yaw_limit=None, counter=0,
                mode_pins=None, max_input_amplitude=.25, fixture=None, engine_pins=None,
                pitch_input_sign=None, yaw_input_sign=None, sustained_windows=None, hard_duration_ms=30000,
                production=False, signed_endpoints=None, production_hard_deadline=False, validation_origin=None,
                rotation=None, allow_planetary_flight=False, air_authority=False):
    rotation = _rotation(rotation)
    shaped = rotation != NATIVE_ROTATION or air_authority
    if (shaped or allow_planetary_flight) and not production:
        raise ValueError('Rotation shaping and planetary flight require the production kernel')
    if production and (signed_endpoints is not None or not shaped):
        signed_endpoints = _signed_endpoints(signed_endpoints)
    if production and sustained_windows is not None:
        if not production_hard_deadline:
            raise ValueError('Production validation windows require a fixed hard deadline')
        if type(validation_origin) is not int or not 0 < validation_origin < 2**64 - hard_duration_ms:
            raise ValueError('Production validation windows require an immutable native epoch')
        sustained_windows = freeze_windows(sustained_windows, hard_duration_ms)
    c = Code(cave); _save(c); _identity(c, newtonian, frame)
    if fixture is not None: _pin_fixture(c, newtonian, fixture)
    w = own + WORK
    # Clear every optional field before this invocation. No old torque can leak.
    c.emit(b'\x48\xbf' + struct.pack('<Q', w) + b'\xb9' + struct.pack('<I', WORK_SIZE // 8) + bytes.fromhex('31 c0 fc f3 48 ab'))
    _copy(c, newtonian + 48, w); _copy(c, frame + 24, w + 8)
    _copy(c, newtonian + 8, w + 32); _copy(c, newtonian + 40, w + 40)
    for source, target in ((16, 48), (24, 56), (32, 64)): _copy(c, newtonian + source, w + target)
    c.emit(bytes.fromhex('48 8b 86 b8 66 00 00')); c.relative(b'\x48\x89\x05', w + 72)
    for index, source in enumerate((frame + 84, frame + 80, frame + 88)): _copy(c, source, w + 96 + index*4, width=4)
    for source, offset in ((frame + 180, 432), (frame + 176, 436), (frame + 144, 444),
                           (frame + 188, 448), (frame + 184, 452), (frame + 128, 456),
                           (frame + 132, 460), (frame + 72, 472), (frame + 56, 476), (frame + 60, 480)):
        _copy(c, source, w + offset, width=4)
    c.emit(bytes.fromhex('8b 85 a0 00 00 00')); c.relative(b'\x89\x05', w + 440)
    _copy(c, frame + 104, w + 464)
    # 183791 seam: pitch gain XMM14, yaw gain XMM8 (XMM8 and XMM1 on 180836); observed pitch [RSP+0x60],
    # yaw [RSP+0x64], roll [RBP-0x6C] (pitch [RBP-0x6C], roll [RBP+0x4160] on 180836); roll gain ship+0x66A0.
    for register, offset in ((6, 108), (7, 112), (13, 116), (6, 120), (7, 124), (13, 128), (14, 144), (8, 148)):
        _scalar(c, register, w + offset)
    for raw, target in ((bytes.fromhex('8b 84 24') + struct.pack('<I', SAVED_TOTAL + 0x60), 132),
                        (bytes.fromhex('8b 84 24') + struct.pack('<I', SAVED_TOTAL + 0x64), 136),
                        (bytes.fromhex('8b 85 94 ff ff ff'), 140), (bytes.fromhex('8b 86 a0 66 00 00'), 152)):
        c.emit(raw); c.relative(b'\x89\x05', w + target)
    for offset in range(0, 0x78, 4):
        c.emit(b'\x8b\x86' + struct.pack('<I', 0x6640 + offset)); c.relative(b'\x89\x05', w + 192 + offset)
    c.relative(b'\x48\x8b\x05', base + 0x6f60828)
    c.emit(bytes.fromhex('48 8b 80 30 cf 72 00 48 85 c0')); c.relative(b'\x0f\x84', 'captured_look')
    c.emit(bytes.fromhex('0f b6 80 c9 01 00 00')); c.relative(b'\x89\x05', w + 312)
    c.label('captured_look')
    _capture_modes(c, base, w)
    c.emit(b'\x48\xb8' + struct.pack('<Q', tick) + b'\xff\xd0'); c.relative(b'\x48\x89\x05', w + 16)
    if counter:
        c.emit(bytes.fromhex('48 8d 8c 24 e0 00 00 00') + b'\x48\xb8' + struct.pack('<Q', counter) + b'\xff\xd0')
        c.emit(bytes.fromhex('85 c0')); c.relative(b'\x0f\x84', 'qpc_failed')
        c.emit(bytes.fromhex('48 8b 84 24 e0 00 00 00')); c.relative(b'\x48\x89\x05', w + 328)
        c.label('qpc_failed')
    c.relative(b'\xc7\x05', w + 316, struct.pack('<I', 1))
    _observation(c, own, newtonian, frame, mailbox, pilot, w)
    if production and sustained_windows is not None:
        # Diagnostic fields confer no admission. Older/default kernels leave
        # them zero; every finite receipt identifies its compiled input bound.
        c.relative(b'\xc7\x05', w + 728, struct.pack('<I', 1))
        c.relative(b'\xc7\x05', w + 696, struct.pack('<f', max_input_amplitude))
    _stage(c, w, 1)
    full_gates(c, newtonian, base, tick, allow_planetary_flight=allow_planetary_flight)
    _stage(c, w, 2)
    _fresh(c, own, frame, mailbox, pilot, tick, newtonian=newtonian, production=production,
           hard_deadline=hard_duration_ms if production_hard_deadline else None)
    _stage(c, w, 3)
    _provenance(c, frame, production=production)
    _stage(c, w, 4)
    c.relative(b'\x48\x8b\x05', newtonian + 48); _ne(c, b'\x48\x3b\x05', own + 104)
    _copy(c, own + 104, w + 424)
    # The actual ordinary route must have captured these identities before
    # target formation, and the late snapshot must still match all of them.
    for offset, width in MODE_FIELDS.values():
        _copy_prefix = b'\x48\x8b\x05' if width == 8 else b'\x8b\x05'
        _compare_prefix = b'\x48\x3b\x05' if width == 8 else b'\x3b\x05'
        c.relative(_copy_prefix, own + PREWORK + offset); _ne(c, _compare_prefix, w + offset)
    _ne(c, b'\x83\x3d', own + PREWORK + 420, b'\x01')
    _ne(c, b'\x83\x3d', w + 420, b'\x01')
    c.relative(b'\x83\x3d', w + 380, b'\x10'); c.relative(b'\x0f\x85', 'ordinary_route')
    c.relative(b'\x83\x3d', w + 348, b'\x00'); c.relative(b'\x0f\x84', 'out')
    c.label('ordinary_route')
    _stage(c, w, 5)
    # Require completed latch work from precisely this live invocation.
    c.relative(b'\x48\x8b\x05', newtonian + 48); _ne(c, b'\x48\x3b\x05', newtonian + 0x400)
    c.emit(bytes.fromhex('48 85 c0')); c.relative(b'\x0f\x84', 'out')
    # The D12 and Newtonian latches were installed independently. Their counter
    # values need not match; associate their current snapshots by the actual
    # native stack/thread identities and each domain's own same-call markers.
    _identity(c, newtonian, frame)
    for source, snapshot in ((newtonian + 48, w), (frame + 24, w + 8)):
        c.relative(b'\x48\x8b\x05', source); _ne(c, b'\x48\x3b\x05', snapshot)
        c.emit(bytes.fromhex('48 85 c0')); c.relative(b'\x0f\x84', 'out')
    for source, last in ((newtonian + 48, own + 120), (frame + 24, own + 128)):
        c.relative(b'\x48\x8b\x05', source)
        c.relative(b'\x48\x3b\x05', last); c.relative(b'\x0f\x86', 'out')
    c.relative(b'\x48\x89\x2d', w + 488)
    _copy(c, newtonian + 40, w + 496); _copy(c, frame + 16, w + 504)
    c.emit(bytes.fromhex('65 8b 04 25 48 00 00 00')); c.relative(b'\x89\x05', w + 512)
    _copy(c, newtonian + 56, w + 516, width=4); _copy(c, frame + 32, w + 520, width=4)
    for source, target in ((newtonian + 48, 528), (frame + 24, 536),
                           (newtonian + 0x400, 544), (frame + 136, 552), (frame + 152, 560)):
        _copy(c, source, w + target)
    _copy(c, newtonian + 48, own + 120); _copy(c, frame + 24, own + 128)
    c.relative(b'\xc7\x05', w + 484, struct.pack('<I', 1))
    _stage(c, w, 6)
    for offset in (80, 88, 92): c.relative(b'\xc7\x05', w + offset, struct.pack('<I', 1))
    _copy(c, frame + 200, w + 336, width=4)
    if production:
        # Immutable compiled endpoints eliminate renewal's odd/even payload
        # window. Only aligned enabled/lease stores change during production.
        _ne(c, b'\x83\x3d', own + 48, b'\x01')
        _copy(c, own + 40, w + 24)
    else:
        # Research mutable signed targets keep their complete seqlock guards.
        c.relative(b'\x48\x8b\x05', own + 32); c.emit(bytes.fromhex('a8 01')); c.relative(b'\x0f\x85', 'out')
        c.emit(bytes.fromhex('48 85 c0')); c.relative(b'\x0f\x84', 'out')
        c.emit(bytes.fromhex('48 89 84 24 88 00 00 00'))
        for offset in range(0, 40, 8):
            c.relative(b'\x48\x8b\x05', own + 40 + offset)
            c.emit(bytes.fromhex('48 89 84 24') + struct.pack('<I', 0xa0 + offset))
        c.emit(bytes.fromhex('83 bc 24 a8 00 00 00 01')); c.relative(b'\x0f\x85', 'out')
        c.emit(bytes.fromhex('48 83 bc 24 a0 00 00 00 00')); c.relative(b'\x0f\x84', 'out')
        c.emit(bytes.fromhex('83 bc 24 b4 00 00 00 00')); c.relative(b'\x0f\x85', 'out')
    _stage(c, w, 7)
    if (production or (pitch_limit is not None and yaw_limit is not None and mode_pins is not None and engine_pins is not None
            and pitch_input_sign in (-1, 1) and yaw_input_sign in (-1, 1))):
        if production and (mode_pins is None or engine_pins is None or fixture is None):
            raise ValueError('Production requires immutable fixture, route and engine pins')
        if not production or sustained_windows is not None:
            for source, diagnostic in ((frame + 80, w + 692), (frame + 84, w + 688)):
                if production:
                    c.relative(b'\xc7\x05', diagnostic, struct.pack('<I', 2))
                c.relative(b'\x8b\x05', source); c.emit(bytes.fromhex('25 ff ff ff 7f 3d') + struct.pack('<f', max_input_amplitude)); c.relative(b'\x0f\x87', 'out')
                if production:
                    c.relative(b'\xc7\x05', diagnostic, struct.pack('<I', 1))
        if not production:
            for name, source, sign in (('pitch', frame + 84, pitch_input_sign), ('yaw', frame + 80, yaw_input_sign)):
                c.relative(b'\x8b\x05', source); c.emit(bytes.fromhex('a9 ff ff ff 7f')); c.relative(b'\x0f\x84', name + '_sign_ok')
                c.emit(bytes.fromhex('25 00 00 00 80 3d') + struct.pack('<I', 0x80000000 if sign < 0 else 0)); c.relative(b'\x0f\x85', 'out')
                c.label(name + '_sign_ok')
            for offset, limit in ((0xac, pitch_limit), (0xb0, yaw_limit)):
                c.emit(bytes.fromhex('8b 84 24') + struct.pack('<I', offset) + bytes.fromhex('25 ff ff ff 7f 3d') + struct.pack('<f', limit))
                c.relative(b'\x0f\x87', 'out')
        _stage(c, w, 8)
        _fresh(c, own, frame, mailbox, pilot, tick, label='agent_commit_checked', newtonian=newtonian,
               production=production, hard_deadline=hard_duration_ms if production_hard_deadline else None,
               observed_tick=w + 704 if production and sustained_windows is not None else None)
        _stage(c, w, 9)
        if sustained_windows is not None:
            emit_window_gate(c, own, sustained_windows, hard_duration_ms,
                             observed_result=w + 700 if production else None,
                             expected_origin=validation_origin if production else None)
        if not production:
            c.relative(b'\x48\x3b\x05', own + 112); c.relative(b'\x0f\x83', 'out')
            c.emit(bytes.fromhex('48 89 84 24 90 00 00 00'))
            c.emit(bytes.fromhex('48 3b 84 24 b8 00 00 00')); c.relative(b'\x0f\x82', 'out')
            c.emit(bytes.fromhex('48 2b 84 24 b8 00 00 00 48 83 f8 32')); c.relative(b'\x0f\x87', 'out')
            c.emit(bytes.fromhex('48 8b 84 24 c0 00 00 00'))
            c.emit(bytes.fromhex('48 3b 84 24 90 00 00 00')); c.relative(b'\x0f\x86', 'out')
            c.emit(bytes.fromhex('48 2b 84 24 90 00 00 00'))
            c.emit(bytes.fromhex('48 83 f8 32')); c.relative(b'\x0f\x87', 'out')
            c.emit(bytes.fromhex('48 8b 84 24 88 00 00 00')); _ne(c, b'\x48\x3b\x05', own + 32)
        else:
            _ne(c, b'\x83\x3d', own + 48, b'\x01')
        _stage(c, w, 10)
        _pin_modes(c, base, mode_pins)
        if fixture is not None: _pin_fixture(c, newtonian, fixture)
        _pin_engine(c, engine_pins, newtonian if allow_planetary_flight else None)
        if air_authority:
            # One read each: the validated copies are the values applied below.
            for source, (low, high), target in ((AIR_SCALE, AIR_RANGE, 0), (AIR_DAMPING, DAMPING_RANGE, 4)):
                c.relative(b'\x8b\x05', own + source)
                c.emit(b'\x3d' + struct.pack('<f', high)); c.relative(b'\x0f\x87', 'out')
                c.emit(b'\x3d' + struct.pack('<f', low)); c.relative(b'\x0f\x82', 'out')
                c.relative(b'\x89\x05', w + AIR_RECEIPT + target)
            for index in range(2):
                c.relative(b'\x8b\x05', own + AIR_SWAY + 4 * index)
                c.emit(bytes.fromhex('89 c1 81 e1 ff ff ff 7f 81 f9') + struct.pack('<f', SWAY_LIMIT))
                c.relative(b'\x0f\x87', 'out')  # |sway| above the limit, or NaN
                c.relative(b'\x89\x05', w + AIR_RECEIPT + 8 + 4 * index)
        _stage(c, w, 11)
        full_gates(c, newtonian, base, tick, prefix='commit_', check_clock=False,
                   allow_planetary_flight=allow_planetary_flight)
        _stage(c, w, 12)
        _provenance(c, frame, production=production)
        _stops(c, own, frame, mailbox, label='agent_final_checked')
        if production: _ne(c, b'\x83\x3d', own + 48, b'\x01')
        else:
            c.emit(bytes.fromhex('48 8b 84 24 88 00 00 00')); _ne(c, b'\x48\x3b\x05', own + 32)
        _stage(c, w, 13)
        c.relative(b'\x48\x8b\x05', newtonian + 48); c.relative(b'\x48\x3b\x05', own + 96); c.relative(b'\x0f\x86', 'out')
        c.relative(b'\x48\x89\x05', own + 96)
        # Register MOVSS keeps upper target lanes intact.
        for axis, source, scale, register, receipt in (('pitch', frame + 84, 0xac, 6, 120), ('yaw', frame + 80, 0xb0, 7, 124)):
            if production and signed_endpoints is None:
                pass  # rotation shaping only: keep the native target
            elif production:
                c.relative(b'\x8b\x05', source)
                c.emit(bytes.fromhex('85 c0')); c.relative(b'\x0f\x88', axis + '_negative_endpoint')
                c.emit(b'\xb9' + struct.pack('<f', signed_endpoints[axis + '_positive']))
                c.relative(b'\xe9', axis + '_endpoint_ready')
                c.label(axis + '_negative_endpoint')
                c.emit(b'\xb9' + struct.pack('<f', signed_endpoints[axis + '_negative']))
                c.label(axis + '_endpoint_ready')
                c.emit(bytes.fromhex('25 ff ff ff 7f 66 0f 6e c0 66 0f 6e c9 f3 0f 59 c1'))
            else:
                c.relative(b'\xf3\x0f\x10\x05', source)
                c.emit(bytes.fromhex('f3 0f 59 84 24') + struct.pack('<I', scale))
            if not production or signed_endpoints is not None:
                c.emit(bytes.fromhex('f3 0f 10') + bytes([0xc0 + 8 * register]))
            if shaped: _shape_rotation(c, axis, rotation[axis], rotation['response'],
                                       w + AIR_RECEIPT if air_authority else None)
            _scalar(c, register, w + receipt)
        if shaped: _shape_rotation(c, 'roll', rotation['roll'], rotation['response'])
        c.relative(b'\xc7\x05', w + 84, struct.pack('<I', 1))
        _stage(c, w, 14)
        if not production:
            c.emit(bytes.fromhex('48 8b 84 24 a0 00 00 00')); c.relative(b'\x48\x89\x05', w + 24)
    c.label('out'); _restore(c)
    c.relative(bytes.fromhex('f3 44 0f 10 0d'), base + GLOBAL_RVA)
    c.emit(bytes.fromhex('48 8d 9e d0 6c 00 00')); c.relative(b'\xe9', site + len(ORIGINAL))
    raw = c.finish()
    if len(raw) > 0x4000: raise ValueError('T66 target cave exceeds reserved pages')
    return raw


def ordinary_code(cave, site, own, newtonian, frame, base, *, fixture=None):
    """Record the actual ordinary branch before its route predicates run."""
    c = Code(cave); _save(c); _identity(c, newtonian, frame)
    if fixture is not None: _pin_fixture(c, newtonian, fixture)
    _ne(c, b'\x83\x3d', own, b'\x01')
    _ne(c, b'\x48\x83\x3d', own + 8, b'\x00')
    c.relative(b'\x48\x8b\x05', base + 0x6f60828); _ne(c, b'\x48\x3b\x05', newtonian + 80)
    c.relative(b'\xc7\x05', own + PREWORK + 420, bytes(4))
    _capture_modes(c, base, own + PREWORK)
    _copy(c, newtonian + 48, own + 104)
    c.label('out'); _restore(c)
    c.relative(bytes.fromhex('48 8b 0d'), base + 0x6ef2d88)
    c.relative(b'\xe9', site + len(ORDINARY_ORIGINAL))
    return c.finish()


def torque_code(cave, site, own, newtonian, frame, mailbox, pilot, tick, *, fixture=None, production=False, hard_deadline=None,
                validation_windows=None, validation_origin=None):
    if validation_windows is not None:
        if not production or hard_deadline is None:
            raise ValueError('Finite production windows require a fixed hard deadline')
        if type(validation_origin) is not int or not 0 < validation_origin < 2**64 - hard_deadline:
            raise ValueError('Finite production windows require an immutable native epoch')
        validation_windows = freeze_windows(validation_windows, hard_deadline)
    c = Code(cave)
    # Replay exactly the native clamp before any observation work.
    c.emit(TORQUE_ORIGINAL); _save(c); _identity(c, newtonian, frame)
    if fixture is not None: _pin_fixture(c, newtonian, fixture)
    w = own + WORK
    c.relative(b'\x48\x8b\x05', newtonian + 48); _ne(c, b'\x48\x3b\x05', w)
    c.relative(b'\x48\x8b\x05', frame + 24); _ne(c, b'\x48\x3b\x05', w + 8)
    _ne(c, b'\x83\x3d', w + 316, b'\x01')
    # Original target's selected-control pointer must equal actual clamp's RAX.
    # Saved RAX lies STACK+64 after ten pushes.
    c.emit(bytes.fromhex('48 8b 84 24') + struct.pack('<I', STACK + 64)); _ne(c, b'\x48\x3b\x05', w + 72)
    c.emit(bytes.fromhex('8b 80 f0 01 00 00')); c.relative(b'\x89\x05', w + 156)
    # Original native XMM0(before clamp) was saved at+20; XMM8 is native after clamp.
    c.emit(bytes.fromhex('0f 10 44 24 20')); c.relative(b'\x0f\x11\x05', w + 160)
    c.relative(bytes.fromhex('44 0f 11 05'), w + 176)
    _fresh(c, own, frame, mailbox, pilot, tick, reject='stale', newtonian=newtonian, production=production, hard_deadline=hard_deadline,
           observed_tick=w + 712 if validation_windows is not None else None)
    if validation_windows is not None:
        emit_window_gate(c, own, validation_windows, hard_deadline, reject='stale', observed_result=w + 720,
                         expected_origin=validation_origin)
    c.relative(b'\xe9', 'seal')
    c.label('stale'); c.relative(b'\xc7\x05', w + 88, bytes(4))
    c.label('seal'); c.relative(b'\xc7\x05', w + 316, struct.pack('<I', 2))
    # Each immutable completed receipt has independent matching slot sentinels.
    c.relative(b'\x48\xff\x05', own + 80); c.relative(b'\x48\x8b\x05', own + 80)
    c.emit(bytes.fromhex('48 89 c2 48 25') + struct.pack('<I', CAPACITY - 1) + bytes.fromhex('48 c1 e0 0a'))
    c.emit(b'\x48\xbf' + struct.pack('<Q', own + RING) + bytes.fromhex('48 01 c7 48 89 d0 48 d1 e0 48 ff c8 48 89 07'))
    c.emit(bytes.fromhex('48 89 87 f8 03 00 00 48 83 c7 10'))
    c.emit(b'\x48\xbe' + struct.pack('<Q', w) + b'\xb9' + struct.pack('<I', WORK_SIZE // 8) + bytes.fromhex('fc f3 48 a5'))
    c.emit(bytes.fromhex('48 ff c0 48 89 87') + struct.pack('<i', SLOT - 8 - (16 + WORK_SIZE)))
    c.emit(bytes.fromhex('48 89 87') + struct.pack('<i', -(16 + WORK_SIZE)))
    c.relative(b'\x48\x89\x15', own + 88)
    c.label('out'); _restore(c); c.relative(b'\xe9', site + len(TORQUE_ORIGINAL))
    return c.finish()


def decode_record(raw):
    if len(raw) != SLOT: raise ValueError('Wrong T66 record size')
    start, end = struct.unpack_from('<Q', raw)[0], struct.unpack_from('<Q', raw, SLOT - 8)[0]
    if not start or start & 1 or start != end: return None
    w = memoryview(raw)[16:16 + WORK_SIZE]
    names = ('invocation', 'd12_invocation', 'tick_ms', 'command_generation', 'ship', 'frame', 'body', 'state', 'context', 'selected_control')
    out = dict(zip(names, struct.unpack_from('<10Q', w)))
    out.update(zip(('admitted', 'override_applied', 'fresh', 'qualified'), struct.unpack_from('<4I', w, 80)))
    for prefix, offset in (('input', 96), ('native_target', 108), ('target', 120), ('rate', 132), ('gain', 144)):
        out.update(zip((prefix + '_' + axis for axis in AXES), struct.unpack_from('<3f', w, offset)))
    out['max_torque'] = struct.unpack_from('<f', w, 156)[0]
    for side, offset in (('before', 160), ('after', 176)):
        out.update(zip(('torque_' + side + '_' + axis for axis in ('x', 'y', 'z')), struct.unpack_from('<3f', w, offset)))
    out['engine_snapshot_hex'] = bytes(w[192:312]).hex()
    out['freelook'], out['capture_stage'] = struct.unpack_from('<2I', w, 312)
    out['complete'] = int(out['capture_stage'] == 2)
    out['counter'] = struct.unpack_from('<Q', w, 328)[0]
    out['qpc'] = out['counter']
    out['d12_applied_mask'] = struct.unpack_from('<I', w, 336)[0]
    out.update({name: struct.unpack_from('<Q' if width == 8 else '<I', w, offset)[0] for name, (offset, width) in MODE_FIELDS.items()})
    out['mode_capture_complete'] = struct.unpack_from('<I', w, 420)[0]
    out['ordinary_invocation'] = struct.unpack_from('<Q', w, 424)[0]
    out['mode_pins'] = {name: out[name] for name in MODE_FIELDS} if out['mode_capture_complete'] == 1 else None
    for name, offset in (('native_input_pitch', 432), ('native_input_yaw', 436), ('native_input_roll', 440),
                         ('native_input_throttle', 444), ('scalar_pitch', 448), ('scalar_yaw', 452),
                         ('processed_yaw', 456), ('processed_pitch', 460)):
        out[name] = struct.unpack_from('<f', w, offset)[0]
    out['d12_generation'] = struct.unpack_from('<Q', w, 464)[0]
    out['d12_mask'], out['d12_conflict'], out['d12_provenance_bits'] = struct.unpack_from('<3I', w, 472)
    out['association_complete'] = struct.unpack_from('<I', w, 484)[0]
    out.update(zip(('native_stack', 'newtonian_stack', 'd12_stack'), struct.unpack_from('<3Q', w, 488)))
    out.update(zip(('native_thread', 'newtonian_thread', 'd12_thread'), struct.unpack_from('<3I', w, 512)))
    out.update(zip(('associated_newtonian_invocation', 'associated_d12_invocation',
                    'newtonian_latch_invocation', 'd12_processed_invocation', 'd12_throttle_invocation'),
                   struct.unpack_from('<5Q', w, 528)))
    out['record_sequence'] = start // 2
    out['applied'] = out['override_applied']
    out['observation_identity_complete'], out['last_native_stage'] = struct.unpack_from('<2I', w, 568)
    out['last_native_stage_name'] = NATIVE_STAGES.get(out['last_native_stage'], 'unknown')
    out['d12_roll_invocation'] = struct.unpack_from('<Q', w, 584)[0]
    out.update(zip(('own_lease_until_ms', 'trial_hard_until_ms', 'd12_source_until_ms', 'd12_lease_until_ms', 'fixture_until_ms'),
                   struct.unpack_from('<5Q', w, 592)))
    out['own_master_enabled'] = struct.unpack_from('<I', w, 632)[0]
    out['own_stop'] = struct.unpack_from('<Q', w, 640)[0]
    out['command_enabled'] = struct.unpack_from('<I', w, 648)[0]
    out['raw_command_generation'] = struct.unpack_from('<Q', w, 656)[0]
    out['pilot_until_ms'], out['mailbox_stop'], out['mailbox_agent_stop'] = struct.unpack_from('<3Q', w, 664)
    out['validation_pitch_bound_state'], out['validation_yaw_bound_state'] = struct.unpack_from('<2I', w, 688)
    out['validation_input_bound'] = struct.unpack_from('<f', w, 696)[0]
    out['validation_window_state'] = struct.unpack_from('<I', w, 700)[0]
    out['validation_commit_tick_ms'], out['validation_torque_tick_ms'] = struct.unpack_from('<2Q', w, 704)
    out['validation_torque_window_state'] = struct.unpack_from('<I', w, 720)[0]
    out['validation_observation_present'] = struct.unpack_from('<I', w, 728)[0]
    # All 0.0 unless an air cave validated them this frame.
    out['air_authority'], out['air_damping'], out['air_sway_pitch'], out['air_sway_yaw'] = struct.unpack_from('<4f', w, AIR_RECEIPT)
    # These are observations, not the strict qualification/admission flags.
    out['observed_newtonian_latch_current'] = int(bool(out['invocation']) and out['newtonian_latch_invocation'] == out['invocation'])
    out['observed_d12_processed_current'] = int(bool(out['d12_invocation']) and out['d12_processed_invocation'] == out['d12_invocation'])
    out['observed_d12_throttle_current'] = int(bool(out['d12_invocation']) and out['d12_throttle_invocation'] == out['d12_invocation'])
    return out


ROTATION_RANGE = (.5, 2.)
ROTATION_KEYS = ('pitch', 'yaw', 'roll', 'response')
NATIVE_ROTATION = {key: 1. for key in ROTATION_KEYS}
# Native observed rates at the seam (build 183791): pitch [RSP+0x60], yaw [RSP+0x64] (both + the cave's
# saved frame), roll [RBP-0x6C]; written at 0x17911E4/0x1791184/0x1791222 as the body angular velocity on
# matrix rows 0/1/2 (180836: pitch [RBP-0x6C], roll [RBP+0x4160]). ModRM bytes encode XMM0 as the register.
OBSERVED_RATE = {'pitch': bytes.fromhex('84 24') + struct.pack('<I', SAVED_TOTAL + 0x60),
                 'yaw': bytes.fromhex('84 24') + struct.pack('<I', SAVED_TOTAL + 0x64),
                 'roll': bytes.fromhex('45 94')}
TARGET_REGISTER = {'pitch': 6, 'yaw': 7, 'roll': 13}


def _rotation(value):
    """Per-class rotation shaping: top-rate multipliers per axis plus one response (gain) multiplier."""
    if value is None: return dict(NATIVE_ROTATION)
    if not isinstance(value, Mapping) or set(value) != set(ROTATION_KEYS):
        raise ValueError('Rotation needs pitch, yaw, roll and response')
    result = {}
    for key in ROTATION_KEYS:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)): raise ValueError('Rotation values must be numbers')
        number = struct.unpack('<f', struct.pack('<f', number))[0]
        if not math.isfinite(number) or not ROTATION_RANGE[0] <= number <= ROTATION_RANGE[1]:
            raise ValueError('Rotation values must be 0.5..2.0')
        result[key] = number
    return result


def _shape_rotation(c, axis, rate, response, air=None):
    """target' = response*(rate*air*target + observed) - observed, low lane only.

    Native forms error = -target - observed at 0x1791915..0x179192B (183791) and multiplies it by the
    axis gain (pitch/yaw: TurnStrength x maneuverability; roll: RollForce). Native steady
    rate equals the target (T66 live: 0.05%), so `rate` scales the top turn rate and
    `response` scales the gain, i.e. how quickly that rate is reached. The MaxTorque box
    clamp, boost factor and integration after the seam stay native. XMM0/XMM1 are
    restored by _restore. `air` (T92) is the address of this frame's validated air receipt (scale,
    damping, pitch sway, yaw sway). For pitch and yaw, never roll:
    target' = response*(damping*(rate*scale*target + observed) - sway) - observed, so the error
    becomes response*(damping*(-rate*scale*target - observed) + sway). With the stick centred the
    controller drives the observed rate toward sway/damping, and damping < 1 softens how firmly it
    holds the nose. The MaxTorque box clamp downstream still bounds the whole torque.
    """
    register, observed = TARGET_REGISTER[axis], OBSERVED_RATE[axis]
    rex = bytes.fromhex('41') if register >= 8 else b''  # REX.B: source XMM8+
    c.emit(bytes.fromhex('f3') + rex + bytes.fromhex('0f 10') + bytes([0xc0 + register % 8]))  # movss xmm0, xmmR
    if rate != 1.:
        c.emit(bytes.fromhex('b8') + struct.pack('<f', rate) + bytes.fromhex('66 0f 6e c8 f3 0f 59 c1'))
    if air is not None and axis != 'roll':
        c.relative(bytes.fromhex('f3 0f 59 05'), air)  # mulss xmm0, [scale]
        c.emit(bytes.fromhex('f3 0f 58') + observed)
        c.relative(bytes.fromhex('f3 0f 59 05'), air + 4)  # mulss xmm0, [damping]
        c.relative(bytes.fromhex('f3 0f 5c 05'), air + (8 if axis == 'pitch' else 12))  # subss xmm0, [sway]
        if response != 1.:
            c.emit(bytes.fromhex('b8') + struct.pack('<f', response) + bytes.fromhex('66 0f 6e c8 f3 0f 59 c1'))
        c.emit(bytes.fromhex('f3 0f 5c') + observed)
    elif response != 1.:
        c.emit(bytes.fromhex('f3 0f 58') + observed)
        c.emit(bytes.fromhex('b8') + struct.pack('<f', response) + bytes.fromhex('66 0f 6e c8 f3 0f 59 c1'))
        c.emit(bytes.fromhex('f3 0f 5c') + observed)
    rex = bytes.fromhex('44') if register >= 8 else b''  # REX.R: destination XMM8+
    c.emit(bytes.fromhex('f3') + rex + bytes.fromhex('0f 10') + bytes([0xc0 + 8 * (register % 8)]))  # movss xmmR, xmm0


def _positive_limit(value):
    if value is None: return None
    raw = struct.pack('<f', value)
    value = struct.unpack('<f', raw)[0]
    if not math.isfinite(value) or value <= 0: raise ValueError('Finite positive measured native rate limit required')
    return value


def _signed_endpoints(value):
    keys = {'pitch_negative', 'pitch_positive', 'yaw_negative', 'yaw_positive'}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError('Four separately measured signed endpoints required')
    result = {}
    for key in keys:
        try: target = struct.unpack('<f', struct.pack('<f', value[key]))[0]
        except (TypeError, OverflowError, struct.error) as error:
            raise ValueError('Finite nonzero measured signed endpoints required') from error
        if not math.isfinite(target) or target == 0:
            raise ValueError('Finite nonzero measured signed endpoints required')
        result[key] = target
    for axis in ('pitch', 'yaw'):
        if (result[axis + '_negative'] < 0) == (result[axis + '_positive'] < 0):
            raise ValueError('Each input direction requires its measured opposite native target polarity')
    return result


class RotationBridge:
    """Caller-owned addon. Caller must independently qualify exact executable.

    Fixture/newtonian/frame pointers must be from the current release instance.
    The writer must perform the release's normal serialized native-code writes.
    No geometry refresh/rearm: fixture migration requires stop, restore and a new
    addon instance. This addon must restore before its prerequisite bridges.
    """
    _production = False
    def __init__(self, pm, pid, base, newtonian_data, frame, mailbox, pilot_deadline, tick, fixture,
                 *, pitch_limit=None, yaw_limit=None, counter=0, hard_duration_ms=30000, max_input_amplitude=.25, mode_pins=None,
                 engine_pins=None, pitch_input_sign=None, yaw_input_sign=None, sustained_windows=None):
        self.pm, self.pid, self.base = pm, pid, base
        self.newtonian_data, self.frame, self.mailbox = newtonian_data, frame, mailbox
        self.pilot_deadline, self.tick, self.counter = pilot_deadline, tick, counter
        self.fixture = dict(fixture)
        self.engine_pins = None if engine_pins is None else dict(engine_pins)
        if self.engine_pins is not None:
            if set(self.engine_pins) != {'engine_snapshot_hex', 'selected_control', 'max_torque_bits'}: raise ValueError('Complete engine pins required')
            if len(bytes.fromhex(self.engine_pins['engine_snapshot_hex'])) != 0x78: raise ValueError('Wrong effective engine snapshot length')
            if not self.engine_pins['selected_control']: raise ValueError('Positive selected-control identity required')
        self.pitch_limit, self.yaw_limit = _positive_limit(pitch_limit), _positive_limit(yaw_limit)
        self.pitch_input_sign, self.yaw_input_sign = pitch_input_sign, yaw_input_sign
        if any(value not in (None, -1, 1) for value in (pitch_input_sign, yaw_input_sign)): raise ValueError('Measured input signs must be-1 or+1')
        self.mode_pins = None if mode_pins is None else dict(mode_pins)
        if self.mode_pins is not None:
            if set(self.mode_pins) != set(MODE_FIELDS): raise ValueError('Complete exact native mode pins required')
            for name in ('device', 'pad'):
                if not 0 <= self.mode_pins[name + '_index'] <= 15: raise ValueError('Native index outside trial bound')
                if not self.mode_pins[name + '_root'] or not self.mode_pins[name + '_descriptor']: raise ValueError('Positive mode identities required')
            if self.mode_pins['device_type4'] == 0x10 and self.mode_pins['route_flag'] == 0: raise ValueError('Special circular route unqualified')
        if not math.isfinite(max_input_amplitude) or not 0 < max_input_amplitude <= .25: raise ValueError('Trial amplitude must be positive and <=0.25')
        self.max_input_amplitude = max_input_amplitude
        if (self.pitch_limit is None) != (self.yaw_limit is None): raise ValueError('Supply both measured rate limits or neither')
        if not isinstance(hard_duration_ms, int) or not 1 <= hard_duration_ms <= 30000: raise ValueError('Hard duration must be1..30000ms')
        for address in (base, newtonian_data, frame, mailbox, pilot_deadline, tick):
            if not isinstance(address, int) or not 0 < address < 2**64: raise ValueError('Positive pinned uint64 addresses required')
        if any(address & 7 for address in (newtonian_data, frame, mailbox, pilot_deadline)): raise ValueError('Unaligned pinned storage')
        self.hard_duration_ms = hard_duration_ms
        self.sustained_windows = None if sustained_windows is None else freeze_windows(sustained_windows, hard_duration_ms)
        self.native_origin = None
        self._prepared_validation = None
        self.data = 0; self.items = {}; self.installed = False; self.terminal = False; self.prepared = False
        self._sequence = 0; self._generation = 0; self._lock = threading.Lock()
        self._configuration = (False, 0., 0.)

    @staticmethod
    def _now():
        K.GetTickCount64.restype = C.c_uint64
        return int(K.GetTickCount64())

    def _write(self, offset, raw): self.pm.write_bytes(self.data + offset, raw, len(raw))

    def prepare(self):
        if self.data: raise RuntimeError('Already prepared')
        for name, (rva, original) in SITES.items():
            if self.pm.read_bytes(self.base + rva, len(original)) != original: raise RuntimeError('T66 original site mismatch: ' + name)
        if self.fixture.get('ship_class') not in (1, 2, 3, 4, 6, 7, 8, 9): raise ValueError('Unsupported class')
        for offset, key in ((8, 'ship'), (16, 'body'), (24, 'state'), (32, 'context')):
            if struct.unpack('<Q', self.pm.read_bytes(self.newtonian_data + offset, 8))[0] != self.fixture[key]: raise RuntimeError('Newtonian fixture mismatch')
        self.data = _allocate_data(self.pm.process_handle, self.base + SITE_RVA, ALLOCATION)
        if self._production and self.sustained_windows is not None:
            # One final compile while sites are still original. Compile/install
            # latency spends this immutable budget; no cave is rewritten live.
            self.native_origin = self._now()
            self._prepared_validation = (self.native_origin, self.hard_duration_ms,
                                         self.sustained_windows, self.max_input_amplitude)
        codes = {}
        for index, (name, (rva, original)) in enumerate(SITES.items()):
            site = self.base + rva; cave = self.data + RING + SLOT * CAPACITY + index * 0x4000
            self.items[name] = {'address': site, 'original': original.hex(), 'cave': cave,
                                'patch': (b'\xe9' + struct.pack('<i', cave - site - 5) + b'\x90' * (len(original) - 5)).hex()}
            codes[name] = (target_code(cave, site, self.data, self.newtonian_data, self.frame, self.mailbox,
                                      self.pilot_deadline, self.base, self.tick, pitch_limit=self.pitch_limit,
                                      yaw_limit=self.yaw_limit, counter=self.counter, mode_pins=self.mode_pins,
                                      max_input_amplitude=self.max_input_amplitude, fixture=self.fixture,
                                      engine_pins=self.engine_pins, pitch_input_sign=self.pitch_input_sign,
                                      yaw_input_sign=self.yaw_input_sign, sustained_windows=self.sustained_windows,
                                      hard_duration_ms=self.hard_duration_ms, production=self._production,
                                      signed_endpoints=getattr(self, 'signed_endpoints', None),
                                      rotation=getattr(self, 'rotation', None),
                                      allow_planetary_flight=getattr(self, 'allow_planetary_flight', False),
                                      air_authority=getattr(self, 'air_authority', False),
                                      production_hard_deadline=self._production and self.hard_duration_ms is not None,
                                      validation_origin=self.native_origin if self._prepared_validation is not None else None) if name == 'target' else
                           ordinary_code(cave, site, self.data, self.newtonian_data, self.frame, self.base, fixture=self.fixture) if name == 'ordinary' else
                           torque_code(cave, site, self.data, self.newtonian_data, self.frame, self.mailbox, self.pilot_deadline, self.tick, fixture=self.fixture,
                                       production=self._production, hard_deadline=self.hard_duration_ms if self._production else None,
                                       validation_windows=self.sustained_windows if self._production else None,
                                       validation_origin=self.native_origin if self._prepared_validation is not None else None))
        self._write(0, bytes(RING))
        for name, code in codes.items():
            address = self.items[name]['cave']; self.pm.write_bytes(address, code, len(code))
            if self.pm.read_bytes(address, len(code)) != code: raise RuntimeError('T66 cave readback failed')
            if not K.FlushInstructionCache(self.pm.process_handle, address, len(code)): raise OSError('FlushInstructionCache')
        self.prepared = True
        metadata = {'data': self.data, 'sites': self.items, 'allocation_size': ALLOCATION, 'retention': 'until process exit',
                'mode': ('observe-only' if not self._production else 'production-shaping' if self.signed_endpoints is None
                         else 'production-additive'), 'exe_sha256': EXPECTED_SHA,
                'hard_duration_ms': self.hard_duration_ms,
                'signed_endpoints': dict(self.signed_endpoints) if self._production and self.signed_endpoints is not None else None,
                'rotation': dict(getattr(self, 'rotation', NATIVE_ROTATION)),
                'allow_planetary_flight': getattr(self, 'allow_planetary_flight', False),
                'air_authority': getattr(self, 'air_authority', False),
                'sustained_authority': None if self.sustained_windows is None else sustained_manifest(
                    self.sustained_windows, self.hard_duration_ms,
                    max_input_amplitude=self.max_input_amplitude if self._production else .25,
                    envelope=PRODUCTION_ENVELOPE if self._production else ENVELOPE)}
        if self._production and self.sustained_windows is not None:
            metadata['production_validation_authority'] = dict(metadata['sustained_authority'])
            metadata['native_origin_tick'] = self.native_origin
        return metadata

    def install(self, writer=replace_code):
        if not self.data or not self.prepared or self.installed or self.terminal: raise RuntimeError('Fresh prepared addon required')
        frozen = self._prepared_validation
        if frozen is not None and (self.native_origin, self.hard_duration_ms, self.sustained_windows,
                                   self.max_input_amplitude) != frozen:
            raise RuntimeError('Prepared finite production authority changed before installation')
        try:
            # Observer first, target second; gates remain inert during installation.
            for name in ('torque', 'ordinary', 'target'):
                item = self.items[name]; writer(self.pm, self.pid, item['address'], bytes.fromhex(item['original']), bytes.fromhex(item['patch']))
            self.native_origin = self._now() if frozen is None else frozen[0]
            if self.sustained_windows is not None or (self._production and self.hard_duration_ms is not None):
                self._write(ORIGIN_OFFSET, struct.pack('<Q', self.native_origin))
            if self.hard_duration_ms is not None: self._write(24, struct.pack('<Q', self.native_origin + self.hard_duration_ms))
            if getattr(self, 'air_authority', False): self._write(AIR_SCALE, struct.pack('<4f', 1., 1., 0., 0.))
            self._write(0, struct.pack('<I', 1)); self.installed = True
        except BaseException:
            self.restore(writer); raise

    def publish(self, *, enabled=False, pitch_full=None, yaw_full=None, now_ms=None, lease_ms=50):
        if not self.installed or self.terminal: raise RuntimeError('Live nonterminal addon required')
        if not isinstance(enabled, bool): raise ValueError('enabled must be a bool')
        if not isinstance(lease_ms, int) or not 1 <= lease_ms <= MAX_LEASE_MS: raise ValueError('Lease must be1..50ms')
        now = self._now() if now_ms is None else int(now_ms)
        if enabled:
            if self.pitch_limit is None: raise ValueError('Observer has no measured rate authority')
            if self.mode_pins is None: raise ValueError('Override requires immutable native route pins')
            if self.engine_pins is None: raise ValueError('Override requires immutable effective engine pins')
            if self.pitch_input_sign not in (-1, 1) or self.yaw_input_sign not in (-1, 1): raise ValueError('Override requires separately measured input signs')
            values = (pitch_full, yaw_full)
            for value, limit in zip(values, (self.pitch_limit, self.yaw_limit)):
                if value is None or not math.isfinite(value) or not 0 < abs(value) <= limit: raise ValueError('Explicit bounded signed singleton targets required')
        else: pitch_full = yaw_full = 0.
        sustained_end = None
        if enabled and self.sustained_windows is not None:
            if self.native_origin is None: raise RuntimeError('Sustained origin unavailable')
            elapsed = now - self.native_origin
            active = next(((start, end) for start, end in self.sustained_windows if start <= elapsed < end), None)
            if active is None: raise ValueError('Outside immutable sustained native window')
            sustained_end = self.native_origin + active[1]
            lease_ms = min(lease_ms, sustained_end - now)
        with self._lock:
            if self.terminal: raise RuntimeError('Terminal T66 addon cannot publish')
            if self._sequence >= 2**64 - 2 or self._generation >= 2**64 - 1: raise RuntimeError('T66 generations exhausted; terminal replacement required')
            # Renewing a held configuration cannot extend one rotation pulse.
            # A fresh explicit disabled publication is required between pulses.
            if enabled and not self._configuration[0]:
                self._write(112, struct.pack('<Q', now + 200 if sustained_end is None else sustained_end))
            elif not enabled: self._write(112, bytes(8))
            self._sequence += 2; self._generation += 1
            self._write(32, struct.pack('<Q', self._sequence - 1))
            self._write(40, struct.pack('<QIffIQQ', self._generation, enabled, pitch_full, yaw_full, 0, now, now + lease_ms))
            self._write(32, struct.pack('<Q', self._sequence))
            self._write(16, struct.pack('<Q', now + lease_ms))
            self._configuration = (enabled, pitch_full, yaw_full)

    def renew(self, now_ms=None, lease_ms=50):
        enabled, pitch, yaw = self._configuration
        self.publish(enabled=enabled, pitch_full=pitch, yaw_full=yaw, now_ms=now_ms, lease_ms=lease_ms)

    def stop(self):
        self.terminal = True
        failures = []
        if self.data:
            # Native stop is published before waiting for any in-flight publisher.
            try: self._write(8, struct.pack('<Q', 1))
            except BaseException as error: failures.append(repr(error))
            with self._lock:
                # A queued publisher must recheck terminal inside this same lock.
                # Try every tombstone independently even if one transport write fails.
                for offset, raw in ((8, struct.pack('<Q', 1)), (0, bytes(4)), (16, bytes(8)),
                                    (48, bytes(4)), (64, bytes(8)), (72, bytes(8)),
                                    (112, bytes(8)), (24, bytes(8))):
                    try: self._write(offset, raw)
                    except BaseException as error: failures.append(repr(error))
                self._configuration = (False, 0., 0.)
        if failures: raise RuntimeError('T66 stop publication failed: ' + '; '.join(failures))

    def restore(self, writer=replace_code):
        results = {}
        try: self.stop()
        except BaseException as error: results['stop'] = repr(error)
        for name in ('target', 'ordinary', 'torque'):
            if name not in self.items: continue
            item = self.items[name]; original = bytes.fromhex(item['original']); patch = bytes.fromhex(item['patch'])
            try:
                current = self.pm.read_bytes(item['address'], len(original))
                if current == patch: writer(self.pm, self.pid, item['address'], patch, original)
                elif current != original: raise RuntimeError('Foreign T66 bytes; restoration refused')
                results[name] = self.pm.read_bytes(item['address'], len(original)) == original
            except BaseException as error: results[name] = repr(error)
        self.installed = False
        if any(value is not True for value in results.values()): raise RuntimeError(str(results))
        return results

    def read_records(self, after=0):
        if not self.data: return [], after, 0
        latest = struct.unpack('<Q', self.pm.read_bytes(self.data + 88, 8))[0]
        first = max(after + 1, latest - CAPACITY + 1); out = []; missed = max(0, first - after - 1)
        for sequence in range(first, latest + 1):
            address = self.data + RING + (sequence & (CAPACITY - 1)) * SLOT
            before = self.pm.read_bytes(address, 8); raw = self.pm.read_bytes(address, SLOT)
            if before != self.pm.read_bytes(address, 8): missed += 1; continue
            record = decode_record(raw)
            if record and record['record_sequence'] == sequence: out.append(record)
            else: missed += 1
        return out, latest, missed


class ProductionRotationBridge(RotationBridge):
    """Qualified release bridge with immutable measured independent endpoints.

    The host verifies the exact executable, current process/fixture, source,
    settings and accepted live evidence before constructing this bridge. This
    class never discovers, attaches, calibrates, qualifies or enables itself.
    Unlike finite research trials, renewable authority ends only at the short
    native lease, explicit disable/stop, or any existing native rejection.
    An explicit investigator may additionally pin nonrenewable validation
    windows and a magnitude bound under the existing fixed native hard deadline.
    """
    _production = True

    def __init__(self, pm, pid, base, newtonian_data, frame, mailbox, pilot_deadline, tick, fixture,
                 *, signed_endpoints, exe_sha256, mode_pins, engine_pins, counter=0, hard_duration_ms=None,
                 validation_windows=None, validation_max_input_amplitude=1., rotation=None,
                 allow_planetary_flight=False, air_authority=False):
        if exe_sha256 != EXPECTED_SHA: raise ValueError('Exact qualified executable SHA256 required')
        rotation = _rotation(rotation)
        if air_authority and not allow_planetary_flight:
            raise ValueError('Air nose authority needs the planetary cave')
        # Shaping-only (T90/T92) keeps native targets; it needs no measured endpoints.
        endpoints = (None if signed_endpoints is None and (rotation != NATIVE_ROTATION or air_authority)
                     else _signed_endpoints(signed_endpoints))
        if mode_pins is None or engine_pins is None:
            raise ValueError('Production requires immutable native route and engine pins')
        if hard_duration_ms is not None and (isinstance(hard_duration_ms, bool) or not isinstance(hard_duration_ms, int)
                                              or not 1 <= hard_duration_ms <= 30000):
            raise ValueError('Finite production qualification duration must be1..30000ms')
        if (type(validation_max_input_amplitude) not in (int, float)
                or not math.isfinite(validation_max_input_amplitude)
                or not 0 < validation_max_input_amplitude <= 1.):
            raise ValueError('Finite production input amplitude must be positive and <=1')
        if validation_windows is None:
            if validation_max_input_amplitude != 1.:
                raise ValueError('Finite production input restriction requires validation windows')
            windows = None
        else:
            windows = freeze_windows(validation_windows, hard_duration_ms)
        super().__init__(pm, pid, base, newtonian_data, frame, mailbox, pilot_deadline, tick, fixture,
                         mode_pins=mode_pins, engine_pins=engine_pins, counter=counter)
        self.hard_duration_ms = hard_duration_ms
        self.sustained_windows = windows
        # Round the optional finite bound toward zero, so native float32
        # comparison never admits more input than the declared numeric limit.
        raw_limit = struct.pack('<f', validation_max_input_amplitude)
        native_limit = struct.unpack('<f', raw_limit)[0]
        if native_limit > validation_max_input_amplitude:
            native_limit = struct.unpack('<f', struct.pack('<I', struct.unpack('<I', raw_limit)[0] - 1))[0]
        if native_limit == 0:
            raise ValueError('Finite production input amplitude is below native float32 range')
        self.max_input_amplitude = native_limit
        self.signed_endpoints = None if endpoints is None else MappingProxyType(endpoints)
        self.rotation = MappingProxyType(rotation)
        self.allow_planetary_flight, self.air_authority = bool(allow_planetary_flight), bool(air_authority)
        self.fixture = MappingProxyType(self.fixture)
        self.mode_pins = MappingProxyType(self.mode_pins)
        self.engine_pins = MappingProxyType(self.engine_pins)

    def publish(self, *, enabled=False, now_ms=None, lease_ms=MAX_LEASE_MS):
        if not self.installed or self.terminal: raise RuntimeError('Live nonterminal addon required')
        if not isinstance(enabled, bool): raise ValueError('enabled must be a bool')
        if not isinstance(lease_ms, int) or not 1 <= lease_ms <= MAX_LEASE_MS: raise ValueError('Lease must be1..50ms')
        now = self._now() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or not 0 <= now < 2**64 - MAX_LEASE_MS:
            raise ValueError('Pinned monotonic uint64 native clock required')
        with self._lock:
            if self.terminal: raise RuntimeError('Terminal T66 addon cannot publish')
            if enabled:
                if not self._configuration[0]:
                    if self._generation >= 2**64 - 1: raise RuntimeError('T66 generations exhausted; terminal replacement required')
                    self._generation += 1
                    self._write(40, struct.pack('<Q', self._generation))
                    self._write(16, struct.pack('<Q', now + lease_ms))
                    self._write(48, struct.pack('<I', 1))
                else:
                    # One aligned deadline publication; no mutable target
                    # payload or odd seqlock can interrupt a held command.
                    self._write(16, struct.pack('<Q', now + lease_ms))
            else:
                self._write(48, bytes(4))
                self._write(16, bytes(8))
            self._configuration = (enabled, 0., 0.)

    def renew(self, now_ms=None, lease_ms=MAX_LEASE_MS):
        self.publish(enabled=self._configuration[0], now_ms=now_ms, lease_ms=lease_ms)

    def set_air(self, scale=1., damping=1., pitch_sway=0., yaw_sway=0.):
        """T92: this frame's air terms (defaults = none), clamped into the cave's ranges.

        Four aligned float stores. A frame that reads a mix of two publications still sees valid values.
        Returns the float32 values written.
        """
        if not self.air_authority: raise RuntimeError('Cave built without air nose authority')
        if not self.installed or self.terminal: raise RuntimeError('Live nonterminal addon required')
        values = (scale, damping, pitch_sway, yaw_sway)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError('Finite air terms required')
        sway = SWAY_LIMIT * .99
        raw = struct.pack('<4f', min(AIR_RANGE[1], max(AIR_RANGE[0], float(scale))),
                          min(DAMPING_RANGE[1], max(DAMPING_RANGE[0], float(damping))),
                          *(min(sway, max(-sway, float(v))) for v in (pitch_sway, yaw_sway)))
        self._write(AIR_SCALE, raw)
        return struct.unpack('<4f', raw)
