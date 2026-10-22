"""Flight-thread T55 consumer. Pure state machine; no game memory or hooks here."""
from dataclasses import replace
import math

from newtonian_flight import Basis, CommandLease, FlightProfile, compute_acceleration
import manual_landing
import planetary_flight
from ship_condition import ShipCondition


SUPPORTED_CLASSES = (1, 2, 3, 4, 6, 7, 8, 9, 10)  # Corvette: 2026-10-22 (user): Corvette flies Sultan on its baseline, marked incomplete


def boost_strength(heat, profile):
    """Share of the boost multiplier delivered at this heat (user design, 2026-10-17): weak when
    cold, rising smoothly to full at the hot zone, full through the hot zone up to overheat. So the
    skill is to keep boost hot without overheating it. Cold strength 1 gives the flat T76 behaviour."""
    x = min(1., max(0., heat/profile.boost_hot_zone))
    x = x*x*(3.-2.*x)  # smoothstep
    return profile.boost_cold_strength+(1.-profile.boost_cold_strength)*x


class BoostBudget:
    """Mod-owned heat, advanced by qualified simulation time, never inventory writes.

    A boundary-crossing frame receives only its remaining fraction of boosted
    authority. A held request cools while locked out and can relight on the next
    frame after reaching the low threshold. Pauses/native fallback freeze heat.
    """
    COOL_DELAY = .5

    def __init__(self):
        self.ship = None
        self.heat = 0.
        self.overheated = False
        self.idle_time = 0.

    def select_ship(self, ship):
        if self.ship != ship:
            self.ship = ship
            self.heat = 0.
            self.overheated = False
            self.idle_time = 0.

    def _cool(self, dt, profile):
        before = max(0., self.idle_time-self.COOL_DELAY)
        self.idle_time += dt
        cooling = max(0., self.idle_time-self.COOL_DELAY)-before
        self.heat = max(0., self.heat-cooling/profile.boost_cool_time)
        if self.overheated and self.heat <= profile.boost_relight_level:
            self.overheated = False

    def step(self, profile, requested, native, dt):
        if not math.isfinite(dt) or dt <= 0.:
            raise ValueError('Boost timestep must be positive and finite')
        multiplier = 1.
        if profile.boost_enabled and requested:
            if profile.boost_source == 'fixed':
                multiplier = profile.boost_multiplier
            else:
                thrust, boosted = native.get('thrust_force'), native.get('boost_thrust_force')
                if (isinstance(thrust, (int, float)) and isinstance(boosted, (int, float))
                        and math.isfinite(thrust) and math.isfinite(boosted) and thrust > 0.):
                    ratio = boosted/thrust
                    if math.isfinite(ratio):
                        multiplier = max(1., ratio)
        if multiplier <= 1. or self.overheated:
            self._cool(dt, profile)
            return 1., False
        burn = min(dt, max(0., (1.-self.heat)*profile.boost_heat_time))
        strength = boost_strength(self.heat, profile)  # at the heat this frame starts from
        self.heat = min(1., self.heat+burn/profile.boost_heat_time)
        self.idle_time = 0.
        if self.heat >= 1.-1e-12:
            self.heat = 1.
            self.overheated = True
            self._cool(max(0., dt-burn), profile)
        # Fractional-frame authority cannot spend more heat than remains.
        return 1.+(multiplier-1.)*strength*(burn/dt), burn > 0.

    def status(self):
        return {'boost_heat': self.heat, 'boost_overheated': self.overheated,
                'boost_active': False, 'boost_multiplier': 1., 'bleeding_overspeed': False}


