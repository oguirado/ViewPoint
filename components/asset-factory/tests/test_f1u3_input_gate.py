"""Synthetic fixtures only; calibration constants here are not production policy."""
import contextlib
import ast
import hashlib
import io
import inspect as source_inspect
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

from gates import input_gate as gate


def calibration(chosen):
    # Synthetic test calibration, never production evidence.
    return gate.core.canonical_json({"schema": "af.input-calibration/v1", "rc": 0, "verdict": "PASS",
        "policy_sha256": gate.core.sha256_json({k: v for k, v in chosen.items() if k != "calibration_ref"})}).encode()


def provider_fixture():
    return {"schema": "af.provider/v1", "id": "birefnet", "slot": "generate", "kind": "model",
            "runnable": True, "revision": "synthetic-fixture-immutable-revision",
            "licence": {"code": "SYNTHETIC", "weights": "SYNTHETIC", "deps": []},
            "territory_excluded": [], "commercial": True, "gated": False,
            "weights": [{"repo": "synthetic-fixture", "revision": "synthetic-weights-pin",
                         "files": [{"path": "fixture.bin", "sha256": hashlib.sha256(b"fixture weight").hexdigest()}]}],
            "runtime": {"container": "synthetic-fixture", "ram_peak_gb_measured": None,
                        "ram_peak_measurement_ref": None}, "known_issues": []}


def preparation_context():
    return {"provider": provider_fixture(), "configuration_sha256": "a" * 64,
            "licence_policy_sha256": "c" * 64, "jurisdiction": "ES", "commercial_use": False,
            "code_sha256": hashlib.sha256(b"synthetic fixture code").hexdigest()}


def png(alpha=None, *, mode="RGBA", width=16, height=16, dark=True):
    if alpha is None:
        alpha = [255 if 5 <= x < 11 and 5 <= y < 11 else 0
                 for y in range(height) for x in range(width)]
    channel_count, color_type = {"RGBA": (4, 6), "RGB": (3, 2), "LA": (2, 4), "P": (1, 3)}[mode]
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            a = alpha[y * width + x]
            color = 2 if dark else 220
            pixels.extend({"RGBA": bytes((color, color, color, a)), "RGB": bytes((color, color, color)),
                           "LA": bytes((color, a)), "P": bytes((1 if a else 0,))}[mode])

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    result = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    if mode == "P":
        result += chunk(b"PLTE", bytes((255, 255, 255, 2, 2, 2))) + chunk(b"tRNS", bytes((0, 255)))
    return result + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b"")


def policy(**updates):
    value = {"schema": "af.input-policy/v1", "min_side_px": 16, "max_encoded_bytes": 65536,
             "max_decoded_pixels": 4096, "alpha_background_max": 0, "alpha_foreground_min": 255,
             "min_foreground_ppm": 100000, "max_foreground_ppm": 700000,
             "max_border_foreground_ppm": 0, "max_small_component_ppm": 0, "max_soft_alpha_ppm": 100000,
             "small_component_max_pixels": 2, "max_components": 1, "connectivity": 8,
             "ppm_rounding": "floor", "calibration_ref": {"path": "calibration.txt", "sha256": "0" * 64}}
    value.update(updates)
    if value.get("calibration_ref") is not None:
        try:
            value["calibration_ref"]["sha256"] = hashlib.sha256(calibration(value)).hexdigest()
        except gate.core.ContractError:
            pass
    return value


def inspect(data=None, chosen=None, decoder=None):
    chosen = policy() if chosen is None else chosen
    try:
        policy_hash = gate.core.sha256_json(chosen)
    except gate.core.ContractError:
        # Intentionally malformed policies still reach the validator, which runs
        # before identity comparison; no floating-point canonical hash exists.
        policy_hash = "0" * 64
    with contextlib.ExitStack() as stack:
        if decoder is not None:
            stack.enter_context(mock.patch.object(gate, "PillowDecoder", return_value=decoder))
        return gate.inspect_reference(png() if data is None else data, policy=chosen,
                                      policy_sha256=policy_hash,
                                      toolchain_sha256=gate.toolchain_identity(), calibration_bytes=calibration(chosen) if "schema" in chosen and chosen.get("calibration_ref") is not None and policy_hash != "0" * 64 else None)


