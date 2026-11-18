"""Candidate identity and contract checks for the offline qualification runner."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from asset_factory import core

CANDIDATE_SCHEMA = "af.qualification-candidate/v1"
RUNTIME_SCHEMA = "af.qualification-python-runtime/v1"
RIGHTS_SCHEMA = "af.qualification-rights/v1"
JSON_LIMIT = 1024 * 1024
PROVIDER_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./-]{0,127}")
OUTPUT_NAME = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


_COMPONENT_ROOT = Path(__file__).resolve().parents[2]
_PIN_ROOTS = {"AF_MODELS_ROOT", "AF_CAMPAIGN_ROOT", "AF_RUNTIME_ROOT", "AF_CONSTRUCT_SOURCE_ROOT"}


class Refusal(ValueError):
    def __init__(self, rc, code):
        super().__init__(code)
        self.rc = rc
        self.code = code


def pin_path(name):
    """Resolve a pin without changing the path string that its subject hashes."""
    if type(name) is not str or not name or "\0" in name or ".." in Path(name).parts:
        raise Refusal(2, "missing_or_invalid_file_pin")
    path = Path(name)
    if path.is_absolute():
        return path
    root = _COMPONENT_ROOT
    relative = name
    if name.startswith("$"):
        prefix, separator, relative = name.partition("/")
        if prefix[1:] not in _PIN_ROOTS or not separator or not relative or Path(relative).is_absolute():
            raise Refusal(2, "missing_or_invalid_file_pin")
        root = Path(os.environ.get(prefix[1:], ""))
        if not root.is_absolute():
            raise Refusal(3, "pin_unavailable")

    try:
        root = root.resolve()
        path = root / relative
        resolved = path.resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise Refusal(3, "pin_unavailable") from exc
    if not resolved.is_relative_to(root):
        raise Refusal(2, "missing_or_invalid_file_pin")
    return path


def fields(value, names, code, *, optional=()):
    required = set(names.split())
    if type(value) is not dict or not required <= value.keys() <= required | set(optional):
        raise Refusal(2, code)


def stamp(path):
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise Refusal(2, "pin_not_regular_file")
    return resolved, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Pins:
    def __init__(self):
        self.files = {}

    def check(self, descriptor, *, json_content=False):
        fields(descriptor, "path sha256", "missing_or_invalid_file_pin")
        name, digest = descriptor["path"], descriptor["sha256"]
        if type(name) is not str or type(digest) is not str or SHA256.fullmatch(digest) is None:
            raise Refusal(2, "missing_or_invalid_file_pin")
        path = pin_path(name)
        try:
            before = stamp(path)
            resolved = before[0]
            stored = self.files.get(path)
            if stored is not None and stored != (digest, before):
                raise Refusal(4, "pin_digest_mismatch")
            if stored is None or json_content:
                actual = core.sha256_file(resolved)
                if actual != digest or stamp(path) != before:
                    raise Refusal(4, "pin_digest_mismatch")
                if json_content:
                    try:
                        core.load_json(resolved, max_bytes=JSON_LIMIT)
                    except core.ContractError as exc:
                        raise Refusal(exc.rc, exc.code) from exc
                self.files[path] = (digest, before)
        except Refusal:
            raise
        except (OSError, ValueError, RuntimeError, core.ContractError) as exc:
            raise Refusal(3, "pin_unavailable") from exc
        return before[0]

    def check_list(self, descriptors, *, required=False):
        if type(descriptors) is not list or (required and not descriptors):
            raise Refusal(2, "missing_or_invalid_file_pins")
        for descriptor in descriptors:
            self.check(descriptor)

    def verify_unchanged(self):
        for path, (_, before) in self.files.items():
            try:
                after = stamp(path)
            except (OSError, ValueError, RuntimeError) as exc:
                raise Refusal(4, "pin_changed_during_run") from exc
            if after != before:
                raise Refusal(4, "pin_changed_during_run")


def check_components(pins, items):
    if type(items) is not list:
        raise Refusal(2, "missing_component_inventory")
    names = set()
    for item in items:
        fields(item, "name files", "invalid_component_pin", optional=("licence",))
        name = item["name"]
        if type(name) is not str or not name.strip() or name in names:
            raise Refusal(2, "invalid_component_pin")
        names.add(name)
        pins.check_list(item["files"], required=True)


def runtime_import_roots(runtime, pins):
    lock_path = pins.check(runtime["lock"], json_content=True)
    value = core.load_json(lock_path, max_bytes=JSON_LIMIT)
    fields(value, "schema interpreter_sha256 files import_roots", "invalid_runtime_lock")
    if (value["schema"] != RUNTIME_SCHEMA or
            value["interpreter_sha256"] != runtime["interpreter"]["sha256"] or
            value["files"] != runtime["files"]):
        raise Refusal(4, "runtime_lock_identity_mismatch")
    roots = value["import_roots"]
    if type(roots) is not list or any(type(root) is not str for root in roots):
        raise Refusal(2, "invalid_runtime_lock")
    runtime_files = [pin_path(item["path"]) for item in runtime["files"]]
    try:
        resolved_roots = [pin_path(root) for root in roots]
    except Refusal as exc:
        raise Refusal(2, "invalid_runtime_import_roots") from exc
    if len(set(resolved_roots)) != len(roots) or any(
        name != str(Path(name)) or not any(root in file.parents for file in runtime_files)
        for name, root in zip(roots, resolved_roots)
    ):
        raise Refusal(2, "invalid_runtime_import_roots")
    return [str(root) for root in resolved_roots]

def check_legacy_subject(candidate, rights):
    """Authenticate supplied historical evidence without consulting its verdict."""
    fields(rights, "schema provider subject_sha256", "invalid_rights_decision",
           optional=("jurisdiction", "commercial_use", "verdict", "licence", "licence_url", "declaration"))
    subject = {key: value for key, value in candidate.items() if key != "rights"}
    if (rights["schema"] != RIGHTS_SCHEMA or rights["provider"] != candidate["provider"] or
            rights["subject_sha256"] != core.sha256_json(subject)):
        raise Refusal(4, "rights_subject_mismatch")


def check_candidate(value, pins):
    if type(value) is not dict:
        raise Refusal(2, "candidate_identity_required")
    provider = value.get("provider")
    if type(provider) is not str:
        raise Refusal(2, "candidate_identity_required")
    fields(value, "schema provider code weights conditioners dependencies runtime request "
                  "input_schema output_schema outputs", "candidate_identity_required",
           optional=("rights", "licence_metadata"))
    if value["schema"] != CANDIDATE_SCHEMA or PROVIDER_ID.fullmatch(provider) is None:
        raise Refusal(2, "candidate_identity_required")
    code = value["code"]
    fields(code, "revision entrypoint files", "code_identity_required")
    if type(code["revision"]) is not str or REVISION.fullmatch(code["revision"]) is None:
        raise Refusal(2, "code_identity_required")
    runtime = value["runtime"]
    if type(runtime) is not dict or "lock" not in runtime:
        raise Refusal(2, "runtime_lock_required")
    fields(runtime, "interpreter files lock cuda", "runtime_identity_required")
    if type(runtime["cuda"]) is not bool:
        raise Refusal(2, "runtime_identity_required")
    outputs = value["outputs"]
    if (type(outputs) is not list or
            any(type(name) is not str or OUTPUT_NAME.fullmatch(name) is None for name in outputs) or
            len(set(outputs)) != len(outputs) or "result.json" not in outputs or
            ("cuda-peaks.json" in outputs) != runtime["cuda"]):
        raise Refusal(2, "output_inventory_required")

    if "rights" in value:
        rights = core.load_json(pins.check(value["rights"], json_content=True), max_bytes=JSON_LIMIT)
        check_legacy_subject(value, rights)

    entrypoint = pins.check(code["entrypoint"])
    interpreter = pins.check(runtime["interpreter"])
    pins.check_list(code["files"])
    pins.check_list(runtime["files"], required=True)
    import_roots = runtime_import_roots(runtime, pins)
    pins.check_list(value["weights"], required=True)
    check_components(pins, value["conditioners"])
    check_components(pins, value["dependencies"])
    request = core.load_json(pins.check(value["request"], json_content=True), max_bytes=JSON_LIMIT)
    input_schema = core.load_json(pins.check(value["input_schema"], json_content=True), max_bytes=JSON_LIMIT)
    output_schema = core.load_json(pins.check(value["output_schema"], json_content=True), max_bytes=JSON_LIMIT)
    core.validate(request, input_schema)
    return interpreter, entrypoint, output_schema, import_roots
