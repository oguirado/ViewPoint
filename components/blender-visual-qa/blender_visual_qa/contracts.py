"""Contracts and schema validation for blender_visual_qa."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource


class ContractError(Exception):
    """Structured contract or schema validation failure."""

    def __init__(self, message: str, code: str = "contract_error", stage: str = "validation") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage


def get_schemas_dir() -> Path:
    """Return the package schemas directory."""
    return Path(__file__).parent / "schemas"


BUILTIN_LOCAL_SCHEMAS: dict[str, dict[str, Any]] = {
    "urn:vqa:probe-config:v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:vqa:probe-config:v1",
        "type": "object",
        "properties": {
            "schema": {"const": "vqa.probe-config/v1"},
            "mm_per_unit": {"type": "integer", "minimum": 1},
            "default_actor_radius_mm": {"type": "integer", "minimum": 0},
            "safe_margin_px": {"type": "integer", "minimum": 0},
            "roles": {"type": "object"},
        },
        "required": ["schema", "roles"],
        "additionalProperties": False,
    },
    "urn:vqa:journal:v2": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:vqa:journal:v2",
        "type": "object",
        "properties": {
            "codec_ver": {"type": "integer", "minimum": 0},
            "entries": {"type": "array"},
            # Native journal semantics belong to the adapter selected by input_schema.
            # This envelope authenticates a complete header without naming a game.
            "format": {"type": "string", "minLength": 1},
            "manifest_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "ruleset_id": {"type": "integer", "minimum": 0},
            "seed": {"type": "integer", "minimum": 0},
        },
        "required": ["codec_ver", "entries"],
        "dependentRequired": {
            key: [other for other in ("format", "manifest_sha256", "ruleset_id", "seed") if other != key]
            for key in ("format", "manifest_sha256", "ruleset_id", "seed")
        },
        "additionalProperties": False,
    },
    "urn:vqa:test-config:v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:vqa:test-config:v1",
        "type": "object", "additionalProperties": False,
    },
    "urn:vqa:test-input:v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:vqa:test-input:v1",
        "type": "object", "additionalProperties": False,
    },
}


def _create_registry() -> Registry:
    """Create a referencing.Registry populated with local schemas."""
    schema_dir = get_schemas_dir()

    def retrieve(uri: str) -> Resource:
        if uri in BUILTIN_LOCAL_SCHEMAS:
            return Resource.from_contents(BUILTIN_LOCAL_SCHEMAS[uri])
        from referencing.exceptions import NoSuchResource
        raise NoSuchResource(uri)

    registry = Registry(retrieve=retrieve)

    # Pre-populate builtins
    for schema_id, schema_content in BUILTIN_LOCAL_SCHEMAS.items():
        registry = registry.with_resource(schema_id, Resource.from_contents(schema_content))

    # Pre-populate all available schema files
    if schema_dir.is_dir():
        for path in sorted(schema_dir.glob("*.schema.json")):
            try:
                content = load_json_file(path)
                schema_id = content.get("$id")
                if schema_id:
                    res = Resource.from_contents(content)
                    registry = registry.with_resource(schema_id, res)
            except Exception:
                pass

    return registry


_REGISTRY: Registry | None = None


def get_registry() -> Registry:
    """Return cached registry or initialize a new one."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _create_registry()
    return _REGISTRY


def register_schema(schema: dict[str, Any]) -> None:
    """Register an in-memory schema into the global registry."""
    global _REGISTRY
    reg = get_registry()
    schema_id = schema.get("$id")
    if schema_id:
        res = Resource.from_contents(schema)
        _REGISTRY = reg.with_resource(schema_id, res)


def _reject_duplicates_and_constants(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object pairs hook rejecting duplicate keys."""
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ContractError(f"Duplicate JSON key: {k!r}", code="duplicate_key", stage="validation")
        res[k] = v
    return res


def _parse_constant(c: str) -> Any:
    """Constant parser rejecting NaN/Infinity."""
    raise ContractError(f"Invalid non-standard JSON constant: {c!r}", code="invalid_constant", stage="validation")


def load_json_str(text: str) -> Any:
    """Load JSON from string, enforcing strict invariants."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicates_and_constants,
            parse_constant=_parse_constant,
        )
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(f"JSON decode error: {exc}", code="json_decode_error", stage="validation") from exc


def load_json_file(path: Path | str) -> Any:
    """Read a file as strict JSON with UTF-8 encoding."""
    target = Path(path)
    if not target.is_file():
        raise ContractError(f"File not found: {target}", code="file_not_found", stage="validation")
    try:
        raw_bytes = target.read_bytes()
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"File is not valid UTF-8: {target}", code="invalid_utf8", stage="validation") from exc
    except OSError as exc:
        raise ContractError(f"Unable to read file: {target}: {exc}", code="io_error", stage="validation") from exc

    value = load_json_str(text)
    _finite_tree(value)
    return value