class QualityTests(unittest.TestCase):
    def refused(self, data, rule, chosen=None, rc=5):
        result = inspect(data, chosen)
        self.assertEqual(result["rc"], rc)
        self.assertEqual(result["status"], gate.core.STATUS[rc])
        self.assertEqual(result["verdict"], gate.core.VERDICT[rc])
        self.assertIn(rule, result["reason_codes"])
        expected = {"opaque_mask": ["opaque_mask", "foreground_occupancy", "border_leakage"],
                    "foreground_occupancy": ["foreground_occupancy", "small_components", "component_count"]
                    if result["metrics"] and result["metrics"]["foreground_pixels"] == 0 else ["foreground_occupancy"],
                    "small_components": ["small_components"]}.get(rule, [rule])
        self.assertEqual(result["reason_codes"], expected)
        return result

    def test_clean_rgba_reference_passes_without_rewrite(self):
        data = png(dark=False)
        before = bytes(data)
        result = inspect(data)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(data, before)
        self.assertEqual(result["metrics"]["foreground_pixels"], 36)
        self.assertEqual(result["metrics"]["foreground_ppm"], 140625)
        self.assertEqual(result["metrics"]["bbox"], [5, 5, 10, 10])

    def test_dark_foreground_with_clean_alpha_passes(self):
        self.assertEqual(inspect(png(dark=True))["rc"], 0)
        self.assertEqual(inspect(png(dark=True))["metrics"], inspect(png(dark=False))["metrics"])

    def test_transparent_palette_and_la_use_equivalent_masks(self):
        baseline = inspect(png())
        for mode in ("LA", "P"):
            with self.subTest(mode=mode):
                decoded = gate.PillowDecoder().decode(png(mode=mode), max_pixels=4096)
                self.assertEqual(decoded[:3], gate.PillowDecoder().decode(png(), max_pixels=4096)[:3])
                # Explicitly encode the equivalent mask as RGBA; gate never rewrites.
                from PIL import Image
                image = Image.open(io.BytesIO(png(mode=mode))).convert("RGBA")
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                result = inspect(stream.getvalue())
                self.assertEqual(result["rc"], 0)
                self.assertEqual(result["metrics"], baseline["metrics"])
                self.assertNotEqual(result["source_sha256"], baseline["source_sha256"])

    def test_thin_attached_structure_passes(self):
        alpha = [255 if 5 <= x < 11 and 5 <= y < 11 or x == 7 and 2 <= y <= 5 else 0
                 for y in range(16) for x in range(16)]
        self.assertEqual(inspect(png(alpha))["rc"], 0)

    def test_opaque_rgb_cart_refused_before_adapter(self):
        # Preserve the legacy spy assertion, but credit only the measured gate
        # refusal: this unit contains no real provider dispatch path.
        adapter = mock.Mock()
        result = self.refused(png(mode="RGB"), "opaque_mask")
        if result["rc"] == 0:
            adapter(result)
        adapter.assert_not_called()
        self.assertEqual(result["rc"], 5)

    def test_dark_background_opaque_rgba_refused(self):
        self.refused(png([255] * 256), "opaque_mask")

    def test_opaque_refused_even_under_permissive_thresholds(self):
        self.refused(png([255] * 256), "opaque_mask",
                     policy(max_foreground_ppm=1000000, max_border_foreground_ppm=1000000))

    def test_empty_alpha_refused_with_unknown_small_fraction(self):
        result = self.refused(png([0] * 256), "foreground_occupancy")
        self.assertIsNone(result["metrics"]["small_component_ppm"])

    def test_low_occupancy_refused(self):
        alpha = [255 if 6 <= x < 9 and 6 <= y < 9 else 0 for y in range(16) for x in range(16)]
        self.refused(png(alpha), "foreground_occupancy")

    def test_high_occupancy_refused(self):
        alpha = [255 if 1 <= x < 15 and 1 <= y < 15 else 0 for y in range(16) for x in range(16)]
        self.refused(png(alpha), "foreground_occupancy")

    def test_speck_alpha_refused(self):
        alpha = [255 if x in (2, 5, 8, 11, 14) and y in (2, 5, 8, 11, 14) else 0
                 for y in range(16) for x in range(16)]
        self.refused(png(alpha), "small_components", policy(min_foreground_ppm=0, max_components=30))

    def test_border_connected_background_refused(self):
        alpha = [255 if 5 <= x < 11 and y < 11 else 0 for y in range(16) for x in range(16)]
        self.refused(png(alpha), "border_leakage")

    def test_halo_refused(self):
        alpha = [255 if 5 <= x < 11 and 5 <= y < 11 else 127 if 2 <= x < 14 and 2 <= y < 14 else 0
                 for y in range(16) for x in range(16)]
        self.refused(png(alpha), "soft_alpha")

    def test_detached_components_refused(self):
        alpha = [255 if (3 <= x < 7 or 10 <= x < 14) and 5 <= y < 11 else 0
                 for y in range(16) for x in range(16)]
        self.refused(png(alpha), "component_count")

    def test_low_resolution_refused(self):
        self.refused(png(), "resolution", policy(min_side_px=17))

    def test_truncated_image_refused(self):
        self.refused(png()[:-14], "image_invalid", rc=2)

    def test_encoded_budget_refused_before_decode(self):
        decoder = mock.Mock()
        result = inspect(png(), policy(max_encoded_bytes=1), decoder=decoder)
        self.assertEqual(result["rc"], 6)
        self.assertIn("encoded_byte_budget", result["reason_codes"])
        self.assertEqual(result["reason_codes"], ["encoded_byte_budget"])
        decoder.decode.assert_not_called()

    def test_pixel_budget_refused_before_pixel_allocation(self):
        from PIL import Image
        with mock.patch.object(Image.Image, "load", side_effect=AssertionError("must reject before load")):
            self.refused(png(), "decoded_pixel_budget", policy(max_decoded_pixels=255), rc=6)

    def test_decoder_unavailable_cannot_see(self):
        decoder = mock.Mock()
        decoder.decode.side_effect = gate.GateIssue(3, "decoder_unavailable")
        result = inspect(decoder=decoder)
        self.assertEqual(result["rc"], 3)
        self.assertIsNone(result["metrics"])
        self.assertEqual(result["reason_codes"], ["decoder_unavailable"])

    def test_decoder_contract_cannot_authorize_missing_alpha(self):
        decoder = mock.Mock()
        decoder.decode.return_value = (16, 16, b"")
        result = inspect(decoder=decoder)
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["reason_codes"], ["decoder_contract"])


