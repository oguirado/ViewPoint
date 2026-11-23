"""Integer sculpt regression: real mesh API + CPU view ring + pinned S0-S4.

evaluate() owns scratch and children; callers supply immutable inputs, mesh
policy, explicit pixel budgets and a bounded deadline. A successful score is
geometry regression only, never appearance approval or an exact distance proof.
No timing or pathname enters the deterministic receipt. rank() orders measured
scores only; it is not an admission decision.
"""
from asset_factory import proc as af_proc
import argparse
import importlib.util
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from asset_factory import core
from tools.mesh import api as mesh
from tools.mesh import contracts as mc

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
ENGINE = ROOT.parent / "vision-engine"
BVQA = ROOT.parent / "blender-visual-qa"
VIEWS = ("front", "back", "right", "left", "top", "bottom")
SCHEMA = "af.sculpt-metric/v1"


class MetricError(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason


def _check(ok, rc, reason):
    if not ok:
        raise MetricError(rc, reason)


def validate_region_policy(policy):
    _check(isinstance(policy, dict) and set(policy) ==
           {"resolution", "max_changed_pixels_ppm", "max_mean_abs_delta_ppm", "timeout_s"},
           2, "region_policy_invalid")
    for key, lo, hi in (("resolution", 32, 256), ("max_changed_pixels_ppm", 0, 1000000),
                        ("max_mean_abs_delta_ppm", 0, 1000000), ("timeout_s", 1, 300)):
        _check(type(policy[key]) is int and lo <= policy[key] <= hi, 2, "region_policy_" + key)


def _receipt():
    return {"schema": SCHEMA, "rc": 3, "status": "INCOMPLETE", "verdict": "CANNOT_SEE",
            "reason_codes": [], "reference_sha256": None, "candidate_sha256": None,
            "mesh_policy_sha256": None, "region_policy_sha256": None,
            "toolchain_sha256": None, "engine_sha256": None,
            "mesh_receipt": None, "render": None, "views": [], "score_ppm": None,
            "absence_reasons": {"score_ppm": "not_evaluated"},
            "coverage_limits": ["sampled_distance_not_exact", "six_exterior_views_not_interior",
                                "normal_witness_not_appearance", "DESIGN_CHOICE_uncalibrated"]}


def _finish(rec, rc, reasons):
    measured = rec.get("mesh_receipt")
    if isinstance(measured, dict) and type(measured.get("rc")) is int and measured["rc"] in core.RC:
        # Preserve the specific stage STALE/BUDGET code. worst_rc normalizes
        # those to INCOMPLETE for whole-gate aggregation, which is not this
        # boundary. Proven ERROR/FAIL still wins over incomplete evidence.
        if measured["rc"] == 2:
            rc = 2
        elif measured["rc"] == 5 and rc != 2:
            rc = 5
        elif rc == 0:
            rc = measured["rc"]
    rec.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
               reason_codes=sorted(set(reasons)))
    return rec


def _remaining(deadline):
    value = deadline - time.monotonic()
    _check(value > 0, 6, "deadline_exceeded")
    return value


