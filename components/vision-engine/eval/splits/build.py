"""Generator of the pre-registered PX split manifests eval/splits/x4_v{2,3}.json (ADV_SEAL1 1, SPEC_INT 3 ADV-15.1).

Renders the synthetic captures only (vadapt.synth via x4_fp.build_control / x3_power.build_trial) and hashes them;
it never classifies a frame, so no outcome on any seed is observed. Deterministic: same code -> byte-identical file.
    python3 -m eval.splits.build [--split x4_v3|x4_v2] [--out PATH]
x4_v2 (CONSUMED by W6 at a66d99e, kept for audit): X4 every control x (test 1000..1029 + cal 30..39); X3 test 1000..1029.
x4_v3 (live): X4 every control x (test 2000..2029 + cal 30..199, the enlarged H_DEAD fit split); X3 every mutant x level x
(test 2000..2029 + cal 40..69, the power check of the H_DEAD fit)."""
import argparse
import sys
from pathlib import Path

from eval.splits import SCHEMA, X4_V2, X4_V3, capture_sha
from visioncore import canon

TEST_SEEDS = tuple(range(1000, 1030))   # fresh held-out split, never inspected (seeds 0..29 are contaminated)
CAL_SEEDS = tuple(range(30, 40))        # calibration split, unchanged
# v3: v2 test seeds consumed by W6; fresh test seeds, CAL enlarged to 30..199 for the H_DEAD re-fit (never 1000..1029)
SPLITS = {"x4_v2": {"test_seeds": TEST_SEEDS, "cal_seeds": CAL_SEEDS, "x3_cal_seeds": (), "out": X4_V2},
          "x4_v3": {"test_seeds": tuple(range(2000, 2030)), "cal_seeds": tuple(range(30, 200)),
                    "x3_cal_seeds": tuple(range(40, 70)), "out": X4_V3}}


def build(test_seeds=TEST_SEEDS, cal_seeds=CAL_SEEDS, x4=None, x3=None, x3_cal_seeds=(), split_id="x4_v2"):
    """x4 = {W, H, frames, controls}; x3 = {frames, mutants, levels}; defaults = the SEAL configuration (v2 partition)."""
    from eval import x3_power as X3, x4_fp as X4
    x4 = x4 or {"W": X4.SEAL_W, "H": X4.SEAL_H, "frames": X4.N_FRAMES, "controls": list(X4.CONTROLS)}
    x3 = x3 or {"frames": X3.N_FRAMES, "mutants": list(X3.MUTANTS), "levels": 3}
    r4 = []
    for c in x4["controls"]:
        for split, seeds in (("cal", cal_seeds), ("test", test_seeds)):
            for s in seeds:
                fr = X4.build_control(c, s, x4["W"], x4["H"], x4["frames"])
                r4.append({"control": c, "seed": s, "split": split, "capture_sha256": capture_sha(fr)})
    r3 = []
    for mu in x3["mutants"]:
        for lv in range(x3["levels"]):
            for split, seeds in (("cal", x3_cal_seeds), ("test", test_seeds)):
                for s in seeds:
                    fr, _ = X3.build_trial(X3.trial_spec(mu, lv, s))
                    r3.append({"mutant": mu, "level": lv, "seed": s, "split": split, "capture_sha256": capture_sha(fr)})
    r4.sort(key=lambda e: (e["control"], e["seed"]))
    r3.sort(key=lambda e: (e["mutant"], e["level"], e["seed"]))
    part = {"cal": list(cal_seeds), "test": list(test_seeds)}
    if x3_cal_seeds:
        part["x3_cal"] = list(x3_cal_seeds)
    return {"schema": SCHEMA, "split_id": split_id, "generator": "eval/splits/build.py", "partition": part,
            "config": {"x4": x4, "x3": x3}, "x4": r4, "x3": r3}


def main(argv=None):
    ap = argparse.ArgumentParser(description="build the pre-registered X4/X3 split manifest (renders + hashes only)")
    ap.add_argument("--split", choices=sorted(SPLITS), default="x4_v3")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    sp = SPLITS[a.split]
    a.out = a.out or str(sp["out"])
    text = canon.dumps(build(sp["test_seeds"], sp["cal_seeds"], x3_cal_seeds=sp["x3_cal_seeds"], split_id=a.split))
    Path(a.out).write_text(text)
    import hashlib
    sys.stdout.write(canon.dumps({"code": "OK", "rc": 0, "out": a.out,
                                  "manifest_sha256": hashlib.sha256(text.encode()).hexdigest()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
