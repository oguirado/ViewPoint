"""Engine path on RAM evidence: synthetic stored capture -> GuiAdapter -> vcore analyze -> vmem ingest -> receipt.

Everything heavy (raw.json, frames.linear.raw, frames.rgb24, tiles.bin, gops.bin, spool) lives in one ramws session and is
released before return; only the vmem store (when --store is given) and the <= 4 KB receipt may touch disk. Imports vision.pin
first; the engine is the pinned wheel only. rc: 0 ok, 2 usage, 5 gate / pin / unforeseen. Never 1.

  python3 vision/ingest.py [--run-id ID] [--store DIR] [--frames N] [--receipt-out FILE]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vision import pin  # noqa: E402  (must precede every engine import)

pin.activate()
import numpy as np  # noqa: E402
import vadapt.ramws as ramws  # noqa: E402
from vadapt.gui.from_screens import GuiAdapter  # noqa: E402
from visioncore import canon, pipeline  # noqa: E402
from visioncore.errors import VqaError  # noqa: E402
from vision import rawjson  # noqa: E402

TOOL, CAMPAIGN = "opensuperficies", "os-cu-0"
SCHEMA = "os.cu.engine-receipt/v1"
RECEIPT_MAX = 4096
W, H = 64, 48
QUOTA = 256 << 20


class IngestError(Exception):
    code = "E_GATE"


def synth_frames(n, w=W, h=H):
    """Deterministic synthetic capture, generated in RAM: a bright block sliding right over a flat background.
    Returned in SCRAMBLED ring order as [(render_index, rgb_bytes)] so the linearisation step has work to do."""
    out = []
    for k in range(n):
        a = np.empty((h, w, 3), np.uint8)
        a[:] = (40, 60, 80)
        a[8:16, 4 + 4 * k:12 + 4 * k] = (250, 200, 10)
        out.append((k, a.tobytes()))
    return out[n // 2:] + out[:n // 2]


def _look_body(vr, f, rows, m0, role):
    import hashlib
    rows = rows[np.argsort(rows["morton"], kind="stable")]
    xh = rows["xh64"].astype("<u8")
    desc = rows.tobytes()
    return {"frame": f, "render_index": vr.frame_nums[f], "role": role, "m0": m0, "m1": int(rows[-1]["morton"]),
            "look": {"grid": vr.grid.ident, "n": len(rows), "ah64": hashlib.sha256(xh.tobytes()).hexdigest()[:16],
                     "desc_blob": hashlib.sha256(desc).hexdigest(), "y_q4_mean": int(rows["y_q4"].astype(np.int64).sum()) // len(rows)},
            "xh64": ["%016x" % int(v) for v in rows["xh64"]], "sig64": ["%016x" % int(v) for v in rows["sig64"]],
            "desc_hex": desc.hex()}


def build_spool(vr, run_n=1, app="oscu", scene="synth"):
    """VisionRun -> vmem.spool/v1 lines (regions, one `tile` look per macroblock at the anchor frame, one episode per GOP)."""
    rid = "r_%06d_%08x" % (run_n, run_n)
    sp = {"schema": "vmem.spool/v1", "run_id": rid}
    lines = [dict(sp, seq=0, type="run", body={
        "scope": {"app": app, "scene": scene, "res": "%dx%d" % (vr.grid.W, vr.grid.H), "grid": vr.grid.ident, "build_seq": run_n},
        "receipt_sha256": vr.frames_sha256, "profile_sha256": vr.params_sha256, "align_source": "render_index"})]
    for f, regs in enumerate(vr.regions):
        tl = vr.tiles[f]
        for r in regs:
            m0, m1 = int(r["block"][1], 16), int(r["block"][2], 16)
            want = {int(a, 16) + k for a, b in r["spans"] for k in range(int(b, 16) - int(a, 16) + 1)}
            rows = tl[[int(t["morton"]) in want for t in tl]]
            if not len(rows):
                continue
            lines.append(dict(sp, seq=len(lines), type="region", body={
                "id": r["id"], "frame": f, "cls": r["cls"], "bbox_px": r["bbox_px"], "xh64": "%016x" % int(rows[0]["xh64"]),
                "sig64": "%016x" % int(rows[0]["sig64"]), "render_index": vr.frame_nums[f], "role": "tile", "m0": m0, "m1": m1,
                "look": {"n": len(rows), "ah64": "%016x" % int(rows[0]["sig64"]), "y_q4_mean": int(rows["y_q4"].mean())},
                "desc_hex": rows.tobytes().hex()}))
    g = vr.gops()
    f0 = vr.frame_nums.index(g[0]["frame_ids"][0]) if g and g[0].get("frame_ids") else 0
    tl = vr.tiles[f0]
    mort = tl["morton"].astype(np.int64)
    for mb in np.unique(mort >> 6):
        lines.append(dict(sp, seq=len(lines), type="look", body=_look_body(vr, f0, tl[(mort >> 6) == mb], int(mb) << 6, "tile")))
    raw = vr.gop_files.get("gops.bin", b"")
    for gp in g:
        lines.append(dict(sp, seq=len(lines), type="episode", body={
            "schema": "vqa.episode/v1", "episode": rid + "/g%d" % gp["gop"], "run_id": rid, "gop": gp["gop"], "f0": gp["f0"],
            "f1": gp["f1"], "n_frames": gp["n_frames"], "cut": int(bool(gp["cut"])), "gop_blob_sha256": gp["gop_blob_sha256"],
            "gop_blob_hex": raw[gp["offset"]:gp["offset"] + gp["length"]].hex(), "verdicts": [{"verdict": "PASS"}], "fg": False,
            "classes_ppm": {"CHANGED": 1}, "delta_bytes": gp["delta_bytes"], "grid": vr.grid.ident,
            "params_sha256": vr.params_sha256, "frame_ids": gp["frame_ids"]}))
    return lines


def run(run_id="e-ingest", n_frames=6, store=None, root=None):
    """Full pass; returns the receipt dict. `store` None -> the vmem store lives in the session and is released too."""
    kw = {"root": root} if root else {}
    ramws.sweep(TOOL, **({"root": root} if root else {}))  # janitor first (stale sessions only; live flock sessions survive)
    ws = ramws.open_run(CAMPAIGN, run_id, QUOTA, tool=TOOL, **kw)
    try:
        frames = synth_frames(n_frames)
        cap = ws.path("cap/")
        rj = rawjson.write_linear(str(cap), W, H, frames)
        os_rows = rawjson.os_manifest(frames, W, H)["frames"]
        ga = GuiAdapter()
        src = ga.frames(str(cap))
        if src.meta["storage"] != "rgb24_raw":
            raise IngestError("storage_not_rgb24_raw")
        eng_shas = [f.sha256 for f in src]
        os_shas = [r["sha256"] for r in os_rows]
        if eng_shas != os_shas:
            raise IngestError("frame_sha_mismatch")
        eng = ws.path("eng/")
        ga.adapt(str(cap), str(eng))
        ws.release("cap")  # raw capture no longer needed once the engine layout exists
        vr = pipeline.analyze(pipeline.ManifestSource(str(eng / "frames.manifest.json")), None, None, ws.path("vision/") / "out")
        spool = ws.path("spool.jsonl")
        with open(spool, "wb") as fh:
            fh.write("".join(canon.dumps(l) for l in build_spool(vr)).encode("utf-8"))
        from vmem import ingest as vingest
        sroot = store or str(ws.path("store/"))
        res = vingest.ingest(sroot, str(spool))
        used_peak = ws.account()
        n_regions = sum(len(r) for r in vr.regions)
        rc = {"schema": SCHEMA, "pin_wheel_sha256": pin.EXPECTED_WHEEL_SHA256, "storage": "rgb24_raw",
              "width": rj["width"], "height": rj["height"], "frames": len(eng_shas), "engine_sha_equals_os": True,
              "frames_sha256": vr.frames_sha256, "params_sha256": vr.params_sha256, "grid": vr.grid.ident,
              "regions": n_regions, "gops": len(vr.gops()), "ingested_gen": int(res.get("gen") or 0),
              "ram_peak_kib": used_peak // 1024, "released": True}
    finally:
        ws.close()
    if os.path.exists(str(ws.root)):
        raise IngestError("ramws_not_released")
    return rc


def _opts(argv):
    opt, a = {}, list(argv)
    while a:
        x = a.pop(0)
        if not x.startswith("--") or not a or x[2:] not in ("run-id", "store", "frames", "receipt-out"):
            raise ValueError("usage: ingest.py [--run-id ID] [--store DIR] [--frames N] [--receipt-out FILE]")
        opt[x[2:]] = a.pop(0)
    return opt


def main(argv):
    try:
        opt = _opts(argv)
        n = int(opt.get("frames", "6"))
        if not 2 <= n <= 32:
            raise ValueError("--frames 2..32")
    except ValueError as e:
        sys.stdout.write(json.dumps({"code": "E_USAGE", "detail": str(e)}, sort_keys=True) + "\n")
        return 2
    try:
        rc = run(opt.get("run-id", "e-ingest"), n, opt.get("store"))
        body = json.dumps(rc, sort_keys=True, separators=(",", ":"))
        if len(body) > RECEIPT_MAX:
            raise IngestError("receipt_too_big")
        if "receipt-out" in opt:
            with open(opt["receipt-out"], "w", encoding="utf-8") as fh:
                fh.write(body + "\n")
        sys.stdout.write(body + "\n")
        return 0
    except (IngestError, ramws.RamError, VqaError, rawjson.RawJsonError) as e:
        sys.stdout.write(json.dumps({"code": getattr(e, "code", "E_GATE"), "detail": str(e)[:200]}, sort_keys=True) + "\n")
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
