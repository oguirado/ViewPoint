# SPDX-License-Identifier: AGPL-3.0-only
"""v0.3 evidence projection (STEP v03-binding, T02 offline slice).

Binds REA slot-host evidence to frozen attempt_evidence_record v0.3
and rea_retention_index v1: every emitted record validates against
the frozen schema bytes (sha256 pinned to fixtures_v03/SHA256SUMS).
v0.2 stays frozen and supported.  Authenticated retention (T01),
attribution witnesses (T03), live readback (T04), recovery (T05)
and the vision mapping grant (T06) stay denying seams.
"""

import copy
import hashlib
import json
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.compare._evaluate import evaluate
from slot_host.compare.tests.test_comparators import behaviour_comparison, event_comparison, event_trace, timeline_side
from slot_host.runtime import evidence_projection as ep
from slot_host.runtime.binding import sha256_of_file
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.tests.test_live_park import _fixture_capability
from slot_host.runtime.runner import run_bound
from slot_host.runtime.receipts import (
    build_attempt_receipt,
    finalize_receipt,
    _shared_adapter,
    evidence_accepted,
    reconcile_run,
    receipt_digest,
)
from slot_host.runtime.tests.binding_fixtures import (
    fixture_retention,
    verified_fixture_binding as verify_binding,
)

setUpModule = fixture_retention

FIXTURES = Path(__file__).resolve().parent / "fixtures_v03"
FROZEN_SUMS_PATH = FIXTURES / "SHA256SUMS"
_FROZEN = {
    "attempt_evidence_record.v0.2.schema.json": "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c",
    "attempt_evidence_record.v0.3.schema.json": "9402c78a4780cab53d9ad9726b103d05e76b504fe693c16b572078c9d8c98dc7",
    "rea_retention_index.v1.schema.json": "9c4113e4981e0b9356e642534cc3fc9684ab03a51265ebdfe5e934f8207e98ad",
}

# Minimal stdlib Draft 2020-12 matcher for the frozen schemas (no
# third-party validator in the slot-host test surface).
_META = {"$schema", "$id", "$comment", "$defs", "title"}
_KW = _META | {"$ref", "type", "const", "enum", "pattern", "minLength", "maxLength", "minimum", "maximum",
              "properties", "required", "additionalProperties", "minProperties", "maxProperties",
              "dependentRequired", "minItems", "maxItems", "items", "prefixItems", "uniqueItems",
              "contains", "minContains", "maxContains", "oneOf", "anyOf", "allOf", "if", "then", "else", "not"}
_TYPES = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
          "integer": lambda v: type(v) is int or (type(v) is float and v.is_integer()),
          "string": lambda v: isinstance(v, str), "boolean": lambda v: type(v) is bool,
          "null": lambda v: v is None}


def _defn(schema, ref):
    assert ref.startswith("#/$defs/")
    return schema["$defs"][ref.removeprefix("#/$defs/")]


def matches(value, node, schema):
    assert not (set(node) - _KW), f"unsupported keywords {set(node) - _KW}"
    if "type" in node and not _TYPES[node["type"]](value):
        return False
    if "$ref" in node and not matches(value, _defn(schema, node["$ref"]), schema):
        return False
    if "const" in node and value != node["const"]:
        return False
    if "enum" in node and value not in node["enum"]:
        return False
    if "not" in node and matches(value, node["not"], schema):
        return False
    for kw, reduce in (("allOf", all), ("anyOf", any), ("oneOf", lambda r: sum(r) == 1)):
        if kw in node and not reduce(matches(value, b, schema) for b in node[kw]):
            return False
    if "if" in node:
        arm = "then" if matches(value, node["if"], schema) else "else"
        if arm in node and not matches(value, node[arm], schema):
            return False
    if isinstance(value, str):
        return (node.get("minLength", 0) <= len(value) <= node.get("maxLength", len(value))
                and ("pattern" not in node or re.search(node["pattern"], value) is not None))
    if type(value) in (int, float):
        return node.get("minimum", value) <= value <= node.get("maximum", value)
    if isinstance(value, list):
        return _arr(value, node, schema)
    if isinstance(value, dict):
        props = node.get("properties", {})
        return (node.get("minProperties", 0) <= len(value) <= node.get("maxProperties", len(value))
                and set(node.get("required", [])) <= set(value)
                and (node.get("additionalProperties") is not False or set(value) <= set(props))
                and all(matches(value[k], props[k], schema) for k in set(props) & set(value))
                and all(k not in value or set(d) <= set(value) for k, d in node.get("dependentRequired", {}).items()))
    return True


def _arr(value, node, schema):
    if not node.get("minItems", 0) <= len(value) <= node.get("maxItems", len(value)):
        return False
    if node.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
        return False
    prefix = node.get("prefixItems", [])
    if not all(matches(item, rule, schema) for item, rule in zip(value, prefix)):
        return False
    if "items" in node and not all(matches(item, node["items"], schema) for item in value[len(prefix):]):
        return False
    if "contains" in node:
        count = sum(matches(item, node["contains"], schema) for item in value)
        return node.get("minContains", 1) <= count <= node.get("maxContains", len(value))
    return True


def validate(instance, schema):
    return matches(instance, schema, schema)


_IMPL = "a" * 64
_MANIFEST_SHA = "c" * 64
_PROVIDER_SHA = "d" * 64
_ADAPTER = "e" * 64
_REQ = "b" * 64
_SAFE = 2**53 - 1


def _events():
    return [
        {"event_id": "event/one", "t_ms": 1, "t_ticks": 1, "kind": "key", "input_digest": "a" * 64},
        {"event_id": "event/two", "t_ms": 2, "t_ticks": 2, "kind": "key", "input_digest": "a" * 64, "occurrence": 1},
    ]


def _timeline_side():
    events = _events()
    raw = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"clock_source": "pts", "origin": "recording_start", "canonicalization": "rea.timeline_digest/v1",
            "time_base": {"num": 1, "den": 1000}, "events": events,
            "timeline_sha256": hashlib.sha256(raw).hexdigest()}


