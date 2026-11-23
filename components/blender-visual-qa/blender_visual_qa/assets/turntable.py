"""bv.assets.turntable - 72-frame turntable of a GLB through the Blender orbit worker (host side, stdlib only).

CLI: python3 -m blender_visual_qa.assets.turntable --glb G --out DIR [--frames N] [--step-mdeg M] [--res R] [--spp S]
     [--threads T] [--with-truth] [--vision off|shadow|on] [--prev RECEIPT] [--blender BIN] [--protocol P] [--no-selfchecks]
Writes DIR/render/000001..N.png, DIR/render/scene.json, DIR/frames.manifest.json, DIR/turntable_receipt.json
(schema bvqa.asset-turntable/v1, canonical sorted JSON; binds input_sha256 + prev_receipt_sha256), then runs
vision_stage.run_stage(DIR, vision=mode). Exit: 0 PASS | 2 usage/IO/harness | 3 PREVIEW (reduced sequence) or no frames
| 5 gate failed | 6 timeout. pixel_sha256 = sha256 of the zlib-inflated PNG scanline stream (same encoder, same pixels).
Checks: sequence_complete, adjacent_differ, rerender_identical (one seeded frame), scale_x_detected (X*1.1 mutant), truth.
"""
import argparse
import hashlib
from io import BytesIO, TextIOWrapper
import json
import os
from pathlib import Path
import shutil
import sys
import types
import zipfile
import zlib

from functools import partial
from blender_visual_qa.assets.runtime_config import asset_config_path
from blender_visual_qa.drive import worker
from blender_visual_qa.vision_delivery import resolve_wheel

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "bvqa.asset-turntable/v1"
PROTOCOL = asset_config_path("render_protocol_v1.json")
WORKER = ROOT / "bpy_side/assets/orbit_worker.py"
PASSES = ROOT / "bpy_side/passes.py"
BLENDER = "/usr/bin/blender"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
RC_OK, RC_IO, RC_PREVIEW, RC_STALE, RC_GATE, RC_BUDGET = 0, 2, 3, 4, 5, 6
HEX8 = 8
HEX_BASE = 16


class TurntableError(Exception):
    def __init__(self, rc, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.rc, self.code, self.detail = rc, code, detail


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def load_protocol(path=None):
    proto, _ = load_protocol_with_sha256(path)
    return proto


def load_protocol_with_sha256(path=None):
    try:
        data = Path(path or PROTOCOL).read_bytes()
        with TextIOWrapper(BytesIO(data)) as source:
            proto = json.load(source)
        tt = proto["turntable"]
        for key in ("frames", "step_mdeg", "res", "spp", "pitch_mdeg", "fov_mdeg", "fit_num", "fit_den",
                    "full_turn_mdeg", "timeout_s", "first_frame", "fps"):
            if type(tt[key]) is not int:
                raise ValueError(key)
        chk = proto["checks"]
        if type(chk["scale_x_permille"]) is not int or not isinstance(chk["truth_frames"], list):
            raise ValueError("checks")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TurntableError(RC_IO, "E_IO", "protocol: %s" % exc)
    return proto, sha256_bytes(data)


def png_pixel_sha(path):
    """-> (pixel_sha256, width, height). Raises TurntableError(RC_IO) on a non-PNG / empty / garbled file."""
    data = Path(path).read_bytes()
    if not data.startswith(PNG_MAGIC):
        raise TurntableError(RC_IO, "E_IO", "%s: not a PNG" % Path(path).name)
    pos, idat, dims = len(PNG_MAGIC), [], None
    while pos + 8 <= len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            dims = (int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big"))
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + n
    if dims is None or not idat:
        raise TurntableError(RC_IO, "E_IO", "%s: no IHDR/IDAT" % Path(path).name)
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise TurntableError(RC_IO, "E_IO", "%s: %s" % (Path(path).name, exc))
    return sha256_bytes(raw), dims[0], dims[1]


# --------------------------------------------------------------------------- STORAGE LAW (frames live in RAM)
RAM_TOOL = "bvqa-asset"
RAM_CAMPAIGN = "asset"
RAM_QUOTA = 4 << 30  # turntable/sheet/bake: 4 GiB (STORAGE_LAW section 3)
EVIDENCE_CAP = 64 << 20  # L6: persistent output per run
EVIDENCE_EXT = (".json", ".jsonl", ".tsv")
RC_RAM = 5


def load_ramws(config=None):
    """Load vadapt/ramws.py from the hash-pinned wheel (fail closed: E_PIN rc 2). Stdlib-only module, exec'd in isolation."""
    try:
        wheel, _ = resolve_wheel(config or None, relative_root=ROOT)
        with zipfile.ZipFile(wheel) as zf:
            src = zf.read("vadapt/ramws.py")
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise TurntableError(RC_IO, "E_PIN", "ramws: %s" % exc)
    mod = types.ModuleType("vadapt_ramws")
    mod.__file__ = "%s!vadapt/ramws.py" % wheel
    sys.modules["vadapt_ramws"] = mod  # dataclass/pickle-safe; the wheel is the only source
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def open_workspace(out, quota=RAM_QUOTA, *, config=None, root=None):
    """-> (ramws module, RamWorkspace) for one asset run. RamError -> TurntableError rc 5."""
    mod = load_ramws(config)
    run_id = "%s-%d" % (sha256_bytes(str(Path(out).resolve()).encode())[:16], os.getpid())
    try:
        kw = {} if root is None else {"root": root}
        return mod, mod.open_run(RAM_CAMPAIGN, run_id, quota, tool=RAM_TOOL, **kw)
    except mod.RamError as exc:
        raise TurntableError(RC_RAM, exc.code, str(exc))


def ram_account(mod, ws):
    """Resync usage after an external writer (Blender); over quota -> rc 5 (the caller's finally closes the workspace)."""
    try:
        return ws.account()
    except mod.RamError as exc:
        raise TurntableError(RC_RAM, exc.code, str(exc))


def persist_evidence(work, out, extra=()):
    """Copy only evidence (receipts/manifests/json) from the workspace tree to `out`; never a frame; capped at EVIDENCE_CAP."""
    work, out, total = Path(work), Path(out), 0
    for f in sorted(work.rglob("*")):
        rel = f.relative_to(work)
        if not f.is_file() or f.suffix not in EVIDENCE_EXT or rel.parts[0] in ("_checks", "truth"):
            continue
        total += f.stat().st_size
        if total > EVIDENCE_CAP:
            raise TurntableError(RC_RAM, "E_EVIDENCE_CAP", "evidence > %d bytes" % EVIDENCE_CAP)
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, out / rel)
    return total


def write_ram_receipt(out, ws, name="ram_receipt.json"):
    """Separate from the sha-chained receipt (peak/swap vary run to run)."""
    rec = dict(ws.receipt(), schema="bvqa.asset-ram/v1", tool=RAM_TOOL)
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / name).write_text(canonical(rec))
    return rec


