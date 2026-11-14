"""Pytest unit test suite for the proposed local W8 text-only training-signal carrier validator.

Covers:
- Positive synthetic fixture and dummy-renderer GRAB_UNAVAILABLE fixture.
- Canonical integer-JSON key-order invariance and supplied-bytes verification.
- AF_UNIX short temporary directory (/tmp/w8*) socket path <= 100 TARGET bytes and cleanup.
- Must-FAIL controls: raw pixels, base64 image blobs, HTML or raw AX trees destined for a model,
  floats/non-canonical numbers, duplicate keys, hash collisions, wrong-session joins,
  non-monotonic T2 trace ticks/seqs, invalid verdicts, replay & mutated replay, missing/exceeded
  bounds, AUSENTE capture retention/byte bounds/source qualification blocking promotion,
  dummy renderer claiming a frame, unproven reproduced/verified labels, and secret/orchestration leaks.
- CLI exit code contract: 0, 2, 3, 4, 5, 6, 7 CITED; code 1 CITED is never returned.
"""

from __future__ import annotations

import copy
import io
import os
from pathlib import Path
import socket
import tempfile
import unittest
from typing import Any, Dict

_HAS_REAL_PYTEST = True
try:
    import pytest  # type: ignore
except ImportError:  # pragma: no cover - stdlib fallback when pytest is not installed locally
    _HAS_REAL_PYTEST = False

    class _RaisesContext:
        def __init__(self, exc_type: Any) -> None:
            self.exc_type = exc_type
            self.value: Any = None

        def __enter__(self) -> "_RaisesContext":
            return self

        def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
            if exc_type is None:
                raise AssertionError(f"DID NOT RAISE {self.exc_type}")
            if issubclass(exc_type, self.exc_type):
                self.value = exc_val
                return True
            return False

    class _PytestFallback:
        @staticmethod
        def raises(exc_type: Any) -> _RaisesContext:
            return _RaisesContext(exc_type)

    pytest = _PytestFallback()  # type: ignore

from contracts.w8evidence.training.validator import (
    AUSENTE,
    EXIT_GATE,
    EXIT_INPUT,
    EXIT_NEVER,
    EXIT_OK,
    EXIT_OPERATOR,
    EXIT_QUARANTINE,
    EXIT_SETUP,
    EXIT_UNAVAILABLE,
    GRAB_UNAVAILABLE,
    TARGET_MAX_UNIX_SOCKET_BYTES,
    DummyRendererPort,
    FakeClock,
    InMemorySupplyPort,
    RejectError,
    SupplyBundle,
    canonical_encode,
    exit_code_for,
    main,
    make_positive_synthetic_fixture,
    sha256_hex,
    strict_json_parse,
    validate_carrier,
)


