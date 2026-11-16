"""Adversarial repair regressions; all image/calibration data are synthetic."""
import builtins
import contextlib
import inspect
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock
import zlib

import test_f1u3_input_gate as base
from gates import input_gate as gate


class RepairTests(unittest.TestCase):
    setUp = base.PublicationTests.setUp
    run_gate = base.PublicationTests.run_gate
    verify = base.PublicationTests.verify

    def emitted(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.run(self.image, self.output, policy_path=self.policy_path,
                          timeout_seconds=kwargs.get("timeout", 10))
        record = json.loads(out.getvalue())
        self.assertEqual(record["rc"], rc)
        return record

    def test_f1_arbitrary_calibration_cannot_authorize(self):
        arbitrary = b"not a calibration record"
        self.chosen["calibration_ref"]["sha256"] = gate._sha(arbitrary)
        self.policy_path.write_text(gate.core.canonical_json(self.chosen))
        (self.root / "calibration.txt").write_bytes(arbitrary)
        self.assertEqual(self.emitted()["rc"], 2)
        self.assertFalse(self.output.exists())

    def test_f1_one_transparent_pixel_never_passes(self):
        chosen = base.policy(min_foreground_ppm=0, max_foreground_ppm=1000000,
                             max_border_foreground_ppm=1000000,
                             max_small_component_ppm=1000000, max_soft_alpha_ppm=1000000)
        result = base.inspect(base.png([0] + [255] * 255), chosen)
        self.assertEqual(result["rc"], 5)
        codes = result.get("reason_codes", result.get("findings"))
        self.assertIn("foreground_occupancy", codes)
        self.assertIn("border_leakage", codes)

    def test_f1_inspection_requires_calibration_evidence(self):
        chosen = base.policy()
        result = gate.inspect_reference(base.png(), policy=chosen,
                                        policy_sha256=gate.core.sha256_json(chosen),
                                        toolchain_sha256=gate.toolchain_identity())
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["reason_codes"], ["calibration_missing"])

    def test_f2_preparation_without_manifest_cannot_pass(self):
        helper = base.IdentityTests()
        original, candidate, receipt, decision = helper.prepared()
        chosen = base.policy()
        result = gate.inspect_prepared_reference(original, candidate, preparation_receipt=receipt,
            decision=decision, policy=chosen, policy_sha256=gate.core.sha256_json(chosen),
            toolchain_sha256=gate.toolchain_identity())
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["reason_codes"], ["preparation_provider_missing"])

    def test_f3_decoder_exception_boundary(self):
        from PIL import Image
        for exc in (struct.error("bad structure"), EOFError("short data"), zlib.error("bad stream")):
            with self.subTest(exception=type(exc).__name__), mock.patch.object(Image, "open", side_effect=exc):
                try:
                    result = base.inspect()
                except Exception as escaped:
                    self.fail("decoder exception escaped: " + type(escaped).__name__)
                self.assertEqual(result["rc"], 2)
                self.assertEqual(result["reason_codes"], ["image_invalid"])

    def test_f3_decoder_has_png_allowlist(self):
        from PIL import Image
        real = Image.open
        with mock.patch.object(Image, "open", wraps=real) as opened:
            self.assertEqual(base.inspect()["rc"], 0)
        self.assertTrue(opened.call_args_list)
        for call in opened.call_args_list:
            self.assertEqual(call.kwargs.get("formats"), ("PNG",))

    def test_f4_palette_and_la_are_unproven(self):
        for mode in ("P", "LA"):
            with self.subTest(mode=mode):
                result = base.inspect(base.png(mode=mode))
                self.assertEqual(result["rc"], 3)
                self.assertEqual(result["reason_codes"], ["encoding_unproven"])

    def test_f4_rgba_encoding_is_recorded(self):
        result = base.inspect()
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["metrics"].get("format"), "PNG")
        self.assertEqual(result["metrics"].get("mode"), "RGBA")

    def test_rgba_sixteen_bit_png_is_unproven(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        pixels = b"".join(b"\0" + b"".join(struct.pack(">HHHH", 514, 514, 514,
                           65535 if 5 <= x < 11 and 5 <= y < 11 else 0) for x in range(16)) for y in range(16))
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 16, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
        result = base.inspect(data)
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["reason_codes"], ["encoding_unproven"])

    def test_f5_rule_level_mutation_coverage_exists(self):
        self.assertTrue(hasattr(base, "RuleMutationTests"), "whole-function PASS mocks are insufficient")

    def test_f6_reason_codes_and_unverified_provenance(self):
        result = base.inspect()
        self.assertIn("reason_codes", result)
        self.assertNotIn("findings", result)
        self.assertEqual(result["preparation"], {"kind": "unverified_provenance"})

    def test_f6_stub_is_adjacent_and_actual_kernel_is_bound(self):
        self.assertFalse(Path(gate.__file__).with_name("_afstub_f1u3.py").exists())
        from asset_factory import core
        self.assertIs(gate.core, core)
        self.assertTrue(hasattr(gate.core, "__file__"))
        kernel_path = Path(gate.core.__file__)
        real = Path.read_bytes
        def changed(path):
            data = real(path)
            return data + b"\n# altered loaded kernel" if path == kernel_path else data
        before = gate.toolchain_identity()
        with mock.patch.object(Path, "read_bytes", changed):
            self.assertNotEqual(gate.toolchain_identity(), before)

    def test_f6_help_is_json_refusal(self):
        child = subprocess.run([sys.executable, "-B", "-m", "gates.input_gate", "--help"],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 2)
        self.assertEqual(json.loads(child.stdout)["reason_codes"], ["usage_invalid"])

    def test_f6_missing_input_is_cannot_see(self):
        self.image.unlink()
        result = self.emitted()
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["reason_codes"], ["input_missing"])

    def test_f7_over_budget_source_identity_is_null(self):
        self.image.write_bytes(b"x" * (self.chosen["max_encoded_bytes"] + 20))
        result = self.emitted()
        self.assertEqual(result["rc"], 6)
        self.assertIsNone(result["source_sha256"])

    def test_f7_unexpected_bundle_file_is_refused(self):
        self.assertEqual(self.run_gate(), 0)
        (self.output / "extra.bin").write_bytes(b"unbound")
        result = self.verify()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["evidence_files_invalid"])

    def test_f7_verification_has_deadline(self):
        self.assertIn("timeout_seconds", inspect.signature(gate.verify_receipt).parameters)

    def test_f7_negative_worker_exit_is_budget(self):
        completed = subprocess.CompletedProcess([], -9, stdout="", stderr="")
        # The current implementation uses run; the repair owns a Popen group.
        if hasattr(gate, "_run_owned_worker"):
            target = mock.patch.object(gate, "_run_owned_worker", return_value=completed)
        else:
            target = mock.patch.object(gate.subprocess, "run", return_value=completed)
        with target:
            result = self.emitted()
        self.assertEqual(result["rc"], 6)

    def test_f8_no_dead_schema_or_protocols(self):
        self.assertFalse(hasattr(gate, "RESULT_SCHEMA"))
        self.assertFalse(hasattr(gate, "BiRefNetBackend"))

    def test_decoder_real_importerror_path(self):
        real = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("synthetic unavailable Pillow")
            return real(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=missing):
            result = base.inspect()
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result.get("reason_codes", result.get("findings")), ["decoder_unavailable"])


