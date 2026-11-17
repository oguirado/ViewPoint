"""Bake -> UV -> linear ORM -> covered-texel PBR, publishing one immutable bundle.

Run with an explicit component import path (also supported by the test bootstrap).
The result is verified UV geometry and maps, not a finished uv_texture slot result:
final GLB material binding, geometric regression and appearance are separate gates.
Existing destinations are refused; all imported writers see owned scratch paths.
"""
import argparse
import fcntl
import functools
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading

from .bake_support import (Refusal, canonical, core, load_record, read_bytes, require_receipt, result,
                           safe_glb, sha, upstream, write_record)
from .orm import pack_maps
from .uv import check_uv
from . import topology

MEMORY_SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
                "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]


def scoped_runner(argv, timeout):
    # Keep the upstream three-value runner contract on the guarded host path.
    try:
        code, output = topology.default_runner(MEMORY_SCOPE + argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise Refusal(6, "deadline_exceeded") from exc
    return code, output, ""


def _tool_files():
    paths = {Path(upstream(n).__file__).resolve() for n in
             ("bake_pbr", "glb_core", "orm_pack", "pbr_validate", "uv_gate", "mesh_gate", "limits", "turntable")}
    paths.add(upstream("bake_pbr").WORKER.resolve())
    component = Path(__file__).resolve().parents[2]
    sibling = Path(upstream("bake_pbr").__file__).resolve().parents[2]
    paths.update(Path(__file__).resolve().with_name(name) for name in ("bake.py", "bake_support.py", "orm.py", "uv.py"))
    paths.add(Path(core.__file__).resolve())
    paths.update(Path(module.__file__).resolve() for module in
                 (topology, topology.af_proc, topology.resources))
    files = []
    for p in paths:
        root, prefix = (component, "component") if p.is_relative_to(component) else (sibling, "sibling")
        files.append({"path": prefix + "/" + p.relative_to(root).as_posix(), "sha256": sha(read_bytes(p))})
    files.append({"path": "runtime/blender", "sha256": sha(read_bytes(Path(upstream("bake_pbr").BLENDER).resolve()))})
    return sorted(files, key=lambda f: f["path"])


def _bounded(function):
    """Own a host deadline and restore signal state; refuse unsupported threads."""
    @functools.wraps(function)
    def run(*args, **kwargs):
        timeout = kwargs.get("timeout_s", 300)
        if type(timeout) is not int or timeout <= 0:
            return result(2, "policy_invalid")
        if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
            return result(3, "deadline_unavailable")
        def expired(signum, frame):
            raise Refusal(6, "terminated" if signum == signal.SIGTERM else "deadline_exceeded")
        previous = {sig: signal.signal(sig, expired) for sig in (signal.SIGTERM, signal.SIGALRM)}
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return function(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in previous.items(): signal.signal(sig, handler)
    return run


def _check_ram(path):
    """The foreign RAM record is telemetry, not a domain PASS receipt."""
    rec = load_record(read_bytes(path))
    if not isinstance(rec, dict) or rec.get("schema") != "bvqa.asset-ram/v1":
        raise Refusal(2, "ram_receipt_invalid")
    if ("rc" in rec and (type(rec["rc"]) is not int or rec["rc"] != 0)) or any(
            key in rec and rec[key] != "PASS" for key in ("status", "verdict")):
        raise Refusal(3, "ram_receipt_not_pass")
    keys = ("ram_peak_bytes", "quota", "released_bytes", "swap_used_delta")
    if any(type(rec.get(k)) is not int for k in keys) or rec.get("fstype") != "tmpfs":
        raise Refusal(3, "ram_telemetry_unknown")
    if rec["quota"] <= 0 or rec["ram_peak_bytes"] < 0 or rec["released_bytes"] < 0:
        raise Refusal(2, "ram_telemetry_invalid")
    if rec["ram_peak_bytes"] > rec["quota"] or rec.get("warnings"):
        raise Refusal(6, "ram_budget_exceeded")


def _check_bake(receipt, tool, staging, high, low, res):
    """Re-evaluate bytes independently of worker messages or receipt verdicts."""
    require_receipt(0, receipt, tool.SCHEMA)
    if receipt.get("input_sha256") != sha(read_bytes(low)) or receipt.get("high_sha256") != sha(read_bytes(high)):
        raise Refusal(4, "bake_input_drift")
    scene = upstream("glb_core").parse_glb(read_bytes(low))
    checks, failed = tool.verify_outputs(staging, scene, res)
    if failed:
        raise Refusal(5, "bake_output_failed")
    if not checks.get("coverage_ppm") or not checks.get("maps"):
        raise Refusal(3, "bake_zero_checks")
    if receipt.get("checks") != checks:
        raise Refusal(4, "bake_receipt_drift")
    return checks


def _pbr(staging, bake_checks, previous):
    """Run the imported PBR checks over *covered* albedo, plus the full ORM.

    Bake gutters/uncovered background are not dielectric material observations.
    Persist the coverage-derived probe so the receipt preimage is explicit.
    """
    b, g, png = upstream("bake_pbr"), upstream("glb_core"), upstream("orm_pack")
    scene = g.parse_glb(read_bytes(staging / "low_uv.glb"))
    res = bake_checks["maps"]["albedo"]["w"]
    cov = sorted(b.coverage(b.uv_tris(scene), res))
    w, _, ch, pixels = png.decode_png(read_bytes(staging / "albedo.png"))
    if not cov or ch < 3:
        raise Refusal(3, "albedo_zero_checks")
    selected = [pixels[(y * w + x) * ch:(y * w + x) * ch + 3] + b"\xff" for x, y in cov]
    tool = upstream("pbr_validate")
    # Gate the exact coverage before padding the persisted probe. Padding must
    # never dilute an invalid-texel fraction at the policy boundary.
    bad = 0
    for start in range(0, len(selected), res):
        row = selected[start:start + res]
        metrics, failures = tool.check_albedo_png(png.encode_png(len(row), 1, b"".join(row)))
        if set(failures) - {"albedo_texture"}:
            raise Refusal(5, "covered_albedo_failed")
        bad += metrics["out_of_range_px"]
    if bad * tool.PPM // len(selected) > tool.ALBEDO_OUT_MAX_PPM:
        raise Refusal(5, "covered_albedo_range")
    # Declared neutral padding cannot amplify an isolated bad covered texel.
    selected += [bytes((128, 128, 128, 255))] * (res * res - len(selected))
    probe = staging / "covered_albedo.png"
    probe.write_bytes(png.encode_png(res, res, b"".join(selected)))
    rc, rec = tool.run(["--albedo", str(probe), "--orm", str(staging / "orm.png"),
                        "--prev-receipt", str(previous)])
    require_receipt(rc, rec, tool.SCHEMA, link="prev_sha256", previous=sha(read_bytes(previous)))
    if type(rec.get("checks")) is not int or rec["checks"] != 2:
        raise Refusal(3, "pbr_zero_checks")
    if rec.get("input_sha256") != sha(b"\0".join([read_bytes(probe), read_bytes(staging / "orm.png")])):
        raise Refusal(4, "pbr_input_drift")
    return rec


def verify_bundle(bundle, *, expected_receipt_sha256, production=False):
    """Rehash all committed bytes against an externally retained receipt digest.

    This is byte-integrity verification, not a replay of the semantic gates.
    """
    try:
        root = Path(bundle)
        if root.is_symlink():
            raise Refusal(2, "symlink_refused")
        raw = read_bytes(root / "bundle_receipt.json")
        if sha(raw) != expected_receipt_sha256:
            raise Refusal(4, "bundle_receipt_drift")
        rec = load_record(raw)
        if not isinstance(rec, dict) or type(rec.get("rc")) is not int:
            raise Refusal(2, "bundle_record_invalid")
        if rec.get("rc") != 0 or rec.get("status") != "OK" or not rec.get("files"):
            raise Refusal(3, "bundle_incomplete")
        if production and rec.get("runner", {}).get("kind") != "scoped-cpu":
            raise Refusal(3, "injected_runner")
        if production:
            # F2-U3 has no frozen factory manifest/schema assignment. Integrity
            # and a real runner cannot establish factory or asset admission.
            raise Refusal(3, "factory_manifest_schema_unassigned")
        if not rec.get("tool_files"):
            raise Refusal(3, "toolchain_unknown")
        # Never follow arbitrary tool paths supplied by a receipt.
        if rec["tool_files"] != _tool_files():
            raise Refusal(4, "toolchain_drift")
        names = set()
        for entry in rec["files"]:
            name = entry["path"]
            if not isinstance(name, str) or Path(name).name != name or name in names:
                raise Refusal(2, "bundle_path_invalid")
            names.add(name)
            if sha(read_bytes(root / name)) != entry["sha256"]:
                raise Refusal(4, "bundle_bytes_drift")
        if {p.name for p in root.iterdir()} != names | {"bundle_receipt.json"}:
            raise Refusal(4, "bundle_inventory_drift")
        _check_ram(root / "ram_receipt.json")
        return result(0, scope="byte-integrity", production_evidence=production,
                      checks_evaluated=len(names), receipt_sha256=sha(raw))
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except Exception as exc:
        return result(2, "bundle_error", detail=str(exc))


@_bounded
def bake_bundle(high, low, destination, *, res=1024, margin_px=8, cage_extrusion_um=5000,
                timeout_s=300, max_stretch=4000, runner=None):
    """Stage result. Thresholds are explicit upstream policies, not calibration.

    runner is a test seam; CLI always launches the real bounded CPU worker.
    Previous geometry-regression receipt/API is AUSENTE; no regression PASS is
    inferred here. Raw input snapshots are retained beside the derived maps.
    """
    staging, lockfd, lock, reserved, published = None, None, None, False, None
    started = time.monotonic()
    try:
        for v in (res, margin_px, cage_extrusion_um, timeout_s, max_stretch):
            if type(v) is not int:
                raise Refusal(2, "policy_invalid")
        if res < 64 or res > 1024 or res & (res - 1) or margin_px < 0 or cage_extrusion_um < 0 or timeout_s <= 0:
            raise Refusal(2, "policy_invalid")
        dest = Path(destination)
        if dest.is_symlink() or dest.exists():
            raise Refusal(5, "destination_exists")
        if not dest.parent.is_dir():
            raise Refusal(2, "destination_parent_missing")
        source_paths = [Path(high), Path(low)]
        originals = [read_bytes(p) for p in source_paths]
        for raw in originals:
            safe_glb(raw)
        # Stable inode: never unlink a flock file while another waiter may hold it.
        lock = dest.parent / (".f2u3-lock-" + dest.name)
        try:
            lockfd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lockfd)
            lockfd = None
            raise Refusal(5, "destination_locked") from exc
        for stale in dest.parent.glob("." + dest.name + ".f2u3-*"):
            if stale.is_symlink() or stale.stat().st_uid != os.getuid():
                raise Refusal(2, "stale_staging_unowned")
            shutil.rmtree(stale)
        staging = Path(tempfile.mkdtemp(prefix="." + dest.name + ".f2u3-", dir=dest.parent))
        hp, lp = staging / "source_high.glb", staging / "source_low.glb"
        hp.write_bytes(originals[0])
        lp.write_bytes(originals[1])
        tool = upstream("bake_pbr")
        tool_files = _tool_files()
        args = ["--high", str(hp), "--low", str(lp), "--out-dir", str(staging), "--res", str(res),
                "--margin-px", str(margin_px), "--cage-extrusion-um", str(cage_extrusion_um),
                "--timeout", str(timeout_s)]
        rc, bake_rec = tool.run(args, runner=runner or scoped_runner)
        require_receipt(rc, bake_rec, tool.SCHEMA)
        _check_ram(staging / "ram_receipt.json")
        checks = _check_bake(bake_rec, tool, staging, hp, lp, res)
        write_record(staging / "bake_receipt.json", bake_rec)
        uv = check_uv(staging / "low_uv.glb", previous=staging / "bake_receipt.json", max_stretch=max_stretch)
        if uv["rc"]:
            return result(uv["rc"], *uv["reason_codes"])
        write_record(staging / "uv_receipt.json", uv["receipt"])
        orm_rec, orm_metrics = pack_maps(staging, previous=staging / "uv_receipt.json")
        write_record(staging / "orm_receipt.json", orm_rec)
        pbr_rec = _pbr(staging, checks, staging / "orm_receipt.json")
        write_record(staging / "pbr_receipt.json", pbr_rec)
        if [read_bytes(p) for p in source_paths] != originals or [read_bytes(hp), read_bytes(lp)] != originals:
            raise Refusal(4, "source_bytes_drift")
        # Recheck the entire baked map set after every downstream writer.
        _check_bake(bake_rec, tool, staging, hp, lp, res)
        files = [{"path": p.name, "sha256": sha(read_bytes(p))} for p in sorted(staging.iterdir())]
        expected = {"source_high.glb", "source_low.glb", "low_uv.glb", "orm.png", "covered_albedo.png",
                    "bake_receipt.json", "uv_receipt.json", "orm_receipt.json", "pbr_receipt.json",
                    "ram_receipt.json"} | {n + ".png" for n in tool.MAPS}
        if {f["path"] for f in files} != expected:
            raise Refusal(3, "bundle_incomplete")
        rec = result(0, scope="verified-uv-maps", files=files, tool_files=tool_files, orm=orm_metrics,
                     runner={"kind": "injected" if runner is not None else "scoped-cpu",
                             "identity": (type(runner).__module__ + "." + type(runner).__qualname__) if runner is not None else "scoped_runner"},
                     checks_evaluated=sum(m["covered"] for m in checks["maps"].values()) + orm_metrics["pixels_checked"] +
                     sum(p["uv_tris"] for p in uv["receipt"]["prims"]) + pbr_rec["checks"],
                     pending_gates=["factory_manifest_schema", "final_glb_material_binding", "raw_geometry_regression", "appearance"],
                     policy={"res": res, "margin_px": margin_px, "cage_extrusion_um": cage_extrusion_um,
                             "max_stretch_permille": max_stretch, "timeout_s": timeout_s})
        write_record(staging / "bundle_receipt.json", rec)
        digest = sha(read_bytes(staging / "bundle_receipt.json"))
        integrity = verify_bundle(staging, expected_receipt_sha256=digest)
        if integrity["rc"]:
            raise Refusal(integrity["rc"], "staged_bundle_drift")
        if time.monotonic() - started >= timeout_s:
            raise Refusal(6, "deadline_exceeded")
        if dest.exists() or dest.is_symlink():
            raise Refusal(5, "destination_exists")
        for p in staging.iterdir():
            with p.open("rb") as stream:
                os.fsync(stream.fileno())
        # Exclusively reserve the name before replacing our own empty directory.
        dest.mkdir()
        reserved = True
        os.replace(staging, dest)
        staging = None
        reserved = False
        published = dest.stat().st_ino
        parentfd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parentfd)
        finally:
            os.close(parentfd)
        published = None
        return result(0, scope=rec["scope"], bundle=str(dest), receipt_sha256=digest,
                      production_evidence=False, runtime_measured=runner is None,
                      checks_evaluated=rec["checks_evaluated"], pending_gates=rec["pending_gates"])
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except Exception as exc:
        # Imported domain exceptions do not share a base class. Preserve only
        # declared stage codes; every other unexpected boundary error is rc 2.
        value = getattr(exc, "rc", None)
        domain = tuple(cls for module, name in (("bake_pbr", "BakeError"), ("turntable", "TurntableError"))
                       if isinstance(cls := getattr(sys.modules.get("blender_visual_qa.assets." + module), name, None), type))
        code = value if isinstance(exc, domain) and type(value) is int and value in (3, 4, 5, 6) else 2
        return result(code, "bake_error", detail=str(exc))
    finally:
        if published is not None and not dest.is_symlink() and dest.stat().st_ino == published:
            shutil.rmtree(dest)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if reserved:
            try: dest.rmdir()
            except OSError: pass
        if lockfd is not None:
            os.close(lockfd)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--high", required=True)
    p.add_argument("--low", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--res", type=int, default=1024)
    p.add_argument("--margin-px", type=int, default=8)
    p.add_argument("--cage-extrusion-um", type=int, default=5000)
    p.add_argument("--max-stretch", type=int, default=4000)
    p.add_argument("--timeout", type=int, default=300)
    try:
        a = p.parse_args(argv)
    except SystemExit:
        print(canonical(result(2, "usage_invalid")), end="")
        return 2
    r = bake_bundle(a.high, a.low, a.out_dir, res=a.res, margin_px=a.margin_px,
                    cage_extrusion_um=a.cage_extrusion_um, timeout_s=a.timeout, max_stretch=a.max_stretch)
    print(canonical(r), end="")
    return r["rc"]


if __name__ == "__main__":
    sys.exit(main())
