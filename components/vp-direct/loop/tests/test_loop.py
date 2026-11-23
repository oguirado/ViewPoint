"""Real MemoryWeb owner/runner and schema gates over a synthetic, offline backend."""
from contextlib import nullcontext
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic_ns
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
VP = HERE.parents[1]
REPO = VP.parents[1]
sys.path[:0] = [str(VP), str(REPO / "agents"), str(REPO / "components/wender-agent")]
from metaviewer_agents.mw_owner import ReplayOwner, Corpus
from tools import memoryweb_gate
from wender.learn import mw_workflow
from wender.learn.script_live import LegacyDispatchOwner
from wender.scripts.types import AdmissionCheck, Reason, Truth
from loop import LoopGoal, ProjectedOwners, ReplayLoop, replay
from rewind.machine import Refused, potential
from recorder.store.memoryweb_store import MemoryWebStore, canonical_bytes

spec = importlib.util.spec_from_file_location("owner_fixture", REPO / "agents/tests/test_mw_owner.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
API = (memoryweb_gate, mw_workflow, LegacyDispatchOwner, AdmissionCheck, Truth, Reason)


def example(name):
    return json.loads((VP / "schemas/tests/examples" / (name + "_v1.valid.json")).read_bytes())


def named(target, name):
    target = deepcopy(target)
    target["anchor_ref"] = "ax:" + name
    target["required_anchor_refs"] = [target["anchor_ref"]]
    target["frame_predicate"]["bindings"][0]["value"] = name
    return target


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir="/dev/shm")))
        self.base = fixture.make_checkout(self.root / "base", API)
        self.original = Corpus(self.base, "fixture/form", API).workflow
        self.original["steps"] = self.original["steps"][:2]
        self.good_target = deepcopy(self.original["steps"][1]["target"])
        self.original["steps"][1]["target"] = named(self.good_target, "Removed selector")
        for step in self.original["steps"]:
            step["locate_wait_ms"] = 70
        self.original["steps"].append(dict(deepcopy(self.original["steps"][0]), seq=3))
        self.original["oracle"]["postconditions"] = [self.original["steps"][2]["verify"]["predicate"]]
        self.store = MemoryWebStore(self.root)
        self.revision = self.store.save_workflow(canonical_bytes(self.original))["workflow_sha256"]
        binding = example("vp.surface_binding")
        binding["surface_id"] = "owned"
        point = example("vp.rewind_receipt")
        point.update(op="checkpoint", capsule_id="owned", goal_id="goal", last_good="p0", confirmed_tick=0)
        point["budget"].update(attempts_used=0, rewinds_used=0, attempts_limit=2)
        self.goal = LoopGoal.create(self.root / "goal.db", goal_id="goal", session_id="session",
            binding=binding, checkpoint=point, score=100, steps=3, duplicates=2, epsilon=10,
            action_quantum="0.01", observation_id="active", generation=1)
        self.life = SimpleNamespace(session_id="session", binding=binding, broker=Mock(),
            vp_perception_call=lambda _: dict(complete=True, to="ACTIVE", fresh_observation_id="active"),
            run_agent=lambda kind, obs, perform: perform())
        self.life.broker.human_needed.side_effect = lambda owner, session, goal, reason: dict(
            by="broker", rec="HUMAN_NEEDED", seq=1, goal_id=goal, session_id=session)
        self.plan = Mock(return_value=self.good_target)
        self.vision = Mock()
        self.vision.execute.side_effect = lambda request: SimpleNamespace(ok=True, code="OK",
            result={"observation_id": self.vision.analyze.return_value["observation_id"]})
        self.vision.analyze.return_value = dict(observation_id="active", plan="selector", changed=None)
        self.authorize = Mock(return_value="preparar")
        self.owner = SimpleNamespace(backend=SimpleNamespace())
        self.calls = []
        fixture.OwnerTests.install_backend(self)
        self.backend = self.owner.backend
        (self.base / "sites/fixture/workflows/form.json").write_bytes(canonical_bytes(self.original))
        parent = ReplayOwner(self.backend, Corpus(self.base, "fixture/form", API), self.root / "run.json",
            notice=dict(mode="replay", network="offline fixtures", acknowledged=True, mono_ns=monotonic_ns()))
        self.open_owner = ProjectedOwners(parent, self.root,
            {"EXPLANATION.md": (self.base / "sites/fixture/EXPLANATION.md").read_bytes()})
        self.session = dict(example("vp.session"), session_id="session", capsule_id="owned",
            display="xpra:7", incarnation=binding["incarnation"], lease=dict(holder=binding["owner"], authority_epoch=1))
        self.loop = ReplayLoop(self.goal, self.store, self.life, open_owner=self.open_owner,
            authorize=self.authorize, vision=self.vision, target_for=self.plan,
            assess=self.assess, checkpoint=self.checkpoint, restore=self.restore, session_for=lambda _: self.session,
            effect_witness=lambda s, step: step["verify"]["predicate"])
        self.loop.bind("fixture", "form", self.revision)

    def assess(self, s, evidence):
        return SimpleNamespace(score=200, scene_digest=evidence,
                               predicates=[SimpleNamespace(frame_not_blank=True)]), evidence

    def checkpoint(self, s):
        receipt = deepcopy(s["checkpoint"])
        receipt.update(op="checkpoint", state_sha256=s["pending"]["post_state_sha256"], last_good="p1",
                       confirmed_tick=1, scene_epoch=s["scene_epoch"], authority_epoch=s["authority_epoch"])
        receipt["budget"].update(attempts_used=s["t"], rewinds_used=s["r"])
        return receipt

    def restore(self, claim, s):
        receipt = deepcopy(s["checkpoint"])
        receipt.update(op="rewind", scene_epoch=s["scene_epoch"] + 1)
        receipt["budget"].update(attempts_used=s["t"], rewinds_used=s["r"])
        return receipt

    def restart(self):
        self.goal = LoopGoal(self.goal.path, self.goal.goal_id)
        self.loop.goal = self.goal
        self.assertEqual(self.goal.replay(), self.goal.status())

    def fail_second(self):
        self.assertEqual(self.loop.advance(), "VERIFIED")
        self.assertEqual(self.loop.advance(), "FAILED_STEP")
        self.assertEqual(self.goal.status()["loop"]["failed_step_seq"], 2)
        self.assertEqual(len(self.proposals), 1)

    def lose_reply(self, *, effect):
        call = self.backend.call
        def intercepted(stage, **kwargs):
            if stage == "propose":
                if effect:
                    call(stage, **kwargs)
                else:
                    self.calls.append("propose")
                raise TimeoutError
            return call(stage, **kwargs)
        with patch.object(self.backend, "call", intercepted):
            self.assertEqual(self.loop.advance(), "UNCERTAIN_OUTCOME")
        self.assertEqual(self.goal.status()["u"], 1)

    def assert_refused(self):
        with self.assertRaisesRegex(Refused, "NO_LEASE"):
            self.loop.advance()

    def test_repair_only_failed_step_resume_and_keep_candidate(self):
        self.fail_second()
        before = self.goal.status()
        self.restart()
        self.assertEqual(self.loop.advance(), "VERIFIED")
        after = self.goal.status()
        self.assertLess(potential(after), potential(before))
        self.assertEqual((after["i"], after["t"], after["r"], after["a"]), (2, 1, 0, 1))
        self.restart()
        self.assertEqual(self.loop.advance(), "VERIFIED")
        self.assertEqual(self.loop.advance(), "COMPLETE")
        self.assertEqual([p["x"] > 100 for p in self.proposals], [False, True, False])
        self.plan.assert_called_once()
        self.assertEqual(self.plan.call_args.args[0]["seq"], 2)
        candidate = self.store.load_workflow("fixture", "form", after["loop"]["revision"])["workflow"]
        expected = deepcopy(self.original)
        expected["steps"][1]["target"] = self.good_target
        self.assertEqual(candidate, expected)
        self.assertFalse((self.root / "sites/fixture/workflows").exists())

    def test_uncertain_verified_by_observation_only(self):
        self.lose_reply(effect=True)
        self.restart()
        self.assertEqual(self.loop.advance(), "VERIFIED")
        self.assertEqual(self.calls.count("propose"), 1)
        self.assertEqual((self.goal.status()["i"], self.goal.status()["t"]), (1, 0))
        self.vision.analyze.assert_not_called()

    def test_unknown_effect_escalates_without_resend_or_repair(self):
        self.lose_reply(effect=False)
        self.restart()
        self.assertEqual(self.loop.advance(), "HUMAN_NEEDED")
        self.assert_refused()
        self.vision.analyze.assert_not_called()
        self.assertEqual(self.calls.count("propose"), 1)
        self.assertEqual(self.goal.status()["t"], 1)
        self.life.broker.human_needed.assert_called_once()

    def test_positive_no_effect_required_and_risk_fenced(self):
        self.lose_reply(effect=False)
        # The independent fixture transport witnessed cancellation before
        # delivery. Its positive status marker is scoped to this exact claim.
        call = self.backend.call
        def with_cancellation(stage, **kwargs):
            result = call(stage, **kwargs)
            if stage == "ax":
                result["nodes"].append(dict(name="Dispatch cancelled"))
            if stage == "state":
                status = dict(result["nodes"][0], id="0000000000000011", name="Dispatch cancelled",
                              actionable=False, ordinal=17)
                result["nodes"].append(status)
                result["counts"]["nodes"] += 1
                result["counts"]["raw"] += 1
            return result
        self.backend.call = with_cancellation
        def cancellation(state, step):
            self.assertEqual((state["loop"]["claim"]["revision"], step["seq"]), (self.revision, 1))
            return named(step["target"], "Dispatch cancelled")["frame_predicate"]
        self.loop.no_effect = cancellation
        self.assertEqual(self.loop.advance(), "FAILED_STEP")
        self.assertEqual(self.goal.status()["loop"]["failed_step_seq"], 1)
        self.assertEqual(self.calls.count("propose"), 1)

    def test_high_risk_no_effect_cannot_repair(self):
        self.authorize.return_value = "enviar"
        self.lose_reply(effect=False)
        self.loop.no_effect = Mock()
        self.assertEqual(self.loop.advance(), "HUMAN_NEEDED")
        self.loop.no_effect.assert_not_called()

    def test_unrepairable_stops_and_notifies(self):
        self.fail_second()
        self.plan.return_value = None
        self.assertEqual(self.loop.advance(), "HARD_STOP_ESCALATED")
        self.restart()
        self.assert_refused()
        self.life.broker.human_needed.assert_called_once_with(
            self.goal.status()["owner"], "session", "goal", "stuck")

    def test_rejected_repair_rewinds_and_exhausts(self):
        self.fail_second()
        self.vision.analyze.return_value["changed"] = None
        self.plan.return_value = named(self.good_target, "invalid alternative")
        self.assertEqual(self.loop.advance(), "FAILED_STEP")
        self.assertEqual(self.goal.status()["i"], 1)
        self.vision.analyze.return_value["changed"] = False
        self.plan.return_value = named(self.good_target, "another invalid alternative")
        self.assertEqual(self.loop.advance(), "REWINDING")
        self.assertEqual(self.loop.advance(), "READY")
        self.assertEqual(self.loop.advance(), "FAILED_STEP")
        self.assertEqual(self.loop.advance(), "HARD_STOP_ESCALATED")
        self.assertEqual((self.goal.status()["t"], self.goal.status()["r"], self.goal.status()["i"]), (2, 1, 1))
        self.restart()
        self.assert_refused()

    def test_repeated_selector_charges_duplicates_without_dispatch(self):
        self.fail_second()
        self.plan.return_value = self.original["steps"][1]["target"]
        for _ in range(2):
            self.assertEqual(self.loop.advance(), "BRANCH_ALREADY_FAILED")
        self.assertEqual(len(self.proposals), 1)
        self.assertEqual(self.goal.status()["d"], 2)
        self.assertEqual(self.loop.advance(), "HARD_STOP_ESCALATED")

    def test_lost_claim_and_interrupted_reconciliation(self):
        with patch.object(replay, "run_one", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.loop.advance()
        self.restart()
        with patch.object(self.loop, "open_owner", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.loop.advance()
        self.restart()
        self.assertEqual(self.loop.advance(), "HUMAN_NEEDED")
        self.assertFalse(self.proposals)

    def test_concurrent_caller_cannot_recover_an_active_flight(self):
        original = replay.run_one
        def contended(owner, doc, surface):
            with self.assertRaisesRegex(Refused, "HOLD"):
                self.loop.advance()
            return original(owner, doc, surface)
        with patch.object(replay, "run_one", contended):
            self.assertEqual(self.loop.advance(), "VERIFIED")
        self.assertEqual(len(self.proposals), 1)

    def test_sleep_wake_and_real_vision_plan_for_failed_step(self):
        from vision.tests import test_vision
        fixture_mode = test_vision.ModeTests()
        self.addCleanup(fixture_mode.doCleanups)
        samples = test_vision.example
        def scoped(name):
            row = samples(name)
            if "surface_id" in row:
                row["surface_id"] = "owned"
            return row
        with patch.object(test_vision, "example", scoped):
            fixture_mode.setUp()
        observation = fixture_mode.lifecycle.vp_perception_call(
            dict(op="wake", session_id="session", reason="fixture"))["fresh_observation_id"]
        node = dict(name="Submit Greeting", actionable=True, id="second")
        fixture_mode.planner.plan_step.side_effect = lambda *args: (
            dict(type="click", button="left"), {}, dict(nodes=[node], actionable=["second"]), node)
        self.plan.side_effect = replay.selector_from_plan
        state = self.goal.status()
        goal = LoopGoal.create(self.root / "integrated.db", goal_id="goal", session_id="session",
            binding=fixture_mode.binding, checkpoint=state["checkpoint"], score=100, steps=3,
            duplicates=2, epsilon=10, action_quantum="0.01", observation_id=observation, generation=1)
        fixture_mode.lifecycle.bind_goal(goal)
        self.goal = self.loop.goal = goal
        self.loop.lifecycle = fixture_mode.lifecycle
        self.loop.vision = fixture_mode.mode
        self.loop.bind("fixture", "form", self.revision)
        self.fail_second()
        def perception(op):
            return fixture_mode.lifecycle.vp_perception_call(dict(op=op, reason="fixture", session_id="session"))
        before_sleep = potential(goal.status())
        self.assertEqual(perception("sleep")["to"], "ASLEEP")
        asleep = potential(goal.status())
        self.assertLess(asleep, before_sleep)
        self.assertEqual((goal.status()["a"], goal.status()["q"]), (0, 1))
        self.assert_refused()
        self.assertNotEqual(perception("wake")["fresh_observation_id"], observation)
        self.assertLess(potential(goal.status()), asleep)
        self.assertEqual(self.loop.advance(), "VERIFIED")
        self.assertEqual(fixture_mode.planner.plan_step.call_count, 1)
        self.assertIsNone(self.plan.call_args.args[1]["changed"])
        self.assertEqual(self.goal.replay()["i"], 2)

    def test_human_take_freezes_budget_and_requires_new_epoch(self):
        self.fail_second()
        before = self.goal.status()
        lease = example("vp.human_lease")
        lease.update(session_id="session", state="HUMAN", epoch_before=1, epoch_after=2, outstanding=[])
        self.goal.human_lease(dict(rec="control", by="broker", op="take", lease=lease))
        self.assert_refused()
        self.assertEqual(potential(before), potential(self.goal.status()))
        lease.update(state="AGENT", epoch_before=2, epoch_after=3)
        self.goal.human_lease(dict(rec="control", by="broker", op="return", lease=lease),
                              observation_id="after-human", generation=2)
        self.assert_refused()  # Stale P4 binding.
        self.life.binding["authority_epoch"] = 3
        self.session["lease"]["authority_epoch"] = 3
        self.life.vp_perception_call = lambda _: dict(complete=True, to="ACTIVE", fresh_observation_id="after-human")
        self.vision.analyze.return_value["observation_id"] = "after-human"
        self.assertEqual(self.loop.advance(), "VERIFIED")

    def test_missing_effect_witness_leaves_even_satisfied_predicate_unresolved(self):
        self.lose_reply(effect=True)
        self.loop.effect_witness = None
        self.assertEqual(self.loop.advance(), "HUMAN_NEEDED")
        self.assertEqual(self.goal.status()["i"], 0)

    def test_score_equality_rejects_candidate_and_keeps_prior_revision(self):
        self.fail_second()
        def equal(s, evidence):
            quality, digest = self.assess(s, evidence)
            quality.score = s["best"] + s["epsilon"]
            return quality, digest
        self.loop.assess = equal
        self.loop.checkpoint = Mock(side_effect=AssertionError("mark before strict score gate"))
        self.assertEqual(self.loop.advance(), "JUDGE_REJECT")
        self.assertEqual(self.goal.replay()["loop"]["revision"], self.revision)
        self.assertEqual([p.name for p in (self.root / "sites/fixture/revisions").iterdir()], [self.revision])

    def test_mutant_reset_budgets_on_recompile(self):
        from loop import goal
        self.fail_second()
        fold = goal.apply_loop
        def forget(state, op, row):
            result = fold(state, op, row)
            if op == "verified" and "checkpoint" in row:
                state["t"] = 0
            return result
        with patch.object(goal, "apply_loop", forget):
            self.assertEqual(self.loop.advance(), "VERIFIED")
        with self.assertRaises(AssertionError):
            self.assertEqual(self.goal.status()["t"], self.plan.call_count)

    def test_mutant_duplicate_dispatch_after_uncertainty(self):
        self.lose_reply(effect=False)
        observe_only = self.loop._reconcile
        def resend(state, workflow):
            doc = replay.projection(workflow, state["i"])
            with self.open_owner(doc) as owner:
                self.assertEqual(replay.run_one(owner, doc, state["surface_id"]), "VERIFIED")
            return observe_only(state, workflow)
        with patch.object(self.loop, "_reconcile", resend):
            self.loop.advance()
        self.assertEqual(len(self.proposals), 1)  # The mutant reached the real offline transport.
        with self.assertRaises(AssertionError):
            self.assertEqual(self.calls.count("propose"), 1)

    def test_mutant_repair_after_uncertain_send(self):
        self.loop.no_effect = None
        self.lose_reply(effect=False)
        def unproved(state, workflow):
            self.goal.transition(state, "read")
            return self.goal.transition(self.goal.status(), "failed", seq=1)
        with patch.object(self.loop, "_reconcile", unproved):
            self.assertEqual(self.loop.advance(), "FAILED_STEP")
        self.loop.advance()
        self.assertEqual(self.plan.call_count, 1)
        with self.assertRaises(AssertionError):
            self.plan.assert_not_called()

    def test_mutant_auto_promote(self):
        self.fail_second()
        save = self.store.save_workflow
        default = self.root / "sites/fixture/workflows/form.json"
        def promote(raw):
            result = save(raw)
            default.parent.mkdir()
            default.write_bytes(raw)
            return result
        with patch.object(self.store, "save_workflow", promote):
            self.assertEqual(self.loop.advance(), "VERIFIED")
        self.assertEqual(replay.pin(json.loads(default.read_bytes())), self.goal.status()["loop"]["revision"])
        with self.assertRaises(AssertionError):
            self.assertFalse(default.exists())

    def test_timeout_cannot_verify(self):
        self.lose_reply(effect=False)
        with patch.object(replay, "semantics", side_effect=TimeoutError):
            self.assertEqual(self.loop.advance(), "HUMAN_NEEDED")
        self.assertEqual(self.goal.replay()["i"], 0)
        self.vision.analyze.assert_not_called()

    def test_mutant_timeout_as_pass(self):
        self.lose_reply(effect=False)
        def credit_timeout(*args, **kwargs):
            try:
                raise TimeoutError
            except TimeoutError:
                return "c" * 64
        with patch.object(replay, "semantics", side_effect=credit_timeout) as reached:
            self.assertEqual(self.loop.advance(), "VERIFIED")
        reached.assert_called_once()
        self.assertIsNone(self.focused)
        with self.assertRaises(AssertionError):
            self.assertEqual(self.goal.status()["i"], 0)

    def test_handoff_delivery_recovery_does_not_repeat_broker_event(self):
        self.lose_reply(effect=False)
        with patch.object(self.goal, "deliver_handoff", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.loop.advance()
        self.restart()
        self.assert_refused()
        for _ in range(2):
            self.assertEqual(self.goal.deliver_handoff(self.life.broker), 1)
        self.life.broker.human_needed.assert_called_once()

    def test_interrupted_handoff_delivery_is_fenced(self):
        self.lose_reply(effect=False)
        self.life.broker.human_needed.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            self.loop.advance()
        self.restart()
        with self.assertRaisesRegex(Refused, "UNCERTAIN_OUTCOME"):
            self.goal.deliver_handoff(self.life.broker)
        self.life.broker.human_needed.assert_called_once()

    def test_failed_restore_escalates_and_keeps_external_effect_frontier(self):
        self.fail_second()
        self.plan.return_value = named(self.good_target, "No such control")
        self.assertEqual(self.loop.advance(), "FAILED_STEP")
        self.plan.return_value = self.good_target
        self.assertEqual(self.loop.advance(), "REWINDING")
        self.loop.restore = lambda claim, s: dict(self.restore(claim, s), digest_match=False)
        self.assertEqual(self.loop.advance(), "HARD_STOP_ESCALATED")
        state = self.goal.replay()
        self.assertEqual(state["reason"], "RESTORE_MISMATCH")
        self.assertEqual(state["checkpoint"]["external_effect_frontier"], "UNREVERTED_HOST_NET_JOURNAL")
        self.assert_refused()
        self.assertEqual(len(self.proposals), 1)

    def test_no_matching_button_or_unique_name_means_no_repair(self):
        step = self.original["steps"][1]
        node = dict(name="Submit Greeting", actionable=True, id="id")
        state = dict(nodes=[node, dict(node, id="alias")], actionable=["id", "alias"])
        plan = [dict(type="click", button="left"), {}, state, node]
        self.assertIsNone(replay.selector_from_plan(step, {"plan": plan}))
        state["nodes"].pop()
        plan[0]["button"] = "right"
        self.assertIsNone(replay.selector_from_plan(step, {"plan": plan}))

    def test_operator_display_refused_before_replay_or_perception(self):
        self.session["display"] = ":0"
        with self.assertRaisesRegex(Refused, "DISPLAY_UNSUPPORTED"):
            self.loop.advance()
        self.vision.execute.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_session_changed_after_admission_cannot_reach_submit(self):
        def switch_display(*_):
            self.session["display"] = ":0"
            return "preparar"
        self.authorize.side_effect = switch_display
        self.assertEqual(self.loop.advance(), "UNCERTAIN_OUTCOME")
        self.assertNotIn("propose", self.calls)

    def test_lost_checkpoint_reply_recovers_without_marking_or_sending_twice(self):
        self.fail_second()
        self.loop.checkpoint = Mock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            self.loop.advance()
        self.restart()
        with self.assertRaisesRegex(Refused, "UNCERTAIN_OUTCOME"):
            self.loop.advance()
        receipt = self.checkpoint(self.goal.status())
        with self.assertRaisesRegex(Refused, "CKPT_CORRUPT"):
            self.loop.finish_checkpoint(dict(receipt, state_sha256="a" * 64))
        self.assertEqual(self.loop.finish_checkpoint(receipt), "VERIFIED")
        self.loop.checkpoint.assert_called_once()
        self.assertEqual(len(self.proposals), 2)
        self.assertEqual(self.goal.replay()["i"], 2)

    def test_replay_revision_from_u2b_compiler(self):
        from recorder import compiler
        from recorder.timeline import TimelinePosition, TimelineRecorder

        original = self.original["steps"][0]
        recorder = TimelineRecorder(surface_id="owned", capsule_id="owned", incarnation="inc",
                                   register=lambda _: nullcontext(), verify_ram_frame=lambda *_: False)
        with recorder.recording(dict(example("vp.demand"), surface_id="owned")):
            for n in range(3):
                now = (n + 1) * 1_000_000
                recorder.add_demo(TimelinePosition(now, "owned", "inc"), dict(mono_ns=now, kind="click", b=1))
        timeline = recorder.snapshot()
        explanation = self.open_owner.sources[Path("EXPLANATION.md")]
        click = compiler.ReviewedClick("click", 1, original["target"], original["verify"]["predicate"])
        review = compiler.RecordingReview(
            timeline_sha256=replay.pin(timeline), demo_id="observed", workflow_id="form",
            recorded_date="2026-11-17", surface=dict(viewport_px=[640, 360], ax_enabled=True,
            auth_boundary="anonymous"), explanation=explanation, evaluation_profile=self.original["evaluation_profile"],
            clicks=dict.fromkeys(range(3), click), navigation={})
        compilation = compiler.compile_recording(timeline, review, self.open_owner.parent)
        sources = dict(self.open_owner.sources)
        sources["demos/observed/EXPLANATION.md"] = explanation
        self.loop.open_owner = ProjectedOwners(self.open_owner.parent, self.root, sources)
        state = self.goal.status()
        self.loop.goal = LoopGoal.create(self.root / "compiled.db", goal_id="goal", session_id="session",
            binding=self.life.binding, checkpoint=state["checkpoint"], score=100, steps=3, duplicates=2,
            epsilon=10, action_quantum="0.01", observation_id="active", generation=1)
        revision = self.store.save_workflow(compilation.workflow)["workflow_sha256"]
        self.loop.bind("fixture", "form", revision)
        self.assertEqual([self.loop.advance() for _ in range(4)], ["VERIFIED"] * 3 + ["COMPLETE"])
        self.assertEqual(self.loop.goal.replay()["t"], 0)
        self.assertEqual(len(self.proposals), 3)
