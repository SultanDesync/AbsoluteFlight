//! DirectInput source. Seeded from AbsoluteHOTAS-Configurator `src-tauri/src/direct_input/devices.rs`
//! (same author, MIT): `DirectInput8Create`, `c_dfDIJoystick2`, `DIPROP_RANGE` 0..65535 by HID usage,
//! the fixed 0x30..0x37 axis order. Changes for the bus: an explicit cooperative level, capability
//! counts and VID/PID, no sleeping on the poll path, and enumeration on its own thread.
//!
//! Threading: the poll-thread `IDirectInput8A` and every device live on the bus poll thread.
//! Re-enumeration runs on a helper thread with its own `IDirectInput8A` and sends plain data back,
//! so no COM object crosses threads.

use std::ffi::c_void;
use std::mem::size_of;
use std::ptr::null_mut;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use windows::core::{Interface, BOOL, GUID};
use windows::Win32::Devices::HumanInterfaceDevice::{
    DirectInput8Create, IDirectInput8A, IDirectInputDevice8A, DI8DEVCLASS_GAMECTRL, DIDATAFORMAT, DIDEVCAPS,
    DIDEVICEINSTANCEA, DIEDFL_ATTACHEDONLY, DIJOYSTATE2, DIPH_BYUSAGE, DIPROPHEADER, DIPROPRANGE, DIPROP_RANGE,
    DISCL_BACKGROUND, DISCL_NONEXCLUSIVE,
};
use windows::Win32::Foundation::{HINSTANCE, HWND};
use windows::Win32::System::LibraryLoader::GetModuleHandleA;

use crate::abi::{AXIS_COUNT, BUTTON_COUNT, POV_COUNT};
use crate::source::{DeviceDescriptor, InputSource, RawState, AXIS_RAW_MAX, AXIS_RAW_MIN};
use crate::token::format_guid_bytes;

const DI_VERSION: u32 = 0x0800;
const DIENUM_CONTINUE: BOOL = BOOL(1);

extern "C" {
    static c_dfDIJoystick2: DIDATAFORMAT;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnumeratedDevice {
    pub instance_guid: [u8; 16],
    pub product_guid: [u8; 16],
    pub instance_name: String,
    pub product_name: String,
}

pub fn guid_bytes(guid: &GUID) -> [u8; 16] {
    let mut b = [0u8; 16];
    b[0..4].copy_from_slice(&guid.data1.to_le_bytes());
    b[4..6].copy_from_slice(&guid.data2.to_le_bytes());
    b[6..8].copy_from_slice(&guid.data3.to_le_bytes());
    b[8..16].copy_from_slice(&guid.data4);
    b
}

fn guid_from_bytes(b: &[u8; 16]) -> GUID {
    GUID {
        data1: u32::from_le_bytes([b[0], b[1], b[2], b[3]]),
        data2: u16::from_le_bytes([b[4], b[5]]),
        data3: u16::from_le_bytes([b[6], b[7]]),
        data4: [b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]],
    }
}

fn c_chars(chars: &[i8]) -> String {
    let bytes: Vec<u8> = chars.iter().take_while(|c| **c != 0).map(|c| *c as u8).collect();
    String::from_utf8_lossy(&bytes).trim().to_string()
}

unsafe fn create_direct_input() -> Result<IDirectInput8A, String> {
    let mut raw: *mut c_void = null_mut();
    let module = GetModuleHandleA(None).map_err(|e| format!("GetModuleHandleA failed: {e}"))?;
    DirectInput8Create(HINSTANCE(module.0), DI_VERSION, &IDirectInput8A::IID, &mut raw, None)
        .map_err(|e| format!("DirectInput8Create failed: {e}"))?;
    if raw.is_null() {
        return Err("DirectInput8Create returned no interface".into());
    }
    Ok(IDirectInput8A::from_raw(raw))
}

unsafe extern "system" fn enum_callback(instance: *mut DIDEVICEINSTANCEA, context: *mut c_void) -> BOOL {
    let list = &mut *(context as *mut Vec<EnumeratedDevice>);
    let instance = &*instance;
    list.push(EnumeratedDevice {
        instance_guid: guid_bytes(&instance.guidInstance),
        product_guid: guid_bytes(&instance.guidProduct),
        instance_name: c_chars(&instance.tszInstanceName),
        product_name: c_chars(&instance.tszProductName),
    });
    DIENUM_CONTINUE
}

