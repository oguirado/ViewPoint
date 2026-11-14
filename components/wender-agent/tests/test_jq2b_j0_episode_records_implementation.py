# -*- coding: utf-8 -*-
"""Public-path test suite for jq2b-j0-episode-records implementation.

Exercises wender.learn.episode_records public APIs, encoding/decoding,
closed metadata allowlists, canonical UTF-8 JSON-L formatting, INV J0 1-4 invariants,
and EpisodeAdapter lifecycle against canonical wender.perceive.session_registry contracts.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import unittest
from typing import Any

import numpy as np

from wender.learn.episode_records import (
    BASE64_DATA_URI_PATTERN,
    BASE64_PAYLOAD_PATTERN,
    CLOSED_RECORD_ALLOWLIST,
    COMMON_RECORD_KEYS,
    FrameRef,
    Handle,
    Observation,
    Receipt,
    SCHEMA_ID,
    SUPPORTED_SOURCE_KINDS,
    SUPPORTED_TARGET_KINDS,
    StreamSpec,
    StreamToken,
    BackendImportForbiddenError,
    Capabilities,
    ClosedAllowlistError,
    ConfigRequiredError,
    DuplicateKeyError,
    EncodingValidationError,
    EpisodeAdapter,
    EpisodeRecord,
    EpisodeRecordError,
    LauncherEvidence,
    Outcome,
    PauseCommand,
    RecordLimitError,
    ResetResult,
    StaleGenerationError,
    UnsupportedCapabilityError,
    compute_diagnostic_seal,
    compute_record_digest,
    decode_record,
    encode_record,
    project_handle,
    validate_closed_record,
)
from wender.state import schema as state_schema


class WriteSpy:
    """Spy fixture to monitor and ensure no pixel/capture-receipt leakage occurs."""

    def __init__(self) -> None:
        self.recorded_writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        # Reject pixel / receipt / authority leakage (INV J0 2, Gate G J0 3)
        if b"data:image" in data or b"pixels" in data or b"frame_data" in data or b"auth_token" in data:
            raise EncodingValidationError("WriteSpy detected illegal pixel or authority leakage!")
        self.recorded_writes.append(data)


class TestEpisodeRecordsEncodingAndValidation(unittest.TestCase):
    """Tests for record validation, metadata allowlist, and canonical encoding."""

    def setUp(self) -> None:
        self.valid_handle = {
            "kind": "cdp_tab",
            "canonical_id": "tab-123",
            "generation": 1,
        }
        self.valid_payload = {
            "action_count": 2,
            "metric_name": "step_latency_ms",
            "step_id": "step-0",
        }
        self.healthy_record = {
            "schema": SCHEMA_ID,
            "episode_id": "ep-9999",
            "handle": self.valid_handle,
            "epoch": 0,
            "row_seq": 1,
            "logical_tick": 10,
            "evidence_status": "verified",
            "payload": self.valid_payload,
        }

    def test_positive_control_closed_digest_row_round_trip(self) -> None:
        """Gate G J0 3 Positive control: healthy record round-trips with canonical bytes and digest."""
        encoded = encode_record(self.healthy_record, max_bytes=2048)
        self.assertTrue(encoded.endswith(b"\n"))

        text = encoded.decode("utf-8")
        parsed = json.loads(text)
        self.assertIn("digest", parsed)
        self.assertEqual(len(parsed["digest"]), 64)

        computed_digest = compute_record_digest(parsed)
        self.assertEqual(parsed["digest"], computed_digest)

        decoded = decode_record(encoded, max_bytes=2048)
        self.assertEqual(decoded["schema"], SCHEMA_ID)
        self.assertEqual(decoded["episode_id"], self.healthy_record["episode_id"])
        self.assertEqual(decoded["handle"], self.valid_handle)
        self.assertEqual(decoded["payload"], self.valid_payload)
        self.assertEqual(decoded["row_seq"], 1)
        self.assertEqual(decoded["logical_tick"], 10)
        self.assertNotIn("seal", decoded)
        self.assertEqual(decoded["digest"], parsed["digest"])

    def test_canonical_utf8_sorted_keys_compact_separators_unescaped_unicode(self) -> None:
        """Canonical encoding uses sorted keys, compact separators, unescaped Unicode, and trailing LF."""
        rec = dict(self.healthy_record)
        rec["payload"] = {"z_key": "café_Δ", "a_key": 7}
        encoded = encode_record(rec, max_bytes=2048)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded.count(b"\n"), 1)
        raw_str = encoded[:-1].decode("utf-8")
        self.assertIn("café_Δ", raw_str)
        self.assertNotIn("\\u00e9", raw_str)
        self.assertNotIn(": ", raw_str)
        self.assertNotIn(", ", raw_str)
        self.assertLess(raw_str.index('"a_key"'), raw_str.index('"z_key"'))

    def test_config_required_for_absent_or_nonpositive_ceilings(self) -> None:
        """Absent or non-positive configured ceilings raise ConfigRequiredError."""
        with self.assertRaises(ConfigRequiredError):
            encode_record(self.healthy_record, max_bytes=None)

        with self.assertRaises(ConfigRequiredError):
            encode_record(self.healthy_record, max_bytes=0)

        with self.assertRaises(ConfigRequiredError):
            encode_record(self.healthy_record, max_bytes=-50)

        with self.assertRaises(ConfigRequiredError):
            encode_record(self.healthy_record, max_bytes=True)  # type: ignore[arg-type]

        with self.assertRaises(ConfigRequiredError):
            decode_record(b'{"schema":"test"}\n', max_bytes=None)

        with self.assertRaises(ConfigRequiredError):
            decode_record(b'{"schema":"test"}\n', max_bytes=0)

    def test_rejection_of_floats_and_nonfinite_values(self) -> None:
        """Encoding rejects float values and non-finite numbers at any depth."""
        bad_record = dict(self.healthy_record)
        bad_record["payload"] = {"score": 3.1415}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

        bad_record["payload"] = {"metric": float("inf")}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

        bad_record["payload"] = {"metric": float("nan")}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

    def test_rejection_of_bool_integers_in_numeric_fields(self) -> None:
        """Booleans masquerading as integers are strictly rejected in numeric fields."""
        bad_record = dict(self.healthy_record)
        bad_record["epoch"] = True
        with self.assertRaises(ClosedAllowlistError):
            encode_record(bad_record, max_bytes=2048)

        bad_record = dict(self.healthy_record)
        bad_record["row_seq"] = False
        with self.assertRaises(ClosedAllowlistError):
            encode_record(bad_record, max_bytes=2048)

        bad_record = dict(self.healthy_record)
        bad_record["logical_tick"] = True
        with self.assertRaises(ClosedAllowlistError):
            encode_record(bad_record, max_bytes=2048)

        bad_handle_gen0 = dict(self.healthy_record)
        bad_handle_gen0["handle"] = {"kind": "cdp_tab", "canonical_id": "tab-123", "generation": 0}
        with self.assertRaises(ClosedAllowlistError):
            encode_record(bad_handle_gen0, max_bytes=2048)

    def test_negative_contrast_hidden_base64_and_pixel_payload(self) -> None:
        """Negative contrast: hidden base64, data URIs, and raw pixel payloads are rejected."""
        bad_record = dict(self.healthy_record)
        bad_record["payload"] = {"screenshot": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

        bad_record = dict(self.healthy_record)
        bad_record["payload"] = {"dump": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

        bad_record = dict(self.healthy_record)
        bad_record["payload"] = {"raw_bytes": bytes([0, 1, 2, 3])}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

        bad_record = dict(self.healthy_record)
        bad_record["payload"] = {"frame_arr": np.zeros((4, 4, 3), dtype=np.uint8)}
        with self.assertRaises(EncodingValidationError):
            encode_record(bad_record, max_bytes=2048)

    def test_inv_j0_2_authority_and_capture_leakage_rejection(self) -> None:
        """INV J0 2: Authority tokens, passwords, cookies, and capture keys are rejected."""
        forbidden_keys = [
            "auth_token",
            "access_token",
            "session_token",
            "password",
            "secret_key",
            "authority",
            "cookie_jar",
            "raw_hook_receipts",
            "tree_dump",
            "checkpoint_bytes",
            "pixels",
            "frame_data",
        ]
        for key in forbidden_keys:
            bad_record = dict(self.healthy_record)
            bad_record["payload"] = {key: "leaked_value"}
            with self.assertRaises(EncodingValidationError, msg=f"Key {key} should be rejected"):
                encode_record(bad_record, max_bytes=2048)

    def test_closed_allowlist_rejects_unapproved_top_level_keys(self) -> None:
        """Closed allowlist rejects unknown top-level keys."""
        bad_record = dict(self.healthy_record)
        bad_record["unapproved_extra_field"] = "unexpected"
        with self.assertRaises(ClosedAllowlistError):
            encode_record(bad_record, max_bytes=2048)

    def test_diagnostic_row_seal(self) -> None:
        """Diagnostic rows carry and validate their own seal."""
        diag_record = dict(self.healthy_record)
        diag_record["evidence_status"] = "diagnostic"
        diag_record["payload"] = {"diagnostic_metric": "heartbeat_ok", "code": 100}

        normalized = validate_closed_record(diag_record)
        seal = compute_diagnostic_seal(normalized)
        diag_record["seal"] = seal

        encoded = encode_record(diag_record, max_bytes=2048)
        decoded = decode_record(encoded, max_bytes=2048)
        self.assertEqual(decoded["seal"], seal)

        diag_record["seal"] = "bad" + seal[3:]
        with self.assertRaises(EncodingValidationError):
            encode_record(diag_record, max_bytes=2048)

    def test_max_bytes_ceiling_enforcement_on_output(self) -> None:
        """Record exceeding configured max_bytes is rejected with RecordLimitError."""
        with self.assertRaises(RecordLimitError):
            encode_record(self.healthy_record, max_bytes=50)


class TestEpisodeRecordsDecodingAndLiteralJson(unittest.TestCase):
    """Tests for raw JSON validation, duplicate keys at all levels, and input bounding."""

    def test_oversize_raw_input_bounded_before_parsing(self) -> None:
        """Raw input exceeding max_bytes ceiling is rejected before UTF-8 or JSON parsing."""
        raw = b'{"schema":"wender.episode/v1"}\n'
        with self.assertRaises(RecordLimitError):
            decode_record(raw, max_bytes=10)

    def test_invalid_utf8_rejected(self) -> None:
        """Invalid UTF-8 bytes are rejected loudly."""
        raw = bytes([123, 34, 120, 34, 58, 34, 255, 254, 34, 125])
        with self.assertRaises(EpisodeRecordError):
            decode_record(raw, max_bytes=1024)

    def test_truncated_json_rejected(self) -> None:
        """Truncated JSON is rejected loudly."""
        raw = b'{"schema":"wender.episode/v1", "episode_id":'
        with self.assertRaises(EpisodeRecordError):
            decode_record(raw, max_bytes=1024)

    def test_reject_literal_raw_json_with_repeated_top_level_keys(self) -> None:
        """Duplicate keys in raw JSON at top level are rejected before mapping construction."""
        raw = (
            b'{"schema":"wender.episode/v1","schema":"duplicate.schema",'
            b'"episode_id":"ep-1","handle":{"kind":"cdp_tab","canonical_id":"tab-1","generation":1},'
            b'"epoch":0,"row_seq":0,"logical_tick":0,"evidence_status":"test","payload":{}}\n'
        )
        with self.assertRaises(DuplicateKeyError):
            decode_record(raw, max_bytes=2048)

    def test_reject_literal_raw_json_with_nested_repeated_keys(self) -> None:
        """Duplicate keys in raw JSON at nested level are rejected."""
        raw = (
            b'{"schema":"wender.episode/v1","episode_id":"ep-1",'
            b'"handle":{"kind":"cdp_tab","kind":"godot_window","canonical_id":"tab-1","generation":1},'
            b'"epoch":0,"row_seq":0,"logical_tick":0,"evidence_status":"test","payload":{}}\n'
        )
        with self.assertRaises(DuplicateKeyError):
            decode_record(raw, max_bytes=2048)

        raw_payload_repeat = (
            b'{"schema":"wender.episode/v1","episode_id":"ep-1",'
            b'"handle":{"kind":"cdp_tab","canonical_id":"tab-1","generation":1},'
            b'"epoch":0,"row_seq":0,"logical_tick":0,"evidence_status":"test",'
            b'"payload":{"nested":{"k":1,"k":2}}}\n'
        )
        with self.assertRaises(DuplicateKeyError):
            decode_record(raw_payload_repeat, max_bytes=2048)

    def test_decoded_float_in_raw_json_rejected(self) -> None:
        """Floats in literal raw JSON input are rejected during decoding."""
        raw = (
            b'{"schema":"wender.episode/v1","episode_id":"ep-1",'
            b'"handle":{"kind":"cdp_tab","canonical_id":"tab-1","generation":1},'
            b'"epoch":0,"row_seq":0,"logical_tick":0,"evidence_status":"test",'
            b'"payload":{"latency":12.34}}\n'
        )
        with self.assertRaises(EncodingValidationError):
            decode_record(raw, max_bytes=2048)


class TestEpisodeAdapterAndInvariants(unittest.TestCase):
    """Tests for EpisodeAdapter lifecycle, INV J0 1-4 invariants, and source policies."""

    def setUp(self) -> None:
        self.cdp_launcher_evidence = LauncherEvidence(
            source_kind="cdp_tab",
            tree_supported=True,
            frame_supported=True,
            t2_supported=True,
            reset_supported=True,
            observer_supported=True,
            binding_id="bind-cdp-01",
        )
        self.godot_launcher_evidence = LauncherEvidence(
            source_kind="godot_window",
            tree_supported=True,
            frame_supported=True,
            t2_supported=True,
            reset_supported=True,
            observer_supported=True,
            binding_id="bind-godot-01",
        )
        self.cdp_adapter = EpisodeAdapter(self.cdp_launcher_evidence, max_batch_size=50)
        self.godot_adapter = EpisodeAdapter(self.godot_launcher_evidence, max_batch_size=50)

    def test_prerequisite_session_registry_canonical_types_lazy_import(self) -> None:
        """Verify canonical runtime types from wender.perceive.session_registry match re-exported types."""
        sr = importlib.import_module("wender.perceive.session_registry")
        self.assertIs(Handle, sr.Handle)
        self.assertIs(FrameRef, sr.FrameRef)
        self.assertIs(Observation, sr.Observation)
        self.assertIs(Receipt, sr.Receipt)
        self.assertIs(StreamSpec, sr.StreamSpec)
        self.assertIs(StreamToken, sr.StreamToken)
        self.assertEqual(SUPPORTED_TARGET_KINDS, sr.SUPPORTED_KINDS)

    def test_inv_j0_1_no_backend_imports_in_core(self) -> None:
        """INV J0 1: Core episode_records module does not import CDP, Godot, or backend drivers."""
        core_mod = sys.modules["wender.learn.episode_records"]
        forbidden_modules = [
            "wender.perceive.cdp_pipe",
            "wender.perceive.cdp_port",
            "wender.perceive.godot",
            "playwright",
        ]
        for mod in forbidden_modules:
            self.assertNotIn(mod, dir(core_mod))

    def test_capabilities_established_by_launcher_evidence_not_self_assertion(self) -> None:
        """Capabilities are established by launcher evidence; adapter self-assertion fails."""
        with self.assertRaises(UnsupportedCapabilityError):
            EpisodeAdapter("self_asserted_capabilities")  # type: ignore[arg-type]

        self.assertTrue(self.cdp_adapter.capabilities.tree)
        self.assertTrue(self.cdp_adapter.capabilities.frame)
        self.assertTrue(self.cdp_adapter.capabilities.t2)
        self.assertEqual(self.cdp_adapter.capabilities.source_kind, "cdp_tab")
        self.assertEqual(self.cdp_adapter.capabilities.binding_id, "bind-cdp-01")

        self.assertEqual(self.godot_adapter.capabilities.source_kind, "godot_window")
        self.assertEqual(self.godot_adapter.capabilities.binding_id, "bind-godot-01")

    def test_inv_j0_3_generation_fencing_after_reset(self) -> None:
        """INV J0 3: Reset advances generation and fences older handle generations."""
        handle_gen1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        obs0 = self.cdp_adapter.observe_tree(handle_gen1, epoch=0)
        self.assertEqual(obs0.epoch, 0)
        self.assertEqual(state_schema.validate_state(obs0.state), [])

        descriptor = {"target": "http://127.0.0.1:8080/test", "width": 800, "height": 600}
        reset_res = self.cdp_adapter.reset(seed=42, descriptor=descriptor, checkpoint=bytes([1, 2, 3, 4]))
        self.assertIsInstance(reset_res, ResetResult)
        self.assertEqual(self.cdp_adapter.generation, 2)

        with self.assertRaises(StaleGenerationError):
            self.cdp_adapter.observe_tree(handle_gen1, epoch=1)

        handle_gen2 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=2)
        obs1 = self.cdp_adapter.observe_tree(handle_gen2, epoch=1)
        self.assertEqual(obs1.epoch, 1)

    def test_inv_j0_4_preserve_unavailable_capability_as_typed_evidence(self) -> None:
        """INV J0 4: Operations on unavailable capabilities raise typed UnsupportedCapabilityError."""
        unsupported_evidence = LauncherEvidence(
            source_kind="cdp_tab",
            tree_supported=False,
            frame_supported=False,
            t2_supported=False,
            reset_supported=False,
            observer_supported=False,
            binding_id="bind-disabled",
        )
        disabled_adapter = EpisodeAdapter(unsupported_evidence)

        handle = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        frame = FrameRef.from_rgb(handle, 0, 0, rgb)
        obs = Observation(handle=handle, epoch=0, frame_seq=0, dpr_ppm=1000000, state={}, frame=frame)

        with self.assertRaises(UnsupportedCapabilityError):
            disabled_adapter.observe_tree(handle, epoch=0)

        with self.assertRaises(UnsupportedCapabilityError):
            disabled_adapter.observe_frame_digest(obs)

        with self.assertRaises(UnsupportedCapabilityError):
            disabled_adapter.submit_t2_batch([PauseCommand(tick=1, seq=0)], trusted_context={})

        with self.assertRaises(UnsupportedCapabilityError):
            disabled_adapter.reset(seed=1, descriptor={})

    def test_observe_frame_digest_returns_approved_metadata_only(self) -> None:
        """observe_frame_digest strictly returns metadata, never pixels or raw hook receipts."""
        handle = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        rgb = np.zeros((12, 16, 3), dtype=np.uint8)
        rgb[0, 0] = [10, 20, 30]
        frame = FrameRef.from_rgb(handle, 0, 5, rgb)
        obs = Observation(
            handle=handle,
            epoch=0,
            frame_seq=5,
            dpr_ppm=1000000,
            state={},
            frame=frame,
        )

        metadata = self.cdp_adapter.observe_frame_digest(obs)
        self.assertEqual(metadata["digest"], frame.digest)
        self.assertEqual(metadata["width"], 16)
        self.assertEqual(metadata["height"], 12)
        self.assertEqual(metadata["dpr_ppm"], 1000000)
        self.assertEqual(metadata["epoch"], 0)
        self.assertEqual(metadata["frame_seq"], 5)
        self.assertNotIn("rgb", metadata)
        self.assertNotIn("pixels", metadata)

    def test_t2_batch_submission_and_read_outcomes(self) -> None:
        """Submit T2 batch with pause command and verify Receipts and Outcomes."""
        cmd = PauseCommand(tick=1, seq=0)
        receipts = self.cdp_adapter.submit_t2_batch([cmd], trusted_context={"ctx": "test"})
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, "accepted")
        self.assertEqual(receipts[0].handle.generation, 1)
        self.assertEqual(receipts[0].handle.kind, "cdp_tab")

        outcomes = self.cdp_adapter.read_outcomes()
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(outcome.seq, 0)
        self.assertEqual(outcome.requested_tick, 1)
        self.assertEqual(outcome.applied_tick, 1)
        self.assertEqual(outcome.order_seq_id, 0)
        self.assertEqual(outcome.status, "applied")
        self.assertEqual(len(outcome.state_sha256), 64)

        self.assertEqual(len(self.cdp_adapter.read_outcomes()), 0)

    def test_write_spy_asserts_no_pixel_receipt_leakage(self) -> None:
        """Write spy ensures encoded records contain no pixel or authority leakage."""
        spy = WriteSpy()
        record = {
            "schema": SCHEMA_ID,
            "episode_id": "ep-spy-01",
            "handle": {"kind": "cdp_tab", "canonical_id": "tab-1", "generation": 1},
            "epoch": 0,
            "row_seq": 1,
            "logical_tick": 1,
            "evidence_status": "verified",
            "payload": {"step": "init"},
        }
        encoded = encode_record(record, max_bytes=1024)
        spy.write(encoded)
        self.assertEqual(len(spy.recorded_writes), 1)

        with self.assertRaises(EncodingValidationError):
            spy.write(b'{"payload":{"pixels":"raw"}}')

    def test_source_neutral_both_source_kinds_exercised(self) -> None:
        """Exercise both CDP and Godot source policies through adapter fixtures."""
        cdp_h = Handle(kind="cdp_tab", canonical_id="tab-cdp", generation=1)
        godot_h = Handle(kind="godot_window", canonical_id="win-godot", generation=1)

        obs_cdp = self.cdp_adapter.observe_tree(cdp_h, epoch=0)
        obs_godot = self.godot_adapter.observe_tree(godot_h, epoch=0)

        self.assertEqual(obs_cdp.handle.kind, "cdp_tab")
        self.assertEqual(obs_godot.handle.kind, "godot_window")
        self.assertEqual(obs_cdp.state["source"]["kind"], "cdp")
        self.assertEqual(obs_godot.state["source"]["kind"], "godot")


if __name__ == "__main__":
    unittest.main()
