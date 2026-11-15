"""RT1-L0 host receipt: toolchain (PATH and ~/.cargo/bin probed separately), cpuinfo features, per-cpu part, GIL, numpy, /dev/shm.
A missing tool is the literal string AUSENTE plus the OPERATOR install line; workers never install anything.
Usage: python3 tools/rt_host.py [--out eval/results/rt1_host.json]"""
import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig

AUSENTE = "AUSENTE"
TOOLS = ("cargo", "rustc", "maturin", "rustup", "clang")
CARGO_PATH_LINE = "export PATH=$HOME/.cargo/bin:$PATH"
INSTALL = {
    "maturin": "python3 -m pip install --user 'maturin>=1.7,<2'",
    "clang": "OPERATOR (optional): install clang; fallback route is gcc + arm_neon.h via ctypes",
}
CRATES_LINE = "cd rust/visioncore_native && ~/.cargo/bin/cargo fetch   # network; offline crates AUSENTE until fetched"
FEATURES = ("asimd", "sve", "sve2")


def _version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        txt = (r.stdout or r.stderr).strip().splitlines()
        return txt[0] if r.returncode == 0 and txt else AUSENTE
    except (OSError, subprocess.SubprocessError):
        return AUSENTE


def probe_tool(name, path_env, cargo_bin):
    """-> {path:{path,version}|AUSENTE..., cargo_bin:{...}}; the two locations never mix."""
    out = {}
    for where, found in (("PATH", shutil.which(name, path=path_env)),
                         ("cargo_bin", shutil.which(name, path=cargo_bin))):
        out[where] = {"path": found, "version": _version(found)} if found else {"path": AUSENTE, "version": AUSENTE}
    return out


def install_line(name, probes):
    on_path = probes["PATH"]["path"] != AUSENTE
    in_cargo = probes["cargo_bin"]["path"] != AUSENTE
    if on_path:
        return None
    if name in ("cargo", "rustc", "rustup"):
        if in_cargo:
            return CARGO_PATH_LINE
        return "OPERATOR: install rustup (https://rustup.rs), then " + CARGO_PATH_LINE
    return INSTALL.get(name, "OPERATOR: install " + name)


def parse_cpuinfo(text):
    """-> (features_per_cpu {cpu:set}, part {cpu:'0x..'}); only 'processor', 'Features', 'CPU part' lines are read."""
    feats, parts, cur = {}, {}, None
    for line in text.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "processor" and v.isdigit():
            cur = int(v)
        elif cur is not None and k == "Features":
            feats[cur] = set(v.split())
        elif cur is not None and k == "CPU part":
            parts[cur] = v
    return feats, parts


def cpu_receipt(text):
    feats, parts = parse_cpuinfo(text)
    every = bool(feats)
    flags = {f: every and all(f in s for s in feats.values()) for f in FEATURES}
    by_part = {}
    for c, p in sorted(parts.items()):
        by_part.setdefault(p, []).append(c)
    return {"features": flags, "cpu_part": {str(c): p for c, p in sorted(parts.items())}, "cpus_by_part": by_part}


def shm_receipt(path="/dev/shm"):
    try:
        sv = os.statvfs(path)
    except OSError:
        return {"path": path, "statvfs": AUSENTE}
    return {"path": path, "total_bytes": sv.f_blocks * sv.f_frsize, "free_bytes": sv.f_bavail * sv.f_frsize}


def receipt(path_env=None, home=None, cpuinfo_text=None, shm_path="/dev/shm"):
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    home = os.path.expanduser("~") if home is None else home
    cargo_bin = os.path.join(home, ".cargo", "bin")
    if cpuinfo_text is None:
        try:
            cpuinfo_text = open("/proc/cpuinfo", encoding="utf-8", errors="replace").read()
        except OSError:
            cpuinfo_text = ""
    tools, operator = {}, []
    for t in TOOLS:
        pr = probe_tool(t, path_env, cargo_bin)
        line = install_line(t, pr)
        absent = pr["PATH"]["path"] == AUSENTE and pr["cargo_bin"]["path"] == AUSENTE
        pr["status"] = "ok" if line is None else (AUSENTE if absent else "not_on_PATH")
        pr["operator_install"] = line
        if line and line not in operator:
            operator.append(line)
        tools[t] = pr
    try:
        npv = importlib.metadata.version("numpy")   # metadata only: tools/ stays stdlib-only (g_layer)
    except importlib.metadata.PackageNotFoundError:
        npv = AUSENTE
    gil = sysconfig.get_config_var("Py_GIL_DISABLED")
    return {"schema": "rt1.host/v1", "tools": tools, "operator_install": operator, "crates_offline": AUSENTE,
            "crates_fetch_line": CRATES_LINE, "cpu": cpu_receipt(cpuinfo_text),
            "python": {"version": sys.version.split()[0], "Py_GIL_DISABLED": gil, "numpy": npv},
            "shm": shm_receipt(shm_path)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    txt = json.dumps(receipt(), indent=2, sort_keys=True) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
    else:
        sys.stdout.write(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
