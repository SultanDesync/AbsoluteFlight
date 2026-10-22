//! Source-independent bus state: the published device table, edges/counters, `producerGeneration`,
//! calibration and snapshot building. Pure logic, no threads and no device APIs, so it is unit-tested
//! with the virtual source.

use std::collections::HashMap;

use crate::abi::{copy_str, DeviceInfoV1, DeviceSnapshotV1, AXIS_COUNT, BUTTON_COUNT, DIGITAL_CONTROL_COUNT, POV_COUNT};
use crate::edges::{digital_state, EdgeState};
use crate::source::{InputSource, AXIS_RAW_MAX, AXIS_RAW_MIN};

/// One published state of the whole bus. Readers get a copy.
#[derive(Clone, Default)]
pub struct Frame {
    /// Advances on every poll.
    pub sequence: u64,
    /// Changes when the device table changes or a device reconnects. Consumers rebase counters on change.
    pub producer_generation: u64,
    pub devices: Vec<DeviceInfoV1>,
    pub snapshots: Vec<DeviceSnapshotV1>,
}

impl Frame {
    pub fn index_of(&self, persistent_id: &str) -> Option<usize> {
        self.devices.iter().position(|d| crate::abi::read_str(&d.persistent_id) == persistent_id)
    }
}

/// Bus-owned per-axis calibration (observed min/max), keyed by persistent id. BUS-1 loads it from
/// `input_bus/calibration.ini`; until then every axis uses the full 0..65535 range.
#[derive(Clone, Default, Debug)]
pub struct Calibration {
    ranges: HashMap<String, [Option<(i32, i32)>; AXIS_COUNT]>,
}

impl Calibration {
    pub fn set(&mut self, persistent_id: &str, axis: usize, minimum: i32, maximum: i32) {
        if axis < AXIS_COUNT && maximum > minimum {
            self.ranges.entry(persistent_id.to_string()).or_insert([None; AXIS_COUNT])[axis] = Some((minimum, maximum));
        }
    }

    pub fn range(&self, persistent_id: &str, axis: usize) -> (i32, i32) {
        self.ranges
            .get(persistent_id)
            .and_then(|axes| axes.get(axis).copied().flatten())
            .unwrap_or((AXIS_RAW_MIN, AXIS_RAW_MAX))
    }
}

/// Bipolar [-1, 1] from a raw value and calibrated range (AbsoluteHOTAS formula).
pub fn normalize(raw: i32, minimum: i32, maximum: i32) -> f32 {
    if maximum <= minimum {
        return 0.0;
    }
    let unit = (raw as f64 - minimum as f64) / (maximum as f64 - minimum as f64);
    (unit * 2.0 - 1.0).clamp(-1.0, 1.0) as f32
}

struct Entry {
    source: usize,
    local: usize,
    persistent_id: String,
    edges: EdgeState,
    /// A device's first-ever connection (e.g. after the source's acquire settle) is not a reconnect.
    ever_connected: bool,
    info: DeviceInfoV1,
    snapshot: DeviceSnapshotV1,
}

#[derive(Default)]
pub struct BusCore {
    entries: Vec<Entry>,
    generation: u64,
    sequence: u64,
    pub calibration: Calibration,
    /// Persistent ids reported by more than one source on the last rebuild (only the first is published).
    pub duplicates: Vec<String>,
}

fn next_nonzero(value: u64) -> u64 {
    match value.wrapping_add(1) {
        0 => 1,
        v => v,
    }
}

