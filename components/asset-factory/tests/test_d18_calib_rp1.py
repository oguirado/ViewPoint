"""CALIB-PREP exposure, cross-split PCM, and rebuilt witness regressions."""

import copy
import json
import unittest
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

import test_n18_calib_protocol as baseline
from test_n18_calib_protocol import FixtureAnchor, fixture, freeze_fixture, read_witness, run, run_with_frozen
from tools.calib.annotations import ONSET
from tools.calib.ledger import ExposureLedger
from tools.calib.pcm import compare_pcm
from tools.calib.protocol import CalibrationError, CalibrationResult, digest, encode, verify_witness


class RepairTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "exposures.jsonl"
        self.anchor = FixtureAnchor(self.path)
        self.ledger = ExposureLedger.initialize(self.path, anchor=self.anchor)

    def test_requires_an_existing_ledger_pin_and_records_before_open(self):
        protocol, cases = fixture()
        raw = encode(protocol)
        with self.assertRaisesRegex(CalibrationError, "protocol_digest_changed"):
            freeze_fixture(raw, ledger=self.ledger, cases=cases)
        self.ledger.approve(raw)
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_history_exists"):
            ExposureLedger.initialize(self.path, anchor=self.anchor)
        held = protocol["cases"][1]
        expected = sorted(held[key] for key in ("audio_digest", "annotation_digest", "alignment_digest"))
        opened = []

        def reader(case_id):
            records = self.path.read_bytes().splitlines(keepends=True)
            entry = json.loads(records[-1])
            self.assertEqual(entry["protocol_digest"], digest(raw))
            self.assertEqual(entry["held_out_digests"], expected)
            self.assertEqual(set(entry["held_out_pcm"]), {held["audio_digest"]})
            self.assertEqual(entry["previous"], digest(records[-2]))
            self.assertEqual(self.anchor.read(self.path), digest(records[-1]))
            opened.append(case_id)
            return cases[case_id]

        self.assertEqual(read_witness(freeze_fixture(raw, ledger=self.ledger, cases=cases).evaluate(reader))["rc"], 0)
        self.assertEqual(opened, ["trial-cal", "trial-held"])

    def test_failed_durability_and_corrupt_ledger_refuse_before_open(self):
        protocol, cases = fixture()
        raw = encode(protocol)
        self.ledger.approve(raw)
        frozen = freeze_fixture(raw, ledger=self.ledger, cases=cases)
        with patch("tools.calib.ledger.os.fsync", side_effect=OSError("injected disk failure")):
            with self.assertRaisesRegex(CalibrationError, "exposure_ledger_unavailable"):
                frozen.evaluate(lambda _: self.fail("opened without durable exposure"))
        with self.path.open("ab") as file:
            file.write(b'{"protocol_digest":')
        with self.assertRaisesRegex(CalibrationError, "invalid_exposure_ledger"):
            freeze_fixture(raw, ledger=ExposureLedger(self.path, anchor=self.anchor), cases=cases)

    def test_pre_frozen_changed_protocol_and_split_swap_are_blocked(self):
        protocol, cases = fixture()
        raw = encode(protocol)
        edited = copy.deepcopy(protocol)
        edited["limits"][ONSET] += 1
        changed = encode(edited)
        for version in (raw, changed):
            self.ledger.approve(version)
        stale = freeze_fixture(changed, ledger=ExposureLedger(self.path, anchor=self.anchor), cases=cases)
        freeze_fixture(raw, ledger=self.ledger, cases=cases).evaluate(cases.get)
        with self.assertRaisesRegex(CalibrationError, "held_out_protocol_changed"):
            stale.evaluate(lambda _: self.fail("opened from a stale freeze"))

        protocol["cases"][0]["split"] = "held-out"
        protocol["cases"][1]["split"] = "calibration"
        swapped = encode(protocol)
        self.ledger.approve(swapped)
        with self.assertRaisesRegex(CalibrationError, "held_out_protocol_changed"):
            freeze_fixture(swapped, ledger=ExposureLedger(self.path, anchor=self.anchor), cases=cases)

    def test_overlapping_evaluators_have_one_reader(self):
        protocol, cases = fixture()
        raw = encode(protocol)
        self.ledger.approve(raw)
        start = Barrier(2)
        opened = []

        def evaluate():
            frozen = freeze_fixture(raw, ledger=ExposureLedger(self.path, anchor=self.anchor), cases=cases)
            start.wait(timeout=5)
            try:
                return read_witness(frozen.evaluate(reader))["rc"]
            except CalibrationError as exc:
                return str(exc)

        def reader(case_id):
            opened.append(case_id)
            return cases[case_id]

        with ThreadPoolExecutor(max_workers=2) as executor:
            flights = [executor.submit(evaluate) for _ in range(2)]
            self.assertCountEqual([flight.result(timeout=10) for flight in flights],
                                  [0, "evaluation_already_started"])
        self.assertEqual(opened, ["trial-cal", "trial-held"])

    def test_pcm_refuses_one_lsb_change_and_cropped_copy(self):
        variants = {
            "one_lsb": fixture(held_samples=[200, -40, 100, 61]),
            "crop": fixture(held_samples=[200, -40, 100, 60], calibration_tail_frames=2),
        }
        for label, (protocol, cases) in variants.items():
            with self.subTest(label=label):
                self.assertNotEqual(protocol["cases"][0]["audio_digest"], protocol["cases"][1]["audio_digest"])
                result = run(protocol, cases)
                witness = read_witness(result)
                self.assertEqual(witness["rc"], 4, witness)
                self.assertEqual(witness["attempts"][1]["reason_codes"], ["cross_split_pcm_match"])
                self.assertEqual(witness["splits"]["held-out"], {"expected": 1, "passed": 0, "refused": 1})
                self.assertEqual(result.qualifications, ())

    def test_pcm_correlation_aligns_an_interior_crop_and_normalizes_gain(self):
        calibration = (10, [array("h", [900, -777, 200, -40, 100, 60, 222, -111])])
        held = (10, [array("h", [-400, 80, -200, -120])])
        self.assertTrue(compare_pcm(calibration, held, 100)[0])
        exact_crop = (10, [calibration[1][0][2:6]])
        self.assertTrue(compare_pcm(calibration, exact_crop, 0)[0])
        with self.assertRaisesRegex(CalibrationError, "pcm_split_correlation_budget_exceeded"):
            compare_pcm(calibration, held, 19)
        with self.assertRaisesRegex(CalibrationError, "pcm_split_sample_rate_unqualified"):
            compare_pcm(calibration, (20, held[1]), 100)

    def test_witness_rebuilt_before_base_reads_even_with_forged_witness_pin(self):
        protocol, cases = fixture()
        result, frozen = run_with_frozen(protocol, cases, ledger=self.ledger)
        protocol_pin = digest(encode(protocol))
        verify_witness(result, frozen, approved_protocol_digest=protocol_pin)
        controls = [
            ("witness_protocol_digest_changed", lambda body: body.update(protocol_digest=digest(b"other protocol"))),
            ("witness_case_split_changed", lambda body: body["attempts"][1].update(case_id="other-case")),
            ("witness_case_split_changed", lambda body: body["attempts"][1].update(split="calibration")),
            ("witness_artifact_digest_changed", lambda body: body["attempts"][1]["artifact_digests"].update(
                body["attempts"][0]["artifact_digests"])),
            ("witness_artifact_digest_changed", lambda body: body["attempts"][1]["artifact_digests"].update(
                render=digest(b"alternate render"))),
            ("witness_rebuild_mismatch", lambda body: body["attempts"][1]["metrics"][ONSET].update(value=[0, 1])),
        ]
        review = baseline.ProtocolTests()
        for reason, mutate in controls:
            with self.subTest(reason=reason, mutate=mutate):
                body = read_witness(result)
                mutate(body)
                witness = encode(body)
                pin = digest(witness)
                forged = CalibrationResult(witness, tuple(
                    encode({**json.loads(qualification), "witness_digest": pin})
                    for qualification in result.qualifications
                ))
                with self.assertRaisesRegex(CalibrationError, reason):
                    verify_witness(forged, frozen, approved_protocol_digest=protocol_pin)
                self.assertIsNone(forged.read_checked("held-out-calibration", pin, frozen_protocol=frozen,
                                                       approved_protocol_digest=protocol_pin,
                                                       approved_fixture_witness_digest=pin))
                self.assertEqual(review.assess_base(protocol, forged, frozen=frozen, approved=pin)["rc"], 3)
        with self.assertRaisesRegex(CalibrationError, "witness_protocol_digest_changed"):
            verify_witness(result, frozen, approved_protocol_digest=digest(b"stale reviewer pin"))
        changed_qualification = encode({**json.loads(result.qualifications[0]), "limit": 1})
        forged = replace(result, qualifications=(changed_qualification, *result.qualifications[1:]))
        with self.assertRaisesRegex(CalibrationError, "witness_rebuild_mismatch"):
            verify_witness(forged, frozen, approved_protocol_digest=protocol_pin)

    def test_reopened_verifier_requires_original_artifacts_and_sealed_witness(self):
        protocol, cases = fixture()
        raw = encode(protocol)
        protocol_pin = self.ledger.approve(raw)
        frozen = freeze_fixture(raw, ledger=self.ledger, cases=cases)
        test = self

        class UnopenedAttempts(dict):
            def __getitem__(self, key):
                test.fail("verification opened artifacts before the ledger recorded a result")

        with self.assertRaisesRegex(CalibrationError, "recorded_witness_unavailable"):
            verify_witness(run(protocol, cases), frozen, approved_protocol_digest=protocol_pin,
                           retained_attempts=UnopenedAttempts(cases))
        result = frozen.evaluate(cases.get)
        reopened = freeze_fixture(raw, ledger=ExposureLedger(self.path, anchor=self.anchor), cases=cases)
        with self.assertRaisesRegex(CalibrationError, "attempt_evidence_unavailable"):
            verify_witness(result, reopened, approved_protocol_digest=protocol_pin)
        with self.assertRaisesRegex(CalibrationError, "retained_case_coverage_changed"):
            verify_witness(result, reopened, approved_protocol_digest=protocol_pin, retained_attempts={})
        witness_pin = digest(result.witness)
        self.assertEqual(result.read_checked(
            "held-out-calibration", witness_pin, frozen_protocol=reopened, approved_protocol_digest=protocol_pin,
            approved_fixture_witness_digest=witness_pin, retained_attempts=cases,
        ), result.witness)

        held = cases["trial-held"]
        render = json.loads(held.render)
        render["frames"][3]["jaw_ppm"] += 1
        replacement = {**cases, "trial-held": replace(held, render=encode(render))}
        forged = run(protocol, replacement)
        self.assertEqual(read_witness(forged)["rc"], 0)
        with self.assertRaisesRegex(CalibrationError, "recorded_witness_changed"):
            verify_witness(forged, reopened, approved_protocol_digest=protocol_pin, retained_attempts=replacement)
