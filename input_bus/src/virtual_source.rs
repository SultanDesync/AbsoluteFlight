//! Agent-drivable virtual device (`docs/INPUT-ARCHITECTURE.md` §6). It is an ordinary bus source with a
//! reserved `virtual:` persistent id, zero GUIDs and VID/PID 0. State is set through a cloneable handle,
//! from any thread; the bus polls it like hardware. It never touches DirectInput or the game.
//!
//! Optional leases expire on the native poll thread, independently of the Python test pipe. Decision
//! bounds belong to that pipe; ordinary Rust test devices remain unleased.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::abi::{AXIS_COUNT, BUTTON_COUNT, POV_COUNT};
use crate::source::{raw_from_normalized, DeviceDescriptor, InputSource, RawState, SourceFactory, POV_CENTERED};
use crate::token::{canonical_device_id, Control, PovDirection, VIRTUAL_PREFIX};

#[derive(Debug)]
struct Shared {
    state: RawState,
    connected: bool,
    lease: Option<Lease>,
}

#[derive(Debug)]
struct Lease {
    duration: Duration,
    deadline: Instant,
}

impl Shared {
    fn expire(&mut self, now: Instant) {
        if self.lease.as_ref().is_some_and(|lease| now >= lease.deadline) {
            self.disconnect();
        }
    }

    fn disconnect(&mut self) {
        self.state = RawState::neutral();
        self.connected = false;
        self.lease = None;
    }
}

#[derive(Clone, Debug)]
pub struct VirtualHandle {
    persistent_id: String,
    shared: Arc<Mutex<Shared>>,
}

impl VirtualHandle {
    pub fn persistent_id(&self) -> &str {
        &self.persistent_id
    }

    fn with<R>(&self, f: impl FnOnce(&mut Shared) -> R) -> R {
        let mut guard = self.shared.lock().unwrap_or_else(|p| p.into_inner());
        guard.expire(Instant::now());
        f(&mut guard)
    }

    /// Arms a fresh, neutral lease. Expiry disconnects until explicitly armed again. Reserve 50 ms
    /// of the ABI's 500 ms ceiling for native polling/publication (the host enforces at least 100 Hz).
    pub(crate) fn enable_lease(&self, duration: Duration) {
        self.with(|s| {
            let duration = duration.min(Duration::from_millis(450));
            s.state = RawState::neutral();
            s.connected = true;
            s.lease = Some(Lease { duration, deadline: Instant::now() + duration });
        });
    }

    pub(crate) fn heartbeat(&self) -> bool {
        self.with(|s| {
            let connected = s.connected;
            if let Some(lease) = s.lease.as_mut().filter(|_| connected) {
                lease.deadline = Instant::now() + lease.duration;
                true
            } else {
                false
            }
        })
    }

    /// Checks and modifies the lease under one lock so expiry cannot race a state setter.
    pub(crate) fn update_leased(&self, update: impl FnOnce(&mut RawState)) -> bool {
        self.with(|s| {
            if s.connected && s.lease.is_some() {
                update(&mut s.state);
                true
            } else {
                false
            }
        })
    }

    pub(crate) fn disconnect(&self) {
        self.with(Shared::disconnect);
    }

    /// Another poll-thread source sharing this handle (used after a stopped bus is restarted).
    pub(crate) fn source_factory(&self) -> SourceFactory {
        let handle = self.clone();
        Box::new(move || Box::new(VirtualSource::from_handle(handle)))
    }

    /// Sets an axis by HID usage index 0..7 to a bipolar normalized value.
    pub fn set_axis(&self, axis: usize, value: f32) {
        if axis < AXIS_COUNT {
            self.with(|s| s.state.axes[axis] = raw_from_normalized(value));
        }
    }

    pub fn set_axis_raw(&self, axis: usize, raw: i32) {
        if axis < AXIS_COUNT {
            self.with(|s| s.state.axes[axis] = raw);
        }
    }

    /// Button 1..128.
    pub fn set_button(&self, button: usize, down: bool) {
        if (1..=BUTTON_COUNT).contains(&button) {
            self.with(|s| s.state.buttons[button - 1] = down);
        }
    }

    /// POV 0..3, `None` = centered.
    pub fn set_pov(&self, pov: usize, direction: Option<PovDirection>) {
        if pov < POV_COUNT {
            let raw = direction.map_or(POV_CENTERED, |d| d.index() as u32 * 9000);
            self.with(|s| s.state.povs[pov] = raw);
        }
    }

