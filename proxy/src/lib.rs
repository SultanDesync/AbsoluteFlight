//! winmm.dll proxy for No Man's Sky (D22, T100).
//!
//! NMS.exe imports WINMM.dll and Binaries ships none, so this copy loads first. Every export jumps
//! through a table filled from System32\winmm.dll at attach (src/exports.rs, generated). Then a thread
//! of our own (never DllMain) finds the Absolute Flight folder, loads its embedded python311.dll and
//! runs boot_inproc.py there. Anything missing or failing is logged and the game plays vanilla.
#![allow(non_snake_case, clippy::missing_safety_doc)]

mod exports;

use std::ffi::{c_void, OsString};
use std::io::Write;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

type Handle = *mut c_void;

#[link(name = "kernel32")]
extern "system" {
    fn GetSystemDirectoryW(buffer: *mut u16, size: u32) -> u32;
    fn GetModuleFileNameW(module: Handle, buffer: *mut u16, size: u32) -> u32;
    fn LoadLibraryW(name: *const u16) -> Handle;
    fn LoadLibraryExW(name: *const u16, file: Handle, flags: u32) -> Handle;
    fn GetProcAddress(module: Handle, name: *const u8) -> *mut c_void;
    fn DisableThreadLibraryCalls(module: Handle) -> i32;
    fn CreateThread(attrs: *mut c_void, stack: usize, start: extern "system" fn(*mut c_void) -> u32,
                    param: *mut c_void, flags: u32, id: *mut u32) -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
    fn GetLocalTime(time: *mut [u16; 8]);
}

#[link(name = "bcrypt")]
extern "system" {
    fn BCryptGenRandom(algorithm: Handle, buffer: *mut u8, len: u32, flags: u32) -> i32;
}

const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 2;
static mut RNG_WARM: i32 = -1;

const DLL_PROCESS_ATTACH: u32 = 1;
const LOAD_WITH_ALTERED_SEARCH_PATH: u32 = 0x8;
const BOOT_SCRIPT: &str = "boot_inproc.py";

fn wide(path: &Path) -> Vec<u16> {
    path.as_os_str().encode_wide().chain(Some(0)).collect()
}

fn module_path(module: Handle) -> PathBuf {
    let mut buffer = vec![0u16; 32768];
    let len = unsafe { GetModuleFileNameW(module, buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    PathBuf::from(OsString::from_wide(&buffer[..len]))
}

fn data_dir() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(|base| PathBuf::from(base).join("AbsoluteHOTAS"))
}

fn log(message: &str) {
    let Some(dir) = data_dir().map(|d| d.join("logs")) else { return };
    let _ = std::fs::create_dir_all(&dir);
    let mut t = [0u16; 8];
    unsafe { GetLocalTime(&mut t) };
    if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("proxy.log")) {
        let _ = writeln!(file, "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03} {message}",
                         t[0], t[1], t[3], t[4], t[5], t[6], t[7]);
    }
}

/// Fill the jump table from the real winmm. Runs under the loader lock, like every proxy of this kind.
unsafe fn resolve_real_winmm() -> bool {
    let mut buffer = [0u16; 260];
    let len = GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) as usize;
    if len == 0 || len >= buffer.len() { return false; }
    let path = PathBuf::from(OsString::from_wide(&buffer[..len])).join("winmm.dll");
    let real = LoadLibraryW(wide(&path).as_ptr());
    if real.is_null() { return false; }
    let table = &mut *std::ptr::addr_of_mut!(exports::AF_WINMM_TABLE);
    for (slot, (name, ordinal)) in table.iter_mut().zip(exports::EXPORTS.iter()) {
        let key = match name { Some(name) => name.as_ptr(), None => *ordinal as usize as *const u8 };
        *slot = GetProcAddress(real, key) as usize;
    }
    true
}

/// The Absolute Flight folder: ABSOLUTE_FLIGHT_HOME, else the path in <data>\install-path.txt,
/// else <game>\GAMEDATA\MODS\AbsoluteFlight. It must hold python\python311.dll and boot_inproc.py.
fn find_home(binaries: &Path) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(home) = std::env::var_os("ABSOLUTE_FLIGHT_HOME") { candidates.push(PathBuf::from(home)); }
    if let Some(text) = data_dir().and_then(|d| std::fs::read_to_string(d.join("install-path.txt")).ok()) {
        let line = text.trim().trim_start_matches('\u{feff}');
        if !line.is_empty() { candidates.push(PathBuf::from(line)); }
    }
    if let Some(game) = binaries.parent() { candidates.push(game.join("GAMEDATA").join("MODS").join("AbsoluteFlight")); }
    for home in candidates {
        let ok = home.join("python").join("python311.dll").is_file() && home.join(BOOT_SCRIPT).is_file();
        log(&format!("home candidate {}: {}", home.display(), if ok { "ok" } else { "not a release folder" }));
        if ok { return Some(home); }
    }
    None
}

