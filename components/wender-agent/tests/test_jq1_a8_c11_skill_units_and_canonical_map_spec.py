"""C7/C-11 independent TARGET references and live/historical coordinate pins.

The reference algorithms remain offline, stdlib-only fixtures with no production
imports; their semantic assertions are unchanged by the FX2 implementation.
DocumentPinningTests separates recorded historical evidence from current source
coordinates so old defect descriptions cannot masquerade as live code. No
frames or fixtures are written, and no network, clock, browser or GPU is used.
"""

from copy import deepcopy
from itertools import permutations, product
import json
from pathlib import Path
import re
import unittest


class SpecRejected(ValueError):
    """A value is outside the explicitly proposed TARGET contract."""


_PIXEL_KEYS = frozenset(("frame", "frames", "pixels", "image", "screenshot", "png"))
_SKILL_FIELDS = frozenset((
    "id", "intent", "params", "steps", "checkpoints", "provenance", "evidence",
    "precondition_signature", "expected_post", "failure_recovery", "support_n", "min_err",
))
_MARKDOWN_PREFIX = "# Wender C-11 skill unit (TARGET v1)\n\n```json\n"
_MARKDOWN_SUFFIX = "\n```\n"
_STATE_SCHEMA = "wender.state-key/target-v1"
_MAP_SCHEMA = "wender.program-map/target-v1"


def _unicode_text(value):
    if type(value) is not str:
        raise SpecRejected("expected exact str")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpecRejected("lone surrogate is not canonical UTF-8 text") from exc


def _validate_json(value, active=None):
    """Exact integer-only JSON domain, plus a structural nonpixel guard.

    Reserved keys and binary rejection do not detect pixels encoded in an
    otherwise allowed string. Trusted nonpixel provenance is a separate
    integration requirement, explicitly ABSENT in this reference.
    """
    kind = type(value)
    if value is None or kind in (bool, int):
        return
    if kind is str:
        _unicode_text(value)
        return
    if kind not in (dict, list):
        raise SpecRejected("unsupported JSON type: " + kind.__name__)
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise SpecRejected("cyclic JSON value")
    active.add(identity)
    try:
        if kind is dict:
            for key, child in value.items():
                _unicode_text(key)  # Inspects keys before json.dumps can coerce them.
                if key.casefold() in _PIXEL_KEYS:
                    raise SpecRejected("reserved pixel key: " + key)
                _validate_json(child, active)
        else:
            for child in value:
                _validate_json(child, active)
    finally:
        active.remove(identity)