def keep_frames(mod, work, out, cap_mb, reason, argv):
    """Opt-in --keep-frames MB: copy frames to <repo>/build/kept_frames/<run_id>/ (cap 256 MB, TTL 24 h) via ramws.keep_frames."""
    import time
    return mod.keep_frames(work, ROOT / "build", Path(out).resolve().name, cap_mb, now=time.time(), reason=reason,
                           argv_sha=sha256_bytes(" ".join(argv).encode()))


def default_runner(argv, timeout, *, work=None):
    if "--run" in argv:
        return worker.asset(argv, timeout, work=work, output_flag="--run")
    return worker.asset(argv, timeout, input_flags=("--glb",), work=work, output_flag="--out")


def worker_argv(blender, glb, out, p, first, last, extra=()):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--",
            "--glb", str(glb), "--out", str(out), "--first", str(first), "--last", str(last),
            "--frames", str(p["frames"]), "--step-mdeg", str(p["step_mdeg"]), "--res", str(p["res"]),
            "--spp", str(p["spp"]), "--pitch-mdeg", str(p["pitch_mdeg"]), "--fov-mdeg", str(p["fov_mdeg"]),
            "--fit-num", str(p["fit_num"]), "--fit-den", str(p["fit_den"]), "--threads", str(p["threads"])] + list(extra)


def _orbit(runner, argv, timeout):
    rc, out, err = runner(argv, timeout)
    if rc:
        tail = (out[-400:] + err[-400:]).replace("\n", " ")
        raise TurntableError(RC_IO, "E_BLENDER", "worker rc %s: %s" % (rc, tail))
    for line in out.splitlines():
        if line.startswith("BVQA_ORBIT "):
            return json.loads(line[len("BVQA_ORBIT "):])
    raise TurntableError(RC_IO, "E_BLENDER", "no BVQA_ORBIT line")


def _hash_frame(path):
    sha, w, h = png_pixel_sha(path)
    return {"file": Path(path).name, "pixel_sha256": sha, "file_sha256": sha256_file(path), "w": w, "h": h}


def run(glb, out, *, ws=None, ram_root=None, ram_config=None, keep_mb=0, **kw):
    """-> (rc, receipt). STORAGE LAW: every frame/scratch .blend/truth npy lives in a vadapt.ramws workspace (4 GiB);
    `out` receives only receipts, manifests and ram_receipt.json (plus <=keep_mb of frames under build/kept_frames)."""
    out = Path(out).resolve()
    mod, own = (sys.modules[type(ws).__module__], None) if ws is not None else open_workspace(out, root=ram_root, config=ram_config)
    ws = ws if ws is not None else own
    try:
        work = ws.path("turntable/")
        rc, rec = _run(glb, out, work, ws, mod, **kw)
        persist_evidence(work, out)
        if keep_mb:
            keep_frames(mod, work, out, keep_mb, "turntable", [str(glb), str(out)])
        write_ram_receipt(out, ws)
        return rc, rec
    finally:
        if own is not None:
            own.close()