class IdentityTests(unittest.TestCase):
    def test_missing_uncalibrated_and_invalid_policies(self):
        self.assertEqual(inspect()["rc"], 0)
        for key in gate.POLICY_KEYS:
            value = policy()
            del value[key]
            with self.subTest(missing=key):
                result = inspect(chosen=value)
                self.assertEqual(result["rc"], 2)
                self.assertEqual(result["reason_codes"], ["policy_invalid"])
        result = inspect(chosen=policy(calibration_ref=None))
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["reason_codes"], ["policy_uncalibrated"])
        for changes in ({"min_side_px": True}, {"max_encoded_bytes": 1.0}, {"connectivity": 6},
                        {"ppm_rounding": "nearest"}, {"alpha_foreground_min": 0},
                        {"max_foreground_ppm": 1000001}, {"extra": 1}):
            with self.subTest(changes=changes):
                result = inspect(chosen=policy(**changes))
                self.assertEqual(result["rc"], 2)
                self.assertEqual(result["reason_codes"], ["policy_invalid"])

    def test_changed_source_policy_or_toolchain_is_stale(self):
        chosen = policy()
        result = gate.inspect_reference(png(), policy=chosen, policy_sha256="0" * 64,
                                        toolchain_sha256=gate.toolchain_identity())
        self.assertEqual(result["rc"], 4)
        result = gate.inspect_reference(png(), policy=chosen, policy_sha256=gate.core.sha256_json(chosen),
                                        toolchain_sha256="0" * 64)
        self.assertEqual(result["rc"], 4)
        self.assertEqual(inspect()["rc"], 0)

    def prepared(self):
        original, candidate = png(mode="RGB"), png()
        decision = {"schema": "af.licence-decision/v1", "rc": 0, "verdict": "PASS", "allowed": True,
                    "provider_id": "birefnet", "operation": "generate", "jurisdiction": "ES", "commercial_use": False,
                    "monetisable": True, "reason_codes": [], "evaluated_dependencies": [],
                    "configuration_sha256": "a" * 64, "provider_sha256": gate.core.sha256_json(provider_fixture()), "policy_sha256": "c" * 64}
        receipt = {"schema": "af.reference-preparation/v1", "rc": 0, "status": "OK", "provider_id": "birefnet",
                   "revision": "synthetic-fixture-immutable-revision", "code_sha256": preparation_context()["code_sha256"],
                   "weights_sha256": gate.core.sha256_json(provider_fixture()["weights"]), "original_sha256": hashlib.sha256(original).hexdigest(),
                   "candidate_sha256": hashlib.sha256(candidate).hexdigest(), "decision_sha256": gate.core.sha256_json(decision)}
        return original, candidate, receipt, decision

    def prep_inspect(self, original, candidate, receipt, decision):
        chosen = policy()
        return gate.inspect_prepared_reference(original, candidate, preparation_receipt=receipt, decision=decision,
                                               policy=chosen, policy_sha256=gate.core.sha256_json(chosen),
                                               toolchain_sha256=gate.toolchain_identity(),
                                               calibration_bytes=calibration(chosen), **preparation_context())

    def test_prepared_candidate_requires_a_pinned_receipt(self):
        original, candidate, receipt, decision = self.prepared()
        self.assertEqual(self.prep_inspect(original, candidate, receipt, decision)["rc"], 0)
        for key, value, rc in (("revision", "main", 2), ("code_sha256", "", 2), ("rc", True, 2), ("rc", 1, 2),
                               ("original_sha256", "0" * 64, 4), ("candidate_sha256", "0" * 64, 4),
                               ("decision_sha256", "0" * 64, 4), ("status", "FAIL", 3)):
            changed = dict(receipt, **{key: value})
            with self.subTest(key=key):
                result = self.prep_inspect(original, candidate, changed, decision)
                self.assertEqual(result["rc"], rc)
                self.assertEqual(result["reason_codes"], [{"revision": "preparation_unpinned",
                    "code_sha256": "preparation_invalid", "rc": "preparation_invalid",
                    "status": "preparation_unproven"}.get(key, "preparation_stale")])
        decision["allowed"] = False
        receipt["decision_sha256"] = gate.core.sha256_json(decision)
        result = self.prep_inspect(original, candidate, receipt, decision)
        self.assertEqual(result["rc"], 2, result)
        self.assertEqual(result["reason_codes"], ["preparation_invalid"])

    def test_preparation_exception_or_missing_receipt_cannot_pass(self):
        original, candidate, receipt, decision = self.prepared()
        self.assertEqual(self.prep_inspect(original, candidate, None, decision)["rc"], 3)
        self.assertEqual(self.prep_inspect(original, candidate, receipt, None)["rc"], 3)
        receipt.update(rc=3, status="INCOMPLETE")
        self.assertEqual(self.prep_inspect(original, candidate, receipt, decision)["rc"], 3)

    def test_preparation_cannot_bypass_candidate_quality(self):
        original, _, receipt, decision = self.prepared()
        candidate = png([255] * 256)
        receipt["candidate_sha256"] = hashlib.sha256(candidate).hexdigest()
        self.assertEqual(self.prep_inspect(original, candidate, receipt, decision)["rc"], 5)

    def test_malformed_preparation_is_error(self):
        original, candidate, receipt, decision = self.prepared()
        self.assertEqual(self.prep_inspect(None, candidate, receipt, decision)["rc"], 2)
        self.assertEqual(self.prep_inspect(original, None, receipt, decision)["rc"], 2)
        self.assertEqual(self.prep_inspect(original, candidate, [], decision)["rc"], 2)
        self.assertEqual(self.prep_inspect(original, candidate, receipt, [])["rc"], 2)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image, self.policy_path, self.output = self.root / "reference.png", self.root / "policy.json", self.root / "bundle"
        self.image.write_bytes(png())
        self.chosen = policy()
        self.policy_path.write_text(gate.core.canonical_json(self.chosen))
        (self.root / "calibration.txt").write_bytes(calibration(self.chosen))

    def run_gate(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.run(self.image, self.output, policy_path=self.policy_path, timeout_seconds=kwargs.get("timeout", 10))
        self.assertEqual(out.getvalue(), gate.core.canonical_json(json.loads(out.getvalue())))
        self.assertEqual(json.loads(out.getvalue())["rc"], rc)
        return rc

    def verify(self, **kwargs):
        return gate.verify_receipt(self.output, expected_source_sha256=kwargs.get("source", hashlib.sha256(png()).hexdigest()),
                                   expected_policy_sha256=kwargs.get("policy_hash", gate.core.sha256_json(self.chosen)),
                                   expected_toolchain_sha256=kwargs.get("toolchain", gate.toolchain_identity()))

    def test_receipt_binds_exact_input_and_canonical_result(self):
        self.assertEqual(self.run_gate(), 0)
        self.assertEqual(self.verify()["rc"], 0)
        self.assertEqual((self.output / "reference.image").read_bytes(), self.image.read_bytes())
        for name in ("receipt.json", "input_gate.json", "policy.json"):
            data = (self.output / name).read_text()
            self.assertEqual(data, gate.core.canonical_json(json.loads(data)))
        self.assertEqual(json.loads((self.output / "input_gate.json").read_text())["schema"], "af.input-gate/v1")
        self.assertEqual(json.loads((self.output / "receipt.json").read_text())["schema"], "af.input-gate-receipt/v1")

    def test_isolated_cli_success_and_usage_errors(self):
        argv = [sys.executable, "-B", "-m", "gates.input_gate",
                "--input", str(self.image), "--output-dir", str(self.output),
                "--policy", str(self.policy_path), "--timeout-seconds", "10"]
        success = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["verdict"], "PASS")
        self.assertEqual(self.verify()["rc"], 0)
        refusal = subprocess.run(argv[:4], capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(refusal.returncode, 2)
        self.assertEqual(json.loads(refusal.stdout)["reason_codes"], ["usage_invalid"])

    def test_bound_source_policy_and_toolchain_drift_refused(self):
        self.assertEqual(self.run_gate(), 0)
        for kwargs in ({"source": "0" * 64}, {"policy_hash": "0" * 64}, {"toolchain": "0" * 64}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.verify(**kwargs)["rc"], 4)

    def test_each_tampered_artifact_is_stale(self):
        self.assertEqual(self.run_gate(), 0)
        for name in ("reference.image", "input_gate.json", "policy.json", "calibration.evidence"):
            path = self.output / name
            original = path.read_bytes()
            if name == "input_gate.json":
                changed = json.loads(original)
                changed["metrics"]["width"] = 99
                path.write_text(gate.core.canonical_json(changed))
            elif name == "policy.json":
                changed = json.loads(original)
                changed["min_side_px"] = 99
                path.write_text(gate.core.canonical_json(changed))
            else:
                path.write_bytes(original + b"tampered")
            with self.subTest(name=name):
                self.assertEqual(self.verify()["rc"], 4)
            path.write_bytes(original)
        self.assertEqual(self.verify()["rc"], 0)

    def test_rehashed_forged_result_still_refused(self):
        self.assertEqual(self.run_gate(), 0)
        result_path, receipt_path = self.output / "input_gate.json", self.output / "receipt.json"
        result, receipt = json.loads(result_path.read_text()), json.loads(receipt_path.read_text())
        result["metrics"]["foreground_pixels"] = 999
        result_path.write_text(gate.core.canonical_json(result))
        receipt["result"]["sha256"] = gate.core.sha256_json(result)
        receipt_path.write_text(gate.core.canonical_json(receipt))
        self.assertEqual(self.verify()["rc"], 4)

    def test_missing_receipt_cannot_see_and_escaping_symlink_errors(self):
        self.assertEqual(self.verify()["rc"], 3)
        self.assertEqual(self.run_gate(), 0)
        path = self.output / "reference.image"
        path.unlink()
        path.symlink_to(self.image)
        self.assertEqual(self.verify()["rc"], 2)

    def test_malformed_receipts_unknown_fields_float_and_bool_are_errors(self):
        self.assertEqual(self.run_gate(), 0)
        receipt_path = self.output / "receipt.json"
        original = receipt_path.read_bytes()
        for data in (b'{"schema":"a","schema":"b"}', b'{"rc":0.5}', b'{"rc":NaN}', b'{}'):
            receipt_path.write_bytes(data)
            self.assertEqual(self.verify()["rc"], 2)
        receipt_path.write_bytes(original)
        result_path = self.output / "input_gate.json"
        result = json.loads(result_path.read_text())
        for update in ({"rc": False}, {"status": "PASS"}, {"extra": 1}):
            changed = dict(result, **update)
            result_path.write_text(gate.core.canonical_json(changed))
            receipt = json.loads(original)
            receipt["result"]["sha256"] = gate.core.sha256_json(changed)
            receipt_path.write_text(gate.core.canonical_json(receipt))
            self.assertEqual(self.verify()["rc"], 2)

    def test_refusal_preserves_existing_artifacts(self):
        self.output.mkdir()
        sentinel = self.output / "receipt.json"
        sentinel.write_bytes(b"caller-owned sentinel")
        for data, rc in ((png([255] * 256), 5), (b"invalid", 2), (png(), 2)):
            self.image.write_bytes(data)
            self.assertEqual(self.run_gate(), rc)
            self.assertEqual(sentinel.read_bytes(), b"caller-owned sentinel")
            self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["receipt.json"])

    def test_every_publication_replace_fault_leaves_no_partial_bundle(self):
        real_replace = gate.os.replace
        for fail_at in range(1, 5):
            calls = []

            def failing_replace(*args, **kwargs):
                calls.append(args)
                if len(calls) == fail_at:
                    raise OSError("synthetic publication failure")
                return real_replace(*args, **kwargs)

            with self.subTest(fail_at=fail_at), mock.patch.object(gate.os, "replace", side_effect=failing_replace):
                self.assertEqual(self.run_gate(), 2)
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob(".input-gate-*")))
            self.assertEqual(self.image.read_bytes(), png())

    def test_timeout_is_budget_and_preserves_outputs(self):
        real_popen = gate.af_proc.popen

        def slow_worker(argv, **kwargs):
            return real_popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], **kwargs)

        self.output.mkdir()
        sentinel = self.output / "receipt.json"
        sentinel.write_bytes(b"caller sentinel")
        with mock.patch.object(gate.af_proc, "popen", side_effect=slow_worker):
            self.assertEqual(self.run_gate(timeout=1), 6)
        self.assertEqual(sentinel.read_bytes(), b"caller sentinel")

    def test_calibration_missing_tampered_and_escape_refused(self):
        path = self.root / "calibration.txt"
        path.unlink()
        self.assertEqual(self.run_gate(), 3)
        path.write_bytes(b"tampered")
        self.assertEqual(self.run_gate(), 4)
        self.chosen["calibration_ref"]["path"] = "../outside.txt"
        self.policy_path.write_text(gate.core.canonical_json(self.chosen))
        self.assertEqual(self.run_gate(), 2)
        self.assertFalse(self.output.exists())

    def test_invalid_timeout_and_unavailable_measurement_preserve_outputs(self):
        self.assertEqual(self.run_gate(timeout=0), 2)
        result = gate._record(3, None, None, None, reason_codes=["decoder_unavailable"])
        with mock.patch.object(gate, "_inspect_worker", return_value=result):
            self.assertEqual(self.run_gate(), 3)
        self.assertFalse(self.output.exists())

    def test_deadline_exceeded_while_staging_cannot_publish(self):
        real_write = gate.core.atomic_write_json

        def expire_on_receipt(target, obj):
            real_write(target, obj)
            if Path(target).name == "receipt.json":
                # Publish has completed staging but the deadline now expires.
                clock.return_value = 1000000000000000000

        with mock.patch.object(gate.time, "monotonic", return_value=1) as clock:
            with mock.patch.object(gate.core, "atomic_write_json", side_effect=expire_on_receipt):
                self.assertEqual(self.run_gate(), 6)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".input-gate-*")))

    def test_worker_binding_and_forbidden_rc_refused(self):
        result = inspect()
        result["source_sha256"] = "0" * 64
        with mock.patch.object(gate, "_inspect_worker", return_value=result):
            self.assertEqual(self.run_gate(), 4)
        completed = subprocess.CompletedProcess([], 1, stdout="{}", stderr="")
        with mock.patch.object(gate, "_run_owned_worker", return_value=completed):
            self.assertEqual(self.run_gate(), 2)
        self.assertFalse(self.output.exists())


