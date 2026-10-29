//! Linux/Proton compatibility add-on for Absolute Flight (works with the release as shipped; nothing in it changes).
//!
//! Under Proton the game's heap reservations land right next to NMS.exe. When they get there first, nothing
//! Absolute Flight places near the game fits: every pyMHF hook fails with MH_ERROR_MEMORY_ALLOC, and the
//! runtime's code caves (game actions, crash damage, Newtonian, exocraft, roll patches) fail with "No free
//! block after the game image" / "No near allocation" (research/re-log/2026-10-29_claude_proton-steam-deck.md,
//! 2026-10-29_claude_proton-loader-deck.md).
//!
//! Dropped into Binaries as AbsoluteFlight-Proton.asi, the release proxy loads this from its DllMain, before
//! the game runs. Under Wine only, it reserves a pool of 64 KB granules on both sides of NMS.exe, then hands
//! them out to Absolute Flight alone:
//! - cyminhook's .pyd: its VirtualQuery/VirtualAlloc/VirtualFree imports point here (MinHook's trampolines).
//! - _ctypes.pyd: its GetProcAddress import points here, so kernel32's Virtual{Query,Alloc,Free}[Ex] looked up
//!   by ctypes (the runtime and pymem) resolve to the same hooks.
//! A pool granule looks free to those callers only, and an allocation there commits inside our reservation, so
//! the game's heap can't take it first. Everything else goes straight to kernel32.
//! Logs to %LOCALAPPDATA%\AbsoluteHOTAS\logs\proton-loader.log. AF_PROTON_LOADER=off disables it; =force runs
//! it outside Wine (for testing on Windows).
#![allow(non_snake_case, clippy::missing_safety_doc)]

use std::ffi::{c_void, CStr};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

type Handle = *mut c_void;

#[repr(C)]
struct MemoryInfo {
    base: usize,
    allocation_base: usize,
    allocation_protect: u32,
    partition_id: u16,
    region_size: usize,
    state: u32,
    protect: u32,
    kind: u32,
}

#[repr(C)]
struct UnicodeString { length: u16, maximum_length: u16, buffer: *const u16 }

#[repr(C)]
struct LoadedNotification { flags: u32, full_name: *const UnicodeString, base_name: *const UnicodeString,
                            base: usize, size: u32 }

type NotificationCallback = unsafe extern "system" fn(u32, *const LoadedNotification, *mut c_void);

#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleW(name: *const u16) -> Handle;
    fn GetProcAddress(module: Handle, name: *const u8) -> *mut c_void;
    fn DisableThreadLibraryCalls(module: Handle) -> i32;
    fn GetProcessId(process: Handle) -> u32;
    fn GetCurrentProcessId() -> u32;
    fn VirtualQuery(address: usize, info: *mut MemoryInfo, len: usize) -> usize;
    fn VirtualQueryEx(process: Handle, address: usize, info: *mut MemoryInfo, len: usize) -> usize;
    fn VirtualAlloc(address: usize, size: usize, kind: u32, protect: u32) -> usize;
    fn VirtualAllocEx(process: Handle, address: usize, size: usize, kind: u32, protect: u32) -> usize;
    fn VirtualFree(address: usize, size: usize, kind: u32) -> i32;
    fn VirtualFreeEx(process: Handle, address: usize, size: usize, kind: u32) -> i32;
    fn VirtualProtect(address: usize, size: usize, protect: u32, old: *mut u32) -> i32;
    fn GetLocalTime(time: *mut [u16; 8]);
}

const DLL_PROCESS_ATTACH: u32 = 1;
const LDR_DLL_NOTIFICATION_REASON_LOADED: u32 = 1;
const MEM_COMMIT: u32 = 0x1000;
const MEM_RESERVE: u32 = 0x2000;
const MEM_DECOMMIT: u32 = 0x4000;
const MEM_RELEASE: u32 = 0x8000;
const MEM_FREE: u32 = 0x10000;
const PAGE_NOACCESS: u32 = 0x01;
const PAGE_READWRITE: u32 = 0x04;
const GRANULE: usize = 0x10000;
// Below the image: MinHook reaches 1 GB from each target, the roll patches 2 GB, Newtonian 256 MB from its site.
const BELOW_SEARCH: usize = 0x0800_0000;
const BELOW: usize = 64;
// Above the image: native_eject.allocate_after_image tries the first 4096 granules past it.
const ABOVE_SEARCH: usize = 0x1000_0000;
const ABOVE: usize = 64;
const CYMINHOOK: &str = "_cyminhook";
const CTYPES: &str = "_ctypes.pyd";

