//! Host A: C exports for loading the bus inside NMS.exe (ctypes from pyMHF) or any other process.
//!
//! `AbsoluteInputBus_QueryApi(1)` returns the v1 table. The poll thread exists only between
//! `AbsoluteInputBus_Start(pollHz)` and `AbsoluteInputBus_Stop()`, so a pyMHF reload can stop it explicitly
//! (no work in `DllMain`). Table functions return `NotReady` while stopped. BUS-0 publishes no capture:
//! the capture entries are present (non-null, per the v1 discovery rule) but return `NotReady`, and the
//! capture capability bits are clear. Every export catches panics; nothing unwinds into the host.

use std::ffi::c_char;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;
use std::time::Duration;

use crate::abi::*;
use crate::bus::{Bus, BusConfig};
use crate::source::{raw_from_normalized, SourceFactory, POV_CENTERED};
use crate::virtual_source::{VirtualHandle, VirtualSource};

pub const PROVIDER_ID: &str = "absolute-input-bus-nms";

struct AgentState {
    handle: VirtualHandle,
    lease_duration: Duration,
    enabled: bool,
    attached: bool,
}

struct HostState {
    bus: Option<Bus>,
    agent: Option<AgentState>,
}

// No synthetic source exists until the host explicitly enables the dev test interface.
static BUS: Mutex<HostState> = Mutex::new(HostState { bus: None, agent: None });

const PROVIDER_ID_C: &[u8] = b"absolute-input-bus-nms\0";
const DISPLAY_NAME_C: &[u8] = b"Absolute Input Bus (NMS)\0";
const VERSION_C: &[u8] = concat!(env!("CARGO_PKG_VERSION"), "\0").as_bytes();

/// Capabilities published by BUS-0: no capture yet.
pub const CAPABILITIES_BUS0: u64 =
    CAPABILITY_DEVICE_ENUMERATION | CAPABILITY_SNAPSHOTS | CAPABILITY_PROFILE_IDENTITY | CAPABILITY_RUNTIME_CONTEXT;

static API: ApiV1 = ApiV1 {
    struct_size: std::mem::size_of::<ApiV1>() as u32,
    abi_version: ABI_VERSION,
    provider_id: PROVIDER_ID_C.as_ptr() as *const c_char,
    display_name: DISPLAY_NAME_C.as_ptr() as *const c_char,
    version: VERSION_C.as_ptr() as *const c_char,
    capabilities: CAPABILITIES_BUS0,
    get_device_count: Some(get_device_count),
    get_device: Some(get_device),
    get_snapshot: Some(get_snapshot),
    get_profile_state: Some(get_profile_state),
    get_runtime_context: Some(get_runtime_context),
    begin_capture: Some(begin_capture),
    poll_capture: Some(poll_capture),
    cancel_capture: Some(cancel_capture),
};

fn guarded<R>(fallback: R, f: impl FnOnce() -> R) -> R {
    catch_unwind(AssertUnwindSafe(f)).unwrap_or(fallback)
}

fn with_bus<R>(not_ready: R, f: impl FnOnce(&Bus) -> R) -> R {
    let guard = BUS.lock().unwrap_or_else(|p| p.into_inner());
    match guard.bus.as_ref() {
        Some(bus) if bus.ready() => f(bus),
        _ => not_ready,
    }
}

#[no_mangle]
pub extern "C" fn AbsoluteInputBus_QueryApi(requested_abi_version: u32) -> *const ApiV1 {
    if requested_abi_version == ABI_VERSION {
        &API
    } else {
        std::ptr::null()
    }
}

/// Starts the DirectInput poll thread. Returns `Ok` if started or already running.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_Start(poll_hz: u32) -> BusResult {
    guarded(BusResult::NotReady, || {
        let mut guard = BUS.lock().unwrap_or_else(|p| p.into_inner());
        if guard.bus.is_some() {
            return BusResult::Ok;
        }
        let mut config = BusConfig { poll_hz: if poll_hz == 0 { 250 } else { poll_hz }, ..Default::default() };
        #[cfg(windows)]
        let mut factories: Vec<SourceFactory> = vec![crate::directinput::DirectInputSource::factory(config.rescan_interval)];
        #[cfg(not(windows))]
        let mut factories: Vec<SourceFactory> = Vec::new();
        if let Some(agent) = guard.agent.as_mut().filter(|agent| agent.enabled) {
            agent.handle.enable_lease(agent.lease_duration);
            factories.push(agent.handle.source_factory());
            config.poll_hz = config.poll_hz.max(100);
        }
        match Bus::start(config, factories) {
            Ok(bus) => {
                bus.wait_ready(Duration::from_secs(2));
                if let Some(agent) = guard.agent.as_mut() {
                    agent.attached = agent.enabled;
                    if agent.enabled {
                        // DirectInput's first enumeration may outlast a short pre-start lease.
                        agent.handle.enable_lease(agent.lease_duration);
                    }
                }
                guard.bus = Some(bus);
                BusResult::Ok
            }
            Err(_) => BusResult::NotReady,
        }
    })
}