fn enumerate_with(direct_input: &IDirectInput8A) -> Result<Vec<EnumeratedDevice>, String> {
    let mut list: Vec<EnumeratedDevice> = Vec::new();
    unsafe {
        direct_input
            .EnumDevices(
                DI8DEVCLASS_GAMECTRL,
                Some(enum_callback),
                &mut list as *mut _ as *mut c_void,
                DIEDFL_ATTACHEDONLY,
            )
            .map_err(|e| format!("EnumDevices failed: {e}"))?;
    }
    Ok(list)
}

/// One-shot enumeration (attached game controllers), for tools.
pub fn enumerate() -> Result<Vec<EnumeratedDevice>, String> {
    let direct_input = unsafe { create_direct_input()? };
    enumerate_with(&direct_input)
}

/// After every Acquire, DirectInput can return a placeholder state (all axes 32767, nothing pressed)
/// for 0–30 ms until the device's first report arrives (measured on the user's VKB/S-TECS/vJoy set,
/// 2026-10-15). Publishing that as real data turned a button already held at rest into a synthetic press.
/// So a freshly acquired device stays "not readable" until its state changes from the first read, or
/// this long has passed with no change (then the first read was already real).
const ACQUIRE_SETTLE: Duration = Duration::from_millis(250);

struct OpenDevice {
    device: IDirectInputDevice8A,
    acquired: bool,
    settle_until: Option<Instant>,
    settle_reference: Option<RawState>,
}

impl Drop for OpenDevice {
    fn drop(&mut self) {
        unsafe {
            let _ = self.device.Unacquire();
        }
    }
}

impl OpenDevice {
    unsafe fn open(direct_input: &IDirectInput8A, found: &EnumeratedDevice) -> Result<(Self, DeviceDescriptor), String> {
        let guid = guid_from_bytes(&found.instance_guid);
        let mut device = None;
        direct_input.CreateDevice(&guid, &mut device, None).map_err(|e| format!("CreateDevice failed: {e}"))?;
        let device = device.ok_or("CreateDevice returned no device")?;
        device
            .SetDataFormat(&c_dfDIJoystick2 as *const DIDATAFORMAT as *mut DIDATAFORMAT)
            .map_err(|e| format!("SetDataFormat failed: {e}"))?;

        // Explicit, not assumed: the in-game host must keep reading while NMS has focus and the
        // standalone host while it doesn't. A null window is tried first; the result is reported.
        let coop = match device.SetCooperativeLevel(HWND(null_mut()), DISCL_NONEXCLUSIVE | DISCL_BACKGROUND) {
            Ok(()) => "coop=nonexclusive|background ok".to_string(),
            Err(e) => format!("coop=FAILED {:#010x} (DirectInput default level in effect)", e.code().0 as u32),
        };

        for usage in 0x30u32..=0x37 {
            let mut range = DIPROPRANGE {
                diph: DIPROPHEADER {
                    dwSize: size_of::<DIPROPRANGE>() as u32,
                    dwHeaderSize: size_of::<DIPROPHEADER>() as u32,
                    dwObj: usage,
                    dwHow: DIPH_BYUSAGE,
                },
                lMin: AXIS_RAW_MIN,
                lMax: AXIS_RAW_MAX,
            };
            let _ = device.SetProperty(&DIPROP_RANGE, &mut range.diph);
        }

        let mut caps = DIDEVCAPS { dwSize: size_of::<DIDEVCAPS>() as u32, ..Default::default() };
        let caps_note = match device.GetCapabilities(&mut caps) {
            Ok(()) => format!("caps axes={} buttons={} povs={}", caps.dwAxes, caps.dwButtons, caps.dwPOVs),
            Err(e) => format!("caps FAILED {:#010x}", e.code().0 as u32),
        };
        let mut notes = format!("{coop}; {caps_note}");
        if caps.dwAxes as usize > AXIS_COUNT || caps.dwButtons as usize > BUTTON_COUNT || caps.dwPOVs as usize > POV_COUNT {
            notes.push_str("; EXCEEDS v1 layout (extra controls not published)");
        }

        // VID/PID live in the product GUID's Data1 for HID devices (as AbsoluteHOTAS DeviceManager reads them).
        let data1 = u32::from_le_bytes([found.product_guid[0], found.product_guid[1], found.product_guid[2], found.product_guid[3]]);
        let descriptor = DeviceDescriptor {
            persistent_id: format_guid_bytes(&found.instance_guid),
            instance_guid: found.instance_guid,
            product_guid: found.product_guid,
            vendor_id: (data1 & 0xFFFF) as u16,
            product_id: (data1 >> 16) as u16,
            axis_count: caps.dwAxes,
            button_count: caps.dwButtons,
            pov_count: caps.dwPOVs,
            instance_name: found.instance_name.clone(),
            product_name: found.product_name.clone(),
            notes,
        };
        let mut open = OpenDevice { device, acquired: false, settle_until: None, settle_reference: None };
        open.acquire();
        Ok((open, descriptor))
    }

