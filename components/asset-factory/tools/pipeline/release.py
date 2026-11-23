"""Plan publication of a verified pipeline export through the local registry."""

from copy import deepcopy
from pathlib import Path
import re

from asset_factory import core
from tools.pipeline import engine
from tools.pipeline.contracts import RUN, validate_stage
from tools.pipeline.engine import Refusal, require
from tools.publish import publisher

# INT must emit this scope only for a production-qualified run. NativeStages
# presently uses a generic scope for both native fixtures and ordinary runs.
PRODUCTION_SCOPE = "production"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _read(ref, config, role, *, capture=None):
    path = Path(ref["path"])
    root = Path(config["source_root"])
    require(path.is_absolute() and ".." not in path.parts and path.is_relative_to(root),
            2, role + "_outside_source_root")
    publisher.enforce_hygiene(path.parts)
    require(path.exists() or path.is_symlink(), 3, role + "_missing")
    _, measured, raw = publisher.source_snapshot(
        str(path), root, timeout_s=config["timeout_s"], capture_limit=capture)
    require(measured["sha256"] == ref["sha256"], 4, role + "_digest_mismatch")
    return raw


def _verified_chain(path, digest, config):
    require(type(digest) is str and _SHA256.fullmatch(digest), 2, "invalid_run_digest")
    header = {"path": str(path), "sha256": digest}
    raw = _read(header, config, "run", capture=engine.MAX_JSON)
    run = publisher.decode(raw)
    core.validate(run, RUN)
    rc = run["rc"]
    require((run["status"], run["verdict"]) == (core.STATUS[rc], core.VERDICT[rc]),
            4, "run_verdict_mismatch")
    require(len(run["stages"]) == len(engine.STAGES), 3, "stage_chain_incomplete")
    require(tuple(row["stage"] for row in run["stages"]) == engine.STAGES, 4, "stage_order_mismatch")
    chain = [row["receipt"] for row in run["stages"]]
    require(core.sha256_json(chain) == run["run_key"], 4, "run_key_mismatch")

    stage_files, native_evidence, stages, verified = {}, {}, {}, {}
    previous, blocked = None, None
    for row in run["stages"]:
        name, ref = row["stage"], row["receipt"]
        stage_raw = _read(ref, config, "stage_receipt", capture=engine.MAX_JSON)
        stage = publisher.decode(stage_raw)
        validate_stage(stage)
        require((stage["stage"], stage["rc"], stage["verdict"], stage["previous"]) ==
                (name, row["rc"], row["verdict"], previous), 4, "stage_link_mismatch")
        if blocked is not None:
            require(stage["rc"] == blocked and stage["reason_codes"] == ["upstream_blocked"],
                    4, "stage_after_refusal")
        elif stage["rc"] != 0:
            blocked = stage["rc"]
        if stage["rc"] == 0:
            identity = {key: stage[key] for key in
                        ("stage", "inputs", "parameters", "previous", "toolchain_sha256")}
            require(stage["key"] == core.sha256_json(identity), 4, "stage_key_mismatch")
        for item in stage["inputs"] + stage["outputs"] + stage["evidence"]:
            item_path, sha = item["path"], item["sha256"]
            if item_path not in verified:
                _read(item, config, "stage_witness")
                verified[item_path] = sha
            require(verified[item_path] == sha, 4, "stage_witness_digest_mismatch")
        for item in stage["evidence"]:
            if Path(item["path"]).suffix == ".json":
                logical = "receipts/" + name + "-" + Path(item["path"]).name
                require(logical not in native_evidence or native_evidence[logical] == item,
                        2, "ambiguous_stage_evidence")
                native_evidence[logical] = item
        stage_files["receipts/stage-" + name + ".json"] = (ref, stage_raw)
        stages[name] = stage
        previous = ref

    stage_rc = core.worst_rc(r["rc"] for r in run["stages"])
    # The engine can record a final source-verification refusal after all rows passed.
    require(rc == stage_rc or (stage_rc == 0 and rc == 3 and run["reason_codes"]),
            4, "aggregate_verdict_mismatch")
    require(run["checks_evaluated"] == sum(s["checks_evaluated"] for s in stages.values()),
            4, "run_check_count_mismatch")
    export = stages["export"]
    subject = next((item for item in export["outputs"] if item["path"] == export["subject"]), None)
    require(subject is not None and Path(subject["path"]).suffix == ".glb", 3, "export_missing")
    if rc == 0:
        require(run["scope"] == PRODUCTION_SCOPE, 5, "production_scope_required")
        terminal = stages[engine.STAGES[-1]]
        require(run["artifact"] in terminal["outputs"] and run["artifact"]["path"] == terminal["subject"]
                and run["artifact"]["sha256"] == subject["sha256"], 4, "terminal_export_mismatch")
    else:
        require(rc == 3 and run["artifact"] is None, 5, "run_not_releasable")
    return run, subject, stages, {"receipts/pipeline-run.json": (header, raw), **stage_files}, native_evidence


