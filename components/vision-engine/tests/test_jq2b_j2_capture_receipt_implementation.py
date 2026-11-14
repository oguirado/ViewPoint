"""Tests for vadapt/capture_receipt.py: closed legacy receipts, C-PIXEL contract,
gate G J2 2, and invariants INV-J2-1 through INV-J2-4.
"""
import copy
import hashlib
import json
import unittest
import zlib

import numpy as np

from vadapt import capture_receipt as cr


def _valid_receipt(digest=None, tick=42, w=320, h=200, state_q16=65536):
    """Healthy independent control receipt."""
    if digest is None:
        digest = hashlib.sha256(b"healthy_frame_pixels_0").hexdigest()
    return {
        "frame_sha256": digest,
        "dimensions": (w, h),
        "tick": tick,
        "telemetry": {"state_q16": state_q16},
    }


def _valid_evidence(receipt, source_kind="ramws", seq=1, gen=1, epoch=0):
    """Healthy independent publication evidence matching control receipt."""
    raw = b"healthy_frame_pixels_0"
    return cr.PublicationEvidence(
        handle_generation=gen,
        epoch=epoch,
        geometry=receipt["dimensions"],
        completed_publication_identity="pub-handle-001",
        frame_sha256=receipt["frame_sha256"],
        committed_tick=receipt["tick"],
        source_kind=source_kind,
        trusted_association=True,
        residency_observed=True,
        sequence=seq,
        crc32=zlib.crc32(raw),
        raw_bytes=raw,
        coherence_proof=True,
        is_dom_ax_derived=True,
    )


class TestGateGJ22HealthyControl(unittest.TestCase):
    """Gate G J2 2: Healthy independent control vs negative contrasts."""

    def test_healthy_independent_control_passes(self):
        receipt = _valid_receipt()
        evidence = _valid_evidence(receipt)
        result = cr.validate_receipt(receipt, publication_evidence=evidence)
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "OK")
        self.assertIsNone(result.reason)
        self.assertEqual(result.receipt["dimensions"], (320, 200))
        self.assertEqual(result.receipt["tick"], 42)
        self.assertEqual(result.receipt["telemetry"]["state_q16"], 65536)
        self.assertIn("verified", result.evidence)

    def test_input_immutability(self):
        receipt = _valid_receipt()
        original = copy.deepcopy(receipt)
        evidence = _valid_evidence(receipt)
        cr.validate_receipt(receipt, publication_evidence=evidence)
        self.assertEqual(receipt, original)

    def test_validation_alone_is_not_visual_pass_ausente(self):
        """Negative contrast: validation alone without evidence returns BLOCKED (AUSENTE)."""
        receipt = _valid_receipt()
        result = cr.validate_receipt(receipt, publication_evidence=None)
        self.assertEqual(result.status, "BLOCKED")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "E_EVIDENCE_AUSENTE")
        self.assertEqual(result.evidence.get("status"), "AUSENTE")


