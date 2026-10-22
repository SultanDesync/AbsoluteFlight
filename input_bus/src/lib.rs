//! Absolute Input Bus for No Man's Sky (`docs/INPUT-BUS.md`, D11).
//!
//! A read-only DirectInput mirror: one poller owns the devices and publishes copied v1 POD snapshots
//! (byte-compatible with AbsoluteHOTAS `AbsoluteInputBusAPI.h`). It never writes game memory and never
//! emits input. Semantics (actions, bindings, shaping) belong to consumers (`docs/INPUT-ARCHITECTURE.md`).
//!
//! Layers: `source` (trait) ← `directinput` / `virtual_source`; `core` (table, edges, generation,
//! calibration); `bus` (poll thread, non-blocking publication); `ffi` (host A exports); `token`
//! (binding token parse/format).
//!
//! One exception to read-only: `codepatch` swaps a call in the host process for the release mod
//! (T100), because Python can't suspend threads in-process. It is separate from the bus and its ABI.

pub mod abi;
pub mod bus;
#[cfg(windows)]
pub mod codepatch;
pub mod core;
#[cfg(windows)]
pub mod directinput;
pub mod edges;
pub mod ffi;
pub mod source;
pub mod token;
pub mod virtual_source;

pub use bus::{Bus, BusConfig, Stats};
pub use core::{Calibration, Frame};
pub use token::{Control, PovDirection, Token};
