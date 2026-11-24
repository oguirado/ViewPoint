"""Closed H1-ENTRY contracts and verification of the isolated bundle."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import types

BUNDLE = Path(__file__).resolve().parent.parent
SCHEMA_DIR = "mc05-contract-candidate/contracts/v0.2/"
SCHEMA_PINS = {
    "headless_request.schema.json": "619ab2243965f876c3a15e276f30eba12617df8716a2d0fdea0e5ee5f1189965",
    "headless_event.schema.json": "0e1ee82215d970d198e514fcf8dafadc68cca20f90e89253965ccd20ac9af609",
    "headless_session.schema.json": "fa626cf78172c3f857814c6bfdd63db44624fe1d0ce722dcdf3cc663263128b9",
}
GEN_MAX = (1 << 53) - 1
REQUEST_ID = re.compile(r"request-[a-z0-9][a-z0-9._-]{0,63}")
OWNER_ID = re.compile(r"actor-[a-z0-9][a-z0-9._-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")


class Invalid(ValueError):
    pass


def closed(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise Invalid("closed object required")


def bounded_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Invalid("integer bound")


def match(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def absolute(value):
    if type(value) is not str or "\0" in value or not value.startswith("/"):
        raise Invalid("canonical absolute path required")
    path = Path(value)
    if str(path) != value or ".." in path.parts or value.startswith("//") or path == Path("/"):
        raise Invalid("canonical absolute path required")
    return path


def private_directory(value):
    path = absolute(value)
    st = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700):
        raise Invalid("owner-private directory required")
    return path


def validate_config(config, state_dir):
    closed(config, ("schema", "owner_id", "generation", "runtime_admitted",
                    "artifacts", "resources", "deadlines", "runtime_root", "external_cgroup"))
    if (config["schema"] != "mc06-host-config/v1" or not match(OWNER_ID, config["owner_id"])
            or type(config["runtime_admitted"]) is not bool):
        raise Invalid("host config schema, owner or admission")
    bounded_int(config["generation"], 1, GEN_MAX)
    resources = config["resources"]
    closed(resources, ("mem_size_mib", "vcpu_count"))
    bounded_int(resources["mem_size_mib"], 256, 256)
    bounded_int(resources["vcpu_count"], 1, 1)
    closed(config["deadlines"], ("startup_seconds", "job_seconds", "stop_seconds"))
    for seconds in config["deadlines"].values():
        bounded_int(seconds, 1, 30)
    closed(config["artifacts"], ("engine", "kernel", "init"))
    for artifact in config["artifacts"].values():
        closed(artifact, ("path", "sha256"))
        absolute(artifact["path"])
        if not match(SHA256, artifact["sha256"]):
            raise Invalid("artifact SHA-256 required")
    state = private_directory(str(state_dir))
    runtime = private_directory(config["runtime_root"])
    absolute(config["external_cgroup"])
    if state == runtime or state in runtime.parents or runtime in state.parents:
        raise Invalid("state and runtime directories must be disjoint")
    return state


def source_bytes(relative, digest):
    path = BUNDLE / relative
    if path.resolve(strict=True) != path:
        raise Invalid("bundle source alias")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as file:
        st = os.fstat(file.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, os.getuid())
                or st.st_mode & 0o022 or st.st_nlink != 1 or st.st_size > (2 << 20)):
            raise Invalid("bundle source custody")
        raw = file.read((2 << 20) + 1)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise Invalid("bundle source pin changed")
    return raw


def pinned_module(name, relative, digest):
    raw = source_bytes(relative, digest)
    path = str(BUNDLE / relative)
    module = types.ModuleType(name)
    module.__file__ = path
    exec(compile(raw, path, "exec"), module.__dict__)
    return module


class Contracts:
    """Validate the keywords used by the exact pinned native schema documents."""

    def __init__(self):
        self.schemas = {
            name: json.loads(source_bytes(SCHEMA_DIR + name, digest))
            for name, digest in SCHEMA_PINS.items()
        }

    def validate(self, kind, value):
        document = self.schemas[kind + ".schema.json"]
        self._check(document, value, document)

    def _check(self, schema, value, document):
        if "$ref" in schema:
            filename, _, fragment = schema["$ref"].partition("#")
            document = self.schemas[filename] if filename else document
            target = document
            for part in fragment.split("/")[1:]:
                target = target[part]
            self._check(target, value, document)
            return
        if "oneOf" in schema:
            matches = 0
            for variant in schema["oneOf"]:
                try:
                    self._check(variant, value, document)
                except Invalid:
                    continue
                matches += 1
            if matches != 1:
                raise Invalid("schema oneOf")
        if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
            raise Invalid("schema const")
        if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
            raise Invalid("schema enum")
        kind = schema.get("type")
        if kind == "object":
            if type(value) is not dict:
                raise Invalid("schema object")
            properties = schema["properties"]
            if schema["additionalProperties"] is False and set(value) - properties.keys():
                raise Invalid("unknown property")
            if not set(schema["required"]) <= value.keys() or len(value) > schema["maxProperties"]:
                raise Invalid("schema properties")
            for key, field in value.items():
                self._check(properties[key], field, document)
        elif kind == "integer":
            bounded_int(value, schema["minimum"], schema["maximum"])
        elif kind == "string":
            if type(value) is not str or not schema.get("minLength", 0) <= len(value) <= schema["maxLength"]:
                raise Invalid("schema string")
            if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
                raise Invalid("schema pattern")
        elif kind == "null":
            if value is not None:
                raise Invalid("schema null")
        elif kind is not None:
            raise Invalid("unsupported schema type")
