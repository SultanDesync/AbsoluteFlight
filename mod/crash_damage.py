"""T94 crash damage: settings, pure impact/damage math and disabling-hit test.

Works with tools/native_crash_damage.py (the four native caves in 0x17951D0 -> 0x1797960 -> TakeDamage, build 183791).
Settings live on the Ship Damage menu page, default Off.
"""
from dataclasses import dataclass
import math

GROUND_DAMAGE = 10.    # damagetable.mbin IMPACTGROUND
INSTANCE_DAMAGE = 30.  # damagetable.mbin IMPACTINSTANCE
NATIVE_BREAK_CHANCE = .1


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    safe: float = 25.          # u/s: below this, scrapes stay native (shields first)
    per_us: float = 4.         # extra hull damage per u/s over safe
    glancing: float = .1       # share of sliding speed counted toward impact speed
    break_speed: float = 60.   # u/s where launch-thruster break chance reaches 100%
    disable_speed: float = 60. # u/s where a hit triggers the disabling crash sequence
    never_kill: bool = True    # cap terrain/object hull damage at hull-1; capped hits are disabling
    ground: float = GROUND_DAMAGE
    instance: float = INSTANCE_DAMAGE
    native_chance: float = NATIVE_BREAK_CHANCE

    @classmethod
    def from_values(cls, values):
        return cls(
            enabled=bool(values.get('crash-damage', False)),
            safe=float(values.get('crash-safe-speed', 25.)),
            per_us=float(values.get('crash-damage-per-us', 4.)),
            glancing=float(values.get('crash-glancing', .1)),
            break_speed=float(values.get('crash-break-speed', 60.)),
            disable_speed=float(values.get('crash-disable-speed', 60.)),
        )


def impact_components(speed, direction, normal, settings=Settings()):
    """(into_surface, sliding, impact) in u/s from speed, velocity direction and surface normal.
    Returns None when vectors or speed are degenerate, matching cave A's pass-through guard."""
    if not (isinstance(speed, (int, float)) and math.isfinite(speed) and 0. <= speed < 5000.):
        return None
    if len(direction) < 3 or len(normal) < 3:
        return None
    dx, dy, dz = (float(x) for x in direction[:3])
    nx, ny, nz = (float(x) for x in normal[:3])
    norm2 = (nx*nx + ny*ny + nz*nz) * (dx*dx + dy*dy + dz*dz)
    if not (math.isfinite(norm2) and norm2 > 1e-12):
        return None
    cos = min(1., abs(dx*nx + dy*ny + dz*nz) / math.sqrt(norm2))
    sin = math.sqrt(max(0., 1. - cos*cos))
    vn = speed * cos
    vt = speed * sin
    veff = vn + settings.glancing * vt
    return vn, vt, veff


def hit_plan(kind, impact, settings=Settings()):
    """Pure model of cave A's scaling for a 'ground' or 'object' hit at effective speed `impact`."""
    table = settings.ground if kind == 'ground' else settings.instance
    disabling = impact >= settings.disable_speed
    if impact <= settings.safe or table <= 0.:
        return {
            'kind': kind,
            'impact': impact,
            'multiplier': 1.,
            'damage': table,
            'hull_first': False,
            'break_chance': settings.native_chance,
            'disabling': disabling,
            'never_kill': settings.never_kill,
        }
    excess = impact - settings.safe
    damage = table + settings.per_us * excess
    multiplier = damage / table
    span = settings.break_speed - settings.safe
    t = 1. if span <= 0. else min(1., max(0., excess / span))
    chance = settings.native_chance + (1. - settings.native_chance) * t
    return {
        'kind': kind,
        'impact': impact,
        'multiplier': multiplier,
        'damage': damage,
        'hull_first': True,
        'break_chance': chance,
        'disabling': disabling,
        'never_kill': settings.never_kill,
    }


def is_disabling(record):
    """True when a hit record from the native ring should trigger the disabling crash sequence:
    impact >= Disabling speed, or the never-kill cap saved the hull from emptying."""
    if not isinstance(record, dict) or record.get('submerged'):
        return False
    return bool(record.get('disabling') or record.get('capped'))


def format_last_hit(record):
    """Coarse menu readout line for the most recent collision hit, or None."""
    if not isinstance(record, dict):
        return None
    kind = str(record.get('kind', 'hit')).upper()
    impact = round(float(record.get('impact', 0.)))
    before, after = record.get('hull_before', -1), record.get('hull_after', -1)
    hull = f', HULL {before}->{after}' if isinstance(before, int) and before >= 0 and isinstance(after, int) and after >= 0 else ''
    tag = ' (DISABLING)' if is_disabling(record) else (' (HULL)' if record.get('hull_first') else ' (SHIELD)')
    return f'LAST HIT: {kind} {impact} U/S{hull}{tag}'
