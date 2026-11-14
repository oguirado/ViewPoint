"""Combined post-pass evaluation harness for GM and FL chain variants."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from eval.gui_synth import prereg
from eval.gui_synth.gen import Src
from eval.gui_synth.labels import derive
from eval.gui_synth.score import aggregate
from visioncore.desc import luma_of_padded
from visioncore.grid import Grid
from visioncore.pipeline import analyze

from .exploration import quick_selection
from .plugins import Variant, parse_variant
from .postpass import BaselineFlickerVeto, apply_claims
from .run import add_honesty, metrics_from_predictions, omit_scorer_default_timings, write_json

HARNESS_FILES = ("run.py", "plugins.py", "exploration.py", "postpass.py", "chain.py")


class _ClaimVariant(Variant):
    """Retain the worker's explicit positive claim before baseline overlay.

    Variant's public output is intentionally an overlaid class/vector mapping.
    Comparing its classes with baseline loses a genuine FL claim on a tile that
    was already class 4, precisely the claim a separate baseline veto must keep.
    The existing worker protocol already sends the complete claim; capture it
    while keeping the inherited validation, deadlines and accounting.
    """

    def _receive(self):
        result = super()._receive()
        if isinstance(result, tuple) and len(result) == 3:
            self.last_claim = result[0]
        return result

    def update(self, prev, cur, tiles):
        self.last_claim = None
        return super().update(prev, cur, tiles)


def compose_chain(baseline, gm_claim, fl_claim, order="gm_then_fl", *,
                  flicker_veto=None):
    """Apply baseline-only negative evidence, then the named first-wins order."""
    if order == "gm_then_fl":
        first, second = gm_claim, fl_claim
    elif order == "fl_then_gm":
        first, second = fl_claim, gm_claim
    else:
        raise ValueError("order must be gm_then_fl or fl_then_gm")
    effective = dict(second, mask=second["mask"] & ~first["mask"])
    return apply_claims(baseline, [first, effective], flicker_veto=flicker_veto)


def compute_harness_sha256():
    parent = Path(__file__).parent
    return hashlib.sha256(
        b"".join((parent / name).read_bytes() for name in HARNESS_FILES)
    ).hexdigest()


def annotate_flicker_fp(metrics):
    if metrics is None:
        return None
    c = np.asarray(metrics["confusion"])
    pred_flicker = int(c[:, 4].sum())
    tp_flicker = int(c[4, 4])
    fp_flicker = pred_flicker - tp_flicker
    metrics["flicker_false_positives"] = fp_flicker
    metrics["flicker_fp"] = fp_flicker

    if "per_kind" in metrics:
        for kind, k_dict in metrics["per_kind"].items():
            kc = np.asarray(k_dict["confusion"])
            k_pred = int(kc[:, 4].sum())
            k_tp = int(kc[4, 4])
            k_fp = k_pred - k_tp
            k_dict["flicker_false_positives"] = k_fp
            k_dict["flicker_fp"] = k_fp
    return metrics


def normalize_spec(spec, expected_kind):
    if not spec.startswith(expected_kind + "="):
        spec = f"{expected_kind}={spec}"
    kind, module = parse_variant(spec)
    if kind != expected_kind:
        raise ValueError(f"expected {expected_kind} variant, got {kind}")
    return spec, module


def run_chain(scenes, gm_spec, fl_spec, order="gm_then_fl", budget_ms=1000, *,
              baseline_flicker_veto=None):
    if order not in ("gm_then_fl", "fl_then_gm"):
        raise ValueError("order must be gm_then_fl or fl_then_gm")

    gm_spec, _ = normalize_spec(gm_spec, "gm")
    fl_spec, fl_module = normalize_spec(fl_spec, "fl")
    if baseline_flicker_veto is None:
        baseline_flicker_veto = fl_module == "eval.v2x.fl_v3"
    elif not isinstance(baseline_flicker_veto, bool):
        raise ValueError("baseline_flicker_veto must be bool or None")

    scenes = quick_selection(scenes, None)

    records = {
        "baseline": {"status": "OK", "rows": [], "error": None, "module_sha256": None, "python_opcodes": None, "calls": None},
        gm_spec: {"status": "OK", "rows": [], "error": None, "module_sha256": None, "python_opcodes": 0, "calls": 0},
        fl_spec: {"status": "OK", "rows": [], "error": None, "module_sha256": None, "python_opcodes": 0, "calls": 0},
        order: {"status": "OK", "rows": [], "error": None, "module_sha256": None, "python_opcodes": 0, "calls": 0},
    }

    for scene in scenes:
        grid = Grid(scene["w"], scene["h"])
        engine = analyze(Src(scene), workers=1, cpus=(2, 3, 4))
        tiles = np.stack(engine.tiles)
        events = derive(scene, grid)
        base = {k: tiles[k].astype(np.int64) for k in ("cls", "mv_dx", "mv_dy")}
        records["baseline"]["rows"].append(metrics_from_predictions(base, events, scene))

        adapter_gm = None
        adapter_fl = None
        veto = BaselineFlickerVeto(grid) if baseline_flicker_veto else None
        try:
            adapter_gm = _ClaimVariant(gm_spec, grid, budget_ms)
            adapter_fl = _ClaimVariant(fl_spec, grid, budget_ms)

            records[gm_spec]["module_sha256"] = adapter_gm.module_sha256
            records[fl_spec]["module_sha256"] = adapter_fl.module_sha256

            records[gm_spec]["status"], records[gm_spec]["error"] = adapter_gm.status, adapter_gm.error
            records[fl_spec]["status"], records[fl_spec]["error"] = adapter_fl.status, adapter_fl.error

            if adapter_gm.status != "OK" or adapter_fl.status != "OK":
                records[order]["status"] = "FAILED"
                errs = [e for e in (adapter_gm.error, adapter_fl.error) if e]
                records[order]["error"] = "; ".join(errs) or "adapter initialization failed"
                continue

            predictions = {
                gm_spec: {k: np.empty_like(v) for k, v in base.items()},
                fl_spec: {k: np.empty_like(v) for k, v in base.items()},
                order: {k: np.empty_like(v) for k, v in base.items()},
            }

            prev = None
            failed = False
            for frame in Src(scene):
                cur = np.ascontiguousarray(luma_of_padded(frame.rgb), dtype=np.uint8)
                b_frame = {k: base[k][frame.i] for k in ("cls", "mv_dx", "mv_dy")}
                veto_mask = None
                fl_tiles = tiles[frame.i]
                if veto is not None:
                    decision = veto.update(fl_tiles, frame.rgb, frame_id=frame.i)
                    veto_mask = decision.mask
                    fl_tiles = veto.current_tiles

                out_gm = adapter_gm.update(prev, cur, tiles[frame.i])
                out_fl = adapter_fl.update(prev, cur, fl_tiles)

                records[gm_spec]["status"], records[gm_spec]["error"] = adapter_gm.status, adapter_gm.error
                records[fl_spec]["status"], records[fl_spec]["error"] = adapter_fl.status, adapter_fl.error

                if out_gm is None or adapter_gm.status != "OK" or out_fl is None or adapter_fl.status != "OK":
                    failed = True
                    records[order]["status"] = "FAILED"
                    errs = [e for e in (records[gm_spec]["error"], records[fl_spec]["error"]) if e]
                    records[order]["error"] = "; ".join(errs) or "adapter frame update failed"
                    break

                gm_claim, fl_claim = adapter_gm.last_claim, adapter_fl.last_claim
                if veto_mask is not None:
                    out_fl = apply_claims(b_frame, [fl_claim], flicker_veto=veto_mask)
                for k in ("cls", "mv_dx", "mv_dy"):
                    predictions[gm_spec][k][frame.i] = out_gm[k]
                    predictions[fl_spec][k][frame.i] = out_fl[k]

                out_chain = compose_chain(b_frame, gm_claim, fl_claim, order,
                                          flicker_veto=veto_mask)

                for k in ("cls", "mv_dx", "mv_dy"):
                    predictions[order][k][frame.i] = out_chain[k]

                prev = cur

            if not failed:
                records[gm_spec]["rows"].append(metrics_from_predictions(predictions[gm_spec], events, scene))
                records[fl_spec]["rows"].append(metrics_from_predictions(predictions[fl_spec], events, scene))
                records[order]["rows"].append(metrics_from_predictions(predictions[order], events, scene))

            records[gm_spec]["python_opcodes"] += adapter_gm.ops
            records[gm_spec]["calls"] += adapter_gm.frames
            records[fl_spec]["python_opcodes"] += adapter_fl.ops
            records[fl_spec]["calls"] += adapter_fl.frames
            records[order]["python_opcodes"] += adapter_gm.ops + adapter_fl.ops
            records[order]["calls"] += adapter_gm.frames

        finally:
            if adapter_gm:
                adapter_gm.close()
            if adapter_fl:
                adapter_fl.close()

    result = {
        "schema": "vqa.v2eval-chain/v1",
        "split": "DEV",
        "order": order,
        "gm_spec": gm_spec,
        "fl_spec": fl_spec,
        "scene_ids": [s["scene_id"] for s in scenes],
        "frame_budget_ms": budget_ms,
        "prereg_sha256": prereg.sha256(),
        "harness_sha256": compute_harness_sha256(),
        "measurement_tag": "VM",
        "variants": {},
    }
    if baseline_flicker_veto:
        result["baseline_flicker_veto"] = {
            "enabled": True,
            "hash_source": "owned eager RGB snapshots or proven exact byte reuse",
            "cost_scope": "VM evidence adapter is not included in worker opcode counts or deadline",
        }

    for name, rec in records.items():
        rows = rec.pop("rows")
        if rec["status"] == "OK" and rows:
            metrics = add_honesty(aggregate(rows))
            annotate_flicker_fp(metrics)
            rec["metrics"] = metrics
        else:
            rec["metrics"] = None

        if rec["status"] == "FAILED" or (baseline_flicker_veto and name in (fl_spec, order)):
            rec["python_opcodes"] = None

        result["variants"][name] = rec

    omit_scorer_default_timings(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="V2EVAL2 chain evaluation harness (GM + FL)")
    parser.add_argument("--gm", required=True, help="GM variant module spec (e.g. eval.v2x.gm_bands_hv)")
    parser.add_argument("--fl", required=True, help="FL variant module spec (e.g. eval.v2x.fl_blinkprior)")
    parser.add_argument("--quick", type=int, metavar="N", help="Select N quick DEV scenes")
    parser.add_argument("--order", choices=("gm_then_fl", "fl_then_gm"), default="gm_then_fl", help="Chain precedence order")
    parser.add_argument("--frame-budget-ms", type=float, default=1000, help="Per-frame budget in ms")
    parser.add_argument("--out", type=Path, help="Output JSON path (prints to stdout if omitted)")

    args = parser.parse_args()

    from .baseline import load_dev, selection

    dev = load_dev()
    scenes = selection(dev)
    if args.quick is not None:
        scenes = quick_selection(scenes, args.quick)

    result = run_chain(scenes, args.gm, args.fl, order=args.order, budget_ms=args.frame_budget_ms)

    if args.out:
        write_json(args.out, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
