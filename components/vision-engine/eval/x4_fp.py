"""X4 - PX false positives: clean run + 5 clean controls x N seeds (default 30), 40 frames each.
Gate G-E2 (SPEC_INT section 3): 0 run-level FP and clean_fp_tile <= 100 ppm. A fault-class tile is cls in
{FLICKER, FROZEN, DRIFT} on any frame >= 1 (these trigger vision FAIL checks); on static controls any cls >= 2 is also a FP.
Also reports the empirical DRIFT FP rate (O3 calibration) and a one-sided Clopper-Pearson upper bound. 0 tokens."""
import argparse
import hashlib
import math
import sys
from pathlib import Path

import numpy as np

from eval import splits as SPL
from eval.x3_power import cp_upper
from tests.px_mutants import driver as D
from visioncore import canon
from visioncore.errors import VqaError
from vadapt.synth import render_scenario
from vadapt.synth.synthetic import DEF_H, DEF_W, _animate, band_rect, noise
from visioncore import params as P

N_FRAMES = 40
FP_TILE_MAX_PPM = 100  # clean_fp_tile <= 1e-4
TEST_SEEDS = 30        # seeds 0..29 = test split (G-E2 n=30 per control); 30..39 = calibration split (DRIFT_* fit)
FIT_SEEDS = tuple(range(30, 40))   # pre-fit partition, declared before any fit (SPEC_INT 3 ADV-15.1)
TEST_SEED_SET = tuple(range(TEST_SEEDS))
BOUND_NUM, BOUND_DEN = 68, 100000000   # SEAL-1 tile-FP bound 6.8e-7, exact rational
SEAL_W, SEAL_H = 640, 400              # 1000 tiles x 39 transitions x 180 runs = 7.02 M tile-frames >= 4.405 M
# ADV_SEAL1 1: seeds 0..29 were inspected during diagnosis (DIAG_X4) -> contaminated, kept only for unit fixtures.
# The G-E2 held-out split is the pre-registered manifest eval/splits/x4_v3.json (test 2000..2029, cal 30..199);
# run()/main() bind to it and refuse anything else. Pinned file sha = pre-registration.
# x4_v2.json (test 1000..1029) was CONSUMED by W6 at a66d99e: retired for X4/X3, kept pinned for audit only.
MANIFEST_SHA256 = "5269d68e99d1d421b1ca7b43055ad0c9b0bde812218aabb0176890ba29584b5d"
MANIFEST_V2_SHA256 = "175bf43bdc5b3a60980c4d52325269b8aa904038cc5202a67a5917c4e55b7c28"
FAULT = (4, 5, 6)      # FLICKER, FROZEN, DRIFT
# control -> (static: any dirty tile is a FP)
CONTROLS = {"clean_static": True, "gpu_jitter": True, "iid_noise": True, "shader_pulse": False, "scroll": False,
            "animated_band": False}
SINE = (0, 3, 6, 9, 11, 12, 12, 12, 11, 9, 6, 3, 0, -3, -6, -9, -11, -12, -12, -12, -11, -9, -6, -3)  # +-12 luma, period 24


def build_control(name, seed, W=DEF_W, H=DEF_H, n=N_FRAMES):
    base_seed = 1 + seed   # injective: distinct seeds never render the same capture (old mod-6 map repeated scenes)
    if name in ("clean_static", "gpu_jitter", "scroll"):
        frames, _, _ = render_scenario(name, base_seed, W, H, n)
        return [f.copy() for f in frames]
    frames, _, _ = render_scenario("clean_static", base_seed, W, H, n)
    out = []
    for i, f in enumerate(frames):
        f = f.copy()
        if name == "iid_noise":
            d = (noise(base_seed, 300 + i, f.shape[:2]) % 3).astype(np.int16) - 1  # +-1 LSB per pixel
            f = np.clip(f.astype(np.int16) + d[..., None], 0, 255).astype(np.uint8)
        elif name == "shader_pulse":
            f = np.clip(f.astype(np.int16) + SINE[i % len(SINE)], 0, 255).astype(np.uint8)
        else:  # animated_band: legitimately animated content next to static HUD
            _animate(f, band_rect(W, H), base_seed, i)
        out.append(f)
    return out