def _rebind_carrier_hashes(carrier: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute state_sha256, gameplay_state_hash, diff_sha256, and t2_trace_sha256 after test edits."""
    state_sha = sha256_hex(canonical_encode(carrier["state"]))
    carrier["state_sha256"] = state_sha
    carrier["gameplay_state_hash"] = state_sha
    diff_sha = sha256_hex(canonical_encode(carrier["ui_tree_diff"]))
    carrier["diff_sha256"] = diff_sha
    if isinstance(carrier.get("t2_trace"), list) and carrier["t2_trace"]:
        carrier["t2_trace"][-1]["state_sha256"] = state_sha
        carrier["t2_trace"][-1]["diff_sha256"] = diff_sha
        carrier["t2_trace_sha256"] = sha256_hex(canonical_encode(carrier["t2_trace"]))
    return carrier


# ---------------------------------------------------------------------------
# 1. Positive controls
# ---------------------------------------------------------------------------


def test_positive_synthetic_fixture_and_supply_port() -> None:
    clock = FakeClock(initial_ns=5_000_000)
    carrier, bundle = make_positive_synthetic_fixture(dummy_renderer=False)
    port = InMemorySupplyPort(clock=clock)
    port.register_bundle(bundle)

    clock.advance_ns(250)
    res = validate_carrier(carrier, supply_port=port, require_promotion=True)
    assert res.valid is True
    assert res.promotable is True
    assert res.absent_facts == []
    assert res.renderer_status == "REFERENCED"
    assert res.state_sha256 == carrier["gameplay_state_hash"]
    assert res.state_sha256 not in (carrier["ax_sha256"], carrier["ax_stable_sha256"])
    assert carrier["ax_sha256"] != carrier["ax_stable_sha256"]
    assert carrier["claims"]["synthetic"] is True
    assert carrier["claims"]["real_capture"] is False
    assert carrier["claims"]["learning_success"] is False


def test_positive_dummy_renderer_grab_unavailable() -> None:
    carrier, bundle = make_positive_synthetic_fixture(dummy_renderer=True)
    port = InMemorySupplyPort()
    port.register_bundle(bundle)

    res = validate_carrier(carrier, supply_port=port, require_promotion=True)
    assert res.valid is True
    assert res.promotable is True
    assert res.renderer_status == GRAB_UNAVAILABLE
    assert carrier["frame_metadata"] == DummyRendererPort.frame_metadata()
    assert carrier["frame_metadata"]["status"] == GRAB_UNAVAILABLE
    assert carrier["frame_metadata"]["frame_sha256"] is None
    assert all(ev["frame_ref"] == GRAB_UNAVAILABLE for ev in carrier["t2_trace"])


def test_positive_canonical_integer_json_key_reordering_invariance() -> None:
    carrier1, _ = make_positive_synthetic_fixture()
    carrier2 = copy.deepcopy(carrier1)
    carrier2["state"] = {k: carrier1["state"][k] for k in reversed(list(carrier1["state"].keys()))}
    carrier2["ui_tree_diff"] = {
        k: carrier1["ui_tree_diff"][k] for k in reversed(list(carrier1["ui_tree_diff"].keys()))
    }

    res1 = validate_carrier(carrier1)
    res2 = validate_carrier(carrier2)
    assert res1.carrier_sha256 == res2.carrier_sha256
    assert res1.state_sha256 == res2.state_sha256
    assert res1.diff_sha256 == res2.diff_sha256


def test_positive_idempotent_redelivery_when_enabled() -> None:
    carrier, bundle = make_positive_synthetic_fixture()
    port = InMemorySupplyPort(allow_idempotent_redelivery=True)
    port.register_bundle(bundle)

    res1 = validate_carrier(carrier, supply_port=port)
    res2 = validate_carrier(carrier, supply_port=port)
    assert res1.carrier_sha256 == res2.carrier_sha256


def test_positive_af_unix_socket_short_tmpdir_under_100_bytes() -> None:
    """Any test binding an AF_UNIX socket must use /tmp with prefix w8, remove it, and stay <= 100 TARGET bytes."""
    saved_tmpdir = None
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="w8") as tmpdir:
        saved_tmpdir = tmpdir
        sock_path = os.path.join(tmpdir, "v.sock")
        assert sock_path.startswith("/tmp/w8")
        assert len(sock_path.encode("utf-8")) <= TARGET_MAX_UNIX_SOCKET_BYTES

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(sock_path)
            srv.listen(1)
            cli.connect(sock_path)
            conn, _ = srv.accept()
            try:
                carrier, _ = make_positive_synthetic_fixture()
                payload = canonical_encode(carrier)
                cli.sendall(payload)
                cli.shutdown(socket.SHUT_WR)
                received = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    received += chunk
                res = validate_carrier(received)
                assert res.valid is True
            finally:
                conn.close()
        finally:
            cli.close()
            srv.close()

    assert saved_tmpdir is not None and not os.path.exists(saved_tmpdir)


# ---------------------------------------------------------------------------
# 2. Must-FAIL controls
# ---------------------------------------------------------------------------


def test_must_fail_raw_pixels_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["state"]["raw_pixels"] = [255, 0, 128]
    _rebind_carrier_hashes(c1)
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "RAW_PIXELS_OR_BINARY"

    c2, _ = make_positive_synthetic_fixture()
    c2["state"]["scanline"] = list(range(32))
    _rebind_carrier_hashes(c2)
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "RAW_PIXELS_OR_BINARY"

    c3, _ = make_positive_synthetic_fixture()
    c3["ui_tree_diff"]["ops"][0]["summary"] = "srf-fbread xid=42 raw dump"
    _rebind_carrier_hashes(c3)
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "RAW_PIXELS_OR_BINARY"

    with pytest.raises(RejectError) as exc_info4:
        strict_json_parse(b'{"a": "hello\x00world"}')
    assert exc_info4.value.code == EXIT_GATE


def test_must_fail_base64_image_blobs_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["ui_tree_diff"]["ops"][0]["summary"] = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    _rebind_carrier_hashes(c1)
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "BASE64_IMAGE_BLOB"

    c2, _ = make_positive_synthetic_fixture()
    c2["ui_tree_diff"]["ops"][0]["summary"] = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    _rebind_carrier_hashes(c2)
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "BASE64_IMAGE_BLOB"

    c3, _ = make_positive_synthetic_fixture()
    c3["state"]["image_base64"] = "short"
    _rebind_carrier_hashes(c3)
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "BASE64_IMAGE_BLOB"


def test_must_fail_html_or_raw_ax_trees_destined_for_model_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["ui_tree_diff"]["ops"][0]["summary"] = "<div class='hud'><span>450</span></div>"
    _rebind_carrier_hashes(c1)
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "HTML_OR_RAW_AX_FOR_MODEL"

    c2, _ = make_positive_synthetic_fixture()
    c2["ui_tree_diff"]["ops"][0]["summary"] = "AXRootWebArea -> AXWindow -> AXStaticText value='score'"
    _rebind_carrier_hashes(c2)
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "HTML_OR_RAW_AX_FOR_MODEL"

    c3, _ = make_positive_synthetic_fixture()
    c3["state"]["destined_for_model"] = 1
    _rebind_carrier_hashes(c3)
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "HTML_OR_RAW_AX_FOR_MODEL"


def test_must_fail_floats_and_noncanonical_numbers_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["state"]["reward"] = 0.75
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "NONCANONICAL_NUMBER"

    bad_wires = [
        b'{"a": 1.0}',
        b'{"a": 1e5}',
        b'{"a": -0}',
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b'\xef\xbb\xbf{"a": 1}',
        b'{"a": "\\ud800"}',
    ]
    for raw in bad_wires:
        with pytest.raises(RejectError) as wire_exc:
            strict_json_parse(raw)
        assert wire_exc.value.code == EXIT_GATE


def test_must_fail_duplicate_keys_rejected() -> None:
    carrier, _ = make_positive_synthetic_fixture()
    raw_text = canonical_encode(carrier).decode("utf-8")
    dup_raw = '{"seed":1,' + raw_text[1:]
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(dup_raw)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "DUPLICATE_KEY"


def test_must_fail_distinct_ax_and_gameplay_hashes_enforced() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["ax_stable_sha256"] = c1["ax_sha256"]
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "AX_HASH_COLLISION"

    c2, _ = make_positive_synthetic_fixture()
    c2["ax_sha256"] = c2["state_sha256"]
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "GAMEPLAY_AX_HASH_COLLISION"

    c3, _ = make_positive_synthetic_fixture()
    c3["gameplay_state_hash"] = "f" * 64
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "STATE_HASH_MISMATCH"


def test_must_fail_wrong_session_joins_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["t2_trace"][0]["session_id"] = "sess-other-999"
    c1["t2_trace_sha256"] = sha256_hex(canonical_encode(c1["t2_trace"]))
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "WRONG_SESSION_JOIN"

    c2, _ = make_positive_synthetic_fixture()
    c2["ui_tree_diff"]["tab_id"] = "tab-wrong-9"
    _rebind_carrier_hashes(c2)
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "WRONG_SESSION_JOIN"

    c3, bundle = make_positive_synthetic_fixture()
    bad_bundle = SupplyBundle(
        session_id=bundle.session_id,
        segment_id=bundle.segment_id,
        tab_id=bundle.tab_id,
        target="wrong-target",
        state_bytes=bundle.state_bytes,
        ui_diff_bytes=bundle.ui_diff_bytes,
        t2_trace_bytes=bundle.t2_trace_bytes,
        ax_sha256=bundle.ax_sha256,
        ax_stable_sha256=bundle.ax_stable_sha256,
        frame_sha256=bundle.frame_sha256,
        renderer_status=bundle.renderer_status,
    )
    port = InMemorySupplyPort()
    port.register_bundle(bad_bundle)
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3, supply_port=port)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "WRONG_SESSION_JOIN"


def test_must_fail_non_monotonic_trace_and_invalid_verdicts_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["t2_trace"][1]["seq"] = 1
    c1["t2_trace_sha256"] = sha256_hex(canonical_encode(c1["t2_trace"]))
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "NON_MONOTONIC_TRACE"

    c2, _ = make_positive_synthetic_fixture()
    c2["t2_trace"][0]["tick"] = 10
    c2["t2_trace"][1]["tick"] = 6
    c2["t2_trace_sha256"] = sha256_hex(canonical_encode(c2["t2_trace"]))
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "NON_MONOTONIC_TRACE"

    c3, _ = make_positive_synthetic_fixture()
    c3["t2_trace"][0]["admission_verdict"] = "DENIED"
    c3["t2_trace"][0]["executor_verdict"] = "APPLIED"
    c3["t2_trace_sha256"] = sha256_hex(canonical_encode(c3["t2_trace"]))
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "INVALID_TRACE_VERDICT"


def test_must_fail_replay_and_mutated_replay_rejected() -> None:
    carrier, bundle = make_positive_synthetic_fixture()
    port = InMemorySupplyPort(allow_idempotent_redelivery=False)
    port.register_bundle(bundle)

    res = validate_carrier(carrier, supply_port=port)
    assert res.valid is True

    with pytest.raises(RejectError) as exc_dup:
        validate_carrier(carrier, supply_port=port)
    assert exc_dup.value.code == EXIT_GATE
    assert exc_dup.value.reject_class == "REPLAY_DETECTED"

    mutated = copy.deepcopy(carrier)
    mutated["seed"] = 999999
    with pytest.raises(RejectError) as exc_mut:
        validate_carrier(mutated, supply_port=port)
    assert exc_mut.value.code == EXIT_QUARANTINE
    assert exc_mut.value.reject_class == "QUARANTINE_MUTATED_REPLAY"


def test_must_fail_missing_bounds_and_exceeded_bounds_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture()
    c1["acquisition_provenance"]["byte_bounds"]["max_state_bytes"] = 0
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "MISSING_BOUNDS"

    c2, _ = make_positive_synthetic_fixture()
    c2["requested_ticks"] = 8
    c2["actual_ticks"] = 12
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "TICK_BOUND_EXCEEDED"

    c3, _ = make_positive_synthetic_fixture()
    c3["acquisition_provenance"]["byte_bounds"]["max_state_bytes"] = 8
    with pytest.raises(RejectError) as exc_info3:
        validate_carrier(c3)
    assert exc_info3.value.code == EXIT_GATE
    assert exc_info3.value.reject_class == "BYTE_BOUND_EXCEEDED"


def test_must_fail_ausente_capture_retention_byte_bounds_or_qualification_blocks_promotion() -> None:
    for field_name in ("capture_retention", "byte_bounds", "source_qualification"):
        carrier, _ = make_positive_synthetic_fixture()
        carrier["acquisition_provenance"][field_name] = AUSENTE

        with pytest.raises(RejectError) as exc_info:
            validate_carrier(carrier, require_promotion=True)
        assert exc_info.value.code == EXIT_GATE
        assert exc_info.value.reject_class == "AUSENTE_BLOCKS_PROMOTION"
        assert f"{field_name}:{AUSENTE}" in exc_info.value.detail

        res = validate_carrier(carrier, require_promotion=False)
        assert res.valid is True
        assert res.promotable is False
        assert f"{field_name}:{AUSENTE}" in res.absent_facts


def test_must_fail_dummy_renderer_claiming_frame_rejected() -> None:
    c1, _ = make_positive_synthetic_fixture(dummy_renderer=False)
    c1["acquisition_provenance"]["renderer_mode"] = "dummy"
    with pytest.raises(RejectError) as exc_info:
        validate_carrier(c1)
    assert exc_info.value.code == EXIT_GATE
    assert exc_info.value.reject_class == "DUMMY_CLAIMED_FRAME"

    c2, _ = make_positive_synthetic_fixture(dummy_renderer=True)
    c2["t2_trace"][0]["frame_ref"] = "a" * 64
    c2["t2_trace_sha256"] = sha256_hex(canonical_encode(c2["t2_trace"]))
    with pytest.raises(RejectError) as exc_info2:
        validate_carrier(c2)
    assert exc_info2.value.code == EXIT_GATE
    assert exc_info2.value.reject_class == "DUMMY_CLAIMED_FRAME"


def test_must_fail_unproven_reproduced_or_verified_labels_and_claims_rejected() -> None:
    for bad_label in ("reproduced", "verified"):
        c, _ = make_positive_synthetic_fixture()
        c["claims"]["labels"].append(bad_label)
        with pytest.raises(RejectError) as exc_info:
            validate_carrier(c)
        assert exc_info.value.code == EXIT_GATE
        assert exc_info.value.reject_class == "UNPROVEN_LABEL"

    c_learn, _ = make_positive_synthetic_fixture()
    c_learn["claims"]["learning_success"] = True
    with pytest.raises(RejectError) as exc_learn:
        validate_carrier(c_learn)
    assert exc_learn.value.code == EXIT_GATE
    assert exc_learn.value.reject_class == "UNPROVEN_CLAIM"

    c_real, _ = make_positive_synthetic_fixture()
    c_real["claims"]["real_capture"] = True
    with pytest.raises(RejectError) as exc_real:
        validate_carrier(c_real)
    assert exc_real.value.code == EXIT_GATE
    assert exc_real.value.reject_class == "UNPROVEN_CLAIM"


def test_must_fail_secrets_credentials_env_cmdline_and_orchestration_rejected() -> None:
    forbidden_payloads = [
        ("SECRET_OR_ENV_LEAK", "CREDENTIAL_TOKEN=redacted"),
        ("SECRET_OR_ENV_LEAK", "ENV_DUMP_PAYLOAD=1"),
        ("SECRET_OR_ENV_LEAK", "CMDLINE_DUMP_PAYLOAD=1"),
        ("SECRET_OR_ENV_LEAK", "PRIVATE_KEY_MATERIAL"),
        ("PASSIVE_DATA_VIOLATION", "ORCHESTRATE_COMMAND run"),
        ("PASSIVE_DATA_VIOLATION", "EXEC_DIRECTIVE spawn"),
    ]
    for expected_class, text in forbidden_payloads:
        c, _ = make_positive_synthetic_fixture()
        c["ui_tree_diff"]["ops"][0]["summary"] = text
        _rebind_carrier_hashes(c)
        with pytest.raises(RejectError) as exc_info:
            validate_carrier(c)
        assert exc_info.value.code == EXIT_GATE
        assert exc_info.value.reject_class == expected_class

    for forbidden_key in ("credentials", "env_dump", "proc_cmdline"):
        c_key, _ = make_positive_synthetic_fixture()
        c_key["state"][forbidden_key] = "present"
        _rebind_carrier_hashes(c_key)
        with pytest.raises(RejectError) as exc_key:
            validate_carrier(c_key)
        assert exc_key.value.code == EXIT_GATE
        assert exc_key.value.reject_class == "SECRET_OR_ENV_LEAK"


def test_cli_exit_codes_0_2_3_4_5_6_7_and_never_1() -> None:
    sink = io.StringIO()
    assert main(["--selftest"], stderr=sink) == EXIT_OK

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        valid_carrier, bundle = make_positive_synthetic_fixture()
        valid_file = tmp_path / "valid.json"
        valid_file.write_bytes(canonical_encode(valid_carrier))
        assert main(["--carrier", str(valid_file)], stderr=sink) == EXIT_OK

        # 2 CITED: invalid CLI arguments or wrong top-level schema
        assert main([], stderr=sink) == EXIT_INPUT
        assert main(["--unknown-flag"], stderr=sink) == EXIT_INPUT
        assert main(["--selftest", "--carrier", str(valid_file)], stderr=sink) == EXIT_INPUT

        bad_schema = copy.deepcopy(valid_carrier)
        bad_schema["schema"] = "wrong-schema-v0"
        bad_schema_file = tmp_path / "bad_schema.json"
        bad_schema_file.write_bytes(canonical_encode(bad_schema))
        assert main(["--carrier", str(bad_schema_file)], stderr=sink) == EXIT_INPUT

        # 3 CITED: execution or setup failure (missing file)
        assert main(["--carrier", str(tmp_path / "does_not_exist.json")], stderr=sink) == EXIT_SETUP

        # 4 CITED: unavailable prerequisite (--require-supply-port with unpopulated bundle)
        assert main(["--carrier", str(valid_file), "--require-supply-port"], stderr=sink) == EXIT_UNAVAILABLE

        # 5 CITED: gate / validation refusal (e.g. AUSENTE blocking promotion)
        ausente_carrier = copy.deepcopy(valid_carrier)
        ausente_carrier["acquisition_provenance"]["capture_retention"] = AUSENTE
        ausente_file = tmp_path / "ausente.json"
        ausente_file.write_bytes(canonical_encode(ausente_carrier))
        assert main(["--carrier", str(ausente_file)], stderr=sink) == EXIT_GATE
        assert main(["--carrier", str(ausente_file), "--allow-ausente-unpromoted"], stderr=sink) == EXIT_OK

        # 6 CITED: indeterminate quarantine (corrupted supplied state_bytes vs carrier state_sha256)
        corrupt_bundle = SupplyBundle(
            session_id=bundle.session_id,
            segment_id=bundle.segment_id,
            tab_id=bundle.tab_id,
            target=bundle.target,
            state_bytes=b'{"corrupt":1}',
            ui_diff_bytes=bundle.ui_diff_bytes,
            t2_trace_bytes=bundle.t2_trace_bytes,
            ax_sha256=bundle.ax_sha256,
            ax_stable_sha256=bundle.ax_stable_sha256,
            frame_sha256=bundle.frame_sha256,
            renderer_status=bundle.renderer_status,
        )
        port = InMemorySupplyPort()
        port.register_bundle(corrupt_bundle)
        with pytest.raises(RejectError) as exc_q:
            validate_carrier(valid_carrier, supply_port=port)
        assert exit_code_for(exc_q.value) == EXIT_QUARANTINE

        # 7 CITED: operator authorization required
        op_carrier = copy.deepcopy(valid_carrier)
        op_carrier["claims"]["labels"].append("operator_override")
        op_file = tmp_path / "operator.json"
        op_file.write_bytes(canonical_encode(op_carrier))
        assert main(["--carrier", str(op_file)], stderr=sink) == EXIT_OPERATOR

        # Code 1 CITED is never returned
        for observed in (
            EXIT_OK,
            EXIT_INPUT,
            EXIT_SETUP,
            EXIT_UNAVAILABLE,
            EXIT_GATE,
            EXIT_QUARANTINE,
            EXIT_OPERATOR,
            exit_code_for(None),
            exit_code_for(RuntimeError("unexpected")),
        ):
            assert observed != EXIT_NEVER


if not _HAS_REAL_PYTEST:  # pragma: no cover
    class TestTrainingValidatorUnittestBridge(unittest.TestCase):
        """Unittest discovery bridge when pytest is absent in a minimal stdlib environment."""

        def test_all_pytest_functions(self) -> None:
            for name, fn in sorted(globals().items()):
                if name.startswith("test_") and callable(fn):
                    with self.subTest(name=name):
                        fn()
