fn main() {
    // c_dfDIJoystick2 is static data in dinput8.lib (import lib + data). Plain kind, so the MSVC
    // linker resolves it from the Windows SDK LIB path that rustc sets up (static= needs an -L path).
    if std::env::var("CARGO_CFG_WINDOWS").is_ok() {
        println!("cargo:rustc-link-lib=dinput8");
        println!("cargo:rustc-link-lib=dxguid");
    }
}