class MutationProofTests(unittest.TestCase):
    """Run real assertions against temporarily broken rules; each must turn red."""
    def assert_red(self, case):
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output).run(unittest.TestSuite([case]))
        self.assertFalse(result.wasSuccessful(), output.getvalue())
        self.assertGreater(len(result.failures), 0, output.getvalue())
        print("MUTANT_RED " + case.id() + " failures=" + str(len(result.failures))
              + " errors=" + str(len(result.errors)), file=sys.stderr)

    def test_each_quality_rule_mutant_is_detected(self):
        checks = (("resolution", "test_low_resolution_refused"),
                  ("foreground_occupancy", "test_low_occupancy_refused"),
                  ("border_leakage", "test_border_connected_background_refused"),
                  ("small_components", "test_speck_alpha_refused"),
                  ("soft_alpha", "test_halo_refused"),
                  ("component_count", "test_detached_components_refused"),
                  ("opaque_mask", "test_opaque_refused_even_under_permissive_thresholds"))
        original = gate._quality_findings
        for rule, test_name in checks:
            with self.subTest(rule=rule), mock.patch.object(gate, "_quality_findings",
                    side_effect=lambda m, p, r=rule: [item for item in original(m, p) if item != r]):
                self.assert_red(QualityTests(test_name))

    def test_forced_pass_mutant_is_detected(self):
        original = gate.inspect_reference

        def forced_pass(*args, **kwargs):
            result = original(*args, **kwargs)
            result.update(rc=0, status="OK", verdict="PASS", reason_codes=[])
            return result

        names = ("test_opaque_rgb_cart_refused_before_adapter", "test_dark_background_opaque_rgba_refused",
                 "test_empty_alpha_refused_with_unknown_small_fraction", "test_high_occupancy_refused",
                 "test_truncated_image_refused", "test_encoded_budget_refused_before_decode",
                 "test_pixel_budget_refused_before_pixel_allocation", "test_decoder_unavailable_cannot_see",
                 "test_decoder_contract_cannot_authorize_missing_alpha")
        with mock.patch.object(gate, "inspect_reference", side_effect=forced_pass):
            for name in names:
                with self.subTest(rule=name):
                    self.assert_red(QualityTests(name))
            self.assert_red(IdentityTests("test_missing_uncalibrated_and_invalid_policies"))
            self.assert_red(IdentityTests("test_changed_source_policy_or_toolchain_is_stale"))
            self.assert_red(IdentityTests("test_preparation_cannot_bypass_candidate_quality"))

    def test_preparation_proof_mutant_is_detected(self):
        with mock.patch.object(gate, "inspect_prepared_reference", return_value={"rc": 0}):
            self.assert_red(IdentityTests("test_preparation_exception_or_missing_receipt_cannot_pass"))
            self.assert_red(IdentityTests("test_prepared_candidate_requires_a_pinned_receipt"))
            self.assert_red(IdentityTests("test_malformed_preparation_is_error"))

    def test_receipt_trust_mutant_is_detected(self):
        with mock.patch.object(gate, "verify_receipt", return_value={"rc": 0}):
            for name in ("test_each_tampered_artifact_is_stale", "test_rehashed_forged_result_still_refused",
                         "test_bound_source_policy_and_toolchain_drift_refused",
                         "test_malformed_receipts_unknown_fields_float_and_bool_are_errors",
                         "test_missing_receipt_cannot_see_and_escaping_symlink_errors"):
                self.assert_red(PublicationTests(name))

    def test_runtime_refusal_mutants_are_detected(self):
        original = gate._record
        trials = (("deadline_exceeded", "test_timeout_is_budget_and_preserves_outputs"),
                  ("deadline_exceeded", "test_deadline_exceeded_while_staging_cannot_publish"),
                  ("calibration_missing", "test_calibration_missing_tampered_and_escape_refused"),
                  ("timeout_invalid", "test_invalid_timeout_and_unavailable_measurement_preserve_outputs"),
                  ("worker_binding_stale", "test_worker_binding_and_forbidden_rc_refused"),
                  ("input_or_publication_invalid", "test_every_publication_replace_fault_leaves_no_partial_bundle"),
                  ("output_exists", "test_refusal_preserves_existing_artifacts"))
        for code, name in trials:
            def suppress_refusal(rc, source, policy_hash, toolchain_hash, metrics=None, reason_codes=(), preparation=None):
                if code in reason_codes:
                    return original(0, source, policy_hash, toolchain_hash, metrics, (), preparation)
                return original(rc, source, policy_hash, toolchain_hash, metrics, reason_codes, preparation)
            with self.subTest(rule=code), mock.patch.object(gate, "_record", side_effect=suppress_refusal):
                self.assert_red(PublicationTests(name))


