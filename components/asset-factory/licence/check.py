"""Pure, bounded licence decisions. No adapter, model or subprocess is loaded.

Run from the component root: python3 -B -m licence.check --config FILE
--providers-root DIR --operation generate. JSON is always supported; YAML is
optional. The CLI only reads files and prints its decision.
"""
import argparse
import errno
import importlib
import json
import os
from pathlib import Path
import re
import stat

from asset_factory import core

from asset_factory.contracts import validate_provider

ROOT = Path(__file__).resolve().parent.parent
# DESIGN_CHOICE work bounds [TARGET], independent of caller configuration.
MAX_INPUT_BYTES = 1024 * 1024
MAX_NODES = 16384
MAX_DEPTH = 32
MAX_YAML_TOKENS = 32768
MAX_INTEGER_BITS = 4096
EU_COUNTRIES = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())


class _Refusal(core.ContractError):
    def __init__(self, code, rc=2, path=""):
        super().__init__(code)
        self.code, self.rc, self.path = code, rc, path

    def as_dict(self):
        return {"rc": self.rc, "status": core.STATUS[self.rc],
                "reason_codes": [self.code], "path": self.path}


def _fail(code, rc=2, path=""):
    raise _Refusal(code, rc, path)


def _budget_tree(value):
    pending = [(value, 0)]
    nodes = 0
    text_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            _fail("budget_exceeded", 6)
        if type(item) is dict:
            if any(type(k) is not str for k in item):
                _fail("config_invalid")
            if nodes + len(pending) + len(item) > MAX_NODES:
                _fail("budget_exceeded", 6)
            for key in item:
                if len(key) > MAX_INPUT_BYTES:
                    _fail("budget_exceeded", 6)
                try:
                    text_bytes += len(key.encode("utf-8"))
                except UnicodeError as exc:
                    raise _Refusal("config_invalid") from exc
            pending.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            if nodes + len(pending) + len(item) > MAX_NODES:
                _fail("budget_exceeded", 6)
            pending.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            if len(item) > MAX_INPUT_BYTES:
                _fail("budget_exceeded", 6)
            try:
                text_bytes += len(item.encode("utf-8"))
            except UnicodeError as exc:
                raise _Refusal("config_invalid") from exc
        elif type(item) is int and item.bit_length() > MAX_INTEGER_BITS:
            _fail("budget_exceeded", 6)
        elif item is not None and type(item) not in (str, int, bool):
            _fail("non_integer_number")
        if text_bytes > MAX_INPUT_BYTES:
            _fail("budget_exceeded", 6)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail("duplicate_key")
        result[key] = value
    return result


def _no_number(_):
    _fail("non_integer_number")


def _read_descriptor(descriptor):
    """Consume and close an already opened, bounded regular-file descriptor."""
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _fail("config_invalid")
            data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            _fail("budget_exceeded", 6)
        return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise _Refusal("config_invalid") from exc


def _read_text(path):
    try:
        # O_NONBLOCK avoids waiting on caller-supplied FIFO paths.
        descriptor = os.open(Path(path), os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, ValueError) as exc:
        raise _Refusal("config_invalid") from exc
    return _read_descriptor(descriptor)


def _read_provider(providers_root, name):
    """Resolve inside-root aliases, then open the full chain without following links."""
    try:
        base = providers_root.resolve(strict=True)
        candidate = (base / name / "provider.json").resolve()
        if not candidate.is_relative_to(base):
            _fail("path_escape")
        relative = candidate.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _Refusal("path_escape") from exc
    directory = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        # Anchor even the root's ancestors: none may become an escaping symlink.
        directory = os.open(base.anchor, flags)
        for part in (*base.parts[1:], *relative.parts[:-1]):
            opened = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = opened
        try:
            descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _Refusal("path_escape") from exc
            raise _Refusal("config_invalid") from exc
        return _json(_read_descriptor(descriptor))
    except OSError as exc:
        raise _Refusal("path_escape") from exc
    finally:
        if directory is not None:
            os.close(directory)