class TestLegacyReceiptSchemaRejections(unittest.TestCase):
    """Strict validation of the closed legacy hook receipt."""

    def test_reject_commit_id_in_receipt(self):
        """commit_id must never be added to the hook receipt."""
        receipt = _valid_receipt()
        receipt["commit_id"] = "commit_12345"
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_COMMIT_ID_FORBIDDEN")

    def test_reject_extra_keys(self):
        receipt = _valid_receipt()
        receipt["metadata"] = {"richer": "outside"}
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_RECEIPT_EXTRA_KEYS")

    def test_reject_missing_keys(self):
        for key in cr.LEGACY_RECEIPT_KEYS:
            receipt = _valid_receipt()
            del receipt[key]
            with self.assertRaises(cr.ReceiptValidationError) as cm:
                cr.validate_receipt(receipt)
            self.assertEqual(cm.exception.code, "E_RECEIPT_MISSING_KEYS")

    def test_reject_bool_masquerading_as_int_in_dimensions(self):
        for bad_dims in ((True, 200), (320, False), (True, False)):
            receipt = _valid_receipt()
            receipt["dimensions"] = bad_dims
            with self.assertRaises(cr.ReceiptValidationError) as cm:
                cr.validate_receipt(receipt)
            self.assertEqual(cm.exception.code, "E_DIMENSIONS_VALUE")

    def test_reject_non_positive_dimensions(self):
        for bad_dims in ((0, 200), (320, 0), (-320, 200), (320, -200)):
            receipt = _valid_receipt()
            receipt["dimensions"] = bad_dims
            with self.assertRaises(cr.ReceiptValidationError) as cm:
                cr.validate_receipt(receipt)
            self.assertEqual(cm.exception.code, "E_DIMENSIONS_VALUE")

    def test_reject_bool_masquerading_as_int_in_tick(self):
        for bad_tick in (True, False):
            receipt = _valid_receipt()
            receipt["tick"] = bad_tick
            with self.assertRaises(cr.ReceiptValidationError) as cm:
                cr.validate_receipt(receipt)
            self.assertEqual(cm.exception.code, "E_TICK_VALUE")

    def test_signed_committed_tick_accepted(self):
        for valid_tick in (-100, 0, 100, 999999):
            receipt = _valid_receipt(tick=valid_tick)
            evidence = _valid_evidence(receipt)
            res = cr.validate_receipt(receipt, publication_evidence=evidence)
            self.assertEqual(res.status, "PASS")

    def test_reject_uppercase_and_invalid_digest(self):
        valid = hashlib.sha256(b"x").hexdigest()
        receipt = _valid_receipt()
        # Uppercase
        receipt["frame_sha256"] = valid.upper()
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_DIGEST_FORMAT")

        # Wrong length
        receipt["frame_sha256"] = valid[:-1]
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_DIGEST_FORMAT")

        # Non-hex characters
        receipt["frame_sha256"] = "z" * 64
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_DIGEST_FORMAT")

    def test_reject_absent_and_unbound_telemetry(self):
        receipt = _valid_receipt()
        receipt["telemetry"] = None
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_TELEMETRY_UNBOUND")

        receipt["telemetry"] = "TELEMETRY_UNBOUND"
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_TELEMETRY_UNBOUND")

        receipt["telemetry"] = {"state_q16": "TELEMETRY_UNBOUND"}
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_TELEMETRY_UNBOUND")

    def test_reject_extra_and_missing_telemetry_keys(self):
        receipt = _valid_receipt()
        receipt["telemetry"] = {}
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_TELEMETRY_MISSING_STATE")

        receipt["telemetry"] = {"state_q16": 65536, "extra_metric": 1}
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_TELEMETRY_EXTRA_KEYS")

    def test_reject_bool_masquerading_as_int_in_telemetry(self):
        receipt = _valid_receipt()
        receipt["telemetry"] = {"state_q16": True}
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            cr.validate_receipt(receipt)
        self.assertEqual(cm.exception.code, "E_TELEMETRY_VALUE")


class TestExpectedConstraintsAndMismatches(unittest.TestCase):
    """Test expected keyword constraints."""

    def test_expected_dimensions_mismatch(self):
        receipt = _valid_receipt(w=320, h=200)
        evidence = _valid_evidence(receipt)
        result = cr.validate_receipt(receipt, publication_evidence=evidence, expected_dimensions=(640, 480))
        self.assertEqual(result.status, "MISMATCH")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "E_DIMENSIONS_MISMATCH")
        self.assertIn("dimensions", result.mismatch_fields)

    def test_expected_tick_mismatch(self):
        receipt = _valid_receipt(tick=10)
        evidence = _valid_evidence(receipt)
        result = cr.validate_receipt(receipt, publication_evidence=evidence, expected_tick=20)
        self.assertEqual(result.status, "MISMATCH")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "E_TICK_MISMATCH")
        self.assertIn("tick", result.mismatch_fields)

    def test_expected_sha256_mismatch(self):
        receipt = _valid_receipt()
        evidence = _valid_evidence(receipt)
        other_sha = hashlib.sha256(b"other").hexdigest()
        result = cr.validate_receipt(receipt, publication_evidence=evidence, expected_sha256=other_sha)
        self.assertEqual(result.status, "MISMATCH")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "E_DIGEST_MISMATCH")
        self.assertIn("frame_sha256", result.mismatch_fields)


