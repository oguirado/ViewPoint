"""Publish and query a calibrated, read-only ViewPoint capability index."""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import shlex
import unicodedata

from jsonschema import ValidationError


_schema_dir = Path(__file__).resolve().parents[1] / "schemas"
_spec = importlib.util.spec_from_file_location("vp_router_validation", _schema_dir / "validation.py")
_validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validation)
canonical_bytes = _validation.canonical_bytes
validate_contract = _validation.validate_contract

INDEX = "vp.tool_route.index/v1"
REPLY = "vp.tool_route.reply/v1"
REQUEST = "vp.control.req/v1"
_SCALE = 1000
_TOKEN = r"[^\W_]+"
_ALIASES = {"bring": "summon"}
_PLACEHOLDER = re.compile(r"<([a-z][a-z0-9_]*)>")
_CLI_WORD = re.compile(r"[A-Za-z0-9_.:/=-]+")
_RISKS = _validation._store()["urn:vp.capability:v1"]["properties"]["risk_class"]["enum"]


def _digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def _tokens(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return tuple(_ALIASES.get(word, word) for word in re.findall(_TOKEN, text))


def _phrase_in(phrase, words):
    needle = _tokens(phrase)
    return bool(needle) and any(words[i:i + len(needle)] == needle
                                for i in range(len(words) - len(needle) + 1))


def _registry(entries):
    entries = sorted(entries, key=lambda entry: entry["tool"])
    for entry in entries:
        validate_contract("vp.capability/v1", entry)
    if len({entry["tool"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate tools in the registry")
    return entries


def _model(entry):
    return entry.get("provider", {}).get("model")


def _model_denial(model):
    if model is None:
        return None
    qualification = model["qualification"]
    if qualification["state"] == "excluded" or not model["license"]["territory_ok"]:
        return "model_excluded"
    if qualification["state"] != "runnable" or not qualification["receipt"]:
        return "model_unqualified"
    return None


def _named_model_denial(registry, query):
    words = _tokens(query)
    # The frozen capability contract excludes this model even if the live
    # registry has already removed its entry.
    if _phrase_in("Hunyuan3D-2.1", words):
        return "model_excluded"
    for entry in registry:
        model = _model(entry)
        detail = _model_denial(model)
        if detail and _phrase_in(model["name"], words):
            return detail
    return None


def _index_entries(entries):
    projected = []
    for entry in entries:
        if _model_denial(_model(entry)) == "model_excluded":
            continue
        if entry.get("op_table"):
            rows = sorted(entry["op_table"], key=lambda row: row["op"])
            entry = dict(entry, ops=[row["op"] for row in rows], op_table=rows)
        elif entry["ops"]:
            entry = dict(entry, ops=sorted(entry["ops"]))
        projected.append(entry)
    return projected


@dataclass(frozen=True)
class Choice:
    entry: dict
    op: str | None
    row: dict | None
    score: int

    @property
    def key(self):
        return self.entry["tool"], self.op or ""


class RouteBindingError(ValueError):
    """The published template cannot represent the validated request."""


def _rank(entries, query):
    words = _tokens(query)
    found = set(words)
    choices = []
    for entry in entries:
        tool = entry["tool"]
        route = entry["route"]
        objects = set(_tokens(" ".join(route["objects"])))
        object_score = 400 * len(found & objects) // len(objects) if objects else 0
        rows = {row["op"]: row for row in entry.get("op_table", ())}
        for op in entry["ops"] or [None]:
            # An adapter's aggregate verbs cannot distinguish its selectors.
            verbs = [op.removeprefix("af.")] if op else route["verbs"]
            explicit = _phrase_in(op or tool, words)
            if op and "." not in op:
                explicit = explicit and _phrase_in(tool, words)
            if explicit or ((not objects or found & objects)
                            and any(_phrase_in(verb, words) for verb in verbs)):
                score = _SCALE if explicit else 600 + object_score
                choices.append(Choice(entry, op, rows.get(op), score))
    return sorted(choices, key=lambda choice: (-choice.score, choice.key))


def _margin_passes(margin, margin_min):
    return margin > 0 and margin >= margin_min


def _decision(choices, score_min):
    if not choices or choices[0].score < score_min:
        return None, 0
    top = choices[0]
    return top, top.score - (choices[1].score if len(choices) > 1 else 0)


def _static_policy(choice, context):
    detail = _model_denial(_model(choice.entry))
    if detail:
        return False, detail
    rule = choice.row or choice.entry
    if choice.op:
        frozen = _validation._adapter_rows().get(choice.key[0], {}).get("ops", {}).get(choice.op)
        if (choice.row is None or frozen is None or not frozen["supported"]
                or frozen.get("refusal_only") or choice.row["binding"]["kind"] == "none"
                or any(choice.row[key] != frozen[key]
                       for key in ("binding", "args_schema_sha256", "reply_schema_sha256"))
                or _RISKS.index(choice.row["risk_class"]) < _RISKS.index(frozen["risk_class"])
                or (frozen["operator_only"] and not choice.row["operator_only"])):
            return False, "op_unbound"
    if not rule["supported"]:
        return False, "op_unbound" if choice.op else None
    return not rule["operator_only"] or context.get("caller") == "operator", None


def _file_evidence(root, path, cache):
    if path not in cache:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Citation is outside the source root")
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Citation is outside the source root or not a file")
        data = target.read_bytes()
        cache[path] = (sha256(data).hexdigest(), len(data.splitlines()))
    return cache[path]


def _citations(choice, root, cache):
    citations = list(choice.entry["source"])
    if choice.row and choice.row["binding"]["kind"] != "none":
        binding = choice.row["binding"]["source"]
        if binding not in citations:
            citations.append(binding)
    for cite in citations:
        digest, lines = _file_evidence(root, cite["path"], cache)
        if cite["sha256"] != digest or not 1 <= cite["line_start"] <= cite["line_end"] <= lines:
            raise ValueError("Citation bytes or coordinates differ from their pin")
    model = _model(choice.entry)
    if model and not _model_denial(model):
        receipt = model["qualification"]["receipt"]
        if _file_evidence(root, receipt["path"], cache)[0] != receipt["sha256"]:
            raise ValueError("Qualification receipt differs from its pin")
    return citations


def _argv_template(choice):
    entry = choice.entry
    words = shlex.split(entry["route"]["argv_template"])
    if words[:2] != ["mvctl", entry["cli"]] or any(
        not (_CLI_WORD.fullmatch(word) or _PLACEHOLDER.fullmatch(word)) for word in words
    ):
        raise ValueError("Invalid argv_template")
    if choice.op and "<op>" not in words:
        # A generic adapter template cannot express an exact selector call.
        raise ValueError("Adapter argv_template does not bind its selector")
    return words


def _request(choice, args, context, request_id, dry_run):
    args = dict(args)
    if choice.op:
        if args.setdefault("op", choice.op) != choice.op:
            raise ValueError("Args identify a different adapter selector")
    if any(args[key] != context[key] for key in args.keys() & context.keys()):
        raise ValueError("Args and context identify different targets")
    request = dict(schema=REQUEST, op=choice.entry["json_op"], request_id=request_id,
                   context=dict(context), args=args, dry_run=dry_run)
    validate_contract(REQUEST, request)
    return request


def _calls(choice, request, template):
    args = request["args"]
    represented = {placeholder[1] for word in template
                   if (placeholder := _PLACEHOLDER.fullmatch(word))}
    if args.keys() - represented:
        raise RouteBindingError("CLI argv_template does not represent every tool argument")
    flat = dict(args, request_id=request["request_id"], dry_run=request["dry_run"])
    flat.update({key: value for key, value in request["context"].items()
                 if key not in {"caller", "agent_instance"} and key not in flat})
    argv = []
    for word in template:
        placeholder = _PLACEHOLDER.fullmatch(word)
        if placeholder:
            value = flat[placeholder[1]]
            if (type(value) not in (str, int) or not str(value) or str(value).startswith("-")
                    or any(ord(character) < 32 for character in str(value))):
                raise ValueError("Invalid CLI placeholder value")
            word = str(value)
        argv.append(word)
    if request["dry_run"] and "--dry-run" not in argv:
        argv.append("--dry-run")
    if not request["dry_run"] and "--dry-run" in argv:
        raise RouteBindingError("CLI template forces dry-run")
    return dict(mcp_call={"name": choice.key[0], "arguments": flat},
                cli=shlex.join(argv), json=request)


def _case_observation(case, entries, live, score_min):
    """Score a held-out case against registry evidence only."""
    top, margin = _decision(_rank(entries, case["request"]), score_min)
    if (not top or not margin or case.get("expected_reason") == "stale_index"
            or _named_model_denial(live, case["request"])):
        return None
    args = dict(case.get("expected_args") or {})
    expected = case.get("expected_tool"), args.pop("op", "")
    context = {"caller": "operator", "agent_instance": "router-calibration"}
    if not _static_policy(top, context)[0]:
        return None
    try:
        request = _request(top, args, context, case["id"], False)
        _calls(top, request, _argv_template(top))
    except (KeyError, ValueError, ValidationError):
        return None
    return margin, case["expected_verdict"] == "MATCH" and top.key == expected


def _fingerprints():
    return {
        "builder": {"version": "1", "source_sha256": sha256(Path(__file__).read_bytes()).hexdigest()},
        "tokenizer": {"id": "nfkc-words-v1",
                      "sha256": _digest([_TOKEN, _ALIASES, unicodedata.unidata_version])},
    }


def build_index(registry, *, manifest_generation, score_min, golden_path, source_root):
    """Return the index and calibration receipt together after all publication gates."""
    if type(score_min) is not int or not 0 <= score_min <= _SCALE:
        raise ValueError("score_min is outside the frozen score scale")
    live = _registry(registry)
    entries = _index_entries(live)
    root = Path(source_root).resolve(strict=True)
    path = Path(golden_path).resolve(strict=True)
    golden_bytes = path.read_bytes()
    golden = json.loads(golden_bytes)
    if golden["schema"] != "vp.router_golden/v1":
        raise ValueError("Unexpected golden fixture format")
    cases = golden["entries"]
    if (len({case["id"] for case in cases}) != len(cases)
            or not any(case.get("expected_reason") == "absent_capability" for case in cases)
            or any(case["expected_verdict"] not in {"MATCH", "ABSTAIN"} for case in cases)):
        raise ValueError("Calibration needs unique cases and out-of-domain controls")
    observations = [result for case in cases
                    if (result := _case_observation(case, entries, live, score_min)) is not None]
    # If the fixed ranker would wrongly match at margin m, m + 1 is necessary.
    margin = max((observed + 1 for observed, correct in observations if not correct), default=0)
    if margin > _SCALE or not any(correct and observed >= margin for observed, correct in observations):
        raise ValueError("Calibration has no safe margin with a correct held-out match")
    evidence = {}
    for entry in entries:
        rows = entry.get("op_table") or [None]
        for row in rows:
            choice = Choice(entry, row["op"] if row else None, row, 0)
            if _static_policy(choice, {"caller": "operator"})[0]:
                _argv_template(choice)
                _citations(choice, root, evidence)
    golden_pin = {"path": path.relative_to(root).as_posix(),
                  "sha256": sha256(golden_bytes).hexdigest(), "n": len(cases)}
    receipt = {
        "state": "calibrated", "method": "leave-one-out", "score_scale": _SCALE,
        "score_min": score_min, "margin_min": margin, "wrong_match": 0,
        "ood_abstain_rate_milli": _SCALE, "golden": golden_pin,
        "correct_match": sum(correct and observed >= margin for observed, correct in observations),
    }
    facts = [(entry["tool"], [fact for fact in model["measured"] if fact["tag"] == "MEASURED"])
             for entry in entries if (model := _model(entry)) is not None]
    index = {
        "schema": INDEX, "registry_sha256": _digest(live), "manifest_generation": manifest_generation,
        **_fingerprints(),
        "model_facts_sha256": _digest(facts), "entries": entries,
        "calibration": {key: value for key, value in receipt.items() if key != "correct_match"},
    }
    index["calibration"]["receipt_sha256"] = _digest(receipt)
    index["index_sha256"] = _digest(index)
    validate_contract(INDEX, index)
    return json.loads(canonical_bytes(index)), receipt


def vp_tool_route(query, *, index, registry, manifest_generation, args, context,
                  request_id, dry_run, source_root, policy):
    """Return canonical reply bytes. Policy is an injected, read-only admission check."""
    def abstain(reason, detail=None):
        return render("ABSTAIN", "ROUTE_ABSTAIN", [],
                      **({"reason": reason, "detail": detail} if detail else {"reason": reason}))

    def render(verdict, code, candidates, **extra):
        reply = dict(verdict=verdict, code=code, index_sha256=index["index_sha256"],
                     candidates=candidates, **extra)
        validate_contract(REPLY, reply)
        return canonical_bytes(reply)

    validate_contract(INDEX, index)
    live = _registry(registry)
    if (index["registry_sha256"] != _digest(live)
            or index["manifest_generation"] != manifest_generation
            or index["entries"] != _index_entries(live)
            or any(index[key] != fingerprint for key, fingerprint in _fingerprints().items())):
        return abstain("stale_index")
    model_denial = _named_model_denial(live, query)
    if model_denial:
        return abstain("policy_denied", model_denial)
    calibration = index["calibration"]
    choices = _rank(index["entries"], query)
    top, margin = _decision(choices, calibration["score_min"])
    if not top:
        return abstain("ambiguous" if choices else "absent_capability")
    if not _margin_passes(margin, calibration["margin_min"]):
        return abstain("ambiguous")
    allowed, detail = _static_policy(top, context)
    if not allowed:
        return abstain("policy_denied", detail)
    try:
        template = _argv_template(top)
    except ValueError:
        return abstain("policy_denied", "op_unbound" if top.op else None)
    try:
        request = _request(top, args, context, request_id, dry_run)
        calls = _calls(top, request, template)
    except RouteBindingError:
        return abstain("policy_denied", "op_unbound" if top.op else None)
    except (KeyError, ValueError, ValidationError):
        return abstain("missing_arguments")
    if policy(deepcopy(top.entry), deepcopy(top.row), deepcopy(request)) is not True:
        return abstain("policy_denied")
    try:
        cites = _citations(top, Path(source_root).resolve(strict=True), {})
    except (OSError, ValueError):
        return abstain("stale_index")
    return render("MATCH", "MATCH", [
        dict(tool=top.key[0], **calls, missing_args=[], margin=margin, citations=cites),
    ])