def _json(text):
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_float=_no_number, parse_constant=_no_number)
    except RecursionError as exc:
        raise _Refusal("budget_exceeded", 6) from exc
    except (ValueError, TypeError) as exc:
        raise _Refusal("config_invalid") from exc
    _budget_tree(value)
    return value


def _yaml(text):
    try:
        yaml = importlib.import_module("yaml")
    except ImportError as exc:
        raise _Refusal("yaml_unavailable", 3) from exc

    class StrictLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        # Merges, aliases and explicit tags are refused before construction.
        values = [(loader.construct_object(key), loader.construct_object(value))
                  for key, value in node.value]
        if any(type(key) is not str or key == "<<" for key, _ in values):
            _fail("config_invalid")
        return _pairs(values)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        depth = 0
        for index, token in enumerate(yaml.scan(text, Loader=StrictLoader)):
            if index >= MAX_YAML_TOKENS:
                _fail("budget_exceeded", 6)
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                _fail("config_invalid")
            if isinstance(token, (yaml.tokens.BlockMappingStartToken, yaml.tokens.BlockSequenceStartToken,
                                  yaml.tokens.FlowMappingStartToken, yaml.tokens.FlowSequenceStartToken)):
                depth += 1
                if depth > MAX_DEPTH:
                    _fail("budget_exceeded", 6)
            elif isinstance(token, (yaml.tokens.BlockEndToken, yaml.tokens.FlowMappingEndToken,
                                    yaml.tokens.FlowSequenceEndToken)):
                depth -= 1
        value = yaml.load(text, Loader=StrictLoader)
    except RecursionError as exc:
        raise _Refusal("budget_exceeded", 6) from exc
    except yaml.YAMLError as exc:
        raise _Refusal("config_invalid") from exc
    _budget_tree(value)
    return value


def _schema(name):
    value = _json(_read_text(ROOT / name))
    if type(value) is not dict:
        _fail("config_invalid")
    # The frozen kernel subset omits annotation keywords.
    return {k: v for k, v in value.items() if k not in ("$id", "$schema")}


def _shape(value, schema, code):
    """Enforce U2's subset even while core.validate is the permitted stub."""
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _shape(value, branch, code)
                matches += 1
            except _Refusal:
                pass
        if matches != 1:
            _fail(code)
    kind = schema.get("type")
    types = {"object": dict, "array": list, "integer": int, "boolean": bool, "string": str, "null": type(None)}
    if kind and type(value) is not types[kind]:
        _fail(code)
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        _fail(code)
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        _fail(code)
    if type(value) is dict:
        props = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            _fail(code)
        for key, item in value.items():
            if key in props:
                _shape(item, props[key], code)
            elif schema.get("additionalProperties") is False:
                _fail(code)
            elif type(schema.get("additionalProperties")) is dict:
                _shape(item, schema["additionalProperties"], code)
    if type(value) is list:
        if len(value) < schema.get("minItems", 0):
            _fail(code)
        for item in value:
            _shape(item, schema.get("items", {}), code)


def _closed(value, keys, code, optional=()):
    if type(value) is not dict or set(keys) - value.keys() or value.keys() - set(keys) - set(optional):
        _fail(code)


def _strings(value, code, *, unique=True):
    if type(value) is not list or any(type(s) is not str or not s for s in value):
        _fail(code)
    if unique and len(set(value)) != len(value):
        _fail(code)


def _policy_rules(policy):
    _budget_tree(policy)
    _closed(policy, ("schema", "groups", "countries"), "config_invalid")
    if policy["schema"] != "af.territory-groups/v1":
        _fail("config_invalid")
    _strings(policy["countries"], "config_invalid")
    if not policy["countries"] or any(re.fullmatch(r"[A-Z]{2}", c) is None for c in policy["countries"]):
        _fail("config_invalid")
    _closed(policy["groups"], ("EU", "UK", "KR"), "config_invalid")
    for group, members in policy["groups"].items():
        _strings(members, "config_invalid")
        expected = EU_COUNTRIES if group == "EU" else {"GB" if group == "UK" else "KR"}
        if set(members) != expected or not set(members) <= set(policy["countries"]):
            _fail("config_invalid")


