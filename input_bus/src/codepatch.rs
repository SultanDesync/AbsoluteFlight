//! In-process code swap for the release mod (T100, replaces the `tools/patch_call.py` subprocess).
//!
//! Same method as `native_roll_patch.replace_code`: suspend every other thread in this process, refuse
//! while any instruction pointer is inside the bytes, compare the expected bytes exactly, write, read
//! back, flush, resume. Python can't do this in-process because a suspended thread may hold the GIL or
//! the heap lock. This code never touches either between suspend and resume: every buffer is allocated
//! before the first SuspendThread, and ctypes releases the GIL for the call.

#[cfg(windows)]
mod win {
    use std::ffi::c_void;

    pub const TH32CS_SNAPTHREAD: u32 = 0x4;
    pub const THREAD_ACCESS: u32 = 0x4A; // SUSPEND_RESUME | GET_CONTEXT | QUERY_INFORMATION
    pub const CONTEXT_CONTROL: u32 = 0x100001;
    pub const PAGE_EXECUTE_READWRITE: u32 = 0x40;
    pub const INVALID_HANDLE: isize = -1;

    #[repr(C)]
    pub struct ThreadEntry32 {
        pub size: u32,
        pub usage: u32,
        pub thread_id: u32,
        pub owner_process_id: u32,
        pub base_priority: i32,
        pub delta_priority: i32,
        pub flags: u32,
    }

    /// x64 CONTEXT: 0x4D0 bytes, 16-byte aligned; ContextFlags at +0x30, Rip at +0xF8.
    #[repr(C, align(16))]
    pub struct Context(pub [u8; 0x4D0]);

    #[link(name = "kernel32")]
    extern "system" {
        pub fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> isize;
        pub fn Thread32First(snapshot: isize, entry: *mut ThreadEntry32) -> i32;
        pub fn Thread32Next(snapshot: isize, entry: *mut ThreadEntry32) -> i32;
        pub fn CloseHandle(handle: isize) -> i32;
        pub fn GetCurrentProcessId() -> u32;
        pub fn GetCurrentThreadId() -> u32;
        pub fn GetCurrentProcess() -> isize;
        pub fn OpenThread(access: u32, inherit: i32, thread_id: u32) -> isize;
        pub fn SuspendThread(thread: isize) -> u32;
        pub fn ResumeThread(thread: isize) -> u32;
        pub fn GetThreadContext(thread: isize, context: *mut Context) -> i32;
        pub fn VirtualProtect(address: *mut c_void, size: usize, protect: u32, old: *mut u32) -> i32;
        pub fn FlushInstructionCache(process: isize, address: *const c_void, size: usize) -> i32;
    }
}

/// Result codes for `AbsoluteInputBus_PatchCode`. Non-negative is success.
pub const PATCHED: i32 = 0;
pub const ALREADY: i32 = 1;
pub const BAD_ARGUMENTS: i32 = -1;
pub const EXPECTED_MISMATCH: i32 = -2;
pub const BUSY: i32 = -3;
pub const OS_ERROR: i32 = -4;
pub const READBACK_MISMATCH: i32 = -5;
pub const PANIC: i32 = -6;

const ATTEMPTS: u32 = 20;

/// Swap `len` (1..=16) bytes at `site` from `expected` to `replacement`. `threads_out` (optional)
/// receives how many threads were held. Returns `ALREADY` if `replacement` is already in place.
///
/// # Safety
/// `site` must be committed code in this process; `expected`/`replacement` must point to `len` bytes.
#[cfg(windows)]
pub unsafe fn patch_code(site: usize, expected: &[u8], replacement: &[u8], threads_out: Option<&mut u32>) -> i32 {
    let len = expected.len();
    if len != replacement.len() || !(1..=16).contains(&len) || site == 0 {
        return BAD_ARGUMENTS;
    }
    let current = std::slice::from_raw_parts(site as *const u8, len);
    if current == replacement {
        return ALREADY;
    }
    let threads = match other_threads() {
        Some(threads) => threads,
        None => return OS_ERROR,
    };
    let mut handles: Vec<isize> = Vec::with_capacity(threads.len());
    let mut context = Box::new(win::Context([0; 0x4D0]));
    for _ in 0..ATTEMPTS {
        let outcome = with_threads_held(&threads, &mut handles, &mut context, site, len, || write(site, expected, replacement));
        match outcome {
            Err(BUSY) => std::thread::sleep(std::time::Duration::from_millis(10)),
            Err(code) => return code,
            Ok(code) => {
                if let Some(out) = threads_out {
                    *out = handles.len() as u32;
                }
                return code;
            }
        }
    }
    BUSY
}

#[cfg(windows)]
fn other_threads() -> Option<Vec<u32>> {
    use win::*;
    unsafe {
        let (pid, own) = (GetCurrentProcessId(), GetCurrentThreadId());
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snapshot == INVALID_HANDLE {
            return None;
        }
        let mut entry = ThreadEntry32 { size: std::mem::size_of::<ThreadEntry32>() as u32, usage: 0, thread_id: 0,
                                        owner_process_id: 0, base_priority: 0, delta_priority: 0, flags: 0 };
        let mut threads = Vec::with_capacity(256);
        let mut more = Thread32First(snapshot, &mut entry) != 0;
        while more {
            if entry.owner_process_id == pid && entry.thread_id != own {
                threads.push(entry.thread_id);
            }
            more = Thread32Next(snapshot, &mut entry) != 0;
        }
        CloseHandle(snapshot);
        Some(threads)
    }
}