/// Stops and joins the poll thread. Safe to call when not running.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_Stop() {
    guarded((), || {
        let mut guard = BUS.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(agent) = guard.agent.take() {
            agent.handle.disconnect();
        }
        if let Some(bus) = guard.bus.take() {
            bus.stop();
        }
    })
}

/// Dev-only opt-in, separate from ABI v1's read-only table. May precede or follow Start.
/// `enabled` is 0 or 1; an enabled lease is 1..500 ms (native expiry caps it at 450 ms to
/// reserve publication margin). Re-enabling always starts neutral. Disable neutralizes and
/// disconnects; its descriptor remains until Stop. Stop clears the opt-in entirely.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentEnable(enabled: u32, lease_ms: u32) -> BusResult {
    guarded(BusResult::NotReady, || {
        if enabled > 1 || (enabled == 1 && !(1..=500).contains(&lease_ms)) {
            return BusResult::InvalidArgument;
        }
        let mut host = BUS.lock().unwrap_or_else(|p| p.into_inner());
        if enabled == 0 {
            if let Some(agent) = host.agent.as_mut() {
                agent.enabled = false;
                agent.handle.disconnect();
            }
            return BusResult::Ok;
        }
        if host.agent.is_none() {
            let (_, handle) = match VirtualSource::new("agent-0") {
                Ok(source) => source,
                Err(_) => return BusResult::NotReady,
            };
            host.agent = Some(AgentState {
                handle,
                lease_duration: Duration::from_millis(lease_ms as u64),
                enabled: false,
                attached: false,
            });
        }
        let running = host.bus.is_some();
        let agent = host.agent.as_mut().unwrap();
        agent.lease_duration = Duration::from_millis(lease_ms as u64);
        agent.enabled = true;
        agent.handle.enable_lease(agent.lease_duration);
        let factory = if running && !agent.attached {
            agent.attached = true;
            Some(agent.handle.source_factory())
        } else {
            None
        };
        if let Some(factory) = factory {
            host.bus.as_ref().unwrap().add_source(factory, 100);
        }
        BusResult::Ok
    })
}

fn agent_result(ok: bool) -> BusResult {
    if ok { BusResult::Ok } else { BusResult::NotReady }
}

fn with_agent(f: impl FnOnce(&VirtualHandle) -> bool) -> BusResult {
    let host = BUS.lock().unwrap_or_else(|p| p.into_inner());
    agent_result(host.agent.as_ref().is_some_and(|agent| agent.enabled && f(&agent.handle)))
}

/// Renews a live lease. An expired/disconnected source must be explicitly enabled again.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentHeartbeat() -> BusResult {
    guarded(BusResult::NotReady, || with_agent(VirtualHandle::heartbeat))
}

/// HID usage index 0..7, finite bipolar value in [-1, 1]. Setters do not renew the lease.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentSetAxis(axis: u32, value: f32) -> BusResult {
    guarded(BusResult::NotReady, || {
        if axis >= AXIS_COUNT as u32 || !value.is_finite() || !(-1.0..=1.0).contains(&value) {
            return BusResult::InvalidArgument;
        }
        with_agent(|handle| handle.update_leased(|state| state.axes[axis as usize] = raw_from_normalized(value)))
    })
}

/// Buttons 1..128, `down` is 0 or 1.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentSetButton(button: u32, down: u32) -> BusResult {
    guarded(BusResult::NotReady, || {
        if !(1..=BUTTON_COUNT as u32).contains(&button) || down > 1 {
            return BusResult::InvalidArgument;
        }
        with_agent(|handle| handle.update_leased(|state| state.buttons[button as usize - 1] = down != 0))
    })
}

