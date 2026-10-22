//! The source trait. A source owns one device family (DirectInput, virtual) and produces v1-shaped
//! raw state. Everything above it (edges, counters, generation, calibration, publication) is shared.

use crate::abi::{AXIS_COUNT, BUTTON_COUNT, POV_COUNT};

/// DirectInput axis range set by every source (`DIPROP_RANGE`).
pub const AXIS_RAW_MIN: i32 = 0;
pub const AXIS_RAW_MAX: i32 = 65535;
pub const AXIS_RAW_CENTER: i32 = 32767;
/// DirectInput's centered POV value.
pub const POV_CENTERED: u32 = 0xFFFF_FFFF;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RawState {
    /// HID usages 0x30..0x37 in fixed order (X, Y, Z, Rx, Ry, Rz, Slider0, Slider1).
    pub axes: [i32; AXIS_COUNT],
    /// Raw DirectInput POV angles in hundredths of a degree, `POV_CENTERED` when centered.
    pub povs: [u32; POV_COUNT],
    pub buttons: [bool; BUTTON_COUNT],
}

impl RawState {
    pub fn neutral() -> Self {
        Self { axes: [AXIS_RAW_CENTER; AXIS_COUNT], povs: [POV_CENTERED; POV_COUNT], buttons: [false; BUTTON_COUNT] }
    }
}

/// Raw value for a bipolar normalized position in [-1, 1] on the 0..65535 range.
pub fn raw_from_normalized(value: f32) -> i32 {
    let unit = (value.clamp(-1.0, 1.0) as f64 + 1.0) / 2.0;
    (AXIS_RAW_MIN as f64 + unit * (AXIS_RAW_MAX - AXIS_RAW_MIN) as f64).round() as i32
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DeviceDescriptor {
    /// Durable identity: `{GUID}` or `virtual:<name>`.
    pub persistent_id: String,
    pub instance_guid: [u8; 16],
    pub product_guid: [u8; 16],
    pub vendor_id: u16,
    pub product_id: u16,
    pub axis_count: u32,
    pub button_count: u32,
    pub pov_count: u32,
    pub instance_name: String,
    pub product_name: String,
    /// Free-form diagnostics for the CLI (e.g. cooperative-level result). Not published over the ABI.
    pub notes: String,
}

/// A device family. Sources live on the bus poll thread (created there by a factory), so they need not be `Send`.
pub trait InputSource {
    fn name(&self) -> &str;

    /// Housekeeping called about once per rescan interval (hot-plug). Returns true when the device list changed.
    fn maintain(&mut self) -> bool;

    fn devices(&self) -> &[DeviceDescriptor];

    /// Current state of device `index` (into `devices()`), or `None` when it is disconnected or unreadable.
    fn poll(&mut self, index: usize) -> Option<RawState>;
}

pub type SourceFactory = Box<dyn FnOnce() -> Box<dyn InputSource> + Send>;
