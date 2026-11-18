"""INT promotion, retirement, registry and wire controls."""
from pathlib import Path
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from asset_factory import core, proc
from tools import gates
from tools.pipeline import engine
from tools.registry.store import Registry, RegistryError
from viewpoint.handlers import LocalDryRun
import test_d18_base_evidence as base_fixture
import test_d18_vpfix_wire as wire
import test_w3_e2efix_regression as regression
from test_safety_proc import launch
from test_safety_proc_wait_slices import Clock
from systemd_fake import FakeRunner


class Integration(unittest.TestCase):
    def test_legacy_proposal_cannot_promote(self):
        fixture = regression.RegressionBoundary()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.pol["repair"] = {"max_passes": 1, "max_hole_edges": 0, "cull_max_permille": 0}
        core.atomic_write_json(fixture.root / "policy.json", fixture.pol)
        fixture.backend.policy_refs["mesh_policy"] = engine.file_ref(fixture.root / "policy.json")
        output = fixture.root / "pipeline"
        run = engine.Pipeline(output, fixture.backend).run()
        stage = next(row for row in run["stages"] if row["stage"] == "mesh")
        self.assertEqual(stage["rc"], 3, run)
        self.assertIn("geometry_scope_missing", engine.load(stage["receipt"]["path"])["reason_codes"])
        self.assertFalse(list((output / "cache/mesh").glob("*/entry.json")))

    def test_transient_retirement_action_does_not_override_witness(self):
        runner = FakeRunner(auto_exit=0)
        job = launch(runner)
        runner.stop_hook = lambda unit: (_ for _ in ()).throw(OSError("temporary"))
        self.assertEqual(job.wait(), 0)
        self.assertEqual(proc.cleanup(job), [])
        self.assertFalse(runner.units)

    def test_cleanup_deadline_spent_once(self):
        clock = Clock()

        class HungReset(FakeRunner):
            hung = True

            def run(self, argv, *, timeout):
                if len(argv) > 2 and argv[2] == "reset-failed" and self.hung:
                    clock.advance(timeout)
                    raise proc.SignalRefusal("systemd_user_manager_unavailable")
                return super().run(argv, timeout=timeout)

        runner = HungReset(auto_exit=0)
        try:
            with proc.using_runner(runner), patch.object(proc.time, "monotonic", clock), patch.object(proc.time, "sleep", clock.advance):
                start = clock()
                result = gates.run_leg("F0-U1", ["/usr/bin/true"], 1)
                self.assertEqual(result["rc"], 3, result)
                self.assertLessEqual(clock() - start, 5)
                errors = result["evidence"]["cleanup_errors"]
                self.assertEqual(len(errors), len(set(errors)))
        finally:
            runner.hung = False
            proc.reconcile_owned(runner=runner)

    def test_missing_high_water_refuses_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Registry(root, namespace="studio").initialize()
            (root / "HIGH_WATER").unlink()
            for method in ("current_snapshot", "initialize"):
                with self.subTest(method=method):
                    with self.assertRaises(RegistryError) as caught:
                        getattr(Registry(root, namespace="studio"), method)()
                    self.assertEqual(caught.exception.reason, "missing_high_water")

    def test_failure_only_record_archive(self):
        f = base_fixture.EvidenceTests()
        f.setUp()
        f.receipt["observations"][0].update(lower=[1, 1], upper=[1, 1])
        gate = f.assess()["gate"]
        raw = base_fixture.encoded(f.make_record("part", gate))
        approved = {(gate["gate_id"], gate["subject_digest"]): {gate["profile_digest"]}}
        with tempfile.TemporaryDirectory() as tmp:
            archive = engine.Pipeline(tmp, object())
            def store(data, scopes):
                return archive.archive_failed_record(data, approved_scopes=scopes,
                            read_checked=lambda role, pin: f.store.get((role, pin)))
            ref = store(raw, approved)
            self.assertEqual(Path(ref["path"]).read_bytes(), raw)
            self.assertEqual(ref, store(raw, approved))
            self.assertEqual(f.verify(raw)["reasons"], ["missing_required_gate"])
            with self.assertRaises(engine.Refusal):
                store(raw, {})
            f.receipt["observations"][0].update(lower=[0, 1], upper=[0, 1])
            passing = f.make_record("part", f.assess()["gate"])
            approved = {("mesh", gate["subject_digest"]): {f.receipt["profile_digest"]}}
            with self.assertRaises(engine.Refusal):
                store(base_fixture.encoded(passing), approved)

    def test_cooperative_child_observes_abort_before_forced_stop(self):
        self.assertEqual(os.environ["AF_PROC_BACKEND"], "pidns")
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            script = ("import os, pathlib, sys, time\n"
                      "flag = pathlib.Path(os.environ['AF_JOB_ABORT_FILE'])\n"
                      "pathlib.Path(sys.argv[1]).touch()\n"
                      "while not flag.exists(): time.sleep(.005)\n")
            job = proc.popen([sys.executable, "-c", script, str(ready)],
                             start_new_session=True, cooperative_abort=True)
            try:
                deadline = time.monotonic() + 4
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
            finally:
                errors = proc.cleanup(job)
            self.assertEqual((errors, job.returncode), ([], 0))

    def test_uncooperative_job_is_stopped_by_owned_unit_name(self):
        runner = FakeRunner()
        job = launch(runner, cooperative_abort=True)
        seen = []
        original = runner.run
        def observe(argv, *, timeout):
            if argv[2] == "kill":
                seen.append((argv[-1], (Path(job._scratch.name) / ".abort").exists()))
            return original(argv, timeout=timeout)
        with patch.object(runner, "run", side_effect=observe):
            self.assertEqual(proc.cleanup(job), [])
        self.assertEqual(seen, [(job.unit_name, True)])
        self.assertFalse(runner.units)

    def test_invalid_envelope_uses_owner_reply(self):
        wire.OwnerWireTests.setUpClass()
        req = wire.request(request_id="w" * 129)
        reply = LocalDryRun().handle(req)
        wire.OwnerWireTests.owner.validate_contract("vp.control.reply/v1", reply)
        self.assertEqual((reply["request_id"], reply["code"]), (req["request_id"], "INVALID_REQUEST"))
        self.assertEqual(reply["effective_request_sha256"], core.sha256_json(req))

    def test_required_stages_and_file_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "allowed"
            root.mkdir()
            outside = Path(tmp) / "unrelated.bin"
            outside.write_bytes(b"fixture")
            (root / "redirect.bin").symlink_to(outside)
            (root / "matte.bin").write_bytes(b"fixture")
            req = wire.request()
            req["args"]["permit_ref"] = "fixture-permit"
            lane = LocalDryRun(allowed_root=root)
            for stage, locator, expected in [
                ("audio", (root / "matte.bin").as_uri(), "INVALID_ARGS"),
                ("matte", outside.as_uri(), "INVALID_ARGS"),
                ("matte", (root / "redirect.bin").as_uri(), "INVALID_ARGS"),
                ("matte", (root / "matte.bin").as_uri(), "HOLD"),
            ]:
                with self.subTest(stage=stage, locator=locator):
                    req["args"]["inputs"] = [dict(wire.reference(stage), locator=locator)]
                    self.assertEqual(lane.handle(req)["code"], expected)
            reply = io.StringIO()
            rc = wire.handlers.main(["--dry-run", "--artifact-root", str(root)],
                                    io.StringIO(json.dumps(req)), reply)
            self.assertEqual((rc, json.loads(reply.getvalue())["code"]), (3, "HOLD"))
            req["args"]["inputs"] = []
            self.assertEqual(lane.handle(req)["code"], "INVALID_ARGS")

