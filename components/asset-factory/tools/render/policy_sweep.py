"""Select measured F5 settings within a frozen frame/configuration matrix.

python -m tools.render.policy_sweep --sweep JSON --vqa-root DIR [--output NEW_JSON]
The manifest schema is adjacent. Only a successful selection is published.
The minimum is observed wall cost within this matrix, never a global optimum.
"""
import argparse
from pathlib import Path

from asset_factory import core
from tools.render import evaluate, metrics
from tools.render.pipeline import HERE, Refusal


def choose(rows, *, required_ids, frames, context_sha256, policy):
    """Pure selection over authenticated, recomputed F5 observations."""
    if (not required_ids or len(set(required_ids)) != len(required_ids)
            or not frames or len(frames) < 2 or len(set(frames)) != len(frames)):
        raise Refusal(2, "invalid_obligations")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise Refusal(2, "duplicate_trial")
    if set(ids) != set(required_ids):
        raise Refusal(3, "trial_coverage_incomplete")
    results, passing, costs = [], [], []
    for row in rows:
        wall = row["wall_ms"]
        if type(wall) is not int or wall <= 0:
            raise Refusal(3, "trial_cost_unavailable")
        costs.append(wall)
        report = row.get("quality")
        if report is None:
            rc = row.get("failure_rc")
            if type(rc) is not int or rc not in (2, 3, 4, 5, 6):
                raise Refusal(2, "invalid_failed_trial")
            results.append({"id": row["id"], "rc": rc, "verdict": core.VERDICT[rc],
                            "cost_ms": None, "wall_ms": wall})
            continue
        if row.get("context_sha256") != context_sha256 or report["policy_sha256"] != core.sha256_json(policy):
            raise Refusal(4, "sweep_identity_drift")
        observed = report.get("frames", [])
        if [f["frame"] for f in observed] != frames:
            raise Refusal(3, "frame_coverage_incomplete")
        verdicts, cost = [], 0
        for frame in observed:
            times = frame["timings_ms"]
            if set(times) != {"render", "oidn", "tone_map"} or any(type(v) is not int or v < 0 for v in times.values()) or times["render"] <= 0:
                raise Refusal(3, "frame_cost_unavailable")
            cost += sum(times.values())
            # Do not accept a caller's summary PASS in place of the metric gate.
            check = metrics.gate(noise=frame.get("noise_ppm"),
                reference_residual=frame.get("reference_flicker_ppm"),
                detail_metrics=frame.get("detail"), motion=frame.get("motion"), policy=policy)
            verdicts.extend((check["rc"], frame["rc"]))
        rc = core.worst_rc(verdicts + [report["rc"]])
        result = {"id": row["id"], "rc": rc, "verdict": core.VERDICT[rc], "cost_ms": cost,
                  "wall_ms": wall, "samples": row["samples"], "denoise": row["denoise"],
                  "run_sha256": report["run_sha256"]}
        results.append(result)
        if rc == 0:
            passing.append(result)
    # One failed trial does not invalidate a different, fully witnessed pass.
    rc = 0 if passing else core.worst_rc([r["rc"] for r in results])
    return {"rc": rc, "verdict": core.VERDICT[rc], "scope": "declared_frames_only",
            "frames": frames, "policy_sha256": core.sha256_json(policy),
            "context_sha256": context_sha256,
            "selection": min(passing, key=lambda r: (r["cost_ms"], r["samples"], r["id"])) if passing else None,
            "trials": results, "total_attempt_wall_ms": sum(costs),
            "reason_codes": [] if passing else ["no_passing_setting"]}


def measure(path, *, vqa_root):
    path = Path(path)
    manifest = core.load_json(path, max_bytes=1048576)
    schema = core.load_json(Path(__file__).with_suffix(".schema.json"), max_bytes=32768)
    core.validate(manifest, schema)
    frames = manifest["frames"]
    if (len(frames) < 2 or len(frames) > 1000 or any(type(f) is not int for f in frames)
            or frames != list(range(frames[0], frames[0] + len(frames)))):
        raise Refusal(2, "invalid_frame_matrix")
    if len(manifest["trials"]) > 128:
        raise Refusal(6, "sweep_budget_exceeded")
    policy_path = (path.parent / manifest["policy"]["path"]).resolve()
    if core.sha256_file(policy_path) != manifest["policy"]["sha256"]:
        raise Refusal(4, "policy_bytes_changed")
    frozen = evaluate.load_policy(HERE / "policy.json")
    if core.load_json(policy_path, max_bytes=16384) != frozen:
        raise Refusal(4, "frozen_policy_drift")
    policy = evaluate.load_policy(policy_path)
    rows, configs, run_paths = [], [], set()
    for trial in manifest["trials"]:
        if type(trial["samples"]) is not int or trial["samples"] <= 0:
            raise Refusal(2, "invalid_sample_count")
        configs.append((trial["samples"], trial["denoise"]))
        row = dict(trial)
        row["quality"] = None
        if trial["run"] is not None:
            if trial["failure_rc"] is not None or trial["denoise"] != "oidn_guided":
                raise Refusal(2, "trial_contract_invalid")
            run_path = (path.parent / trial["run"]["path"]).resolve()
            if run_path in run_paths:
                raise Refusal(2, "reused_trial_incarnation")
            run_paths.add(run_path)
            if core.sha256_file(run_path) != trial["run"]["sha256"]:
                raise Refusal(4, "run_bytes_changed")
            run, receipts = evaluate.read_run(run_path.parent)
            params = run["parameters"]
            if params["samples"] != trial["samples"] or params["references"] != frames:
                raise Refusal(4, "trial_parameters_changed")
            report = evaluate.evaluate(run_path.parent, vqa_root=vqa_root, policy_path=policy_path)
            context = {"toolchain": run["toolchain"], "parameters": {k:v for k,v in params.items() if k != "samples"},
                "reference_receipts": [r["receipt_sha256"] for r in report["reference_frames"]],
                "frame_identity": [{k:r[k] for k in ("view","width","height","denoiser","inputs")} for _,r in receipts if r["samples"] == trial["samples"]]}
            row["context_sha256"] = core.sha256_json(context)
            row["quality"] = report
        rows.append(row)
    if len(set(configs)) != len(configs):
        raise Refusal(2, "duplicate_configuration")
    return choose(rows, required_ids=manifest["required_ids"], frames=frames,
                  context_sha256=manifest["context_sha256"], policy=policy)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", required=True, type=Path)
    parser.add_argument("--vqa-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    try:
        args = parser.parse_args(argv)
        result = measure(args.sweep, vqa_root=args.vqa_root)
        if result["rc"] == 0 and args.output:
            core.atomic_write_json(args.output, result)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    except Refusal as exc:
        result = {"rc": exc.rc, "verdict": core.VERDICT[exc.rc], "reason_codes": [exc.reason]}
    except (OSError, ValueError, KeyError, TypeError, core.ContractError) as exc:
        result = {"rc": 2, "verdict": "ERROR", "reason_codes": [type(exc).__name__ + ":" + str(exc)]}
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