class TestInvariantsJ2(unittest.TestCase):
    """Test INV-J2-1 through INV-J2-4."""

    def test_inv_j2_1_declaration_before_digest(self):
        """INV-J2-1: Declaration must validate before hashing. raw_sha256 != normalized_sha256."""
        decl = cr.PixelDeclaration(
            channel_order="bgr",
            stride_bytes=320 * 3 + 16,  # 16 bytes stride padding
            orientation="bottom_to_top",
            alpha="none",
            color_space="srgb",
            width=320,
            height=200,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-0",
            binding_id="bind-1",
        )
        decl.validate()
        decl_sha = decl.pixel_decl_sha256()
        self.assertEqual(len(decl_sha), 64)
        self.assertTrue(decl_sha.islower())

        # Construct raw bytes with bottom_to_top and padding
        raw = bytearray((320 * 3 + 16) * 200)
        raw[0:3] = b"\x01\x02\x03"  # BGR
        raw_sha = decl.raw_sha256(raw)
        norm_sha = decl.normalized_sha256(raw)
        self.assertNotEqual(raw_sha, norm_sha)

        # Invalid declaration is rejected before digest
        bad_decl = cr.PixelDeclaration(
            channel_order="bgr",
            stride_bytes=10,  # too small
            orientation="bottom_to_top",
            alpha="none",
            color_space="srgb",
            width=320,
            height=200,
            dpr_ppm=1000000,
            zoom_ppm=1000000,
            environment_id="env-0",
            binding_id="bind-1",
        )
        with self.assertRaises(cr.ReceiptValidationError) as cm:
            bad_decl.pixel_decl_sha256()
        self.assertEqual(cm.exception.code, "E_DECL_STRIDE")

    def test_inv_j2_2_geometry_and_handle_invariants(self):
        """INV-J2-2: Evidence geometry, handle generation and publication identity checked."""
        receipt = _valid_receipt(w=320, h=200)
        # Geometry mismatch
        bad_ev = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=(640, 480),
            completed_publication_identity="pub-1",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=receipt["tick"],
            source_kind="ramws",
        )
        res = cr.validate_receipt(receipt, publication_evidence=bad_ev)
        self.assertEqual(res.status, "MISMATCH")
        self.assertEqual(res.code, "E_INV_J2_2")

    def test_inv_j2_3_crc_and_seqlock_revalidation_after_copying(self):
        """INV-J2-3: Owned bytes sequence and CRC revalidation catches torn read."""
        raw = b"valid_frame_payload"
        ev = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=(320, 200),
            completed_publication_identity="pub-1",
            frame_sha256=hashlib.sha256(b"healthy_frame_pixels_0").hexdigest(),
            committed_tick=42,
            source_kind="ramws",
            sequence=2,
            crc32=zlib.crc32(raw),
            raw_bytes=raw,
        )
        receipt = _valid_receipt()
        # Normal revalidation passes
        self.assertTrue(cr.revalidate_owned_bytes_and_crc(raw, 2, ev.crc32, 2))

        # Corrupted payload (CRC mismatch)
        torn_raw = b"corrupted_payload!!"
        self.assertFalse(cr.revalidate_owned_bytes_and_crc(torn_raw, 2, ev.crc32, 2))

        # Torn read evidence fails validation with BLOCKED
        bad_crc_ev = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=(320, 200),
            completed_publication_identity="pub-1",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=42,
            source_kind="ramws",
            sequence=2,
            crc32=zlib.crc32(raw) ^ 0xFFFFFFFF,  # bad CRC
            raw_bytes=raw,
        )
        res = cr.validate_receipt(receipt, publication_evidence=bad_crc_ev)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_INV_J2_3_CRC_TORN")

    def test_inv_j2_4_unsupported_capture_and_unobserved_residency_blocked(self):
        """INV-J2-4: Unsupported capture and unobserved residency return typed BLOCKED."""
        receipt = _valid_receipt()

        # Unknown capture source
        ev_unknown = _valid_evidence(receipt, source_kind="unknown_driver")
        res = cr.validate_receipt(receipt, publication_evidence=ev_unknown)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_UNSUPPORTED_CAPTURE")

        # Electron brokered publisher is AUSENTE until H2/H3
        ev_electron = _valid_evidence(receipt, source_kind="electron-brokered")
        res = cr.validate_receipt(receipt, publication_evidence=ev_electron)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_ELECTRON_AUSENTE")

        # CDP not derived from DOM/AX
        ev_cdp_bad = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=receipt["dimensions"],
            completed_publication_identity="pub-cdp",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=receipt["tick"],
            source_kind="cdp",
            is_dom_ax_derived=False,
        )
        res = cr.validate_receipt(receipt, publication_evidence=ev_cdp_bad)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_CDP_NOT_DOM_AX")

        # Chromium screen without trusted crop association
        ev_chrom = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=receipt["dimensions"],
            completed_publication_identity="pub-chrom",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=receipt["tick"],
            source_kind="chromium",
            trusted_association=False,
        )
        res = cr.validate_receipt(receipt, publication_evidence=ev_chrom)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_UNTRUSTED_CROP_ASSOCIATION")

        # capturePage alone supplies no coherence or residency proof
        ev_cpage = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=receipt["dimensions"],
            completed_publication_identity="pub-cpage",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=receipt["tick"],
            source_kind="ramws",
            method="capturePage",
            coherence_proof=False,
        )
        res = cr.validate_receipt(receipt, publication_evidence=ev_cpage)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_CAPTURE_PAGE_NO_PROOF")

        # Unobserved residency
        ev_unobs = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=receipt["dimensions"],
            completed_publication_identity="pub-unobs",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=receipt["tick"],
            source_kind="ramws",
            residency_observed=False,
        )
        res = cr.validate_receipt(receipt, publication_evidence=ev_unobs)
        self.assertEqual(res.status, "BLOCKED")
        self.assertEqual(res.code, "E_UNOBSERVED_RESIDENCY")


