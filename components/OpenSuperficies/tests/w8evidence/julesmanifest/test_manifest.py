"""
Pytest test suite for local Jules massive manifest validator and proposed carriers.

Tests positive controls and all must-FAIL scenarios:
- Must-FAIL 1: unmerged dependency
- Must-FAIL 2: changed suite digest
- Must-FAIL 3: missing negative control
- Must-FAIL 4: stale host evidence
- Must-FAIL 5: unqualified width
- Must-FAIL 6: copied receipt
- Must-FAIL 7: hidden failed attempt
- Must-FAIL 8: missing STOP envelope
- Must-FAIL 9: inferred live authorization
"""

import hashlib
import json
import pytest
import tempfile
from pathlib import Path

from tests.w8evidence.julesmanifest.validator import (
    EXIT_INPUT,
    EXIT_PREREQ_UNAVAILABLE,
    EXIT_GATE_CONTAINMENT,
    CaptureCarrierValidator,
    IdentityBindingValidator,
    LoopCarrierValidator,
    ManifestValidator,
    SidecarValidator,
    SnapshotValidator,
    SpeechPayloadValidator,
    TrainingSignalValidator,
    TranscriptValidator,
    ValidationError,
)


def make_valid_manifest() -> dict:
    return {
        "repo": "oguirado/OpenSuperficies",
        "source_commit": "9188769e3b202352153981d0ddfd9204c4b9abd4",
        "suite_sha256": "a" * 64,
        "fixture_sha256": "b" * 64,
        "seed": 1337,
        "ordered_gate_argv": ["python3", "-m", "pytest", "-q", "tests/w8evidence/julesmanifest"],
        "obligations": [
            {"id": "ob_1", "weight": 10, "state": "accepted"},
            {"id": "ob_2", "weight": 5, "state": "accepted"},
        ],
        "resource_vector": {
            "cpu_numerator": 2,
            "cpu_denominator": 1,
            "ram_bytes": 4294967296,
        },
        "capture_scope": {"scope_id": "scope_offline"},
        "stop_envelope": {"capacity": 100},
        "task_id": "os-f-w8-3-19-jules-massive-manifest",
        "attempt_id": "a1",
        "lease_id": "lease-bea606fa27982db3",
        "epoch": 1,
        "expected_negative_verdicts": ["unmerged_dep", "changed_suite", "missing_stop"],
        "evidence_refs": {"host_ref": "ref_host_01"},
        "live_authorized": False,
    }


def test_positive_control_manifest():
    manifest = make_valid_manifest()
    res = ManifestValidator.validate(manifest, computed_suite_sha256="a" * 64)
    assert res["live_authorized"] is False


def test_must_fail_unmerged_dependency():
    manifest = make_valid_manifest()
    manifest["has_unmerged_dependency"] = True
    with pytest.raises(ValidationError) as exc_info:
        ManifestValidator.validate(manifest)
    assert exc_info.value.exit_code == EXIT_PREREQ_UNAVAILABLE


def test_must_fail_changed_suite_digest():
    manifest = make_valid_manifest()
    with pytest.raises(ValidationError) as exc_info:
        ManifestValidator.validate(manifest, computed_suite_sha256="c" * 64)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_must_fail_missing_negative_control():
    manifest = make_valid_manifest()
    manifest["expected_negative_verdicts"] = []
    with pytest.raises(ValidationError) as exc_info:
        ManifestValidator.validate(manifest)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_must_fail_stale_host_evidence():
    manifest = make_valid_manifest()
    manifest["evidence_refs"] = {"stale": True}
    with pytest.raises(ValidationError) as exc_info:
        ManifestValidator.validate(manifest)
    assert exc_info.value.exit_code == EXIT_PREREQ_UNAVAILABLE


def test_must_fail_unqualified_width():
    snapshot = {
        "sample_id": "s1",
        "acquired_monotonic_ns": 1000,
        "workload_commit": "9188769e3b202352153981d0ddfd9204c4b9abd4",
        "cpu_numerator": 1,
        "cpu_denominator": 1,
        "ram_bytes": 1024,
        "pids": 10,
        "fds": 5,
        "ancestor_limits": {},
        "physical_headroom_bytes": 1024,
        "startup_bytes": 512,
        "collector_bytes": 128,
        "reaction_reserve_bytes": 64,
        "pressure": "low",
        "tail_latency_ns": 5,
        "dwell_ns": 10,
        "last_qualified_width": {"unqualified": True},
        "entries": [],
    }
    with pytest.raises(ValidationError) as exc_info:
        SnapshotValidator.validate(snapshot)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_must_fail_copied_receipt():
    transcript = {
        "task_id": "t1",
        "attempt": "a1",
        "lease_id": "l1",
        "epoch": 1,
        "repo_commit": "9188769e3b202352153981d0ddfd9204c4b9abd4",
        "gate_argv": ["python3"],
        "image_sha256": "a" * 64,
        "runner_sha256": "b" * 64,
        "cache_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "handle": "h1",
        "events": [],
        "receipt": {"copied_from_attempt": "a0"},
        "extract_sha256": "e" * 64,
        "durable": True,
        "acknowledged": True,
        "purge_verified": True,
        "bridge_result": None,
        "reason": None,
    }
    with pytest.raises(ValidationError) as exc_info:
        TranscriptValidator.validate(transcript)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_must_fail_hidden_failed_attempt():
    loop = {
        "wave_id": "w1",
        "obligations": [{"id": "ob1", "state": "failed"}],
        "attempts": [{"task_id": "t1", "attempt_id": "a1", "receipt_digest": "r1"}],
        "resource_vector": {},
        "escrow": "esc1",
        "lease_id": "l1",
        "epoch": 1,
        "origin_verified": True,
        "gates_verified": True,
        "cleanup_verified": True,
        "settlement_id": "s1",
        "body_sha256": "b" * 64,
        "disposition": "SUCCESS",
    }
    with pytest.raises(ValidationError) as exc_info:
        LoopCarrierValidator.validate(loop)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_must_fail_missing_stop_envelope():
    manifest = make_valid_manifest()
    manifest["stop_envelope"] = None
    with pytest.raises(ValidationError) as exc_info:
        ManifestValidator.validate(manifest)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_must_fail_inferred_live_authorization():
    manifest = make_valid_manifest()
    manifest["live_authorized"] = True
    with pytest.raises(ValidationError) as exc_info:
        ManifestValidator.validate(manifest)
    assert exc_info.value.exit_code == EXIT_GATE_CONTAINMENT