#[derive(Clone, Copy, PartialEq)]
enum State { Free, Head(usize), Tail }

#[derive(Clone, Copy)]
struct Granule { address: usize, state: State }

/// Every reserved granule, sorted by address.
static POOL: Mutex<Vec<Granule>> = Mutex::new(Vec::new());

fn log(message: &str) {
    let Some(dir) = std::env::var_os("LOCALAPPDATA").map(|b| PathBuf::from(b).join("AbsoluteHOTAS").join("logs")) else { return };
    let _ = std::fs::create_dir_all(&dir);
    let mut t = [0u16; 8];
    unsafe { GetLocalTime(&mut t) };
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("proton-loader.log")) {
        let _ = writeln!(file, "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03} {message}",
                         t[0], t[1], t[3], t[4], t[5], t[6], t[7]);
    }
}

fn wide(text: &str) -> Vec<u16> { text.encode_utf16().chain(Some(0)).collect() }

fn pool() -> std::sync::MutexGuard<'static, Vec<Granule>> { POOL.lock().unwrap_or_else(|e| e.into_inner()) }

/// Index of the free granule holding `address`, and the end of the run of free, adjacent granules from it.
fn free_run(pool: &[Granule], address: usize) -> Option<(usize, usize)> {
    let index = pool.iter().position(|g| address >= g.address && address < g.address + GRANULE)?;
    if pool[index].state != State::Free { return None; }
    let mut end = pool[index].address + GRANULE;
    for g in &pool[index + 1..] {
        if g.address != end || g.state != State::Free { break; }
        end += GRANULE;
    }
    Some((index, end))
}

unsafe fn query(address: usize, info: *mut MemoryInfo, written: usize) -> usize {
    if written >= std::mem::size_of::<MemoryInfo>() {
        if let Some((_, end)) = free_run(&pool(), address) {
            let page = address & !0xFFF;
            *info = MemoryInfo { base: page, allocation_base: 0, allocation_protect: 0, partition_id: 0,
                                 region_size: end - page, state: MEM_FREE, protect: PAGE_NOACCESS, kind: 0 };
        }
    }
    written
}

/// Commit [base, end) granule by granule; base on success, 0 (nothing left committed) on failure.
unsafe fn commit(base: usize, end: usize, protect: u32) -> usize {
    let mut at = base;
    while at < end {
        if VirtualAlloc(at, GRANULE.min(end - at), MEM_COMMIT, protect) == 0 {
            decommit(base, at);
            return 0;
        }
        at += GRANULE;
    }
    base
}

unsafe fn decommit(base: usize, end: usize) -> i32 {
    let mut done = 1;
    for at in (base..end).step_by(GRANULE) { done &= VirtualFree(at, 0, MEM_DECOMMIT).min(1); }
    done
}

/// A reservation at an address in the pool: commit it inside our granules. None when it isn't ours.
unsafe fn alloc(address: usize, size: usize, kind: u32, protect: u32, caller: &str) -> Option<usize> {
    if address == 0 || size == 0 || kind & MEM_RESERVE == 0 { return None; }
    let base = address & !(GRANULE - 1);
    let count = (address + size - base).div_ceil(GRANULE);
    let mut pool = pool();
    let (index, end) = free_run(&pool, base)?;
    if base + count * GRANULE > end {
        drop(pool);
        log(&format!("{caller} asked for {size:#x} at {address:#x}: larger than the free pool there ({:#x})", end - base));
        return None;
    }
    // Each granule is its own reservation, and one commit can't span two: commit them one at a time.
    let result = if kind & MEM_COMMIT != 0 { commit(base, address + size, protect) } else { base };
    if result != 0 {
        pool[index].state = State::Head(count);
        for g in &mut pool[index + 1..index + count] { g.state = State::Tail; }
    }
    let left = pool.iter().filter(|g| g.state == State::Free).count();
    drop(pool);
    log(&format!("{caller} asked for {size:#x} at {address:#x}: {} ({left} spare left)",
                 if result != 0 { "given from spare" } else { "commit in spare failed" }));
    Some(result)
}

/// Releasing a block we gave out: decommit it and put its granules back. None when it isn't ours.
unsafe fn free(address: usize, kind: u32) -> Option<i32> {
    if kind & MEM_RELEASE == 0 { return None; }
    let mut pool = pool();
    let index = pool.iter().position(|g| g.address == address)?;
    let State::Head(count) = pool[index].state else { return None };
    let done = decommit(address, address + count * GRANULE);
    for g in &mut pool[index..index + count] { g.state = State::Free; }
    Some(done)
}

