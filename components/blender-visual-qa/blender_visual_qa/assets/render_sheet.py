"""bv.assets.render_sheet - 16-view x 5-pass render sheet of a GLB through the Blender sheet worker (host side, stdlib only).

Port (SPLIT) of scr-blender-render-sheet (router §3.4). Engine words stripped; camera rule shared with the turntable orbit worker.
CLI: python3 -m blender_visual_qa.assets.render_sheet --glb G --out DIR [--lod GLB]... [--views P0,P1,..] [--res R] [--spp S]
     [--threads T] [--prev RECEIPT] [--blender BIN] [--protocol P] [--timeout S] [--no-selfchecks] [--no-sheet]
LOD0: 16 views (P0..P7 +15 deg, H0/H1 +60 deg, L0 -20 deg, O0..O3 + OT ortho) x passes beauty, clay, unlit_albedo, normal_ws, mask
-> DIR/lod0/<view>_<pass>.png and lossless DIR/sheet_<pass>.png (4 columns). Each --lod GLB (LOD1, LOD2) renders P0..P7 x
beauty, clay, mask into DIR/lod<N>/; its silhouette IoU against LOD0 (mask, per view, ppm) is gated by ASSET_LOD<N>_IOU_MIN_PPM.
Receipt DIR/render_receipt.json (schema bvqa.asset-sheet/v1, canonical sorted JSON; binds input_sha256 + prev_receipt_sha256).
Exit: 0 PASS | 2 usage/IO/harness | 3 PREVIEW (reduced views/res/spp) | 5 gate failed | 6 timeout.
Checks: sheet_views (exactly SHEET_VIEWS distinct ids), artefacts_complete, resolution, mask_coverage (subject visible, not
clipped to the full frame), beauty_views_distinct, passes_distinct, rerender_identical + scale_x_detected (one seeded view),
lod<N>_complete, lod<N>_not_copy, lod<N>_iou.
"""
import argparse
import json
from pathlib import Path
import sys

from blender_visual_qa.assets import glb_core, limits
from blender_visual_qa.assets.turntable import (TurntableError, canonical, default_runner, keep_frames, load_protocol,
                                                open_workspace, persist_evidence, png_pixel_sha, ram_account, sha256_file,
                                                write_ram_receipt)
from blender_visual_qa.drive import png
from blender_visual_qa.drive.journal import DriveError

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "bvqa.asset-sheet/v1"
WORKER = ROOT / "bpy_side/assets/sheet_worker.py"
BLENDER = "/usr/bin/blender"
VIEWS = tuple([("P%d" % i, "persp", 45000 * i, 15000) for i in range(8)]
              + [("H0", "persp", 45000, 60000), ("H1", "persp", 225000, 60000), ("L0", "persp", 135000, -20000)]
              + [("O%d" % i, "ortho", 90000 * i, 0) for i in range(4)] + [("OT", "ortho", 0, 90000)])
SHEET_VIEWS = 16
PASSES = ("beauty", "clay", "unlit_albedo", "normal_ws", "mask")
LOD_VIEWS = tuple("P%d" % i for i in range(8))
LOD_PASSES = ("beauty", "clay", "mask")
LOD_IOU_MIN = {1: limits.ASSET_LOD1_IOU_MIN_PPM, 2: limits.ASSET_LOD2_IOU_MIN_PPM}
SHEET_RES = 384
SHEET_SPP = 64
SHEET_COLS = 4
ORTHO_PERMILLE = 1100
MASK_ON = 128
PPM = 1000000
HEX8 = 8
HEX_BASE = 16
RC_OK, RC_IO, RC_PREVIEW, RC_GATE = limits.RC_OK, limits.RC_USAGE, limits.RC_INCOMPLETE, limits.RC_FAIL


def view_arg(views):
    return ",".join("%s:%s:%d:%d" % v for v in views)


def worker_argv(blender, glb, out, views, passes, p, extra=()):
    return [blender, "-b", "--factory-startup", "-noaudio", "--python", str(WORKER), "--",
            "--glb", str(glb), "--out", str(out), "--views", view_arg(views), "--passes", ",".join(passes),
            "--res", str(p["res"]), "--spp", str(p["spp"]), "--fov-mdeg", str(p["fov_mdeg"]), "--fit-num", str(p["fit_num"]),
            "--fit-den", str(p["fit_den"]), "--ortho-permille", str(ORTHO_PERMILLE), "--threads", str(p["threads"])] + list(extra)


