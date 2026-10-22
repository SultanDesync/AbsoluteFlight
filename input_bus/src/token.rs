//! Binding tokens in the AbsoluteHOTAS format:
//!
//! ```text
//! {INSTANCE-GUID}@axis:0x30      HID usage 0x30..0x37
//! {INSTANCE-GUID}@button:7       button 1..128
//! {INSTANCE-GUID}@pov:0:up       POV 0..3, up|right|down|left
//! virtual:agent-0@button:3       reserved virtual-device ids
//! ```
//!
//! Parsing is case-insensitive and tolerant of surrounding whitespace; formatting is canonical
//! (uppercase GUID in braces, `0x%02X` axis usages), so `format(parse(x))` is stable.

use std::fmt;
use std::str::FromStr;

use crate::abi::{BUTTON_COUNT, DEVICE_ID_CAPACITY, POV_COUNT};

pub const VIRTUAL_PREFIX: &str = "virtual:";
pub const FIRST_AXIS_USAGE: u8 = 0x30;
pub const LAST_AXIS_USAGE: u8 = 0x37;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PovDirection {
    Up,
    Right,
    Down,
    Left,
}

impl PovDirection {
    pub const ALL: [PovDirection; 4] = [Self::Up, Self::Right, Self::Down, Self::Left];

    pub fn index(self) -> usize {
        self as usize
    }

    pub fn name(self) -> &'static str {
        ["up", "right", "down", "left"][self.index()]
    }

    fn parse(text: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|d| d.name().eq_ignore_ascii_case(text))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Control {
    /// HID usage 0x30..=0x37.
    Axis(u8),
    /// Physical button 1..=128.
    Button(u8),
    /// POV hat 0..=3 and one of four directions.
    Pov(u8, PovDirection),
}

impl Control {
    /// Digital channel in `DeviceSnapshotV1` (buttons 0..127, POV directions 128..143); `None` for axes.
    pub fn digital_channel(self) -> Option<usize> {
        match self {
            Control::Axis(_) => None,
            Control::Button(n) => Some(n as usize - 1),
            Control::Pov(i, d) => Some(BUTTON_COUNT + i as usize * 4 + d.index()),
        }
    }

    pub fn from_digital_channel(channel: usize) -> Option<Self> {
        if channel < BUTTON_COUNT {
            Some(Control::Button(channel as u8 + 1))
        } else if channel < BUTTON_COUNT + POV_COUNT * 4 {
            let ordinal = channel - BUTTON_COUNT;
            Some(Control::Pov((ordinal / 4) as u8, PovDirection::ALL[ordinal % 4]))
        } else {
            None
        }
    }

    /// Axis index 0..7 into `rawAxes`/`normalizedAxes`.
    pub fn axis_index(self) -> Option<usize> {
        match self {
            Control::Axis(usage) => Some((usage - FIRST_AXIS_USAGE) as usize),
            _ => None,
        }
    }

    /// `BindingV1::controlId`: button 1..128, POV direction 129..144, axis usage 0x30..0x37.
    pub fn control_id(self) -> u32 {
        match self {
            Control::Axis(usage) => usage as u32,
            Control::Button(n) => n as u32,
            Control::Pov(..) => self.digital_channel().unwrap() as u32 + 1,
        }
    }
}