    unsafe fn acquire(&mut self) {
        let was = self.acquired;
        self.acquired = self.device.Acquire().is_ok();
        if self.acquired && !was {
            self.settle_until = Some(Instant::now() + ACQUIRE_SETTLE);
            self.settle_reference = None;
        }
    }

    /// Never sleeps: one Poll, and on failure one Acquire + Poll. `None` = unreadable/disconnected.
    unsafe fn read(&mut self) -> Option<RawState> {
        if self.device.Poll().is_err() {
            self.acquired = false;
            self.acquire();
            if !self.acquired || self.device.Poll().is_err() {
                return None;
            }
        }
        let mut state = DIJOYSTATE2::default();
        self.device
            .GetDeviceState(size_of::<DIJOYSTATE2>() as u32, &mut state as *mut _ as *mut c_void)
            .ok()?;
        let mut buttons = [false; BUTTON_COUNT];
        for (out, raw) in buttons.iter_mut().zip(state.rgbButtons.iter()) {
            *out = raw & 0x80 != 0;
        }
        let raw = RawState {
            axes: [state.lX, state.lY, state.lZ, state.lRx, state.lRy, state.lRz, state.rglSlider[0], state.rglSlider[1]],
            povs: state.rgdwPOV,
            buttons,
        };
        if let Some(deadline) = self.settle_until {
            match self.settle_reference {
                None => {
                    self.settle_reference = Some(raw);
                    return None;
                }
                Some(reference) if reference == raw && Instant::now() < deadline => return None,
                Some(_) => {
                    self.settle_until = None;
                    self.settle_reference = None;
                }
            }
        }
        Some(raw)
    }
}

pub struct DirectInputSource {
    direct_input: IDirectInput8A,
    /// Parallel to `descriptors`.
    open: Vec<(EnumeratedDevice, OpenDevice)>,
    descriptors: Vec<DeviceDescriptor>,
    last_enumerated: Vec<[u8; 16]>,
    /// Errors from opening devices, kept for diagnostics.
    pub open_errors: Vec<String>,
    updates: Receiver<Vec<EnumeratedDevice>>,
    stop: Arc<AtomicBool>,
    enumerator: Option<JoinHandle<()>>,
}

impl DirectInputSource {
    /// Enumerates once synchronously (so devices exist from the first poll), then starts the
    /// background enumerator. Must be called on the thread that will poll.
    pub fn new(rescan_interval: Duration) -> Result<Self, String> {
        let direct_input = unsafe { create_direct_input()? };
        let initial = enumerate_with(&direct_input)?;
        let (sender, updates) = channel();
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = stop.clone();
        let enumerator = std::thread::Builder::new()
            .name("absolute-input-bus-enum".into())
            .spawn(move || {
                let Ok(direct_input) = (unsafe { create_direct_input() }) else { return };
                let step = Duration::from_millis(50);
                let mut waited = Duration::ZERO;
                while !thread_stop.load(Ordering::Acquire) {
                    std::thread::sleep(step);
                    waited += step;
                    if waited < rescan_interval {
                        continue;
                    }
                    waited = Duration::ZERO;
                    if let Ok(list) = enumerate_with(&direct_input) {
                        if sender.send(list).is_err() {
                            return;
                        }
                    }
                }
            })
            .map_err(|e| format!("enumerator thread: {e}"))?;
        let mut source = Self {
            direct_input,
            open: Vec::new(),
            descriptors: Vec::new(),
            last_enumerated: Vec::new(),
            open_errors: Vec::new(),
            updates,
            stop,
            enumerator: Some(enumerator),
        };
        source.last_enumerated = initial.iter().map(|e| e.instance_guid).collect();
        source.last_enumerated.sort_unstable();
        source.apply(initial);
        Ok(source)
    }

