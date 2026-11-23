"""Rehash a render run, call the sibling vqa3d noise metric, judge flicker/detail.

python -m tools.render.evaluate --run DIR --vqa-root DIR [--policy JSON]
The quality record goes to stdout, including FAIL/CANNOT_SEE; no caller path is
replaced. Sparse references remain sparse and never become inferred truth.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from asset_factory import core
from tools.render import metrics, identity
from tools.render.pipeline import HERE, Refusal


def local(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise Refusal(2, "path_escape")
    return path


def read_run(root, *, expected_request_sha256=None):
    root = Path(root).resolve()
    if not (root / "receipt.json").is_file():
        raise Refusal(3, "render_manifest_unavailable")
    run = core.load_json(root / "receipt.json", max_bytes=1048576)
    core.validate(run, core.load_json(HERE / "run.schema.json", max_bytes=32768))
    if run.get("schema") != "af.render-run/v1" or type(run.get("rc")) is not int or run["rc"] != 0:
        raise Refusal(2, "run_invalid")
    schema = core.load_json(HERE / "frame.schema.json", max_bytes=32768)
    checked = {}
    def rehash(item, base=None):
        path = local(base, item["path"]) if base is not None else Path(item["path"]).resolve()
        if path not in checked:
            checked[path] = core.sha256_file(path)
        if item["sha256"] != checked[path]:
            raise Refusal(4, "bytes_changed:" + str(path))
        return path
    params = run["parameters"]
    fields = ("start", "end", "samples", "reference_samples")
    if (any(type(params.get(k)) is not int for k in fields)
            or not 0 <= params["start"] <= params["end"]
            or params["end"] - params["start"] >= 1000
            or not 0 < params["samples"] < params["reference_samples"]
            or type(params.get("references")) is not list
            or len(params["references"]) > 1000
            or any(type(f) is not int or not params["start"] <= f <= params["end"] for f in params["references"])
            or len(set(params["references"])) != len(params["references"])):
        raise Refusal(2, "run_parameters_invalid")
    expected = [(f, params["samples"]) for f in range(params["start"], params["end"] + 1)]
    expected += [(f, params["reference_samples"]) for f in params["references"]]
    if len(expected) != len(run["frames"]) or len(expected) > 2000:
        raise Refusal(3, "frame_coverage_incomplete")
    identity.parameters(params)
    toolchain = identity.sources(run["toolchain"])
    if "request_sha256" not in run or "job" not in run:
        raise Refusal(3, "run_provenance_unavailable")
    identity.digest(run["request_sha256"])
    request_hash = identity.request_sha256(params, run["toolchain"])
    if run["request_sha256"] != request_hash:
        raise Refusal(4, "request_identity_drift")
    if expected_request_sha256 is not None:
        identity.digest(expected_request_sha256)
        if expected_request_sha256 != request_hash:
            raise Refusal(4, "unexpected_render_request")
    if run["job"]["role"] != "render_job":
        raise Refusal(2, "render_job_role_invalid")
    job_path = rehash(run["job"], root)
    job = core.load_json(job_path, max_bytes=1048576)
    identity.job(job, params, request_hash, expected)
    found, rows = [], []
    shared = None
    for item in run["frames"]:
        path = rehash(item, root)
        receipt = core.load_json(path, max_bytes=1048576)
        core.validate(receipt, schema)
        identity.frame(receipt, params, toolchain, request_hash, run["job"]["sha256"])
        key = receipt["frame"], receipt["samples"]
        if key in found:
            raise Refusal(2, "duplicate_frame")
        found.append(key)
        task = job["tasks"][len(found) - 1]
        if item["role"] != "render_frame" or path.parent != local(root, task["dir"]):
            raise Refusal(4, "frame_job_path_drift")
        frame_identity = (receipt["width"], receipt["height"], receipt["resolution_percentage"], receipt["view"],
                    receipt["device"], receipt["denoiser"], receipt["inputs"])
        if shared is not None and frame_identity != shared:
            raise Refusal(4, "cross_run_identity")
        shared = frame_identity
        for source in receipt["inputs"]:
            rehash(source)
        output_names = set()
        for output in receipt["outputs"]:
            output_path = rehash(output, root)
            if output_path.parent != path.parent or output_path.name in output_names:
                raise Refusal(2, "output_identity_invalid")
            output_names.add(output_path.name)
        if not {"raw.png", "denoised.png", "colour.pfm", "albedo.pfm", "normal.pfm", "denoised.pfm"} <= output_names:
            raise Refusal(3, "passes_incomplete")
        rows.append((path.parent, receipt))
    if found != expected:
        raise Refusal(4, "frame_order_or_identity_changed")
    for item in run["toolchain"]:
        rehash(item)
    return run, rows


def load_policy(path):
    policy = core.load_json(path, max_bytes=16384)
    core.validate(policy, core.load_json(HERE / "policy.schema.json", max_bytes=32768))
    fields = {"noise_max_ppm", "flicker_max_ppm", "detail_min_ppm", "detail_tile_min_ppm", "motion_min_coverage_ppm",
              "motion_guide_max_error", "motion_downsample", "motion_tile", "motion_radius", "rgb_mse_max_ppm"}
    if set(policy) != fields | {"schema", "provenance"} or policy["schema"] != "af.render-policy/v1":
        raise Refusal(2, "policy_invalid")
    if any(type(policy[k]) is not int or not 0 <= policy[k] <= 2000000 for k in fields):
        raise Refusal(2, "policy_invalid")
    if not (0 <= policy["rgb_mse_max_ppm"] <= 1000 and 950000 <= policy["detail_min_ppm"] <= 1000000):
        raise Refusal(2, "rgb_or_detail_policy_weakened")
    return policy


class NoiseMetric:
    def __init__(self, root):
        root = Path(root).resolve()
        path = root / "vqa3d" / "metrics.py"
        if not path.is_file():
            raise Refusal(3, "vqa3d_noise_unavailable")
        try:
            spec = importlib.util.spec_from_file_location("af_render_vqa_noise", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.compute = getattr(module, "noise_ppm", None)
            if not callable(self.compute):
                raise Refusal(2, "vqa3d_noise_interface_invalid")
        except Refusal:
            raise
        except Exception as exc:
            raise Refusal(2, "vqa3d_noise_load_failed:" + type(exc).__name__) from None
        policy_path = root / "vqa3d" / "profiles" / "film-animation.json"
        # The sibling profile is integer-only canonical JSON, just as our records are.
        profile = core.load_json(policy_path, max_bytes=1048576)
        if type(profile) is not dict or profile.get("schema") != "vqa3d.profile/v1" or profile.get("id") != "film-animation" or "extends" in profile:
            raise Refusal(2, "vqa3d_profile_invalid_or_unresolved")
        self.settings = profile["defaults"]["frame_noise_below"]
        self.identity = {"module_path": str(path), "module_sha256": core.sha256_file(path),
                         "profile_path": str(policy_path), "profile_file_sha256": core.sha256_file(policy_path),
                         "profile_sha256": hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":"),
                                                                     ensure_ascii=False).encode("utf-8")).hexdigest(),
                         "threshold": self.settings["threshold"],
                         "noise_limit_ppm": self.settings["production_max_outlier_ppm"],
                         "calibrated_px": self.settings["production_calibrated_px"]}

    def measure(self, array):
        return int(self.compute(array.shape[1], array.shape[0], array.astype("uint8").tobytes(),
                                threshold=self.settings["threshold"]))


def evaluate(root, *, vqa_root, policy_path=HERE / "policy.json", expected_request_sha256=None):
    root = Path(root).resolve()
    policy = load_policy(policy_path)
    run, receipts = read_run(root, expected_request_sha256=expected_request_sha256)
    noise_metric = NoiseMetric(vqa_root)
    if policy["noise_max_ppm"] != noise_metric.settings["production_max_outlier_ppm"]:
        raise Refusal(4, "noise_policy_drift")
    samples = run["parameters"]["samples"]
    candidates = [(directory, receipt) for directory, receipt in receipts if receipt["samples"] == samples]
    references = {receipt["frame"]: (directory, receipt) for directory, receipt in receipts if receipt["samples"] != samples}
    reference_pairs = []
    reference_residuals = {}
    reference_frames = []
    for frame, (directory, receipt) in sorted(references.items()):
        reference_frames.append({"frame": frame, "samples": receipt["samples"],
            "reference_kind": "OIDN-cleaned high-sample Cycles",
            "noise_ppm": noise_metric.measure(metrics.rgb(directory / "denoised.png")),
            "raw_noise_ppm": noise_metric.measure(metrics.rgb(directory / "raw.png")),
            "timings_ms": receipt["timings_ms"],
            "receipt_sha256": core.sha256_file(directory / "receipt.json")})
    by_frame = {receipt["frame"]: directory for directory, receipt in candidates}
    for frame in sorted(references):
        if frame - 1 in references:
            before, after = frame - 1, frame
            residual = metrics.reference_delta(
                metrics.luma(metrics.rgb(by_frame[before] / "denoised.png")),
                metrics.luma(metrics.rgb(by_frame[after] / "denoised.png")),
                metrics.luma(metrics.rgb(references[before][0] / "denoised.png")),
                metrics.luma(metrics.rgb(references[after][0] / "denoised.png")))
            raw_residual = metrics.reference_delta(
                metrics.luma(metrics.rgb(by_frame[before] / "denoised.png")),
                metrics.luma(metrics.rgb(by_frame[after] / "denoised.png")),
                metrics.luma(metrics.rgb(references[before][0] / "raw.png")),
                metrics.luma(metrics.rgb(references[after][0] / "raw.png")))
            reference_pairs.append({"before": before, "after": after, "flicker_ppm": residual,
                                    "raw_reference_flicker_ppm": raw_residual})
            reference_residuals[before] = reference_residuals[after] = residual
    frames, pairs, failures, unavailable = [], [], [], []
    previous = None
    for directory, receipt in candidates:
        frame = receipt["frame"]
        image = metrics.rgb(directory / "denoised.png")
        if image.shape[:2] != (receipt["height"], receipt["width"]):
            raise Refusal(4, "frame_dimension_drift")
        luma = metrics.luma(image)
        noise = noise_metric.measure(image)
        raw_noise = noise_metric.measure(metrics.rgb(directory / "raw.png"))
        guide = metrics.guide(directory)
        motion = None
        if previous is not None:
            motion = metrics.motion_residual(previous[1], luma, previous[2], guide,
                factor=policy["motion_downsample"], tile=policy["motion_tile"], radius=policy["motion_radius"],
                max_error=policy["motion_guide_max_error"])
            pairs.append({"before": previous[0], "after": frame, **motion})
        detail = None
        raw_detail = None
        if frame in references:
            reference_dir, ref_receipt = references[frame]
            reference = metrics.rgb(reference_dir / "denoised.png")
            raw_reference = metrics.rgb(reference_dir / "raw.png")
            detail = metrics.detail(luma, metrics.luma(reference))
            detail["rgb_mse_ppm"] = metrics.rgb_mse_ppm(image, reference)
            raw_detail = metrics.detail(luma, metrics.luma(raw_reference))
            raw_detail["rgb_mse_ppm"] = metrics.rgb_mse_ppm(image, raw_reference)
        verdict = metrics.gate(noise=noise, reference_residual=reference_residuals.get(frame),
                               detail_metrics=detail, motion=motion, policy=policy)
        if image.shape[0] * image.shape[1] != noise_metric.settings["production_calibrated_px"]:
            verdict["unavailable"].append("noise_size_uncalibrated")
            if verdict["rc"] == 0:
                verdict.update(rc=3, verdict="CANNOT_SEE")
        row = {"frame": frame, "noise_ppm": noise, "raw_noise_ppm": raw_noise,
               "rgb_sha256": hashlib.sha256(image.astype("uint8").tobytes()).hexdigest(),
               "reference_flicker_ppm": reference_residuals.get(frame), "detail": detail,
               "detail_raw_reference": raw_detail,
               "motion": motion, "timings_ms": receipt["timings_ms"], **verdict}
        frames.append(row)
        failures.extend({"frame": frame, "reason": r} for r in verdict["failed"])
        unavailable.extend({"frame": frame, "reason": r} for r in verdict["unavailable"])
        previous = frame, luma, guide
    rc = core.worst_rc([f["rc"] for f in frames])
    report = {"schema": "af.render-quality/v1", "rc": rc, "verdict": core.VERDICT[rc],
              "policy": policy, "policy_sha256": core.sha256_json(policy),
              "policy_file_sha256": core.sha256_file(policy_path),
              "run_sha256": core.sha256_file(root / "receipt.json"),
              "metric_code_sha256": core.sha256_file(HERE / "metrics.py"), "vqa3d": noise_metric.identity,
              "frames": frames, "temporal_pairs": pairs, "reference_pairs": reference_pairs,
              "reference_frames": reference_frames,
              "failures": failures, "unavailable": unavailable,
              "limits": ["Thresholds are DESIGN_CHOICE, not statistically calibrated.",
                         "Motion residual does not distinguish physical illumination changes without dense references.",
                         "Motion is block translation within the declared radius; excluded blocks remain unobserved.",
                         "Detail is measured only at actual reference frames; missing truth never passes.",
                         "Primary reference is OIDN-cleaned high-sample Cycles; shared denoiser bias is not excluded. Raw-reference diagnostics are also retained.",
                         "Noise is vqa3d's local outlier proxy, not a full perceptual quality score."]}
    core.validate(report, core.load_json(HERE / "quality.schema.json", max_bytes=32768))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--vqa-root", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    parser.add_argument("--expected-request-sha256")
    try:
        args = parser.parse_args(argv)
        result = evaluate(args.run, vqa_root=args.vqa_root, policy_path=args.policy,
                          expected_request_sha256=args.expected_request_sha256)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    except Refusal as exc:
        result = {"rc": exc.rc, "verdict": core.VERDICT[exc.rc], "reason_codes": [exc.reason]}
    except (OSError, ValueError, KeyError, TypeError, core.ContractError) as exc:
        result = {"rc": 2, "verdict": "ERROR", "reason_codes": [type(exc).__name__ + ":" + str(exc)]}
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
