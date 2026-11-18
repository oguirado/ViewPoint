use std::{env, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-changed=vp_capture.h");
    println!("cargo:rerun-if-changed=../../contracts/vp-toro-1/v1/bulk_v1.h");
    println!("cargo:rerun-if-env-changed=CC");
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR"));
    let compiler = env::var_os("CC").unwrap_or_else(|| "cc".into());
    let mut objects = Vec::new();
    for source in ["x11", "bulk_linux"] {
        let path = format!("src/{source}.c");
        println!("cargo:rerun-if-changed={path}");
        let object = out.join(format!("{source}.o"));
        let status = Command::new(&compiler)
            .args([
                "-std=c11", "-O2", "-fPIC", "-Wall", "-Wextra", "-Werror", "-c",
            ])
            .arg(path)
            .arg("-o")
            .arg(&object)
            .status()
            .expect("run cc with the installed Linux and XCB headers");
        assert!(status.success(), "compile {source}");
        objects.push(object);
    }
    let status = Command::new("ar")
        .arg("crs")
        .arg(out.join("libvp_capture_native.a"))
        .args(objects)
        .status()
        .expect("run ar");
    assert!(status.success(), "archive native capture support");
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=vp_capture_native");
    for library in ["xcb-shm", "xcb", "crypto"] {
        println!("cargo:rustc-link-lib={library}");
    }
}
