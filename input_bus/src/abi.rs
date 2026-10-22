//! `#[repr(C)]` mirrors of AbsoluteHOTAS `include/AbsoluteInputBusAPI.h` (ABI v1).
//!
//! Layout is enforced by `tests/layout.rs` against `tests/layout_v1_msvc.txt`, which was produced by
//! compiling `tests/layout_probe.cpp` against the C++ header with MSVC x64. Enums that cross the boundary
//! are `u32` newtypes, so a value we don't know can never be undefined behaviour on our side.

use std::ffi::c_char;

pub const ABI_VERSION: u32 = 1;
pub const AXIS_COUNT: usize = 8;
pub const BUTTON_COUNT: usize = 128;
pub const POV_COUNT: usize = 4;
pub const POV_DIRECTION_COUNT: usize = 16;
pub const DIGITAL_CONTROL_COUNT: usize = BUTTON_COUNT + POV_DIRECTION_COUNT;
pub const DIGITAL_WORD_COUNT: usize = 3;
pub const CONSUMER_ID_CAPACITY: usize = 64;
pub const DEVICE_ID_CAPACITY: usize = 48;
pub const DEVICE_NAME_CAPACITY: usize = 128;
pub const BINDING_TEXT_CAPACITY: usize = 256;
pub const PROFILE_ID_CAPACITY: usize = 96;
pub const DETAIL_CAPACITY: usize = 192;

pub const CAPABILITY_DEVICE_ENUMERATION: u64 = 1 << 0;
pub const CAPABILITY_SNAPSHOTS: u64 = 1 << 1;
pub const CAPABILITY_BUTTON_CAPTURE: u64 = 1 << 2;
pub const CAPABILITY_AXIS_CAPTURE: u64 = 1 << 3;
pub const CAPABILITY_PROFILE_IDENTITY: u64 = 1 << 4;
pub const CAPABILITY_RUNTIME_CONTEXT: u64 = 1 << 5;
pub const CAPABILITIES_V1: u64 = CAPABILITY_DEVICE_ENUMERATION
    | CAPABILITY_SNAPSHOTS
    | CAPABILITY_BUTTON_CAPTURE
    | CAPABILITY_AXIS_CAPTURE
    | CAPABILITY_PROFILE_IDENTITY
    | CAPABILITY_RUNTIME_CONTEXT;

pub const CAPTURE_BUTTONS: u32 = 1 << 0;
pub const CAPTURE_POV_DIRECTIONS: u32 = 1 << 1;
pub const CAPTURE_AXES: u32 = 1 << 2;
pub const CAPTURE_DIGITAL: u32 = CAPTURE_BUTTONS | CAPTURE_POV_DIRECTIONS;
pub const CAPTURE_ALL: u32 = CAPTURE_DIGITAL | CAPTURE_AXES;

pub const CONTEXT_SIGNAL_IS_PILOT: u32 = 1 << 0;
pub const CONTEXT_SIGNAL_GAMEPLAY_ACTIVE: u32 = 1 << 1;
pub const CONTEXT_SIGNAL_TARGETING_MODE: u32 = 1 << 2;
pub const CONTEXT_SOURCE_AUTOMATIC_PILOT: u32 = 1 << 0;

macro_rules! u32_enum {
    ($name:ident { $($variant:ident = $value:expr),* $(,)? }) => {
        #[repr(transparent)]
        #[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
        pub struct $name(pub u32);
        #[allow(non_upper_case_globals)]
        impl $name { $(pub const $variant: Self = Self($value);)* }
    };
}

u32_enum!(BusResult { Ok = 0, NotReady = 1, InvalidArgument = 2, NotFound = 3, Busy = 4, StaleSession = 5 });
u32_enum!(ControlKind { None = 0, Button = 1, PovDirection = 2, Axis = 3 });
u32_enum!(CaptureState { Idle = 0, Capturing = 1, Captured = 2, Cancelled = 3, TimedOut = 4, Error = 5 });
u32_enum!(RuntimeContext { Unknown = 0, Suspended = 1, OnFoot = 2, Piloting = 3 });

