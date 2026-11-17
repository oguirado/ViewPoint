"""Candidate identity and contract checks for the offline qualification runner."""

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


# Hunyuan3D-2.1 weight identities from PRE_INVENTORY.json.
EXCLUDED_SHA256 = frozenset({
    "6b519fc7242f78e9b5f47ea4d55668fe3d944a2d27332f4ca68d29a6ff603f5e",
    "ae616c24393dd1854372b0639e5541666f7521cbe219669255e865cb7f89466a",
    "c3e254d7b61353497ea0be2c4013df4ea8f739ee88cffa0ba58cd085459ed565",
    "675a1b5cd0098b2002637c443946529c03c5cd54427f40245263350feb3dd5b8",
    "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc",
    "5cbe97f25e6e7abd4bccc80ab07524ec0c86d24118486a9ba49bb5dfb070288a",
})


class Refusal(ValueError):
    def __init__(self, rc, code):
        super().__init__(code)
        self.rc = rc
        self.code = code


def fields(value, names, code):
    if type(value) is not dict or set(value) != set(names.split()):
        raise Refusal(2, code)


def check_exclusion(value):
    slug = re.sub(r"[^a-z0-9]", "", value.casefold())
    if "hunyuan" in slug or "hy3d" in slug:
        raise Refusal(5, "provider_excluded_eu")


def stamp(path):
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise Refusal(2, "pin_not_regular_file")
    return resolved, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class Pins:
    def __init__(self):
        self.files = {}

    def check(self, descriptor):
        fields(descriptor, "path sha256", "missing_or_invalid_file_pin")
        name, digest = descriptor["path"], descriptor["sha256"]
        if (type(name) is not str or not Path(name).is_absolute() or ".." in Path(name).parts or
                type(digest) is not str or SHA256.fullmatch(digest) is None):
            raise Refusal(2, "missing_or_invalid_file_pin")
        check_exclusion(name)
        if digest in EXCLUDED_SHA256:
            raise Refusal(5, "provider_excluded_eu")
        path = Path(name)
        try:
            before = stamp(path)
            check_exclusion(str(before[0]))
            stored = self.files.get(path)
            if stored is None:
                actual = core.sha256_file(before[0])
                if actual in EXCLUDED_SHA256:
                    raise Refusal(5, "provider_excluded_eu")
                if actual != digest or stamp(path) != before:
                    raise Refusal(4, "pin_digest_mismatch")
                self.files[path] = (digest, before)
            elif stored != (digest, before):
                raise Refusal(4, "pin_digest_mismatch")
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


def check_components(pins, items, *, licences):
    if type(items) is not list:
        raise Refusal(2, "missing_component_inventory")
    names = set()
    for item in items:
        fields(item, "name files licence" if licences else "name files", "invalid_component_pin")
        name = item["name"]
        if type(name) is not str or not name.strip() or name in names:
            raise Refusal(2, "invalid_component_pin")
        check_exclusion(name)
        names.add(name)
        if licences:
            pins.check(item["licence"])
        pins.check_list(item["files"], required=True)


def runtime_import_roots(runtime, pins):
    lock_path = pins.check(runtime["lock"])
    value = core.load_json(lock_path, max_bytes=JSON_LIMIT)
    fields(value, "schema interpreter_sha256 files import_roots", "invalid_runtime_lock")
    if (value["schema"] != RUNTIME_SCHEMA or
            value["interpreter_sha256"] != runtime["interpreter"]["sha256"] or
            value["files"] != runtime["files"]):
        raise Refusal(4, "runtime_lock_identity_mismatch")
    roots = value["import_roots"]
    if type(roots) is not list or any(type(root) is not str for root in roots):
        raise Refusal(2, "invalid_runtime_lock")
    runtime_files = [Path(item["path"]) for item in runtime["files"]]
    if len(set(roots)) != len(roots) or any(
        not Path(root).is_absolute() or root != str(Path(root)) or ".." in Path(root).parts or
        not any(Path(root) in file.parents for file in runtime_files)
        for root in roots
    ):
        raise Refusal(2, "invalid_runtime_import_roots")
    return roots

def check_candidate(value, pins):
    if type(value) is not dict:
        raise Refusal(2, "candidate_identity_required")
    provider = value.get("provider")
    if type(provider) is not str:
        raise Refusal(2, "candidate_identity_required")
    # Check the canonical family before reading any candidate files or rights.
    check_exclusion(provider)
    fields(value, "schema provider code weights conditioners dependencies runtime rights request "
                  "input_schema output_schema outputs", "candidate_identity_required")
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

    rights = core.load_json(pins.check(value["rights"]), max_bytes=JSON_LIMIT)
    fields(rights, "schema provider jurisdiction commercial_use verdict subject_sha256",
           "invalid_rights_decision")
    subject = {key: val for key, val in value.items() if key != "rights"}
    if (rights["schema"] != RIGHTS_SCHEMA or rights["provider"] != provider or
            rights["subject_sha256"] != core.sha256_json(subject)):
        raise Refusal(4, "rights_subject_mismatch")
    if (rights["jurisdiction"] != "EU" or rights["commercial_use"] is not True or
            rights["verdict"] != "PASS"):
        raise Refusal(5, "commercial_eu_rights_unproven")

    entrypoint = pins.check(code["entrypoint"])
    interpreter = pins.check(runtime["interpreter"])
    pins.check_list(code["files"])
    pins.check_list(runtime["files"], required=True)
    import_roots = runtime_import_roots(runtime, pins)
    pins.check_list(value["weights"], required=True)
    check_components(pins, value["conditioners"], licences=False)
    check_components(pins, value["dependencies"], licences=True)
    request = core.load_json(pins.check(value["request"]), max_bytes=JSON_LIMIT)
    input_schema = core.load_json(pins.check(value["input_schema"]), max_bytes=JSON_LIMIT)
    output_schema = core.load_json(pins.check(value["output_schema"]), max_bytes=JSON_LIMIT)
    core.validate(request, input_schema)
    return interpreter, entrypoint, output_schema, import_roots
