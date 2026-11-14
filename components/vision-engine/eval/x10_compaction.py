"""X10 compaction safety (G-E8): 5 synthetic runs, then 3 compaction cycles (each adds 3 clean runs). Per cycle: foreground
id-set sha256 before == after (fg_sha_stable), g_fg_monotone, L2 bytes (active units + live blobs) per run, store commit +
`vmem verify` gates, idempotence of a second compaction. Negative control: FG over the cap -> rc 5 E_GATE fg_cap.
Synthetic store (real runs AUSENTE). Usage: python3 -m eval.x10_compaction --out <dir>"""
import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

from visioncore import canon, ids, layout, params, pipeline
from visioncore.errors import VqaError
from vmem import compact as C, gates, tiers, units as U
from vmem import gen as G
from eval.x9_vmem import build_units, commit

CYCLES = ((6, 7, 8), (9, 10, 11), (12, 13, 14))
RUN_FRAMES = 200
BEFORE_FIXED_B = 32768  # units/episodes/index/journal allowance (AMD-L2-1, [M] 17,128 B)
BEFORE_TILE_B = 26  # 16 B/tile incompressible sig64+xh64 floor + 10 B/tile compressible allowance (AMD-L2-1)


LU_META_B = 1600  # per look-unit metadata allowance (AMD-L2-3, [M] 97,693 B/gen at n_lu 62 -> 1,576 B/lu total, 1,279 B/lu above the 18,432 B base)
BEFORE_LOOK_B = 32  # one TileDesc row per tile of each look desc_blob


