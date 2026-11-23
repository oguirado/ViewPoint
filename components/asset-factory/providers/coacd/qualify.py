"""Frozen native experiments, never a production licence/admission bypass.

Freeze inputs, explicit policies, runtime identity and wrapper before running.
All outputs are qualification evidence; even a measured PASS leaves the
provider's production readiness unchanged. No repair or source simplification.
"""
from pathlib import Path
import re
import sys
import tempfile

from asset_factory import core
from . import adapter, worker
from .contracts import Refusal, VERSION, require, validate_policy, validate_request


def freeze(cases):
    require(isinstance(cases, list) and bool(cases), 2, "cases_invalid")
    runtime = worker.runtime_info()
    require(runtime["coacd_available"], 3, "coacd_unavailable")
    require(runtime["coacd_version"] == VERSION and runtime["code_sha256"],
            4, "runtime_version_stale")
    names, frozen = set(), []
    for case in cases:
        require(set(case) == {"name", "request", "policy"}, 2, "case_invalid")
        name = case["name"]
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name)
                and name not in names, 2, "case_name_invalid")
        names.add(name)
        registration = validate_request(case["request"])
        validate_policy(case["policy"])
        require(case["request"]["params"]["max_hulls"] == case["policy"]["max_hulls"],
                2, "hull_budget_mismatch")
        require(case["request"]["context"]["toolchain_sha256"] == adapter.toolchain_identity(),
                4, "toolchain_stale")
        source = case["request"]["inputs"][0]
        require(core.sha256_file(Path(source["path"])) == source["sha256"], 4, "source_stale")
        frozen.append(dict(case, schema_registration=registration))
    return {"schema": "af.coacd-qualification-plan/v1", "runtime": runtime,
            "toolchain_sha256": adapter.toolchain_identity(), "cases": frozen,
            "production_ready": False}


def check_plan(plan, expected_sha256):
    require(core.sha256_json(plan) == expected_sha256, 4, "freeze_stale")
    require(plan["schema"] == "af.coacd-qualification-plan/v1"
            and plan["production_ready"] is False, 2, "plan_invalid")
    require(plan["toolchain_sha256"] == adapter.toolchain_identity(), 4, "toolchain_stale")
    require(plan["runtime"] == worker.runtime_info(), 4, "runtime_code_stale")


def run_case(plan, expected_sha256, name, output_dir):
    check_plan(plan, expected_sha256)
    cases = [case for case in plan["cases"] if case["name"] == name]
    require(len(cases) == 1, 2, "case_invalid")
    case = cases[0]
    request, policy = case["request"], case["policy"]
    validate_request(request)
    validate_policy(policy)
    ref = request["inputs"][0]
    source = adapter.read_source(ref["path"])
    require(core.sha256_file(Path(ref["path"])) == ref["sha256"], 4, "source_stale")
    job = adapter._job("qualify", source, policy, params=request["params"])
    require(job["source_sha256"] == ref["sha256"], 4, "source_stale")
    job["qualification_runtime"] = plan["runtime"]
    folder = Path(output_dir)
    require(not folder.exists() and not folder.is_symlink(), 2, "output_exists")
    folder.mkdir()  # exclusive evidence directory; never publish it as an asset
    (folder / "source.glb").write_bytes(source)
    core.atomic_write_json(folder / "job.json", job)
    core.atomic_write_json(folder / "freeze.json", plan)
    try:
        rec = adapter._spawn(folder, min(policy["timeout_s"], request["context"]["timeout_s"]))
    except Refusal as exc:
        from .contracts import finish, receipt
        rec = finish(receipt(ref["sha256"], core.sha256_json(policy)), exc.rc, [exc.code])
        core.atomic_write_json(folder / "worker-receipt.json", rec)
    require(core.sha256_file(Path(ref["path"])) == ref["sha256"], 4, "source_stale")
    check_plan(plan, expected_sha256)
    hulls = [{"path": p.name, "sha256": core.sha256_file(p), "source_sha256": ref["sha256"]}
             for p in sorted(folder.glob("hull_*.obj"))]
    result = {"schema": "af.coacd-qualification/v1", "name": name,
              "freeze_sha256": expected_sha256, "source_sha256": ref["sha256"],
              "policy_sha256": core.sha256_json(policy), "toolchain_sha256": plan["toolchain_sha256"],
              "runtime": plan["runtime"], "params": request["params"],
              "rc": rec["rc"], "verdict": rec["verdict"], "production_ready": False,
              "hulls": hulls, "gate": {"path": "worker-receipt.json",
                                       "sha256": core.sha256_file(folder / "worker-receipt.json")}}
    core.atomic_write_json(folder / "qualification.json", result)
    return result


def main(argv=None):
    try:
        parser = adapter.Parser(description=__doc__)
        parser.add_argument("operation", choices=("freeze", "run"))
        parser.add_argument("--cases")
        parser.add_argument("--plan", required=True)
        parser.add_argument("--plan-sha256")
        parser.add_argument("--case")
        parser.add_argument("--output-dir")
        args = parser.parse_args(argv)
        plan_path = Path(args.plan)
        if args.operation == "freeze":
            require(args.cases and not plan_path.exists() and not plan_path.is_symlink(),
                    2, "freeze_output_exists_or_cases_missing")
            plan = freeze(core.load_json(Path(args.cases), max_bytes=1024 * 1024))
            # Exclusive creation: a freeze is never silently replaced.
            with tempfile.TemporaryDirectory(prefix=".af-freeze-", dir=plan_path.parent) as temp:
                staged = Path(temp) / "freeze.json"
                core.atomic_write_json(staged, plan)
                adapter._rename_new(staged, plan_path)
            return core.emit({"rc": 0, "freeze_sha256": core.sha256_json(plan)})
        require(args.plan_sha256 and args.case and args.output_dir, 2, "usage_error")
        return core.emit(run_case(core.load_json(plan_path, max_bytes=1024 * 1024),
                                 args.plan_sha256, args.case, args.output_dir))
    except Refusal as exc:
        return core.emit({"rc": exc.rc, "reason_codes": [exc.code]})
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except (OSError, ValueError, TypeError, KeyError):
        return core.emit({"rc": 2, "reason_codes": ["qualification_invalid"]})


if __name__ == "__main__":
    sys.exit(main())