unsafe fn current(process: Handle) -> bool { process as isize == -1 || GetProcessId(process) == GetCurrentProcessId() }

unsafe extern "system" fn query_hook(address: usize, info: *mut MemoryInfo, len: usize) -> usize {
    query(address, info, VirtualQuery(address, info, len))
}

unsafe extern "system" fn query_ex_hook(process: Handle, address: usize, info: *mut MemoryInfo, len: usize) -> usize {
    let written = VirtualQueryEx(process, address, info, len);
    if current(process) { query(address, info, written) } else { written }
}

unsafe extern "system" fn alloc_hook(address: usize, size: usize, kind: u32, protect: u32) -> usize {
    alloc(address, size, kind, protect, "VirtualAlloc").unwrap_or_else(|| VirtualAlloc(address, size, kind, protect))
}

unsafe extern "system" fn alloc_ex_hook(process: Handle, address: usize, size: usize, kind: u32, protect: u32) -> usize {
    if current(process) {
        if let Some(result) = alloc(address, size, kind, protect, "VirtualAllocEx") { return result; }
    }
    VirtualAllocEx(process, address, size, kind, protect)
}

unsafe extern "system" fn free_hook(address: usize, size: usize, kind: u32) -> i32 {
    free(address, kind).unwrap_or_else(|| VirtualFree(address, size, kind))
}

unsafe extern "system" fn free_ex_hook(process: Handle, address: usize, size: usize, kind: u32) -> i32 {
    if current(process) {
        if let Some(done) = free(address, kind) { return done; }
    }
    VirtualFreeEx(process, address, size, kind)
}

fn hook_for(name: &[u8]) -> Option<usize> {
    Some(match name {
        b"VirtualQuery" => query_hook as *const () as usize,
        b"VirtualQueryEx" => query_ex_hook as *const () as usize,
        b"VirtualAlloc" => alloc_hook as *const () as usize,
        b"VirtualAllocEx" => alloc_ex_hook as *const () as usize,
        b"VirtualFree" => free_hook as *const () as usize,
        b"VirtualFreeEx" => free_ex_hook as *const () as usize,
        _ => return None,
    })
}

/// ctypes resolves every kernel32 function by name through this: hand back our hooks for the six above.
unsafe extern "system" fn get_proc_hook(module: Handle, name: *const u8) -> *mut c_void {
    if name as usize > 0xFFFF {
        let kernel = [wide("kernel32.dll"), wide("kernelbase.dll")].iter().any(|n| GetModuleHandleW(n.as_ptr()) == module);
        if kernel {
            if let Some(hook) = hook_for(CStr::from_ptr(name as *const i8).to_bytes()) { return hook as *mut c_void; }
        }
    }
    GetProcAddress(module, name)
}

/// Reserve up to `want` free granules stepping from `start` by `step` (+-GRANULE) until `limit`.
unsafe fn reserve(start: usize, limit: usize, step: isize, want: usize) -> usize {
    let (mut taken, mut address) = (0, start);
    let mut found = Vec::new();
    while taken < want && (if step < 0 { address >= limit } else { address < limit }) {
        let mut info = std::mem::zeroed::<MemoryInfo>();
        if VirtualQuery(address, &mut info, std::mem::size_of::<MemoryInfo>()) == 0 { break; }
        if info.state == MEM_FREE {
            if VirtualAlloc(address, GRANULE, MEM_RESERVE, PAGE_NOACCESS) == address {
                found.push(Granule { address, state: State::Free });
                taken += 1;
            }
            address = address.wrapping_add_signed(step);
        } else if step < 0 {
            let below = if info.allocation_base != 0 { info.allocation_base } else { info.base };
            if below < GRANULE { break; }
            address = (below & !(GRANULE - 1)) - GRANULE;
        } else {
            address = (info.base + info.region_size + GRANULE - 1) & !(GRANULE - 1);
        }
    }
    let mut pool = pool();
    pool.extend(found);
    pool.sort_by_key(|g| g.address);
    taken
}