impl fmt::Display for Control {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Control::Axis(usage) => write!(f, "axis:0x{usage:02X}"),
            Control::Button(n) => write!(f, "button:{n}"),
            Control::Pov(i, d) => write!(f, "pov:{i}:{}", d.name()),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Token {
    /// Canonical persistent device id.
    pub device: String,
    pub control: Control,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenError(pub String);

impl fmt::Display for TokenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TokenError {}

fn err<T>(text: &str, why: &str) -> Result<T, TokenError> {
    Err(TokenError(format!("invalid token {text:?}: {why}")))
}

/// Canonicalizes a persistent device id: `{GUID}` (uppercased) or `virtual:<name>`
/// (`<name>` lowercase ASCII letters, digits, `-`, `_`). Anything else is rejected.
pub fn canonical_device_id(text: &str) -> Result<String, TokenError> {
    let text = text.trim();
    if text.starts_with('{') {
        return canonical_guid(text).ok_or_else(|| TokenError(format!("invalid device GUID {text:?}")));
    }
    if text.len() > VIRTUAL_PREFIX.len() && text[..VIRTUAL_PREFIX.len()].eq_ignore_ascii_case(VIRTUAL_PREFIX) {
        let name = text[VIRTUAL_PREFIX.len()..].to_ascii_lowercase();
        let ok = name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_');
        if ok && VIRTUAL_PREFIX.len() + name.len() < DEVICE_ID_CAPACITY {
            return Ok(format!("{VIRTUAL_PREFIX}{name}"));
        }
    }
    Err(TokenError(format!("invalid device id {text:?}: expected {{GUID}} or virtual:<name>")))
}

fn canonical_guid(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    if bytes.len() != 38 || bytes[0] != b'{' || bytes[37] != b'}' {
        return None;
    }
    let mut out = String::with_capacity(38);
    for (i, &b) in bytes.iter().enumerate() {
        match i {
            0 | 37 => out.push(b as char),
            9 | 14 | 19 | 24 => {
                if b != b'-' {
                    return None;
                }
                out.push('-');
            }
            _ => {
                if !b.is_ascii_hexdigit() {
                    return None;
                }
                out.push(b.to_ascii_uppercase() as char);
            }
        }
    }
    Some(out)
}

/// Formats 16 GUID bytes in Windows memory order (`Data1` LE u32, `Data2`/`Data3` LE u16, `Data4` bytes)
/// as the canonical `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`.
pub fn format_guid_bytes(b: &[u8; 16]) -> String {
    let d1 = u32::from_le_bytes([b[0], b[1], b[2], b[3]]);
    let d2 = u16::from_le_bytes([b[4], b[5]]);
    let d3 = u16::from_le_bytes([b[6], b[7]]);
    format!(
        "{{{d1:08X}-{d2:04X}-{d3:04X}-{:02X}{:02X}-{:02X}{:02X}{:02X}{:02X}{:02X}{:02X}}}",
        b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]
    )
}

fn parse_number(text: &str) -> Option<u32> {
    let text = text.trim();
    if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        u32::from_str_radix(hex, 16).ok()
    } else {
        text.parse().ok()
    }
}

impl FromStr for Token {
    type Err = TokenError;

    fn from_str(text: &str) -> Result<Self, TokenError> {
        let trimmed = text.trim();
        let Some((device, control)) = trimmed.rsplit_once('@') else {
            return err(text, "missing '@'");
        };
        let device = canonical_device_id(device)?;
        let mut parts = control.trim().split(':');
        let kind = parts.next().unwrap_or("").trim().to_ascii_lowercase();
        let control = match kind.as_str() {
            "axis" => {
                let (Some(value), None) = (parts.next(), parts.next()) else {
                    return err(text, "axis needs exactly one usage");
                };
                match parse_number(value) {
                    Some(u) if (FIRST_AXIS_USAGE as u32..=LAST_AXIS_USAGE as u32).contains(&u) => Control::Axis(u as u8),
                    _ => return err(text, "axis usage must be 0x30..0x37"),
                }
            }
            "button" => {
                let (Some(value), None) = (parts.next(), parts.next()) else {
                    return err(text, "button needs exactly one number");
                };
                match value.trim().parse::<u32>() {
                    Ok(n) if (1..=BUTTON_COUNT as u32).contains(&n) => Control::Button(n as u8),
                    _ => return err(text, "button must be 1..128"),
                }
            }
            "pov" => {
                let (Some(index), Some(direction), None) = (parts.next(), parts.next(), parts.next()) else {
                    return err(text, "pov needs index and direction");
                };
                let index = match index.trim().parse::<u32>() {
                    Ok(i) if (i as usize) < POV_COUNT => i as u8,
                    _ => return err(text, "pov index must be 0..3"),
                };
                let Some(direction) = PovDirection::parse(direction.trim()) else {
                    return err(text, "pov direction must be up|right|down|left");
                };
                Control::Pov(index, direction)
            }
            _ => return err(text, "control must be axis:, button: or pov:"),
        };
        Ok(Token { device, control })
    }
}

