"""Cross-protocol PCM and external ledger-head regressions."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_n18_calib_protocol import FixtureAnchor, fixture, freeze_fixture, read_witness, run
from tools.calib.annotations import ONSET
from tools.calib.ledger import ExposureLedger
from tools.calib.protocol import CalibrationError, digest, encode, freeze


class ExposureTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "exposure.jsonl"
        self.anchor = FixtureAnchor(self.path)
        self.ledger = ExposureLedger.initialize(self.path, anchor=self.anchor)

    def reopen(self):
        return ExposureLedger(self.path, anchor=FixtureAnchor(self.path))

    def approve(self, protocol, cases):
        raw = encode(protocol)
        self.ledger.approve(raw)
        return raw, freeze_fixture(raw, ledger=self.ledger, cases=cases)

    def test_exposed_pcm_rejected_in_both_splits_at_freeze_and_evaluation(self):
        protocol, cases = fixture()
        protocol["limits"][ONSET] = 1
        _, first = self.approve(protocol, cases)
        copy_samples = [-300, 200, 400, -79]
        variants = {
            "held-out": fixture(held_samples=copy_samples, held_id="new-held"),
            "calibration": fixture(calibration_samples=copy_samples, held_samples=[700, 200, -300, 100],
                                   held_id="independent-held"),
        }
        pre_frozen = {split: self.approve(candidate, attempts)
                      for split, (candidate, attempts) in variants.items()}

        self.assertEqual(read_witness(first.evaluate(cases.get))["rc"], 5)
        for split, (raw, frozen) in pre_frozen.items():
            with self.subTest(split=split):
                cases_for_split = variants[split][1]
                with self.assertRaisesRegex(CalibrationError, "exposed_held_out_pcm_match") as raised:
                    freeze_fixture(raw, ledger=self.reopen(), cases=cases_for_split)
                self.assertEqual(raised.exception.rc, 4)
                with self.assertRaisesRegex(CalibrationError, "exposed_held_out_pcm_match") as raised:
                    frozen.evaluate(lambda _: self.fail("attempt opened after another protocol exposed its PCM"))
                self.assertEqual(raised.exception.rc, 4)

        fresh, fresh_cases = fixture(held_samples=[700, 200, -300, 100], held_id="independent-held")
        _, independent = self.approve(fresh, fresh_cases)
        self.assertEqual(read_witness(independent.evaluate(fresh_cases.get))["rc"], 0)

    def test_line_aligned_rollback_and_reinitialization_refused_after_reopen(self):
        protocol, cases = fixture()
        raw, frozen = self.approve(protocol, cases)
        prefix = self.path.read_bytes()
        self.assertEqual(read_witness(frozen.evaluate(cases.get))["rc"], 0)
        head = self.anchor.read(self.path)
        self.assertNotEqual(digest(prefix.splitlines(keepends=True)[-1]), head)
        self.path.write_bytes(prefix)
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_head_changed") as raised:
            freeze_fixture(raw, ledger=self.reopen(), cases=cases)
        self.assertEqual(raised.exception.rc, 4)
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_head_changed"):
            self.reopen().approve(encode({**protocol, "limits": {**protocol["limits"], ONSET: 1}}))

        self.path.unlink()
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_unavailable"):
            frozen.evaluate(lambda _: self.fail("attempt opened after ledger deletion"))
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_history_exists") as raised:
            ExposureLedger.initialize(self.path, anchor=FixtureAnchor(self.path))
        self.assertEqual(raised.exception.rc, 4)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.anchor.read(self.path), head)

    def test_broken_chain_and_canonical_rechaining_refused(self):
        protocol, cases = fixture()
        raw, frozen = self.approve(protocol, cases)
        frozen.evaluate(cases.get)
        self.ledger.approve(encode({**protocol, "limits": {**protocol["limits"], ONSET: 1}}))
        lines = self.path.read_bytes().splitlines(keepends=True)
        entry = json.loads(lines[2])
        broken = encode({**entry, "previous": digest(b"wrong predecessor")})
        self.path.write_bytes(b"".join([*lines[:2], broken, *lines[3:]]))
        with self.assertRaisesRegex(CalibrationError, "broken_exposure_chain") as raised:
            freeze_fixture(raw, ledger=self.reopen(), cases=cases)
        self.assertEqual(raised.exception.rc, 3)

        sealing_event = json.loads(lines[-2])
        replacement = encode({**sealing_event, "witness_digest": digest(b"unapproved witness")})
        approval = encode({**json.loads(lines[-1]), "previous": digest(replacement)})
        self.path.write_bytes(b"".join([*lines[:-2], replacement, approval]))
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_head_changed"):
            freeze_fixture(raw, ledger=self.reopen(), cases=cases)

    def test_anchor_failure_refuses_before_opening_attempts(self):
        protocol, cases = fixture()
        raw, frozen = self.approve(protocol, cases)
        with patch.object(self.anchor, "advance", return_value=False):
            with self.assertRaisesRegex(CalibrationError, "exposure_anchor_changed"):
                frozen.evaluate(lambda _: self.fail("attempt opened without a confirmed anchor advance"))
        with self.assertRaisesRegex(CalibrationError, "exposure_ledger_head_changed"):
            freeze_fixture(raw, ledger=self.reopen(), cases=cases)
        with self.assertRaisesRegex(CalibrationError, "exposure_anchor_unavailable"):
            freeze_fixture(raw, ledger=ExposureLedger(self.path, anchor=None), cases=cases)

    def test_crop_meeting_minimum_is_stale_and_shorter_signal_is_unavailable(self):
        protocol, cases = fixture(held_samples=[-40, 100, 60, 0])
        witness = read_witness(run(protocol, cases))
        self.assertEqual(witness["pcm_min_match_frames"], 3)
        self.assertEqual(witness["rc"], 4)
        self.assertEqual(witness["attempts"][1]["reason_codes"], ["cross_split_pcm_match"])
        protocol, cases = fixture(held_samples=[-40, 100, 0, 0])
        with self.assertRaisesRegex(CalibrationError, "pcm_split_overlap_unavailable") as raised:
            self.approve(protocol, cases)
        self.assertEqual(raised.exception.rc, 3)

    def test_freeze_requires_digest_bound_audio_preflight(self):
        protocol, cases = fixture()
        raw = encode(protocol)
        self.ledger.approve(raw)
        with self.assertRaisesRegex(CalibrationError, "calibration_input_audio_unavailable"):
            freeze(raw, ledger=self.ledger, read_audio=lambda _: None)
        with self.assertRaisesRegex(CalibrationError, "audio_digest_changed") as raised:
            freeze(raw, ledger=self.ledger, read_audio=lambda _: cases["trial-held"].audio)
        self.assertEqual(raised.exception.rc, 4)
        self.assertEqual(read_witness(freeze_fixture(raw, ledger=self.ledger, cases=cases).evaluate(cases.get))["rc"], 0)