def test_capture_carrier_validation():
    capture = {
        "sample_id": "s1",
        "workload_repo": "oguirado/OpenSuperficies",
        "workload_commit": "9188769e3b202352153981d0ddfd9204c4b9abd4",
        "engine_sha256": "e" * 64,
        "seed": 42,
        "requested_ticks": 100,
        "actual_ticks": 100,
        "account_uid": "u1",
        "pid": 1234,
        "process_start": 1000,
        "namespace_id": "ns1",
        "acquired_monotonic_ns": 50000,
        "clock_unit": "ns",
        "raw_capture_sha256": hashlib.sha256(b"raw text").hexdigest(),
        "inventory": ["item1"],
        "behavior": "PASS",
        "cpu_numerator": 1,
        "cpu_denominator": 1,
        "ram_bytes": 1024,
        "censored": False,
        "status": "OK",
        "reason": "PASS",
        "raw_capture_text": "raw text",
    }
    res = CaptureCarrierValidator.validate(capture)
    assert res["status"] == "OK"


def test_speech_payload_validation():
    speech = {
        "schema": "os.cu.speech-act/v1",
        "act": "finding",
        "wave_id": "w1",
        "task_id": "t1",
        "attempt_id": "a1",
        "lease_id": "l1",
        "seq": 1,
        "repro_sha256": "r" * 64,
        "affected": ["file.py"],
        "status": "PASS",
        "body": "finding details",
    }
    res = SpeechPayloadValidator.validate(speech)
    assert res["act"] == "finding"


def test_sidecar_validation():
    sidecar = SidecarValidator()
    raw = b"sidecar body bytes"
    msg = {
        "sender": "jules",
        "task_id": "t1",
        "attempt_id": "a1",
        "lease_id": "l1",
        "epoch": 1,
        "seq": 1,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_length": len(raw),
    }
    ans = sidecar.process(msg, raw, verified_sig=True)
    assert ans == "APPLIED_ACK"

    # Replay same
    ans2 = sidecar.process(msg, raw, verified_sig=True)
    assert ans2 == "IDEMPOTENT_REPLAY"

    # Changed bytes at same seq
    msg_bad = dict(msg)
    raw_bad = b"different body bytes"
    msg_bad["body_sha256"] = hashlib.sha256(raw_bad).hexdigest()
    msg_bad["body_length"] = len(raw_bad)
    ans3 = sidecar.process(msg_bad, raw_bad, verified_sig=True)
    assert ans3 == "QUARANTINE_CHANGED_BYTES"


def test_identity_binding():
    data = {
        "normalization_version": "v1",
        "repo": "oguirado/OpenSuperficies",
        "base_commit": "9188769e3b202352153981d0ddfd9204c4b9abd4",
        "image": "img1",
        "toolchain": "tc1",
        "cache_manifest": "c1",
        "ordered_gate_argv": ["pytest"],
        "relative_cwd": ".",
        "outcome": "PASS",
        "assertion_or_crash_signature": "none",
        "ordered_selected_frames": [],
        "affected_path_and_symbol": [],
        "input_identity": "in1",
    }
    encoded = IdentityBindingValidator.encode(data)
    decoded = IdentityBindingValidator.decode_and_validate(encoded)
    assert decoded["repo"] == "oguirado/OpenSuperficies"


def test_training_signal_carrier():
    ts = {
        "repository": "oguirado/OpenSuperficies",
        "engine_version": "1.0",
        "source_version": "1.0",
        "tool_version": "1.0",
        "seed": 123,
        "requested_ticks": 10,
        "actual_ticks": 10,
        "target": "tgt",
        "tab_id": "tab1",
        "session_id": "sess1",
        "segment_id": "seg1",
        "acquisition_provenance": "prov1",
        "state": {"counter": 1},
        "ax_sha256": "1" * 64,
        "ax_stable_sha256": "2" * 64,
        "diff_sha256": "3" * 64,
        "t2_trace": {
            "ticks": [1, 2, 3],
            "seqs": [10, 20, 30],
            "admission_verdict": "PASS",
            "executor_verdict": "PASS",
        },
        "frame_refs": ["4" * 64],
        "state_sha256": "5" * 64,
        "gameplay_state_hash": "6" * 64,
        "renderer": "dummy",
        "frame_grab_status": "GRAB_UNAVAILABLE",
    }
    res = TrainingSignalValidator.validate(ts)
    assert res["renderer"] == "dummy"


def test_unix_socket_tempdir_constraint():
    """Verify any test binding AF_UNIX socket uses short tempdir in /tmp with prefix w8, <100 TARGET bytes."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="w8") as tmp_dir:
        sock_path = str(Path(tmp_dir) / "test.sock")
        assert len(sock_path.encode("utf-8")) <= 100
        assert tmp_dir.startswith("/tmp/w8")
