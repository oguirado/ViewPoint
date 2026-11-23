#!/usr/bin/env python3
"""P5 host report: compares current T0 fit with Hoeffding-bound still noise thresholds.

Inputs: run directory containing still_manifest.json (holding samples_q4) and rows.jsonl (bvqa.calib-row/v1).
Method:
  - T0_still = calibrate.t0_still(samples_q4)
  - T0_fp = calibrate.t0_fp(fit_rows) over FIT rows only
  - contaminated = calibrate.fit_t0(fit_rows, samples_q4)["contaminated"]
  - Hoeffding bound: for samples in 0..R, eps = 2 * R * sqrt(ln(2/alpha) / b)
  - Candidate threshold: mu + eps in q4 units per alpha in (0.01, 0.001, 0.0001) and b in (64, 256).
Outputs one JSON file and one Markdown table file to a new out dir.
"""
import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_visual_qa.truth import calibrate, score  # noqa: E402
from blender_visual_qa.truth.score import VqaError  # noqa: E402
from blender_visual_qa.truth.still_noise_bound import (  # noqa: E402, F401
    ALPHAS,
    B_TILES,
    compute_bounds,
)


def load_run(run_dir, seeds_spec=None):
    run_path = Path(run_dir).resolve()
    still_path = run_path / "still_manifest.json"
    rows_path = run_path / "rows.jsonl"

    if not still_path.exists():
        raise VqaError("E_SCHEMA", {"reason": f"missing still_manifest.json in {run_path}"})
    if not rows_path.exists():
        raise VqaError("E_SCHEMA", {"reason": f"missing rows.jsonl in {run_path}"})

    try:
        still_data = json.loads(still_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise VqaError("E_SCHEMA", {"reason": f"invalid still_manifest.json: {e}"})

    samples_q4 = still_data.get("samples_q4")
    if samples_q4 is None or not isinstance(samples_q4, list) or len(samples_q4) == 0:
        raise VqaError("E_SCHEMA", {"reason": "empty still probe"})

    rows = []
    for line in rows_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(score.canon.loads(line))

    # Determine seeds split
    seeds = None
    if seeds_spec:
        seeds = calibrate.parse_seeds(seeds_spec)
    else:
        # Check calib_report.json or report_pass.json or report.json
        for rname in ("calib_report.json", "report_pass.json", "report.json"):
            rp = run_path / rname
            if rp.exists():
                try:
                    rep_data = json.loads(rp.read_text(encoding="utf-8"))
                    if isinstance(rep_data.get("seeds"), dict) and rep_data["seeds"].get("fit"):
                        seeds = rep_data["seeds"]
                        break
                except Exception:
                    pass

    if not seeds and isinstance(still_data.get("still_meta"), dict) and still_data["still_meta"].get("seeds"):
        seeds = {"fit": list(still_data["still_meta"]["seeds"]), "gate": []}

    if seeds and seeds.get("fit"):
        fit_s = set(seeds["fit"])
        fit_rows = [r for r in rows if r.get("seed") in fit_s]
    else:
        fit_rows = list(rows)

    return samples_q4, rows, fit_rows, seeds


def render_markdown(doc):
    lines = [
        "# Still Noise Bound Report (P5)",
        "",
        f"- **T0_still**: {doc['T0_still']}",
        f"- **T0_fp**: {doc['T0_fp']}",
        f"- **frozen_t0_q4**: {doc['frozen_t0_q4']}",
        f"- **contaminated**: {doc['contaminated']}",
        f"- **mu (mean q4)**: {doc['mu']:.4f}",
        f"- **R (p99.9 rank q4)**: {doc['R']:.4f}",
        "",
        "| alpha | b (tiles px) | eps (q4) | candidate_threshold (q4) |",
        "|---|---|---|---|",
    ]
    for row in doc["bounds"]:
        lines.append(
            f"| {row['alpha']} | {row['b']} | {row['eps']:.4f} | {row['candidate_threshold_q4']:.4f} |"
        )
    return "\n".join(lines) + "\n"


def run_still_noise_bound(run_dir, out_dir, seeds_spec=None):
    run_path = Path(run_dir).resolve()
    out_path = Path(out_dir).resolve()

    if out_path == run_path or run_path in out_path.parents or out_path in run_path.parents:
        raise VqaError("E_SCHEMA", {"reason": "--out must be outside --run dir"})

    out_path.mkdir(parents=True, exist_ok=True)

    samples_q4, rows, fit_rows, seeds = load_run(run_dir, seeds_spec)
    doc = compute_bounds(samples_q4, fit_rows, rows)

    json_path = out_path / "still_noise_bound.json"
    md_path = out_path / "still_noise_bound.md"

    json_bytes = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    json_path.write_bytes(json_bytes)
    md_path.write_text(render_markdown(doc), encoding="utf-8")

    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="still_noise_bound")
    ap.add_argument("--run", required=True, help="input run directory (read only)")
    ap.add_argument("--out", required=True, help="new output directory")
    ap.add_argument("--seeds", default=None, help="fit:<a-b>,gate:<c-d>")
    a = ap.parse_args(argv)

    try:
        doc = run_still_noise_bound(a.run, a.out, a.seeds)
        return 0
    except (VqaError, KeyError, ValueError) as e:
        if not isinstance(e, VqaError):
            e = VqaError("E_SCHEMA", {"reason": repr(e)})
        sys.stdout.write(json.dumps(e.to_obj(), sort_keys=True) + "\n")
        return e.rc


if __name__ == "__main__":
    sys.exit(main())
