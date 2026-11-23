"""Shared F0 kernel. Stage completion and asset acceptance are separate."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from collections.abc import Iterable

RC = (0, 2, 3, 4, 5, 6)
STATUS = {0: "OK", 2: "ERROR", 3: "INCOMPLETE", 4: "STALE", 5: "FAIL", 6: "BUDGET_EXCEEDED"}
VERDICT = {0: "PASS", 2: "ERROR", 3: "CANNOT_SEE", 4: "CANNOT_SEE", 5: "FAIL", 6: "CANNOT_SEE"}
SLOTS = ("generate", "refine", "topology", "uv_texture", "parts", "rig", "motion", "render")

# DESIGN_CHOICE: bound recursive JSON/schema work before Python's recursion limit.
_MAX_DEPTH = 128
_KEYWORDS = frozenset(("$schema", "$id", "$ref", "$defs", "type", "const", "enum",
                       "required", "properties", "additionalProperties", "items", "minItems", "oneOf"))
_TYPES = {"null": type(None), "boolean": bool, "integer": int, "string": str,
          "array": list, "object": dict}


class ContractError(Exception):
    rc = 2

    def __init__(self, code: str, path: str = "$"):
        self.code = code
        # Rejected input must not make its own refusal impossible to serialize.
        self.path = path.encode("utf-8", errors="backslashreplace").decode("utf-8")
        super().__init__(code + ": " + self.path)

    def as_dict(self) -> dict:
        return {"rc": 2, "status": "ERROR", "reason_codes": [self.code], "path": self.path}


def _pointer(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _check_utf8(value, path):
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ContractError("schema_mismatch", path) from exc


def _check_json(obj, path="$", active=None, depth=0):
    if depth > _MAX_DEPTH:
        raise ContractError("schema_mismatch", path)
    if obj is None or type(obj) in (str, int, bool):
        if type(obj) is str:
            _check_utf8(obj, path)
        return
    if type(obj) is float:
        raise ContractError("non_integer_number", path)
    if type(obj) not in (dict, list):
        raise ContractError("schema_mismatch", path)
    active = set() if active is None else active
    if id(obj) in active:
        raise ContractError("schema_mismatch", path)
    active.add(id(obj))
    try:
        pairs = obj.items() if type(obj) is dict else enumerate(obj)
        for key, value in pairs:
            if type(obj) is dict:
                if type(key) is not str:
                    raise ContractError("schema_mismatch", path)
                _check_utf8(key, _pointer(path, key))
            _check_json(value, _pointer(path, key), active, depth + 1)
    finally:
        active.remove(id(obj))


def canonical_json(obj: object) -> str:
    _check_json(obj)
    try:
        result = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False) + "\n"
        result.encode("utf-8")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ContractError("schema_mismatch") from exc


def sha256_json(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError) as exc:
        raise ContractError("schema_mismatch", str(path)) from exc
    return digest.hexdigest()


def _reject_number(_value):
    raise ContractError("non_integer_number")


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        _check_utf8(key, _pointer("$", key))
        if key in obj:
            raise ContractError("schema_mismatch", _pointer("$", key))
        obj[key] = value
    return obj


def load_json(path: Path, *, max_bytes: int) -> object:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ContractError("schema_mismatch", str(path))
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ContractError("schema_mismatch", str(path))
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_float=_reject_number, parse_constant=_reject_number)
        _check_json(obj)
        return obj
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("schema_mismatch", str(path)) from exc


def _resolve_ref(root, ref):
    if type(ref) is not str or not (ref == "#" or ref.startswith("#/")):
        raise ContractError("schema_mismatch", "$ref")
    node = root
    if ref == "#":
        return node
    for part in ref[2:].split("/"):
        # Only JSON Pointer escaping is accepted; no URI/network resolution.
        if "~" in part.replace("~0", "").replace("~1", ""):
            raise ContractError("schema_mismatch", ref)
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            if type(node) is list:
                if not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                    raise KeyError(key)
                node = node[int(key)]
            else:
                node = node[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ContractError("schema_mismatch", ref) from exc
    if type(node) is not dict:
        raise ContractError("schema_mismatch", ref)
    return node


def _preflight(node, root, seen, depth=0):
    if depth > _MAX_DEPTH or type(node) is not dict:
        raise ContractError("schema_mismatch", "$schema")
    if id(node) in seen:
        return
    seen.add(id(node))
    if set(node) - _KEYWORDS:
        raise ContractError("schema_mismatch", "$schema")
    for key in ("$schema", "$id"):
        if key in node and (type(node[key]) is not str or not node[key]):
            raise ContractError("schema_mismatch", key)
    if "$ref" in node:
        _preflight(_resolve_ref(root, node["$ref"]), root, seen, depth + 1)
    if "type" in node:
        types = node["type"] if type(node["type"]) is list else [node["type"]]
        if not types or any(type(t) is not str or t not in _TYPES for t in types):
            raise ContractError("schema_mismatch", "$schema/type")
        if len(set(types)) != len(types):
            raise ContractError("schema_mismatch", "$schema/type")
    for key in ("properties", "$defs"):
        if key in node:
            if type(node[key]) is not dict:
                raise ContractError("schema_mismatch", "$schema/" + key)
            for child in node[key].values():
                _preflight(child, root, seen, depth + 1)
    if "required" in node:
        required = node["required"]
        if (type(required) is not list or any(type(k) is not str for k in required)
                or len(set(required)) != len(required)):
            raise ContractError("schema_mismatch", "$schema/required")
    if "enum" in node:
        values = node["enum"]
        if (type(values) is not list or not values
                or len({canonical_json(v) for v in values}) != len(values)):
            raise ContractError("schema_mismatch", "$schema/enum")
    if "minItems" in node and (type(node["minItems"]) is not int or node["minItems"] < 0):
        raise ContractError("schema_mismatch", "$schema/minItems")
    if "additionalProperties" in node:
        ap = node["additionalProperties"]
        if type(ap) is not bool:
            _preflight(ap, root, seen, depth + 1)
    if "items" in node:
        _preflight(node["items"], root, seen, depth + 1)
    if "oneOf" in node:
        if type(node["oneOf"]) is not list or not node["oneOf"]:
            raise ContractError("schema_mismatch", "$schema/oneOf")
        for child in node["oneOf"]:
            _preflight(child, root, seen, depth + 1)


def _matches(value, node, root, path, active, depth):
    pair = (id(value), id(node))
    if depth > _MAX_DEPTH or pair in active:
        raise ContractError("schema_mismatch", path)
    active.add(pair)
    try:
        if "$ref" in node:
            _matches(value, _resolve_ref(root, node["$ref"]), root, path, active, depth + 1)
        if "type" in node:
            types = node["type"] if type(node["type"]) is list else [node["type"]]
            if not any(type(value) is _TYPES[t] for t in types):
                code = "non_integer_number" if type(value) is bool and "integer" in types else "schema_mismatch"
                raise ContractError(code, path)
        if "const" in node and canonical_json(value) != canonical_json(node["const"]):
            raise ContractError("schema_mismatch", path)
        if "enum" in node and not any(canonical_json(value) == canonical_json(v) for v in node["enum"]):
            raise ContractError("schema_mismatch", path)
        if "oneOf" in node:
            matches = 0
            for child in node["oneOf"]:
                try:
                    _matches(value, child, root, path, active, depth + 1)
                except ContractError:
                    continue
                matches += 1
            if matches != 1:
                raise ContractError("schema_mismatch", path)
        if type(value) is dict:
            for key in node.get("required", []):
                if key not in value:
                    raise ContractError("missing_required", _pointer(path, key))
            properties = node.get("properties", {})
            for key, child in value.items():
                if key in properties:
                    _matches(child, properties[key], root, _pointer(path, key), active, depth + 1)
                else:
                    extra = node.get("additionalProperties", True)
                    if extra is False:
                        raise ContractError("unknown_property", _pointer(path, key))
                    if type(extra) is dict:
                        _matches(child, extra, root, _pointer(path, key), active, depth + 1)
        if type(value) is list:
            if len(value) < node.get("minItems", 0):
                raise ContractError("schema_mismatch", path)
            if "items" in node:
                for index, child in enumerate(value):
                    _matches(child, node["items"], root, _pointer(path, index), active, depth + 1)
    finally:
        active.remove(pair)


def validate(payload: object, schema: dict) -> None:
    _check_json(schema)
    _preflight(schema, schema, set())
    _check_json(payload)
    _matches(payload, schema, schema, "$", set(), 0)


def worst_rc(rcs: Iterable[int]) -> int:
    values = list(rcs)
    if any(type(rc) is not int or rc not in RC for rc in values):
        return 2
    if 2 in values:
        return 2
    if 5 in values:
        return 5
    if not values or any(rc in (3, 4, 6) for rc in values):
        return 3
    return 0


def atomic_write_json(target: Path, obj: object) -> None:
    content = canonical_json(obj).encode("utf-8")
    target = Path(target)
    staged = None
    try:
        fd, name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
        staged = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
    except (OSError, ValueError) as exc:
        raise ContractError("schema_mismatch", str(target)) from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def emit(obj: dict) -> int:
    if type(obj) is not dict or type(obj.get("rc")) is not int or obj["rc"] not in RC:
        raise ContractError("schema_mismatch", "$/rc")
    sys.stdout.write(canonical_json(obj))
    return obj["rc"]