    /// Drives one control named by a token's control part. Axes take `value`, digital controls `value > 0.5`.
    pub fn set_control(&self, control: Control, value: f32) {
        match control {
            Control::Axis(_) => self.set_axis(control.axis_index().unwrap(), value),
            Control::Button(n) => self.set_button(n as usize, value > 0.5),
            Control::Pov(i, d) => self.set_pov(i as usize, (value > 0.5).then_some(d)),
        }
    }

    /// Centers every axis, releases every button, centers every POV. The watchdog's safe state.
    pub fn neutral(&self) {
        self.with(|s| s.state = RawState::neutral());
    }

    /// Simulates unplug/replug (exercises reconnect baselines and generation bumps).
    pub fn set_connected(&self, connected: bool) {
        self.with(|s| s.connected = connected);
    }

    pub fn state(&self) -> RawState {
        self.with(|s| s.state)
    }
}

pub struct VirtualSource {
    handle: VirtualHandle,
    devices: Vec<DeviceDescriptor>,
}

impl VirtualSource {
    /// `name` becomes `virtual:<name>`; e.g. `agent-0`.
    pub fn new(name: &str) -> Result<(Self, VirtualHandle), crate::token::TokenError> {
        let persistent_id = canonical_device_id(&format!("{VIRTUAL_PREFIX}{name}"))?;
        let handle = VirtualHandle {
            persistent_id: persistent_id.clone(),
            shared: Arc::new(Mutex::new(Shared { state: RawState::neutral(), connected: true, lease: None })),
        };
        Ok((Self::from_handle(handle.clone()), handle))
    }

    fn from_handle(handle: VirtualHandle) -> Self {
        let name = handle.persistent_id.strip_prefix(VIRTUAL_PREFIX).unwrap_or("device");
        let descriptor = DeviceDescriptor {
            persistent_id: handle.persistent_id.clone(),
            instance_guid: [0; 16],
            product_guid: [0; 16],
            vendor_id: 0,
            product_id: 0,
            axis_count: AXIS_COUNT as u32,
            button_count: BUTTON_COUNT as u32,
            pov_count: POV_COUNT as u32,
            instance_name: format!("Virtual {name}"),
            product_name: "AbsoluteHOTAS-NMS virtual device".to_string(),
            notes: "synthetic source".to_string(),
        };
        Self { handle, devices: vec![descriptor] }
    }

    /// A bus source factory plus the handle that drives it.
    pub fn factory(name: &str) -> Result<(SourceFactory, VirtualHandle), crate::token::TokenError> {
        let (source, handle) = Self::new(name)?;
        Ok((Box::new(move || Box::new(source) as Box<dyn InputSource>), handle))
    }
}

impl InputSource for VirtualSource {
    fn name(&self) -> &str {
        "virtual"
    }

    fn maintain(&mut self) -> bool {
        false
    }

    fn devices(&self) -> &[DeviceDescriptor] {
        &self.devices
    }

    fn poll(&mut self, index: usize) -> Option<RawState> {
        if index != 0 {
            return None;
        }
        self.handle.with(|s| s.connected.then_some(s.state))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expired_lease_neutralizes_every_control_and_cannot_be_renewed() {
        let (mut source, handle) = VirtualSource::new("lease-test").unwrap();
        handle.enable_lease(Duration::from_millis(500));
        assert!(handle.update_leased(|state| {
            state.axes[0] = 65535;
            state.buttons[127] = true;
            state.povs[3] = 9000;
        }));
        {
            let mut shared = handle.shared.lock().unwrap();
            assert_eq!(shared.lease.as_ref().unwrap().duration, Duration::from_millis(450));
            shared.lease.as_mut().unwrap().deadline = Instant::now();
        }
        assert!(source.poll(0).is_none());
        assert_eq!(handle.state(), RawState::neutral());
        assert!(!handle.heartbeat());
        assert!(!handle.update_leased(|state| state.buttons[0] = true));
        handle.enable_lease(Duration::from_millis(100));
        assert_eq!(source.poll(0), Some(RawState::neutral()));
        assert!(handle.heartbeat());
    }

    #[test]
    fn unleased_rust_test_sources_keep_existing_behavior() {
        let (mut source, handle) = VirtualSource::new("unleased").unwrap();
        handle.set_button(1, true);
        assert!(source.poll(0).unwrap().buttons[0]);
        handle.neutral();
        assert_eq!(source.poll(0), Some(RawState::neutral()));
    }
}