def _sheet(runner, argv, timeout):
    rc, out, err = runner(argv, timeout)
    if rc:
        tail = (out[-400:] + err[-400:]).replace("\n", " ")
        raise TurntableError(RC_IO, "E_BLENDER", "worker rc %s: %s" % (rc, tail))
    for line in out.splitlines():
        if line.startswith("BVQA_SHEET "):
            return json.loads(line[len("BVQA_SHEET "):])
    raise TurntableError(RC_IO, "E_BLENDER", "no BVQA_SHEET line")


def decode_rgb(path):
    try:
        return png.decode(Path(path).read_bytes())
    except (OSError, DriveError) as exc:
        raise TurntableError(RC_IO, "E_IO", "%s: %s" % (Path(path).name, exc))


def mask_bits(path):
    w, h, rgb = decode_rgb(path)
    return w, h, bytes(1 if rgb[i] >= MASK_ON else 0 for i in range(0, len(rgb), 3))


def iou_ppm(a, b):
    inter = sum(x & y for x, y in zip(a, b))
    union = sum(x | y for x, y in zip(a, b))
    return PPM if not union else inter * PPM // union


def write_sheet(paths, cols, dst):
    """Lossless contact sheet: rows of the rendered PNGs copied byte for byte into one RGB8 PNG."""
    cells = [decode_rgb(p) for p in paths]
    w, h = cells[0][0], cells[0][1]
    rows = -(-len(cells) // cols)
    canvas = bytearray(w * cols * h * rows * 3)
    stride = w * cols * 3
    for k, (_, _, rgb) in enumerate(cells):
        cx, cy = k % cols, k // cols
        for y in range(h):
            off = (cy * h + y) * stride + cx * w * 3
            canvas[off:off + w * 3] = rgb[y * w * 3:(y + 1) * w * 3]
    Path(dst).write_bytes(png.encode(w * cols, h * rows, bytes(canvas)))
    return sha256_file(dst)


def _collect(d, views, passes, lod, rows, missing):
    for v in views:
        for p in passes:
            f = d / ("%s_%s.png" % (v[0], p))
            if not f.is_file() or not f.stat().st_size:
                missing.append("lod%d/%s" % (lod, f.name))
                continue
            sha, w, h = png_pixel_sha(f)
            rows.append({"lod": lod, "view": v[0], "pass": p, "file": "lod%d/%s" % (lod, f.name), "pixel_sha256": sha,
                         "file_sha256": sha256_file(f), "w": w, "h": h})


def run(glb, out, *, ws=None, ram_root=None, ram_config=None, keep_mb=0, **kw):
    """-> (rc, receipt). STORAGE LAW: lod*/ views, _checks/ and sheet_*.png live in a vadapt.ramws workspace (4 GiB);
    `out` receives only render_receipt.json + ram_receipt.json (plus <=keep_mb of images under build/kept_frames)."""
    out = Path(out).resolve()
    mod, own = (sys.modules[type(ws).__module__], None) if ws is not None else open_workspace(out, root=ram_root, config=ram_config)
    ws = ws if ws is not None else own
    try:
        work = ws.path("sheet/")
        rc, rec = _run(glb, out, work, ws, mod, **kw)
        persist_evidence(work, out)
        if keep_mb:
            keep_frames(mod, work, out, keep_mb, "render_sheet", [str(glb), str(out)])
        write_ram_receipt(out, ws)
        return rc, rec
    finally:
        if own is not None:
            own.close()


def _run(glb, out, work, ws, mod, *, lods=(), views=None, res=None, spp=None, threads=4, prev=None, protocol=None, blender=BLENDER,
         timeout=None, selfchecks=True, sheets=True, runner=default_runner):
    proto = load_protocol(protocol)
    tt = proto["turntable"]
    glb = Path(glb)
    lods = [Path(x) for x in lods]
    for f in [glb] + lods:
        if not f.is_file():
            raise TurntableError(RC_IO, "E_IO", "glb missing: %s" % f)
        try:
            glb_core.parse_glb(f.read_bytes())
        except glb_core.GlbError as exc:
            raise TurntableError(RC_IO, "E_SCHEMA", "%s: %s" % (f.name, exc))
    if len(lods) > len(LOD_IOU_MIN):
        raise TurntableError(RC_IO, "E_USAGE", "at most %d --lod" % len(LOD_IOU_MIN))
    by_id = {v[0]: v for v in VIEWS}
    sel = list(VIEWS) if views is None else [by_id[v] for v in views if v in by_id]
    if views is not None and len(sel) != len(views):
        raise TurntableError(RC_IO, "E_USAGE", "unknown view in %s" % list(views))
    p = {"res": res or SHEET_RES, "spp": spp or SHEET_SPP, "fov_mdeg": tt["fov_mdeg"], "fit_num": tt["fit_num"],
         "fit_den": tt["fit_den"], "threads": threads}
    timeout = timeout or tt["timeout_s"]
    prev_sha = None
    if prev:
        if not Path(prev).is_file():
            raise TurntableError(RC_IO, "E_IO", "prev receipt missing")
        prev_sha = sha256_file(prev)
    input_sha = sha256_file(glb)
    reasons = []
    if views is not None:
        reasons.append("views %s" % ",".join(v[0] for v in sel))
    if p["res"] != SHEET_RES:
        reasons.append("res %s != %s" % (p["res"], SHEET_RES))
    if p["spp"] != SHEET_SPP:
        reasons.append("spp %s != %s" % (p["spp"], SHEET_SPP))
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    out.mkdir(parents=True, exist_ok=True)
    rp = out / "render_receipt.json"
    if rp.exists():
        rp.unlink()
    for old in list(out.glob("lod*/*.png")) + list(out.glob("sheet_*.png")):  # stale pre-LAW frames
        old.unlink()
    base = {"schema": SCHEMA, "input_sha256": input_sha, "prev_receipt_sha256": prev_sha, "params": p,
            "protocol_sha256": sha256_file(protocol or ROOT / "config/render_protocol_v1.json"), "preview_reasons": reasons,
            "views": [v[0] for v in sel], "passes": list(PASSES),
            "lods": [{"lod": n + 1, "input_sha256": sha256_file(f)} for n, f in enumerate(lods)]}

    rows, missing = [], []
    d0 = work / "lod0"
    info = _sheet(runner, worker_argv(blender, glb, d0, sel, PASSES, p), timeout)
    ram_account(mod, ws)
    _collect(d0, sel, PASSES, 0, rows, missing)
    lod_views = [by_id[v] for v in LOD_VIEWS if v in {s[0] for s in sel}]
    for n, f in enumerate(lods, 1):
        _sheet(runner, worker_argv(blender, f, work / ("lod%d" % n), lod_views, LOD_PASSES, p), timeout)
        ram_account(mod, ws)
        _collect(work / ("lod%d" % n), lod_views, LOD_PASSES, n, rows, missing)

    lod0 = [r for r in rows if r["lod"] == 0]
    got_views = sorted({r["view"] for r in lod0})
    check("sheet_views", len(got_views) == SHEET_VIEWS if views is None else len(got_views) == len(sel),
          "views=%d/%d" % (len(got_views), SHEET_VIEWS))
    check("artefacts_complete", not missing, "images=%d missing=%s" % (len(rows), missing[:5]))
    sizes = {(r["w"], r["h"]) for r in rows}
    check("resolution", sizes == {(p["res"], p["res"])}, "sizes=%s" % sorted(sizes))
    extra = {"images": rows, "blender": info.get("blender"), "worker": {k: info[k] for k in info if k.endswith("_um") or k == "tris"}}

    def finish(rc, verdict):
        rec = dict(base, **extra)
        rec.update({"checks": checks, "failed": [c["check"] for c in checks if not c["ok"]], "rc": rc, "verdict": verdict})
        rp.write_text(canonical(rec))
        return rc, rec

    if not lod0 or missing or sizes != {(p["res"], p["res"])}:
        return finish(RC_GATE, "FAIL")

    sha = {(r["lod"], r["view"], r["pass"]): r["pixel_sha256"] for r in rows}
    masks, cover = {}, {}
    for v in sel:
        _, _, bits = masks[(0, v[0])] = mask_bits(d0 / ("%s_mask.png" % v[0]))
        cover[v[0]] = sum(bits) * PPM // len(bits)
    bad = [v for v in cover if not 0 < cover[v] < PPM]
    check("mask_coverage", not bad, "ppm=%s bad=%s" % ([cover[v[0]] for v in sel], bad))
    beauty = [sha[(0, v[0], "beauty")] for v in sel]
    dup = sorted({v[0] for v, s in zip(sel, beauty) if beauty.count(s) > 1})
    check("beauty_views_distinct", not dup, "identical=%s" % dup)
    same = [v[0] for v in sel if len({sha[(0, v[0], ps)] for ps in PASSES}) != len(PASSES)]
    check("passes_distinct", not same, "views_with_identical_passes=%s" % same)

    for n, f in enumerate(lods, 1):
        copy = sha256_file(f) == input_sha or all(sha[(n, v[0], ps)] == sha[(0, v[0], ps)] for v in lod_views for ps in LOD_PASSES)
        check("lod%d_not_copy" % n, not copy, "lod%d renders identical to lod0=%s" % (n, copy))
        ious = []
        for v in lod_views:
            _, _, bits = mask_bits(work / ("lod%d" % n) / ("%s_mask.png" % v[0]))
            ious.append(iou_ppm(masks[(0, v[0])][2], bits))
        extra.setdefault("lod_iou_ppm", {})["lod%d" % n] = ious
        check("lod%d_iou" % n, ious and min(ious) >= LOD_IOU_MIN[n], "min=%s floor=%s" % (min(ious or [0]), LOD_IOU_MIN[n]))

    if selfchecks:
        v = sel[int(input_sha[:HEX8], HEX_BASE) % len(sel)]
        cdir = work / "_checks"
        got = {}
        for sub, ext in (("rerender", ()), ("scale_x", ("--scale-x-permille", str(proto["checks"]["scale_x_permille"])))):
            (cdir / sub).mkdir(parents=True, exist_ok=True)
            _sheet(runner, worker_argv(blender, glb, cdir / sub, [v], ("beauty",), p, ext), timeout)
            ram_account(mod, ws)
            got[sub] = png_pixel_sha(cdir / sub / ("%s_beauty.png" % v[0]))[0]
        check("rerender_identical", got["rerender"] == sha[(0, v[0], "beauty")], "view=%s" % v[0])
        check("scale_x_detected", got["scale_x"] != sha[(0, v[0], "beauty")], "view=%s" % v[0])

    if sheets and all(c["ok"] for c in checks):
        extra["sheets"] = {ps: write_sheet([d0 / ("%s_%s.png" % (v[0], ps)) for v in sel], SHEET_COLS, work / ("sheet_%s.png" % ps))
                           for ps in PASSES}
    if any(not c["ok"] for c in checks):
        return finish(RC_GATE, "FAIL")
    if reasons:
        return finish(RC_PREVIEW, "PREVIEW")
    return finish(RC_OK, "PASS")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="render-sheet")
    ap.add_argument("--glb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lod", action="append", default=[])
    ap.add_argument("--views")
    ap.add_argument("--res", type=int)
    ap.add_argument("--spp", type=int)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--prev")
    ap.add_argument("--protocol")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--no-selfchecks", action="store_true")
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--keep-frames", type=int, default=0, dest="keep_mb", help="opt-in: keep <=MB of images in build/kept_frames")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return RC_IO if exc.code else RC_OK
    try:
        rc, rec = run(a.glb, a.out, lods=a.lod, views=a.views.split(",") if a.views else None, res=a.res, spp=a.spp,
                      threads=a.threads, prev=a.prev, protocol=a.protocol, blender=a.blender, timeout=a.timeout,
                      selfchecks=not a.no_selfchecks, sheets=not a.no_sheet, keep_mb=a.keep_mb)
    except TurntableError as exc:
        print(canonical({"schema": SCHEMA, "rc": exc.rc, "code": exc.code, "error": exc.detail}))
        return exc.rc
    print(canonical({"schema": SCHEMA, "rc": rc, "verdict": rec["verdict"], "failed": rec["failed"], "input_sha256": rec["input_sha256"],
                     "images": len(rec["images"]), "receipt_sha256": sha256_file(Path(a.out) / "render_receipt.json")}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