def _run(glb, out, work, ws, mod, *, protocol=None, frames=None, step_mdeg=None, res=None, spp=None, threads=4, with_truth=False,
         vision="shadow", prev=None, blender=BLENDER, timeout=None, selfchecks=True, runner=default_runner,
         vision_runner=None):
    if runner is default_runner:
        runner = partial(default_runner, work=work)
    proto, protocol_sha = load_protocol_with_sha256(protocol)
    tt, chk = proto["turntable"], proto["checks"]
    glb = Path(glb)
    if not glb.is_file():
        raise TurntableError(RC_IO, "E_IO", "glb missing")
    if vision not in ("off", "shadow", "on"):
        raise TurntableError(RC_IO, "E_SCHEMA", "vision mode")
    p = {"frames": tt["frames"] if frames is None else frames, "step_mdeg": tt["step_mdeg"] if step_mdeg is None else step_mdeg,
         "res": res or tt["res"], "spp": spp or tt["spp"], "pitch_mdeg": tt["pitch_mdeg"], "fov_mdeg": tt["fov_mdeg"],
         "fit_num": tt["fit_num"], "fit_den": tt["fit_den"], "threads": threads}
    timeout = timeout or tt["timeout_s"]
    prev_sha = None
    if prev:
        if not Path(prev).is_file():
            raise TurntableError(RC_IO, "E_IO", "prev receipt missing")
        prev_sha = sha256_file(prev)
    input_sha = sha256_file(glb)
    checks, reasons = [], []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    for key in ("frames", "step_mdeg", "res", "spp"):
        if p[key] != tt[key]:
            reasons.append("%s %s != protocol %s" % (key, p[key], tt[key]))
    base = {"schema": SCHEMA, "input_sha256": input_sha, "prev_receipt_sha256": prev_sha, "params": p,
            "protocol_sha256": protocol_sha, "preview_reasons": reasons}
    rp = out / "turntable_receipt.json"

    def finish(rc, verdict, extra):
        rec = dict(base, **extra)
        rec.update({"checks": checks, "failed": [c["check"] for c in checks if not c["ok"]], "rc": rc, "verdict": verdict})
        worker.publish(rp, canonical(rec).encode())
        return rc, rec

    if p["frames"] < 1 or p["step_mdeg"] < 1 or p["frames"] * p["step_mdeg"] > tt["full_turn_mdeg"]:
        check("sequence_complete", False, "frames*step=%s outside (0, %s]; nothing rendered" % (
            p["frames"] * p["step_mdeg"], tt["full_turn_mdeg"]))
        if p["frames"] < 1:
            return finish(RC_PREVIEW, "PREVIEW", {"frames": []})
        return finish(RC_GATE, "FAIL", {"frames": []})

    rdir = work / "render"
    rdir.mkdir(exist_ok=True)
    info = _orbit(runner, worker_argv(blender, glb, rdir, p, tt["first_frame"], p["frames"]), timeout)
    ram_account(mod, ws)
    rows, missing = [], []
    for n in range(tt["first_frame"], tt["first_frame"] + p["frames"]):
        f = rdir / ("%06d.png" % n)
        if not f.is_file() or not f.stat().st_size:
            missing.append(n)
            continue
        row = _hash_frame(f)
        row.update({"frame": n, "az_mdeg": (n - tt["first_frame"]) * p["step_mdeg"]})
        rows.append(row)
    extra = {"frames": rows, "blender": info.get("blender"), "orbit": {k: info[k] for k in info if k.endswith("_um")}}
    check("sequence_complete", not missing, "frames=%d/%d missing=%s" % (len(rows), p["frames"], missing[:5]))
    if not rows:
        return finish(RC_PREVIEW, "PREVIEW", extra)
    same = [rows[i]["frame"] for i in range(1, len(rows)) if rows[i]["pixel_sha256"] == rows[i - 1]["pixel_sha256"]]
    check("adjacent_differ", not same, "identical_adjacent=%s" % same[:5])
    sizes = {(r["w"], r["h"]) for r in rows}
    check("resolution", sizes == {(p["res"], p["res"])}, "sizes=%s" % sorted(sizes))

    if selfchecks and rows:
        idx = int(input_sha[:HEX8], HEX_BASE) % p["frames"] + tt["first_frame"]
        want = next((r for r in rows if r["frame"] == idx), None)
        if want is not None:
            cdir = work / "_checks"
            for sub, ext in (("rerender", ()), ("scale_x", ("--scale-x-permille", str(chk["scale_x_permille"])))):
                (cdir / sub).mkdir(parents=True, exist_ok=True)
                _orbit(runner, worker_argv(blender, glb, cdir / sub, p, idx, idx, ext), timeout)
                ram_account(mod, ws)
            again = png_pixel_sha(cdir / "rerender" / ("%06d.png" % idx))[0]
            mutant = png_pixel_sha(cdir / "scale_x" / ("%06d.png" % idx))[0]
            check("rerender_identical", again == want["pixel_sha256"], "frame=%d" % idx)
            check("scale_x_detected", mutant != want["pixel_sha256"], "frame=%d scale_x=%s permille" % (idx, chk["scale_x_permille"]))

    (rdir / "scene.json").write_text(canonical({"app": "blender", "scene": "turntable", "fps_num": tt["fps"], "fps_den": 1}))
    (work / "frames.manifest.json").write_text(canonical({
        "schema": "bvqa.asset-turntable-frames/v1", "input_sha256": input_sha, "layout": "render/%06d.png",
        "frames": [{k: r[k] for k in ("frame", "az_mdeg", "file", "pixel_sha256", "file_sha256")} for r in rows]}))

    if with_truth:
        keys = [n for n in chk["truth_frames"] if n <= p["frames"]]
        blend = work / "_checks" / "truth.blend"
        (work / "_checks").mkdir(exist_ok=True)
        _orbit(runner, worker_argv(blender, glb, work / "_checks" / "truth_render", p, tt["first_frame"], tt["first_frame"] - 1,
                                   ("--save-blend", str(blend), "--keyframes", ",".join(map(str, keys)))), timeout)
        for n in keys:
            rc, so, se = runner([blender, "-b", "--factory-startup", "-noaudio", str(blend), "--python", str(PASSES), "--",
                                 "--run", str(work), "--frames", "%d:%d" % (n, n), "--threads", str(threads)], timeout)
            if rc:
                raise TurntableError(RC_IO, "E_TRUTH", "passes rc %s: %s" % (rc, (so[-300:] + se[-300:]).replace("\n", " ")))
        got = sorted(x.name for x in (work / "truth").glob("f*.ob.npy")) if (work / "truth").is_dir() else []
        check("truth", len(got) == len(keys), "frames=%s ob_npy=%d" % (keys, len(got)))

    vis = {"mode": vision, "status": "SKIPPED"}
    if vision != "off" and all(c["ok"] for c in checks):
        if vision_runner is None:
            sys.path.insert(0, str(ROOT))
            from blender_visual_qa import vision_stage
            vision_runner = vision_stage.run_stage
        try:
            vis = dict(vision_runner(work, vision=vision), mode=vision)
        except Exception as exc:  # StageError is a ValueError; shadow is advisory, 'on' gates
            vis = {"mode": vision, "status": "ERROR", "error": str(exc)[:300]}
            if vision == "on":
                check("vision_stage", False, vis["error"])
    extra["vision"] = {k: vis[k] for k in vis if k in ("mode", "status", "error", "engine_commit", "wheel_sha256")}
    if any(not c["ok"] for c in checks):
        return finish(RC_GATE, "FAIL", extra)
    if reasons:
        return finish(RC_PREVIEW, "PREVIEW", extra)
    return finish(RC_OK, "PASS", extra)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="turntable")
    ap.add_argument("--glb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--protocol")
    ap.add_argument("--frames", type=int)
    ap.add_argument("--step-mdeg", type=int, dest="step_mdeg")
    ap.add_argument("--res", type=int)
    ap.add_argument("--spp", type=int)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--with-truth", action="store_true", dest="with_truth")
    ap.add_argument("--vision", choices=("off", "shadow", "on"), default="shadow")
    ap.add_argument("--prev")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--no-selfchecks", action="store_true")
    ap.add_argument("--keep-frames", type=int, default=0, dest="keep_mb", help="opt-in: keep <=MB of frames in build/kept_frames")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return RC_IO if exc.code else RC_OK
    try:
        rc, rec = run(a.glb, a.out, protocol=a.protocol, frames=a.frames, step_mdeg=a.step_mdeg, res=a.res, spp=a.spp,
                      threads=a.threads, with_truth=a.with_truth, vision=a.vision, prev=a.prev, blender=a.blender,
                      timeout=a.timeout, selfchecks=not a.no_selfchecks, keep_mb=a.keep_mb)
    except TurntableError as exc:
        print(canonical({"schema": SCHEMA, "rc": exc.rc, "code": exc.code, "error": exc.detail}))
        return exc.rc
    print(canonical({"schema": SCHEMA, "rc": rc, "verdict": rec["verdict"], "failed": rec["failed"],
                     "frames": len(rec["frames"]), "receipt_sha256": sha256_file(Path(a.out) / "turntable_receipt.json")}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