/// Marker for plain-old-data ABI structs whose all-zero bit pattern is valid.
///
/// # Safety
/// Implementors must contain only integers, floats, arrays of those, nested `Pod` types,
/// raw pointers or `Option<fn>` (all of which are valid when zeroed).
pub unsafe trait Pod: Copy + Sized {
    fn zeroed() -> Self {
        // SAFETY: guaranteed by the trait contract.
        unsafe { std::mem::zeroed() }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct DeviceInfoV1 {
    pub struct_size: u32,
    pub device_index: u32,
    pub instance_guid: [u8; 16],
    pub product_guid: [u8; 16],
    pub vendor_id: u16,
    pub product_id: u16,
    pub axis_count: u32,
    pub button_count: u32,
    pub pov_count: u32,
    pub persistent_id: [c_char; DEVICE_ID_CAPACITY],
    pub instance_name: [c_char; DEVICE_NAME_CAPACITY],
    pub product_name: [c_char; DEVICE_NAME_CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct DeviceSnapshotV1 {
    pub struct_size: u32,
    pub device_index: u32,
    pub sequence: u64,
    pub producer_generation: u64,
    pub connected: u8,
    pub reserved: [u8; 7],
    pub raw_axes: [i32; AXIS_COUNT],
    pub axis_minimum: [i32; AXIS_COUNT],
    pub axis_maximum: [i32; AXIS_COUNT],
    pub normalized_axes: [f32; AXIS_COUNT],
    pub pov_hundredths: [i32; POV_COUNT],
    pub digital_down: [u64; DIGITAL_WORD_COUNT],
    pub press_count: [u32; DIGITAL_CONTROL_COUNT],
    pub release_count: [u32; DIGITAL_CONTROL_COUNT],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct BindingV1 {
    pub struct_size: u32,
    pub kind: ControlKind,
    pub device_index: u32,
    pub control_id: u32,
    pub instance_guid: [u8; 16],
    pub persistent_id: [c_char; DEVICE_ID_CAPACITY],
    pub product_name: [c_char; DEVICE_NAME_CAPACITY],
    pub binding_text: [c_char; BINDING_TEXT_CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CaptureRequestV1 {
    pub struct_size: u32,
    pub allowed_controls: u32,
    pub settle_milliseconds: u32,
    pub timeout_milliseconds: u32,
    pub consumer_id: [c_char; CONSUMER_ID_CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CaptureResultV1 {
    pub struct_size: u32,
    pub state: CaptureState,
    pub session_id: u64,
    pub binding: BindingV1,
    pub detail: [c_char; DETAIL_CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct ProfileStateV1 {
    pub struct_size: u32,
    pub active_slot: u32,
    pub generation: u64,
    pub profile_id: [c_char; PROFILE_ID_CAPACITY],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct RuntimeContextV1 {
    pub struct_size: u32,
    pub context: RuntimeContext,
    pub sequence: u64,
    pub producer_generation: u64,
    pub context_generation: u64,
    pub valid_signals: u32,
    pub active_signals: u32,
    pub source_flags: u32,
    pub reserved: u32,
    pub selected_output_age_milliseconds: i64,
}

pub type GetDeviceCountFn = unsafe extern "C" fn() -> u32;
pub type GetDeviceFn = unsafe extern "C" fn(u32, *mut DeviceInfoV1) -> BusResult;
pub type GetSnapshotFn = unsafe extern "C" fn(u32, *mut DeviceSnapshotV1) -> BusResult;
pub type GetProfileStateFn = unsafe extern "C" fn(*mut ProfileStateV1) -> BusResult;
pub type GetRuntimeContextFn = unsafe extern "C" fn(*mut RuntimeContextV1) -> BusResult;
pub type BeginCaptureFn = unsafe extern "C" fn(*const CaptureRequestV1, *mut u64) -> BusResult;
pub type PollCaptureFn = unsafe extern "C" fn(u64, *mut CaptureResultV1) -> BusResult;
pub type CancelCaptureFn = unsafe extern "C" fn(u64) -> BusResult;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct ApiV1 {
    pub struct_size: u32,
    pub abi_version: u32,
    pub provider_id: *const c_char,
    pub display_name: *const c_char,
    pub version: *const c_char,
    pub capabilities: u64,
    pub get_device_count: Option<GetDeviceCountFn>,
    pub get_device: Option<GetDeviceFn>,
    pub get_snapshot: Option<GetSnapshotFn>,
    pub get_profile_state: Option<GetProfileStateFn>,
    pub get_runtime_context: Option<GetRuntimeContextFn>,
    pub begin_capture: Option<BeginCaptureFn>,
    pub poll_capture: Option<PollCaptureFn>,
    pub cancel_capture: Option<CancelCaptureFn>,
}

// SAFETY: the table holds only pointers to 'static strings and functions.
unsafe impl Sync for ApiV1 {}

unsafe impl Pod for DeviceInfoV1 {}
unsafe impl Pod for DeviceSnapshotV1 {}
unsafe impl Pod for BindingV1 {}
unsafe impl Pod for CaptureRequestV1 {}
unsafe impl Pod for CaptureResultV1 {}
unsafe impl Pod for ProfileStateV1 {}
unsafe impl Pod for RuntimeContextV1 {}

macro_rules! sized_new {
    ($($ty:ident),*) => {$(
        impl $ty {
            /// Zeroed value with `struct_size` filled in, matching the C++ default member initializers.
            pub fn new() -> Self {
                let mut value = <Self as Pod>::zeroed();
                value.struct_size = std::mem::size_of::<Self>() as u32;
                value
            }
        }
        impl Default for $ty { fn default() -> Self { Self::new() } }
    )*};
}
sized_new!(BindingV1, CaptureResultV1, ProfileStateV1);

impl DeviceInfoV1 {
    pub fn new() -> Self {
        let mut value = <Self as Pod>::zeroed();
        value.struct_size = std::mem::size_of::<Self>() as u32;
        value.pov_count = POV_COUNT as u32;
        value
    }
}
impl Default for DeviceInfoV1 {
    fn default() -> Self {
        Self::new()
    }
}

impl DeviceSnapshotV1 {
    pub fn new() -> Self {
        let mut value = <Self as Pod>::zeroed();
        value.struct_size = std::mem::size_of::<Self>() as u32;
        value.pov_hundredths = [-1; POV_COUNT];
        value
    }
}
impl Default for DeviceSnapshotV1 {
    fn default() -> Self {
        Self::new()
    }
}

impl CaptureRequestV1 {
    pub fn new() -> Self {
        let mut value = <Self as Pod>::zeroed();
        value.struct_size = std::mem::size_of::<Self>() as u32;
        value.allowed_controls = CAPTURE_DIGITAL;
        value.settle_milliseconds = 50;
        value.timeout_milliseconds = 8000;
        value
    }
}
impl Default for CaptureRequestV1 {
    fn default() -> Self {
        Self::new()
    }
}

impl RuntimeContextV1 {
    pub fn new() -> Self {
        let mut value = <Self as Pod>::zeroed();
        value.struct_size = std::mem::size_of::<Self>() as u32;
        value.selected_output_age_milliseconds = -1;
        value
    }
}
impl Default for RuntimeContextV1 {
    fn default() -> Self {
        Self::new()
    }
}

impl DeviceSnapshotV1 {
    pub fn is_down(&self, channel: usize) -> bool {
        channel < DIGITAL_CONTROL_COUNT && self.digital_down[channel / 64] & (1u64 << (channel % 64)) != 0
    }
}

/// Copies `value` into a fixed C string field, truncating and always NUL-terminating.
pub fn copy_str(destination: &mut [c_char], value: &str) {
    destination.fill(0);
    let bytes = value.as_bytes();
    let count = bytes.len().min(destination.len().saturating_sub(1));
    for (slot, byte) in destination.iter_mut().zip(&bytes[..count]) {
        *slot = *byte as c_char;
    }
}

/// Reads a fixed C string field (lossy UTF-8, stops at the first NUL).
pub fn read_str(source: &[c_char]) -> String {
    let bytes: Vec<u8> = source.iter().take_while(|c| **c != 0).map(|c| *c as u8).collect();
    String::from_utf8_lossy(&bytes).into_owned()
}