class FlightConsumer:
    def __init__(self, boost=None, condition=None):
        self.lease = CommandLease(ttl=.1, capacity=256)
        self.previous = (0., 0., 0.)
        self.boost = boost if boost is not None else BoostBudget()
        self.latest = {'admitted': False, 'reason': 'not selected', 'command_state': 'expired',
                       **self.boost.status()}
        self.identity = None
        self.density = planetary_flight.DensityFilter()
        self.condition = condition if condition is not None else ShipCondition()  # T93: engines out after a ditch

    def fallback(self, reason, **extra):
        self.previous = (0., 0., 0.)
        self.latest = {'admitted': False, 'reason': reason, **self.boost.status(), **extra}
        return 2, None

    def consume(self, command, profiles, native, now_tick):
        """Return (native helper result, FlightResult or None).

        native contains only the current invocation's proven class, body basis,
        velocity, effective ThrustForce and effective native time step. Nothing
        here integrates or writes velocity. Return 0 always means immediate coast.
        """
        if command is None or not command.enabled:
            return self.fallback('not selected or input disabled')
        # The installed native helper owns the complete mode/property gates. This
        # optional flag lets adapters retain that rejection rather than turn it
        # into a zero-force override; live helper calls arrive only after passing.
        if native.get('admitted') is False:
            return self.fallback(native.get('reason', 'native gate rejected'))
        class_id = native['class_id']
        if class_id not in SUPPORTED_CLASSES:
            return self.fallback('ship class unqualified', ship_class=class_id)
        if class_id not in profiles:
            return self.fallback('ship class profile missing', ship_class=class_id)
        identity = (native.get('ship'), native.get('body'), class_id)
        if self.identity != identity:
            self.previous = (0., 0., 0.)
            self.identity = identity
            self.density.reset()
        self.boost.select_ship(native.get('ship'))
        condition = self.condition
        condition.select_ship(native.get('ship'))
        water = native.get('water')
        if (water is None or not water.ditch) and condition.engines_out:condition.reset()  # setting turned off
        now = now_tick / 1000.
        # Observe expiry BEFORE submitting a resumed generation. Recovery cannot
        # turn a stale receipt into fresh thrust merely by refreshing a clock.
        self.lease.sample(now)
        self.lease.submit(command.generation, now, produced_at=(command.deadline - 100) / 1000.)
        sample = self.lease.sample(now)
        usable = (sample.usable and sample.generation == command.generation
                  and now_tick < command.deadline)
        profile = FlightProfile.from_mapping(profiles[class_id])
        basis = Basis(*native['basis'])
        multiplier, boosting = self.boost.step(
            profile, bool(usable and command.throttle.boost_requested and not command.throttle.parking
                          and not condition.engines_out), native, native['dt'])
        planet = native.get('planet')
        planetary = None
        if planet is not None and class_id in planetary_flight.AIR_CLASSES:
            # T88: gravity and air below orbit. Sultan's engine loss replaces the native Planet-block blend.
            rho = self.density.step(planet['density'], native['dt'])
            style = planetary_flight.AirStyle.from_profile(profiles[class_id])
            # T93: over water with Ditch at sea on, the sea is ours (the helper cancels the native
            # ground-clearance term); hitting it too fast or too steeply knocks the engines out.
            owned = bool(water is not None and water.ditch and native.get('over_water'))
            submerged = planetary_flight.submersion(planet['altitude']) if owned else 0.
            if usable:
                condition.step(native['dt'], now_tick)
                if (submerged > 0. and planetary_flight.ditch_impact(native['velocity'], planet['up'], water)
                        and condition.knock_out('ditch', impact=math.hypot(*native['velocity']))):
                    self.previous = (0., 0., 0.)
            air = planetary_flight.compute(
                profile, style, native['air'], planet, rho,
                command.lateral, command.vertical, command.throttle, native['velocity'], basis,
                native.get('space_thrust_force', native['thrust_force']), native['dt'],
                previous_acceleration_world=self.previous, boost_multiplier=multiplier, command_fresh=usable,
                trim=getattr(command, 'trim', 0.),
                water=water if owned else None, submerged=submerged, engines_out=condition.engines_out,
                gear=native.get('gear'), clearance=native.get('clearance'))
            result = air.flight
            if usable and condition.blasting(now_tick):
                # T93: the purge's boost stage relit the thrusters; the blast throws the ship up out of the water.
                up = planet['up']
                result = replace(result, acceleration_world=tuple(a+u*planetary_flight.BLAST_ACCEL
                                                                  for a, u in zip(result.acceleration_world, up)))
            speed = math.hypot(*native['velocity'])
            slip, slip_right, slip_up = planetary_flight.sideslip(native['velocity'], basis)
            planetary = {'density': rho, 'engine_efficiency': air.engine_efficiency,
                         'gravity': math.hypot(*air.gravity), 'altitude': planet['altitude'],
                         'drag': math.hypot(*air.drag), 'lift': math.hypot(*air.lift),
                         # T92: read by the rotation host, which ignores it once older than its lease.
                         'nose_authority': planetary_flight.nose_authority(style, rho, speed, slip),
                         # Sway input for the rotation seam: slip angle and body direction, slip pressure.
                         'slip': slip, 'slip_right': slip_right, 'slip_up': slip_up,
                         'slip_pressure': planetary_flight.slip_pressure(style, rho, speed),
                         # Past the terminal speed the host multiplies the sway and buffet by this.
                         'overspeed_multiplier': planetary_flight.overspeed_multiplier(style, rho, speed),
                         'overspeed': planetary_flight.overspeed(style, rho, speed),
                         # T88 lift trim: Trim to hover takes this share.
                         'hover_trim': air.hover_trim,
                         # T93: the helper cancels the native ground-clearance term while water_owned.
                         'water_owned': owned, 'submerged': submerged,
                         'buoyancy': math.hypot(*air.buoyancy), 'water_drag': math.hypot(*air.water_drag),
                         # T101: gear down (hover mode) and the native terrain clearance from the cave.
                         'gear': native.get('gear') is not None, 'clearance': native.get('clearance'),
                         'tilt': manual_landing.tilt(native['basis'][1], planet['up']),
                         # Landing HUD: level bubble, drift over the ground and sink rate (manual_landing.panel).
                         **manual_landing.ground_frame(native['basis'], planet['up'], native['velocity']),
                         **condition.status(),
                         'tick': now_tick}
        else:
            self.density.reset()
            result = compute_acceleration(
                profile, command.lateral, max(-1., min(1., command.vertical+getattr(command, 'trim', 0.))), command.throttle,
                native['velocity'], basis, native['thrust_force'], native['dt'],
                previous_acceleration_world=self.previous, command_fresh=usable,
                boost_multiplier=multiplier)
        # Automatic overspeed braking must not become pilot thrust on the next
        # jerk-smoothed frame; that would compound the bleed and brake below cap.
        self.previous = (result.pilot_acceleration_world if result.pilot_acceleration_world is not None
                         else result.acceleration_world)
        self.latest = {
            'admitted': True, 'reason': '' if usable else
                'generation rejected' if sample.generation != command.generation else sample.state,
            'command_state': sample.state, 'generation': command.generation,
            'age_ms': None if sample.age is None else sample.age * 1000., 'ship_class': class_id,
            'speed': math.hypot(*native['velocity']),
            'forward_speed': sum(a*b for a,b in zip(native['velocity'], native['basis'][2])),
            'cap_active': result.cap_active, 'acceleration_body': result.acceleration_body,
            'acceleration_world': result.acceleration_world, 'parking': bool(usable and result.parking),
            'target_speed': result.target_speed, 'zone': command.throttle.zone,
            **self.boost.status(), 'boost_active': boosting, 'boost_multiplier': multiplier,
            'bleeding_overspeed': result.bleeding_overspeed, 'planetary': planetary,
        }
        return (1 if usable else 0), result
