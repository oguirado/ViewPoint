"""X8 citation integrity (G-E6): 10 corrupted citations x 25 synthetic items fed to the S6 verifier (visioncore.cite).
Valid arm: one real pointer per item (track / region from the run, else a static_content field) must be accepted.
Corrupt arm: 10 deterministic corruptions of that valid result must each be rejected (rc 3, FAIL -> CANNOT_SEE).
Rows: vqa.eval-row/v1 JSONL, integers only. Usage: python3 -m eval.x8_citations --out <dir>"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

from visioncore import canon, cite, cli, ids, layout, pack as packmod, pipeline
from vadapt.synth import synthetic as syn

CORRUPTIONS = ("truncated_morton", "padded_frame", "uppercase", "run_prefix", "frame_absent", "tile_outside_grid",
               "region_absent", "forged_quote", "wrong_reason", "no_pointer")
_CLS_REASON = {"FROZEN": "frozen", "FLICKER": "flicker", "DRIFT": "drift", "MOVED": "teleport", "CHANGED": "missing"}
_FLAG_REASON = (("FROZEN", "frozen"), ("FLICKER", "flicker"), ("CYCLE", "flicker"), ("DRIFT", "drift"))
N_ITEMS = 25


def items(n=N_ITEMS):
    out = [(name, seed) for seed in (1, 2, 3, 4) for name in syn.SCENARIOS]
    return out[:n]


def _jsonl(p):
    return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]


def _valid_candidate(vdir, v):
    """-> (pointer, reason, how). Derived from the run's own gops/regions files, never from the verifier."""
    for g in _jsonl(vdir / "gops.jsonl"):
        for t in g.get("tracks", []):
            for flag, reason in _FLAG_REASON:
                if flag in t.get("flags", []):
                    return t["k"], reason, "track"
    regs = _jsonl(vdir / "regions.jsonl")
    for r in regs:
        if r["cls"] in _CLS_REASON and r["cls"] != "CHANGED":
            return r["id"], _CLS_REASON[r["cls"]], "region"
    s, gw, gh = layout.grid_params(v["W"], v["H"])
    return ids.field_id(0, layout.morton(0, 0)), "static_content", "field"


def corrupt(kind, res, grid):
    p = res["pointers"][0]
    r = dict(res, pointers=list(res["pointers"]))
    r.pop("digest_quote", None)
    if kind == "truncated_morton":
        r["pointers"] = [p[:-1]]
    elif kind == "padded_frame":
        r["pointers"] = [p[0] + "0" + p[1:]]
    elif kind == "uppercase":
        r["pointers"] = [p.upper()]
    elif kind == "run_prefix":
        r["pointers"] = ["r_000104_77abe901/" + p]
    elif kind == "frame_absent":
        r["pointers"] = [ids.field_id(9999, 0)]
    elif kind == "tile_outside_grid":
        r["pointers"] = [ids.field_id(0, layout.morton(200, 200))]
    elif kind == "region_absent":
        r["pointers"] = ["r0:ffff"]
    elif kind == "forged_quote":
        r["digest_quote"] = res["digest_quote"] + " mv=9,9"
    elif kind == "wrong_reason":
        r["reason_class"] = "black"  # cut/flash evidence exists only for global/gop pointers
    elif kind == "no_pointer":
        r["pointers"] = []
    else:
        raise ValueError(kind)
    return r


def clopper_pearson_lb_all(n, alpha_ppm=50000):
    """One-sided 95 % lower bound for k = n successes in n trials: alpha^(1/n), returned as ppm (integer)."""
    return int((alpha_ppm / 1_000_000) ** (1.0 / n) * 1_000_000)


