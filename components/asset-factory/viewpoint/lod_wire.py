#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fixture-scoped adapter for the frozen vp_factory_call af.lod selector."""

import argparse
import fcntl
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import warnings

OP = "af.lod"
TOOL = "vp_factory_call"
MEDIA = "model/gltf-binary"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ENV = {
    "PATH": os.defpath, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC",
    "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
}


class Rejected(Exception):
    """An intentional, observable refusal."""


def require(condition, code):
    if not condition:
        raise Rejected(code)


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON property")
    return result


def read(path):
    with Path(path).open("rb") as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("JSON exceeds lane budget")
    return json.loads(data, object_pairs_hook=unique_object)


def canonical(obj):
    encoded = json.dumps(obj, allow_nan=False, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return (encoded + "\n").encode("utf-8")


def hash_json(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, obj):
    with path.open("xb") as stream:
        stream.write(canonical(obj))


def within(root, path, exists=True):
    path = Path(path)
    require(path.is_absolute() and root in path.parents and path.resolve(strict=exists) == path,
            "EXCHANGE_PATH_OUTSIDE_ROOT")
    return path


def pinned(path, expected):
    if digest(path) != expected:
        raise RuntimeError(f"input pin stale: {path}")
    return path


def schema_valid(value, schema, artifact_schema=None):
    from jsonschema import Draft202012Validator

    if artifact_schema is None:
        return Draft202012Validator(schema).is_valid(value)
    # The qualified system jsonschema predates python-referencing.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from jsonschema import RefResolver
    resolver = RefResolver.from_schema(schema, store={artifact_schema["$id"]: artifact_schema})
    return Draft202012Validator(schema, resolver=resolver).is_valid(value)


def load_config(path):
    path = path.absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("noncanonical command configuration")
    config = read(path)
    row = config["owner_row"]
    if (config["selector"] != OP or config["scope"] != "offline_fixture"
            or config["live_use_permitted"] is not False or config["status"] != "factory_pending"
            or row["supported"] is not False or row["binding"]["kind"] != "factory_pending"
            or row["arguments"]["properties"]["op"]["const"] != OP
            or row["factory"]["input_stages"] != ["mesh"]
            or row["factory"]["checkpoint"] != {"backend": "blender_rw1", "name": "lod_chain", "phase": "after"}
            or hash_json(row["arguments"]) != row["args_schema_sha256"]
            or hash_json(row["reply"]) != row["reply_schema_sha256"]
            or hash_json({"op": OP, "binding": row["binding"]}) != row["binding_sha256"]
            or hash_json(row["factory"]["checkpoint"]) != config["checkpoint_sha256"]):
        raise RuntimeError("owner contract pin mismatch")
    source = config["sources"]
    matrix = read(pinned(Path(source["matrix"]["path"]), source["matrix"]["sha256"]))
    if matrix["ops"][OP] != row or matrix["artifact_ref"] != config["artifact_schema"]:
        raise RuntimeError("owner matrix projection mismatch")
    for label in ("handler", "topology_schema", "native_receipt"):
        pin = source[label]
        pinned(Path(pin["path"]), pin["sha256"])
    return path.parent, config


def artifact(path, stage):
    return {"schema": "vp.artifact_ref/v1", "locator": path.as_uri(), "bytes": path.stat().st_size,
            "sha256": digest(path), "media_type": MEDIA, "stage": stage, "copy": "file"}


def resolve_ref(ref, root):
    uri = urlsplit(ref["locator"])
    require(uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment or "%" in uri.path),
            "EXCHANGE_LOCATOR_UNSUPPORTED")
    try:
        path = within(root, Path(uri.path))
    except FileNotFoundError as exc:
        raise Rejected("ARTIFACT_MISSING") from exc
    require(path.is_file() and path.stat().st_size == ref["bytes"] and digest(path) == ref["sha256"],
            "SOURCE_DIGEST_MISMATCH")
    return path


