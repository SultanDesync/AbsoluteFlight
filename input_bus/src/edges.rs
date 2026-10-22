//! Digital state, POV direction decoding and press/release counters (port of AbsoluteHOTAS `InputBusState.h`).

use crate::abi::{BUTTON_COUNT, DIGITAL_CONTROL_COUNT, POV_COUNT};
use crate::source::RawState;

pub type DigitalArray = [bool; DIGITAL_CONTROL_COUNT];

/// True when a raw DirectInput POV angle (hundredths of a degree) is within 45° of `direction`
/// (0 up, 1 right, 2 down, 3 left). Diagonals therefore activate two directions. Centered is
/// any value whose low word is 0xFFFF.
pub fn pov_direction_active(raw: u32, direction: usize) -> bool {
    if direction >= 4 || raw & 0xFFFF == 0xFFFF {
        return false;
    }
    let target = direction as u32 * 9000;
    let mut difference = raw.abs_diff(target);
    if difference > 18000 {
        difference = 36000 - difference.min(36000);
    }
    difference <= 4500
}

pub fn digital_state(state: &RawState) -> DigitalArray {
    let mut out = [false; DIGITAL_CONTROL_COUNT];
    out[..BUTTON_COUNT].copy_from_slice(&state.buttons);
    for pov in 0..POV_COUNT {
        for direction in 0..4 {
            out[BUTTON_COUNT + pov * 4 + direction] = pov_direction_active(state.povs[pov], direction);
        }
    }
    out
}

/// Number of edges between two observations of a monotonic counter (uint32 wrap).
pub fn counter_delta(now: u32, then: u32) -> u32 {
    now.wrapping_sub(then)
}

#[derive(Clone)]
pub struct EdgeState {
    pub initialized: bool,
    pub connected: bool,
    pub previous_down: DigitalArray,
    pub press_count: [u32; DIGITAL_CONTROL_COUNT],
    pub release_count: [u32; DIGITAL_CONTROL_COUNT],
}

impl Default for EdgeState {
    fn default() -> Self {
        Self {
            initialized: false,
            connected: false,
            previous_down: [false; DIGITAL_CONTROL_COUNT],
            press_count: [0; DIGITAL_CONTROL_COUNT],
            release_count: [0; DIGITAL_CONTROL_COUNT],
        }
    }
}

impl EdgeState {
    /// Advances counters. The first observation and every reconnection are level baselines:
    /// a control already held at either boundary does not synthesize a press. A disconnect
    /// releases every held control (counted as a release).
    pub fn update(&mut self, connected: bool, current: &DigitalArray) {
        if !self.initialized || (!self.connected && connected) {
            self.initialized = true;
            self.connected = connected;
            self.previous_down = if connected { *current } else { [false; DIGITAL_CONTROL_COUNT] };
            return;
        }
        let effective = if connected { *current } else { [false; DIGITAL_CONTROL_COUNT] };
        for (i, (&was, &is)) in self.previous_down.iter().zip(effective.iter()).enumerate() {
            match (was, is) {
                (false, true) => self.press_count[i] = self.press_count[i].wrapping_add(1),
                (true, false) => self.release_count[i] = self.release_count[i].wrapping_add(1),
                _ => {}
            }
        }
        self.previous_down = effective;
        self.connected = connected;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn with(channels: &[usize]) -> DigitalArray {
        let mut a = [false; DIGITAL_CONTROL_COUNT];
        for &c in channels {
            a[c] = true;
        }
        a
    }

    #[test]
    fn first_observation_is_baseline() {
        let mut e = EdgeState::default();
        e.update(true, &with(&[3]));
        assert_eq!(e.press_count[3], 0);
        e.update(true, &with(&[]));
        assert_eq!(e.release_count[3], 1);
        e.update(true, &with(&[3]));
        assert_eq!(e.press_count[3], 1);
    }

    #[test]
    fn reconnect_is_baseline_and_disconnect_releases() {
        let mut e = EdgeState::default();
        e.update(true, &with(&[]));
        e.update(true, &with(&[5]));
        assert_eq!((e.press_count[5], e.release_count[5]), (1, 0));
        e.update(false, &with(&[5]));
        assert_eq!(e.release_count[5], 1, "disconnect releases held controls");
        e.update(true, &with(&[5]));
        assert_eq!(e.press_count[5], 1, "held at reconnect must not synthesize a press");
    }

    #[test]
    fn counter_wraps() {
        let mut e = EdgeState::default();
        e.update(true, &with(&[]));
        e.press_count[0] = u32::MAX;
        e.release_count[0] = u32::MAX;
        let seen_press = e.press_count[0];
        let seen_release = e.release_count[0];
        e.update(true, &with(&[0]));
        e.update(true, &with(&[]));
        e.update(true, &with(&[0]));
        assert_eq!(e.press_count[0], 1);
        assert_eq!(e.release_count[0], 0);
        assert_eq!(counter_delta(e.press_count[0], seen_press), 2);
        assert_eq!(counter_delta(e.release_count[0], seen_release), 1);
    }

    #[test]
    fn pov_directions() {
        assert!(pov_direction_active(0, 0));
        assert!(!pov_direction_active(0, 1));
        assert!(pov_direction_active(4500, 0) && pov_direction_active(4500, 1));
        assert!(pov_direction_active(31500, 3) && pov_direction_active(31500, 0));
        assert!(pov_direction_active(18000, 2));
        assert!(!pov_direction_active(0xFFFF_FFFF, 0));
        assert!(!pov_direction_active(0xFFFF, 0));
        let mut raw = RawState::neutral();
        raw.povs[1] = 9000;
        raw.buttons[2] = true;
        let d = digital_state(&raw);
        assert!(d[2] && d[BUTTON_COUNT + 4 + 1]);
        assert_eq!(d.iter().filter(|x| **x).count(), 2);
    }
}