    /// Factory for `Bus::start`; construction happens on the poll thread.
    pub fn factory(rescan_interval: Duration) -> crate::source::SourceFactory {
        Box::new(move || match DirectInputSource::new(rescan_interval) {
            Ok(source) => Box::new(source) as Box<dyn InputSource>,
            Err(error) => Box::new(FailedSource { error, devices: Vec::new() }),
        })
    }

    /// Opens newly seen devices and keeps already-open ones (with their descriptors) by instance GUID.
    fn apply(&mut self, list: Vec<EnumeratedDevice>) {
        let mut previous: Vec<(EnumeratedDevice, OpenDevice, DeviceDescriptor)> =
            std::mem::take(&mut self.open).into_iter().zip(std::mem::take(&mut self.descriptors)).map(|((e, d), desc)| (e, d, desc)).collect();
        self.open_errors.clear();
        for found in list {
            if let Some(position) = previous.iter().position(|(e, _, _)| e.instance_guid == found.instance_guid) {
                let (_, device, descriptor) = previous.swap_remove(position);
                self.open.push((found, device));
                self.descriptors.push(descriptor);
                continue;
            }
            match unsafe { OpenDevice::open(&self.direct_input, &found) } {
                Ok((device, descriptor)) => {
                    self.open.push((found, device));
                    self.descriptors.push(descriptor);
                }
                Err(error) => {
                    self.open_errors.push(format!("{} ({}): {error}", found.instance_name, format_guid_bytes(&found.instance_guid)));
                }
            }
        }
    }
}

impl Drop for DirectInputSource {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.open.clear();
        if let Some(thread) = self.enumerator.take() {
            let _ = thread.join();
        }
    }
}

impl InputSource for DirectInputSource {
    fn name(&self) -> &str {
        "directinput"
    }

    fn maintain(&mut self) -> bool {
        let mut latest = None;
        while let Ok(list) = self.updates.try_recv() {
            latest = Some(list);
        }
        let Some(list) = latest else { return false };
        // Compare against the last *enumerated* set, not the opened set, so a device that fails to
        // open doesn't force a rebuild (and a generation bump) on every rescan.
        let mut found: Vec<[u8; 16]> = list.iter().map(|e| e.instance_guid).collect();
        found.sort_unstable();
        if found == self.last_enumerated {
            return false;
        }
        self.last_enumerated = found;
        let before: Vec<String> = self.descriptors.iter().map(|d| d.persistent_id.clone()).collect();
        self.apply(list);
        before != self.descriptors.iter().map(|d| d.persistent_id.clone()).collect::<Vec<_>>()
    }

    fn devices(&self) -> &[DeviceDescriptor] {
        &self.descriptors
    }

    fn poll(&mut self, index: usize) -> Option<RawState> {
        let (_, device) = self.open.get_mut(index)?;
        unsafe { device.read() }
    }
}

/// Stand-in when DirectInput can't be created, so the bus still runs (virtual sources keep working).
struct FailedSource {
    #[allow(dead_code)]
    error: String,
    devices: Vec<DeviceDescriptor>,
}

impl InputSource for FailedSource {
    fn name(&self) -> &str {
        "directinput(failed)"
    }
    fn maintain(&mut self) -> bool {
        false
    }
    fn devices(&self) -> &[DeviceDescriptor] {
        &self.devices
    }
    fn poll(&mut self, _index: usize) -> Option<RawState> {
        None
    }
}