class TestRenderIndexAndSimulationTick(unittest.TestCase):
    """A render_index is never a simulation tick. Same-tick commits require binding."""

    def test_render_index_not_simulation_tick(self):
        receipt = _valid_receipt(tick=100)
        # Evidence has committed_tick 101 (e.g. caller passed render_index)
        ev = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=receipt["dimensions"],
            completed_publication_identity="pub-1",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=101,
            source_kind="ramws",
        )
        res = cr.validate_receipt(receipt, publication_evidence=ev)
        self.assertEqual(res.status, "MISMATCH")
        self.assertEqual(res.code, "E_TICK_MISMATCH")

    def test_same_tick_distinct_commits_require_independent_binding(self):
        receipt = _valid_receipt(tick=50)
        ev = _valid_evidence(receipt)
        prior_commits = {50: hashlib.sha256(b"prior_different_commit").hexdigest()}
        res = cr.validate_receipt(receipt, publication_evidence=ev, previous_commits=prior_commits)
        self.assertEqual(res.status, "MISMATCH")
        self.assertEqual(res.code, "E_SAME_TICK_CONFLICT")

        # With explicit independent publication binding, it passes
        ev_bound = cr.PublicationEvidence(
            handle_generation=1,
            epoch=0,
            geometry=receipt["dimensions"],
            completed_publication_identity="pub-1",
            frame_sha256=receipt["frame_sha256"],
            committed_tick=50,
            source_kind="ramws",
            independent_publication_binding="binding-branch-secondary",
        )
        res_bound = cr.validate_receipt(receipt, publication_evidence=ev_bound, previous_commits=prior_commits)
        self.assertEqual(res_bound.status, "PASS")


