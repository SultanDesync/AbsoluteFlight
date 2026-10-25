"""Runtime glue for G-load effects: strain model -> native greyout and blackout lanes (T77 seam).

The dark periphery and the fade to black are drawn by the mod HUD (flight_hud.tunnel); the native
vignette lane is used only to mask the scene black once the fade is complete.

The release runtime owns threads and code writes; this owns the decisions:
- install only when the setting is on, the flight patches are installed, the display is flat (not VR)
  and both call sites hold their exact original bytes. The call sites are in the HUD screen-effect
  publisher, which runs on a different thread from the flight update (live 2026-10-17: HUD 43468,
  flight 39360), so the flight-thread drain cannot patch them between frames. The runtime passes a
  writer that swaps each call from a separate process (tools/patch_call.py: suspend, check no
  instruction pointer is inside, write, resume), the method codex's T77 live proof used;
- each tick: strain from the flight law's commanded acceleration (Sultan only; otherwise it recovers),
  identity refresh, and one leased command per lane. Native values pass through when the strain is
  zero, the setting is off or the command lapses (100 ms).
"""
import ctypes as C

import native_vignette as nv
from g_strain import GStrain


class GEffects:
    def __init__(self, log=lambda message: None):
        self.log = log; self.strain = GStrain(); self.lanes = None; self.reason = 'off'
        self.failed = False; self.hud_thread = None; self.last = None

    # ----- install / restore (runtime thread, under the runtime lock like _install) -----
    def ready_to_install(self, values, patches_installed, flight_thread, base, read=C.string_at):
        if self.lanes is not None or self.failed: return False
        if not values.get('g-effects-enabled') or values.get('flight-model') != 'sultan':
            self.reason = 'off'; return False
        if not patches_installed: self.reason = 'waiting for flight'; return False
        if int.from_bytes(read(base+nv.VR_MODE_GLOBAL, 4), 'little'):
            self.reason = 'VR display (flat only)'; return False
        if not self.hud_thread:
            self.reason = 'waiting for HUD frame'; return False
        for lane, rva in nv.SITES.items():
            if read(base+rva, 5) != nv.original_call(rva):
                self.reason = 'native code changed'; self.failed = True
                self.log(f'G effects refused: {lane} call site {rva:#x} is not the original call'); return False
        return True

    def install(self, pm, pid, base, tick_function, pilot_address, write_code, allocate_near):
        lanes = {}
        try:
            for lane, rva in nv.SITES.items():
                cave = allocate_near(pm.process_handle, base+rva)  # retained until process exit
                lanes[lane] = nv.Lane(lane, base, cave, tick_function, pilot_address, allow_combat=True)
                lanes[lane].write_code()
            for lane in lanes.values():
                write_code(pm, pid, lane.site, lane.original, lane.patch); lane.installed = True
        except BaseException as error:
            self.failed = True; self.reason = 'install failed'
            self.log('G effects install failed: '+repr(error))
            for lane in lanes.values():
                if lane.installed:
                    try: lane.disable(); write_code(pm, pid, lane.site, lane.patch, lane.original); lane.installed = False
                    except BaseException as restore_error: self.log('G effects restore failed: '+repr(restore_error))
            return False
        self.lanes = lanes; self.reason = 'installed'
        self.log('G effects installed: ' + ', '.join(f'{k} {v.site:#x} -> {v.cave:#x}' for k, v in lanes.items()))
        return True

    def uninstall(self, pm, pid, write_code):
        lanes, self.lanes = self.lanes, None
        if not lanes: return {}
        results = {}
        for name, lane in lanes.items():
            try:
                lane.disable()
                if lane.installed: write_code(pm, pid, lane.site, lane.patch, lane.original); lane.installed = False
                results[name] = True
            except BaseException as error: results[name] = repr(error)
        self.reason = 'restored'
        return results

    # ----- per tick (runtime thread) -----
    def tick(self, values, dt, now_tick, acceleration_body, identity):
        """acceleration_body: commanded (right, up, forward) u/s^2, or None outside Sultan flight.
        identity: (app, ship, attachment). Returns the effects dict (control scale for the caller)."""
        on = bool(values.get('g-effects-enabled') and values.get('flight-model') == 'sultan')
        if not on and self.strain.crash:
            # T93 crash blackout with G effects off: the mod HUD fade only (native lanes stay off).
            if self.lanes:
                for lane in self.lanes.values(): lane.disable()
            self.last = self.strain.step(None, dt, values)
            return self.last
        if not on:
            self.strain.reset()
            self.strain.measure(acceleration_body, values)  # the HUD G meter works with effects off
            if self.lanes:
                for lane in self.lanes.values(): lane.disable()
            self.last = None
            return {'greyout': 0., 'tunnel': 0., 'fade': 0., 'blackout': False, 'control': 1., 'red': 0.}
        effects = self.strain.step(acceleration_body, dt, values)
        self.last = effects
        if self.lanes:
            app, ship, attachment = identity
            for lane in self.lanes.values(): lane.set_identity(app, ship, attachment)
            active = effects['greyout'] > .001 or effects['tunnel'] > .001 or effects['fade'] > .001
            grey = self.lanes['greyout']; native_grey = grey.snapshot()['native'][1]
            ok_grey = 0. <= native_grey <= 1.
            grey.publish(now_tick, (max(effects['greyout'], native_grey if ok_grey else 0.), 0.), active)
            # The periphery and fade are drawn in the mod HUD (flight_hud.tunnel_layers): the native
            # vignette is a VR comfort shape (user, 2026-10-17). Native radii only mask the scene
            # black once the fade is complete and pass through otherwise.
            self.lanes['vignette'].publish(now_tick, nv.BLACK_VIGNETTE, effects['fade'] >= .999)
        return effects

    def status(self):
        out = {**self.strain.status(), 'installed': self.lanes is not None, 'reason': self.reason}
        if self.lanes:
            for name, lane in self.lanes.items(): out[name+'_applied'] = lane.snapshot()['applied']
        return out