def _assembly_parent(registry, locator, snapshot, provider, export):
    closure = registry.resolve(locator, snapshot=snapshot)
    require(locator == closure["locator"], 2, "assembly_locator_must_be_pinned")
    record = closure["record"]
    lineage = record["lineage"]
    recipe_pin = lineage["assembly_recipe_digest"]
    mesh_digest = record["geometry_digest"]
    require(record["record_kind"] == "asset" and lineage["operation"] == "assembled"
            and recipe_pin in closure["objects"] and record["instances"],
            3, "assembly_closure_missing")
    require(mesh_digest in {"sha256:" + item["sha256"] for item in [export, *provider["inputs"]]},
            4, "assembly_run_subject_mismatch")
    recipe = publisher.decode(registry.read_blob(locator, recipe_pin, snapshot=snapshot))
    require(type(recipe) is dict and recipe.get("schema") == "af.assembly-recipe/v1"
            and type(recipe.get("parts")) is list, 3, "assembly_recipe_missing")
    instances = {item["instance_id"]: item for item in record["instances"]}
    require(len(instances) == len(record["instances"]) == len(recipe["parts"]),
            4, "assembly_instance_mismatch")
    for item in recipe["parts"]:
        require(type(item) is dict and type(item.get("instance_id")) is str,
                2, "assembly_recipe_invalid")
        instance = instances.pop(item["instance_id"], None)
        require(instance is not None and instance["record_digest"] == item.get("part_record_sha256")
                and instance["transform"] == item.get("transform"), 4, "assembly_instance_mismatch")
        part = closure["lineage_records"].get(instance["record_digest"], {})
        require(part.get("record_kind") == "part" and part.get("geometry_digest") == item.get("artifact_sha256"),
                4, "assembly_part_mismatch")
    return closure["record_digest"]


def build_release(run_path, *, run_sha256, registry, expected_snapshot, slug, version,
                  asset_class, record, blobs, publish_config, provenance, assembly_locator=None,
                  max_export_bytes=128 * 2**20):
    """Register an immutable asset and return its verified closure and offline PUB plan.

    The caller supplies an authorized registry, matching asset-record metadata,
    provenance and source root. A pinned assembly locator can contribute its
    recipe and part closure when its mesh was consumed by the run.
    """
    config = publisher.configuration(publish_config)
    require(config.get("source_root"), 3, "source_root_required")
    require(type(max_export_bytes) is int and max_export_bytes > 0, 2, "invalid_export_budget")
    run, export, stages, receipts, evidence = _verified_chain(run_path, run_sha256, config)

    mesh = _read(export, config, "export", capture=max_export_bytes)
    mesh_pin = "sha256:" + export["sha256"]
    require(record["record_kind"] == "asset" and record["geometry_digest"] == mesh_pin
            and any(item["digest"] == mesh_pin and item["media_type"] == "model/gltf-binary"
                    and item["size_bytes"] == len(mesh) for item in record["files"]),
            4, "record_export_mismatch")
    record = deepcopy(record)
    blobs = dict(blobs)
    dependencies = set(record["dependencies"])
    blobs[mesh_pin] = mesh
    for logical, (ref, raw) in receipts.items():
        pin = "sha256:" + ref["sha256"]
        record["files"].append({"digest": pin, "logical_path": logical, "role": "pipeline-receipt",
                                "media_type": "application/json", "size_bytes": len(raw)})
        blobs[pin] = raw
        dependencies.add(pin)
    if assembly_locator is not None:
        parent = _assembly_parent(registry, assembly_locator, expected_snapshot, stages["provider"], export)
        lineage = record["lineage"]
        lineage["parent_records"] = sorted(set(lineage["parent_records"]) | {parent})
        dependencies.add(parent)

    record["annotations"].update(pipeline_scope=run["scope"], pipeline_asset_verdict=run["verdict"])
    record["files"].sort(key=lambda item: (item["role"], item["logical_path"], item["digest"]))
    record["dependencies"] = sorted(dependencies)
    encoded_record = core.canonical_json(record).encode("utf-8")
    record_sha = core.sha256_json(record)

    verdict = run["verdict"]
    provenance = deepcopy(provenance)
    provenance["inputs"] += [{"role": "pipeline-run", "sha256": run_sha256},
                             {"role": "registry-record", "sha256": record_sha}]
    sources = {"asset.glb": export, **{name: ref for name, (ref, _) in receipts.items()}}
    if verdict == "CANNOT_SEE":
        provenance["absence_reasons"] = sorted(set(provenance["absence_reasons"] + run["reason_codes"]
                                                    + ["pipeline_acceptance_incomplete"]))
        provenance["gates"] = [{"name": "pipeline-run", "verdict": verdict,
                                "receipt": "receipts/pipeline-run.json", "verdict_key": "verdict",
                                "numbers": {}}]
    else:
        for gate in provenance["gates"]:
            name = gate["receipt"]
            require(name in evidence, 3, "production_gate_outside_chain")
            sources[name] = evidence[name]

    source = {"schema": "af.publish-source/v1", "assets": [{
        "asset_class": asset_class, "asset_id": slug, "version": version, "final_verdict": verdict,
        "files": [{"path": name, "source": ref["path"]} for name, ref in sources.items()],
        "provenance": provenance,
    }]}
    plan = publisher.build_plan(source, config)
    require(all(item["sha256"] == sources[item["path"]]["sha256"] for item in plan["assets"][0]["files"]
                if item["path"] in sources), 4, "publish_source_digest_mismatch")

    snapshot = registry.put(slug, encoded_record, blobs=blobs, expected_snapshot=expected_snapshot)
    locator = f"af://{registry.namespace}/{slug}@sha256:{record_sha}"
    return {"asset_verdict": verdict, "registry": registry.resolve(locator, snapshot=snapshot),
            "publish_plan": plan}