def config_doc(W, H, n):
    return {"W": W, "H": H, "frames": n, "params_sha256": P.params_sha256(), "profile": "default"}


def _sha(doc):
    return hashlib.sha256(canon.dumps(doc).encode()).hexdigest()


def config_sha(W=SEAL_W, H=SEAL_H, n=N_FRAMES):
    return _sha(config_doc(W, H, n))


def partition_doc():
    return {"fit": list(FIT_SEEDS), "test": list(TEST_SEED_SET), "controls": list(CONTROLS)}


def partition_sha():
    """sha of the seed partition declared in this module: fit 30..39 / test 0..29 (frozen constants)."""
    return _sha(partition_doc())


def split_of(seed):
    return "test" if seed in TEST_SEED_SET else "cal" if seed in FIT_SEEDS else "none"


def load_manifest(path=SPL.X4_V3, pinned=MANIFEST_SHA256):
    return SPL.load(path, pinned)


def _check_cfg(manifest, W, H, n):
    c = manifest.config["x4"]
    if (c["W"], c["H"], c["frames"]) != (W, H, n):
        SPL.leak("config_not_registered", "X4", got=[W, H, n], want=[c["W"], c["H"], c["frames"]])


def run_trial(name, seed, W=DEF_W, H=DEF_H, n=N_FRAMES, manifest=None):
    """With a manifest: the (control, seed) must be registered and the rendered capture sha must match it BEFORE any
    classification (else rc 5 split_leak); the row takes its split from the manifest and records the manifest sha."""
    if manifest is not None:
        _check_cfg(manifest, W, H, n)
        manifest.entry("x4", (name, seed))
    frames = build_control(name, seed, W, H, n)
    cap = hashlib.sha256()
    for f in frames:
        cap.update(f.tobytes())
    entry = manifest.verify("x4", (name, seed), cap.hexdigest()) if manifest is not None else None
    g, cls, _, _ = D.classify_frames(frames)
    c = cls[1:]
    per = {D.CLS[k]: int((c == k).sum()) for k in range(7)}
    fault = int(np.isin(c, FAULT).sum())
    dirty = int((c >= 2).sum())
    fp = dirty if CONTROLS[name] else fault
    row = {"schema": "vqa.eval-row/v1", "x": "X4", "item": name, "tier": "PX", "control": name, "seed": seed, "rep": 0,
           "tile_frames": int(c.size), "fault_tiles": fault, "dirty_tiles": dirty, "fp_tiles": fp,
           "drift_tiles": per["DRIFT"], "classes": per, "fp": fp > 0,
           "split": split_of(seed), "capture_sha256": cap.hexdigest(), "partition_sha256": partition_sha(),
           "config_sha256": _sha(config_doc(W, H, n))}
    if entry is not None:
        row.update(split=entry["split"], manifest_sha256=manifest.sha, partition_sha256=manifest.sha)
    return row


def required_tile_frames():
    """Smallest N with (1 - p)^N <= 0.05 at p = 6.8e-7 (zero-FP exact one-sided CP95), integer-checked."""
    n = int(math.log(20) / -math.log1p(-BOUND_NUM / BOUND_DEN))
    while (1 - BOUND_NUM / BOUND_DEN) ** n > 0.05:
        n += 1
    return n


def _leak(why, **kw):
    raise VqaError("E_GATE", dict(kw, reason="split_leak", x="X4", why=why))


