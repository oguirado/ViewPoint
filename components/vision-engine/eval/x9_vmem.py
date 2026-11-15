"""X9 visual memory (G-E8): synthetic store of 5 runs (3 clean, 2 FAIL), 20 answerable queries (12 route, 4 last-good,
4 diff-region) + 5 unrecorded queries that MUST abstain NOT_IN_VISUAL_MEMORY (rc 3).
Metrics: recall@5 (route), exact (last-good/diff-region), tokens/answer <= 2,520, cite_valid via check_answer, verify gates.
The store is synthetic (vmem has no ingest yet; real captures AUSENTE). Usage: python3 -m eval.x9_vmem --out <dir>"""
import argparse
import hashlib
import os
import struct
import sys
import tempfile
from pathlib import Path

from visioncore import canon, layout
from visioncore.errors import VqaError
from vmem import gates, router, units as U
from vmem.store import Store

APP, SCENE, W, H = "eval_demo", "hud", 1280, 720
GRID = "grid:1280x720:s16"
SUBJECTS = (("ammo_label", 0x0a40, 0x0a7f, "ammo counter label baseline ok"),
            ("health_bar", 0x0b00, 0x0b3f, "health bar fill baseline ok"),
            ("minimap_frame", 0x0c00, 0x0c3f, "minimap frame border baseline ok"),
            ("score_ticker", 0x0d00, 0x0d3f, "score ticker text baseline ok"))
LOOK_CHANGE_BUILD = {"health_bar": 5, "minimap_frame": 3}  # first build whose look differs from build 1
CROP_BYTES = 2048


def tile(xh, sig=0, y=100, sq=50):
    return struct.pack(layout.TILE_FMT, 0, 0, 0, y, 0, 0, sq, 0, 0, 0, 0, 0, sig, xh)


T1 = b"".join(tile(100 + i, sig=0xF0F0 + i) for i in range(4))
T3 = b"".join(tile(900 + i, sig=0x0F0F << 20) for i in range(4))
SHA = {T1: canon.sha256_bytes(T1), T3: canon.sha256_bytes(T3)}


def run_id(i):
    return "r_%06d_%08x" % (i, 0x77abe900 + i)


def crop(i, j):
    return hashlib.shake_256(b"crop%d/%d" % (i, j)).digest(CROP_BYTES)


def anchor(run, cr=None, sig="0" * 16, frame=3):
    return {"run_id": run, "cite": run + "/f%d:t0a40" % frame, "frame": frame, "render_index": frame,
            "bbox_px": [0, 0, 32, 32], "mv_px": [0, 0], "xh64": "0" * 16, "sig64": sig, "crop": cr, "desc_blob": None}