def load_policy(path: Path) -> dict:
    policy = _json(_read_text(path))
    _policy_rules(policy)
    return policy


def _factory_rules(factory, policy):
    _require_schemas()
    _budget_tree(factory)
    schema = FACTORY_SCHEMA
    _shape(factory, schema, "config_invalid")
    core.validate(factory, schema)
    if factory["jurisdiction"] not in policy["countries"]:
        _fail("unknown_jurisdiction")
    for field in ("models_path",):
        if not factory[field]:
            _fail("config_invalid")
    memory = factory["memory"]
    if memory["ceiling_gb_default"] <= 0 or memory["min_free_gb_before_start"] < 0:
        _fail("config_invalid")
    appearance = factory["gates"]["appearance"]
    if appearance["floor"] < 0 or appearance["target"] < appearance["floor"] or not factory["gates"]["vqa3d_profile"]:
        _fail("config_invalid")
    for slot in factory["slots"].values():
        if not slot["provider"] or "\x00" in slot["provider"]:
            _fail("config_invalid")


def load_factory_config(path: Path) -> dict:
    text = _read_text(path)
    # JSON remains accepted regardless of filename, including .yaml.
    if Path(path).suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        factory = _json(text)
    else:
        factory = _yaml(text)
    _factory_rules(factory, load_policy(ROOT / "config/territory-groups.v1.json"))
    return factory


def _pinned(value):
    return type(value) is str and bool(value.strip()) and value.strip().lower() not in ("main", "master", "latest", "head")


def _territories(value, policy):
    _strings(value, "provider_invalid")
    if any(v not in policy["groups"] and v not in policy["countries"] for v in value):
        _fail("provider_invalid")