def check_binding(rows, seeds=TEST_SEEDS, config=None):
    """Validate every row against the frozen partition, never against its own label. Anything unbound, relabelled,
    drawn from the fit seeds, duplicated, or off-config -> rc 5 E_GATE split_leak."""
    want_p, want_c = partition_sha(), config or config_sha()
    seen, caps = set(), set()
    for r in rows:
        for k in ("seed", "control", "capture_sha256", "partition_sha256", "config_sha256", "split"):
            if r.get(k) in (None, ""):
                _leak("unbound_row", missing=k)
        if r["partition_sha256"] != want_p:
            _leak("partition_sha_mismatch")
        if r["config_sha256"] != want_c:
            _leak("config_sha_mismatch")
        if split_of(r["seed"]) != "test" or r["split"] != "test":
            _leak("not_test_seed", seed=r["seed"], label=r["split"])
        if r["control"] not in CONTROLS or r["seed"] >= seeds:
            _leak("outside_registered_set", seed=r["seed"])
        key = (r["control"], r["seed"])
        if key in seen or r["capture_sha256"] in caps:
            _leak("duplicate_row", seed=r["seed"])
        seen.add(key)
        caps.add(r["capture_sha256"])


def summarize(rows, seeds=TEST_SEEDS, config=None):
    """Legacy v1 partition (seeds 0..29, contaminated per ADV_SEAL1 1; unit fixtures only, never the G-E2 seal path).
    PASS also needs the full sample (every control x every test seed) and exact one-sided CP95 tile UB <= 6.8e-7."""
    check_binding(rows, seeds, config)
    return _stats(rows, seeds, config)