class RuleMutationTests(unittest.TestCase):
    """Remove one actual guard; the corresponding exact-code oracle must go red."""
    def mutant(self, name, code, *, transform=None):
        tree = ast.parse(source_inspect.getsource(getattr(gate, name)))
        changes = []
        class RemoveGuard(ast.NodeTransformer):
            def visit_If(self, node):
                self.generic_visit(node)
                if (not changes and node.body and isinstance(node.body[0], ast.Raise)
                        and code in [part.value for part in ast.walk(node.body[0])
                                     if isinstance(part, ast.Constant)]):
                    changes.append(code)
                    return ast.If(test=ast.Constant(False), body=node.body, orelse=node.orelse)
                return node
        if transform is None:
            tree = RemoveGuard().visit(tree)
            self.assertEqual(changes, [code], "mutant must change exactly its named guard")
        else:
            tree = transform(tree)
        namespace = dict(gate.__dict__)
        exec(compile(ast.fix_missing_locations(tree), "<rule-mutant>", "exec"), namespace)
        return namespace[name]

    def test_binding_licence_symlink_calibration_and_remeasurement_mutants(self):
        from test_f1u3_repair import BindingTests
        trials = (
            ("inspect_prepared_reference", "preparation_stale", "test_original_hash_binding"),
            ("inspect_prepared_reference", "preparation_decision_identity_stale", "test_preparation_licence_flags_are_metadata"),
            ("inspect_prepared_reference", "preparation_decision_stale", "test_current_decision_hashes"),
            ("inspect_prepared_reference", "preparation_provider_stale", "test_code_weights_revision_binding"),
            ("_safe_file", "evidence_path_invalid", "test_symlink_to_identical_bytes"),
            ("_calibration", "calibration_policy_stale", "test_rehashed_calibration_policy_drift"),
            ("verify_receipt", "calibration_stale", "test_calibration_receipt_equality"),
            ("verify_receipt", "result_stale", "test_forged_result_exact_reason"),
            ("run", "calibration_path_escape", "test_existing_escaping_calibration"),
        )
        for name, code, test in trials:
            with self.subTest(rule=code), mock.patch.object(gate, name, self.mutant(name, code)):
                result = unittest.TestResult()
                BindingTests(test).run(result)
                self.assertGreater(len(result.failures), 0, str(result.errors))
                self.assertEqual(result.errors, [])
                print("RULE_MUTANT_RED " + code, file=sys.stderr)

    def test_exclusive_publication_mutant(self):
        from test_f1u3_repair import BindingTests
        def permit_existing(tree):
            changes = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mkdir":
                    node.keywords.append(ast.keyword(arg="exist_ok", value=ast.Constant(True)))
                    changes.append(node)
            self.assertEqual(len(changes), 1)
            return tree
        broken = self.mutant("_publish", None, transform=permit_existing)
        with mock.patch.object(gate, "_publish", broken):
            result = unittest.TestResult()
            BindingTests("test_existing_empty_and_nonempty_outputs_survive").run(result)
            self.assertGreater(len(result.failures), 0, str(result.errors))
            self.assertEqual(result.errors, [])
        print("RULE_MUTANT_RED output_exists (exclusive mkdir bypass)", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