/// Suspend `threads`, check none is inside `site..site+len`, run `f`, resume. No allocation while held:
/// `handles` has its capacity already and `context` is reused.
#[cfg(windows)]
fn with_threads_held(threads: &[u32], handles: &mut Vec<isize>, context: &mut win::Context, site: usize, len: usize,
                     f: impl FnOnce() -> i32) -> Result<i32, i32> {
    use win::*;
    handles.clear();
    let mut result = Ok(PATCHED);
    unsafe {
        for &thread_id in threads {
            let handle = OpenThread(THREAD_ACCESS, 0, thread_id);
            if handle == 0 {
                continue; // exited since the snapshot
            }
            if SuspendThread(handle) == u32::MAX {
                CloseHandle(handle);
                result = Err(OS_ERROR);
                break;
            }
            handles.push(handle);
        }
        if result.is_ok() {
            for &handle in handles.iter() {
                context.0.fill(0);
                context.0[0x30..0x34].copy_from_slice(&CONTEXT_CONTROL.to_le_bytes());
                if GetThreadContext(handle, context) == 0 {
                    result = Err(OS_ERROR);
                    break;
                }
                let rip = u64::from_le_bytes(context.0[0xF8..0x100].try_into().unwrap()) as usize;
                if site < rip && rip < site + len {
                    result = Err(BUSY);
                    break;
                }
            }
        }
        if result.is_ok() {
            let code = f();
            result = if code < 0 { Err(code) } else { Ok(code) };
        }
        for &handle in handles.iter().rev() {
            ResumeThread(handle);
            CloseHandle(handle);
        }
    }
    result
}

#[cfg(windows)]
fn write(site: usize, expected: &[u8], replacement: &[u8]) -> i32 {
    use win::*;
    let len = expected.len();
    unsafe {
        let bytes = std::slice::from_raw_parts_mut(site as *mut u8, len);
        if bytes != expected {
            return EXPECTED_MISMATCH;
        }
        let mut old = 0u32;
        if VirtualProtect(site as *mut _, len, PAGE_EXECUTE_READWRITE, &mut old) == 0 {
            return OS_ERROR;
        }
        let process = GetCurrentProcess();
        bytes.copy_from_slice(replacement);
        let mut code = PATCHED;
        if bytes != replacement {
            code = READBACK_MISMATCH;
        } else if FlushInstructionCache(process, site as *const _, len) == 0 {
            code = OS_ERROR;
        }
        if code != PATCHED {
            bytes.copy_from_slice(expected);
            FlushInstructionCache(process, site as *const _, len);
        }
        let mut ignored = 0u32;
        if VirtualProtect(site as *mut _, len, old, &mut ignored) == 0 && code == PATCHED {
            code = OS_ERROR;
        }
        code
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    #[link(name = "kernel32")]
    extern "system" {
        fn VirtualAlloc(address: *mut std::ffi::c_void, size: usize, kind: u32, protect: u32) -> *mut u8;
    }

    #[test]
    fn swaps_and_restores_with_other_threads_running() {
        let page = unsafe { VirtualAlloc(std::ptr::null_mut(), 0x1000, 0x3000, 0x20) } as usize; // PAGE_EXECUTE_READ
        assert_ne!(page, 0);
        let original = [0xE8, 0x8F, 0xEA, 0xEA, 0x00];
        let patch = [0xE8, 0x11, 0x22, 0x33, 0x44];
        unsafe {
            let mut old = 0;
            win::VirtualProtect(page as *mut _, 5, 0x40, &mut old);
            std::ptr::copy_nonoverlapping(original.as_ptr(), page as *mut u8, 5);
            win::VirtualProtect(page as *mut _, 5, 0x20, &mut old);
        }
        let stop = Arc::new(AtomicBool::new(false));
        let spinners: Vec<_> = (0..4).map(|_| {
            let stop = stop.clone();
            std::thread::spawn(move || while !stop.load(Ordering::Relaxed) { std::hint::spin_loop() })
        }).collect();
        unsafe {
            assert_eq!(patch_code(page, &[0xE8, 0xDE, 0xAD, 0xBE, 0xEF], &patch, None), EXPECTED_MISMATCH);
            let mut held = 0;
            assert_eq!(patch_code(page, &original, &patch, Some(&mut held)), PATCHED);
            assert!(held >= 4);
            assert_eq!(std::slice::from_raw_parts(page as *const u8, 5), &patch);
            assert_eq!(patch_code(page, &original, &patch, None), ALREADY);
            assert_eq!(patch_code(page, &patch, &original, None), PATCHED);
            assert_eq!(std::slice::from_raw_parts(page as *const u8, 5), &original);
            assert_eq!(patch_code(page, &original, &patch[..4], None), BAD_ARGUMENTS);
        }
        stop.store(true, Ordering::Relaxed);
        for spinner in spinners {
            spinner.join().unwrap();
        }
    }
}