def _stats(rows, seeds, config=None):
    """config = the expected (frozen) config sha the rows were validated against; the summary records it, never a row's."""
    tf = sum(r["tile_frames"] for r in rows)
    fpt = sum(r["fp_tiles"] for r in rows)
    runs_fp = sum(r["fp"] for r in rows)
    drift = sum(r["drift_tiles"] for r in rows)
    per = []
    for n in CONTROLS:
        r = [x for x in rows if x["control"] == n]
        per.append({"control": n, "runs": len(r), "fp_runs": sum(x["fp"] for x in r), "fp_tiles": sum(x["fp_tiles"] for x in r),
                    "tile_frames": sum(x["tile_frames"] for x in r)})
    ppm = (fpt * 1000000) // tf if tf else 0
    ub = cp_upper(fpt, tf) if tf else 1.0
    bound_met = bool(tf) and ub * BOUND_DEN <= BOUND_NUM
    complete = len(rows) == len(CONTROLS) * seeds and tf >= required_tile_frames()
    ok = bool(rows) and runs_fp == 0 and ppm <= FP_TILE_MAX_PPM and bound_met and complete
    return {"schema": "vqa.eval-summary/v1", "x": "X4", "gate": "G-E2", "seeds": seeds, "runs": len(rows), "fp_runs": runs_fp,
            "tile_frames": tf, "required_tile_frames": required_tile_frames(), "sample_complete": complete,
            "fp_tiles": fpt, "clean_fp_tile_ppm": ppm,
            "run_fp_cp95_upper_ppm": -int(-cp_upper(runs_fp, len(rows)) * 1e6 // 1) if rows else 0,
            "tile_fp_cp95_upper_ppb": -int(-ub * 1e9 // 1), "tile_bound_ppb": 680, "bound_met": bound_met,
            "partition_sha256": partition_sha(), "config_sha256": config or config_sha(),
            "drift_fp_tile_frames": drift, "drift_fp_ppm": (drift * 1000000) // tf if tf else 0,
            "controls": per, "verdict": "PASS" if ok else "FAIL"}


def manifest_config_sha(manifest):
    """Frozen evaluation config of the held-out receipt (JC3-2): the manifest's registered x4 config (W, H, frames)
    bound to the fitted parameter table (params_sha256 of the effective defaults, profile "default")."""
    c = manifest.config["x4"]
    return _sha(config_doc(c["W"], c["H"], c["frames"]))


def check_manifest_binding(rows, manifest):
    """Held-out binding (ADV_SEAL1 1, JC3-2): every row carries this manifest's sha as manifest and partition sha, the
    frozen config sha (registered config + fitted params), is a registered TEST key whose capture sha equals the
    registered one, no duplicates. Anything else (incl. a missing field) -> rc 5 E_GATE split_leak."""
    seen, want_c = set(), manifest_config_sha(manifest)
    for r in rows:
        if r.get("manifest_sha256") != manifest.sha:
            SPL.leak("manifest_sha_mismatch", "X4", seed=r.get("seed"))
        if r.get("partition_sha256") != manifest.sha:
            SPL.leak("partition_sha_mismatch", "X4", seed=r.get("seed"))
        if r.get("config_sha256") in (None, ""):
            SPL.leak("unbound_row", "X4", seed=r.get("seed"), missing="config_sha256")
        if r["config_sha256"] != want_c:
            SPL.leak("config_sha_mismatch", "X4", seed=r.get("seed"), got=r["config_sha256"], want=want_c)
        key = (r.get("control"), r.get("seed"))
        e = manifest.verify("x4", key, r.get("capture_sha256"))
        if e["split"] != "test" or r.get("split") != "test":
            SPL.leak("not_test_seed", "X4", seed=r.get("seed"), label=r.get("split"))
        if key in seen:
            SPL.leak("duplicate_row", "X4", seed=r.get("seed"))
        seen.add(key)


def summarize_manifest(rows, manifest):
    """G-E2 verdict over the pre-registered held-out split: binding as above, PASS also needs every registered test key."""
    check_manifest_binding(rows, manifest)
    test = manifest.keys("x4", "test")
    s = _stats(rows, len({k[1] for k in test}), manifest_config_sha(manifest))
    complete = len(rows) == len(test) and s["tile_frames"] >= required_tile_frames()
    ok = s["verdict"] == "PASS" and complete
    return dict(s, sample_complete=complete, verdict="PASS" if ok else "FAIL", partition_sha256=manifest.sha,
                manifest_sha256=manifest.sha, split_id=manifest.doc.get("split_id"), params_sha256=P.params_sha256())


def run_rows(seeds=30, out_dir=None, controls=None, seed_start=0, W=SEAL_W, H=SEAL_H, n=N_FRAMES, manifest=None,
             split="test"):
    """Without a manifest (unit fixtures only): legacy seeds, seed_start >= 30 = calibration rows, never summarised.
    With a manifest: the registered keys of `split` (first `seeds` seeds per control); the test split is summarised."""
    if manifest is not None:
        c = manifest.config["x4"]
        W, H, n = c["W"], c["H"], c["frames"]
        keys = [k for k in manifest.keys("x4", split) if controls is None or k[0] in controls]
        per = sorted({k[1] for k in keys})[:seeds]
        rows = [run_trial(c, s, W, H, n, manifest) for c, s in keys if s in per]
        summ = summarize_manifest(rows, manifest) if controls is None and split == "test" else None
        return _write(rows, summ, out_dir)
    rows = [run_trial(c, s, W, H, n) for c in (controls or CONTROLS) for s in range(seed_start, seed_start + seeds)]
    summ = summarize(rows, seeds, _sha(config_doc(W, H, n))) if controls is None and seed_start < TEST_SEEDS else None
    return _write(rows, summ, out_dir)


def _write(rows, summ, out_dir):
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "X4.jsonl").write_text("".join(canon.dumps(r) for r in rows))
        if summ:
            (out / "X4.summary.json").write_text(canon.dumps(summ))
    return rows, summ


def run(out_dir=None, seeds=30, manifest=None):
    """Runner entry point (E3 run_all): held-out split of the pinned manifest only -> summary dict (PASS|FAIL)."""
    return run_rows(seeds, out_dir, manifest=manifest or load_manifest())[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="X4 PX false positives (G-E2)")
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--seed-start", type=int, default=0, help=">= 30 writes calibration rows only (no gate)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    a = ap.parse_args(argv)
    if a.seed_start >= TEST_SEEDS:
        rows, _ = run_rows(a.seeds, a.out, manifest=load_manifest(), split="cal")
        sys.stdout.write(canon.dumps({"code": "OK", "rc": 0, "split": "cal", "runs": len(rows)}))
        return 0
    s = run(a.out, a.seeds)
    ok = s["verdict"] == "PASS"
    sys.stdout.write(canon.dumps({"code": "OK" if ok else "E_GATE", "rc": 0 if ok else 5, "gate": "G-E2", "verdict": s["verdict"],
                                  "fp_runs": s["fp_runs"], "runs": s["runs"], "clean_fp_tile_ppm": s["clean_fp_tile_ppm"]}))
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
