fn main() {
    // winmm.def gives every stub the system DLL's export name and ordinal (proxy/gen_exports.py).
    let def = std::path::Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("winmm.def");
    println!("cargo:rerun-if-changed=winmm.def");
    println!("cargo:rustc-cdylib-link-arg=/DEF:{}", def.display());
}