# @anchor:bv.contracts.canonical
def canonical_json(obj: Any) -> str:
    """Return byte-stable canonical JSON string."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def canonical_json_bytes(obj: Any) -> bytes:
    """Return byte-stable canonical JSON bytes."""
    return canonical_json(obj).encode("utf-8")


def compute_sha256_bytes(data: bytes) -> str:
    """Compute sha256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_file_sha256(path: Path | str) -> str:
    """Compute sha256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# @anchor:bv.contracts.validate
def validate_schema(instance: dict[str, Any], schema_stem: str) -> None:
    """Validate instance against local schema stem (e.g. 'adapter', 'checklist')."""
    schema_path = get_schemas_dir() / f"{schema_stem}.v1.schema.json"
    if not schema_path.is_file():
        raise ContractError(
            f"Schema file not found for {schema_stem}: {schema_path}",
            code="E_SCHEMA",
            stage="validation",
        )
    schema = load_json_file(schema_path)
    registry = get_registry()
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    if errors:
        msg = f"Schema validation error for {schema_stem}: {errors[0].message} at {'/'.join(str(p) for p in errors[0].path)}"
        raise ContractError(msg, code="schema_violation", stage="validation")


def _check_boolean_not_int(val: Any, name: str) -> None:
    """Ensure boolean is not passed where int is required."""
    if isinstance(val, bool):
        raise ContractError(
            f"Field {name} cannot be a boolean (found {val})",
            code="boolean_as_integer",
            stage="validation",
        )


@dataclass
class Config:
    """Validated configuration bundle."""

    adapter: dict[str, Any]
    checklist: dict[str, Any]
    plan: dict[str, Any]
    adapter_path: Path
    checklist_path: Path
    plan_path: Path
    adapter_dir: Path
    run_id: str
    configuration_sha256: str
    input_files: list[dict[str, str]]
    tool_files: list[dict[str, str]]
    dependencies: list[dict[str, str]] = field(default_factory=list)


@dataclass
class RunBundle:
    """Validated run directory bundle."""

    run_dir: Path
    report: dict[str, Any]
    receipt: dict[str, Any] | None
    capture_manifest: dict[str, Any] | None
    bindings: list[dict[str, str]]


ALLOWED_ARGV_TOKENS = {"{project}", "{run_dir}", "{driver_input}", "{blender}", "{probe}"}


def _validate_driver_argv(argv: list[str]) -> None:
    """Validate driver.argv substitutions and executable path."""
    if not argv:
        raise ContractError("driver.argv cannot be empty", code="empty_driver_argv", stage="validation")

    exec_path = argv[0]
    # Check if first element is an allowed substitution token or absolute executable regular file
    if exec_path in ALLOWED_ARGV_TOKENS:
        pass
    else:
        if not os.path.isabs(exec_path):
            raise ContractError(
                f"driver.argv[0] must be an absolute path: {exec_path}",
                code="invalid_driver_executable",
                stage="validation",
            )
        p = Path(exec_path)
        if not (p.exists() and p.is_file()):
            raise ContractError(
                f"driver.argv[0] must be an existing regular file: {exec_path}",
                code="invalid_driver_executable",
                stage="validation",
            )
        if not os.access(p, os.X_OK):
            raise ContractError(
                f"driver.argv[0] must be executable: {exec_path}",
                code="invalid_driver_executable",
                stage="validation",
            )

    for i, token in enumerate(argv):
        if "{" in token or "}" in token:
            if token not in ALLOWED_ARGV_TOKENS:
                raise ContractError(
                    f"Forbidden substitution in driver.argv[{i}]: {token!r}. Only exact tokens {sorted(ALLOWED_ARGV_TOKENS)} allowed.",
                    code="invalid_argv_substitution",
                    stage="validation",
                )


def _validate_path_traversal(p_str: str, name: str) -> None:
    """Ensure relative path does not start with / or contain .."""
    if p_str.startswith("/"):
        raise ContractError(f"Field {name} must be a relative path: {p_str}", code="path_escape", stage="validation")
    parts = Path(p_str).parts
    if ".." in parts:
        raise ContractError(
            f"Field {name} contains forbidden '..' traversal: {p_str}",
            code="path_escape",
            stage="validation",
        )


def _collect_tool_files():
    root = Path(__file__).resolve().parent.parent
    files = sorted((root / "blender_visual_qa").rglob("*.py"))
    files += sorted(get_schemas_dir().glob("*.json"))
    return [{"path": p.relative_to(root).as_posix(), "sha256": compute_file_sha256(p)} for p in files]


def _validate_schema_ref(schema_ref: str, field_name: str, registry=None) -> None:
    """Validate schema reference is a non-remote, registered local schema."""
    if not schema_ref or not isinstance(schema_ref, str):
        raise ContractError(f"{field_name} must be a non-empty string", code="invalid_schema_ref", stage="validation")
    if schema_ref.startswith("http://") or schema_ref.startswith("https://"):
        raise ContractError(
            f"Remote schema references are forbidden for {field_name}: {schema_ref}",
            code="remote_schema_forbidden",
            stage="validation",
        )
    reg = registry if registry is not None else get_registry()
    try:
        resource = reg.resolver().lookup(schema_ref)
        if resource is None:
            raise ContractError(f"Unknown schema id for {field_name}: {schema_ref}", code="unknown_schema_id", stage="validation")
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError(f"Unknown schema id for {field_name}: {schema_ref}", code="unknown_schema_id", stage="validation") from exc


# @anchor:bv.contracts.load
def load(adapter_path, checklist_path, plan_path):
    """Validate a Blender configuration without executing source or scripts."""
    paths = [Path(p).resolve() for p in (adapter_path, checklist_path, plan_path)]
    adapter, checklist, plan = [load_json_file(p) for p in paths]
    for data, stem in zip((adapter, checklist, plan), ("adapter", "checklist", "capture-plan")):
        validate_schema(data, stem)
    for data in (checklist, plan):
        if data["adapter_id"] != adapter["adapter_id"] or data["ruleset_id"] != adapter["ruleset_id"]:
            raise ContractError("configuration identity mismatch", "E_SCHEMA", "validation")
    rel = adapter["blend_relpath"]
    _validate_path_traversal(rel, "blend_relpath")
    source_root = Path(adapter["source"]["root"])
    if not source_root.is_absolute():
        raise ContractError("source root must be absolute", "E_SCHEMA", "validation")
    root = source_root.resolve()
    blend = (root / rel).resolve()
    if not blend.is_relative_to(root) or not blend.is_file():
        raise ContractError("blend escapes source or is absent", "E_IO", "validation")
    if compute_file_sha256(blend) != adapter["blend_sha256"]:
        raise ContractError("blend content differs from declared hash", "E_STALE", "validation")
    checks = checklist["checks"]
    if not checks or len({row["id"] for row in checks}) != len(checks):
        raise ContractError("empty or duplicate checks", "E_SCHEMA", "validation")
    inputs = [{"path": name, "sha256": compute_file_sha256(p)} for name, p in zip(("adapter.json", "checklist.json", "capture-plan.json"), paths)]
    payload = {"adapter": adapter, "checklist": checklist, "capture_plan": plan}
    digest = compute_sha256_bytes(canonical_json_bytes(payload))
    tools = _collect_tool_files()
    run_id = compute_sha256_bytes(canonical_json_bytes({"configuration_sha256": digest, "tools": tools}))
    return Config(adapter, checklist, plan, *paths, paths[0].parent, run_id, digest, inputs, tools)


def verify_run(run_dir: str | Path) -> RunBundle:
    """Verify run directory integrity and load artifacts."""
    r_dir = Path(run_dir).resolve()
    if not r_dir.is_dir():
        raise ContractError(f"Run directory not found: {r_dir}", code="run_dir_not_found", stage="integrity")

    # Reject symlinks leaving root
    for root, dirs, files in os.walk(r_dir, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            if p.is_symlink():
                resolved = p.resolve()
                try:
                    resolved.relative_to(r_dir)
                except ValueError as exc:
                    raise ContractError(
                        f"Symlink {p} escapes run directory to {resolved}",
                        code="path_escape",
                        stage="integrity",
                    ) from exc

    report_path = r_dir / "report.json"
    if not report_path.is_file():
        raise ContractError("report.json not found in run directory", code="report_not_found", stage="integrity")

    report = load_json_file(report_path)
    validate_schema(report, "report")

    receipt_path = r_dir / "receipt.json"
    if not receipt_path.is_file():
        raise ContractError("receipt.json not found in run directory", code="receipt_not_found", stage="integrity")

    receipt = load_json_file(receipt_path)
    validate_schema(receipt, "receipt")

    # Verify receipt run_id matches report run_id
    if receipt.get("run_id") != report.get("run_id"):
        raise ContractError(
            f"Receipt run_id mismatch: {receipt.get('run_id')} vs {report.get('run_id')}",
            code="run_id_mismatch",
            stage="integrity",
        )

    # Verify hash-cycle prevention: receipt.json must NOT be in report bindings
    report_bindings = report.get("bindings", [])
    for b in report_bindings:
        if b.get("path") in ("receipt.json", "./receipt.json"):
            raise ContractError(
                "Hash cycle violation: receipt.json must not be listed in report bindings",
                code="hash_cycle_violation",
                stage="integrity",
            )

    # Verify receipt binds report.json hash
    actual_report_sha = compute_file_sha256(report_path)
    expected_report_sha = receipt.get("report", {}).get("sha256")
    if actual_report_sha != expected_report_sha:
        raise ContractError(
            f"Receipt report sha256 mismatch: expected {expected_report_sha}, got {actual_report_sha}",
            code="receipt_sha_mismatch",
            stage="integrity",
        )

    if receipt["report"]["path"] != "report.json":
        raise ContractError("receipt must bind report.json", "receipt_binding", "integrity")
    for rows in [receipt["input_files"], receipt["artifact_files"], receipt["gate_refs"], report["bindings"]]:
        if len({r["path"] for r in rows}) != len(rows):
            raise ContractError("duplicate receipt binding", "duplicate_binding", "integrity")
        for row in rows:
            resolve_bound_file(r_dir, row)

    # Verify receipt artifact_files
    for art in receipt.get("artifact_files", []):
        a_rel = art.get("path")
        exp_sha = art.get("sha256")
        if a_rel and exp_sha:
            a_full = r_dir / a_rel
            if not a_full.is_file():
                raise ContractError(f"Receipt artifact missing: {a_rel}", code="missing_artifact", stage="integrity")
            act_sha = compute_file_sha256(a_full)
            if act_sha != exp_sha:
                raise ContractError(
                    f"Receipt artifact sha256 mismatch for {a_rel}: expected {exp_sha}, got {act_sha}",
                    code="artifact_sha_mismatch",
                    stage="integrity",
                )

    # Verify receipt input_files
    for inp in receipt.get("input_files", []):
        i_rel = inp.get("path")
        exp_sha = inp.get("sha256")
        if i_rel and exp_sha:
            cand = r_dir / i_rel
            if not cand.is_file():
                cand = r_dir / "config" / i_rel
            if not cand.is_file():
                raise ContractError(f"Receipt input missing: {i_rel}", code="missing_input", stage="integrity")
            act_sha = compute_file_sha256(cand)
            if act_sha != exp_sha:
                raise ContractError(
                    f"Receipt input sha256 mismatch for {i_rel}: expected {exp_sha}, got {act_sha}",
                    code="input_sha_mismatch",
                    stage="integrity",
                )

    # Check bindings sha256
    bindings = report.get("bindings", [])
    for b in bindings:
        b_rel = b.get("path")
        exp_sha = b.get("sha256")
        if b_rel and exp_sha:
            b_full = r_dir / b_rel
            if not b_full.is_file():
                raise ContractError(
                    f"Bound artifact file missing: {b_rel}",
                    code="missing_artifact",
                    stage="integrity",
                )
            act_sha = compute_file_sha256(b_full)
            if act_sha != exp_sha:
                raise ContractError(
                    f"Bound artifact sha256 mismatch for {b_rel}: expected {exp_sha}, got {act_sha}",
                    code="artifact_sha_mismatch",
                    stage="integrity",
                )

    expected_tools = _collect_tool_files()
    if receipt["tool_files"] != expected_tools:
        raise ContractError("receipt tool projection differs from identity-bound dependencies", "tool_binding", "integrity")
    bound_paths = {r["path"] for r in receipt["artifact_files"] + receipt["input_files"]}
    actual_paths = {str(p.relative_to(r_dir)) for p in r_dir.rglob("*") if p.is_file()
                    and p.name not in ("report.json", "receipt.json") and not p.name.startswith(".tmp.")}
    if actual_paths != bound_paths:
        raise ContractError("receipt does not enumerate exact saved assets", "receipt_coverage", "integrity")
    if report["scope"] == "offline-selftest":
        input_ref = {"path": "config/selftest-input.json", "sha256": receipt["configuration_sha256"]}
        input_path = resolve_bound_file(r_dir, input_ref)
        if receipt["input_files"] != [input_ref] or receipt["run_id"] != input_ref["sha256"]:
            raise ContractError("offline fixture identity mismatch", "run_id_mismatch", "integrity")
        if load_json_file(input_path) != {"mode": "offline", "tool_files": expected_tools}:
            raise ContractError("offline input projection mismatch", "tool_binding", "integrity")
        if receipt["gate_refs"]:
            raise ContractError("offline fixtures cannot claim render gate provenance", "scope_binding", "integrity")
        result_path = r_dir / "selftest_results.json"
        if "selftest_results.json" not in bound_paths:
            raise ContractError("saved selftest results missing", "missing_selftest_results", "integrity")
        result = load_json_file(result_path)
        validate_schema(result, "selftest-results")
        if result.get("mode") != "offline":
            raise ContractError("selftest scope mismatch", "scope_binding", "integrity")
        return RunBundle(r_dir, report, receipt, None, bindings)

    capture_manifest: dict[str, Any] | None = None
    for candidate in [r_dir / "capture" / "manifest.json", r_dir / "manifest.json"]:
        if candidate.is_file():
            capture_manifest = load_json_file(candidate)
            break
    if capture_manifest is None:
        raise ContractError("Capture manifest missing in run directory", code="missing_capture_manifest", stage="integrity")
    validate_schema(capture_manifest, "capture")

    verify_capture(capture_manifest, r_dir)
    for required in ("config/adapter.json", "config/checklist.json", "config/capture-plan.json"):
        if required not in bound_paths:
            raise ContractError(f"sealed input missing: {required}", "missing_input", "integrity")
    cfg = load(r_dir / "config/adapter.json", r_dir / "config/checklist.json", r_dir / "config/capture-plan.json")
    if cfg.configuration_sha256 != receipt["configuration_sha256"] or cfg.run_id != receipt["run_id"]:
        raise ContractError("configuration/run identity mismatch", "run_id_mismatch", "integrity")
    if capture_manifest["adapter_id"] != cfg.adapter["adapter_id"] or capture_manifest["ruleset_id"] != cfg.adapter["ruleset_id"]:
        raise ContractError("capture configuration mismatch", "capture_binding", "integrity")
    saved_judge = r_dir / "judge/attempts.json"
    if saved_judge.is_file():
        value = load_json_file(saved_judge)
        validate_normalized_judge(value.get("final_results"))
        core_stage_path = r_dir / "judge/core-stage.json"
        if core_stage_path.is_file():
            saved_stage = load_json_file(core_stage_path)
            validate_judge_stage(saved_stage)
            if saved_stage["results"] != value["final_results"] or saved_stage["accounting"] != value.get("accounting", {}):
                raise ContractError("judge stage correspondence mismatch", "invalid_judge_results", "integrity")
        for path in (r_dir / "judge").glob("**/response.json"):
            validate_schema(load_json_file(path), "judge-output")
        for path in (r_dir / "judge").glob("**/input.json"):
            validate_schema(load_json_file(path), "judge-input")

    if saved_judge.is_file():
        verify_judge_bindings(cfg, capture_manifest, value["final_results"])
    oracles_file = r_dir / "oracles" / "results.json"
    if oracles_file.is_file():
        verify_oracle_bindings(cfg, capture_manifest, load_json_file(oracles_file))

    return RunBundle(
        run_dir=r_dir,
        report=report,
        receipt=receipt,
        capture_manifest=capture_manifest,
        bindings=bindings,
    )


def _finite_tree(value):
    import math
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("Non-finite JSON number", "invalid_number")
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)


def resolve_bound_file(root: Path, reference: dict[str, str]) -> Path:
    rel = reference["path"]
    _validate_path_traversal(rel, "binding.path")
    candidate = root / rel
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError(f"Binding escapes root: {rel}", "path_escape", "integrity") from exc
    if not candidate.is_file():
        raise ContractError(f"Bound input/artifact missing: {rel}", "missing_artifact", "integrity")
    if compute_file_sha256(candidate) != reference["sha256"]:
        raise ContractError(f"Bound bytes changed: {rel}", "artifact_sha_mismatch", "integrity")
    return candidate


def _references(value):
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            yield {"path": value["path"], "sha256": value["sha256"]}
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def _owner_inputs(adapter, root):
    """Explicit pinned owner schemas extend a run-local registry only."""
    dependencies = {}
    for row in _references(adapter):
        resolve_bound_file(root, row)
        if row["path"] in ("adapter.json", "checklist.json", "capture-plan.json"):
            raise ContractError("dependency collides with configuration envelope", "input_collision")
        if row["path"] in dependencies and dependencies[row["path"]] != row["sha256"]:
            raise ContractError("conflicting dependency hashes", "input_collision")
        dependencies[row["path"]] = row["sha256"]
    reg = _create_registry()
    ext = adapter["extensions"]
    schema_rows = ext.get("schema_registry", [])
    if not isinstance(schema_rows, list):
        raise ContractError("schema_registry must be an explicit pinned list", "invalid_schema_registry")
    owner_ids = set()
    for row in schema_rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ContractError("schema_registry entries need path and SHA256", "invalid_schema_registry")
        schema = load_json_file(resolve_bound_file(root, row))
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id.startswith("urn:") or schema_id in owner_ids:
            raise ContractError("owner schema needs unique local id", "invalid_schema_registry")
        try:
            reg.resolver().lookup(schema_id)
        except Exception:
            pass
        else:
            raise ContractError("owner schema cannot replace package registry", "schema_collision")
        _reject_remote_refs(schema)
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
            reg = reg.with_resource(schema_id, Resource.from_contents(schema))
        except Exception as exc:
            raise ContractError(f"invalid owner schema: {exc}", "invalid_schema_registry") from exc
        owner_ids.add(schema_id)
    # Resolve every declared reference, including unused schema branches, without network retrieval.
    for row in schema_rows:
        schema = load_json_file(root / row["path"])
        def verify_refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    try:
                        reg.resolver(base_uri=schema["$id"]).lookup(value["$ref"])
                    except Exception as exc:
                        raise ContractError("unknown local owner schema reference", "unknown_schema_id") from exc
                for item in value.values():
                    verify_refs(item)
            elif isinstance(value, list):
                for item in value:
                    verify_refs(item)
        verify_refs(schema)
    # Runtime extension controls have an explicit contract; game policy requires a pinned owner schema.
    controls = {"driver_reference", "preflight", "probe_extension_schema", "schema_registry", "extension_schema", "mock_runner"}
    policy = {key: value for key, value in ext.items() if key not in controls}
    schema_id = ext.get("extension_schema")
    if policy and not schema_id:
        raise ContractError("undeclared adapter extensions", "unchecked_extensions")
    if schema_id:
        if schema_id not in owner_ids:
            raise ContractError("extension schema must be pinned by adapter", "unknown_schema_id")
        schema = reg.resolver().lookup(schema_id).contents
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ContractError("extension policy schema must be closed", "unchecked_extensions")
        try:
            jsonschema.Draft202012Validator(schema, registry=reg).validate(policy)
        except Exception as exc:
            raise ContractError(f"adapter extension validation: {exc}", "unchecked_extensions") from exc
    if "probe_extension_schema" in ext:
        schema = ext["probe_extension_schema"]
        if not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ContractError("telemetry extension schema must be closed", "invalid_extensions")
        _reject_remote_refs(schema)
        jsonschema.Draft202012Validator.check_schema(schema)
    ref = ext.get("driver_reference")
    if ref is not None:
        # Offline capture accepts an explicit input witness, but never qualifies render.
        if ext.get("mock_runner") is True and isinstance(ref, dict) and set(ref) == {"input_sha256"}:
            pass
        elif not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise ContractError("driver reference must be a pinned file", "invalid_driver_reference")
    if "mock_runner" in ext and not isinstance(ext["mock_runner"], bool):
        raise ContractError("mock_runner must be boolean", "invalid_extensions")
    return [{"path": key, "sha256": value} for key, value in sorted(dependencies.items())], reg


def _reject_remote_refs(value):
    if isinstance(value, dict):
        if "$ref" in value and not value["$ref"].startswith(("#", "urn:")):
            raise ContractError("remote schema reference forbidden", "remote_schema_forbidden")
        for item in value.values():
            _reject_remote_refs(item)
    elif isinstance(value, list):
        for item in value:
            _reject_remote_refs(item)


def stage_config(config: Config, run_dir: Path):
    import shutil
    directory = run_dir / "config"
    directory.mkdir(exist_ok=False)
    sources = {"adapter.json": config.adapter_path, "checklist.json": config.checklist_path,
               "capture-plan.json": config.plan_path}
    sources.update({row["path"]: config.adapter_dir / row["path"] for row in config.dependencies})
    bindings = []
    for rel, source in sorted(sources.items()):
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        expected = next(row["sha256"] for row in config.input_files if row["path"] == rel)
        if compute_file_sha256(target) != expected:
            raise ContractError(f"input changed during staging: {rel}", "input_race", "preflight")
        target.chmod(0o444)
        bindings.append({"path": "config/" + rel, "sha256": expected})
    staged = load(directory / "adapter.json", directory / "checklist.json", directory / "capture-plan.json")
    if staged.run_id != config.run_id:
        raise ContractError("staging changed identity", "run_id_mismatch", "preflight")
    return staged, bindings


def verify_capture(capture, root):
    validate_schema(capture, "capture")
    for reference in _references(capture):
        resolve_bound_file(root, reference)
    frame_ids = [f["evidence_id"] for f in capture["frames"]]
    clip_ids = [c["evidence_id"] for c in capture["clips"]]
    point_ids = [p["point_id"] for p in capture["points"]]
    for ids in (frame_ids, clip_ids, point_ids):
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate capture identity", "capture_binding", "integrity")
    telemetry_path = resolve_bound_file(root, capture["telemetry"])
    rows = []
    for line in telemetry_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = load_json_str(line)
            validate_schema(row, "probe")
            rows.append(row)
    by_render = {}
    for row in rows:
        if row["render_index"] in by_render:
            raise ContractError("duplicate telemetry render index", "capture_binding", "integrity")
        by_render[row["render_index"]] = row
    for frame in capture["frames"]:
        row = by_render.get(frame["render_index"])
        if row is None or any(frame[key] != row[key] for key in ("tick", "call_index", "sequence", "viewport_px")):
            raise ContractError("capture frame/telemetry correspondence mismatch", "capture_binding", "integrity")
    for point in capture["points"]:
        if point["complete"] and (not point["selected_frame_ids"] or point["clip_id"] is None):
            raise ContractError("complete point lacks PNG or MP4", "capture_binding", "integrity")
        if not set(point["selected_frame_ids"]).issubset(frame_ids) or (point.get("clip_id") is not None and point["clip_id"] not in clip_ids):
            raise ContractError("capture point references unknown media", "capture_binding", "integrity")


def validate_normalized_judge(results):
    if not isinstance(results, list):
        raise ContractError("normalized final judge results missing", "invalid_judge_results", "integrity")
    seen = set()
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("check_id"), str) or row.get("check_id") in seen or not row.get("check_id"):
            raise ContractError("duplicate/invalid normalized judge identity", "invalid_judge_results", "integrity")
        seen.add(row["check_id"])
        required = {"check_id", "verdict", "confidence", "reason", "observations"}
        allowed = required | {"evidence_ids", "point_id", "raw"}
        if not required.issubset(row) or not set(row).issubset(allowed):
            raise ContractError("malformed normalized judge fields", "invalid_judge_results", "integrity")
        if not isinstance(row["observations"], list) or any(not isinstance(o, dict) for o in row["observations"]):
            raise ContractError("invalid normalized observations", "invalid_judge_results", "integrity")
        if "evidence_ids" in row and (not isinstance(row["evidence_ids"], list) or any(not isinstance(e, str) or not e for e in row["evidence_ids"])):
            raise ContractError("invalid normalized evidence ids", "invalid_judge_results", "integrity")
        if row.get("verdict") not in ("PASS", "FAIL", "CANNOT_SEE") or not isinstance(row.get("reason"), str) or not row["reason"]:
            raise ContractError("invalid normalized judge result", "invalid_judge_results", "integrity")
        confidence = row.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ContractError("invalid normalized judge confidence", "invalid_judge_results", "integrity")
        # Raw provenance is passive and uses the closed transport result contract.
        result_schema = load_json_file(get_schemas_dir() / "judge-output.v1.schema.json")
        validator = jsonschema.Draft202012Validator({**result_schema["$defs"]["result"], "$defs": result_schema["$defs"]})
        validator_errors = list(validator.iter_errors({k: row[k] for k in required}))
        if "point_id" in row and (not isinstance(row["point_id"], str) or not row["point_id"]):
            validator_errors.append("invalid point id")
        if "raw" in row:
            raw = row["raw"]
            if not isinstance(raw, dict) or set(raw) != {"verdict", "confidence", "reason", "observations"}:
                raise ContractError("malformed raw judge provenance", "invalid_judge_results", "integrity")
            validator_errors.extend(validator.iter_errors(dict(raw, check_id=row["check_id"])))
        if validator_errors:
            raise ContractError("invalid typed judge provenance", "invalid_judge_results", "integrity")
        _finite_tree(row)


def fresh_output_path(path):
    candidate = Path(path).absolute()
    for parent in (candidate, *candidate.parents):
        if parent.is_symlink():
            raise ContractError("output path traverses a symlink", "path_escape", "preflight")
    return candidate.resolve()


def validate_judge_stage(stage):
    expected = {"schema", "attempts", "results", "accounting", "errors"}
    if not isinstance(stage, dict) or set(stage) != expected or stage["schema"] != "vqa.judge-stage/v1":
        raise ContractError("malformed judge stage", "invalid_judge_results", "integrity")
    if not isinstance(stage["attempts"], list) or not isinstance(stage["errors"], list) or not isinstance(stage["accounting"], dict):
        raise ContractError("malformed judge stage fields", "invalid_judge_results", "integrity")
    validate_normalized_judge(stage["results"])
    _finite_tree(stage)


def _check_evidence_window(check, capture, point_id=None):
    frames = {f["evidence_id"]: f for f in capture["frames"]}
    points = {p["point_id"]: p for p in capture["points"]}
    clips = {c["evidence_id"]: c for c in capture["clips"]}
    ids = check["point_ids"] if point_id is None else [point_id]
    selected = {eid for pid in ids if pid in points for eid in points[pid]["selected_frame_ids"]}
    return frames, points, clips, selected


def verify_oracle_bindings(config, capture, results):
    """Authenticate stage meaning against immutable configuration and capture clocks."""
    if not isinstance(results, list):
        raise ContractError("oracle results must be a list", "invalid_oracle_results", "integrity")
    checks = {c["id"]: c for c in config.checklist["checks"]}
    seen = set()
    for aggregate in results:
        validate_schema(aggregate, "oracle-result")
        for row in aggregate.get("instances", [aggregate]):
            validate_schema(row, "oracle-result")
            check = checks.get(row["check_id"])
            pid = row.get("point_id")
            key = (row["check_id"], None if row["oracle_id"] == "cards.variation" else pid)
            if check is None or check["deterministic"] is None or key in seen or (pid is not None and pid not in check["point_ids"]):
                raise ContractError("invalid/duplicate oracle instance", "invalid_oracle_results", "integrity")
            seen.add(key)
            det = check["deterministic"]
            if row["oracle_id"] != det["oracle_id"] or row["parameters"] != det["parameters"]:
                raise ContractError("oracle configuration mismatch", "invalid_oracle_results", "integrity")
            if pid is None and len(check["point_ids"]) > 1 and row["oracle_id"] != "cards.variation":
                raise ContractError("point-specific oracle instance missing", "invalid_oracle_results", "integrity")
            target = None if row["oracle_id"] == "cards.variation" else pid
            frames, points, clips, allowed = _check_evidence_window(check, capture, target)
            if row["oracle_id"] in ("frame.black", "frame.magenta", "frame.uniform"):
                windows = det.get("deterministic_windows")
                allowed = set(frames) if windows is None else {eid for eid, frame in frames.items()
                    if any(w["start_render_index"] <= frame["render_index"] <= w["end_render_index"] for w in windows)}
            if row["oracle_id"] == "frame.frozen":
                starts = [frames[e]["render_index"] for e in allowed]
                allowed = {eid for eid, f in frames.items() if starts and min(starts) <= f["render_index"] < min(starts) + det["parameters"].get("window_frames", 30)}
            evidence = row["evidence_ids"]
            if not set(evidence).issubset(allowed):
                raise ContractError("oracle evidence outside declared point/window", "oracle_binding", "integrity")
            if row["verdict"] in ("PASS", "FAIL") and (not row["applicable"] or not row["oracle_executed"] or not evidence):
                raise ContractError("semantic oracle verdict lacks executed evidence", "oracle_binding", "integrity")
            if evidence:
                if not any(frames[e]["tick"] == row["tick"] and frames[e]["render_index"] == row["render_index"] for e in evidence):
                    raise ContractError("oracle authoritative clock mismatch", "oracle_binding", "integrity")
            elif row["tick"] is not None or row["render_index"] is not None:
                raise ContractError("oracle clocks have no evidence", "oracle_binding", "integrity")


def verify_judge_bindings(config, capture, results):
    validate_normalized_judge(results)
    checks = {c["id"]: c for c in config.checklist["checks"]}
    for row in results:
        check = checks.get(row["check_id"])
        if check is None or check["judge"] is None or (row.get("point_id") is not None and row["point_id"] not in check["point_ids"]):
            raise ContractError("unknown/unrequested judge instance", "invalid_judge_results", "integrity")
        frames, points, clips, selected = _check_evidence_window(check, capture)
        allowed = selected | {points[p]["clip_id"] for p in check["point_ids"] if p in points and points[p]["clip_id"]}
        evidence = row.get("evidence_ids", [])
        # S3 assigns transport media ids; authenticate their source projection,
        # staged bytes and clocks rather than treating them as capture ids.
        run_root = config.adapter_path.parent.parent
        projected = set()
        for input_path in sorted((run_root / "judge").glob("**/input.json")):
            transport = load_json_file(input_path)
            validate_schema(transport, "judge-input")
            requested = next((c for c in transport["checks"] if c["check_id"] == row["check_id"]), None)
            if requested is None:
                continue
            for media in transport["media"]:
                if media["evidence_id"] not in requested["evidence_ids"]:
                    continue
                source_ids = media["source_evidence_ids"]
                if media["role"] == "attention" or not source_ids or not set(source_ids).issubset(allowed):
                    raise ContractError("judge media source outside declared capture", "judge_binding", "integrity")
                resolve_bound_file(run_root, {"path": media["path"], "sha256": media["sha256"]})
                starts = [frames[e]["tick"] if e in frames else clips[e]["tick_start"] for e in source_ids]
                ends = [frames[e]["tick"] if e in frames else clips[e]["tick_end"] for e in source_ids]
                if media["tick_start"] != min(starts) or media["tick_end"] != max(ends):
                    raise ContractError("judge media clock mismatch", "judge_binding", "integrity")
                if media["kind"] == "png" and media["role"] == "render" and len(source_ids) == 1 and source_ids[0] in frames:
                    if media["sha256"] != frames[source_ids[0]]["file"]["sha256"]:
                        raise ContractError("judge frame source bytes mismatch", "judge_binding", "integrity")
                source_frames = set(e for e in source_ids if e in frames)
                source_frames.update(e for source in source_ids if source in clips for e in clips[source]["frame_ids"])
                for clock in media.get("source_map", {}).values():
                    if not any(all(clock[k] == frames[e][k] for k in ("tick", "render_index")) for e in source_frames if e in frames):
                        raise ContractError("judge source clock mismatch", "judge_binding", "integrity")
                projected.add(media["evidence_id"])
        allowed |= projected
        observations = row["observations"] + row.get("raw", {}).get("observations", [])
        if not set(evidence).issubset(allowed) or any(o["evidence_id"] not in allowed for o in observations):
            raise ContractError("judge evidence outside declared capture", "judge_binding", "integrity")
        if row["verdict"] in ("PASS", "FAIL") and not evidence:
            raise ContractError("accepted judge verdict lacks captured evidence", "judge_binding", "integrity")


def capture_recording_complete(capture):
    """A native semantic failure does not imply missing recording coverage."""
    return capture.get("complete") is True or bool(
        capture.get("scope") == "render" and capture.get("drive_results")
        and capture.get("points") and all(p["complete"] for p in capture["points"])
        and capture.get("preflight", {}).get("passed") is True
        and capture.get("driver_reference") and not capture.get("errors")
        and not capture.get("incomplete"))