def _media_side():
    return {"recording_sha256": "f" * 64, "container": "mp4", "codec": "h264", "clock_source": "pts",
            "time_base": {"num": 1, "den": 1000},
            "frames": [{"frame_index": 0, "pts_ms": 0, "pts_ticks": 3600, "decoded_frame_sha256": "1" * 64,
                        "frame_file_sha256": "2" * 64}]}


def _receipt(**over):
    kwargs = dict(
        attempt_id="att-v03-0001", request_id="req-v03-0001", operation="record", slot_role="verify",
        slot_id="fixture-tool", tool_version="2.0", upstream_licence_id="MIT",
        source_url="https://example.org/tool", use_disposition="external_execution",
        provider_id="rea.provider/v2", provider_revision="2.0", implementation_sha256=_IMPL,
        producer_kind="external_slot", producer_id="rea.provider/v2", producer_version="2.0",
        producer_implementation_sha256=_IMPL, process_bound=False,
        perception_level="none", perception_ceiling="none",
        parent_episode_id="ep-parent-1", replay=None,
        shared_schema_ref=ep.V03_REF, manifest_sha256=_MANIFEST_SHA,
    )
    kwargs.update(over)
    return build_attempt_receipt(**kwargs)


_COMMAND = {"kind": "argv", "arguments": ["/fixture/stage/fixture-tool", ""], "cwd": "/fixture/work",
            "timeout_ms": 100, "max_output_bytes": 1024, "env_sha256": ep.env_sha256({"PATH": "/usr/bin"})}
_SCOPE = {"kind": "posix_child", "id": "rea-posix-child/fixture"}


class FrozenSchemaPins(unittest.TestCase):
    def test_fixture_hashes_match_frozen_sums(self):
        self.assertEqual(FROZEN_SUMS_PATH, FIXTURES / "SHA256SUMS")
        lines = FROZEN_SUMS_PATH.read_text(encoding="utf-8").splitlines()
        expected = {}
        for line in lines:
            sha, name = line.split("  ", maxsplit=1)
            self.assertNotIn(name, expected)
            expected[name] = sha
        self.assertEqual({name: expected[name] for name in _FROZEN}, _FROZEN)
        paths = set()
        for path in FIXTURES.rglob("*"):
            relative = path.relative_to(FIXTURES)
            if (path != FROZEN_SUMS_PATH and "__pycache__" not in relative.parts
                    and path.suffix not in {".pyc", ".pyo"} and path.is_file()):
                paths.add(relative.as_posix())
        self.assertEqual(set(expected), paths)
        self.assertEqual(expected.get("tools/attempt_evidence.py.txt"),
                         "bc7cecfefc522c55911124ff1ac3d7be44ce2c5a45a0522b6629619a96d2022f")
        for name, sha in expected.items():
            self.assertEqual(hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest(), sha, name)


class SharedAdapterPins(unittest.TestCase):
    def test_v03_ref_binds_v03(self):
        adapter = _shared_adapter(ep.V03_REF, ep.V03_SHA, "receipt:fixture/1")
        self.assertEqual(adapter["binding_state"], "bound")
        self.assertEqual(adapter["shared_schema_id"], ep.V03_ID)
        self.assertEqual(adapter["shared_schema_sha256"], ep.V03_SHA)
        self.assertFalse(adapter["live_use_permitted"])
        self.assertNotIn("shared_receipt_ref", adapter)

    def test_v02_still_binds_v02(self):
        adapter = _shared_adapter(ep.V02_REF, ep.V02_SHA, "receipt:fixture/1")
        self.assertEqual(adapter["shared_schema_id"], ep.V02_ID)
        self.assertEqual(adapter["shared_schema_sha256"], ep.V02_SHA)

    def test_unknown_and_pending_refs_stay_pending(self):
        for ref in ("evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending",
                    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v1",
                    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending",
                    "https://example.org/other.json", "shared:fix"):
            self.assertEqual(_shared_adapter(ref, "0" * 64, "receipt:x")["binding_state"], "pending_publication", ref)

    def test_wrong_sha_denies_v03(self):
        self.assertEqual(_shared_adapter(ep.V03_REF, "0" * 64, "receipt:x")["binding_state"], "pending_publication")

    def test_evidence_accepted_allows_v03_adapter_for_offline_fixture(self):
        receipt = _receipt(shared_receipt_ref="receipt:fixture/1")
        custody = {"producer_binding_verified": True, "artifact_digest_verified": True, "cleanup_observed": True,
                   "bound_process_id": 42, "observed_process_id": 42, "bound_starttime": 7, "observed_starttime": 7,
                   "timed_out": False, "provider_id": "rea.provider/v2", "request_id": "req-v03-0001",
                   "observed_sha256": _IMPL, "implementation_sha256": _IMPL, "result_sha256": "e" * 64}
        slot_result = {"schema": "rea.slot_result/v1", "status": "OK", "rc": 0, "sha256": "e" * 64}
        frozen = finalize_receipt(copy.deepcopy(receipt), slot_result, custody=custody, implementation_sha256=_IMPL)
        wrapper = {"receipt": frozen, "custody": custody}
        self.assertFalse(evidence_accepted(wrapper))
        self.assertTrue(evidence_accepted(dict(wrapper, custody=dict(custody, offline_fixture=True))))