/// POV 0..3, direction -1=centered, 0=up, 1=right, 2=down, 3=left.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentSetPov(pov: u32, direction: i32) -> BusResult {
    guarded(BusResult::NotReady, || {
        if pov >= POV_COUNT as u32 || !(-1..=3).contains(&direction) {
            return BusResult::InvalidArgument;
        }
        let raw = if direction < 0 { POV_CENTERED } else { direction as u32 * 9000 };
        with_agent(|handle| handle.update_leased(|state| state.povs[pov as usize] = raw))
    })
}

/// Releases all controls without renewing the lease. Also succeeds after lease expiry.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentNeutral() -> BusResult {
    guarded(BusResult::NotReady, || {
        let host = BUS.lock().unwrap_or_else(|p| p.into_inner());
        match host.agent.as_ref() {
            Some(agent) => { agent.handle.neutral(); BusResult::Ok }
            None => BusResult::NotReady,
        }
    })
}

/// Immediately neutralizes and disconnects. Heartbeats cannot reconnect it.
#[no_mangle]
pub extern "C" fn AbsoluteInputBus_AgentDisconnect() -> BusResult {
    guarded(BusResult::NotReady, || {
        let mut host = BUS.lock().unwrap_or_else(|p| p.into_inner());
        match host.agent.as_mut() {
            Some(agent) => {
                agent.enabled = false;
                agent.handle.disconnect();
                BusResult::Ok
            }
            None => BusResult::NotReady,
        }
    })
}

/// Release mod code swap (T100; `codepatch`). Independent of Start/Stop. Returns 0 patched, 1 already in
/// place, negative on refusal (`codepatch` codes); `threads_out` may be null.
#[cfg(windows)]
#[no_mangle]
pub unsafe extern "C" fn AbsoluteInputBus_PatchCode(site: usize, expected: *const u8, replacement: *const u8, len: u32,
                                                    threads_out: *mut u32) -> i32 {
    guarded(crate::codepatch::PANIC, || {
        if expected.is_null() || replacement.is_null() || len == 0 || len > 16 {
            return crate::codepatch::BAD_ARGUMENTS;
        }
        let expected = std::slice::from_raw_parts(expected, len as usize);
        let replacement = std::slice::from_raw_parts(replacement, len as usize);
        crate::codepatch::patch_code(site, expected, replacement, threads_out.as_mut())
    })
}

unsafe extern "C" fn get_device_count() -> u32 {
    guarded(0, || with_bus(0, |bus| bus.with_frame(|f| f.devices.len() as u32)))
}

unsafe extern "C" fn get_device(index: u32, output: *mut DeviceInfoV1) -> BusResult {
    guarded(BusResult::NotReady, || {
        if output.is_null() || (*output).struct_size < std::mem::size_of::<DeviceInfoV1>() as u32 {
            return BusResult::InvalidArgument;
        }
        with_bus(BusResult::NotReady, |bus| {
            bus.with_frame(|f| match f.devices.get(index as usize) {
                Some(info) => {
                    *output = *info;
                    BusResult::Ok
                }
                None => BusResult::NotFound,
            })
        })
    })
}

unsafe extern "C" fn get_snapshot(index: u32, output: *mut DeviceSnapshotV1) -> BusResult {
    guarded(BusResult::NotReady, || {
        if output.is_null() || (*output).struct_size < std::mem::size_of::<DeviceSnapshotV1>() as u32 {
            return BusResult::InvalidArgument;
        }
        with_bus(BusResult::NotReady, |bus| {
            bus.with_frame(|f| match f.snapshots.get(index as usize) {
                Some(snapshot) => {
                    *output = *snapshot;
                    BusResult::Ok
                }
                None => BusResult::NotFound,
            })
        })
    })
}

unsafe extern "C" fn get_profile_state(output: *mut ProfileStateV1) -> BusResult {
    guarded(BusResult::NotReady, || {
        if output.is_null() || (*output).struct_size < std::mem::size_of::<ProfileStateV1>() as u32 {
            return BusResult::InvalidArgument;
        }
        with_bus(BusResult::NotReady, |_| {
            let mut state = ProfileStateV1::new();
            state.active_slot = 0;
            state.generation = 1;
            copy_str(&mut state.profile_id, "base");
            *output = state;
            BusResult::Ok
        })
    })
}

