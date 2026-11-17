"""S3-U3: mesh-specific reproduction cannot be replaced by a truthy flag."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from wender.remember.skills import SkillRejected, admit
from wender.remember.sculpt import record_sha256
from wender.remember.sculpt_store import SculptSkillStore, SculptPublicationUncertain
from wender.learn.sculpt_replay import qualify_sculpt_skill

PIN = hashlib.sha256(b"synthetic-integer-grid-kernel-v1").hexdigest()
GUARDS = ["topology", "boundary", "outside_patch"]


def mesh(spacing=1000, spike=200):
    # Analytic open triangular grid. Geometry comes from this deterministic
    # fixture generator, not a model; fixtures make no appearance claims.
    vertices = [[x * spacing, y * spacing, spike if (x, y) == (1, 1) else 0]
                for y in range(3) for x in range(3)]
    faces = []
    for y in range(2):
        for x in range(2):
            a = y * 3 + x
            faces.extend([[a, a + 1, a + 3], [a + 1, a + 4, a + 3]])
    return (json.dumps({"vertices": vertices, "faces": faces}, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def smooth(raw, recipe, deadline):
    value = json.loads(raw)
    # Locally calculated patch: remove an isolated centre-height outlier.
    value["vertices"][4][2] = sum(value["vertices"][i][2] for i in (1, 3, 5, 7)) // 4
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def evaluate(raw, candidate, recipe, deadline):
    before, after = json.loads(raw), json.loads(candidate)
    return {"score_before": -before["vertices"][4][2] ** 2,
            "score_after": -after["vertices"][4][2] ** 2,
            "guards": {"topology": before["faces"] == after["faces"],
                       "boundary": all(before["vertices"][i] == after["vertices"][i]
                                       for i in range(9) if i != 4),
                       "outside_patch": before["vertices"][4][:2] == after["vertices"][4][:2]}}


def qualify(**changes):
    args = dict(source_mesh=mesh(), held_out_meshes=[mesh(1100, 100), mesh(1300, 300)],
                toolchain_sha256=PIN, required_guards=GUARDS, metric="negative_peak_z_squared",
                min_gain=1, replay=smooth, evaluate=evaluate, verified_by="analytic-grid-verifier")
    args.update(changes)
    return qualify_sculpt_skill(draft(), **args)


def seal_trial(row):
    row["receipt_sha256"] = record_sha256({k: v for k, v in row.items() if k != "receipt_sha256"})


def draft():
    return {"id": "smooth-rim", "intent": "smooth a measured rim outlier",
            "params": {"kernel": "integer-neighbour-mean"},
            "steps": [{"path": "api", "action": {"skill": "smooth-rim"}}],
            "checkpoints": [{"metric": "roughness"}],
            "provenance": {"source": "synthetic-source-mesh"},
            "evidence": {"reproduced": True, "verified_by": "source-only"},
            "domain": "sculpt"}


class TestSculptAdmissionControl(unittest.TestCase):
    def test_source_only_reproduction_is_refused(self):
        with self.assertRaises(SkillRejected):
            admit(draft())

    def test_two_distinct_held_out_meshes_are_measured_and_admitted(self):
        report = qualify()
        self.assertEqual(report["rc"], 0)
        self.assertEqual(len(report["supported_meshes"]), 2)
        self.assertEqual(json.loads(admit(report["skill"])), report["skill"])
        for trial in report["policy"]["trials"]:
            self.assertEqual(trial["rc"], 0)
            self.assertGreater(trial["score_after"], trial["score_before"])
            self.assertEqual(trial["output_sha256"], trial["replay_sha256"])
            self.assertGreaterEqual(trial["wall_ns"], 0)

    def test_source_duplicate_and_training_alias_refused_before_execution(self):
        from unittest.mock import Mock
        for meshes, extra in (([mesh(), mesh(1100)], {}),
                              ([mesh(1100), mesh(1100)], {}), ([mesh(1100)], {}),
                              ([mesh(1100), mesh(1300)], {"training_mesh_sha256s": [
                                  hashlib.sha256(mesh(1100)).hexdigest()]})):
            executor = Mock(side_effect=AssertionError("must not execute"))
            report = qualify(held_out_meshes=meshes, replay=executor, **extra)
            self.assertEqual(report["rc"], 3)
            self.assertIsNone(report["skill"])
            executor.assert_not_called()

    def test_source_only_support_mutant_rejected_even_with_fresh_receipt_hash(self):
        skill = qualify()["skill"]
        source = skill["sculpt"]["source_mesh_sha256"]
        skill["sculpt"]["trials"] = skill["sculpt"]["trials"][:1]
        row = skill["sculpt"]["trials"][0]
        row["mesh_sha256"] = source
        seal_trial(row)
        with self.assertRaisesRegex(SkillRejected, "not held out"):
            admit(skill)

    def test_duplicate_receipts_do_not_increase_support(self):
        skill = qualify()["skill"]
        skill["sculpt"]["trials"][1] = deepcopy(skill["sculpt"]["trials"][0])
        with self.assertRaisesRegex(SkillRejected, "duplicate reproduction mesh"):
            admit(skill)

    def test_bad_guard_noop_drift_and_missing_measurement_are_not_support(self):
        calls = []
        def drift(raw, recipe, deadline):
            calls.append(raw)
            return smooth(raw, recipe, deadline) if len(calls) % 2 else raw
        def fail_guard(*args):
            result = evaluate(*args)
            result["guards"]["boundary"] = False
            return result
        def missing(*args):
            result = evaluate(*args)
            result["score_before"] = None
            return result
        for args, rc, reason in (({"replay": lambda raw, *_: raw}, 5, "no_op"),
                                 ({"replay": drift}, 5, "replay_drift"),
                                 ({"evaluate": fail_guard}, 5, "guard_failed"),
                                 ({"evaluate": missing}, 2, "reproduction_contract")):
            with self.subTest(reason=reason):
                report = qualify(**args)
                self.assertEqual(report["rc"], rc)
                self.assertIsNone(report["skill"])
                self.assertEqual(len(report["policy"]["trials"]), 2)
                self.assertTrue(all(reason in row["reason_codes"] for row in report["policy"]["trials"]))

    def test_failed_attempt_retained_beside_two_successes(self):
        failed = mesh(1700, 700)
        def unsupported(raw, *args):
            if raw == failed:
                raise NotImplementedError
            return smooth(raw, *args)
        report = qualify(held_out_meshes=[failed, mesh(1100), mesh(1300)], replay=unsupported)
        self.assertEqual(report["rc"], 0)
        self.assertEqual(len(report["supported_meshes"]), 2)
        self.assertEqual([row["rc"] for row in report["policy"]["trials"]], [3, 0, 0])

    def test_receipt_tampering_revision_change_and_false_success_are_refused(self):
        original = qualify()["skill"]
        mutations = []
        for key, value in (("rc", True), ("score_before", False), ("score_after", None),
                           ("output_sha256", None), ("reason_codes", ["contradiction"]),
                           ("guards", {name: False for name in GUARDS})):
            skill = deepcopy(original)
            row = skill["sculpt"]["trials"][0]
            row[key] = value
            seal_trial(row)
            mutations.append(skill)
        skill = deepcopy(original)
        skill["sculpt"]["trials"][0]["score_after"] += 1
        mutations.append(skill)
        skill = deepcopy(original)
        skill["params"]["kernel"] = "other-kernel"
        mutations.append(skill)
        skill = deepcopy(original)
        del skill["domain"]
        mutations.append(skill)
        for skill in mutations:
            with self.subTest(skill=skill):
                with self.assertRaises(SkillRejected):
                    admit(skill)

    def test_policy_and_callback_inputs_are_copied(self):
        original = draft()
        guards = list(GUARDS)
        def mutate(raw, recipe, deadline):
            recipe["params"].clear()
            guards.clear()
            return smooth(raw, recipe, deadline)
        report = qualify_sculpt_skill(original, source_mesh=mesh(),
            held_out_meshes=[mesh(1100), mesh(1300)], toolchain_sha256=PIN,
            required_guards=guards, metric="negative_peak_z_squared", min_gain=1,
            replay=mutate, evaluate=evaluate, verified_by="verifier")
        self.assertEqual(report["rc"], 0)
        self.assertEqual(original, draft())
        self.assertEqual(report["skill"]["sculpt"]["required_guards"], GUARDS)

    def test_transplanted_trial_cannot_support_another_recipe_policy_or_toolchain(self):
        from wender.remember.sculpt import skill_revision
        original = qualify()["skill"]
        for change in ("recipe", "toolchain", "policy"):
            skill = deepcopy(original)
            if change == "recipe":
                skill["params"]["kernel"] = "different"
                skill["sculpt"]["skill_sha256"] = skill_revision(skill)
            elif change == "toolchain":
                skill["sculpt"]["toolchain_sha256"] = "0" * 64
            else:
                skill["sculpt"]["metric"] = "different"
            with self.subTest(change=change):
                with self.assertRaisesRegex(SkillRejected, "identity stale"):
                    admit(skill)

    def test_timeout_and_callback_error_retained_with_exact_rc(self):
        def timeout(*args):
            raise TimeoutError
        def broken(*args):
            raise RuntimeError("private detail must not be recorded")
        for callback, rc in ((timeout, 6), (broken, 2)):
            report = qualify(replay=callback)
            self.assertEqual(report["rc"], rc)
            self.assertNotIn("private detail", json.dumps(report))

    def test_invalid_policy_fails_before_replay(self):
        for change in ({"min_gain": True}, {"min_gain": 0}, {"required_guards": []},
                       {"required_guards": ["x", "x"]}, {"toolchain_sha256": "latest"},
                       {"timeout_s": True}, {"required_guards": None},
                       {"held_out_meshes": iter([mesh(1100), mesh(1300)])},
                       {"training_mesh_sha256s": [None]}):
            with self.subTest(change=change):
                with self.assertRaises(SkillRejected):
                    qualify(**change)


class TestSculptPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SculptSkillStore(self.root)
        self.skill = qualify()["skill"]

    def test_atomic_roundtrip_new_process_and_idempotence(self):
        self.store.publish(self.skill)
        path = self.root / "sculpt_skills.json"
        before = path.read_bytes()
        self.store.publish(self.skill)
        self.assertEqual(path.read_bytes(), before)
        script = ("import json,sys; from wender.remember.sculpt_store import SculptSkillStore; "
                  "print(json.dumps(SculptSkillStore(sys.argv[1]).load(expected_toolchain_sha256=sys.argv[2])))")
        child = subprocess.run([sys.executable, "-B", "-c", script, str(self.root), PIN],
                               timeout=20, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(child.stdout), [self.skill])
        loaded = self.store.load(expected_toolchain_sha256=PIN)
        loaded[0]["params"].clear()
        self.assertEqual(self.store.load(expected_toolchain_sha256=PIN), [self.skill])

    def test_refusal_preserves_caller_bytes_and_does_not_create_store(self):
        absent = SculptSkillStore(self.root / "absent")
        with self.assertRaises(SkillRejected):
            absent.publish(draft())
        self.assertFalse((self.root / "absent").exists())
        self.store.publish(self.skill)
        path = self.root / "sculpt_skills.json"
        before = path.read_bytes()
        with self.assertRaises(SkillRejected):
            self.store.publish(draft())
        self.assertEqual(path.read_bytes(), before)

    def test_unlabelled_legacy_skill_cannot_bypass_sculpt_store_gate(self):
        legacy = draft()
        del legacy["domain"]
        self.assertIsInstance(admit(legacy), str)
        with self.assertRaisesRegex(SkillRejected, "domain required"):
            self.store.publish(legacy)
        self.assertFalse((self.root / "sculpt_skills.json").exists())

    def test_postcommit_durability_error_requires_reconciliation(self):
        actual_fsync = os.fsync
        def fsync(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("planted directory durability error")
            return actual_fsync(fd)
        with patch("wender.remember.sculpt_store.os.fsync", side_effect=fsync):
            with self.assertRaises(SculptPublicationUncertain) as caught:
                self.store.publish(self.skill)
        raw = (self.root / "sculpt_skills.json").read_bytes()
        self.assertEqual(caught.exception.published_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.store.load(expected_toolchain_sha256=PIN), [self.skill])

    def test_replace_failure_preserves_bytes_and_cleans_staging(self):
        self.store.publish(self.skill)
        path = self.root / "sculpt_skills.json"
        before = path.read_bytes()
        other_draft = draft()
        other_draft["id"] = "different"
        other = qualify_sculpt_skill(other_draft, source_mesh=mesh(),
            held_out_meshes=[mesh(1100), mesh(1300)], toolchain_sha256=PIN,
            required_guards=GUARDS, metric="negative_peak_z_squared", min_gain=1,
            replay=smooth, evaluate=evaluate, verified_by="verifier")["skill"]
        with patch("wender.remember.sculpt_store.os.replace", side_effect=OSError("planted")):
            with self.assertRaises(OSError):
                self.store.publish(other)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["sculpt_skills.json"])

    def test_tamper_duplicate_json_and_policy_drift_never_load(self):
        self.store.publish(self.skill)
        path = self.root / "sculpt_skills.json"
        raw = path.read_bytes()
        for corrupted in (raw.replace(b"smooth-rim", b"evil-rim"), raw[:-1],
                          b'{"schema":1,"schema":2}\n', b'{"value":1.0}\n'):
            path.write_bytes(corrupted)
            with self.assertRaises(SkillRejected):
                self.store.load(expected_toolchain_sha256=PIN)
            with self.assertRaises(SkillRejected):
                self.store.publish(self.skill)
            self.assertEqual(path.read_bytes(), corrupted)
        path.write_bytes(raw)
        with self.assertRaisesRegex(SkillRejected, "toolchain stale"):
            self.store.load(expected_toolchain_sha256="0" * 64)

    def test_recomputed_outer_checksum_cannot_hide_source_only_evidence(self):
        self.store.publish(self.skill)
        path = self.root / "sculpt_skills.json"
        value = json.loads(path.read_bytes())
        value["skills"][0]["sculpt"]["trials"] = value["skills"][0]["sculpt"]["trials"][:1]
        value["sha256"] = record_sha256({k: v for k, v in value.items() if k != "sha256"})
        from wender.remember.program_map import canonical_json
        path.write_text(canonical_json(value) + "\n")
        with self.assertRaises(SkillRejected):
            self.store.load(expected_toolchain_sha256=PIN)

    def test_conflicting_revision_symlink_and_concurrent_writer_refused(self):
        import fcntl
        self.store.publish(self.skill)
        conflicting = deepcopy(self.skill)
        conflicting["evidence"]["version"] += 1
        with self.assertRaisesRegex(SkillRejected, "conflicting"):
            self.store.publish(conflicting)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(SkillRejected, "symlink"):
            SculptSkillStore(alias).load(expected_toolchain_sha256=PIN)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.store.publish(self.skill)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