def l2_before_limit(W=1280, H=720, frames=RUN_FRAMES, n_look=0, n_lu=0):
    """G-E8 before-compaction limit, AMD-L2-3: n_I*26*N + 32*n_look + 1,600*n_lu + 32,768 B, N = tiles of the W x H grid,
    n_I = ceil(frames/K_GOP), n_look = tiles covered by look desc_blobs, n_lu = look units. n_look = n_lu = 0: AMD-L2-1 form."""
    n_i = -(-frames // params.PARAMS["GOP_K"])
    _s, gw, gh = layout.grid_params(W, H)
    return n_i * BEFORE_TILE_B * gw * gh + BEFORE_LOOK_B * n_look + LU_META_B * n_lu + BEFORE_FIXED_B


L2_LIMIT_BYTES = l2_before_limit(1280, 720)  # 720p reference: 968,768 B (SPEC_VMEM l.254, SPEC_INT §6)
AFTER_GEN_B = 18432  # per-generation metadata copy allowance (AMD-L2-2, [M] 16,529 B + 11.5 %)
AFTER_FIXED_B = 8192  # journal allowance (AMD-L2-2, [M] 3,182 B)
AFTER_LOOK_B = 32  # one TileDesc row per tile of the kept BG look desc_blob


def l2_after_limit(n_look=1, keep=G.VM_GEN_KEEP, n_lu=0):
    """G-E8 after-compaction limit at rollback-window expiry, AMD-L2-3: keep*(18,432 + 1,600*n_lu) + 32*n_look + 8,192 B
    (720p full-look workload n_look 3,728, n_lu 62: 598,016 B; n_lu = 0, n_look = 1: 81,952 B, AMD-L2-2 form)."""
    return keep * (AFTER_GEN_B + LU_META_B * n_lu) + AFTER_LOOK_B * n_look + AFTER_FIXED_B


L2_AFTER_LIMIT_BYTES = l2_after_limit(1)  # AMD-L2-2 base reference: 81,952 B (n_lu 0)
FULL_N_LOOK, FULL_N_LU = 3728, 62  # 720p S7 look workload: 60 macroblock + 2 hint looks (SPEC_VMEM §3.4)
L2_LIMIT_FULL_BYTES = l2_before_limit(1280, 720, RUN_FRAMES, FULL_N_LOOK, FULL_N_LU)  # 1,187,264 B
L2_AFTER_LIMIT_FULL_BYTES = l2_after_limit(FULL_N_LOOK, n_lu=FULL_N_LU)  # 598,016 B
CLEAN_SCENARIOS = ("clean_static", "gpu_jitter")  # SPEC_VMEM l.254 / SPEC_TIME l.276: the budget is scoped to CLEAN runs


def _look_body(vr, f, rows, m0, role):
    """One SPEC_VMEM §3.4 `type:"look"` body for the tile rows of an aligned Morton block at anchor frame f (ascending Morton)."""
    rows = rows[np.argsort(rows["morton"], kind="stable")]
    xh = rows["xh64"].astype("<u8")
    desc = rows.tobytes()
    return {"frame": f, "render_index": vr.frame_nums[f], "role": role, "m0": m0, "m1": int(rows[-1]["morton"]),
            "look": {"grid": vr.grid.ident, "n": len(rows), "ah64": hashlib.sha256(xh.tobytes()).hexdigest()[:16],
                     "desc_blob": hashlib.sha256(desc).hexdigest(), "y_q4_mean": int(rows["y_q4"].astype(np.int64).sum()) // len(rows)},
            "xh64": ["%016x" % int(v) for v in rows["xh64"]], "sig64": ["%016x" % int(v) for v in rows["sig64"]],
            "desc_hex": desc.hex()}


def anchor_frame(vr):
    """SPEC_VMEM §3.4 anchor keyframe: the I-frame of g0 (first captured frame of the first GOP); no GOP -> frame 0."""
    g = vr.gops()
    return vr.frame_nums.index(g[0]["frame_ids"][0]) if g and g[0].get("frame_ids") else 0


def anchor_looks(vr, hints=()):
    """S7 look producer (SPEC_VMEM §3.4): at the anchor keyframe, one `tile` look per macroblock (mb = m >> 6, aligned 8x8
    tiles; together they cover every tile exactly once) + one look per region hint of that frame (role from the hint, block =
    smallest aligned 4^k Morton block holding every tile under the bbox)."""
    f = anchor_frame(vr)
    tl = vr.tiles[f]
    mort = tl["morton"].astype(np.int64)
    out = [_look_body(vr, f, tl[(mort >> 6) == mb], int(mb) << 6, "tile") for mb in np.unique(mort >> 6)]
    W, H = vr.grid.W, vr.grid.H
    for h in hints:
        if h.get("i") != vr.frame_nums[f]:
            continue
        ms = [int(t[-4:], 16) for t in ids.field_ids_of_rect(h["bbox_px"], W, H, 0)]
        if not ms:
            continue
        k = 0
        while (min(ms) >> 2 * k) != (max(ms) >> 2 * k):
            k += 1
        base = min(ms) >> 2 * k
        out.append(_look_body(vr, f, tl[(mort >> 2 * k) == base], base << 2 * k, h["role"]))
    return out


def look_coverage(lines, N):
    """-> True iff the `tile` looks cover tiles 0..N-1 of the grid exactly once (JC3-1)."""
    seen = []
    for l in lines:
        if l["type"] == "look" and l["body"]["role"] == "tile":
            seen.extend(bytes.fromhex(l["body"]["desc_hex"])[i:i + 32] for i in range(0, len(l["body"]["desc_hex"]) // 2, 32))
    ms = [int.from_bytes(r[:2], "little") for r in seen]  # TILE_DT field 0 = morton u16 LE
    return len(ms) == N and len(set(ms)) == N


def build_spool(vr, run_n=1, app="x10", scene="s", hints=()):
    """VisionRun -> vmem.spool/v1 lines: every S4 region (desc = its tile rows), the S7 anchor looks (every macroblock + every
    hint, §3.4) and every GOP as an episode with its blob. The macroblock looks MUST cover every tile exactly once."""
    rid = "r_%06d_%08x" % (run_n, run_n)
    W, H = vr.grid.W, vr.grid.H
    sp = {"schema": "vmem.spool/v1", "run_id": rid}
    lines = [dict(sp, seq=0, type="run", body={
        "scope": {"app": app, "scene": scene, "res": "%dx%d" % (W, H), "grid": vr.grid.ident, "build_seq": run_n},
        "receipt_sha256": vr.frames_sha256, "profile_sha256": vr.params_sha256, "align_source": "render_index"})]
    for f, regs in enumerate(vr.regions):
        tl = vr.tiles[f]
        for r in regs:
            m0, m1 = int(r["block"][1], 16), int(r["block"][2], 16)
            rows = tl[[int(t["morton"]) in {int(a, 16) + k for a, b in r["spans"] for k in range(int(b, 16) - int(a, 16) + 1)}
                       for t in tl]]
            if not len(rows):
                continue
            lines.append(dict(sp, seq=len(lines), type="region", body={
                "id": r["id"], "frame": f, "cls": r["cls"], "bbox_px": r["bbox_px"], "xh64": "%016x" % int(rows[0]["xh64"]),
                "sig64": "%016x" % int(rows[0]["sig64"]), "render_index": vr.frame_nums[f], "role": "tile", "m0": m0, "m1": m1,
                "look": {"n": len(rows), "ah64": "%016x" % int(rows[0]["sig64"]), "y_q4_mean": int(rows[:]["y_q4"].mean())},
                "desc_hex": rows.tobytes().hex()}))
    for b in anchor_looks(vr, hints):
        lines.append(dict(sp, seq=len(lines), type="look", body=b))
    if not look_coverage(lines, vr.grid.N):
        raise VqaError("E_SCHEMA", {"reason": "look_coverage"})
    raw = vr.gop_files.get("gops.bin", b"")
    for g in vr.gops():
        lines.append(dict(sp, seq=len(lines), type="episode", body={
            "schema": "vqa.episode/v1", "episode": rid + "/g%d" % g["gop"], "run_id": rid, "gop": g["gop"], "f0": g["f0"],
            "f1": g["f1"], "n_frames": g["n_frames"], "cut": int(bool(g["cut"])), "gop_blob_sha256": g["gop_blob_sha256"],
            "gop_blob_hex": raw[g["offset"]:g["offset"] + g["length"]].hex(), "verdicts": [{"verdict": "PASS"}], "fg": False,
            "classes_ppm": {"CHANGED": 1}, "delta_bytes": g["delta_bytes"], "grid": vr.grid.ident,
            "params_sha256": vr.params_sha256, "frame_ids": g["frame_ids"]}))
    return lines


def store_bytes(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(root) for f in fs)


def store_breakdown(root):
    """Bytes per top-level store entry (blobs/, gen/, ...) plus the raw GOP blob total."""
    out = {}
    for d, _s, fs in os.walk(root):
        top = os.path.relpath(d, root).split(os.sep)[0]
        out[top] = out.get(top, 0) + sum(os.path.getsize(os.path.join(d, f)) for f in fs)
    return dict(sorted(out.items()))


def _analyze_synth(scenario, seed, W, H, n):
    """Synthetic capture -> pipeline.analyze with the scenario's region hints (as `vcore analyze --hints`); the hints ride on
    the VisionRun as `x10_hints` for the S7 look producer."""
    from vadapt.synth import synthetic as syn
    fr, _, hints = syn.render_scenario(scenario, seed, W, H, n)
    vr = pipeline.analyze(syn.SynthSource(fr, syn.build_manifest(fr, scenario, seed)), hints=hints)
    vr.x10_hints = hints
    return vr


def _ingest_run(vr, tmp):
    from vmem import ingest
    sp = os.path.join(tmp, "spool.jsonl")
    with open(sp, "w") as f:
        f.write("".join(canon.dumps(l) for l in build_spool(vr, hints=getattr(vr, "x10_hints", ()))))
    root = os.path.join(tmp, "store")
    ingest.ingest(root, sp)
    return root


def _info(vr, n, W, H, scenario):
    hints = getattr(vr, "x10_hints", ())
    return {"frames": n, "resolution": "%dx%d" % (W, H), "scenario": scenario,
            "looks_tile": len({int(m) >> 6 for m in vr.tiles[anchor_frame(vr)]["morton"]}),
            "looks_hint": sum(1 for h in hints if h.get("i") == vr.frame_nums[anchor_frame(vr)]),
            "regions": sum(len(r) for r in vr.regions), "gops": len(vr.gops()),
            "gops_bin_bytes": len(vr.gop_files.get("gops.bin", b""))}


def measure_run_l2(W=1280, H=720, n=RUN_FRAMES, scenario="flicker", seed=1):
    """Real path: synthetic n-frame capture -> pipeline.analyze -> spool -> vmem.ingest.ingest -> bytes of the whole store."""
    vr = _analyze_synth(scenario, seed, W, H, n)
    with tempfile.TemporaryDirectory(prefix="x10l2_") as tmp:
        root = _ingest_run(vr, tmp)
        return store_bytes(root), dict(_info(vr, n, W, H, scenario), breakdown=store_breakdown(root))


def expire_window(st, plan_gen=None):
    """X10 window-expiry protocol (DECISION_L2b (1)): after a compaction gen, commit VM_GEN_KEEP-1 content-identical acked
    gens so CURRENT = c + VM_GEN_KEEP - 1, then gc blobs + gens outside the window + compact the journal. Returns the
    gens left on disk; they MUST equal the window [CURRENT-VM_GEN_KEEP+1, CURRENT]."""
    from vmem import ingest as I
    for _ in range(G.VM_GEN_KEEP - 1):
        I.write_gen(st, *I._load(st))
    st.gc_blobs()
    st.compact_journal()
    cur = G.current_gen(st.root)
    return G.list_gens(st.root), list(range(cur - G.VM_GEN_KEEP + 1, cur + 1))


def measure_clean_l2(scenario, W=1280, H=720, n=RUN_FRAMES, seed=1):
    """One CLEAN n-frame run through analyze -> ingest: store bytes before and after clean compaction (the run aged out of
    the COMPACT_K_CLEAN window: k_clean=0) + blob gc. -> dict(scenario, before_bytes, after_bytes, info)."""
    from vmem import ingest as I
    from vmem.store import Store
    vr = _analyze_synth(scenario, seed, W, H, n)
    with tempfile.TemporaryDirectory(prefix="x10c_") as tmp:
        root = _ingest_run(vr, tmp)
        before = store_bytes(root)
        st = Store(root)
        with st.lock():
            active, archive = I._load(st)
            sizes = {s: os.stat(I.blob_path(root, s, e)).st_size for s, e in I.iter_blobs(root)}
            looks_before = [u["look"]["n"] for u in active if u["look"] and u["look"]["desc_blob"]]
            plan = C.compact(active, sizes, k_clean=0, gen=(st.current() or 0) + 1)
            I.write_gen(st, plan["active"], archive + plan["archive"])
            gens, window = expire_window(st)
            active, _ = I._load(st)
            looks = [u["look"]["n"] for u in active if u["look"] and u["look"]["desc_blob"]]
            n_look, n_lu = sum(looks), len(looks)
        return {"scenario": scenario, "before_bytes": before, "after_bytes": store_bytes(root),
                "info": dict(_info(vr, n, W, H, scenario), retired=len(plan["retired"]), breakdown=store_breakdown(root),
                             n_look=n_look, n_lu=n_lu, n_look_before=sum(looks_before),
                             n_lu_before=len(looks_before), gens=gens, window=window, window_ok=gens == window)}


def measure_clean_set(scenarios=CLEAN_SCENARIOS):
    return [measure_clean_l2(s) for s in scenarios]


def storage_verdict(measured, limit=L2_LIMIT_BYTES):
    """G-E8 storage: absent or non-integer measurement, or > limit, fails."""
    return isinstance(measured, int) and not isinstance(measured, bool) and 0 < measured <= limit


def _before_limit(m):
    info = m.get("info") if isinstance(m.get("info"), dict) else {}
    try:
        W, H = (int(v) for v in str(info.get("resolution", "1280x720")).split("x"))
        nl = info.get("n_look_before", 0)
        nu = info.get("n_lu_before", 0)
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (nl, nu)):
            return 0  # malformed measurement -> fails closed
        return l2_before_limit(W, H, int(info.get("frames", RUN_FRAMES)), nl, nu)
    except (ValueError, TypeError, VqaError):
        return 0  # malformed measurement -> fails closed


def _after_limit(m):
    info = m.get("info") if isinstance(m.get("info"), dict) else {}
    n = info.get("n_look", 1)
    nu = info.get("n_lu", 0)
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in (n, nu)):
        return 0  # malformed measurement -> fails closed
    return l2_after_limit(n, n_lu=nu)


def clean_storage_verdict(ms, scenarios=CLEAN_SCENARIOS):
    """G-E8 storage (SPEC_VMEM l.254, clean runs): every required clean scenario measured, before <= n_I*26*N + 32*n_look + 1,600*n_lu + 32,768 B and
    after <= keep*(18,432 + 1,600*n_lu) + 32*n_look + 8,192 B at window expiry with list_gens == window (AMD-L2-3). The limits follow the measurement's grid, n_look and n_lu (no info -> 720p/200, n_look = n_lu = 0).
    Absent/malformed/missing scenario -> False."""
    if not isinstance(ms, (list, tuple)):
        return False
    got = {m.get("scenario"): m for m in ms if isinstance(m, dict)}
    return all(s in got and storage_verdict(got[s].get("before_bytes"), _before_limit(got[s]))
               and storage_verdict(got[s].get("after_bytes"), _after_limit(got[s]))
               and (got[s].get("info") or {}).get("window_ok", True) is True for s in scenarios)


def _num(u):
    return int(u["id"][-6:])


def l2_bytes(active, sizes):
    live = set()
    for u in active:
        live |= U.blob_refs(u)
    return sum(len(U.dump_unit(u).encode("utf-8")) for u in active) + sum(sizes.get(b, 0) for b in live)


def _row(item, ok, **kw):
    r = {"schema": "vqa.eval-row/v1", "x": "X10", "item": item, "tier": "PX", "rc": 0 if ok else 5, "kill": None,
         "ok": ok, "absence_reason": "synthetic store; real runs/captures AUSENTE"}
    r.update(kw)
    return r


def run(out_dir=None, measure=measure_clean_set, advisory=measure_run_l2):
    rows = []
    with tempfile.TemporaryDirectory(prefix="x10_") as tmp:
        root = os.path.join(tmp, "store")
        us, blobs, _ = build_units(range(1, 6))
        sizes = {canon.sha256_bytes(b): len(b) for b, _e in blobs}
        commit(root, us, blobs)
        archive = []
        base_l2 = l2_bytes(us, sizes)
        rows.append(_row("initial_5_runs", True, cycle=0, runs=5, l2_bytes=base_l2, l2_bytes_per_run=base_l2 // 5,
                         fg_bytes=C.fg_bytes(us, sizes)))
        stable, cycles_ok, last = [], 0, 5
        for c, runs in enumerate(CYCLES, 1):
            n0 = max(_num(u) for u in us + archive)
            new, nb, _ = build_units(runs, fail_runs=(), n_start=n0)
            for b, e in nb:
                sizes[canon.sha256_bytes(b)] = len(b)
            blobs = nb
            us = us + new
            before = C.fg_ids_sha256(us + archive)
            out = C.compact(us, sizes)
            after = C.fg_ids_sha256(out["active"] + out["archive"])
            try:
                mono = C.g_fg_monotone(us, out["active"] + out["archive"])
            except VqaError:
                mono = False
            again = C.compact(out["active"], sizes, gen=c + 100)
            idem = again["fg_ids_sha256"] == out["fg_ids_sha256"] and again["retired"] == []
            us, archive = out["active"], archive + out["archive"]
            try:
                commit(root, us, blobs, archive)
                failed = gates.failed(gates.verify(root))
            except VqaError as e:  # the store's own strict commit gate refused the generation
                failed = ["commit:" + str(e.detail.get("reason", e.code))]
            last = runs[-1]
            l2 = l2_bytes(us, sizes)
            ok = before == after and mono and idem and not failed and len(out["retired"]) > 0
            stable.append(before == after)
            cycles_ok += ok
            rows.append(_row("cycle_%d" % c, ok, cycle=c, runs=last, fg_sha_stable=before == after, g_fg_monotone=mono,
                             idempotent=idem, retired=len(out["retired"]), dropped_blobs=len(out["dropped_blobs"]),
                             l2_bytes=l2, l2_bytes_per_run=l2 // last, fg_bytes=C.fg_bytes(us, sizes),
                             verify_failed_gates=failed))
        tbl = dict(tiers.DEFAULTS, FG_BLOCK_PERMILLE=0)
        try:
            C.compact(us, sizes, table=tbl)
            ctl_code, ctl_rc, ctl_reason = "OK", 0, None
        except VqaError as e:
            ctl_code, ctl_rc, ctl_reason = e.code, e.rc, e.detail.get("reason")
        ctl = (ctl_code, ctl_rc, ctl_reason) == ("E_GATE", 5, "fg_cap")
        rows.append(_row("fg_cap_control", ctl, code=ctl_code, reason=ctl_reason))
    try:
        clean = measure() if measure else None
    except Exception as e:  # a failed measurement is an absent measurement -> FAIL, never PASS
        clean = None
        rows.append(_row("l2_clean_error", False, error=type(e).__name__ + ":" + str(e)[:200]))
    store_ok = clean_storage_verdict(clean)
    for sc in CLEAN_SCENARIOS:
        m = next((m for m in (clean or []) if isinstance(m, dict) and m.get("scenario") == sc), None)
        ok = clean_storage_verdict([m], (sc,)) if m else False
        rows.append(_row("l2_clean_" + sc, ok, l2_before_bytes=(m or {}).get("before_bytes"),
                         l2_after_bytes=(m or {}).get("after_bytes"), l2_before_limit_bytes=_before_limit(m or {}),
                         l2_after_limit_bytes=_after_limit(m or {}), **((m or {}).get("info") or {})))
    heavy_bytes, heavy_info = None, {}
    if advisory:
        try:
            heavy_bytes, heavy_info = advisory()
        except Exception as e:
            heavy_info = {"error": type(e).__name__ + ":" + str(e)[:200]}
    rows.append(_row("l2_heavy_flicker_advisory", True, advisory=True, l2_bytes_measured=heavy_bytes,
                     l2_limit_bytes=L2_LIMIT_BYTES, note="heavy noise-driven run; KNOWN_LIMITATIONS KL-1; never PASS/FAIL-affecting",
                     **heavy_info))
    l2_final = next(r["l2_bytes"] for r in reversed(rows) if r["item"].startswith("cycle_") or r["item"] == "initial_5_runs")
    cl = {m.get("scenario"): m for m in (clean or []) if isinstance(m, dict)}
    summary = {"schema": "vqa.eval-summary/v1", "x": "X10", "gate": "G-E8", "cycles": len(CYCLES), "cycles_ok": cycles_ok,
               "fg_sha_stable": all(stable) and bool(stable), "fg_cap_control_rejected": ctl,
               "l2_bytes_initial_5_runs": base_l2, "l2_bytes_final": l2_final, "runs_final": last,
               "l2_bytes_per_run_final": l2_final // last,
               "l2_target": "CLEAN 200-frame runs through analyze -> vmem.ingest: <= n_I*26*N + 32*n_look + 1,600*n_lu + 32,768 B before (720p n_look 3,728, n_lu 62: 1,187,264 B, AMD-L2-3) / <= VM_GEN_KEEP*(18,432 + 1,600*n_lu) + 32*n_look + 8,192 B after clean compaction at window expiry (720p 598,016 B, AMD-L2-3)",
               "l2_clean_before_bytes": {k: cl[k].get("before_bytes") for k in sorted(cl)},
               "l2_clean_after_bytes": {k: cl[k].get("after_bytes") for k in sorted(cl)},
               "l2_limit_bytes": L2_LIMIT_FULL_BYTES, "l2_after_limit_bytes": L2_AFTER_LIMIT_FULL_BYTES, "l2_storage_ok": store_ok,
               "l2_heavy_flicker_advisory_bytes": heavy_bytes,
               "verdict": "PASS" if cycles_ok == len(CYCLES) and all(stable) and ctl and store_ok else "FAIL",
               "source": "synthetic"}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        canon.write_atomic(out / "X10.jsonl", "".join(canon.dumps(r) for r in rows).encode())
        canon.write_atomic(out / "X10.summary.json", canon.dumps(summary).encode())
    return rows, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    _, s = run(a.out)
    rc = 0 if s["verdict"] == "PASS" else 5
    sys.stdout.write(canon.dumps(dict(s, code="OK" if rc == 0 else "E_GATE", rc=rc)))
    return rc


if __name__ == "__main__":
    sys.exit(main())