impl fmt::Display for Token {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}@{}", self.device, self.control)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const G: &str = "{6F1D2B60-D5A0-11CF-BFC7-444553540000}";

    #[test]
    fn round_trips_every_control() {
        let mut controls: Vec<Control> = (0x30..=0x37).map(Control::Axis).collect();
        controls.extend((1..=128).map(Control::Button));
        for i in 0..4 {
            controls.extend(PovDirection::ALL.map(|d| Control::Pov(i, d)));
        }
        for device in [G, "virtual:agent-0"] {
            for control in &controls {
                let token = Token { device: device.to_string(), control: *control };
                let text = token.to_string();
                assert_eq!(text.parse::<Token>().unwrap(), token, "{text}");
            }
        }
    }

    #[test]
    fn canonical_formatting() {
        let parsed: Token = " {6f1d2b60-d5a0-11cf-bfc7-444553540000} @ AXIS:0x31 ".parse().unwrap();
        assert_eq!(parsed.to_string(), format!("{G}@axis:0x31"));
        assert_eq!("VIRTUAL:Agent-0@Pov:2:LEFT".parse::<Token>().unwrap().to_string(), "virtual:agent-0@pov:2:left");
        assert_eq!(format!("{G}@axis:50").parse::<Token>().unwrap().control, Control::Axis(0x32));
    }

    #[test]
    fn matches_absolutehotas_examples() {
        // Strings exactly as AbsoluteHOTAS InputBus.cpp formats them.
        for text in [format!("{G}@button:7"), format!("{G}@pov:0:up"), format!("{G}@axis:0x30")] {
            assert_eq!(text.parse::<Token>().unwrap().to_string(), text);
        }
    }

    #[test]
    fn rejects_bad_tokens() {
        for bad in [
            "".to_string(),
            "no-at-sign".to_string(),
            format!("{G}@axis:0x38"),
            format!("{G}@axis:0x2F"),
            format!("{G}@button:0"),
            format!("{G}@button:129"),
            format!("{G}@pov:4:up"),
            format!("{G}@pov:0:north"),
            format!("{G}@pov:0"),
            format!("{G}@slider:1"),
            "{6F1D2B60-D5A0-11CF-BFC7-44455354000}@button:1".to_string(),
            "{6F1D2B60_D5A0-11CF-BFC7-444553540000}@button:1".to_string(),
            "Gladiator@button:1".to_string(),
            "virtual:@button:1".to_string(),
            "virtual:has space@button:1".to_string(),
        ] {
            assert!(bad.parse::<Token>().is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn channel_mapping_matches_v1() {
        assert_eq!(Control::Button(1).digital_channel(), Some(0));
        assert_eq!(Control::Button(128).digital_channel(), Some(127));
        assert_eq!(Control::Pov(0, PovDirection::Up).digital_channel(), Some(128));
        assert_eq!(Control::Pov(3, PovDirection::Left).digital_channel(), Some(143));
        assert_eq!(Control::Pov(0, PovDirection::Up).control_id(), 129);
        assert_eq!(Control::Pov(3, PovDirection::Left).control_id(), 144);
        for channel in 0..144 {
            assert_eq!(Control::from_digital_channel(channel).unwrap().digital_channel(), Some(channel));
        }
        assert_eq!(Control::from_digital_channel(144), None);
    }

    #[test]
    fn guid_bytes_format() {
        // GUID_Joystick-style layout check: Data1/2/3 little-endian in memory.
        let bytes = [0x60, 0x2B, 0x1D, 0x6F, 0xA0, 0xD5, 0xCF, 0x11, 0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00];
        assert_eq!(format_guid_bytes(&bytes), G);
    }
}