/// Publishes `Unknown` with no valid signals until the context service (T41) exists.
unsafe extern "C" fn get_runtime_context(output: *mut RuntimeContextV1) -> BusResult {
    guarded(BusResult::NotReady, || {
        if output.is_null() || (*output).struct_size < std::mem::size_of::<RuntimeContextV1>() as u32 {
            return BusResult::InvalidArgument;
        }
        with_bus(BusResult::NotReady, |bus| {
            let mut context = RuntimeContextV1::new();
            bus.with_frame(|f| {
                context.sequence = f.sequence;
                context.producer_generation = f.producer_generation;
            });
            context.context = RuntimeContext::Unknown;
            context.context_generation = 1;
            *output = context;
            BusResult::Ok
        })
    })
}

unsafe extern "C" fn begin_capture(_request: *const CaptureRequestV1, _session: *mut u64) -> BusResult {
    BusResult::NotReady
}

unsafe extern "C" fn poll_capture(_session: u64, _output: *mut CaptureResultV1) -> BusResult {
    BusResult::NotReady
}

unsafe extern "C" fn cancel_capture(_session: u64) -> BusResult {
    BusResult::NotReady
}

#[cfg(test)]
mod tests {
    use super::*;

    static HOST_TEST: Mutex<()> = Mutex::new(());

    fn agent_snapshot() -> Option<DeviceSnapshotV1> {
        with_bus(None, |bus| bus.with_frame(|frame| frame.index_of("virtual:agent-0").map(|i| frame.snapshots[i])))
    }