def _provider_rules(provider, policy):
    _budget_tree(provider)
    keys = ("schema", "id", "slot", "kind", "runnable", "revision", "licence", "territory_excluded",
            "commercial", "gated", "weights", "runtime", "known_issues")
    _closed(provider, keys, "provider_invalid", ("blocked_reason",))
    if provider["schema"] != "af.provider/v1" or not isinstance(provider["id"], str) or not provider["id"]:
        _fail("provider_invalid")
    if provider["slot"] not in core.SLOTS or provider["kind"] not in ("model", "tool") or type(provider["runnable"]) is not bool:
        _fail("provider_invalid")
    if provider["runnable"]:
        if not _pinned(provider["revision"]) or "blocked_reason" in provider:
            _fail("provider_invalid")
    elif not isinstance(provider.get("blocked_reason"), str) or not provider["blocked_reason"]:
        _fail("provider_invalid")
    elif provider["revision"] is not None and not _pinned(provider["revision"]):
        _fail("provider_invalid")
    for key in ("commercial", "gated"):
        if provider[key] is not None and type(provider[key]) is not bool:
            _fail("provider_invalid")
    _territories(provider["territory_excluded"], policy)
    _closed(provider["licence"], ("code", "weights", "deps"), "provider_invalid")
    if any(type(provider["licence"][k]) is not str or not provider["licence"][k] for k in ("code", "weights")):
        _fail("provider_invalid")
    deps = provider["licence"]["deps"]
    if type(deps) is not list:
        _fail("provider_invalid")
    names = set()
    for dep in deps:
        _closed(dep, ("name", "licence", "commercial", "territory_excluded", "gated", "used_by"), "provider_invalid")
        if any(type(dep[k]) is not str or not dep[k] for k in ("name", "licence")) or dep["name"] in names:
            _fail("provider_invalid")
        names.add(dep["name"])
        for key in ("commercial", "gated"):
            if dep[key] is not None and type(dep[key]) is not bool:
                _fail("provider_invalid")
        _territories(dep["territory_excluded"], policy)
        _strings(dep["used_by"], "provider_invalid")
        if not dep["used_by"] or any(op not in core.SLOTS for op in dep["used_by"]):
            _fail("provider_invalid")
    _strings(provider["known_issues"], "provider_invalid", unique=False)
    weights = provider["weights"]
    if type(weights) is not list or (provider["runnable"] and provider["kind"] == "model" and not weights):
        _fail("provider_invalid")
    if provider["kind"] == "tool" and weights:
        _fail("provider_invalid")
    for weight in weights:
        _closed(weight, ("repo", "revision", "files"), "provider_invalid")
        if type(weight["repo"]) is not str or not weight["repo"] or not _pinned(weight["revision"]):
            _fail("provider_invalid")
        if type(weight["files"]) is not list or not weight["files"]:
            _fail("provider_invalid")
        for file in weight["files"]:
            _closed(file, ("path", "sha256"), "provider_invalid")
            if type(file["path"]) is not str or not file["path"] or type(file["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", file["sha256"]) is None:
                _fail("provider_invalid")
    runtime = provider["runtime"]
    _closed(runtime, ("container", "ram_peak_gb_measured", "ram_peak_measurement_ref"), "provider_invalid")
    if type(runtime["container"]) is not str:
        _fail("provider_invalid")
    peak, ref = runtime["ram_peak_gb_measured"], runtime["ram_peak_measurement_ref"]
    if peak is not None and (type(peak) is not int or peak <= 0):
        _fail("provider_invalid")
    if peak is None and ref is not None:
        _fail("provider_invalid")
    if peak is not None:
        _closed(ref, ("path", "sha256"), "provider_invalid")
        if type(ref["path"]) is not str or not ref["path"] or type(ref["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) is None:
            _fail("provider_invalid")
    if validate_provider is not None:
        try:
            validate_provider(provider)
        except core.ContractError as exc:
            raise _Refusal("provider_invalid") from exc


def _excluded(jurisdiction, exclusions, policy):
    return any(jurisdiction in policy["groups"].get(term, [term]) for term in exclusions)


def _record(factory, provider, operation, policy, rc, reasons, evaluated, monetisable):
    _require_schemas()
    # If the malformed input cannot populate the frozen record, raise instead.
    if (type(factory) is not dict or type(provider) is not dict or
            type(factory.get("jurisdiction")) is not str or type(factory.get("commercial_use")) is not bool or
            type(provider.get("id")) is not str or operation not in core.SLOTS):
        _fail(reasons[0] if reasons else "config_invalid")
    result = {"schema": "af.licence-decision/v1", "rc": rc, "verdict": core.VERDICT[rc],
              "allowed": rc == 0, "provider_id": provider["id"], "operation": operation,
              "jurisdiction": factory["jurisdiction"], "commercial_use": factory["commercial_use"],
              "monetisable": rc == 0 and monetisable, "reason_codes": reasons,
              "evaluated_dependencies": evaluated, "configuration_sha256": core.sha256_json(factory),
              "provider_sha256": core.sha256_json(provider), "policy_sha256": core.sha256_json(policy)}
    schema = DECISION_SCHEMA
    _shape(result, schema, "config_invalid")
    core.validate(result, schema)
    return result


def check_provider(factory: dict, provider: dict, *, operation: str, policy: dict) -> dict:
    """Return current authorization, never an asset-quality verdict."""
    # Unhashable inputs cannot form a bound decision, including on refusal.
    # Keep their original ContractError outside the decision-building handler.
    for bound in (factory, provider, policy):
        _budget_tree(bound)
    try:
        _policy_rules(policy)
        _factory_rules(factory, policy)
        _provider_rules(provider, policy)
        if operation not in core.SLOTS:
            _fail("config_invalid")
    except core.ContractError as exc:
        # Over-budget and noncanonical inputs cannot safely be hashed.
        if exc.rc == 6 or exc.code == "non_integer_number":
            raise
        return _record(factory, provider, operation, policy, exc.rc, [exc.code], [], False)

    reasons, evaluated = [], []
    codes = []
    monetisable = provider["commercial"] is True

    def reason(code, rc):
        if code not in reasons:
            reasons.append(code)
        codes.append(rc)

    if _excluded(factory["jurisdiction"], provider["territory_excluded"], policy):
        reason("territory_excluded", 5)
    if not provider["runnable"]:
        reason("provider_blocked", 5)
    # q3: gated=true has no entitlement-proof rule in the frozen interface.
    # Fail closed until that missing rule is supplied; a flag is not proof.
    elif provider["commercial"] is None or provider["gated"] is not False:
        reason("rights_unknown", 3)
    if provider["commercial"] is False and factory["commercial_use"]:
        reason("commercial_refused", 5)
    for dep in provider["licence"]["deps"]:
        if operation not in dep["used_by"]:
            continue
        evaluated.append(dep["name"])
        monetisable = monetisable and dep["commercial"] is True
        if _excluded(factory["jurisdiction"], dep["territory_excluded"], policy):
            reason("dependency_territory_excluded", 5)
        if dep["commercial"] is False and factory["commercial_use"]:
            reason("dependency_noncommercial", 5)
        if dep["commercial"] is None or dep["gated"] is not False:
            reason("rights_unknown", 3)
    rc = core.worst_rc(codes or [0])
    return _record(factory, provider, operation, policy, rc, reasons, evaluated, monetisable)


def check_configuration(factory: dict, providers: dict[str, dict], *, policy: dict) -> dict:
    _policy_rules(policy)
    _factory_rules(factory, policy)
    if type(providers) is not dict:
        _fail("provider_invalid")
    decisions = {}
    for slot in core.SLOTS:
        name = factory["slots"][slot]["provider"]
        if name == "none":
            continue
        if name not in providers or type(providers[name]) is not dict or providers[name].get("id") != name:
            _fail("provider_invalid")
        decisions[slot] = check_provider(factory, providers[name], operation=slot, policy=policy)
    rc = core.worst_rc(d["rc"] for d in decisions.values())
    return {"rc": rc, "verdict": core.VERDICT[rc], "decisions": decisions}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _fail("config_invalid")


def main(argv: list[str] | None = None) -> int:
    try:
        _require_schemas()
        parser = _Parser(description=__doc__)
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--providers-root", type=Path, required=True)
        parser.add_argument("--operation", choices=core.SLOTS, required=True)
        args = parser.parse_args(argv)
        factory = load_factory_config(args.config)
        policy = load_policy(ROOT / "config/territory-groups.v1.json")
        name = factory["slots"][args.operation]["provider"]
        if name == "none":
            _fail("provider_invalid")
        provider = _read_provider(args.providers_root, name)
        if type(provider) is not dict or provider.get("id") != name:
            _fail("provider_invalid")
        return core.emit(check_provider(factory, provider, operation=args.operation, policy=policy))
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except OSError:
        return core.emit(_Refusal("config_invalid").as_dict())
    except RecursionError:
        return core.emit(_Refusal("budget_exceeded", 6).as_dict())


def _require_schemas():
    if _SCHEMA_ERROR is not None:
        raise _SCHEMA_ERROR


# Snapshot packaged data at import so successful library evaluations stay pure.
# Defer a failed snapshot to the guarded CLI/API boundary, never a traceback.
try:
    FACTORY_SCHEMA = _schema("config/factory.schema.json")
    DECISION_SCHEMA = _schema("licence/decision.schema.json")
except core.ContractError as exc:
    FACTORY_SCHEMA = DECISION_SCHEMA = None
    _SCHEMA_ERROR = exc
else:
    _SCHEMA_ERROR = None


if __name__ == "__main__":
    raise SystemExit(main())