def admit(request, lane, config):
    if not isinstance(request, dict) or request.get("dry_run") is not False:
        raise Rejected("LIVE_REQUEST_REQUIRED")
    request_id = request.get("request_id")
    context = request.get("context")
    require(set(request) == {"schema", "request_id", "op", "context", "args", "dry_run"}
            and request["schema"] == "vp.control.req/v1" and request["op"] == TOOL
            and isinstance(request_id, str) and IDENTIFIER.fullmatch(request_id)
            and isinstance(context, dict) and set(context) == {"caller", "agent_instance"}
            and context["caller"] in ("agent", "operator")
            and isinstance(context["agent_instance"], str) and IDENTIFIER.fullmatch(context["agent_instance"]),
            "INVALID_ENVELOPE")
    args = request["args"]
    require(schema_valid(args, config["owner_row"]["arguments"], config["artifact_schema"]),
            "INVALID_OWNER_ARGS")
    require(IDENTIFIER.fullmatch(args["goal_id"]) is not None, "INVALID_OWNER_ARGS")
    require([ref["stage"] for ref in args["inputs"]] == config["owner_row"]["factory"]["input_stages"],
            "INPUT_STAGE_MISMATCH")
    ref, = args["inputs"]
    require(ref["media_type"] == MEDIA, "MESH_MEDIA_REQUIRED")
    mesh = resolve_ref(ref, lane / "fixtures")
    permit = config["diagnostic_permit"]
    authorized = permit["subjects"].get(mesh.name)
    require(args.get("permit_ref") == permit["id"] and authorized is not None
            and authorized["sha256"] == ref["sha256"], "FIXTURE_PERMIT_SCOPE")
    require(args.get("seed", 0) == config["producer"]["seed"], "SEED_UNSUPPORTED")
    return mesh, authorized