def canonical_json(value):
    """TARGET canonical Unicode text; no floats, coercion or normalization."""
    try:
        _validate_json(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (RecursionError, OverflowError) as exc:
        raise SpecRejected("JSON nesting exceeds the local runtime capacity") from exc


def _exact_fields(value, required, optional=()):
    if type(value) is not dict or not set(required).issubset(value) or set(value) - (
            set(required) | set(optional)):
        raise SpecRejected("missing or unknown record fields")


def _nonblank(value):
    _unicode_text(value)
    if not value.strip():
        raise SpecRejected("expected nonblank text")


def _metadata_object(value):
    if type(value) is not dict or not value:
        raise SpecRejected("expected nonempty metadata object")


def _validate_steps(steps, *, allow_empty=False):
    if type(steps) is not list or (not steps and not allow_empty):
        raise SpecRejected("steps must be a nonempty list")
    for step in steps:
        _exact_fields(step, ("path", "action"))
        if step["path"] not in ("api", "ax", "coordinates"):
            raise SpecRejected("step path must be api|ax|coordinates")
        _metadata_object(step["action"])


def _positive_version(value):
    if type(value) is not int or value < 1:
        raise SpecRejected("evidence.version must be a positive exact int")
    return value


def _validate_skill(skill, *, require_version):
    canonical_json(skill)
    _exact_fields(skill, _SKILL_FIELDS)
    _nonblank(skill["id"])
    _nonblank(skill["intent"])
    if type(skill["params"]) is not dict:
        raise SpecRejected("params must be an object")
    _metadata_object(skill["precondition_signature"])
    _metadata_object(skill["expected_post"])
    _validate_steps(skill["steps"])
    checkpoints = skill["checkpoints"]
    if type(checkpoints) is not list or not checkpoints:
        raise SpecRejected("checkpoints must be a nonempty list")
    for checkpoint in checkpoints:
        _exact_fields(checkpoint, ("after_step", "expected_post"))
        index = checkpoint["after_step"]
        if type(index) is not int or not 0 <= index < len(skill["steps"]):
            raise SpecRejected("after_step must be an in-range zero-based exact int")
        _metadata_object(checkpoint["expected_post"])
    recovery = skill["failure_recovery"]
    _exact_fields(recovery, ("policy", "steps"))
    if recovery["policy"] == "abort":
        _validate_steps(recovery["steps"], allow_empty=True)
        if recovery["steps"]:
            raise SpecRejected("abort recovery must have no steps")
    elif recovery["policy"] == "replay":
        _validate_steps(recovery["steps"])
    else:
        raise SpecRejected("unknown recovery policy")
    _exact_fields(skill["provenance"], ("source",))
    _nonblank(skill["provenance"]["source"])
    evidence = skill["evidence"]
    required = ("reproduced", "verified_by", "version") if require_version else (
        "reproduced", "verified_by")
    _exact_fields(evidence, required, ("version", "runs", "sha256"))
    if type(evidence["reproduced"]) is not bool:
        raise SpecRejected("evidence.reproduced must be an exact bool")
    _unicode_text(evidence["verified_by"])
    if "version" in evidence:
        _positive_version(evidence["version"])
    if "runs" in evidence and (type(evidence["runs"]) is not int or evidence["runs"] < 1):
        raise SpecRejected("evidence.runs must be a positive exact int")
    if "sha256" in evidence and (type(evidence["sha256"]) is not str or
            re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None):
        raise SpecRejected("evidence.sha256 must be 64 lowercase hexadecimal characters")
    if type(skill["support_n"]) is not int or skill["support_n"] < 0:
        raise SpecRejected("support_n must be a nonnegative exact int")
    if type(skill["min_err"]) is not int:
        raise SpecRejected("min_err must be an exact int")


def promotion_eligible(skill):
    """TARGET literal numeric gate; min_err's operational meaning is ABSENT.

    A verifier label records a supplied claim. This pure predicate does not
    replay an application or establish that the claim is independently true.
    """
    _validate_skill(skill, require_version=False)
    return (skill["evidence"]["reproduced"] is True and
            bool(skill["evidence"]["verified_by"].strip()) and
            skill["support_n"] >= 3 and skill["min_err"] >= 0)


def _require_admitted(skill):
    _validate_skill(skill, require_version=True)
    if not promotion_eligible(skill):
        raise SpecRejected("skill does not satisfy the promotion gate")


def next_evidence_version(previous=None):
    """TARGET 1 for absence; otherwise previous positive version + 1.

    Missing/invalid versions on an existing record are refused, never silently
    repaired. Legacy-record migration is deliberately outside this reference.
    """
    if previous is None:
        return 1
    _require_admitted(previous)
    return previous["evidence"]["version"] + 1


def promote_skill(draft, previous=None):
    """Return a new admitted value; validate everything before returning it.

    The draft may omit version; a supplied version must be exactly the next
    value. All other validated evidence, including optional runs/sha256, is
    retained. Neither caller-owned input is mutated on success or rejection.
    """
    if not promotion_eligible(draft):
        raise SpecRejected("skill does not satisfy the promotion gate")
    version = next_evidence_version(previous)
    if previous is not None and previous["id"] != draft["id"]:
        raise SpecRejected("previous record belongs to another skill id")
    if "version" in draft["evidence"] and draft["evidence"]["version"] != version:
        raise SpecRejected("supplied version is not the exact next version")
    admitted = deepcopy(draft)
    admitted["evidence"]["version"] = version
    _require_admitted(admitted)
    return admitted


def render_skill_markdown(skill):
    """TARGET exact Markdown envelope around one canonical JSON line."""
    _require_admitted(skill)
    return _MARKDOWN_PREFIX + canonical_json(skill) + _MARKDOWN_SUFFIX


def _unique_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise SpecRejected("duplicate JSON object key: " + key)
        value[key] = child
    return value


def _reject_json_number(value):
    raise SpecRejected("noninteger JSON number: " + value)


def parse_skill_markdown(text):
    """TARGET reject alternate fences, duplicates, trailing data and rewrites."""
    _unicode_text(text)
    if not text.startswith(_MARKDOWN_PREFIX) or not text.endswith(_MARKDOWN_SUFFIX):
        raise SpecRejected("invalid Markdown envelope")
    payload = text[len(_MARKDOWN_PREFIX):-len(_MARKDOWN_SUFFIX)]
    if not payload or "\n" in payload or "\r" in payload:
        raise SpecRejected("payload must be one nonempty JSON line")
    try:
        skill = json.loads(payload, object_pairs_hook=_unique_object,
                           parse_float=_reject_json_number, parse_constant=_reject_json_number)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SpecRejected("invalid strict JSON payload") from exc
    _require_admitted(skill)
    if canonical_json(skill) != payload:
        raise SpecRejected("payload is not in the canonical spelling")
    return deepcopy(skill)


def state_key(screen_fp, ax_stable_sha):
    """TARGET injective encoding on exact None/bool/int/Unicode-str pairs."""
    components = []
    for value in (screen_fp, ax_stable_sha):
        if value is None:
            tag = "none"
        elif type(value) is bool:
            tag = "bool"
        elif type(value) is int:
            tag = "int"
        elif type(value) is str:
            tag = "str"
        else:
            raise SpecRejected("state component must be exact None/bool/int/str")
        components.append([tag, value])
    return canonical_json([_STATE_SCHEMA, components[0], components[1]])


class CanonicalProgramMap:
    """TARGET in-memory graph with canonical identity and defensive ownership."""

    def __init__(self, program):
        _nonblank(program)
        self._program = program
        self._nodes = {}
        self._edges = {}  # Full canonical edge text -> independently owned value.

    def observe(self, screen_fp, ax_stable_sha):
        key = state_key(screen_fp, ax_stable_sha)
        if key not in self._nodes:
            self._nodes[key] = {"screen_fp": screen_fp, "ax_stable_sha": ax_stable_sha, "seen": 1}
        else:
            self._nodes[key]["seen"] += 1
        return key

    def add_edge(self, pre, action, post, changed=None):
        _unicode_text(pre)
        _unicode_text(post)
        candidate = {"pre": pre, "action": action, "post": post, "changed": changed}
        identity = canonical_json(candidate)
        if pre not in self._nodes or post not in self._nodes:
            raise KeyError("unknown state")
        # No stored node, edge or counter changes before every check succeeds.
        if identity not in self._edges:
            self._edges[identity] = deepcopy(candidate)
        return deepcopy(self._edges[identity])

    def _ordered_edges(self):
        return sorted(self._edges.values(), key=lambda edge: (
            edge["pre"], canonical_json(edge["action"]), edge["post"],
            canonical_json(edge["changed"])))

    def successors(self, key):
        _unicode_text(key)
        if key not in self._nodes:
            raise KeyError("unknown state")
        return deepcopy([edge for edge in self._ordered_edges() if edge["pre"] == key])

    def snapshot(self):
        return deepcopy({
            "schema": _MAP_SCHEMA,
            "program": self._program,
            "nodes": {key: self._nodes[key] for key in sorted(self._nodes)},
            "edges": self._ordered_edges(),
        })

    def to_json(self):
        return canonical_json(self.snapshot())


# Independent literal fixtures: no expected-value helper repeats the reference
# algorithm. Fixture constructors below only allocate ordinary input records.
_EXPECTED_SKILL_JSON_V1 = (
    '{"checkpoints":[{"after_step":0,"expected_post":{"screen":"ready"}}],'
    '"evidence":{"reproduced":true,"verified_by":"fixture-verifier","version":1},'
    '"expected_post":{"screen":"ready"},"failure_recovery":{"policy":"abort","steps":[]},'
    '"id":"skill.open","intent":"Open a panel","min_err":0,"params":{"name":"demo"},'
    '"precondition_signature":{"screen":"idle"},"provenance":{"source":"fixture://demo"},'
    '"steps":[{"action":{"name":"open"},"path":"api"}],"support_n":3}'
)
_EXPECTED_MARKDOWN_V1 = (
    '# Wender C-11 skill unit (TARGET v1)\n\n```json\n' + _EXPECTED_SKILL_JSON_V1 + '\n```\n'
)
_A = '["wender.state-key/target-v1",["str","a"],["none",null]]'
_B = '["wender.state-key/target-v1",["str","b"],["none",null]]'
_C = '["wender.state-key/target-v1",["str","c"],["none",null]]'


def _draft():
    return {
        "id": "skill.open", "intent": "Open a panel", "params": {"name": "demo"},
        "steps": [{"path": "api", "action": {"name": "open"}}],
        "checkpoints": [{"after_step": 0, "expected_post": {"screen": "ready"}}],
        "provenance": {"source": "fixture://demo"},
        "evidence": {"reproduced": True, "verified_by": "fixture-verifier"},
        "precondition_signature": {"screen": "idle"}, "expected_post": {"screen": "ready"},
        "failure_recovery": {"policy": "abort", "steps": []}, "support_n": 3, "min_err": 0,
    }


def _versioned():
    record = _draft()
    record["evidence"]["version"] = 1
    return record


def _graph():
    graph = CanonicalProgramMap("fixture-program")
    graph.observe("a", None)
    graph.observe("b", None)
    return graph


class CanonicalJsonTests(unittest.TestCase):
    def test_concrete_canonical_json_and_unicode(self):
        self.assertEqual(canonical_json({"z": {"k": "e\u0301"},
                                        "a": [None, False, True, -2, 0, "é"]}),
                         '{"a":[null,false,true,-2,0,"é"],"z":{"k":"e\u0301"}}')
        self.assertEqual(canonical_json({"control": "\0\b\f\n\r\t\x1f", "quote": '"\\'}),
                         '{"control":"\\u0000\\b\\f\\n\\r\\t\\u001f","quote":"\\\"\\\\"}')

    def test_float_values_and_nonstring_keys_are_refused_at_every_depth(self):
        for bad in (1.25, {"x": 1.25}, {"x": [None, {"y": 1.25}]},
                    {1.25: "only-key"}, {"x": {1.25: "only-key"}},
                    {"x": [{"y": {1.25: "only-key"}}]},
                    {0: "int"}, {True: "bool"}, {None: "none"}, {("a",): "tuple"},
                    {"x": [{False: "nested-bool"}]}):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(SpecRejected):
                    canonical_json(bad)

    def test_nonfinite_floats_and_unsupported_types_are_refused(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 0.0,
                    b"bytes", bytearray(b"bytes"), memoryview(b"bytes"),
                    (1, 2), {1, 2}, frozenset((1,)), object(), complex(1, 2)):
            with self.subTest(kind=type(bad).__name__, value=repr(bad)):
                with self.assertRaises(SpecRejected):
                    canonical_json({"nested": [bad]})

    def test_exact_type_policy_rejects_subclasses_including_object_keys(self):
        class IntSubclass(int):
            pass

        class StrSubclass(str):
            pass

        class ListSubclass(list):
            pass

        class DictSubclass(dict):
            pass

        for bad in (IntSubclass(3), StrSubclass("s"), ListSubclass([1]), DictSubclass(x=1),
                    {StrSubclass("key"): 1}):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(SpecRejected):
                    canonical_json(bad)

    def test_cycles_refused_but_repeated_acyclic_alias_is_accepted(self):
        cycle_list = []
        cycle_list.append(cycle_list)
        cycle_dict = {}
        cycle_dict["self"] = cycle_dict
        for bad in (cycle_list, cycle_dict, {"indirect": [cycle_dict]}):
            with self.assertRaisesRegex(SpecRejected, "cyclic"):
                canonical_json(bad)
        shared = {"v": [1]}
        self.assertEqual(canonical_json([shared, shared]), '[{"v":[1]},{"v":[1]}]')

    def test_pixel_names_and_lone_surrogates_are_refused(self):
        for key in ("frame", "frames", "pixels", "image", "screenshot", "png", "PiXeLs"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(SpecRejected, "pixel"):
                    canonical_json({"outer": [{key: "text-is-still-forbidden-here"}]})
        for bad in ("\ud800", "\udfff", {"\ud800": "x"}, {"x": "\udfff"}):
            with self.assertRaises(SpecRejected):
                canonical_json(bad)


class SkillPromotionTests(unittest.TestCase):
    def test_full_promotion_cross_product_against_literal_truth_table(self):
        # Columns are (reproduced, verified-label-present): FF, FT, TF, TT.
        # This concrete table deliberately does not calculate expected gates.
        expected = {
            (0, -1): (False, False, False, False),
            (0, 0): (False, False, False, False),
            (0, 1): (False, False, False, False),
            (2, -1): (False, False, False, False),
            (2, 0): (False, False, False, False),
            (2, 1): (False, False, False, False),
            (3, -1): (False, False, False, False),
            (3, 0): (False, False, False, True),
            (3, 1): (False, False, False, True),
            (4, -1): (False, False, False, False),
            (4, 0): (False, False, False, True),
            (4, 1): (False, False, False, True),
        }
        conditions = ((False, ""), (False, "verifier"), (True, ""), (True, "verifier"))
        for (support, minimum), outcomes in expected.items():
            for (reproduced, verifier), outcome in zip(conditions, outcomes):
                with self.subTest(support=support, min_err=minimum,
                                  reproduced=reproduced, verified_by=verifier):
                    draft = _draft()
                    draft["support_n"], draft["min_err"] = support, minimum
                    draft["evidence"].update(reproduced=reproduced, verified_by=verifier)
                    self.assertIs(promotion_eligible(draft), outcome)
                    if outcome:
                        self.assertEqual(promote_skill(draft)["evidence"]["version"], 1)
                    else:
                        with self.assertRaises(SpecRejected):
                            promote_skill(draft)

    def test_min_err_has_no_invented_upper_cutoff(self):
        draft = _draft()
        draft["min_err"] = 10 ** 40
        self.assertIs(promotion_eligible(draft), True)
        self.assertEqual(promote_skill(draft)["min_err"], 10000000000000000000000000000000000000000)

    def test_numeric_and_verification_types_are_strict(self):
        cases = (("support_n", -1), ("support_n", True), ("support_n", 3.0),
                 ("support_n", "3"), ("min_err", False), ("min_err", 0.0),
                 ("min_err", "0"))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                draft = _draft()
                draft[field] = value
                with self.assertRaises(SpecRejected):
                    promotion_eligible(draft)
        for field, value in (("reproduced", 1), ("reproduced", "true"),
                             ("verified_by", True), ("verified_by", [])):
            draft = _draft()
            draft["evidence"][field] = value
            with self.assertRaises(SpecRejected):
                promotion_eligible(draft)
        draft = _draft()
        draft["evidence"]["verified_by"] = " \t\n"
        self.assertIs(promotion_eligible(draft), False)

    def test_every_required_field_missing_and_unknown_fields_rejected(self):
        for field in ("id", "intent", "params", "steps", "checkpoints", "provenance", "evidence",
                      "precondition_signature", "expected_post", "failure_recovery", "support_n", "min_err"):
            with self.subTest(field=field):
                draft = _draft()
                del draft[field]
                with self.assertRaises(SpecRejected):
                    promotion_eligible(draft)
        for extra in ("schema", "unexpected"):
            draft = _draft()
            draft[extra] = "value"
            with self.assertRaises(SpecRejected):
                promotion_eligible(draft)

    def test_schemas_reject_malformed_metadata_steps_checkpoints_and_recovery(self):
        cases = (
            ("id", " "), ("intent", ""), ("params", []),
            ("precondition_signature", {}), ("expected_post", []),
            ("steps", []), ("steps", [{"path": "shell", "action": {"name": "open"}}]),
            ("steps", [{"path": "api", "action": {}}]),
            ("steps", [{"path": "api", "action": {"name": "open"}, "extra": 1}]),
            ("checkpoints", []),
            ("checkpoints", [{"after_step": -1, "expected_post": {"screen": "ready"}}]),
            ("checkpoints", [{"after_step": 1, "expected_post": {"screen": "ready"}}]),
            ("checkpoints", [{"after_step": False, "expected_post": {"screen": "ready"}}]),
            ("checkpoints", [{"after_step": 0, "expected_post": {}}]),
            ("checkpoints", [{"after": 0, "expect": {"screen": "ready"}}]),
            ("failure_recovery", {"policy": "continue", "steps": []}),
            ("failure_recovery", {"policy": "replay", "steps": []}),
            ("failure_recovery", {"policy": "abort", "steps": [{"path": "api", "action": {"x": 1}}]}),
            ("provenance", {"source": ""}), ("provenance", {"source": "fixture", "extra": 1}),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                draft = _draft()
                draft[field] = value
                with self.assertRaises(SpecRejected):
                    promotion_eligible(draft)

    def test_zero_based_checkpoints_and_explicit_recovery_are_preserved(self):
        draft = _draft()
        draft["steps"].append({"path": "ax", "action": {"name": "confirm"}})
        draft["checkpoints"].append({"after_step": 1, "expected_post": {"screen": "confirmed"}})
        draft["failure_recovery"] = {"policy": "replay", "steps": [
            {"path": "coordinates", "action": {"x": 0, "y": 0}}]}
        admitted = promote_skill(draft)
        self.assertEqual(admitted["checkpoints"], [
            {"after_step": 0, "expected_post": {"screen": "ready"}},
            {"after_step": 1, "expected_post": {"screen": "confirmed"}}])
        self.assertEqual(admitted["failure_recovery"], {"policy": "replay", "steps": [
            {"path": "coordinates", "action": {"x": 0, "y": 0}}]})

    def test_versions_one_and_two_and_retained_replay_evidence(self):
        draft = _draft()
        draft["evidence"].update(runs=3, sha256="0123456789abcdef" * 4)
        self.assertEqual(next_evidence_version(), 1)
        first = promote_skill(draft)
        self.assertEqual(first["evidence"], {
            "reproduced": True, "verified_by": "fixture-verifier", "version": 1, "runs": 3,
            "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"})
        self.assertEqual(next_evidence_version(first), 2)
        draft["support_n"] = 4
        second = promote_skill(draft, first)
        self.assertEqual(second["evidence"], {
            "reproduced": True, "verified_by": "fixture-verifier", "version": 2, "runs": 3,
            "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"})
        self.assertEqual(first["support_n"], 3)
        self.assertEqual(second["support_n"], 4)
        self.assertEqual(parse_skill_markdown(render_skill_markdown(second))["evidence"], {
            "reproduced": True, "verified_by": "fixture-verifier", "version": 2, "runs": 3,
            "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"})

    def test_invalid_or_missing_previous_versions_are_rejected_without_repair(self):
        for invalid in (None, False, True, -1, 0, 1.0, "1", [], {}):
            with self.subTest(version=invalid):
                previous = _versioned()
                previous["evidence"]["version"] = invalid
                with self.assertRaises(SpecRejected):
                    next_evidence_version(previous)
                with self.assertRaises(SpecRejected):
                    promote_skill(_draft(), previous)
                self.assertEqual(previous["evidence"]["version"], invalid)
        with self.assertRaises(SpecRejected):
            next_evidence_version(_draft())
        for invalid in ({}, [], False, 0):
            with self.assertRaises(SpecRejected):
                next_evidence_version(invalid)

    def test_supplied_versions_cannot_be_stale_skipped_or_invalid(self):
        for previous, invalid_versions in ((None, (0, -1, False, True, 1.0, "1", 2, 9)),
                                            (_versioned(), (0, -1, False, True, 1.0, "2", 1, 3, 9))):
            for invalid in invalid_versions:
                with self.subTest(previous=previous is not None, version=invalid):
                    draft = _draft()
                    draft["evidence"]["version"] = invalid
                    with self.assertRaises(SpecRejected):
                        promote_skill(draft, previous)
        draft = _draft()
        draft["evidence"]["version"] = 1
        self.assertEqual(promote_skill(draft)["evidence"]["version"], 1)
        draft["evidence"]["version"] = 2
        self.assertEqual(promote_skill(draft, _versioned())["evidence"]["version"], 2)

    def test_reject_other_skill_and_unverified_previous(self):
        previous = _versioned()
        previous["id"] = "skill.other"
        with self.assertRaises(SpecRejected):
            promote_skill(_draft(), previous)
        previous = _versioned()
        previous["evidence"]["reproduced"] = False
        with self.assertRaises(SpecRejected):
            next_evidence_version(previous)

    def test_optional_evidence_fields_are_validated(self):
        for field, value in (("runs", 0), ("runs", True), ("runs", 3.0),
                             ("sha256", "A" * 64), ("sha256", "a" * 63),
                             ("sha256", "g" * 64), ("sha256", 1), ("unknown", 1)):
            with self.subTest(field=field, value=value):
                draft = _draft()
                draft["evidence"][field] = value
                with self.assertRaises(SpecRejected):
                    promote_skill(draft)

    def test_promotion_defensive_copy_and_rejection_leave_inputs_unchanged(self):
        draft = _draft()
        first = promote_skill(draft)
        draft["steps"][0]["action"]["name"] = "mutated-input"
        self.assertEqual(first["steps"], [{"path": "api", "action": {"name": "open"}}])
        second = promote_skill(_draft(), first)
        second["evidence"]["verified_by"] = "mutated-return"
        second["checkpoints"][0]["expected_post"]["screen"] = "mutated-return"
        self.assertEqual(first["evidence"], {
            "reproduced": True, "verified_by": "fixture-verifier", "version": 1})
        self.assertEqual(first["checkpoints"], [{"after_step": 0, "expected_post": {"screen": "ready"}}])
        invalid = _draft()
        invalid["evidence"]["version"] = 8
        with self.assertRaises(SpecRejected):
            promote_skill(invalid, first)
        self.assertEqual(invalid["evidence"], {
            "reproduced": True, "verified_by": "fixture-verifier", "version": 8})
        self.assertEqual(first["evidence"]["version"], 1)


class MarkdownTests(unittest.TestCase):
    def test_exact_literal_render_and_parse(self):
        self.assertEqual(render_skill_markdown(_versioned()), _EXPECTED_MARKDOWN_V1)
        parsed = parse_skill_markdown(_EXPECTED_MARKDOWN_V1)
        self.assertEqual(parsed, _versioned())
        self.assertEqual(canonical_json(parsed), _EXPECTED_SKILL_JSON_V1)

    def test_unicode_fences_controls_and_combining_text_roundtrip_without_normalization(self):
        skill = _versioned()
        text = '汉字🙂 é e\u0301 👩\u200d💻\n```json\n{"x":1}\n```\n\t\x00\x1f "\\\u2028\u2029'
        skill["intent"] = text
        skill["params"] = {"e\u0301": "decomposed", "é": "composed", "値": text}
        markdown = render_skill_markdown(skill)
        parsed = parse_skill_markdown(markdown)
        self.assertEqual(parsed["intent"],
                         '汉字🙂 é e\u0301 👩\u200d💻\n```json\n{"x":1}\n```\n\t\x00\x1f "\\\u2028\u2029')
        self.assertEqual(parsed["params"]["é"], "composed")
        self.assertEqual(parsed["params"]["e\u0301"], "decomposed")
        self.assertEqual(len(parsed["params"]), 3)
        self.assertEqual(markdown.count("\n"), 5)
        self.assertIn('\\n```json\\n{\\"x\\":1}\\n```\\n', markdown)
        self.assertIn('\\t\\u0000\\u001f', markdown)
        self.assertIn('"e\u0301":"decomposed"', markdown)
        self.assertEqual(render_skill_markdown(parsed), markdown)

    def test_equal_mapping_insertion_orders_have_one_literal_render(self):
        skill = _versioned()
        reordered = {key: skill[key] for key in reversed(tuple(skill))}
        reordered["evidence"] = {"version": 1, "verified_by": "fixture-verifier", "reproduced": True}
        self.assertEqual(render_skill_markdown(reordered), _EXPECTED_MARKDOWN_V1)

    def test_envelope_truncation_fences_trailing_data_and_extra_documents_rejected(self):
        malformed = (
            "", _EXPECTED_MARKDOWN_V1[1:], _EXPECTED_MARKDOWN_V1[:-1],
            "\ufeff" + _EXPECTED_MARKDOWN_V1, "prefix\n" + _EXPECTED_MARKDOWN_V1,
            _EXPECTED_MARKDOWN_V1 + "trailing", _EXPECTED_MARKDOWN_V1 + "\n",
            _EXPECTED_MARKDOWN_V1 + _EXPECTED_MARKDOWN_V1,
            _EXPECTED_MARKDOWN_V1.replace("```json", "~~~json", 1),
            _EXPECTED_MARKDOWN_V1.replace("```json", "````json", 1),
            _EXPECTED_MARKDOWN_V1.replace("```json", "```JSON", 1),
            _EXPECTED_MARKDOWN_V1.replace("```json", "```", 1),
            _EXPECTED_MARKDOWN_V1.replace("\n", "\r\n"),
            _EXPECTED_MARKDOWN_V1[:-4] + "````\n",
            _EXPECTED_MARKDOWN_V1.replace("\n```\n", "\n``` trailing\n"),
        )
        for index, text in enumerate(malformed):
            with self.subTest(case=index):
                with self.assertRaises(SpecRejected):
                    parse_skill_markdown(text)

    def test_duplicate_keys_at_top_level_and_nested_levels_are_rejected(self):
        for payload in (
            _EXPECTED_SKILL_JSON_V1.replace('"id":"skill.open"', '"id":"skill.open","id":"skill.open"'),
            _EXPECTED_SKILL_JSON_V1.replace('"name":"demo"', '"name":"demo","name":"demo"'),
            _EXPECTED_SKILL_JSON_V1.replace('"after_step":0', '"after_step":0,"after_step":0'),
            _EXPECTED_SKILL_JSON_V1.replace('"version":1', '"version":1,"version":2'),
        ):
            with self.assertRaises(SpecRejected):
                parse_skill_markdown(_MARKDOWN_PREFIX + payload + _MARKDOWN_SUFFIX)

    def test_noncanonical_json_and_invalid_numbers_are_rejected(self):
        malformed_payloads = (
            " " + _EXPECTED_SKILL_JSON_V1, _EXPECTED_SKILL_JSON_V1 + " ",
            _EXPECTED_SKILL_JSON_V1.replace('"support_n":3', '"support_n" : 3'),
            _EXPECTED_SKILL_JSON_V1.replace('"min_err":0', '"min_err":-0'),
            _EXPECTED_SKILL_JSON_V1.replace('"min_err":0', '"min_err":0.0'),
            _EXPECTED_SKILL_JSON_V1.replace('"min_err":0', '"min_err":0e0'),
            _EXPECTED_SKILL_JSON_V1.replace('"min_err":0', '"min_err":NaN'),
            _EXPECTED_SKILL_JSON_V1.replace('"min_err":0', '"min_err":Infinity'),
            _EXPECTED_SKILL_JSON_V1.replace('"min_err":0', '"min_err":-Infinity'),
            _EXPECTED_SKILL_JSON_V1.replace('"name":"demo"', '"name":"\\u0064emo"'),
            _EXPECTED_SKILL_JSON_V1.replace('"reproduced":true,"verified_by":"fixture-verifier","version":1',
                                            '"version":1,"verified_by":"fixture-verifier","reproduced":true'),
            _EXPECTED_SKILL_JSON_V1 + "{}", _EXPECTED_SKILL_JSON_V1[:-1] + ",}",
            _EXPECTED_SKILL_JSON_V1.replace('"name":"demo"', '1.25:"demo"'),
            _EXPECTED_SKILL_JSON_V1.replace('"name":"demo"', '"name":"\\ud800"'),
            _EXPECTED_SKILL_JSON_V1.replace('"support_n":3', '"support_n":\n3'),
        )
        for index, payload in enumerate(malformed_payloads):
            with self.subTest(case=index):
                with self.assertRaises(SpecRejected):
                    parse_skill_markdown(_MARKDOWN_PREFIX + payload + _MARKDOWN_SUFFIX)

    def test_invalid_schema_and_ineligible_records_are_rejected_by_both_boundaries(self):
        bad_records = []
        missing = _versioned()
        del missing["failure_recovery"]
        bad_records.append(missing)
        extra = _versioned()
        extra["unexpected"] = "x"
        bad_records.append(extra)
        for field, value in (("support_n", 2), ("min_err", -1), ("steps", []), ("params", [])):
            bad = _versioned()
            bad[field] = value
            bad_records.append(bad)
        for version in (None, 0, False, "1"):
            bad = _versioned()
            bad["evidence"]["version"] = version
            bad_records.append(bad)
        bad_records.extend((_draft(), [], {}, True))
        for index, bad in enumerate(bad_records):
            with self.subTest(case=index):
                payload = json.dumps(bad, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                with self.assertRaises(SpecRejected):
                    parse_skill_markdown(_MARKDOWN_PREFIX + payload + _MARKDOWN_SUFFIX)
                with self.assertRaises(SpecRejected):
                    render_skill_markdown(bad)

    def test_nested_pixel_keys_float_keys_and_binary_cannot_be_rendered(self):
        for bad in ({"nest": [{1.25: "value"}]}, {"nest": [{"frames": [1, 2]}]},
                    {"nest": [b"bytes"]}):
            record = _versioned()
            record["params"] = bad
            with self.assertRaises(SpecRejected):
                render_skill_markdown(record)
        payload = _EXPECTED_SKILL_JSON_V1.replace('"name":"demo"', '"nest":[{"png":"data"}]')
        with self.assertRaises(SpecRejected):
            parse_skill_markdown(_MARKDOWN_PREFIX + payload + _MARKDOWN_SUFFIX)

    def test_parser_returns_independent_records(self):
        first = parse_skill_markdown(_EXPECTED_MARKDOWN_V1)
        first["steps"][0]["action"]["name"] = "changed"
        first["params"]["name"] = "changed"
        second = parse_skill_markdown(_EXPECTED_MARKDOWN_V1)
        self.assertEqual(second["steps"], [{"path": "api", "action": {"name": "open"}}])
        self.assertEqual(second["params"], {"name": "demo"})
        self.assertEqual(render_skill_markdown(second), _EXPECTED_MARKDOWN_V1)


class StateIdentityTests(unittest.TestCase):
    def test_literal_typed_keys_cover_collisions_in_both_components(self):
        cases = (
            ((None, None), '["wender.state-key/target-v1",["none",null],["none",null]]'),
            (("", None), '["wender.state-key/target-v1",["str",""],["none",null]]'),
            (("-", None), '["wender.state-key/target-v1",["str","-"],["none",null]]'),
            ((0, None), '["wender.state-key/target-v1",["int",0],["none",null]]'),
            (("0", None), '["wender.state-key/target-v1",["str","0"],["none",null]]'),
            ((False, None), '["wender.state-key/target-v1",["bool",false],["none",null]]'),
            ((None, ""), '["wender.state-key/target-v1",["none",null],["str",""]]'),
            ((None, "-"), '["wender.state-key/target-v1",["none",null],["str","-"]]'),
            ((None, 0), '["wender.state-key/target-v1",["none",null],["int",0]]'),
            ((None, "0"), '["wender.state-key/target-v1",["none",null],["str","0"]]'),
            ((None, False), '["wender.state-key/target-v1",["none",null],["bool",false]]'),
            ((1, None), '["wender.state-key/target-v1",["int",1],["none",null]]'),
            ((True, None), '["wender.state-key/target-v1",["bool",true],["none",null]]'),
            (("1", None), '["wender.state-key/target-v1",["str","1"],["none",null]]'),
            ((None, 1), '["wender.state-key/target-v1",["none",null],["int",1]]'),
            ((None, True), '["wender.state-key/target-v1",["none",null],["bool",true]]'),
            ((None, "1"), '["wender.state-key/target-v1",["none",null],["str","1"]]'),
            (("a:b", "c"), '["wender.state-key/target-v1",["str","a:b"],["str","c"]]'),
            (("a", "b:c"), '["wender.state-key/target-v1",["str","a"],["str","b:c"]]'),
        )
        outputs = []
        for args, expected in cases:
            with self.subTest(args=args):
                value = state_key(*args)
                self.assertEqual(value, expected)
                outputs.append(value)
        self.assertEqual(len(set(outputs)), 19)

    def test_full_falsy_and_typed_pair_cross_product_has_64_identities(self):
        values = (None, "", "-", 0, False, 1, True, "1")
        self.assertEqual(len({state_key(first, second) for first, second in product(values, repeat=2)}), 64)

    def test_unicode_is_preserved_without_normalization(self):
        self.assertEqual(state_key("é", "汉字🙂"),
                         '["wender.state-key/target-v1",["str","é"],["str","汉字🙂"]]')
        self.assertEqual(state_key("e\u0301", "汉字🙂"),
                         '["wender.state-key/target-v1",["str","e\u0301"],["str","汉字🙂"]]')
        self.assertNotEqual(state_key("é", None), state_key("e\u0301", None))
        self.assertEqual(state_key("a\n```\0", '"\\'),
                         '["wender.state-key/target-v1",["str","a\\n```\\u0000"],["str","\\\"\\\\"]]')

    def test_invalid_types_and_lone_surrogates_are_rejected_on_either_side(self):
        class IntSubclass(int):
            pass

        for invalid in (0.0, [], {}, (), b"fp", IntSubclass(1), "\ud800"):
            for args in ((invalid, None), (None, invalid)):
                with self.subTest(args=repr(args)):
                    with self.assertRaises(SpecRejected):
                        state_key(*args)


class CanonicalMapTests(unittest.TestCase):
    def test_observation_returns_literal_identity_and_separate_typed_nodes(self):
        graph = CanonicalProgramMap("fixture-program")
        self.assertEqual(graph.observe("a", None), _A)
        self.assertEqual(graph.observe("a", None), _A)
        graph.observe(0, None)
        graph.observe(False, None)
        nodes = graph.snapshot()["nodes"]
        self.assertEqual(nodes, {
            '["wender.state-key/target-v1",["str","a"],["none",null]]': {
                "screen_fp": "a", "ax_stable_sha": None, "seen": 2},
            '["wender.state-key/target-v1",["int",0],["none",null]]': {
                "screen_fp": 0, "ax_stable_sha": None, "seen": 1},
            '["wender.state-key/target-v1",["bool",false],["none",null]]': {
                "screen_fp": False, "ax_stable_sha": None, "seen": 1},
        })
        self.assertIs(type(nodes['["wender.state-key/target-v1",["int",0],["none",null]]']["screen_fp"]), int)
        self.assertIs(type(nodes['["wender.state-key/target-v1",["bool",false],["none",null]]']["screen_fp"]), bool)

    def test_observation_multiset_permutations_preserve_typed_nodes_and_counts(self):
        observations = (("a", None), (0, None), (False, None), ("a", None))
        expected = {
            "schema": "wender.program-map/target-v1", "program": "fixture-program", "edges": [],
            "nodes": {
                '["wender.state-key/target-v1",["str","a"],["none",null]]': {
                    "screen_fp": "a", "ax_stable_sha": None, "seen": 2},
                '["wender.state-key/target-v1",["int",0],["none",null]]': {
                    "screen_fp": 0, "ax_stable_sha": None, "seen": 1},
                '["wender.state-key/target-v1",["bool",false],["none",null]]': {
                    "screen_fp": False, "ax_stable_sha": None, "seen": 1},
            },
        }
        serialized = set()
        # Permute indices so Python equality cannot collapse 0/False fixtures.
        for order in permutations(range(4)):
            graph = CanonicalProgramMap("fixture-program")
            for index in order:
                graph.observe(*observations[index])
            self.assertEqual(graph.snapshot(), expected)
            self.assertEqual(list(graph.snapshot()["nodes"]), [
                '["wender.state-key/target-v1",["bool",false],["none",null]]',
                '["wender.state-key/target-v1",["int",0],["none",null]]',
                '["wender.state-key/target-v1",["str","a"],["none",null]]',
            ])
            serialized.add(graph.to_json())
        self.assertEqual(len(serialized), 1)

    def test_canonical_edge_identity_distinguishes_true_and_integer_one_in_both_fields(self):
        graph = _graph()
        for action, changed in ((True, True), (1, 1), (True, 1), (1, True)):
            graph.add_edge(_A, action, _B, changed)
        edges = graph.successors(_A)
        self.assertEqual(len(edges), 4)
        self.assertEqual([canonical_json([edge["action"], edge["changed"]]) for edge in edges],
                         ["[1,1]", "[1,true]", "[true,1]", "[true,true]"])
        self.assertEqual([type(edge["action"]) for edge in edges], [int, int, bool, bool])
        self.assertEqual([type(edge["changed"]) for edge in edges], [int, bool, int, bool])

    def test_all_edge_permutations_use_changed_tiebreak_and_nested_typed_identity(self):
        inputs = (
            ({"op": "click"}, {"result": True}),
            ({"op": "click"}, {"result": 1}),
            ({"op": "click"}, {"result": 0}),
            ({"op": True}, {"result": 1}),
            ({"op": 1}, {"result": 1}),
        )
        expected_edges = [
            {"pre": _A, "action": {"op": "click"}, "post": _B, "changed": {"result": 0}},
            {"pre": _A, "action": {"op": "click"}, "post": _B, "changed": {"result": 1}},
            {"pre": _A, "action": {"op": "click"}, "post": _B, "changed": {"result": True}},
            {"pre": _A, "action": {"op": 1}, "post": _B, "changed": {"result": 1}},
            {"pre": _A, "action": {"op": True}, "post": _B, "changed": {"result": 1}},
        ]
        canonical_observations = set()
        for order in permutations(inputs):
            graph = _graph()
            for action, changed in order:
                graph.add_edge(_A, action, _B, changed)
            edges = graph.successors(_A)
            self.assertEqual(edges, expected_edges)
            # Literal scalar strings catch bool/int equality masking a bad order.
            self.assertEqual([canonical_json(edge["changed"]) for edge in edges],
                             ['{"result":0}', '{"result":1}', '{"result":true}',
                              '{"result":1}', '{"result":1}'])
            self.assertEqual([canonical_json(edge["action"]) for edge in edges],
                             ['{"op":"click"}', '{"op":"click"}', '{"op":"click"}',
                              '{"op":1}', '{"op":true}'])
            self.assertEqual(graph.snapshot()["edges"], expected_edges)
            canonical_observations.add(graph.to_json())
        self.assertEqual(len(canonical_observations), 1)

    def test_full_sort_priority_pre_action_post_changed_is_concrete(self):
        graph = _graph()
        graph.observe("c", None)
        for pre, action, post, changed in (
            (_B, 0, _A, 0), (_A, 2, _B, 0), (_A, 1, _C, 0),
            (_A, 1, _B, 2), (_A, 1, _B, 1)):
            graph.add_edge(pre, action, post, changed)
        self.assertEqual(graph.snapshot()["edges"], [
            {"pre": _A, "action": 1, "post": _B, "changed": 1},
            {"pre": _A, "action": 1, "post": _B, "changed": 2},
            {"pre": _A, "action": 1, "post": _C, "changed": 0},
            {"pre": _A, "action": 2, "post": _B, "changed": 0},
            {"pre": _B, "action": 0, "post": _A, "changed": 0},
        ])
        self.assertEqual(graph.successors(_B), [{"pre": _B, "action": 0, "post": _A, "changed": 0}])
        self.assertEqual(graph.successors(_C), [])

    def test_object_key_order_is_deduplicated_but_array_order_is_preserved(self):
        graph = _graph()
        graph.add_edge(_A, {"b": 2, "a": 1}, _B, [1, 2])
        graph.add_edge(_A, {"a": 1, "b": 2}, _B, [1, 2])
        graph.add_edge(_A, {"a": 1, "b": 2}, _B, [2, 1])
        self.assertEqual(graph.successors(_A), [
            {"pre": _A, "action": {"a": 1, "b": 2}, "post": _B, "changed": [1, 2]},
            {"pre": _A, "action": {"a": 1, "b": 2}, "post": _B, "changed": [2, 1]},
        ])

    def test_all_mutable_boundaries_are_defensively_copied(self):
        graph = _graph()
        action = {"op": {"args": [1, {"value": "original"}]}}
        changed = {"fields": [{"name": "ready"}]}
        returned = graph.add_edge(_A, action, _B, changed)
        action["op"]["args"][1]["value"] = "mutated-input"
        changed["fields"][0]["name"] = "mutated-input"
        returned["action"]["op"]["args"][1]["value"] = "mutated-return"
        returned["changed"]["fields"][0]["name"] = "mutated-return"
        duplicate = graph.add_edge(_A, {"op": {"args": [1, {"value": "original"}]}}, _B,
                                   {"fields": [{"name": "ready"}]})
        duplicate["action"]["op"]["args"].append(9)
        duplicate["changed"]["fields"].clear()
        successors = graph.successors(_A)
        successors[0]["action"]["op"]["args"].clear()
        successors[0]["changed"]["fields"].clear()
        successors.append({"extra": True})
        snapshot = graph.snapshot()
        snapshot["nodes"][_A]["seen"] = 999
        snapshot["nodes"][_A]["screen_fp"] = "changed"
        snapshot["edges"][0]["action"]["op"]["args"][1]["value"] = "mutated-snapshot"
        snapshot["edges"][0]["changed"]["fields"][0]["name"] = "mutated-snapshot"
        snapshot["program"] = "changed"
        self.assertEqual(graph.snapshot(), {
            "schema": "wender.program-map/target-v1", "program": "fixture-program",
            "nodes": {
                _A: {"screen_fp": "a", "ax_stable_sha": None, "seen": 1},
                _B: {"screen_fp": "b", "ax_stable_sha": None, "seen": 1},
            },
            "edges": [{"pre": _A, "action": {"op": {"args": [1, {"value": "original"}]}},
                       "post": _B, "changed": {"fields": [{"name": "ready"}]}}],
        })

    def test_rejected_edges_and_observations_are_atomic(self):
        graph = _graph()
        graph.add_edge(_A, {"op": "open"}, _B, {"ready": True})
        invalids = (0.0, {1.25: "float-key"}, {"x": [{1.25: "nested-float-key"}]},
                    {"x": [{True: "nested-bool-key"}]}, {"x": [b"binary"]},
                    {"x": {"pixels": "forbidden"}}, (1, 2))
        for invalid in invalids:
            for action, changed in ((invalid, None), ({"op": "other"}, invalid)):
                with self.subTest(value=repr(invalid)):
                    with self.assertRaises(SpecRejected):
                        graph.add_edge(_A, action, _B, changed)
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(SpecRejected):
            graph.add_edge(_A, {"cycle": cycle}, _B)
        for pre, post in (("unknown", _B), (_A, "unknown")):
            with self.assertRaises(KeyError):
                graph.add_edge(pre, {}, post)
        for invalid in (0.0, [], {}, "\ud800"):
            with self.assertRaises(SpecRejected):
                graph.observe(invalid, None)
        self.assertEqual(graph.snapshot(), {
            "schema": "wender.program-map/target-v1", "program": "fixture-program",
            "nodes": {
                _A: {"screen_fp": "a", "ax_stable_sha": None, "seen": 1},
                _B: {"screen_fp": "b", "ax_stable_sha": None, "seen": 1},
            },
            "edges": [{"pre": _A, "action": {"op": "open"}, "post": _B, "changed": {"ready": True}}],
        })

    def test_map_name_edge_endpoints_and_unknown_successors_are_validated(self):
        for program in ("", " \t", None, True, 0, [], "\ud800"):
            with self.assertRaises(SpecRejected):
                CanonicalProgramMap(program)
        graph = _graph()
        for invalid in (None, True, 0, [], 0.0):
            with self.assertRaises(SpecRejected):
                graph.add_edge(invalid, {}, _B)
            with self.assertRaises(SpecRejected):
                graph.add_edge(_A, {}, invalid)
            with self.assertRaises(SpecRejected):
                graph.successors(invalid)
        with self.assertRaises(KeyError):
            graph.successors("unknown")
        self.assertEqual(graph.snapshot()["edges"], [])

    def test_empty_map_has_a_literal_canonical_snapshot(self):
        self.assertEqual(CanonicalProgramMap("é").to_json(),
                         '{"edges":[],"nodes":{},"program":"é","schema":"wender.program-map/target-v1"}')


_BOUND_FILES = frozenset((
    "wender/remember/program_map.py", "wender/remember/skills.py", "wender/remember/store.py",
    "wender/learn/replay.py", "wender/learn/segmenter.py", "wender/learn/demonstrations.py",
    "wender/learn/explore.py", "wender/state/schema.py",
    "docs/spec/WA_S3_REMEMBER_CONTRACTS.md", "docs/spec/WA_S4_LEARN_CONTRACTS.md",
))
_BASELINE_SOURCE_LINE_PINS = {
    ("wender/remember/program_map.py", 11): "def state_key(screen_fp, ax_stable_sha):",
    ("wender/remember/program_map.py", 22): "    def observe(self, screen_fp, ax_stable_sha):",
    ("wender/remember/program_map.py", 28): "    def add_edge(self, pre, action, post, changed=None):",
    ("wender/remember/program_map.py", 37): "    def successors(self, key):",
    ("wender/remember/program_map.py", 40): "    def to_json(self):",
    ("wender/remember/skills.py", 17): "def admit(skill):",
    ("wender/remember/skills.py", 26): '    ev = skill["evidence"]',
    ("wender/remember/store.py", 57): "def _version(skill):",
    ("wender/remember/store.py", 203): "    def append_skill(self, skill):",
    ("wender/learn/replay.py", 135): '    draft["evidence"] = {',
    ("wender/state/schema.py", 31): 'def _no_float(o, path="$"):',
}
# Live pins change only with reviewed production coordinates; baseline evidence remains above.
_SOURCE_LINE_PINS = {
    ('wender/remember/program_map.py', 55): 'def canonical_json(value):',
    ('wender/remember/program_map.py', 69): 'def state_key(screen_fp, ax_stable_sha):',
    ('wender/remember/program_map.py', 88): 'def canonicalize_state_key(stored_key, screen_fp, ax_stable_sha):',
    ('wender/remember/program_map.py', 118): '    def observe(self, screen_fp, ax_stable_sha):',
    ('wender/remember/program_map.py', 125): '    def add_edge(self, pre, action, post, changed=None):',
    ('wender/remember/program_map.py', 138): '    def successors(self, key):',
    ('wender/remember/program_map.py', 143): '    def snapshot(self):',
    ('wender/remember/program_map.py', 147): '    def to_json(self):',
    ('wender/remember/skills.py', 170): 'def admit(skill):',
    ('wender/remember/skills.py', 179): 'def promotion_eligible(skill):',
    ('wender/remember/skills.py', 186): 'def next_evidence_version(previous=None):',
    ('wender/remember/skills.py', 197): 'def promote_skill(draft, previous=None):',
    ('wender/remember/skills.py', 213): 'def render_skill_md(skill):',
    ('wender/remember/skills.py', 234): 'def parse_skill_md(text):',
    ('wender/remember/store.py', 57): 'def _version(skill):',
    ('wender/remember/store.py', 115): '    def _maps(self, records):',
    ('wender/remember/store.py', 196): '    def append_map(self, program_map):',
    ('wender/remember/store.py', 220): '    def append_skill(self, skill):',
    ('wender/learn/replay.py', 73): 'def _checkpoint_index(checkpoint, count):',
    ('wender/learn/replay.py', 83): 'def _validate_expect(expect):',
    ('wender/learn/replay.py', 106): 'def _observe_state(driver):',
    ('wender/learn/replay.py', 116): 'def replay_and_admit(draft, driver, verified_by, runs=2):',
    ('wender/learn/segmenter.py', 212): 'def _verified_handoff(intent, actions, segment, receipt, replayer):',
    ('wender/learn/segmenter.py', 270): 'def run_pipeline(frames, engine, labeler=demonstrations.label_intent,',
    ('wender/learn/demonstrations.py', 25): 'def label_intent(segment_frames):',
    ('wender/learn/demonstrations.py', 40): 'def reproduced_share_ppm(segments):',
    ('wender/learn/explore.py', 11): 'def _detect_stopstate(state):',
    ('wender/learn/explore.py', 50): 'def explore(driver, program, max_steps=32, reset=None):',
    ('wender/learn/explore.py', 110): '    def checked_observe():',
    ('wender/state/schema.py', 50): 'def _no_float(o, path="$"):',
}
_CITATION_RE = re.compile(
    r"(?P<path>(?:wender|docs)/[A-Za-z0-9_./-]+\.(?:py|md)):"
    r"(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?(?![0-9-])"
)


def _citation_coordinates(text):
    return [(match["path"], int(match["start"]), int(match["end"] or match["start"]))
            for match in _CITATION_RE.finditer(text)]


class DocumentPinningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.document_path = cls.root / "docs/spec/jq1_a8_c11_skill_units_and_canonical_map_spec.md"

    def _document(self):
        self.assertTrue(self.document_path.is_file(), "required C7/C-11 document is absent")
        return self.document_path.read_text(encoding="utf-8")

    def test_document_has_substantive_required_sections(self):
        document = self._document()
        required = {
            "Source binding and status": ("bridge/integration", "f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0"),
            "C7 baseline behavior analysis": ("D-A8", "f0c75b21:wender/remember/program_map.py#L"),
            "C-11 skill-unit target": ("precondition_signature", "expected_post", "failure_recovery"),
            "Promotion thresholds": ("support_n", "min_err", "ABSENT"),
            "Evidence version lifecycle": ("next_evidence_version", "previous", "version"),
            "Deterministic non-pixel Markdown": ("render_skill_markdown", "parse_skill_markdown", "Unicode"),
            "Typed state identity": ("wender.state-key/target-v1", "None", "False"),
            "Canonical edge identity and ordering": ("changed", "True", "canonical"),
            "Ownership and integer-only validation": ("float", "copy", "keys"),
            "Reference functions and examples": ("promote_skill", "CanonicalProgramMap", "```python"),
            "Verification matrix": ("permutation", "Unicode", "version"),
            "Historical integration gaps": ("ABSENT", "min_err", "pixel"),
            "FX2 production implementation": ("wender.state-key/v1", "render_skill_md", "previous",
                                              'stopstate="unavailable"', "fx2-f1-perceive-wait-text-stopstate"),
            "Journal migration boundary": ("pre-existing journals", "reserved Store owner", "observation counts",
                                           "read normalization", "node identity conflict", "typed endpoints"),
            "ABSENT and integration limits": ("ABSENT", "F1", "Store", "min_err"),
        }
        sections = {}
        for match in re.finditer(r"^## ([^\n]+)\n", document, flags=re.MULTILINE):
            end = document.find("\n## ", match.end())
            self.assertNotIn(match[1], sections, "duplicate document section")
            sections[match[1]] = document[match.end():end if end != -1 else len(document)]
        for heading, tokens in required.items():
            with self.subTest(section=heading):
                self.assertIn(heading, sections)
                for token in tokens:
                    self.assertIn(token, sections[heading])

    def test_historical_source_evidence_keeps_snapshot_qualified_coordinates(self):
        document = self._document()
        for (path, number), expected in _BASELINE_SOURCE_LINE_PINS.items():
            with self.subTest(path=path, line=number):
                coordinate = "f0c75b21:" + path + "#L" + str(number)
                self.assertIn("`" + coordinate + "`", document)
                self.assertIn("`" + expected.strip() + "`", document)
        self.assertEqual(_citation_coordinates(
            "f0c75b21:wender/remember/program_map.py#L11-L12"), [])

    def test_requested_citation_lines_have_exact_bound_text(self):
        document = self._document()
        coordinates = set(_citation_coordinates(document))
        for (path, number), expected in _SOURCE_LINE_PINS.items():
            with self.subTest(path=path, line=number):
                source = self.root / path
                self.assertTrue(source.is_file())
                lines = source.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(len(lines), number)
                self.assertEqual(lines[number - 1], expected)
                # The document retains the original schema citation; bind that
                # exact reference to the current definition after 8483040.
                citation_number = {("wender/state/schema.py", 50): 31,
                                   # WDEMO replaced the stub; the document keeps the original stub citations.
                                   ("wender/learn/demonstrations.py", 25): 16,
                                   ("wender/learn/demonstrations.py", 40): 29}.get((path, number), number)
                self.assertIn((path, citation_number, citation_number), coordinates,
                              "required exact single-line citation missing from document")

    def test_all_document_source_coordinates_exist_in_bound_files_and_ranges(self):
        coordinates = _citation_coordinates(self._document())
        self.assertTrue(coordinates, "no source coordinates found")
        cited_files = set()
        for path, first, last in coordinates:
            with self.subTest(path=path, first=first, last=last):
                self.assertIn(path, _BOUND_FILES, "document cites an unbound source")
                cited_files.add(path)
                source = self.root / path
                self.assertTrue(source.is_file())
                lines = source.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(first, 1)
                self.assertGreaterEqual(last, first)
                self.assertLessEqual(last, len(lines))
                self.assertTrue(any(line.strip() for line in lines[first - 1:last]),
                                "source range consists entirely of blank lines")
        self.assertEqual(cited_files, set(_BOUND_FILES))

    def test_coordinate_parser_preserves_paths_and_ranges(self):
        self.assertEqual(_citation_coordinates(
            '`wender/remember/program_map.py:11` and `wender/remember/store.py:57-64`; '
            '`wender/learn/replay.py:135-140`, `docs/spec/WA_S3_REMEMBER_CONTRACTS.md:9`.'),
            [("wender/remember/program_map.py", 11, 11),
             ("wender/remember/store.py", 57, 64),
             ("wender/learn/replay.py", 135, 140),
             ("docs/spec/WA_S3_REMEMBER_CONTRACTS.md", 9, 9)])


if __name__ == "__main__":
    unittest.main()
