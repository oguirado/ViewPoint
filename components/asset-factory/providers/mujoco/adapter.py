"""Physics slot admission; the bundled provider stays blocked until qualified."""
from pathlib import Path
import argparse
import sys
import tempfile
import time

from asset_factory import contracts as slot_contracts, core
from tools.physics import api
from tools.physics.contracts import Refusal, require, resolve_file, digest

HERE = Path(__file__).resolve().parent


def validate_request(request):
    registered = HERE.parents[1] / "slots/physics.schema.json"
    path = registered if registered.is_file() else HERE / "physics.schema.draft.json"
    core.validate(request, core.load_json(path, max_bytes=256*1024))
    require(request.get("kind") == "request" and request.get("provider_id") == "mujoco")
    require(request["params"] == {}, code="params_unsupported")
    roles = [i["role"] for i in request["inputs"]]
    require(len(roles) == 2 and set(roles) == {"physics_scene", "physics_protocol"}, code="input_exclusivity")
    require(request["provider_revision"] not in ("main", "master", "latest", "HEAD"), code="unpinned_revision")
    for item in request["inputs"]:
        digest(item["sha256"])
    if "physics" in slot_contracts.SLOTS:
        try:
            slot_contracts.validate_slot(request, "physics", "request")
        except core.ContractError as exc:
            raise Refusal(exc.rc, exc.code) from exc
    return "registered" if registered.is_file() else "draft"


def execute(request, provider, decision, factory, policy, *, evidence_root, output_dir):
    start = time.monotonic_ns()
    try:
        validate_request(request)
        schema = HERE.parents[1] / "slots/provider.schema.json"
        if "physics" not in slot_contracts.SLOTS:
            schema = HERE / "provider.schema.draft.json"
        core.validate(provider, core.load_json(schema, max_bytes=256*1024))
        if "physics" in slot_contracts.SLOTS:
            slot_contracts.validate_provider(provider)
        require(provider["id"] == "mujoco" and provider["slot"] == "physics"
                and provider["kind"] == "tool" and provider["revision"] == request["provider_revision"])
        require(provider["runnable"], 5, "provider_blocked")
        require(type(factory.get("commercial_use")) is bool
                and request["context"]["commercial_use"] == factory["commercial_use"]
                and request["context"]["jurisdiction"] == factory["jurisdiction"])
        require(decision.get("schema") == "af.licence-decision/v1"
                and decision.get("provider_id") == "mujoco" and decision.get("operation") == "physics")
        require(decision.get("configuration_sha256") == core.sha256_json(factory)
                and decision.get("provider_sha256") == core.sha256_json(provider)
                and decision.get("policy_sha256") == core.sha256_json(policy)
                and request["context"]["policy_sha256"] == core.sha256_json(policy), 4, "decision_stale")
        require(decision.get("allowed") is True and type(decision.get("rc")) is int
                and decision["rc"] == 0 and decision.get("verdict") == "PASS", 5, "licence_refused")
        require(decision.get("jurisdiction") == factory["jurisdiction"]
                and type(decision.get("commercial_use")) is bool
                and decision["commercial_use"] == factory["commercial_use"], 4, "decision_context_stale")
        require(provider["runtime"]["ram_peak_gb_measured"] is not None, 3, "peak_unmeasured")
        target = Path(output_dir)
        require(not target.exists() and not target.is_symlink(), code="output_exists")
        paths = {item["role"]: resolve_file(evidence_root, {k:v for k,v in item.items() if k != "role"})
                 for item in request["inputs"]}
        protocol = core.load_json(paths["physics_protocol"], max_bytes=8*2**20)
        require(0 < protocol["timeout_s"] <= request["context"]["timeout_s"])
        with tempfile.TemporaryDirectory(prefix=".physics-slot-", dir=target.parent) as staging:
            generation = Path(staging) / "generation"
            receipt = api.qualification(paths["physics_scene"], paths["physics_protocol"],
                                        evidence_root=evidence_root, output_dir=generation)
            require(receipt["rc"] == 0, receipt["rc"], receipt["reason_codes"][0] if receipt["reason_codes"] else "physics_refused")
            result = _result(request, 0, (time.monotonic_ns()-start)//1000000)
            result["metrics"]["peak_rss_bytes"] = receipt["peak_rss_bytes"]
            result["absence_reasons"] = {}
            for role, filename, schema_id in (("physics_trace", "trace-0.json", "af.physics-trace/v1"),
                                               ("physics_receipt", "receipt.json", "af.physics-receipt/v1")):
                reference = {"role":role,"path":filename,"media_type":"application/json",
                             "sha256":core.sha256_file(generation/filename)}
                result["outputs"].append(reference)
                if role == "physics_receipt":
                    result["receipts"].append({"path":filename,"sha256":reference["sha256"],"schema":schema_id})
            core.atomic_write_json(generation/"result.json", result)
            for role, path in paths.items():
                original = next(i for i in request["inputs"] if i["role"] == role)
                require(core.sha256_file(path) == original["sha256"], 4, "request_input_stale")
            core.atomic_write_json(generation/"request.json", request)
            api._publish_directory(generation, target)
            return result
    except Refusal as exc:
        rc, reason = exc.rc, exc.code
    except (core.ContractError, OSError, ValueError, KeyError, TypeError):
        rc, reason = 2, "physics_slot_invalid"
    return _result(request, rc, (time.monotonic_ns()-start)//1000000, reason)


def _result(request, rc, wall_ms, reason=None):
    request = request if type(request) is dict else {}
    return {"schema":"af.physics/v1", "kind":"result", "slot":"physics",
            "request_id":request.get("request_id", ""), "provider_id":"mujoco",
            "provider_revision":request.get("provider_revision", ""), "rc":rc, "status":core.STATUS[rc],
            "outputs":[], "receipts":[], "metrics":{"wall_ms":wall_ms,"peak_rss_bytes":None},
            "absence_reasons":{"peak_rss_bytes":"runtime_not_observed", **({"physics":reason} if reason else {})}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    for key in ("request", "provider", "decision", "factory", "policy", "root", "out"):
        parser.add_argument("--"+key, required=True)
    args = parser.parse_args(argv)
    try:
        objects = [core.load_json(Path(getattr(args,key)), max_bytes=8*2**20)
                   for key in ("request", "provider", "decision", "factory", "policy")]
        return core.emit(execute(*objects, evidence_root=args.root, output_dir=args.out))
    except core.ContractError as exc:
        return core.emit(exc.as_dict())


if __name__ == "__main__":
    sys.exit(main())