class ExternalStarted(unittest.TestCase):
    def test_registered_url_matches_frozen_schema(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        def build(url):
            return ep.build_external_started(_receipt(source_url=url), installation_ref="installation/x",
                                             command=_COMMAND, scope=_SCOPE, executor_id="host/rea-launcher")
        prefix = "https://vendor.example/"
        for url in ("HTTPS://Vendor.Example.:443/tool@stable", "https://[::1]:443/tool",
                    prefix + "x" * (2048 - len(prefix))):
            record = build(url)
            self.assertTrue(validate(record, schema), url)
            self.assertEqual(record["slot"]["source_url"], url)
        for url in ("https://vendor.invalid/tool", "https://Vendor.INVALID.:443/tool",
                    "https://fixture.user@example.org/tool", "https://fixture@example.org/tool",
                    "https://bad_host/tool", "https://-bad.org/tool", "https://vendor.org:123456/tool",
                    "https://vendor.org/white space", "https://vendor.org/ctrl\x00", "https://vendor.org\n",
                    prefix + "x" * (2049 - len(prefix))):
            with self.subTest(url=url[:80]), self.assertRaisesRegex(ValueError, "slot.source_url"):
                build(url)

    def test_started_checks_frozen_command_scope_and_slot_bounds(self):
        options = {"installation_ref": "installation/x", "command": _COMMAND, "scope": _SCOPE,
                   "executor_id": "host/rea-launcher"}
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        ready = ep.build_external_started(_receipt(), **(options | {
            "command": dict(_COMMAND, timeout_ms=_SAFE), "scope": dict(_SCOPE, memory_high_bytes=None),
        }))
        self.assertTrue(validate(ready, schema))
        for key in ("timeout_ms", "max_output_bytes"):
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, f"command.{key}"):
                ep.build_external_started(_receipt(), **(options | {"command": dict(_COMMAND, **{key: _SAFE + 1})}))
        for key in ("cgroup_dev", "cgroup_ino", "memory_high_bytes", "memory_max_bytes", "memory_swap_max_bytes"):
            scope = {"kind": "cgroup_v2", "id": "test/scope", "cgroup_dev": 0, "cgroup_ino": 1, key: _SAFE + 1}
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, f"scope.{key}"):
                ep.build_external_started(_receipt(), **(options | {"scope": scope}))
        with self.assertRaisesRegex(ValueError, "slot_id"):
            ep.build_external_started(_receipt(slot_id="a" * 25), **options)

    def test_started_validates_with_digest_linkage(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        receipt = _receipt()
        started = ep.build_external_started(receipt, installation_ref="installation/fixture-tool",
                                            command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")
        self.assertTrue(validate(started, schema))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", started["attempt_id"]))
        self.assertEqual(started["command_sha256"], ep.record_digest(dict(_COMMAND)))
        self.assertEqual(started["external_process"]["slot_id"], "fixture-tool")
        self.assertNotIn("capsule", started)
        self.assertNotIn("fixture_sha256", started)

    def test_started_denies_non_kebab_slot_id(self):
        receipt = _receipt(slot_id="fixture_tool")
        with self.assertRaises(ValueError):
            ep.build_external_started(receipt, installation_ref="installation/x",
                                      command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")

    def test_started_denies_urn_source_url(self):
        receipt = _receipt(source_url="urn:fixture:slot")
        with self.assertRaises(ValueError):
            ep.build_external_started(receipt, installation_ref="installation/x",
                                      command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")

    def test_started_denies_relative_executable(self):
        bad = dict(_COMMAND, arguments=["fixture-tool"])
        with self.assertRaises(ValueError):
            ep.build_external_started(_receipt(), installation_ref="installation/x",
                                      command=bad, scope=dict(_SCOPE), executor_id="host/rea-launcher")

class ExternalObservedAborted(unittest.TestCase):
    def _started(self, receipt=None):
        return ep.build_external_started(receipt or _receipt(), installation_ref="installation/fixture-tool",
                                         command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")

    def _process(self):
        return {"pid": 41, "start_id": "host/start", "job_id": "host/job", "starttime_ticks": 156, "boot_id": "host/boot-fixture"}

    def _launch_obs(self):
        return {"observer_id": "observer/launch", "observation_id": "launch/fixture",
                "receipt_sha256": ep.record_digest({"attempt_id": "att-v03-0001", "pid": 41}),
                "executable_sha256": _IMPL, "executed_path": "/fixture/stage/fixture-tool"}

    def test_inline_artifact_enforces_exact_byte_cap(self):
        receipt = _receipt()
        started = self._started(receipt)
        options = {"installation_ref": "installation/fixture-tool", "scope": _SCOPE,
                   "process": self._process(), "launch_observation": self._launch_obs(),
                   "cleanup_retirement_sha256": "f" * 64}
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        payload = b"x" * 65536
        observed = ep.build_external_observed(receipt, started, **options, artifact_payload=payload,
                                             artifact_sha256=hashlib.sha256(payload).hexdigest())
        self.assertTrue(validate(observed, schema))
        self.assertEqual(observed["artifact"]["bytes"], 65536)
        payload += b"x"
        with self.assertRaisesRegex(ValueError, "inline artifact exceeds"):
            ep.build_external_observed(receipt, started, **options, artifact_payload=payload,
                                       artifact_sha256=hashlib.sha256(payload).hexdigest())

    def test_external_terminals_enforce_independent_observers(self):
        receipt = _receipt()
        started = self._started(receipt)
        builders = (
            (ep.build_external_observed, {"installation_ref": "installation/fixture-tool", "scope": _SCOPE,
                                          "process": self._process(), "launch_observation": self._launch_obs()}),
            (ep.build_external_aborted, {"abort_reason": LIVE_EXECUTION_PARKED}),
        )
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        for build, options in builders:
            unknown = build(receipt, started, **options, cleanup_state="unknown", cleanup_observer_id=started["executor_id"])
            self.assertTrue(validate(unknown, schema))
            for field in ("artifact_observer_id", "cleanup_observer_id"):
                with self.subTest(builder=build.__name__, field=field), self.assertRaisesRegex(ValueError, "independent"):
                    build(receipt, started, **options, cleanup_retirement_sha256="f" * 64,
                          **{field: started["executor_id"]})

    def test_observed_denies_unsafe_process_start_ticks(self):
        receipt = _receipt()
        started = self._started(receipt)
        process = dict(self._process(), starttime_ticks=_SAFE + 1)
        with self.assertRaisesRegex(ValueError, "process.starttime_ticks"):
            ep.build_external_observed(receipt, started, installation_ref="installation/fixture-tool", scope=_SCOPE,
                                       process=process, launch_observation=self._launch_obs(), cleanup_state="unknown")

    def test_observed_rejects_unbound_command_digest_and_scope(self):
        receipt = _receipt()
        started = self._started(receipt)
        for start, scope in ((dict(started, command_sha256="invalid"), _SCOPE),
                             (dict(started, command_sha256="0" * 64), _SCOPE),
                             (started, dict(_SCOPE, id="foreign/scope"))):
            with self.subTest(scope=scope, digest=start["command_sha256"]), self.assertRaisesRegex(ValueError, "command digest and scope"):
                ep.build_external_observed(receipt, start, installation_ref="installation/fixture-tool", scope=scope,
                                           process=self._process(), launch_observation=self._launch_obs(), cleanup_state="unknown")

    def test_observed_validates_with_execution_binding(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        receipt = _receipt()
        started = self._started(receipt)
        payload = b'{"schema":"rea.slot_result/v1","status":"OK","rc":0,"sha256":"' + b"e" * 64 + b'"}\n'
        observed = ep.build_external_observed(
            receipt, started, installation_ref="installation/fixture-tool", scope=dict(_SCOPE),
            process=self._process(), launch_observation=self._launch_obs(),
            artifact_payload=payload, artifact_sha256=hashlib.sha256(payload).hexdigest(),
            cleanup_retirement_sha256="f" * 64)
        self.assertTrue(validate(observed, schema))
        self.assertEqual(observed["start_sha256"], ep.record_digest(started))
        self.assertEqual(observed["execution"]["command_sha256"], started["command_sha256"])
        self.assertEqual(observed["artifact"]["execution_sha256"], ep.record_digest(observed["execution"]))
        self.assertEqual(observed["artifact"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(observed["artifact"]["bytes"], len(payload))
        self.assertIn("attribution_receipt_sha256", observed["artifact"])
        self.assertEqual(observed["observation"]["producer"]["kind"], "external_slot")

    def test_observed_denies_mismatched_started(self):
        receipt = _receipt()
        started = self._started(receipt)
        started["attempt_id"] = "0" * 32
        with self.assertRaises(ValueError):
            ep.build_external_observed(
                receipt, started, installation_ref="installation/fixture-tool", scope=dict(_SCOPE),
                process=self._process(), launch_observation=self._launch_obs(),
                cleanup_retirement_sha256="f" * 64)

    def test_aborted_before_spawn_validates(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        receipt = _receipt()
        started = self._started(receipt)
        aborted = ep.build_external_aborted(receipt, started, abort_reason=LIVE_EXECUTION_PARKED,
                                            cleanup_retirement_sha256="f" * 64)
        self.assertTrue(validate(aborted, schema))
        self.assertEqual(aborted["state"], "aborted_before_spawn")
        self.assertEqual(aborted["start_sha256"], ep.record_digest(started))
        self.assertEqual(aborted["abort_reason"], "LIVE_EXECUTION_PARKED")
        verdict = aborted.get("verdict")
        self.assertTrue(verdict is None or verdict["result"] == "abstain")

    def test_aborted_denies_unwitnessed_retirement(self):
        receipt = _receipt()
        started = self._started(receipt)
        with self.assertRaises(ValueError):
            ep.build_external_aborted(receipt, started, abort_reason="LIVE_EXECUTION_PARKED")

    def test_unseeded_replay_projects_without_seed(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        receipt = _receipt(operation="replay", replay={"input_tape_sha256": "9" * 64,
                                                       "determinism_mode": "uncontrolled"})
        started = self._started(receipt)
        self.assertTrue(validate(started, schema))
        self.assertEqual(started["replay_inputs"]["determinism_mode"], "uncontrolled")
        self.assertNotIn("seed", started["replay_inputs"])


    def test_started_and_inline_terminal_reject_legacy_and_object_timeline(self):
        external = _receipt()
        compare = _receipt(operation="compare", reference_episode_id="ep-reference", candidate_episode_id="ep-candidate")
        compare["observation"]["producer"] = {"kind": "host_mini_tool", "id": "host/compare", "process_bound": False}
        external_start = self._started(external)
        compare_args = {"executor_id": "host/compare", "reference_attempt_id": "1" * 32, "candidate_attempt_id": "2" * 32}
        compare_start = ep.build_compare_started(compare, **compare_args)
        observed_args = {"installation_ref": "installation/fixture-tool", "scope": _SCOPE,
                         "process": self._process(), "launch_observation": self._launch_obs(), "cleanup_state": "unknown"}
        legacy = dict(_timeline_side(), canonicalization="tools.record_digest/v0.2", timeline_sha256=ep.record_digest(_events()))
        indexed = _timeline_side()
        indexed.pop("events")
        indexed["events_object_ref"] = "object/known-events"
        branches = (
            (self._started, external, (), {}),
            (ep.build_external_observed, external, (external_start,), observed_args),
            (ep.build_compare_started, compare, (), compare_args),
            (ep.build_compare_observed, compare, (compare_start,), {"cleanup_state": "unknown"}),
        )
        for build, receipt, args, kwargs in branches:
            for side, error in ((legacy, "canonicalization"), (indexed, "events_object_ref")):
                with self.subTest(branch=build.__name__, error=error), self.assertRaisesRegex(ValueError, error):
                    build(dict(receipt, timeline={"reference": side}), *args, **kwargs)

    def test_timeline_validates_unique_identity_ordinals_and_fractional_order(self):
        events = [dict(event, t_ms=0) for event in _events()]
        side = dict(_timeline_side(), time_base={"num": 1, "den": 10_000})

        def started(rows):
            raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            timeline = dict(side, events=rows, timeline_sha256=hashlib.sha256(raw).hexdigest())
            return self._started(_receipt(timeline={"reference": timeline}))

        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        good = started(events)
        self.assertTrue(validate(good, schema))
        self.assertEqual(good["timeline"]["reference"]["events"], events)
        for error, changed in (
            ("duplicate event_id", {"event_id": events[0]["event_id"]}),
            ("occurrence", {"occurrence": 2}),
            ("source order", {"t_ticks": 0}),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                started([events[0], dict(events[1], **changed)])

    def test_media_timeline_and_replay_reject_numbers_above_the_frozen_ceiling(self):
        media = _media_side()
        for key in ("frame_index", "pts_ms", "pts_ticks"):
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, key):
                self._started(_receipt(media={"reference": dict(media, frames=[dict(media["frames"][0], **{key: _SAFE + 1})])}))
        with self.assertRaisesRegex(ValueError, "frames_unavailable_reason"):
            self._started(_receipt(media={"reference": dict(media, frames=[], frames_unavailable_reason="")}))
        timeline = dict(_timeline_side(), time_base={"num": 1, "den": _SAFE + 1}, events=[])
        timeline["timeline_sha256"] = hashlib.sha256(b"[]").hexdigest()
        with self.assertRaisesRegex(ValueError, "time_base.den"):
            self._started(_receipt(timeline={"reference": timeline}))
        for key in ("t_ms", "t_ticks"):
            event = dict(_events()[0], t_ms=_SAFE + 1, t_ticks=_SAFE + 1)
            if key == "t_ms":
                event.pop("t_ticks")
            else:
                # Keep the projected milliseconds in range so only the tick breaks the bound.
                event["t_ms"] = (_SAFE + 1) // 10
            base = {"num": 1, "den": 10_000}
            timeline = dict(_timeline_side(), time_base=base, events=[event], timeline_sha256=ep.jcs_digest([event]))
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, f"event.{key}"):
                self._started(_receipt(timeline={"reference": timeline}))
        for mode, key in (("seeded", "seed"), ("distribution", "sample_count")):
            replay = {"determinism_mode": mode, "input_tape_sha256": "a" * 64, key: _SAFE + 1}
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, f"replay.{key}"):
                self._started(_receipt(operation="replay", replay=replay))


class TimelineMediaTicket(unittest.TestCase):
    def test_timeline_and_media_project_and_validate(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        receipt = _receipt(media={"reference": _media_side()}, timeline={"reference": _timeline_side()})
        started = ep.build_external_started(receipt, installation_ref="installation/fixture-tool",
                                            command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")
        self.assertTrue(validate(started, schema))
        self.assertIn("media", started)
        self.assertIn("timeline", started)

    def test_timeline_digest_tamper_denies(self):
        receipt = _receipt(timeline={"reference": _timeline_side()})
        receipt["timeline"]["reference"]["timeline_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            ep.build_external_started(receipt, installation_ref="installation/fixture-tool",
                                      command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")

    def test_timeline_out_of_order_denies(self):
        events = list(reversed(_events()))
        raw = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        side = {"clock_source": "pts", "origin": "recording_start", "canonicalization": "rea.timeline_digest/v1",
                "time_base": {"num": 1, "den": 1000}, "events": events, "timeline_sha256": hashlib.sha256(raw).hexdigest()}
        receipt = _receipt(timeline={"reference": side})
        with self.assertRaises(ValueError):
            ep.build_external_started(receipt, installation_ref="installation/fixture-tool",
                                      command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")

    def test_redacted_vision_ticket_projects(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        ticket = {"projection": "redacted", "status": "granted", "lease_id_sha256": "5" * 64, "front": "ViewPoint_REA",
                  "owner_pid": 40, "owner_starttime_ticks": 140, "grant_receipt_sha256": "6" * 64,
                  "released_after_stop": True, "release_receipt_sha256": "7" * 64}
        receipt = _receipt(perception_level="vision", perception_ceiling="vision", vision_pool_ticket=ticket,
                           process_bound=False)
        receipt["observation"]["producer"] = {"kind": "external_slot", "id": "rea.provider/v2", "process_bound": False}
        started = ep.build_external_started(receipt, installation_ref="installation/fixture-tool",
                                            command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")
        observed = ep.build_external_observed(
            receipt, started, installation_ref="installation/fixture-tool", scope=dict(_SCOPE),
            process={"pid": 41, "start_id": "host/start", "job_id": "host/job", "starttime_ticks": 156},
            launch_observation={"observer_id": "observer/launch", "observation_id": "launch/fixture",
                                "receipt_sha256": ep.record_digest({"attempt_id": "att-v03-0001", "pid": 41}),
                                "executable_sha256": _IMPL, "executed_path": "/fixture/stage/fixture-tool"},
            cleanup_retirement_sha256="f" * 64)
        self.assertTrue(validate(observed, schema))
        self.assertEqual(observed["observation"]["vision_pool_ticket"]["projection"], "redacted")
        self.assertNotIn("lease_id", observed["observation"]["vision_pool_ticket"])

    def test_restricted_ticket_denies_t06(self):
        ticket = {"projection": "restricted", "status": "granted", "lease_id_sha256": "5" * 64,
                  "lease_id": "lease-raw-secret", "front": "ViewPoint_REA"}
        receipt = _receipt(perception_level="vision", perception_ceiling="vision", vision_pool_ticket=ticket,
                           process_bound=False)
        receipt["observation"]["producer"] = {"kind": "host_mini_tool", "id": "host/compare", "process_bound": False}
        started = ep.build_external_started(receipt, installation_ref="installation/fixture-tool",
                                            command=dict(_COMMAND), scope=dict(_SCOPE), executor_id="host/rea-launcher")
        with self.assertRaises(ValueError):
            ep.build_external_observed(
                receipt, started, installation_ref="installation/fixture-tool", scope=dict(_SCOPE),
                process={"pid": 41, "start_id": "host/start", "job_id": "host/job", "starttime_ticks": 156},
                launch_observation={"observer_id": "observer/launch", "observation_id": "launch/fixture",
                                    "receipt_sha256": ep.record_digest({"attempt_id": "att-v03-0001", "pid": 41}),
                                    "executable_sha256": _IMPL, "executed_path": "/fixture/stage/fixture-tool"},
                cleanup_retirement_sha256="f" * 64)


class CompareBranch(unittest.TestCase):
    def _compare_receipt(self):
        receipt = _receipt(operation="compare", reference_episode_id="ep-ref-1", candidate_episode_id="ep-cand-1",
                           media={"reference": _media_side(), "candidate": _media_side()},
                           timeline={"reference": _timeline_side(), "candidate": _timeline_side()})
        receipt["observation"]["producer"] = {"kind": "host_mini_tool", "id": "host/compare", "process_bound": False}
        return receipt

    def test_real_comparator_mismatch_round_trips_with_check_id(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        index_schema = json.loads((FIXTURES / "rea_retention_index.v1.schema.json").read_bytes())
        for tool, comparison, field in (
            ("behaviour.compare", behaviour_comparison("off"), "case_id"),
            ("timeline.compare", event_comparison(candidate=timeline_side(event_trace((20, 147, 500), prefix="c"))), "event_id"),
        ):
            with self.subTest(field=field):
                verdict = evaluate(tool, comparison)["verdict"]
                self.assertEqual(verdict["result"], "mismatch")
                check, = [check for check in verdict["checks"] if check["outcome"] == "mismatch"]
                self.assertIn(field, check["first_divergence"])
                self.assertNotIn("check_id", check["first_divergence"])
                receipt = dict(self._compare_receipt(), verdict=verdict)
                bundle = ep.build_bundle(receipt, launched=True, installation_ref="installation/x", command=_COMMAND,
                                         scope=_SCOPE, reference_attempt_id="1" * 32, candidate_attempt_id="2" * 32,
                                         cleanup_state="unknown")
                self.assertTrue(validate(bundle["started"], schema))
                self.assertTrue(validate(bundle["terminal"], schema))
                self.assertTrue(validate(bundle["retention_index"], index_schema))
                self.assertEqual(bundle["terminal"]["verdict"]["first_divergence"],
                                 dict(check["first_divergence"], check_id=check["check_id"]))
                self.assertEqual(bundle["retention_index"]["profile_sha256"], ep.record_digest(receipt))

    def test_explicit_divergence_and_verdict_require_frozen_bounds(self):
        receipt = self._compare_receipt()
        policy = {"id": "policy/compare", "version": "v" * 64, "sha256": "b" * 64}
        divergence = {"check_id": "check/timeline", "occurrence": _SAFE, "delta_ms": -_SAFE}
        nominal = dict(receipt["verdict"], result="mismatch", policy=policy, first_divergence=divergence,
                       contrast_ids=[f"contrast/{i}" for i in range(128)],
                       negative_controls=[{"control_id": f"control/{i}"} for i in range(128)])
        receipt["verdict"] = nominal
        started = ep.build_compare_started(receipt, executor_id="host/compare", reference_attempt_id="1" * 32,
                                           candidate_attempt_id="2" * 32)
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        self.assertTrue(validate(ep.build_compare_observed(receipt, started, cleanup_state="unknown"), schema))
        for field, bad in (("check_id", None), ("event_id", "bad id"), ("case_id", 42), ("occurrence", -1),
                           ("frame_index", _SAFE + 1), ("reference_t_ms", False), ("candidate_t_ms", -1),
                           ("delta_ms", -_SAFE - 1), ("note", "x" * 2001)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"first_divergence.{field}"):
                ep.project_verdict({"verdict": dict(nominal, first_divergence=dict(divergence, **{field: bad}))})
        for changes, error in (
            ({"policy": dict(policy, version="")}, "policy.version"),
            ({"policy": dict(policy, version="v" * 65)}, "policy.version"),
            ({"contrast_ids": nominal["contrast_ids"] + ["contrast/128"]}, "contrast_ids"),
            ({"negative_controls": nominal["negative_controls"] + [{"control_id": "control/128"}]}, "negative_control_ids"),
            ({"independent_qualification": True}, "independent_qualification"),
            ({"result": "match", "independent_qualification": True, "oracle_independence": "derived_from_same_source"},
             "independent_qualification"),
        ):
            with self.subTest(error=error, fields=tuple(changes)), self.assertRaisesRegex(ValueError, error):
                ep.project_verdict({"verdict": nominal | changes})

    def test_compare_terminal_enforces_independent_observers(self):
        receipt = self._compare_receipt()
        started = ep.build_compare_started(receipt, executor_id="host/compare", reference_attempt_id="1" * 32,
                                           candidate_attempt_id="2" * 32)
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        unknown = ep.build_compare_observed(receipt, started, cleanup_state="unknown", cleanup_observer_id="host/compare")
        self.assertTrue(validate(unknown, schema))
        for field in ("artifact_observer_id", "cleanup_observer_id"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "independent"):
                ep.build_compare_observed(receipt, started, cleanup_retirement_sha256="f" * 64, **{field: "host/compare"})

    def test_compare_bundle_retains_started_request_digest(self):
        receipt = self._compare_receipt()
        pinned = hashlib.sha256(b'{"request_id":"req-v03-0001","tool":"behaviour.compare"}').hexdigest()
        start = ep.build_compare_started(receipt, executor_id="host/rea-launcher", reference_attempt_id="1" * 32,
                                         candidate_attempt_id="2" * 32, request_sha=pinned)
        self.assertNotEqual(pinned, ep.request_sha256(receipt["attempt"]["request_id"], receipt["attempt"]["attempt_id"]))
        with patch.object(ep, "build_compare_started", return_value=start):
            bundle = ep.build_bundle(receipt, launched=True, installation_ref="installation/x", command=_COMMAND,
                                     scope=_SCOPE, reference_attempt_id="1" * 32, candidate_attempt_id="2" * 32,
                                     cleanup_state="unknown")
        self.assertEqual(bundle["retention_index"]["request_sha256"], pinned)
        self.assertEqual(bundle["terminal"]["start_sha256"], ep.record_digest(start))

    def test_external_abort_rejects_decisive_compare_verdict(self):
        receipt = self._compare_receipt()
        receipt["verdict"]["result"] = "match"
        started = ep.build_external_started(receipt, installation_ref="installation/x", command=_COMMAND,
                                            scope=_SCOPE, executor_id="host/rea-launcher")
        with self.assertRaisesRegex(ValueError, "requires an abstain verdict"):
            ep.build_external_aborted(receipt, started, abort_reason=LIVE_EXECUTION_PARKED, cleanup_state="unknown")

    def test_compare_started_observed_validate(self):
        schema = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        receipt = self._compare_receipt()
        started = ep.build_compare_started(receipt, executor_id="host/compare",
                                           reference_attempt_id="1" * 32, candidate_attempt_id="2" * 32)
        self.assertTrue(validate(started, schema))
        observed = ep.build_compare_observed(receipt, started, cleanup_retirement_sha256="f" * 64)
        self.assertTrue(validate(observed, schema))
        self.assertEqual(observed["start_sha256"], ep.record_digest(started))
        self.assertEqual(observed["observation"]["producer"]["process_bound"], False)
        self.assertNotIn("execution", observed)
        self.assertNotIn("slot", observed)


class RetentionIndex(unittest.TestCase):
    def test_real_reconciliation_maps_to_prior_index_and_retains_original_digest(self):
        schema = json.loads((FIXTURES / "rea_retention_index.v1.schema.json").read_bytes())
        schema3 = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_bytes())
        for prior_id in ("att-v03-prior", "a" * 32):
            with self.subTest(prior_id=prior_id):
                prior = _receipt(attempt_id=prior_id, tool_version="versión")
                prior["attempt"]["control_outcome"] = "UNCERTAIN_OUTCOME"
                before = copy.deepcopy(prior)
                cleanup = {"bound_process_id": 41, "observed_process_id": 41,
                           "bound_starttime": 156, "observed_starttime": 156, "cleanup_observed": True,
                           "request_id": prior["attempt"]["request_id"], "provider_id": prior["slot"]["provider_id"],
                           "observed_sha256": _IMPL, "prior_attempt_id": prior_id,
                           "prior_receipt_sha256": receipt_digest(before)}
                with self.assertRaisesRegex(ValueError, "prior attempt_id"):
                    reconcile_run(prior, new_attempt_id="att-v03-rejected", request_id="req-v03-rejected",
                                  custody=dict(cleanup, prior_attempt_id="att-v03-foreign"))
                reconciled = reconcile_run(prior, new_attempt_id="att-v03-reconciled", request_id="req-v03-reconciled", custody=cleanup)
                self.assertEqual(prior, before)
                self.assertEqual(reconciled["attempt"]["reconciles_attempt_id"], prior_id)
                self.assertEqual(reconciled["attempt"]["host_artifact_sha256"], receipt_digest(before))
                prior_index = ep.build_retention_index(prior, profile_sha256=ep.record_digest(prior))
                bundle = ep.build_bundle(reconciled, launched=False, installation_ref="installation/x", command=_COMMAND,
                                         scope=_SCOPE, abort_reason="UNCERTAIN_OUTCOME", cleanup_state="unknown")
                index = bundle["retention_index"]
                self.assertTrue(validate(prior_index, schema))
                self.assertTrue(validate(index, schema))
                self.assertTrue(validate(bundle["started"], schema3))
                self.assertTrue(validate(bundle["terminal"], schema3))
                self.assertEqual(index["reconciles_attempt_id"], prior_index["attempt_id"])
                self.assertNotEqual(index["attempt_id"], index["reconciles_attempt_id"])

    def test_index_rejects_unsafe_object_bytes_and_unknown_side(self):
        receipt = _receipt()
        entry = {"object_ref": "object/result", "role": "provider_result", "bytes": _SAFE + 1, "sha256": "a" * 64}
        with self.assertRaisesRegex(ValueError, "index object bytes"):
            ep.build_retention_index(receipt, profile_sha256=ep.record_digest(receipt), objects=[entry])
        receipt["media"] = {"other": _media_side()}
        with self.assertRaisesRegex(ValueError, "media must name"):
            ep.build_retention_index(receipt, profile_sha256=ep.record_digest(receipt))

    def test_index_validates_and_binds_profile(self):
        schema = json.loads((FIXTURES / "rea_retention_index.v1.schema.json").read_text())
        receipt = _receipt(media={"reference": _media_side()}, timeline={"reference": _timeline_side()})
        bundle = ep.build_bundle(receipt, launched=False, installation_ref="installation/fixture-tool",
                                 command=dict(_COMMAND), scope=dict(_SCOPE),
                                 cleanup_retirement_sha256="f" * 64)
        index = bundle["retention_index"]
        self.assertTrue(validate(index, schema))
        profile_entry = [o for o in index["objects"] if o["role"] == "rea_profile"]
        self.assertEqual(len(profile_entry), 1)
        self.assertEqual(profile_entry[0]["sha256"], index["profile_sha256"])
        self.assertEqual(profile_entry[0]["sha256"], ep.record_digest(receipt))
        refs = [o["object_ref"] for o in index["objects"]]
        self.assertEqual(len(refs), len(set(refs)))
        schema3 = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        self.assertTrue(validate(bundle["started"], schema3))
        self.assertTrue(validate(bundle["terminal"], schema3))

    def test_duplicate_object_ref_denies(self):
        receipt = _receipt()
        objects = [{"object_ref": "object/x", "role": "cleanup", "bytes": 0, "sha256": "a" * 64},
                   {"object_ref": "object/x", "role": "cleanup", "bytes": 0, "sha256": "b" * 64}]
        with self.assertRaises(ValueError):
            ep.build_retention_index(receipt, profile_sha256=receipt_digest(receipt), objects=objects)

class RunnerIntegration(unittest.TestCase):
    """run_bound attaches a valid offline bundle; parked refusals abort."""

    def _fixture_script(self, path: Path):
        script = (
            b"#!/usr/bin/env python3\n"
            b"# SPDX-License-Identifier: AGPL-3.0-only\n"
            b"import sys, json\n"
            b"sys.stdin.buffer.read()\n"
            b"obj = {\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"e\" + \"f\"*63}\n"
            b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
            b"sys.stdout.flush()\n"
        )
        path.write_bytes(script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return sha256_of_file(path)

    def _manifest(self, impl, shared_ref):
        def _op(oid, role):
            return {"id": oid, "role": role, "input_ref": f"urn:rea.slot_request:v2#slot={role}",
                    "output_ref": f"urn:rea.slot_result:v2#slot={role}",
                    "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []},
                    "required_effects": ["process.spawn:child"], "required_grants": ["grant.exec.external_process"],
                    "limits": {"timeout_s": 5, "max_output_bytes": 4096},
                    "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"}}
        ops = [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")]
        return {"schema": "rea.slot_manifest/v2", "slot_id": "fixture-slot", "status": "registered",
                "upstream": {"name": "fixture", "source_url": "https://example.com/fixture", "version": "1.0.0",
                             "licence_id": "MIT", "licence_evidence_refs": ["fixture: licence"],
                             "component_notices": [], "user_installed": True},
                "provider_ref": {"schema": "rea.provider/v2", "provider_id": "fixture-slot",
                                 "provider_revision": "1.0.0", "provider_sha256": _PROVIDER_SHA,
                                 "implementation_sha256": impl},
                "contract_refs": [
                    {"role": "replay", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ,
                     "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _REQ, "execution_profile": "external_process"},
                    {"role": "verify", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ,
                     "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _REQ, "execution_profile": "external_process"}],
                "transport": {"kind": "cli_argv", "driver_location": "outside_capsule",
                              "executable_binding": "user_selected", "argv_prefix": [], "framing": "none",
                              "cwd_policy": "run_scratch", "env_allowlist": ["PATH"]},
                "operations": ops,
                "capabilities": {"target_kinds": ["fixture_target"], "platforms": ["linux"],
                                 "prerequisite_probes": [], "optional_features": [],
                                 "adapter_identity": {"adapter_id": "fixture.v1", "adapter_sha256": _ADAPTER, "provenance": "host_authored"}},
                "perception": {"required_level": "none", "optional_levels": [],
                               "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}]},
                "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"],
                                  "state_domain": "per_case", "effects": ["process.spawn:child"],
                                  "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]},
                "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1",
                               "coverage_requirements": {"channel": "error"}, "tolerance_units": "none",
                               "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}},
                "evidence": {"shared_schema_ref": shared_ref, "required_rea_fields": ["episode", "observation", "slot"],
                             "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt",
                             "visibility": "redacted_projection"},
                "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"],
                                "supported_entry_points": ["cli", "mcp", "json"],
                                "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"]},
                "discovery": {"summary": "fixture", "search_terms": ["fixture"],
                              "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []}}

    def test_live_fixture_run_carries_valid_v03_bundle(self):
        schema3 = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        index_schema = json.loads((FIXTURES / "rea_retention_index.v1.schema.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "fixture.py"
            impl = self._fixture_script(exe)
            manifest = self._manifest(impl, ep.V03_REF)
            binding = verify_binding(manifest, str(exe), operation_id="record",
                                     host_grants={"grant.exec.external_process"})
            self.assertEqual(binding.shared_schema_ref, ep.V03_REF)
            req, att = "req-v03-run-1", "att-v03-run-1"
            capability = _fixture_capability(binding, req, att)
            wrapper = run_bound(binding, [], timeout_s=5, cwd=str(directory),
                                request_id=req, attempt_id=att, live_authority=capability)
            denied = run_bound(binding, [], timeout_s=5, cwd=str(directory),
                               request_id=req + "b", attempt_id=att, live_authority=capability)
            self.assertEqual(denied["receipt"]["attempt"].get("control_outcome"), LIVE_EXECUTION_PARKED)
            self.assertIsNone(denied["pid"])
            self.assertIsNotNone(wrapper.get("pid"))
            bundle = wrapper.get("shared_records")
            self.assertIsNotNone(bundle, "live fixture run must carry v0.3 records")
            self.assertTrue(validate(bundle["started"], schema3))
            self.assertTrue(validate(bundle["terminal"], schema3))
            self.assertEqual(bundle["terminal"]["state"], "observed")
            self.assertTrue(validate(bundle["retention_index"], index_schema))
            self.assertEqual(bundle["retention_index"]["attempt_id"], bundle["started"]["attempt_id"])

    def test_parked_refusal_carries_aborted_bundle(self):
        schema3 = json.loads((FIXTURES / "attempt_evidence_record.v0.3.schema.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "fixture.py"
            impl = self._fixture_script(exe)
            manifest = self._manifest(impl, ep.V03_REF)
            binding = verify_binding(manifest, str(exe), operation_id="record",
                                     host_grants={"grant.exec.external_process"})
            wrapper = run_bound(binding, [], timeout_s=5, cwd=str(directory),
                                request_id="req-v03-park-1", attempt_id="att-v03-park-1")
            self.assertEqual(wrapper["receipt"]["attempt"]["control_outcome"], LIVE_EXECUTION_PARKED)
            bundle = wrapper.get("shared_records")
            self.assertIsNotNone(bundle, "parked refusal must carry aborted_before_spawn records")
            self.assertTrue(validate(bundle["started"], schema3))
            self.assertTrue(validate(bundle["terminal"], schema3))
            self.assertEqual(bundle["terminal"]["state"], "aborted_before_spawn")
            self.assertEqual(bundle["terminal"]["abort_reason"], LIVE_EXECUTION_PARKED)


if __name__ == "__main__":
    unittest.main()