def build_item(name, seed, tmp):
    """-> (vision dict, pack dict, valid FAIL result, how). Runs analyze + pack in-process (same code as `vcore`)."""
    fr, _, _ = syn.render_scenario(name, seed)
    src = syn.SynthSource(fr, syn.build_manifest(fr, name, seed))
    vdir = Path(tmp) / ("%s_s%d" % (name, seed))
    pipeline.analyze(src, None, None, vdir)
    v = cite.load_vision(vdir)
    batch = cli._vision_batch(vdir)
    ptr, reason, how = _valid_candidate(vdir, v)
    kind, d = ids.parse_any(ptr)
    # X8 tests citation integrity, not whole-run media scheduling. A legal
    # one-GOP answer-pack containing the witness avoids mandatory-frame
    # overflow on cut-heavy scroll captures; the full analyzed evidence stays.
    witness = next(g for g in batch["gops"]
                   if d["frame"] in g["frame_ids"])
    batch["gops"] = [witness]
    pk = packmod.pack(batch).pack
    pk["api_version"] = v["api_version"]
    res = {"check_id": "x8", "status": "FAIL", "pointers": [ptr], "reason_class": reason,
           "digest_quote": cite.render_quote(ptr, v, kind, d)}
    return v, pk, res, how


def run(out_dir=None, n_items=N_ITEMS):
    rows, valid_ok, bad_rej, bad_n, per_kind = [], 0, 0, 0, {k: 0 for k in CORRUPTIONS}
    with tempfile.TemporaryDirectory(prefix="x8_") as tmp:
        for name, seed in items(n_items):
            item = "%s@s%d" % (name, seed)
            v, pk, res, how = build_item(name, seed, tmp)
            o = cite.verify_citations(v, {"results": [res]}, pk)
            ok = o["rc"] == 0
            valid_ok += ok
            rows.append({"schema": "vqa.eval-row/v1", "x": "X8", "item": item, "tier": "PX", "arm": "valid", "rc": o["rc"],
                         "kill": None, "accepted": ok, "pointer_kind": how,
                         "absence_reason": "no judge tokens; synthetic captures (real captures AUSENTE)"})
            for ck in CORRUPTIONS:
                c = corrupt(ck, res, v["grid"])
                oc = cite.verify_citations(v, {"results": [c]}, pk)
                out_status = oc["results"][0]["status_out"] if oc["results"] else None
                rejected = oc["rc"] == 3 and out_status == "CANNOT_SEE"
                bad_n += 1
                bad_rej += rejected
                per_kind[ck] += rejected
                rows.append({"schema": "vqa.eval-row/v1", "x": "X8", "item": item, "tier": "PX", "arm": "corrupt",
                             "corruption": ck, "rc": oc["rc"], "kill": rejected, "status_out": out_status,
                             "absence_reason": None})
    lb = clopper_pearson_lb_all(bad_n) if bad_rej == bad_n and bad_n else None
    summary = {"schema": "vqa.eval-summary/v1", "x": "X8", "gate": "G-E6", "items": len(items(n_items)),
               "valid_total": len(items(n_items)), "valid_accepted": valid_ok, "corrupt_total": bad_n,
               "corrupt_rejected": bad_rej, "per_corruption_rejected": per_kind, "reject_lb95_ppm": lb,
               "cite_relevant": "AUSENTE (needs real judge answers, X6)",
               "verdict": "PASS" if valid_ok == len(items(n_items)) and bad_rej == bad_n and bad_n else "FAIL",
               "source": "synthetic"}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        canon.write_atomic(out / "X8.jsonl", "".join(canon.dumps(r) for r in rows).encode())
        canon.write_atomic(out / "X8.summary.json", canon.dumps(summary).encode())
    return rows, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--items", type=int, default=N_ITEMS)
    a = ap.parse_args(argv)
    _, s = run(a.out, a.items)
    rc = 0 if s["verdict"] == "PASS" else 5
    sys.stdout.write(canon.dumps(dict(s, code="OK" if rc == 0 else "E_GATE", rc=rc)))
    return rc


if __name__ == "__main__":
    sys.exit(main())