def _child(argv, *, deadline, payload=None):
    """File streams and one service, with bounded output and complete disposal."""
    _remaining(deadline)
    job = af_proc.popen(argv, input=payload or b"", start_new_session=True, cwd=ROOT,
        env={"PATH": os.defpath, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
             "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""})
    try:
        while af_proc.poll_exit(job) is None:
            _check(sum(p.stat().st_size for p in (job.stdout_path, job.stderr_path)) <= 4*2**20,
                   6, "child_output_budget")
            time.sleep(min(.01, _remaining(deadline)))
        _check(sum(p.stat().st_size for p in (job.stdout_path, job.stderr_path)) <= 4*2**20,
               6, "child_output_budget")
        return job.returncode, job.stdout_path.read_bytes()
    finally:
        job.stop()

def _engine_pin():
    config = core.load_json(BVQA / "config/vision.json", max_bytes=65536)
    wheel = (BVQA / config["engine_path"]).resolve()
    _check(wheel.is_relative_to(BVQA.resolve()), 2, "engine_path_escape")
    expected = config["pin"]["wheel_sha256"]
    _check(mc.valid_hash(expected) and core.sha256_file(wheel) == expected, 4, "engine_pin_stale")
    return wheel, expected


def _toolchain(wheel_hash, adapter, blender):
    sources = {p.name: core.sha256_file(p) for p in sorted(HERE.glob("*.py"))}
    sources.update(engine=wheel_hash, adapter=core.sha256_file(adapter),
                   blender=core.sha256_file(blender), mesh=mesh.toolchain_identity())
    return core.sha256_json(sources)


def _residual_metrics(pixels, changed, change_ppm, delta_ppm):
    """Necessary integer consistency of exact RGB residual counts and ratios."""
    _check(change_ppm == (changed * 1000000 + pixels - 1) // pixels,
           2, "pixel_ratio_invalid")
    # Every unequal RGB pixel contributes between one and 765 absolute units.
    minimum = (changed * 1000000 + pixels * 765 - 1) // (pixels * 765)
    maximum = (changed * 1000000 + pixels - 1) // pixels
    _check(minimum <= delta_ppm <= maximum, 2, "residual_count_invalid")


def combine(mesh_receipt, vision, *, region_policy):
    """Internal aggregation boundary; no cached/model verdict is authoritative.

    Recompute both distance directions and each view guard; empty/missing
    measurements never pass. Returns (rc, reasons, score_ppm).
    """
    validate_region_policy(region_policy)
    mc.validate_receipt(mesh_receipt)
    reasons, codes = [], [mesh_receipt["rc"]]
    m = mesh_receipt["metrics"]
    required = ("hausdorff_forward_um", "hausdorff_backward_um", "raw_diag_um", "silhouette_iou_ppm")
    if any(m.get(k) is None for k in required) or not m.get("raw_diag_um"):
        return core.worst_rc(codes + [3]), ["mesh_evidence_missing"], None
    _check(m.get("distance_is_exact") is False, 2, "distance_estimator_invalid")
    per_view = m.get("silhouette_per_view_ppm")
    _check(isinstance(per_view, dict) and set(per_view) == set(VIEWS[:4]), 3, "mesh_view_missing")
    _check(all(type(v) is int and 0 <= v <= 1000000 for v in per_view.values()) and
           0 <= m["silhouette_iou_ppm"] <= 1000000, 2, "mesh_iou_invalid")
    distance = max(m["hausdorff_forward_um"], m["hausdorff_backward_um"])
    relative = (distance * 1000000 + m["raw_diag_um"] - 1) // m["raw_diag_um"]
    if distance * 1000000 > m["raw_diag_um"] * mc.DISTANCE_MAX_PPM:
        codes.append(5)
        reasons.append("distance_regression")
    iou = min(m["silhouette_iou_ppm"], *per_view.values())
    if iou < mc.IOU_MIN_PPM:
        codes.append(5)
        reasons.append("silhouette_regression")
    if not any(codes):
        # Recompute regression guards first: a false cached success remains FAIL.
        # A genuine success also needs the complete, passing topology inventory.
        mc.validate_success(mesh_receipt, "compare")
    _check(isinstance(vision, dict) and type(vision.get("rc")) is int and vision["rc"] in core.RC,
           2, "vision_contract")
    codes.append(vision["rc"])
    if vision["rc"]:
        return core.worst_rc(codes), vision.get("reason_codes", []) + reasons, None
    rows = vision.get("views")
    _check(isinstance(rows, list) and len(rows) == len(VIEWS), 3, "view_missing")
    _check(all(isinstance(row, dict) for row in rows) and
           sorted(row.get("view", "") for row in rows) == sorted(VIEWS), 2, "view_duplicate_or_unknown")
    max_changed = max_delta = 0
    for row in rows:
        for key in ("reference_frame_sha256", "candidate_frame_sha256", "frame_manifest_sha256",
                    "residual_manifest_sha256", "params_sha256", "tiles_sha256"):
            _check(mc.valid_hash(row.get(key)), 2, "frame_identity_invalid")
        _check(isinstance(row.get("regions"), list), 3, "regions_missing")
        _check(row.get("comparison_encoding") == "exact_changed_pixels_binary_rgb24/v1",
               2, "comparison_encoding_invalid")
        pixels, changed = row.get("pixel_count"), row.get("changed_pixels")
        _check(type(pixels) is int and pixels == region_policy["resolution"] ** 2 and
               type(changed) is int and 0 <= changed <= pixels, 2, "pixel_count_invalid")
        for key in ("reference_foreground_pixels", "candidate_foreground_pixels"):
            _check(type(row.get(key)) is int and 0 < row[key] <= pixels, 3, "empty_view")
        for key in ("changed_pixels_ppm", "mean_abs_delta_ppm"):
            _check(type(row.get(key)) is int and 0 <= row[key] <= 1000000, 2, "region_metric_invalid")
        _residual_metrics(pixels, changed, row["changed_pixels_ppm"], row["mean_abs_delta_ppm"])
        _check((changed == 0) == (row["reference_frame_sha256"] == row["candidate_frame_sha256"]),
               4, "frame_evidence_stale")
        uncovered = row.get("uncovered_changed_pixels")
        _check(type(uncovered) is int and 0 <= uncovered <= changed, 2, "region_coverage_invalid")
        if uncovered or (changed and not row["regions"]):
            codes.append(3)
            reasons.append(row["view"] + ":region_coverage_incomplete")
        scores = row.get("region_scores")
        _check(isinstance(scores, list) and len(scores) == len(row["regions"]), 3, "region_scores_missing")
        region_ids, intervals, covered_changed, covered_pixels = set(), [], 0, 0
        for original, scored in zip(row["regions"], scores):
            _check(isinstance(original, dict) and isinstance(scored, dict) and
                   scored.get("id") == original.get("id") and
                   scored.get("spans") == original.get("spans"), 2, "region_membership_invalid")
            ident, spans = scored.get("id"), scored.get("spans")
            _check(type(ident) is str and ident and ident not in region_ids,
                   2, "region_membership_invalid")
            region_ids.add(ident)
            _check(isinstance(spans, list) and spans, 2, "region_membership_invalid")
            for span in spans:
                _check(isinstance(span, list) and len(span) == 2 and
                       all(type(value) is str and len(value) == 4 and
                           all(c in "0123456789abcdef" for c in value) for value in span) and
                       span[0] <= span[1], 2, "region_membership_invalid")
                intervals.append(tuple(span))
            count, regional_changed = scored.get("pixel_count"), scored.get("changed_pixels")
            _check(type(count) is int and 0 < count <= pixels and
                   type(regional_changed) is int and 0 <= regional_changed <= count,
                   2, "region_coverage_invalid")
            covered_pixels += count
            covered_changed += regional_changed
            _check(type(scored.get("score_ppm")) is int and 0 <= scored["score_ppm"] <= 1000000,
                   2, "region_score_invalid")
            for key, limit in (("changed_pixels_ppm", "max_changed_pixels_ppm"),
                               ("mean_abs_delta_ppm", "max_mean_abs_delta_ppm")):
                _check(type(scored.get(key)) is int and 0 <= scored[key] <= 1000000,
                       2, "region_metric_invalid")
                if scored[key] > region_policy[limit]:
                    codes.append(5)
                    reasons.append(row["view"] + ":region:" + key)
            _residual_metrics(count, regional_changed, scored["changed_pixels_ppm"],
                              scored["mean_abs_delta_ppm"])
            _check(scored["score_ppm"] == min(1000000 - scored["changed_pixels_ppm"],
                                            1000000 - scored["mean_abs_delta_ppm"]),
                   2, "region_score_invalid")
        intervals.sort()
        _check(all(left[1] < right[0] for left, right in zip(intervals, intervals[1:])),
               2, "region_membership_invalid")
        if scores:
            _check(covered_pixels <= pixels and covered_changed + uncovered == changed,
                   2, "region_coverage_invalid")
        for key, limit in (("changed_pixels_ppm", "max_changed_pixels_ppm"),
                           ("mean_abs_delta_ppm", "max_mean_abs_delta_ppm")):
            if row[key] > region_policy[limit]:
                codes.append(5)
                reasons.append(row["view"] + ":" + key)
        max_changed = max(max_changed, row["changed_pixels_ppm"])
        max_delta = max(max_delta, row["mean_abs_delta_ppm"])
    reasons.extend(mesh_receipt["failed"])
    # Conservative bottleneck, not a fitted perceptual score or weighted average.
    score = min(iou, max(0, 1000000 - relative), 1000000 - max_changed, 1000000 - max_delta)
    return core.worst_rc(codes), sorted(set(reasons)), score


def evaluate(reference_glb, candidate_glb, *, mesh_policy, region_policy, blender=Path("/usr/bin/blender")):
    """Return af.sculpt-metric/v1; never writes caller paths, including refusals.

    Pixel budgets have no defaults. The deadline covers input acquisition, mesh
    measurement, CPU rendering, engine analysis, and final source revalidation.
    """
    rec = _receipt()
    try:
        validate_region_policy(region_policy)
        mc.validate_policy(mesh_policy)
        deadline = time.monotonic() + region_policy["timeout_s"]
        rec["mesh_policy_sha256"] = core.sha256_json(mesh_policy)
        rec["region_policy_sha256"] = core.sha256_json(region_policy)
        wheel, wheel_hash = _engine_pin()
        rec["engine_sha256"] = wheel_hash
        adapter = ENGINE / "vadapt/sculpt_frames.py"
        _check(Path(blender).is_file(), 3, "blender_unavailable")
        pin = _toolchain(wheel_hash, adapter, blender)
        rec["toolchain_sha256"] = pin
        spec = importlib.util.spec_from_file_location("sculpt_ramws", ENGINE / "vadapt/ramws.py")
        ramws = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ramws)
        _check(ramws.is_ram_fs("/dev/shm") is not None, 3, "ram_workspace_unavailable")
        raw, candidate = (mesh._acquire(Path(path), deadline) for path in (reference_glb, candidate_glb))
        rec["reference_sha256"], rec["candidate_sha256"] = mc.digest(raw), mc.digest(candidate)
        with tempfile.TemporaryDirectory(prefix="af-sculpt-metric-", dir="/dev/shm") as scratch:
            root = Path(scratch)
            a, b = root / "reference.glb", root / "candidate.glb"
            a.write_bytes(raw)
            b.write_bytes(candidate)
            # Do not change a hashed policy according to wall-clock progress:
            # identical inputs must produce identical receipts. Reserve its
            # entire child budget before starting the existing bounded API.
            _check(_remaining(deadline) >= mesh_policy["timeout_s"], 6, "insufficient_mesh_deadline")
            _, measured = mesh.compare_meshes(a, b, policy=mesh_policy)
            rec["mesh_receipt"] = measured
            if measured["rc"] not in (0, 5):
                return _finish(rec, measured["rc"], ["mesh_measurement_unavailable"])
            _remaining(deadline)
            request = root / "request.json"
            core.atomic_write_json(request, {"resolution": region_policy["resolution"]})
            rc, _ = _child([str(blender), "-b", "--factory-startup", "-t", "2", "--python-exit-code", "2",
                            "--python", str(HERE / "render.py"), "--", str(request)], deadline=deadline)
            _check(rc == 0, 2, "blender_render_failed")
            render = core.load_json(root / "render.json", max_bytes=65536)
            _check(render.get("reference_sha256") == rec["reference_sha256"] and
                   render.get("candidate_sha256") == rec["candidate_sha256"], 4, "render_source_stale")
            _check(render.get("views") == list(VIEWS) and render.get("device") == "CPU" and
                   render.get("engine") == "CYCLES" and render.get("resolution") == region_policy["resolution"],
                   3, "render_coverage_missing")
            rec["render"] = render
            payload = {"root": str(root), "wheel": str(wheel), "engine_sha256": wheel_hash,
                       "adapter": str(adapter), "adapter_sha256": core.sha256_file(adapter),
                       "views": list(VIEWS), "resolution": region_policy["resolution"]}
            rc, output = _child([sys.executable, "-I", "-B", str(HERE / "vision_worker.py")],
                                deadline=deadline, payload=core.canonical_json(payload).encode())
            vision = mc.loads(output)
            _check(type(rc) is int and rc in core.RC and rc == vision.get("rc"), 2, "vision_exit_invalid")
            if rc == 0:
                _check(vision.get("engine_sha256") == wheel_hash and
                       vision.get("adapter_sha256") == payload["adapter_sha256"], 4, "vision_pin_stale")
            rc, reasons, score = combine(measured, vision, region_policy=region_policy)
            rec["views"] = vision.get("views", [])
            rec["score_ppm"] = score
            if score is not None:
                rec["absence_reasons"] = {}
        for path, key in ((reference_glb, "reference_sha256"), (candidate_glb, "candidate_sha256")):
            _check(mc.digest(mesh._acquire(Path(path), deadline)) == rec[key], 4, "source_stale")
        _, final_wheel_hash = _engine_pin()
        _check(_toolchain(final_wheel_hash, adapter, blender) == pin, 4, "toolchain_stale")
        _remaining(deadline)
        return _finish(rec, rc, reasons)
    except af_proc.SignalRefusal as exc:
        return _finish(rec, exc.rc, [exc.code])
    except MetricError as exc:
        return _finish(rec, exc.rc, [exc.reason])
    except mc.MeshError as exc:
        return _finish(rec, exc.rc, [exc.code])
    except core.ContractError as exc:
        return _finish(rec, 2, [exc.code])
    except subprocess.TimeoutExpired:
        return _finish(rec, 6, ["deadline_exceeded"])
    except FileNotFoundError:
        return _finish(rec, 3, ["input_or_evaluator_missing"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _finish(rec, 2, ["input_or_evaluator_invalid"])


def rank(receipts):
    """Descending integer score; ties use smallest candidate digest, never input order.

    Includes measured FAILs for comparison; incomplete, stale and error receipts
    cannot participate. Ranking never upgrades a gate verdict.
    """
    bindings = ("reference_sha256", "mesh_policy_sha256", "region_policy_sha256", "toolchain_sha256")
    _check(isinstance(receipts, list) and bool(receipts), 3, "ranking_empty")
    seen = set()
    for rec in receipts:
        _check(isinstance(rec, dict) and rec.get("schema") == SCHEMA and
               type(rec.get("rc")) is int and rec["rc"] in (0, 5)
               and type(rec.get("score_ppm")) is int and 0 <= rec["score_ppm"] <= 1000000
               and mc.valid_hash(rec.get("candidate_sha256")), 3, "ranking_unmeasured")
        _check(all(mc.valid_hash(rec.get(key)) for key in bindings), 3, "ranking_bindings_missing")
        _check(all(rec[key] == receipts[0][key] for key in bindings), 4, "ranking_context_stale")
        _check(rec["candidate_sha256"] not in seen, 2, "ranking_duplicate")
        seen.add(rec["candidate_sha256"])
    return sorted(receipts, key=lambda rec: (-rec["score_ppm"], rec["candidate_sha256"]))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise MetricError(2, "arguments_invalid")


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        for flag in ("reference", "candidate", "mesh-policy", "region-policy"):
            parser.add_argument("--" + flag, type=Path, required=True)
        args = parser.parse_args(argv)
        result = evaluate(args.reference, args.candidate,
                          mesh_policy=core.load_json(args.mesh_policy, max_bytes=65536),
                          region_policy=core.load_json(args.region_policy, max_bytes=65536))
    except MetricError as exc:
        result = _finish(_receipt(), exc.rc, [exc.reason])
    except (OSError, core.ContractError):
        result = _finish(_receipt(), 2, ["policy_input_invalid"])
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
