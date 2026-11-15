"""X1 determinism (G-E1): 6 synthetic clean captures x 3 repeats; sha256 of every S0-S5 output must match across repeats
and between the serial arm (workers=1) and the parallel arm (workers=N>1) of every repeat.
Rows: vqa.eval-row/v1 JSONL (integers only). Usage: python3 -m eval.x1_determinism --out <dir> [--repeats 3]"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from visioncore import canon, pipeline
from vadapt.synth import synthetic as syn

PAR_WORKERS = 4   # parallel arm: threads/workers N > 1
CAPTURES = (("clean_static", 1), ("clean_static", 2), ("gpu_jitter", 1), ("gpu_jitter", 2), ("scroll", 1), ("scroll", 2))


def digest_run(name, seed, W=syn.DEF_W, H=syn.DEF_H, n=syn.DEF_N, workers=1):
    """Fresh render + fresh analyze -> {file: sha256} over every output blob (tiles, frames, regions, links, gops, vision.json)."""
    fr, _, _ = syn.render_scenario(name, seed, W, H, n)
    src = syn.SynthSource(fr, syn.build_manifest(fr, name, seed))
    run = pipeline.analyze(src, workers=workers)
    return {k: hashlib.sha256(v).hexdigest() for k, v in sorted(run._blobs().items())}


def _overall(d):
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def run(out_dir=None, repeats=3, captures=CAPTURES, W=syn.DEF_W, H=syn.DEF_H, n=syn.DEF_N, digest=digest_run,
        par_workers=PAR_WORKERS):
    """Every capture x repeat runs BOTH arms (workers=1, workers=par_workers) and records both digests; a row matches
    only if serial == parallel == the first serial digest of the capture. par_workers <= 1 -> FAIL (no parallel arm)."""
    rows, per = [], {}
    for name, seed in captures:
        item = "%s@s%d" % (name, seed)
        ref = None
        for rep in range(repeats):
            d1 = digest(name, seed, W, H, n, 1)
            dn = digest(name, seed, W, H, n, par_workers)
            o1, on = _overall(d1), _overall(dn)
            ref = o1 if ref is None else ref
            same = o1 == ref and on == ref and par_workers > 1
            per.setdefault(item, []).append(same)
            rows.append({"schema": "vqa.eval-row/v1", "x": "X1", "item": item, "tier": "PX", "rep": rep,
                         "rc": 0 if same else 5, "kill": None, "files_n": len(d1), "digest_sha256": o1,
                         "digest_parallel_sha256": on, "workers_serial": 1, "workers_parallel": par_workers,
                         "match_ref": same, "absence_reason": "no judge tokens; synthetic captures (real captures AUSENTE)"})
    ok = sum(all(v) for v in per.values())
    summary = {"schema": "vqa.eval-summary/v1", "x": "X1", "gate": "G-E1", "captures": len(per), "repeats": repeats,
               "runs": len(rows), "captures_identical": ok, "arms": {"serial": 1, "parallel": par_workers},
               "verdict": "PASS" if ok == len(per) and per and par_workers > 1 else "FAIL",
               "source": "synthetic"}
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        canon.write_atomic(out / "X1.jsonl", "".join(canon.dumps(r) for r in rows).encode())
        canon.write_atomic(out / "X1.summary.json", canon.dumps(summary).encode())
    return rows, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args(argv)
    _, s = run(a.out, a.repeats)
    sys.stdout.write(canon.dumps(dict(s, code="OK" if s["verdict"] == "PASS" else "E_DETERMINISM",
                                      rc=0 if s["verdict"] == "PASS" else 5)))
    return 0 if s["verdict"] == "PASS" else 5


if __name__ == "__main__":
    sys.exit(main())