def look(tiles, ah):
    return {"grid": GRID, "n": len(tiles) // layout.TILE_SIZE, "ah64": ah, "y_q4_mean": 100, "desc_blob": SHA[tiles]}


def tk_of(sub):
    s = next(x for x in SUBJECTS if x[0] == sub)
    return U.track_key(APP, SCENE, W, H, "ui", s[1], s[2])


def build_units(run_nums, fail_runs=(2, 4), with_crops=True, n_start=0):
    """-> (units, blobs[(bytes, ext)], expect). Clean runs pin GOLDEN baselines; FAIL runs pin FAIL defect+verdict."""
    us, blobs, n = [], {T1: "bin", T3: "bin"}, n_start
    last_clean = max([r for r in run_nums if r not in fail_runs] or [0])
    expect = {"last_good": {}}
    for i in run_nums:
        rid, sc = run_id(i), {"app": APP, "scene": SCENE, "res": "1280x720", "grid": GRID, "build_seq": i}
        fail = i in fail_runs
        for j, (sub, m0, m1, text) in enumerate(SUBJECTS):
            tk = tk_of(sub)
            changed = i >= LOOK_CHANGE_BUILD.get(sub, 1 << 30)
            tl = T3 if changed else T1
            n += 1
            u = U.make_unit(n, "baseline", sc, tk, pin=None if fail else "GOLDEN", subject=sub, summary=text,
                            look=look(tl, ("3" if changed else "1") * 16), anchors=[anchor(rid)])
            us.append(u)
            if i == last_clean and not fail:
                expect["last_good"][sub] = u["id"]
        n += 1
        cr = None
        if with_crops:
            c = crop(i, 0)
            blobs[c] = "png"
            cr = canon.sha256_bytes(c)
        us.append(U.make_unit(n, "episode", sc, tk_of("ammo_label"), subject="ammo_label", cls="UNCHANGED",
                              verdict="FAIL" if fail else "PASS", pin="FAIL" if fail else None,
                              check_id="render.flicker" if fail else None,
                              summary="episode run %d %s" % (i, "fail" if fail else "pass"),
                              anchors=[anchor(rid, cr)]))
        if fail:
            sub, text = ("health_bar", "health bar flicker defect") if i == fail_runs[0] else \
                        ("score_ticker", "score ticker frozen defect")
            n += 1
            us.append(U.make_unit(n, "defect", sc, tk_of(sub), pin="FAIL", predicate="flicker" if sub == "health_bar" else "frozen",
                                  cls="FLICKER" if sub == "health_bar" else "FROZEN", subject=sub, verdict="FAIL",
                                  check_id="render.flicker", summary=text,
                                  anchors=[anchor(rid, sig="%016x" % (0xDEADBEEF12345678 ^ i))]))
    return us, [(b, e) for b, e in blobs.items()], expect


def store_files(us, archive=()):
    files = {"units/active.jsonl": "".join(canon.dumps(u) for u in us).encode(),
             "VISUAL_DIGEST.md": b"# digest\nscope eval_demo/hud synthetic runs\n",
             "catalogue/foreground.tsv": U.render_catalogue(list(us) + list(archive)).encode()}
    if archive:
        files["units/archive.jsonl"] = "".join(canon.dumps(u) for u in archive).encode()
    files.update(router.build_tk_index(us))
    files.update(router.build_sig_index(us))
    return files


def commit(root, us, blobs, archive=()):
    st = Store(root)
    with st.lock():
        return st.commit(store_files(us, archive), blobs)


ROUTE_Q = (("ammo counter", "ammo_label"), ("ammo label", "ammo_label"), ("health bar", "health_bar"),
           ("health fill", "health_bar"), ("minimap border", "minimap_frame"), ("minimap frame", "minimap_frame"),
           ("score ticker", "score_ticker"), ("score text", "score_ticker"), ("health flicker", "health_bar"),
           ("score frozen", "score_ticker"), ("flicker defect", "health_bar"), ("frozen defect", "score_ticker"))
UNRECORDED = (("route", {"q": "inventory grid slots"}), ("route", {"q": "dialog subtitle caption", "scope": APP + "/" + SCENE}),
              ("route", {"q": "ammo", "scope": "no_such_app/hud"}), ("route", {"q": "ammo", "tk": "T:" + APP + ":" + SCENE + ":1280x720:ui:0f00-0f3f"}),
              ("route", {"sig": 0xAAAAAAAAAAAAAAAA}))


def _row(qid, kind, ok, rc, **kw):
    r = {"schema": "vqa.eval-row/v1", "x": "X9", "item": qid, "tier": "PX", "query_kind": kind, "rc": rc, "kill": None,
         "ok": ok, "absence_reason": "synthetic store; real runs/captures AUSENTE"}
    r.update(kw)
    return r


def run(out_dir=None):
    rows = []
    with tempfile.TemporaryDirectory(prefix="x9_") as tmp:
        root = os.path.join(tmp, "store")
        us, blobs, expect = build_units(range(1, 6))
        commit(root, us, blobs)
        ver = gates.verify(root)
        gates_failed = gates.failed(ver)
        hits5 = top1 = 0
        max_tok = 0
        cite_ok = 0
        for k, (q, sub) in enumerate(ROUTE_Q):
            r = router.route(root, q=q)
            subs = [row[2] for row in r["r"]]
            want = tk_of(sub)
            in5 = want in subs
            hits5 += in5
            top1 += bool(subs) and subs[0] == want
            a = router.answer(root, q=q)
            max_tok = max(max_tok, a["budget_tok"])
            try:
                router.check_answer(root, a)
                cv = True
            except VqaError:
                cv = False
            cite_ok += cv
            rows.append(_row("route%02d" % k, "route", in5, 0, recall_at_5=in5, budget_tok=a["budget_tok"], cite_valid=cv))
        lg_ok = 0
        for sub, uid in sorted(expect["last_good"].items()):
            got = router.last_good(root, subject=sub)["unit_id"]
            lg_ok += got == uid
            rows.append(_row("last_good_" + sub, "last-good", got == uid, 0))
        dr_ok = 0
        for sub, _m0, _m1, _t in SUBJECTS:
            got = router.diff_region(root, tk_of(sub), from_build=1)
            fc = (got.get("first_change") or {}).get("build_seq")
            ok = fc == LOOK_CHANGE_BUILD.get(sub)
            dr_ok += ok
            rows.append(_row("diff_region_" + sub, "diff-region", ok, 0, first_change=fc))
        abst = 0
        for k, (_kind, kw) in enumerate(UNRECORDED):
            try:
                router.route(root, **kw)
                code, rc = "ANSWERED", 0
            except VqaError as e:
                code, rc = e.code, e.rc
            ok = (code, rc) == ("NOT_IN_VISUAL_MEMORY", 3)
            abst += ok
            rows.append(_row("unrecorded%02d" % k, "unrecorded", ok, rc, code=code))
    n_ans = len(ROUTE_Q) + len(expect["last_good"]) + len(SUBJECTS)
    recall_ppm = hits5 * 1000000 // len(ROUTE_Q)
    summary = {"schema": "vqa.eval-summary/v1", "x": "X9", "gate": "G-E8", "answerable": n_ans,
               "recall_at_5_ppm": recall_ppm, "top1": top1, "route_total": len(ROUTE_Q), "last_good_ok": lg_ok,
               "diff_region_ok": dr_ok, "unrecorded_total": len(UNRECORDED), "unrecorded_abstained": abst,
               "max_answer_tok": max_tok, "cite_valid": cite_ok, "verify_failed_gates": gates_failed,
               "l2_bytes_per_200_frame_run": "AUSENTE (see X10 for synthetic L2 bytes)",
               "verdict": "PASS" if (recall_ppm >= 850000 and abst == len(UNRECORDED) and max_tok <= 2520
                                     and lg_ok == len(expect["last_good"]) and dr_ok == len(SUBJECTS)
                                     and cite_ok == len(ROUTE_Q) and not gates_failed) else "FAIL",
               "source": "synthetic"}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        canon.write_atomic(out / "X9.jsonl", "".join(canon.dumps(r) for r in rows).encode())
        canon.write_atomic(out / "X9.summary.json", canon.dumps(summary).encode())
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