impl BusCore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn producer_generation(&self) -> u64 {
        self.generation
    }

    /// Rebuilds the published table from every source's descriptors. Always a new generation;
    /// counters restart from a level baseline.
    pub fn rebuild(&mut self, sources: &[Box<dyn InputSource>]) {
        self.generation = next_nonzero(self.generation);
        self.entries.clear();
        self.duplicates.clear();
        for (source_index, source) in sources.iter().enumerate() {
            for (local, d) in source.devices().iter().enumerate() {
                if self.entries.iter().any(|e| e.persistent_id == d.persistent_id) {
                    self.duplicates.push(d.persistent_id.clone());
                    continue;
                }
                let device_index = self.entries.len() as u32;
                let mut info = DeviceInfoV1::new();
                info.device_index = device_index;
                info.instance_guid = d.instance_guid;
                info.product_guid = d.product_guid;
                info.vendor_id = d.vendor_id;
                info.product_id = d.product_id;
                info.axis_count = d.axis_count.min(AXIS_COUNT as u32);
                info.button_count = d.button_count.min(BUTTON_COUNT as u32);
                info.pov_count = POV_COUNT as u32;
                copy_str(&mut info.persistent_id, &d.persistent_id);
                copy_str(&mut info.instance_name, &d.instance_name);
                copy_str(&mut info.product_name, &d.product_name);
                let mut snapshot = DeviceSnapshotV1::new();
                snapshot.device_index = device_index;
                snapshot.producer_generation = self.generation;
                self.entries.push(Entry {
                    source: source_index,
                    local,
                    persistent_id: d.persistent_id.clone(),
                    edges: EdgeState::default(),
                    ever_connected: false,
                    info,
                    snapshot,
                });
            }
        }
    }

    /// Polls every published device once and rebuilds its snapshot.
    pub fn poll(&mut self, sources: &mut [Box<dyn InputSource>]) {
        self.sequence += 1;
        let mut reconnected = false;
        for entry in &mut self.entries {
            let raw = sources[entry.source].poll(entry.local);
            let connected = raw.is_some();
            if connected && entry.ever_connected && !entry.edges.connected {
                reconnected = true;
            }
            entry.ever_connected |= connected;
            let digital = raw.as_ref().map(digital_state).unwrap_or([false; DIGITAL_CONTROL_COUNT]);
            entry.edges.update(connected, &digital);

            let out = &mut entry.snapshot;
            out.connected = connected as u8;
            out.digital_down = [0; 3];
            for (channel, down) in digital.iter().enumerate() {
                if *down {
                    out.digital_down[channel / 64] |= 1 << (channel % 64);
                }
            }
            out.press_count = entry.edges.press_count;
            out.release_count = entry.edges.release_count;
            for axis in 0..AXIS_COUNT {
                let (minimum, maximum) = self.calibration.range(&entry.persistent_id, axis);
                out.axis_minimum[axis] = minimum;
                out.axis_maximum[axis] = maximum;
                match &raw {
                    Some(state) => {
                        out.raw_axes[axis] = state.axes[axis];
                        out.normalized_axes[axis] = normalize(state.axes[axis], minimum, maximum);
                    }
                    None => {
                        out.raw_axes[axis] = 0;
                        out.normalized_axes[axis] = 0.0;
                    }
                }
            }
            for pov in 0..POV_COUNT {
                out.pov_hundredths[pov] = match &raw {
                    Some(state) if state.povs[pov] & 0xFFFF != 0xFFFF => state.povs[pov] as i32,
                    _ => -1,
                };
            }
        }
        if reconnected {
            self.generation = next_nonzero(self.generation);
        }
        for entry in &mut self.entries {
            entry.snapshot.sequence = self.sequence;
            entry.snapshot.producer_generation = self.generation;
        }
    }

    /// Copies the current state into `frame`, reusing its allocations.
    pub fn write_frame(&self, frame: &mut Frame) {
        frame.sequence = self.sequence;
        frame.producer_generation = self.generation;
        frame.devices.clear();
        frame.devices.extend(self.entries.iter().map(|e| e.info));
        frame.snapshots.clear();
        frame.snapshots.extend(self.entries.iter().map(|e| e.snapshot));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::edges::counter_delta;
    use crate::token::{Control, PovDirection, Token};
    use crate::virtual_source::VirtualSource;

    fn setup() -> (BusCore, Vec<Box<dyn InputSource>>, crate::virtual_source::VirtualHandle) {
        let (source, handle) = VirtualSource::new("agent-0").unwrap();
        let mut sources: Vec<Box<dyn InputSource>> = vec![Box::new(source)];
        let mut core = BusCore::new();
        core.rebuild(&sources);
        core.poll(&mut sources);
        (core, sources, handle)
    }

    fn frame(core: &BusCore) -> Frame {
        let mut f = Frame::default();
        core.write_frame(&mut f);
        f
    }

    #[test]
    fn virtual_device_is_published_like_hardware() {
        let (core, _, _) = setup();
        let f = frame(&core);
        assert_eq!(f.devices.len(), 1);
        let info = &f.devices[0];
        assert_eq!(crate::abi::read_str(&info.persistent_id), "virtual:agent-0");
        assert_eq!((info.vendor_id, info.product_id, info.axis_count, info.button_count, info.pov_count), (0, 0, 8, 128, 4));
        assert_eq!(info.instance_guid, [0; 16]);
        let s = &f.snapshots[0];
        assert_eq!(s.connected, 1);
        assert!(s.normalized_axes.iter().all(|v| v.abs() < 1e-4));
        assert_eq!(s.pov_hundredths, [-1; 4]);
        assert_eq!(f.producer_generation, 1);
    }

    #[test]
    fn virtual_token_drives_counters_and_axes() {
        let (mut core, mut sources, h) = setup();
        let boost: Token = "virtual:agent-0@button:3".parse().unwrap();
        let hat: Token = "virtual:agent-0@pov:0:down".parse().unwrap();
        let pitch: Token = "virtual:agent-0@axis:0x31".parse().unwrap();
        let before = frame(&core).snapshots[0];

        // A tap shorter than one consumer frame: press and release across two polls.
        h.set_control(boost.control, 1.0);
        h.set_control(hat.control, 1.0);
        h.set_control(pitch.control, -0.5);
        core.poll(&mut sources);
        h.set_control(boost.control, 0.0);
        core.poll(&mut sources);

        let s = frame(&core).snapshots[0];
        let ch = boost.control.digital_channel().unwrap();
        assert_eq!(counter_delta(s.press_count[ch], before.press_count[ch]), 1);
        assert_eq!(counter_delta(s.release_count[ch], before.release_count[ch]), 1);
        assert!(!s.is_down(ch));
        let hat_ch = Control::Pov(0, PovDirection::Down).digital_channel().unwrap();
        assert!(s.is_down(hat_ch));
        assert_eq!(s.pov_hundredths[0], 18000);
        let axis = pitch.control.axis_index().unwrap();
        assert!((s.normalized_axes[axis] + 0.5).abs() < 1e-3, "{}", s.normalized_axes[axis]);
        assert_eq!(s.sequence, 3);
    }

    #[test]
    fn reconnect_bumps_generation_without_synthetic_press() {
        let (mut core, mut sources, h) = setup();
        h.set_button(1, true);
        core.poll(&mut sources);
        let g1 = core.producer_generation();
        h.set_connected(false);
        core.poll(&mut sources);
        let s = frame(&core).snapshots[0];
        assert_eq!(s.connected, 0);
        assert_eq!(s.raw_axes, [0; 8]);
        assert_eq!(core.producer_generation(), g1, "disconnect alone keeps the generation");
        h.set_connected(true);
        core.poll(&mut sources);
        let s = frame(&core).snapshots[0];
        assert_eq!(core.producer_generation(), g1 + 1);
        assert_eq!(s.producer_generation, g1 + 1);
        assert_eq!(s.press_count[0], 1, "held across reconnect: no second press");
    }

    #[test]
    fn late_first_connection_is_baseline_not_reconnect() {
        let (source, h) = VirtualSource::new("agent-0").unwrap();
        h.set_connected(false);
        let mut sources: Vec<Box<dyn InputSource>> = vec![Box::new(source)];
        let mut core = BusCore::new();
        core.rebuild(&sources);
        core.poll(&mut sources);
        let generation = core.producer_generation();
        h.set_button(3, true);
        h.set_connected(true);
        core.poll(&mut sources);
        let s = frame(&core).snapshots[0];
        assert_eq!(core.producer_generation(), generation, "first connection must not bump the generation");
        assert_eq!(s.connected, 1);
        assert!(s.is_down(2));
        assert_eq!(s.press_count[2], 0, "held at first connection: no synthetic press");
    }

    #[test]
    fn calibration_and_normalization() {
        assert_eq!(normalize(0, 0, 65535), -1.0);
        assert_eq!(normalize(65535, 0, 65535), 1.0);
        assert_eq!(normalize(70000, 0, 65535), 1.0);
        assert_eq!(normalize(5, 5, 5), 0.0);
        let (mut core, mut sources, h) = setup();
        core.calibration.set("virtual:agent-0", 2, 1000, 3000);
        h.set_axis_raw(2, 2000);
        core.poll(&mut sources);
        let s = frame(&core).snapshots[0];
        assert_eq!((s.axis_minimum[2], s.axis_maximum[2]), (1000, 3000));
        assert!(s.normalized_axes[2].abs() < 1e-6);
    }
}