/// Point the module's kernel32 import slots named in `names` at our hooks.
unsafe fn patch_imports(module: usize, names: &[&[u8]]) -> usize {
    let read32 = |at: usize| (at as *const u32).read_unaligned() as usize;
    let nt = module + read32(module + 0x3C);
    let imports = read32(nt + 24 + 112 + 8); // PE32+ optional header, data directory 1
    if imports == 0 { return 0; }
    let mut patched = 0;
    let mut descriptor = module + imports;
    while read32(descriptor + 12) != 0 {
        let dll = CStr::from_ptr((module + read32(descriptor + 12)) as *const i8).to_string_lossy();
        if dll.eq_ignore_ascii_case("kernel32.dll") {
            let (mut names_at, mut slots) = (module + read32(descriptor), module + read32(descriptor + 16));
            loop {
                let entry = *(names_at as *const u64);
                if entry == 0 { break; }
                if entry >> 63 == 0 {
                    let name = CStr::from_ptr((module + entry as usize + 2) as *const i8).to_bytes();
                    let hook = if !names.contains(&name) { None }
                               else if name == b"GetProcAddress" { Some(get_proc_hook as *const () as usize) }
                               else { hook_for(name) };
                    if let Some(hook) = hook {
                        let mut old = 0;
                        if VirtualProtect(slots, 8, PAGE_READWRITE, &mut old) != 0 {
                            *(slots as *mut usize) = hook;
                            VirtualProtect(slots, 8, old, &mut old);
                            patched += 1;
                        }
                    }
                }
                names_at += 8;
                slots += 8;
            }
        }
        descriptor += 20;
    }
    patched
}

const CYMINHOOK_IMPORTS: &[&[u8]] = &[b"VirtualQuery", b"VirtualAlloc", b"VirtualFree"];
const CTYPES_IMPORTS: &[&[u8]] = &[b"GetProcAddress"];

unsafe fn patch_module(name: &str, base: usize) {
    let lower = name.to_ascii_lowercase();
    let names = if lower.starts_with(CYMINHOOK) { CYMINHOOK_IMPORTS } else if lower == CTYPES { CTYPES_IMPORTS } else { return };
    log(&format!("{name} loaded: {} of {} imports redirected", patch_imports(base, names), names.len()));
}

unsafe extern "system" fn on_load(reason: u32, data: *const LoadedNotification, _: *mut c_void) {
    if reason != LDR_DLL_NOTIFICATION_REASON_LOADED || data.is_null() { return; }
    let name = &*(*data).base_name;
    patch_module(&String::from_utf16_lossy(std::slice::from_raw_parts(name.buffer, name.length as usize / 2)), (*data).base);
}

#[no_mangle]
pub unsafe extern "system" fn DllMain(module: Handle, reason: u32, _: *mut c_void) -> i32 {
    if reason != DLL_PROCESS_ATTACH { return 1; }
    DisableThreadLibraryCalls(module);
    let mode = std::env::var("AF_PROTON_LOADER").unwrap_or_default().to_ascii_lowercase();
    let ntdll = GetModuleHandleW(wide("ntdll.dll").as_ptr());
    let wine = !GetProcAddress(ntdll, b"wine_get_version\0".as_ptr()).is_null();
    if mode == "off" || !(wine || mode == "force") {
        log("not under Wine (or AF_PROTON_LOADER=off): inert");
        return 1;
    }
    let image = GetModuleHandleW(std::ptr::null()) as usize;
    let size = (image as *const u8).add(*((image + 0x3C) as *const u32) as usize + 0x50).cast::<u32>().read_unaligned() as usize;
    let end = (image + size + GRANULE - 1) & !(GRANULE - 1);
    let below = reserve(image - GRANULE, image.saturating_sub(BELOW_SEARCH), -(GRANULE as isize), BELOW);
    let above = reserve(end, end + ABOVE_SEARCH, GRANULE as isize, ABOVE);
    let (low, high) = { let p = pool(); (p.first().map_or(0, |g| g.address), p.last().map_or(0, |g| g.address + GRANULE)) };
    log(&format!("{} image {image:#x}+{size:#x}: reserved {below} spare granules below it and {above} above, {low:#x}..{high:#x}",
                 if wine { "Wine" } else { "forced" }));
    // Already loaded (a test harness loading this through ctypes): patch them now.
    for name in [CTYPES, "_cyminhook.cp311-win_amd64.pyd"] {
        let base = GetModuleHandleW(wide(name).as_ptr()) as usize;
        if base != 0 { patch_module(name, base); }
    }
    let register = GetProcAddress(ntdll, b"LdrRegisterDllNotification\0".as_ptr());
    if register.is_null() { log("no LdrRegisterDllNotification: inert"); return 1; }
    let register: unsafe extern "system" fn(u32, NotificationCallback, *mut c_void, *mut *mut c_void) -> i32 =
        std::mem::transmute(register);
    let mut cookie = std::ptr::null_mut();
    let status = register(0, on_load, std::ptr::null_mut(), &mut cookie);
    if status != 0 { log(&format!("LdrRegisterDllNotification failed {status:#x}: inert")); }
    1
}
