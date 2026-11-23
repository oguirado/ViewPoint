from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
VP = HERE.parents[2]
REPO = VP.parents[1]
sys.path[:0] = [str(VP), str(REPO / "agents"), str(REPO / "components/wender-agent")]

from control.core.control import CapabilityRegistry, Dispatcher, LaneIdentity, RequestStore
from metaviewer_agents.mw_owner import ReplayOwner
from recorder import compiler
from recorder.store.memoryweb_store import MemoryWebStore, StoreError, canonical_bytes
from recorder.timeline import TimelinePosition, TimelineRecorder
from tools import memoryweb_gate as gate
from wender.learn import mw_workflow
from wender.learn.script_live import LegacyDispatchOwner
from wender.scripts.types import AdmissionCheck, Reason, Truth


def digest(raw):
    return sha256(raw).hexdigest()


def presence(name):
    return dict(kind="state_signal", capability_id="mv.ax.present.v1",
                bindings=[dict(name="name", type="string", value=name)],
                evidence_refs=[], comparison="truth", snapshot_ref=None)


def target(name):
    anchor = "ax:" + name
    return dict(rung="T1", anchor_ref=anchor, offset_dx_px=0, offset_dy_px=0,
                consistency_tolerance_px=0, required_anchor_refs=[anchor],
                frame_kind="element", frame_predicate=presence(name), relative_xy_bps=None)


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=HERE, prefix=".compiler-")))
        self.store = MemoryWebStore(self.root)
        fixture = REPO / "memoryweb-builtin/sites/sample-chat"
        self.site = json.loads((fixture / "site.json").read_bytes())
        explanation = (fixture / "NOTES.md").read_bytes()
        self.owner = ReplayOwner(
            None,
            SimpleNamespace(root=self.root, site=self.site, intact=lambda: True, workflow={"execution_policy": self.site["execution_policy"]},
                            api=(gate, mw_workflow, LegacyDispatchOwner, AdmissionCheck, Truth, Reason)),
            self.root / "owner-result.json",
        )
        demand = json.loads((VP / "schemas/tests/examples/vp.demand_v1.valid.json").read_bytes())
        recorder = TimelineRecorder(surface_id="surface", capsule_id="capsule", incarnation="current",
                                    register=lambda _: nullcontext(), verify_ram_frame=lambda *_: False)
        with recorder.recording(demand):
            for button in (1, 3):
                recorder.add_demo(TimelinePosition(1_000_000, "capsule", "current"),
                                  {"mono_ns": 1_000_000, "kind": "click", "b": button})
            recorder.add_cdp(TimelinePosition(3_000_000, "capsule", "current"), text="synthetic-secret")
        self.timeline = recorder.snapshot()
        self.review = compiler.RecordingReview(
            timeline_sha256=digest(canonical_bytes(self.timeline)), demo_id="reviewed-demo",
            workflow_id="reviewed-flow", recorded_date="2026-11-17",
            surface=dict(viewport_px=[800, 600], ax_enabled=True, auth_boundary="anonymous"),
            explanation=explanation,
            evaluation_profile=dict(kind="initial_task_v1", fixture_ids=["sample-chat"],
                                    cohort_sha256=digest(explanation), baseline_workflow_sha256=None),
            clicks={
                0: compiler.ReviewedClick("click", 1, target("Résumé"), presence("Menu")),
                1: compiler.ReviewedClick("right_click", 3, target("Menu"), presence("Details")),
            }, navigation={},
        )

    def compile(self, timeline=None, review=None):
        return compiler.compile_recording(self.timeline if timeline is None else timeline,
                                          self.review if review is None else review, self.owner)

    def rebound(self, timeline, **changes):
        return replace(self.review, timeline_sha256=digest(canonical_bytes(timeline)), **changes)

    def assert_refused(self, code, call):
        with self.assertRaises(compiler.CompileError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_recompile_is_byte_stable_canonical_and_redacted(self):
        first = self.compile()
        self.assertEqual(first, self.compile(canonical_bytes(self.timeline)))
        for raw in (first.demo, first.demo_steps, first.workflow):
            self.assertEqual(raw, (gate.canon(json.loads(raw)) + "\n").encode())
            self.assertNotIn(b"synthetic-secret", raw)

    def test_replay_steps_preserve_reviewed_target_and_postcondition(self):
        workflow = json.loads(self.compile().workflow)
        steps = workflow["steps"]
        self.assertEqual([s["action"]["kind"] for s in steps], ["click", "right_click"])
        self.assertEqual([s["verify"]["recorded_gap_ms"] for s in steps], [0, 2])
        for index, step in enumerate(steps):
            compiler._validator("step").validate(step)
            click = self.review.clicks[index]
            self.assertEqual(step["target"], click.target)
            self.assertEqual(step["verify"]["predicate"], click.postcondition)
        self.assertEqual(workflow["oracle"]["postconditions"], [presence("Details")])

    def test_real_replay_owner_checks_support_without_running_the_workflow(self):
        with (patch.object(self.owner, "supports", wraps=self.owner.supports) as supports,
              patch.object(socket, "socket", side_effect=AssertionError("network access")),
              patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess access")),
              patch.object(self.owner, "sense", side_effect=AssertionError("live acquisition")),
              patch.object(self.owner, "send", side_effect=AssertionError("action dispatch"))):
            workflow = json.loads(self.compile().workflow)
            supports.assert_called_once_with(workflow)
            self.assertIs(self.owner.supports(workflow).result, Truth.TRUE)

    def test_sealed_u2a_timeline_bytes_bind_demo_provenance(self):
        artifact = self.root / "recording.json"
        artifact.write_bytes(canonical_bytes(self.timeline))
        result = self.compile(artifact.read_bytes())
        demo = json.loads(result.demo)
        self.assertEqual(demo["steps_sha256"], digest(result.demo_steps))
        self.assertEqual(demo["provenance"]["derived_from_sha256"], digest(artifact.read_bytes()))
        self.assertEqual(demo["events_summary"]["total_events"], len(self.timeline["events"]))

    def assert_other_review_is_refused(self):
        raw = canonical_bytes(self.timeline)
        wrong_review = replace(self.review, timeline_sha256=digest(raw + b"\n"))
        self.assert_refused("SCHEMA_GAP", lambda: self.compile(raw, wrong_review))

    def test_u2a_timeline_refuses_review_for_other_bytes(self):
        self.assert_other_review_is_refused()

    def test_mutant_ignoring_u2a_timeline_digest_is_killed(self):
        decode = compiler._decode_timeline

        def use_review_digest(raw):
            events, _ = decode(raw)
            return events, digest(raw + b"\n")

        with patch.object(compiler, "_decode_timeline", side_effect=use_review_digest) as mutant:
            with self.assertRaisesRegex(AssertionError, "CompileError not raised"):
                self.assert_other_review_is_refused()
        mutant.assert_called_once_with(canonical_bytes(self.timeline))

    def test_memoryweb_gate_validates_staged_demo_and_workflow(self):
        result = self.compile()
        site = self.root / "sites" / self.site["id"]
        demo = site / "demos" / self.review.demo_id
        workflow = site / "workflows" / (self.review.workflow_id + ".json")
        demo.mkdir(parents=True)
        workflow.parent.mkdir()
        (demo / "demo.json").write_bytes(result.demo)
        (demo / "steps.json").write_bytes(result.demo_steps)
        (demo / "EXPLANATION.md").write_bytes(result.explanation)
        workflow.write_bytes(result.workflow)
        bundle = json.loads((REPO / "memoryweb-builtin/standards/schemas/mw.v1.schema.json").read_bytes())
        context = gate.ValidationContext(self.root, capability_check=self.owner.capability_check,
                                         evidence_check=self.owner.evidence_check)
        for path in (demo / "demo.json", demo / "steps.json", workflow):
            with self.subTest(path=path.name):
                verdict = gate.validate_file(path, bundle, context=context)
                self.assertEqual(verdict.rc, 0, verdict)

    def test_publishes_only_immutable_candidate_through_dispatcher(self):
        cap = json.loads((VP / "schemas/tests/examples/vp.capability_v1.valid.json").read_bytes())
        cap.update(tool="vp_record_compile", cli="record-compile", json_op="vp_record_compile", risk_class="preparar")
        registry = CapabilityRegistry([cap])
        identity = LaneIdentity("agent", "fixture-compiler")
        bound = dict(session_id="s-fixture", capsule_id="capsule", incarnation="current", authority_epoch=7)
        allowed = dict(bound, caller=identity.caller, agent_instance=identity.agent_instance)

        def resolve(context):
            if context == allowed:
                return canonical_bytes(self.timeline), self.review, self.owner
            return None

        dispatcher = Dispatcher(
            registry, RequestStore(self.root / "requests.sqlite"),
            pace_risk=lambda *_: True, lease_epoch=lambda *_: True,
            tools={"vp_record_compile": compiler.vp_record_compile(resolve, self.store)},
        )
        preview = dispatcher.dispatch_mcp(
            "vp_record_compile", dict(bound, request_id="preview", dry_run=True), identity)
        self.assertEqual(preview["code"], "OK")
        self.assertEqual(preview["evidence"], [])
        self.assertIn("candidate_publication", preview["not_verified"])
        site = self.root / "sites" / self.site["id"]
        self.assertFalse(site.exists())
        blocked = dispatcher.dispatch_mcp("vp_record_compile",
            dict(bound, request_id="stale", dry_run=False, authority_epoch=6), identity)
        self.assertEqual(blocked["code"], "HOLD")
        self.assertFalse(site.exists())
        parameters = dict(bound, request_id="publish", dry_run=False)
        published = dispatcher.dispatch_mcp("vp_record_compile", parameters, identity)
        self.assertEqual(published["code"], "OK")
        self.assertEqual(published["result"], {})
        sha = self.compile().digests()["workflow_sha256"]
        self.assertEqual(len(published["evidence"]), 1)
        evidence = published["evidence"][0]
        self.assertEqual(evidence["sha256"], sha)
        self.assertEqual((self.root / evidence["path"]).read_bytes(), self.compile().workflow)
        read = self.store.load_workflow(self.site["id"], self.review.workflow_id, sha)
        self.assertEqual(read["workflow"]["status"], "draft")
        self.assertEqual(dispatcher.dispatch_mcp("vp_record_compile", parameters, identity), published)
        self.assertEqual(list((site / "revisions").iterdir()), [site / "revisions" / sha])
        self.assertFalse((site / "workflows").exists())
        self.assertFalse((site / "runs").exists())

    def test_navigate_is_only_a_reviewed_starting_condition(self):
        timeline = deepcopy(self.timeline)
        row = dict(timeline["events"][0], action="navigate", op="navigate", mono_ns=0)
        timeline["events"].insert(0, row)
        starting = presence("Fixture ready")
        clicks = {index + 1: click for index, click in self.review.clicks.items()}
        review = self.rebound(timeline, clicks=clicks, navigation={0: starting})
        workflow = json.loads(self.compile(timeline, review).workflow)
        self.assertEqual(workflow["preconditions"], [starting])
        self.assertEqual(workflow["trigger_sig"]["select_when"], [starting])
        self.assertEqual(len(workflow["steps"]), 2)
        self.assert_refused("OUT_OF_SUPPORT", lambda: self.compile(timeline, replace(review, navigation={})))
        row["mono_ns"] = 1_000_000
        timeline["events"][0], timeline["events"][1] = timeline["events"][1], row
        late = self.rebound(timeline, clicks={0: clicks[1], 2: clicks[2]}, navigation={1: starting})
        self.assert_refused("OUT_OF_SUPPORT", lambda: self.compile(timeline, late))

    def test_null_target_and_placeholder_capability_refused(self):
        clicks = dict(self.review.clicks)
        clicks[0] = replace(clicks[0], target=None)
        self.assert_refused("OUT_OF_SUPPORT", lambda: self.compile(review=replace(self.review, clicks=clicks)))
        self.site["engine_compat"][0]["required_capabilities"] = ["placeholder-capability"]
        self.assert_refused("OUT_OF_SUPPORT", self.compile)

    def test_unbound_review_mixed_identity_and_recording_gap_refused(self):
        self.assert_refused("SCHEMA_GAP", lambda: self.compile(review=replace(self.review, clicks={})))
        timeline = deepcopy(self.timeline)
        timeline["events"][-1]["incarnation"] = "retired"
        self.assert_refused("SCHEMA_GAP", lambda: self.compile(timeline))
        self.assert_refused("NO_LEASE", lambda: self.compile(timeline, self.rebound(timeline)))
        timeline["events"][-1].update(incarnation="current", gap=True)
        self.assert_refused("OUT_OF_SUPPORT", lambda: self.compile(timeline, self.rebound(timeline)))

    def test_partial_or_unsupported_action_cannot_be_dropped(self):
        for row in (dict(source="demo", op="text"),
                    dict(source="channel", op="act"),
                    dict(source="demo", op="key", action="key")):
            with self.subTest(row=row):
                timeline = deepcopy(self.timeline)
                timeline["events"][-1].update(row)
                self.assert_refused("OUT_OF_SUPPORT", lambda: self.compile(timeline, self.rebound(timeline)))

    def test_65537_bytes_refused(self):
        timeline = deepcopy(self.timeline)
        timeline["events"][-1]["op"] = "x"
        size = len(canonical_bytes(timeline))
        timeline["events"][-1]["op"] *= 65_537 - size + 1
        raw = canonical_bytes(timeline)
        self.assertEqual(len(raw), 65_537)
        self.assert_refused("ARTIFACT_OVERSIZE", lambda: self.compile(raw))
        self.assert_refused("ARTIFACT_OVERSIZE", lambda: self.compile(timeline))
        timeline["events"][-1]["op"] = timeline["events"][-1]["op"][:-1]
        allowed = canonical_bytes(timeline)
        self.assertEqual(len(allowed), 65_536)
        self.assertTrue(self.compile(allowed, self.rebound(timeline)).workflow)

    def assert_step_mutant_is_refused(self, change):
        original = compiler._workflow_step

        def mutant(*args):
            step = original(*args)
            change(step)
            return step

        with patch.object(compiler, "_workflow_step", side_effect=mutant) as activated:
            self.assert_refused("OUT_OF_SUPPORT", self.compile)
            self.assertGreater(activated.call_count, 0)

    def test_mutant_navigate_step_is_rejected(self):
        self.assert_step_mutant_is_refused(lambda step: step["action"].update(kind="navigate"))

    def test_mutant_null_target_is_rejected(self):
        self.assert_step_mutant_is_refused(lambda step: step.update(target=None))

    def test_mutant_unsorted_keys_is_rejected_by_store(self):
        def mutant(value):
            if value.get("schema") == "mw.workflow.v1":
                return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
            return canonical_bytes(value)

        expected = self.compile().workflow
        with patch.object(compiler, "canonical_bytes", side_effect=mutant) as activated:
            bad = self.compile().workflow
            self.assertTrue(any(call.args[0].get("schema") == "mw.workflow.v1" for call in activated.call_args_list))
        self.assertNotEqual(bad, expected)
        self.assertEqual(json.loads(bad), json.loads(expected))
        with self.assertRaises(StoreError) as refused:
            self.store.save_workflow(bad)
        self.assertEqual(refused.exception.code, "JSON_NONCANONICAL")

    def test_mutant_publish_as_default_is_rejected_before_write(self):
        original = compiler._draft

        def mutant(*args):
            return dict(original(*args), status="promoted")

        handler = compiler.vp_record_compile(lambda _: (self.timeline, self.review, self.owner), self.store)
        with patch.object(compiler, "_draft", side_effect=mutant) as activated:
            result = handler.execute(dict(context=dict(session_id="fixture"), dry_run=False))
        activated.assert_called_once()
        self.assertEqual(result.code, "OUT_OF_SUPPORT")
        self.assertFalse((self.root / "sites").exists())


if __name__ == "__main__":
    unittest.main()