    fn wait_agent(predicate: impl Fn(DeviceSnapshotV1) -> bool) -> DeviceSnapshotV1 {
        let start = std::time::Instant::now();
        loop {
            if let Some(snapshot) = agent_snapshot().filter(|snapshot| predicate(*snapshot)) {
                return snapshot;
            }
            assert!(start.elapsed() < Duration::from_secs(2), "virtual source state did not publish");
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn query_validates_version_and_table() {
        assert!(AbsoluteInputBus_QueryApi(2).is_null());
        let api = unsafe { &*AbsoluteInputBus_QueryApi(1) };
        assert_eq!(api.abi_version, 1);
        assert_eq!(api.struct_size as usize, std::mem::size_of::<ApiV1>());
        assert_eq!(api.capabilities & (CAPABILITY_BUTTON_CAPTURE | CAPABILITY_AXIS_CAPTURE), 0);
        let provider = unsafe { std::ffi::CStr::from_ptr(api.provider_id) };
        assert_eq!(provider.to_str().unwrap(), PROVIDER_ID);
        assert!(api.get_device_count.is_some() && api.cancel_capture.is_some());
    }

    #[test]
    fn stopped_bus_is_not_ready_and_bad_args_rejected() {
        let _serial = HOST_TEST.lock().unwrap();
        AbsoluteInputBus_Stop();
        let api = unsafe { &*AbsoluteInputBus_QueryApi(1) };
        unsafe {
            assert_eq!(api.get_device_count.unwrap()(), 0);
            let mut info = DeviceInfoV1::new();
            assert_eq!(api.get_device.unwrap()(0, &mut info), BusResult::NotReady);
            info.struct_size = 4;
            assert_eq!(api.get_device.unwrap()(0, &mut info), BusResult::InvalidArgument);
            assert_eq!(api.get_snapshot.unwrap()(0, std::ptr::null_mut()), BusResult::InvalidArgument);
            let mut session = 0u64;
            assert_eq!(api.begin_capture.unwrap()(std::ptr::null(), &mut session), BusResult::NotReady);
        }
    }

    #[test]
    fn agent_source_is_opt_in_reused_after_disable_and_cleared_on_stop() {
        let _serial = HOST_TEST.lock().unwrap();
        AbsoluteInputBus_Stop();
        assert_eq!(AbsoluteInputBus_AgentHeartbeat(), BusResult::NotReady);
        assert_eq!(AbsoluteInputBus_AgentSetAxis(0, 0.5), BusResult::NotReady);
        assert_eq!(AbsoluteInputBus_Start(250), BusResult::Ok);
        assert!(agent_snapshot().is_none(), "ordinary bus start exposed a synthetic source");
        assert_eq!(AbsoluteInputBus_AgentEnable(1, 500), BusResult::Ok);
        wait_agent(|snapshot| snapshot.connected != 0);
        assert_eq!(AbsoluteInputBus_AgentSetAxis(7, -0.5), BusResult::Ok);
        assert_eq!(AbsoluteInputBus_AgentSetButton(128, 1), BusResult::Ok);
        assert_eq!(AbsoluteInputBus_AgentSetPov(3, 2), BusResult::Ok);
        let active = wait_agent(|snapshot| snapshot.is_down(127));
        assert!((active.normalized_axes[7] + 0.5).abs() < 1e-3);
        assert_eq!(active.pov_hundredths[3], 18000);
        assert_eq!(AbsoluteInputBus_AgentNeutral(), BusResult::Ok);
        wait_agent(|snapshot| !snapshot.is_down(127) && snapshot.pov_hundredths[3] == -1);
        assert_eq!(AbsoluteInputBus_AgentEnable(0, 0), BusResult::Ok);
        wait_agent(|snapshot| snapshot.connected == 0);
        assert_eq!(AbsoluteInputBus_AgentHeartbeat(), BusResult::NotReady);
        assert_eq!(AbsoluteInputBus_AgentEnable(1, 500), BusResult::Ok);
        wait_agent(|snapshot| snapshot.connected != 0);
        let count = with_bus(0, |bus| bus.with_frame(|frame| frame.devices.iter()
            .filter(|info| read_str(&info.persistent_id) == "virtual:agent-0").count()));
        assert_eq!(count, 1);
        assert_eq!(AbsoluteInputBus_AgentDisconnect(), BusResult::Ok);
        wait_agent(|snapshot| snapshot.connected == 0);
        assert_eq!(AbsoluteInputBus_AgentSetButton(1, 1), BusResult::NotReady);
        AbsoluteInputBus_Stop();
        assert_eq!(AbsoluteInputBus_Start(250), BusResult::Ok);
        assert!(agent_snapshot().is_none(), "Stop retained the dev opt-in");
        AbsoluteInputBus_Stop();
    }

    #[test]
    fn pre_start_enable_lease_expiry_and_argument_validation() {
        let _serial = HOST_TEST.lock().unwrap();
        AbsoluteInputBus_Stop();
        for (enabled, lease_ms) in [(2, 100), (1, 0), (1, 501)] {
            assert_eq!(AbsoluteInputBus_AgentEnable(enabled, lease_ms), BusResult::InvalidArgument);
        }
        for (axis, value) in [(8, 0.0), (0, 1.1), (0, f32::NAN), (0, f32::INFINITY)] {
            assert_eq!(AbsoluteInputBus_AgentSetAxis(axis, value), BusResult::InvalidArgument);
        }
        for (button, down) in [(0, 1), (129, 0), (1, 2)] {
            assert_eq!(AbsoluteInputBus_AgentSetButton(button, down), BusResult::InvalidArgument);
        }
        for (pov, direction) in [(4, 0), (0, -2), (0, 4)] {
            assert_eq!(AbsoluteInputBus_AgentSetPov(pov, direction), BusResult::InvalidArgument);
        }
        assert_eq!(AbsoluteInputBus_AgentEnable(1, 100), BusResult::Ok);
        assert_eq!(AbsoluteInputBus_Start(1), BusResult::Ok);
        wait_agent(|snapshot| snapshot.connected != 0);
        assert_eq!(AbsoluteInputBus_AgentSetButton(1, 1), BusResult::Ok);
        wait_agent(|snapshot| snapshot.is_down(0));
        assert_eq!(AbsoluteInputBus_AgentHeartbeat(), BusResult::Ok);
        // No host calls during silence: expiry must come from the native poll thread.
        std::thread::sleep(Duration::from_millis(250));
        let expired = agent_snapshot().unwrap();
        assert_eq!(expired.connected, 0);
        assert_eq!(expired.digital_down, [0; 3]);
        assert_eq!(expired.normalized_axes, [0.0; 8]);
        assert_eq!(AbsoluteInputBus_AgentHeartbeat(), BusResult::NotReady);
        assert_eq!(AbsoluteInputBus_AgentSetButton(1, 1), BusResult::NotReady);
        assert_eq!(AbsoluteInputBus_AgentNeutral(), BusResult::Ok);
        AbsoluteInputBus_Stop();
    }
}