unsafe fn start_python(home: &Path) -> Result<i32, String> {
    let python = home.join("python");
    let dll = LoadLibraryExW(wide(&python.join("python311.dll")).as_ptr(), std::ptr::null_mut(), LOAD_WITH_ALTERED_SEARCH_PATH);
    if dll.is_null() { return Err("python311.dll did not load".into()); }
    let proc = |name: &[u8]| {
        let p = GetProcAddress(dll, name.as_ptr());
        if p.is_null() { Err(format!("python311.dll has no {}", String::from_utf8_lossy(&name[..name.len() - 1]))) } else { Ok(p) }
    };
    let set_home: extern "C" fn(*const u16) = std::mem::transmute(proc(b"Py_SetPythonHome\0")?);
    let initialize: extern "C" fn(i32) = std::mem::transmute(proc(b"Py_InitializeEx\0")?);
    let is_initialized: extern "C" fn() -> i32 = std::mem::transmute(proc(b"Py_IsInitialized\0")?);
    let run: extern "C" fn(*const u8) -> i32 = std::mem::transmute(proc(b"PyRun_SimpleString\0")?);
    if is_initialized() != 0 { return Err("Python is already running in this process (Play.bat too?)".into()); }
    // Ignore PYTHONHOME/PYTHONPATH and the user site, and write no bytecode next to the install.
    for flag in [&b"Py_IgnoreEnvironmentFlag\0"[..], b"Py_NoUserSiteDirectory\0", b"Py_DontWriteBytecodeFlag\0"] {
        *(proc(flag)? as *mut i32) = 1;
    }
    set_home(Box::leak(wide(&python).into_boxed_slice()).as_ptr()); // Python keeps the pointer
    initialize(0); // 0: leave the game's signal handlers alone
    // The folder goes in as hex so no path character can break the string.
    let hex: String = home.as_os_str().to_string_lossy().bytes().map(|b| format!("{b:02x}")).collect();
    let code = format!(
        "AF_HOME = bytes.fromhex('{hex}').decode('utf-8')\n\
         exec(compile(open(AF_HOME + r'\\{BOOT_SCRIPT}', 'rb').read(), AF_HOME + r'\\{BOOT_SCRIPT}', 'exec'))\n\0");
    // Blocks for the life of the game once the mod is running (pyMHF's event loop), like pyMHF's injected thread.
    Ok(run(code.as_ptr()))
}

extern "system" fn boot(_: *mut c_void) -> u32 {
    let exe = module_path(std::ptr::null_mut());
    log(&format!("proxy loaded in {} (bcrypt RNG warm-up status {:#x})", exe.display(), unsafe { RNG_WARM }));
    let is_nms = exe.file_name().map(|n| n.to_string_lossy().eq_ignore_ascii_case("NMS.exe")).unwrap_or(false);
    if !is_nms { log("not NMS.exe: staying inert"); return 0; }
    let Some(home) = exe.parent().and_then(find_home) else {
        log("no Absolute Flight folder found: staying inert");
        return 0;
    };
    match unsafe { start_python(&home) } {
        Ok(code) => log(&format!("boot_inproc.py returned {code} (0: mod stopped or chose to stay off; see python-boot.log)")),
        Err(error) => log(&format!("{error}: staying inert")),
    }
    0
}

#[no_mangle]
pub unsafe extern "system" fn DllMain(module: Handle, reason: u32, _: *mut c_void) -> i32 {
    if reason == DLL_PROCESS_ATTACH {
        DisableThreadLibraryCalls(module);
        if !resolve_real_winmm() {
            log("could not load System32\\winmm.dll");
            return 0; // fail the load rather than hand the game null functions
        }
        // Load bcrypt's system RNG now, under the loader lock. Py_InitializeEx calls BCryptGenRandom for its
        // hash seed; done cold on the boot thread that takes bcrypt's provider lock, then the loader lock,
        // while Steam's overlay holds the loader lock and wants bcrypt's: a deadlock (live, 2026-10-24).
        let mut seed = [0u8; 8];
        RNG_WARM = BCryptGenRandom(std::ptr::null_mut(), seed.as_mut_ptr(), 8, BCRYPT_USE_SYSTEM_PREFERRED_RNG);
        // Starts once the loader lock is released, so never runs inside DllMain.
        let thread = CreateThread(std::ptr::null_mut(), 0, boot, std::ptr::null_mut(), 0, std::ptr::null_mut());
        if !thread.is_null() { CloseHandle(thread); }
    }
    1
}
