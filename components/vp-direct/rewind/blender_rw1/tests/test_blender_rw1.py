from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


DIRECT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(DIRECT), str(DIRECT.parent / "blender-visual-qa"),
                str(DIRECT / "vendor" / "broker")]

from data.locator import ArtifactLocator
from rewind import blender_rw1 as rewind
from rewind.machine import GoalMachine, Refused
from schemas.validation import canonical_bytes, validate_contract
from vendor.rewind import fakes
from vqa3d_bridge import QualityResult, RenderPredicate


def fixture_sha(label):
    return sha256(label.encode()).hexdigest()


class BlenderRewindTests(unittest.TestCase):
    backend_class = rewind.BlenderRewind

    def setUp(self):
        tmp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cap = fakes.Capsule(rt=str(self.root / "runtime"))
        self.addCleanup(self.cap.broker.close)
        self.addCleanup(self.cap.stop_bridge)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.locator = ArtifactLocator({"fixture": self.artifacts})
        self.inputs = (self.artifact("inputs", "admitted reference and manifest"),)
        self.ops = fakes.FakeOps()
        self.binding = rewind.RuntimeBinding(7, fixture_sha("rw1 runtime"), fixture_sha("effects"))
        self.backend = self.new_backend()
        self.budget = dict(attempts_used=0, attempts_limit=10, rewinds_used=0, rewinds_limit=3)

    def new_backend(self):
        return self.backend_class(
            self.cap.broker, self.ops, capsule_id="capsule-fixture", goal_id="goal-fixture",
            admitted_inputs=self.inputs, binding=lambda: self.binding,
            compatibility_sha256=self.binding.compatibility_sha256, resolve_artifact=self.locator.resolve,
        )

    def artifact(self, name, content):
        (self.artifacts / name).write_text(content, encoding="utf-8")
        return self.locator.put(dict(schema="vp.artifact_ref/v1", locator=f"af://fixture/{name}",
                                     sha256=fixture_sha(content), bytes=len(content.encode()),
                                     media_type="text/plain", stage=name, copy="none"))

    def mark(self, tick=0, *, refs=None, accepted=True, **overrides):
        def score_checkpoint(blend_sha256):
            path = Path(self.cap.rt) / "ckpt" / f"p{tick}.blend"
            self.assertEqual(sha256(path.read_bytes()).hexdigest(), blend_sha256)
            return QualityResult(fixture_sha(blend_sha256 + "render"), 50 + tick,
                                 (RenderPredicate("render/frame_000000.png", accepted),))

        args = dict(by="broker", scene_epoch=1, authority_epoch=7, tick=tick, frame=2,
                    artifact_refs=self.inputs if refs is None else refs,
                    score_checkpoint=score_checkpoint, budget=dict(self.budget, attempts_used=tick))
        return self.backend.mark(**(args | overrides))

    def restore(self, point="p0", **overrides):
        args = dict(by="broker", scene_epoch=1, authority_epoch=7,
                    budget=dict(self.budget, rewinds_used=1))
        return self.backend.restore(point, **(args | overrides))

    def refused(self, code, function, *args, **kwargs):
        with self.assertRaises(rewind.RewindError) as result:
            function(*args, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_restores_scene_artifacts_and_acceptance_from_same_stage(self):
        base = self.mark(accepted=False)
        self.cap.scene["col:PROPS"].append("repair")
        stage_refs = (self.artifact("repair", "new mesh"),)
        stage = self.mark(1, refs=stage_refs)
        self.assertNotEqual(base.checkpoint.quality, stage.checkpoint.quality)
        self.assertTrue(stage.checkpoint.quality["accepted"])
        self.assertEqual(stage.checkpoint.artifact_refs, stage_refs)
        result = self.restore()
        validate_contract("vp.rewind_receipt/v1", result.receipt)
        self.assertEqual(self.cap.digest(), base.checkpoint.record["digest"])
        self.assertEqual(result.checkpoint, base.checkpoint)
        self.assertEqual(result.checkpoint.artifact_refs, self.inputs)
        self.assertEqual(result.checkpoint.quality["score"], 50)
        self.assertFalse(result.checkpoint.quality["accepted"])
        self.assertEqual(result.receipt["state_sha256"], base.receipt["state_sha256"])
        self.assertEqual(result.receipt["scene_epoch"], 2)
        self.assertEqual(result.receipt["last_good"], "p1")
        self.assertEqual(result.receipt["confirmed_tick"], stage.receipt["confirmed_tick"])
        self.assertEqual(result.receipt["external_effect_frontier"], "UNREVERTED_HOST_NET_JOURNAL")
        self.assertIs(result.receipt["digest_match"], True)
        self.assertEqual(self.backend.current(), result.checkpoint)
        second = self.restore("p1", scene_epoch=2, budget=dict(self.budget, rewinds_used=2))
        self.assertEqual(second.receipt["scene_epoch"], 3)
        self.assertEqual(second.checkpoint, stage.checkpoint)
        records = self.cap.events()
        self.assertEqual(rewind.rw.replay(records).violations, [])

    def test_agent_cannot_mark_or_restore(self):
        self.refused("NO_LEASE", self.mark, by="agent")
        self.mark()
        with patch.object(self.cap.broker, "_dispatch_script", wraps=self.cap.broker._dispatch_script) as dispatch:
            self.refused("NO_LEASE", self.restore, by="agent")
        dispatch.assert_not_called()

    def test_p0_requires_the_exact_admitted_artifact_set(self):
        other = self.artifact("substitute", "other admitted root, wrong input")
        with patch.object(self.cap.broker, "_dispatch_script", wraps=self.cap.broker._dispatch_script) as dispatch:
            self.refused("RESTORE_MISMATCH", self.mark, refs=(other,))
        dispatch.assert_not_called()
        self.assertEqual(self.mark().checkpoint.artifact_refs, self.inputs)

    def fill_ring(self):
        self.mark()
        for tick in range(1, rewind.rw.RING_SLOTS):
            self.cap.scene["col:PROPS"].append(tick)
            self.mark(tick)

    def assert_ring_retains_points(self):
        paths = [Path(self.cap.rt) / "ckpt" / f"{point}.blend" for point in self.backend.points()]
        before = {path: path.read_bytes() for path in paths}
        last_good = self.backend.current()
        with patch.object(self.cap.broker, "_dispatch_script", wraps=self.cap.broker._dispatch_script) as dispatch:
            self.refused("CKPT_BUDGET", self.mark, rewind.rw.RING_SLOTS)
        dispatch.assert_not_called()
        self.assertEqual(self.backend.points(), tuple(f"p{i}" for i in range(rewind.rw.RING_SLOTS)))
        self.assertEqual(self.backend.current(), last_good)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(set((Path(self.cap.rt) / "ckpt").iterdir()), set(paths))

    def test_ninth_mark_refuses_and_keeps_p0_and_last_good(self):
        self.fill_ring()
        self.assert_ring_retains_points()

    def test_mutant_evict_p0_breaks_the_ring_oracle(self):
        self.fill_ring()
        path = Path(self.cap.rt) / "ckpt" / "p0.blend"
        mark = self.backend.mark

        def evict_on_refusal(**kwargs):
            try:
                return mark(**kwargs)
            except rewind.RewindError as exc:
                if exc.code == "CKPT_BUDGET":
                    self.backend._points.pop("p0")
                    path.unlink()
                raise

        self.assertTrue(path.exists())
        with patch.object(self.backend, "mark", side_effect=evict_on_refusal) as mutant:
            with self.assertRaises(AssertionError):
                self.assert_ring_retains_points()
            mutant.assert_called_once()
        self.assertFalse(path.exists())

    def test_corrupt_blend_refuses_without_dispatch(self):
        self.mark()
        (Path(self.cap.rt) / "ckpt" / "p0.blend").write_bytes(b"other scene")
        with patch.object(self.cap.broker, "_dispatch_script", wraps=self.cap.broker._dispatch_script) as dispatch:
            self.refused("CKPT_CORRUPT", self.restore)
        dispatch.assert_not_called()

    def test_artifact_effect_frontier_and_stale_epochs_refuse_before_load(self):
        self.mark()
        with patch.object(self.cap.broker, "_dispatch_script", wraps=self.cap.broker._dispatch_script) as dispatch:
            self.refused("RESTORE_MISMATCH", self.restore, scene_epoch=0)
            self.refused("RESTORE_MISMATCH", self.restore, authority_epoch=6)
            with patch.object(self, "binding", replace(self.binding, effect_frontier_sha256=fixture_sha("advanced"))):
                self.refused("RESTORE_MISMATCH", self.restore)
            (self.artifacts / "inputs").write_text("substituted source", encoding="utf-8")
            self.refused("RESTORE_MISMATCH", self.restore)
        dispatch.assert_not_called()

    def test_effect_frontier_change_after_load_blocks_publication(self):
        self.mark()
        self.cap.scene["col:PROPS"].append("edited")

        def drift_during_load():
            self.binding = replace(self.binding, effect_frontier_sha256=fixture_sha("new effect"))
            return fakes.digest_script()

        with patch.object(self.ops, "digest_script", side_effect=drift_during_load) as probe:
            self.refused("RESTORE_MISMATCH", self.restore)
        probe.assert_called_once()
        self.assertIsNone(self.backend.current())
        self.assertEqual(self.cap.broker.mode, "FROZEN")

    def test_artifact_change_after_load_blocks_publication(self):
        self.mark()
        self.cap.scene["col:PROPS"].append("edited")

        def replace_during_readback():
            (self.artifacts / "inputs").write_text("different source", encoding="utf-8")
            return fakes.digest_script()

        with patch.object(self.ops, "digest_script", side_effect=replace_during_readback) as probe:
            self.refused("RESTORE_MISMATCH", self.restore)
        probe.assert_called_once()
        self.assertIsNone(self.backend.current())

    def test_unbounded_restore_refuses_before_dispatch(self):
        self.mark()
        with patch.object(self.cap.broker, "job_cap_s", rewind.rw.RESTORE_CAP_S + 1), \
                patch.object(self.cap.broker, "_dispatch_script", wraps=self.cap.broker._dispatch_script) as dispatch:
            self.refused("RESTORE_TIMEOUT", self.restore)
        dispatch.assert_not_called()

    def test_wrong_scene_readback_stops_and_still_bumps_epoch(self):
        self.mark()
        self.cap.scene["col:PROPS"].append("unrestored")
        with patch.object(self.ops, "restore_script", side_effect=fakes.noop_restore_script):
            self.refused("RESTORE_MISMATCH", self.restore)
        self.assertIsNone(self.backend.current())
        self.assertFalse(self.cap.events(rec="control", op="rewind")[-1]["digest_match"])
        self.assertEqual(self.cap.events(rec="control", op="rewind")[-1]["scene_epoch_after"], 2)
        self.assertEqual(self.cap.broker.mode, "FROZEN")
        self.refused("NO_LEASE", self.restore, scene_epoch=2)
        recovered = self.restore(by="director", scene_epoch=2)
        self.assertEqual(recovered.receipt["scene_epoch"], 3)
        self.assertEqual(self.cap.broker.mode, "FROZEN")

    def test_mutant_skip_digest_match_breaks_scene_oracle(self):
        base = self.mark()
        self.cap.scene["col:PROPS"].append("unrestored")
        with patch.object(self.ops, "restore_script", side_effect=fakes.noop_restore_script), \
                patch.object(rewind.rw, "digest_match", return_value=True) as mutant:
            result = self.restore()
        self.assertGreaterEqual(mutant.call_count, 2)
        self.assertTrue(result.receipt["digest_match"])
        with self.assertRaises(AssertionError):
            self.assertEqual(self.cap.digest(), base.checkpoint.record["digest"])

    def restart(self):
        replacement = self.cap.restart_broker()
        self.addCleanup(replacement.close)
        return self.new_backend()

    def test_reopen_recovers_each_stage_and_current_point_at_the_restored_epoch(self):
        base = self.mark(accepted=False)
        self.cap.scene["col:PROPS"].append("stage-one")
        refs = (self.artifact("baked", "mesh digest"),)
        stage = self.mark(1, refs=refs)
        self.restore("p0")
        jobs = list(self.cap.ran)
        old = self.backend
        self.backend = self.restart()
        self.assertEqual(self.cap.ran, jobs)
        self.assertEqual(self.backend.points(), ("p0", "p1"))
        self.assertEqual(self.backend.current(), base.checkpoint)
        self.refused("NO_LEASE", old.restore, "p0", by="broker", scene_epoch=2,
                     authority_epoch=7, budget=self.budget)
        restored = self.restore("p1", scene_epoch=2)
        self.assertEqual(restored.checkpoint, stage.checkpoint)
        self.assertEqual(restored.receipt["scene_epoch"], 3)
        self.assertEqual(self.cap.digest(), stage.checkpoint.record["digest"])

    def test_reopen_refuses_epoch_drift_before_exposing_acceptance(self):
        self.mark()
        self.binding = replace(self.binding, authority_epoch=self.binding.authority_epoch + 1)
        self.refused("RESTORE_MISMATCH", self.restart)

    def test_mutant_trusting_unpinned_manifest_fails_recovered_score_oracle(self):
        verified = self.mark()
        original = rewind.Catalog.read
        def trust_unpinned(catalog):
            manifest, pin = original(catalog)
            # Return a different payload as though it had the verified payload's digest.
            manifest["points"][0]["quality"]["score"] += 1
            return manifest, pin
        with patch.object(rewind.Catalog, "read", autospec=True, side_effect=trust_unpinned) as mutant:
            self.backend = self.restart()
            mutant.assert_called_once()
            with self.assertRaises(AssertionError):
                self.assertEqual(self.backend.current().quality, verified.checkpoint.quality)

    def test_interrupted_publication_fences_reopen_without_dispatch(self):
        self.mark()
        with patch.object(self.backend._catalog, "finish", side_effect=OSError("crash after journal append")):
            with self.assertRaises(OSError):
                self.restore()
        calls = list(self.cap.ran)
        self.refused("UNCERTAIN_OUTCOME", self.restart)
        self.assertEqual(self.cap.ran, calls)

    def test_mutant_rehashed_acceptance_is_refused_by_independent_broker_pin(self):
        self.mark()
        with self.backend._catalog.transaction() as db:
            data, = db.execute("SELECT manifest FROM catalog").fetchone()
            manifest = json.loads(data)
            manifest["points"][0]["quality"]["score"] += 1
            raw = canonical_bytes(manifest)
            db.execute("UPDATE catalog SET manifest=?,digest=?", (raw, sha256(raw).hexdigest()))
        self.refused("UNCERTAIN_OUTCOME", self.restart)

    def test_mutant_restored_reference_with_invalid_wire_field_fails_validation(self):
        self.mark()
        checkpoint = self.backend._points["p0"]
        # A compromised resolver agrees with the corrupted copy mode on the stored reference.
        checkpoint.artifact_refs[0]["copy"] = "silent"
        with self.assertRaises(rewind.ValidationError):
            self.restore()
        self.assertEqual(self.cap.events(rec="control", op="rewind"), [])

    def composite_goal(self):
        base = self.mark(accepted=False)
        binding = json.loads((DIRECT / "schemas/tests/examples/vp.surface_binding_v1.valid.json").read_bytes())
        binding["authority_epoch"] = self.binding.authority_epoch
        goal = GoalMachine.create(self.root / "goals.sqlite", goal_id="goal-fixture", session_id="session",
                                  binding=binding, checkpoint=base, score=50, steps=4, duplicates=3,
                                  epsilon=1, action_quantum="0.01", observation_id="o0", generation=1)
        return goal, base

    def begin_rewind(self, goal):
        self.assertEqual(goal.propose("bad.a1", {"edit": 1}, observation=goal.status()["observation"]),
                         "EXECUTING")
        quality = QualityResult(fixture_sha("failed"), 1, (RenderPredicate("render/frame_000000.png", True),))
        self.assertEqual(goal.judge("bad.a1", fixture_sha("rejected-scene"), quality), "JUDGE_REJECT")
        goal.observed("rejected", 2)
        self.assertEqual(goal.propose("alt.a1", {"edit": 2}, observation="rejected",
                                     supersedes="bad.a1", retry=True), "REWINDING")

    def test_goal_machine_consumes_real_composite_restore_after_both_owners_restart(self):
        goal, base = self.composite_goal()
        self.begin_rewind(goal)
        goal = GoalMachine(self.root / "goals.sqlite", "goal-fixture")
        self.backend = self.restart()
        def restore(claim, state):
            self.assertEqual(claim, rewind.rw.control_job_id("restore", "p0", "e2"))
            return self.restore(budget=dict(self.budget, attempts_used=state["t"], rewinds_used=state["r"]))
        self.assertEqual(goal.run_restore(restore), "READY")
        ledger = goal.replay()
        self.assertEqual(ledger["checkpoint_evidence"], base.goal_evidence())
        self.assertEqual((ledger["best"], ledger["scene_epoch"], ledger["r"]), (50, 2, 1))
        self.assertEqual(len(self.cap.events(rec="control", op="rewind")), 1)

    def test_real_mark_pins_judged_score_and_refs_in_goal_replay(self):
        goal, _ = self.composite_goal()
        self.assertEqual(goal.propose("mesh.a1", {"repair": 1}, observation="o0"), "EXECUTING")
        self.cap.scene["col:PROPS"].append("repaired")
        quality = QualityResult(fixture_sha("qualified render"), 80,
                                (RenderPredicate("render/frame_000000.png", True),))
        self.assertEqual(goal.judge("mesh.a1", rewind.scene_state_sha256(self.cap.digest()), quality),
                         "JUDGING")
        ref = self.artifact("repaired", "stage-output")
        result = self.mark(1, refs=(ref,), score_checkpoint=lambda _: quality)
        # Neither the previous plain-receipt API nor a different score may discard the pin.
        with self.assertRaisesRegex(Refused, "RESTORE_MISMATCH"):
            goal.commit("mesh.a1", result.receipt)
        other = replace(result, checkpoint=replace(result.checkpoint,
                        quality=dict(result.checkpoint.quality, score=79)))
        with self.assertRaisesRegex(Refused, "RESTORE_MISMATCH"):
            goal.commit("mesh.a1", other)
        self.assertEqual(goal.commit("mesh.a1", result), "COMMITTED")
        ledger = GoalMachine(self.root / "goals.sqlite", "goal-fixture").replay()
        self.assertEqual(ledger["checkpoint_evidence"]["artifact_refs"], [ref])
        self.assertEqual(ledger["best"], ledger["checkpoint_evidence"]["quality"]["score"])

    def test_mutant_substituted_gate_on_same_scene_hard_stops_goal(self):
        goal, _ = self.composite_goal()
        self.begin_rewind(goal)
        def substitute(claim, state):
            result = self.restore(budget=dict(self.budget, attempts_used=state["t"], rewinds_used=state["r"]))
            result.checkpoint.quality["score"] += 1
            return result
        self.assertEqual(goal.run_restore(substitute), "HARD_STOP_ESCALATED")
        self.assertEqual(goal.replay()["reason"], "RESTORE_MISMATCH")

    def test_mutant_discarding_composite_acceptance_fails_goal_restore_oracle(self):
        goal, base = self.composite_goal()
        self.begin_rewind(goal)
        def substitute(claim, state):
            result = self.restore(budget=dict(self.budget, attempts_used=state["t"], rewinds_used=state["r"]))
            result.checkpoint.quality["score"] += 1
            return result
        # Replace actual restored evidence with cached evidence from the marked point.
        with patch.object(rewind.CheckpointResult, "goal_evidence",
                          return_value=base.goal_evidence()) as mutant:
            with self.assertRaises(AssertionError):
                self.assertEqual(goal.run_restore(substitute), "HARD_STOP_ESCALATED")
            mutant.assert_called_once()

    def test_existing_runtime_ring_fails_closed_after_reopen(self):
        self.mark()
        self.refused("UNCERTAIN_OUTCOME", self.new_backend)


if __name__ == "__main__":
    unittest.main()
