# input_bus — Absolute Input Bus for NMS (BUS-0)

Read-only DirectInput mirror per `docs/INPUT-BUS.md` (D11). Layers above it: `docs/INPUT-ARCHITECTURE.md`.
Never writes game memory, never emits input.

```
cargo test                                   # layout parity vs AbsoluteInputBusAPI.h, tokens, counters, bus thread
cargo run --bin input-bus -- list [--json]   # DirectInput devices, VID/PID, caps, cooperative-level result, first raw values
cargo run --bin input-bus -- watch --seconds 10 [--device vjoy] [--virtual] [--hz 250]
cargo build --release                        # target/release/absolute_input_bus.dll (host A) + input-bus.exe
python -I scripts/bus0_probe.py target\release\absolute_input_bus.dll --seconds 5   # ctypes consumer, one JSON line
```

| File | What |
|------|------|
| `src/abi.rs` | `#[repr(C)]` v1 structs/constants (u32 newtype enums) |
| `src/token.rs` | `{GUID}@axis:0x30` / `@button:N` / `@pov:N:dir` / `virtual:<name>@…` parse + canonical format |
| `src/edges.rs` | POV direction decoding, press/release counters (uint32 wrap), reconnect baselines |
| `src/source.rs` | `InputSource` trait, `RawState`, `DeviceDescriptor` |
| `src/directinput.rs` | DirectInput source (seeded from AbsoluteHOTAS-Configurator `direct_input/devices.rs`, MIT) |
| `src/virtual_source.rs` | agent virtual device `virtual:agent-0` (same bus path as hardware) |
| `src/core.rs` | device table, generation, calibration, snapshot building (pure, unit-tested) |
| `src/bus.rs` | poll thread + two-slot `try_lock` publication (poller never waits on readers) |
| `src/ffi.rs` | `AbsoluteInputBus_QueryApi(1)`, `AbsoluteInputBus_Start(pollHz)`, `AbsoluteInputBus_Stop()` |
| `tests/layout.rs` | asserts every size/align/offset in `layout_v1_msvc.txt` (from `layout_probe.cpp`, MSVC x64) |

Regenerating the layout file: compile `tests/layout_probe.cpp` with MSVC x64 `/std:c++20 /I <AbsoluteHOTAS>/include`, run it, save stdout as `tests/layout_v1_msvc.txt` (LF, no BOM).
