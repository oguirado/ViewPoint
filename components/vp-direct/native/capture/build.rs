use std::{env, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-changed=vp_capture.h");
    println!("cargo:rerun-if-changed=src/x11.c");
    println!("cargo:rerun-if-env-changed=CC");
    println!("cargo:rerun-if-env-changed=AR");

    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR"));
    let compiler = env::var_os("CC").unwrap_or_else(|| "cc".into());
    let object = out.join("x11.o");
    let status = Command::new(compiler)
        .args([
            "-std=c11",
            "-O2",
            "-fPIC",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-c",
            "src/x11.c",
        ])
        .arg("-o")
        .arg(&object)
        .status()
        .expect("run cc with the installed Linux and XCB headers");
    assert!(status.success(), "compile src/x11.c");

    let archiver = env::var_os("AR").unwrap_or_else(|| "ar".into());
    let status = Command::new(archiver)
        .arg("crs")
        .arg(out.join("libvp_capture_x11.a"))
        .arg(object)
        .status()
        .expect("archive native capture transport");
    assert!(status.success(), "archive native capture transport");
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=vp_capture_x11");
    for library in ["xcb-shm", "xcb", "crypto"] {
        println!("cargo:rustc-link-lib={library}");
    }
}
