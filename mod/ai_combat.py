"""NPC beam switching and shield timing, NMS 180836 / Steam 25732212.

This changes eleven loaded table floats through AIPilotTuning's guarded ownership
engine; no code patches or hooks. SPACE.LaserHealthPoint controls the firing NPC's
own hull-health threshold, in percent. An explicit beams-off choice uses -1 so the
native branch cannot request a beam even at zero health. A beam already requested
can retain its native minimum-duration tail.

Shield timings use five shared profiles. They affect pirates and police, and also
NPC allies, traders, planetary ships and swarm drones using those profiles. Freighter,
shield-generator, civilian-leader, swarm-hive and NULL profiles are excluded. The
shield component retains a pointer to its selected row and reads its delay/refill
values every update, so existing ships should adopt changes; live timing is untested.
"""
from ai_pilots import AIPilotTuning, PINS as PILOT_PINS, _bounded

COMBAT_KNOBS = {
    'ai-beam-switch-health': (.5, .01, 1.),
    'ai-shield-delay-scale': (1., 0., 10.),
    'ai-shield-refill-scale': (1., .25, 10.),
}
COMBAT_BOOLS = {'ai-low-health-beams': True, 'ai-shield-recharge': True}

ARRAYS = {'behaviour': (0x40, 0xD0), 'shield': (0x70, 0x38)}
FIELDS = {
    'behaviour': {'LaserHealthPoint': 0xBC},
    'shield': {'RechargeDelayTime': 0x28, 'RechargeTime': 0x2C},
}
ROW_ID_OFFSETS = {'shield': 0x10}
# Shipped values, independently checked against the current executable reflection
# and extracted AISPACESHIPATTACKDATATABLE. Default endpoints preserve vanilla.
TUNING = {('behaviour', 'SPACE', 'LaserHealthPoint'): (50., 50.)}
for _profile, _delay, _refill in (
        ('WEAK', 12., 10.), ('STANDARD', 6., 5.), ('STRONG', 10., 8.),
        ('FAST', 3., 1.), ('FAST_STRONG', 4., 3.)):
    TUNING[('shield', _profile, 'RechargeDelayTime')] = (_delay, _delay)
    TUNING[('shield', _profile, 'RechargeTime')] = (_refill, _refill)
del _profile, _delay, _refill

PINS = {
    **PILOT_PINS,
    # Fire lookup, own shootable and health-percent calculation, then +BC threshold.
    0x1714457: bytes.fromhex('e8e49e0000488bf84885c0'),
    0x1714468: bytes.fromhex('0fbf0d41b29903488b5608e8980c61014885c0'),
    0x171447D: bytes.fromhex('488bc8e88b7c29fff30f109fbc0000000f2fd87231'),
    # The selected branch requests beam mode1; native gun cadence follows otherwise.
    0x17144A7: bytes.fromhex('ba01000000488bce0f1040300f5840400f29442420e8fffaffff'),
    0x17144C3: bytes.fromhex('f30f1005318ca405f30f5c86fc7c00000f2f87b800000076114c8bcb4c8bc5498bd6488bcee823000000'),
    # Health percent is clamped 0..100, including fallback100 for nonpositive max HP.
    0x9AC110: bytes.fromhex('448b81700300004585c07e40418bd066410f6ec02b9110040000b8000000000f5bc00f49c2660f6ec80f57d20f5bc9f30f5ec8f30f10050dd71704f30f5fd1f30f5dc2f30f590559dc1704c3f30f100550dc1704c3'),
    # Shield lookup uses the same attack-table pointer, dynamic array+70/count+78.
    0x1797A26: bytes.fromhex('488b8810d96c004863417885c00f8e060100004c8b52084c8bd848895c2450418bd0488b5970'),
    # Stride38 and retained row pointer at shield-component+70.
    0x1797A78: bytes.fromhex('4963c0486bc8384803cb0f84b200000048837e480048894e70'),
    # Reads RechargeTime+2C, skips <=0, compares time since damage to delay+28.
    0x1797EDF: bytes.fromhex('488b4770f30f10502c410f2fd376674c8b5f48f3410f108bdc040000410f2fcb7612f30f1005f3519c05f30f5cc10f2f40287242'),
    # Amount restored is trunc_i32(min(maxHealth*dt/RechargeTime, missingHealth)).
    0x1797F13: bytes.fromhex('66410f6ec8498bcb0f5bc9452bc266410f6ec0f3410f59ca0f5bc0f30f5ecaf30f5dc8f30f2cd1e8b157dfff'),
}


def tuning_values(values=None):
    """Normalize just these settings; malformed persisted booleans retain defaults."""
    values = values or {}
    knobs = {key: _bounded(values.get(key, default), default, low, high)
             for key, (default, low, high) in COMBAT_KNOBS.items()}
    knobs.update({key: values[key] if isinstance(values.get(key), bool) else default
                  for key, default in COMBAT_BOOLS.items()})
    return knobs


def target_value(key, readability, tuning=None):
    """Blend numeric endpoints; explicit off takes effect at any positive strength."""
    vanilla = TUNING[key][0]
    strength = _bounded(readability, 0., 0., 1.)
    if strength == 0.:
        return vanilla
    knobs = tuning_values(tuning)
    field = key[2]
    if field == 'LaserHealthPoint':
        if not knobs['ai-low-health-beams']:
            return -1.
        endpoint = 100. * knobs['ai-beam-switch-health']
    elif field == 'RechargeDelayTime':
        endpoint = vanilla * knobs['ai-shield-delay-scale']
    elif field == 'RechargeTime':
        if not knobs['ai-shield-recharge']:
            return 0.
        endpoint = vanilla * knobs['ai-shield-refill-scale']
    else:
        raise KeyError(key)
    return endpoint if strength == 1. else vanilla + (endpoint - vanilla) * strength


class NPCCombatTuning(AIPilotTuning):
    """Independent ownership of combat fields; pilot movement can update concurrently."""
    arrays, fields, targets, pins = ARRAYS, FIELDS, TUNING, PINS
    row_id_offsets = ROW_ID_OFFSETS
    normalize = staticmethod(tuning_values)
    target = staticmethod(target_value)