def stage_producer(attempt, config):
    native = config["producer"]
    target = attempt / "producer"
    target.mkdir()
    for name, expected in native["stage_files"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RuntimeError("unsafe configured producer file")
        src = pinned(Path(native["source"]) / name, expected)
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        pinned(dst, expected)
    return target


def producer_request(request, mesh, cfg, producer):
    policy = read(producer / "criteria.json")
    native = cfg["producer"]
    bundle = read(producer / "native-bundle.json")
    provider = bundle["provider"]
    return {
        "schema": native["schema"], "kind": "request", "slot": provider["slot"],
        "request_id": request["request_id"], "provider_id": provider["id"],
        "provider_revision": provider["revision"],
        "inputs": [{"path": str(mesh), "role": native["input_role"], "media_type": MEDIA,
                    "sha256": request["args"]["inputs"][0]["sha256"]}],
        "params": {"method": native["method"]},
        "context": {"commercial_use": False, "policy_sha256": hash_json(policy),
                    "toolchain_sha256": bundle["source_bundle_sha256"], "timeout_s": native["timeout_s"]},
    }


def scope_command(attempt, config, scope, adjustment):
    native = config["producer"]
    return [*native["scope_prefix"], f"--unit={scope}", *native["scope_limits"],
            "nice", "-n", str(adjustment), "taskset", "-c", native["affinity"],
            *native["argv_prefix"], str(attempt / "producer/entry.py"), "run",
            str(attempt / "producer-request.json"), str(attempt / "producer/native")]


def child_env(attempt, use_bus=False):
    env = dict(ENV, TMPDIR=str(attempt), XDG_CACHE_HOME=str(attempt))
    if use_bus:
        for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def inspect_producer(attempt, request, mesh, subject, config):
    producer = attempt / "producer"
    output = producer / "native"
    request_path = attempt / "producer-request.json"
    result_path = output / "result.json"
    expected = producer_request(request, mesh, config, producer)
    for name, pin in config["producer"]["stage_files"].items():
        pinned(within(producer, producer / name), pin)
    schema_pin = config["sources"]["topology_schema"]
    topology_schema = read(Path(schema_pin["path"]))
    require(read(request_path) == expected and schema_valid(expected, topology_schema),
            "PRODUCER_REQUEST_MISMATCH")
    result = read(result_path)
    require(schema_valid(result, topology_schema) and result["kind"] == "result"
            and result["rc"] == 0 and result["status"] == "OK"
            and all(result[key] == expected[key] for key in
                    ("request_id", "schema", "slot", "provider_id", "provider_revision")),
            "PRODUCER_RESULT_MISMATCH")
    source_sha = request["args"]["inputs"][0]["sha256"]
    repaired = within(output, output / "repaired.glb")
    repaired_sha = digest(repaired)
    repair_path = within(output, output / "repair.json")
    repair = read(repair_path)
    require(digest(mesh) == source_sha == repair["input_sha256"] and repair["rc"] == 0
            and repair["candidate_sha256"] == repaired_sha, "CHAIN_SOURCE_MISMATCH")

    engine = output / "engine"
    engine_path = within(output, output / "engine-attempt.json")
    engine_record = read(engine_path)
    require(engine_record == read(within(engine, engine / "receipt.json"))
            and engine_record["raw_sha256"] == repaired_sha and engine_record["rc"] == 0
            and engine_record["status"] == "OK" and engine_record["scope"] == "geometry-only"
            and engine_record["method"] == config["producer"]["method"]
            and engine_record["parameters"]["seed"] == config["producer"]["seed"], "ENGINE_CHAIN_MISMATCH")
    names = config["producer"]["level_names"]
    levels = [within(engine, engine / name) for name in names]
    refs = [artifact(level, config["output_stage"]) for level in levels]
    records = {row["path"]: row for row in engine_record["artifacts"]}
    ancestors = [repaired_sha, refs[0]["sha256"], refs[0]["sha256"]]
    roles = ["master", "LOD1", "LOD2"]
    for ref, level, ancestor, role in zip(refs, levels, ancestors, roles, strict=True):
        record = records[level.name]
        metrics = within(engine, engine / record["metrics_receipt_path"])
        require(record["role"] == role and record["source_sha256"] == ancestor
                and ref["sha256"] == record["sha256"] and digest(metrics) == record["metrics_receipt_sha256"],
                "LOD_ARTIFACT_MISMATCH")
    require(result["outputs"] == [{"path": str(levels[0]), "sha256": refs[0]["sha256"],
                                   "media_type": MEDIA, "role": "topology_glb"}],
            "PRODUCER_OUTPUT_MISMATCH")
    evidence_path = within(output, output / "evidence.json")
    certificate_path = within(output, output / "certificate.json")
    evidence = read(evidence_path)
    require(result["receipts"] == [{"path": str(evidence_path), "sha256": digest(evidence_path),
                                    "schema": evidence["schema"]}]
            and evidence["rc"] == 0 and evidence["qualification"] == "local_geometry_diagnostic"
            and evidence["scope"]["memory_high_bytes"] == 6 * 2**30
            and evidence["scope"]["memory_max_bytes"] == 8 * 2**30
            and evidence["scope"]["swap_max_bytes"] == 0
            and evidence["raw_sha256"] == source_sha and evidence["repaired_sha256"] == repaired_sha
            and evidence["level_sha256"] == [ref["sha256"] for ref in refs]
            and evidence["certificate"] == {"path": str(certificate_path), "sha256": digest(certificate_path)},
            "PRODUCER_EVIDENCE_MISMATCH")

    # Re-import the source, repaired mesh and each emitted level in the reader process.
    bundle = read(producer / "native-bundle.json")
    source_root = Path(bundle["source_root"])
    native = config["producer"]
    if (source_root != Path(schema_pin["path"]).parents[1]
            or hash_json(bundle["source_files"]) != native["source_bundle_sha256"]
            or bundle["source_bundle_sha256"] != native["source_bundle_sha256"]
            or bundle["engine"] != native["engine"]):
        raise RuntimeError("producer bundle pin mismatch")
    for name, pin in bundle["source_files"].items():
        base = source_root.parent if name.startswith(("asset-factory/", "blender-visual-qa/")) else producer
        pinned(within(base, base / name), pin)
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    spec = spec_from_file_location("lod_measurement", producer / "measurement.py")
    oracle = module_from_spec(spec)
    spec.loader.exec_module(oracle)
    criteria = read(producer / "criteria.json")
    policy = criteria["measurement"]
    raw_pin, raw_faces = oracle.read_mesh(mesh, policy)
    clean_pin, clean_faces = oracle.read_mesh(repaired, policy)
    require(raw_pin == "sha256:" + source_sha and clean_pin == "sha256:" + repaired_sha
            and len(raw_faces) == len(clean_faces) + 1
            and {frozenset(face) for face in raw_faces} == {frozenset(face) for face in clean_faces},
            "INDEPENDENT_REPAIR_MISMATCH")
    budgets = criteria["modes"][expected["params"]["method"]]
    bounds = policy["reference_bounds_um"][subject["reference"]]
    pins = ["sha256:" + pin for pin in [repaired_sha, *(ref["sha256"] for ref in refs)]]
    proof = oracle.measure(repaired, levels, [budgets["target_faces"], *budgets["lod_targets"]],
                           policy, expected_pins=pins, expected_bounds=bounds)
    require(proof["rc"] == 0 and proof == read(certificate_path), "INDEPENDENT_LOD_MISMATCH")
    chain = {"source_sha256": source_sha, "repaired_sha256": repaired_sha,
             "triangles": [len(raw_faces), *(entry["triangles"] for entry in proof["levels"])],
             "levels": refs, "certificate_sha256": digest(certificate_path)}
    return chain, {
        name: digest(path) for name, path in
        (("request", request_path), ("result", result_path), ("repair", repair_path),
         ("engine_receipt", engine_path), ("evidence", evidence_path), ("certificate", certificate_path))
    }


def verify(reply, request, work, lane, config):
    mesh, subject = admit(request, lane, config)
    require(schema_valid(reply, config["owner_row"]["reply"], config["artifact_schema"]),
            "INVALID_OWNER_RESULT")
    require(bool(reply["artifact_refs"]), "NO_SUBSTANTIVE_EFFECT")
    try:
        receipt_path = within(work, Path(reply["receipt"]["path"]))
    except FileNotFoundError as exc:
        raise Rejected("MISSING_EFFECT_RECEIPT") from exc
    require(digest(receipt_path) == reply["receipt"]["sha256"], "EFFECT_RECEIPT_STALE")
    receipt = read(receipt_path)
    require(receipt.get("schema") == "rp4.af.lod.execution/v1", "NO_SUBSTANTIVE_EFFECT")
    require(receipt.get("chain", {}).get("source_sha256") == request["args"]["inputs"][0]["sha256"],
            "CHAIN_SOURCE_MISMATCH")
    attempt = receipt_path.parent
    require(receipt_path.name == "execution.json" and attempt.name.startswith("attempt.")
            and attempt.parent == work / "jobs" / request["request_id"]
            and receipt["request_sha256"] == hash_json(request)
            and receipt["config_sha256"] == hash_json(config)
            and receipt["checkpoint"]["designation"] == config["owner_row"]["factory"]["checkpoint"]
            and receipt["checkpoint"]["custody"] == config["scope"]
            and receipt["permit"] == request["args"]["permit_ref"], "EXECUTION_IDENTITY_MISMATCH")
    command = receipt["command"]
    expected_scope = f"rp4-wire-lod-{attempt.name.split('.')[-1]}.scope"
    require(command["scope"] == expected_scope and type(command["niceness_adjustment"]) is int
            and command["argv"] == scope_command(attempt, config, expected_scope, command["niceness_adjustment"])
            and command["scope_after"] == "inactive" and command["rc"] == 0
            and command["engine"] == config["producer"]["engine"],
            "COMMAND_IDENTITY_MISMATCH")
    pinned(Path(command["engine"]["path"]), command["engine"]["sha256"])
    chain, pins = inspect_producer(attempt, request, mesh, subject, config)
    require(receipt["chain"] == chain and receipt["producer_pins"] == pins
            and receipt["checkpoint"]["state_sha256"] == hash_json(chain)
            and reply["artifact_refs"] == chain["levels"] and reply.get("gate") == config["gate"],
            "LOD_CHECKPOINT_MISMATCH")
    for ref in reply["artifact_refs"]:
        resolve_ref(ref, attempt)
    return {"status": "RESOLVED", "chain": chain, "command": command,
            "checkpoint": receipt["checkpoint"], "producer_pins": pins}


def run(request, work, lane, config):
    mesh, subject = admit(request, lane, config)
    jobs = work / "jobs"
    jobs.mkdir(exist_ok=True)
    within(work, jobs)
    job = jobs / request["request_id"]
    job.mkdir(exist_ok=True)
    within(jobs, job)
    committed = job / "reply.json"
    fd = os.open(job / ".claim", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(fd, "rb") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise RuntimeError("request claim is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if committed.exists():
            cached = read(within(job, committed))
            old = read(within(job, Path(cached["receipt"]["path"])))
            require(old["request_sha256"] == hash_json(request), "REQUEST_ID_CONFLICT")
            verify(cached, request, work, lane, config)
            return cached

        attempt = Path(tempfile.mkdtemp(prefix="attempt.", dir=job))
        producer = stage_producer(attempt, config)
        write(attempt / "producer-request.json", producer_request(request, mesh, config, producer))
        priority = os.getpriority(os.PRIO_PROCESS, 0)
        if priority > 15:
            raise RuntimeError("cannot set producer niceness to 15")
        adjustment = 15 - priority
        scope = f"rp4-wire-lod-{attempt.name.split('.')[-1]}.scope"
        argv = scope_command(attempt, config, scope, adjustment)
        env = child_env(attempt, use_bus=True)
        with (attempt / "producer.log").open("xb") as log:
            rc = subprocess.run(argv, cwd=attempt, env=env, stdout=log,
                                stderr=subprocess.STDOUT, check=False).returncode
        inactive = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState",
                                   "--value", scope], env=env, cwd=attempt, capture_output=True,
                                  text=True, check=True).stdout.strip()
        if rc or inactive != "inactive":
            raise RuntimeError(f"producer rc {rc}, scope {inactive}. See {attempt / 'producer.log'}")
        chain, pins = inspect_producer(attempt, request, mesh, subject, config)
        record = {"schema": "rp4.af.lod.execution/v1", "request_sha256": hash_json(request),
                  "config_sha256": hash_json(config), "permit": request["args"]["permit_ref"],
                  "chain": chain, "producer_pins": pins,
                  "checkpoint": {"designation": config["owner_row"]["factory"]["checkpoint"],
                                 "custody": config["scope"], "state_sha256": hash_json(chain)},
                  "command": {"argv": argv, "scope": scope, "rc": rc, "scope_after": inactive,
                              "niceness_adjustment": adjustment, "engine": read(producer / "native-bundle.json")["engine"]}}
        execution = attempt / "execution.json"
        write(execution, record)
        reply = {"artifact_refs": chain["levels"], "gate": config["gate"],
                 "receipt": {"path": str(execution), "sha256": digest(execution)}}
        require(schema_valid(reply, config["owner_row"]["reply"], config["artifact_schema"]),
                "INVALID_OWNER_RESULT")
        pending = attempt / "reply.json"
        write(pending, reply)
        os.replace(pending, committed)
        return reply


def main():
    if sys.argv[1:2] == ["inspect"]:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from viewpoint.lod_inspection import main as inspect_main

        return inspect_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "verify"))
    parser.add_argument("config", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("request", type=Path)
    parser.add_argument("reply", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        lane, config = load_config(args.config)
        work = within(lane / "runs", args.work)
        request = read(within(work, args.request))
        if args.mode == "run" and args.reply is None:
            result = run(request, work, lane, config)
        elif args.mode == "verify" and args.reply is not None:
            result = verify(read(within(work, args.reply)), request, work, lane, config)
        else:
            return 2
        sys.stdout.buffer.write(canonical(result))
        return 0
    except Rejected as exc:
        print(json.dumps({"rejected": str(exc)}))
        return 1
    except (KeyError, ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Harness error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