class BindingTests(unittest.TestCase):
    """Independent exact-reason oracles for the guards named in review F5."""
    setUp = RepairTests.setUp
    run_gate = RepairTests.run_gate
    verify = RepairTests.verify
    emitted = RepairTests.emitted

    def prepared_result(self, *, receipt_updates=None, decision_updates=None, context_updates=None):
        helper = base.IdentityTests()
        original, candidate, receipt, decision = helper.prepared()
        decision.update(decision_updates or {})
        receipt["decision_sha256"] = gate.core.sha256_json(decision)
        receipt.update(receipt_updates or {})
        context = base.preparation_context()
        context.update(context_updates or {})
        chosen = base.policy()
        return gate.inspect_prepared_reference(original, candidate, preparation_receipt=receipt,
            decision=decision, policy=chosen, policy_sha256=gate.core.sha256_json(chosen),
            toolchain_sha256=gate.toolchain_identity(), calibration_bytes=base.calibration(chosen), **context)

    def test_original_hash_binding(self):
        self.assertEqual(self.prepared_result()["rc"], 0)
        result = self.prepared_result(receipt_updates={"original_sha256": "0" * 64})
        self.assertEqual(result["rc"], 4)
        self.assertEqual(result["reason_codes"], ["preparation_stale"])

    def test_preparation_licence(self):
        for update in ({"allowed": False}, {"verdict": "FAIL"}, {"operation": "rig"}):
            with self.subTest(update=update):
                result = self.prepared_result(decision_updates=update)
                self.assertEqual(result["rc"], 5)
                self.assertEqual(result["reason_codes"], ["preparation_licence_refused"])

    def test_current_decision_hashes(self):
        for key in ("configuration_sha256", "provider_sha256", "policy_sha256", "jurisdiction", "commercial_use"):
            update = {key: "0" * 64 if key.endswith("sha256") else "GB" if key == "jurisdiction" else True}
            with self.subTest(key=key):
                result = self.prepared_result(decision_updates=update)
                self.assertEqual(result["rc"], 4)
                self.assertEqual(result["reason_codes"], ["preparation_decision_stale"])

    def test_code_weights_revision_binding(self):
        for update in ({"code_sha256": "0" * 64}, {"weights_sha256": "0" * 64}, {"revision": "other-immutable-pin"}):
            with self.subTest(update=update):
                result = self.prepared_result(receipt_updates=update)
                self.assertEqual(result["rc"], 4)
                self.assertEqual(result["reason_codes"], ["preparation_provider_stale"])

    def test_closed_decision(self):
        result = self.prepared_result(decision_updates={"unexpected": True})
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["preparation_invalid"])

    def test_symlink_to_identical_bytes(self):
        self.assertEqual(self.run_gate(), 0)
        path = self.output / "reference.image"
        path.unlink()
        path.symlink_to(self.image)
        result = self.verify()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["evidence_path_invalid"])

    def test_rehashed_calibration_policy_drift(self):
        self.assertEqual(self.run_gate(), 0)
        path = self.output / "calibration.evidence"
        record = json.loads(path.read_bytes())
        record["policy_sha256"] = "0" * 64
        blob = gate.core.canonical_json(record).encode()
        path.write_bytes(blob)
        self.chosen["calibration_ref"]["sha256"] = gate._sha(blob)
        (self.output / "policy.json").write_text(gate.core.canonical_json(self.chosen))
        receipt_path = self.output / "receipt.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["calibration"]["sha256"] = gate._sha(blob)
        receipt["policy"]["sha256"] = gate.core.sha256_json(self.chosen)
        receipt_path.write_text(gate.core.canonical_json(receipt))
        result = self.verify()
        self.assertEqual(result["rc"], 4)
        self.assertEqual(result["reason_codes"], ["calibration_policy_stale"])

    def test_calibration_receipt_equality(self):
        self.assertEqual(self.run_gate(), 0)
        path = self.output / "receipt.json"
        receipt = json.loads(path.read_bytes())
        receipt["calibration"]["sha256"] = "0" * 64
        path.write_text(gate.core.canonical_json(receipt))
        result = self.verify()
        self.assertEqual(result["rc"], 4)
        self.assertEqual(result["reason_codes"], ["calibration_stale"])

    def test_forged_result_exact_reason(self):
        self.assertEqual(self.run_gate(), 0)
        result_path, receipt_path = self.output / "input_gate.json", self.output / "receipt.json"
        result, receipt = json.loads(result_path.read_bytes()), json.loads(receipt_path.read_bytes())
        result["metrics"]["foreground_pixels"] = 999
        result_path.write_text(gate.core.canonical_json(result))
        receipt["result"]["sha256"] = gate.core.sha256_json(result)
        receipt_path.write_text(gate.core.canonical_json(receipt))
        refused = self.verify()
        self.assertEqual(refused["rc"], 4)
        self.assertEqual(refused["reason_codes"], ["result_stale"])

    def test_existing_escaping_calibration(self):
        # Existing, correctly hashed evidence outside the policy root isolates
        # containment from the missing-file and hash checks.
        outside = self.root / "outside.json"
        outside.write_bytes(base.calibration(self.chosen))
        inside = self.root / "nested"
        inside.mkdir()
        self.policy_path = inside / "policy.json"
        self.chosen["calibration_ref"]["path"] = "../outside.json"
        self.policy_path.write_text(gate.core.canonical_json(self.chosen))
        result = self.emitted()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["calibration_path_escape"])
        self.assertFalse(self.output.exists())

    def test_existing_empty_and_nonempty_outputs_survive(self):
        self.output.mkdir()
        before = self.output.stat().st_ino
        result = self.emitted()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["output_exists"])
        self.assertEqual(self.output.stat().st_ino, before)
        self.assertEqual(list(self.output.iterdir()), [])
        marker = self.output / "receipt.json"
        marker.write_bytes(b"caller bytes")
        result = self.emitted()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["output_exists"])
        self.assertEqual(marker.read_bytes(), b"caller bytes")

    def test_publication_race_preserves_caller_empty_directory(self):
        real_mkdir = Path.mkdir
        captured = {}
        def caller_wins(path, *args, **kwargs):
            if path == self.output:
                real_mkdir(path)
                captured["inode"] = path.stat().st_ino
            return real_mkdir(path, *args, **kwargs)
        with mock.patch.object(Path, "mkdir", caller_wins):
            result = self.emitted()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["output_exists"])
        self.assertEqual(self.output.stat().st_ino, captured["inode"])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_all_publication_replace_boundaries_rollback(self):
        real_replace = gate.os.replace
        for fail_at in range(1, 9):
            calls = []
            def failing(*args, **kwargs):
                calls.append(args)
                if len(calls) == fail_at:
                    raise OSError("injected replace failure")
                return real_replace(*args, **kwargs)
            with self.subTest(fail_at=fail_at), mock.patch.object(gate.os, "replace", side_effect=failing):
                result = self.emitted()
                self.assertEqual(result["rc"], 2)
                self.assertEqual(result["reason_codes"], ["input_or_publication_invalid"])
            self.assertFalse(self.output.exists())
            self.assertEqual(list(self.root.glob(".input-gate-*")), [])

    def test_worker_duplicate_keys_are_errors(self):
        payload = {"image": "", "policy": {}, "policy_sha256": "0" * 64,
                   "toolchain_sha256": "0" * 64, "calibration": None}
        raw = json.dumps(payload).replace('"image": ""', '"image": "ignored", "image": ""')
        out = io.StringIO()
        with mock.patch.object(gate.sys, "argv", ["worker", "--worker"]), \
                mock.patch.object(gate.sys, "stdin", io.StringIO(raw)), contextlib.redirect_stdout(out):
            rc = gate.main()
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["reason_codes"], ["worker_input_invalid"])

    def test_eps_never_reaches_ghostscript(self):
        from PIL import EpsImagePlugin
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 16 16\n%%EndComments\nshowpage\n"
        with mock.patch.object(EpsImagePlugin, "Ghostscript", side_effect=OSError("must not be reached")) as ghost:
            result = base.inspect(eps)
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["image_invalid"])
        ghost.assert_not_called()

    def test_timeout_kills_real_worker_process_group(self):
        pidfile = self.root / "grandchild.pid"
        parent_code = ("import subprocess,sys,time,pathlib; "
                       "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                       "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)")
        real_popen = subprocess.Popen
        parents = []
        def parent(argv, **kwargs):
            process = real_popen([sys.executable, "-I", "-c", parent_code, str(pidfile)], **kwargs)
            parents.append(process)
            return process
        with mock.patch.object(gate.subprocess, "Popen", side_effect=parent), \
                mock.patch.object(gate.os, "killpg", wraps=os.killpg) as kill:
            result = self.emitted(timeout=1)
        self.assertEqual(result["rc"], 6)
        self.assertEqual(result["reason_codes"], ["deadline_exceeded"])
        self.assertIsNotNone(parents[0].returncode)
        kill.assert_called_once_with(parents[0].pid, gate.signal.SIGKILL)
        self.assertTrue(pidfile.is_file(), "real grandchild must have started")
        stat = Path("/proc") / pidfile.read_text() / "stat"
        # A reparented zombie is dead; its reaping belongs to the host init.
        for _ in range(50):
            if not stat.exists() or stat.read_text().split(")", 1)[1].strip().split()[0] == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail("grandchild remained running after process-group cleanup")
        self.assertFalse(self.output.exists())

    def test_verification_timeout_never_passes(self):
        self.assertEqual(self.run_gate(), 0)
        with mock.patch.object(gate, "_run_owned_worker", side_effect=subprocess.TimeoutExpired("worker", 1)):
            result = self.verify()
        self.assertEqual(result["rc"], 6)
        self.assertEqual(result["reason_codes"], ["deadline_exceeded"])

    def test_verification_memory_budget_never_escapes(self):
        self.assertEqual(self.run_gate(), 0)
        with mock.patch.object(gate.core, "load_json", side_effect=MemoryError):
            try:
                result = self.verify()
            except MemoryError:
                self.fail("verification MemoryError escaped the stage boundary")
        self.assertEqual(result["rc"], 6)
        self.assertEqual(result["reason_codes"], ["memory_budget"])


if __name__ == "__main__":
    unittest.main()