class TestHoldingsCommandsAndAdmission(unittest.TestCase):
    """External T2 fields remain exactly tick, seq, cmd, args. Admission vs economics."""

    def test_holdings_pause_command(self):
        cmd = cr.make_holdings_command("pause", tick=10, seq=1)
        self.assertEqual(cmd, {"tick": 10, "seq": 1, "cmd": "pause", "args": {}})
        # Pause cannot take arguments
        with self.assertRaises(cr.ReceiptValidationError):
            cr.make_holdings_command("pause", tick=10, seq=1, args={"foo": "bar"})

    def test_holdings_new_game_command(self):
        valid_args = {"country_id": "us", "difficulty": 2, "opening_mode": "standard", "seed": 12345}
        cmd = cr.make_holdings_command("new_game", tick=0, seq=0, args=valid_args)
        self.assertEqual(cmd["cmd"], "new_game")
        self.assertEqual(cmd["args"], valid_args)

        # Missing required args
        with self.assertRaises(cr.ReceiptValidationError):
            cr.make_holdings_command("new_game", tick=0, seq=0, args={"country_id": "us"})

    def test_admission_tracker_advances_bookkeeping_without_simulation_mutation(self):
        tracker = cr.AdmissionTracker(initial_seq=10)
        pause_cmd = cr.make_holdings_command("pause", tick=1, seq=11)

        # Rejected attempt
        rec1 = tracker.submit_attempt(pause_cmd, admitted=False)
        self.assertEqual(tracker.bookkeeping_seq, 11)
        self.assertEqual(tracker.simulation_tick, 0)
        self.assertFalse(rec1["admitted"])

        # Another rejected attempt
        rec2 = tracker.submit_attempt(pause_cmd, admitted=False)
        self.assertEqual(tracker.bookkeeping_seq, 12)
        self.assertEqual(tracker.simulation_tick, 0)

        # Admitted attempt
        rec3 = tracker.submit_attempt(pause_cmd, admitted=True)
        self.assertEqual(tracker.bookkeeping_seq, 13)
        self.assertEqual(tracker.simulation_tick, 1)
        self.assertTrue(rec3["admitted"])


class TestNoPixelPersistenceSpy(unittest.TestCase):
    """Write spies verify that frames are never persisted to disk, swap, logs, or receipts."""

    def test_write_spy_rejects_raw_pixels_in_receipt(self):
        spy = cr.NoPixelPersistenceSpy()
        receipt = _valid_receipt()
        # Clean receipt passes inspection
        spy.inspect_receipt(receipt)
        self.assertFalse(spy.persistence_attempted)

        # Receipt containing raw numpy array or bytebuffer fails spy
        bad_receipt = dict(receipt)
        bad_receipt["raw_pixels"] = np.zeros((10, 10), dtype=np.uint8)
        with self.assertRaises(cr.PixelPersistenceForbiddenError):
            spy.inspect_receipt(bad_receipt)
        self.assertTrue(spy.persistence_attempted)


class TestVersionedMetadata(unittest.TestCase):
    """Versioned metadata binds frame_seq, committed_tick, state_sha256 and observer_ref."""

    def test_versioned_metadata_validation(self):
        valid_meta = cr.VersionedMetadata(
            frame_seq=5,
            committed_tick=12,
            state_sha256=hashlib.sha256(b"state").hexdigest(),
            observer_ref="obs-ref-primary",
        )
        valid_meta.validate()

        bad_meta = cr.VersionedMetadata(
            frame_seq=-1,
            committed_tick=12,
            state_sha256=hashlib.sha256(b"state").hexdigest(),
            observer_ref="obs",
        )
        with self.assertRaises(cr.ReceiptValidationError):
            bad_meta.validate()


if __name__ == "__main__":
    unittest.main()
