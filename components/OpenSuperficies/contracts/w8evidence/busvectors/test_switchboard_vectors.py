"""Pytest and unittest contract suite for the host-owned per-capsule stream boundary.

Gate command (orchestrator):
  env -u OS_CU_TEST taskset -c 10-14 env PYTHONPATH=. python3 -m pytest -q contracts/w8evidence/busvectors

Also runnable directly via Python stdlib:
  python3 -B contracts/w8evidence/busvectors/test_switchboard_vectors.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest

from contracts.w8evidence.busvectors.validator import (
    AFFECTED_ACT_MIN_ITEMS,
    AFFECTED_MAX_ITEMS,
    ALLOWED_ACTS,
    ALLOWED_EXIT_CODES,
    ALLOWED_STATUS_VALUES,
    BODY_MAX_LEN,
    EXIT_EXECUTION_SETUP,
    EXIT_FORBIDDEN_ONE,
    EXIT_GATE_CONTAINMENT,
    EXIT_INDETERMINATE_QUARANTINE,
    EXIT_INPUT,
    EXIT_OPERATOR,
    EXIT_SUCCESS,
    EXIT_UNAVAILABLE_PREREQ,
    ID_MAX_LEN,
    LIVE_SWITCHBOARD_PACKAGE,
    MAX_JSON_DEPTH,
    MAX_UNIX_SOCKET_PATH_BYTES,
    PATH_MAX_LEN,
    SEQ_MAX,
    SEQ_MIN,
    SHA256_LEN,
    SIDECAR_REQUIRED_KEYS,
    SPEECH_ACT_REQUIRED_KEYS,
    SPEECH_ACT_SCHEMA,
    AuthenticatedVerifierPort,
    BoundaryRefusal,
    FakeMonotonicClock,
    HostSenderBinding,
    HostStreamBoundaryPort,
    InMemoryBodyStore,
    canonical_json_bytes,
    main as cli_main,
    map_speech_act_to_bus_intent,
    normalize_exit_code,
    run_vectors_file,
    sha256_hex,
    strict_parse_json,
    validate_sidecar_envelope,
    validate_speech_payload,
    validate_vector_case,
)


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
VECTORS_PATH = HERE / "vectors.json"
FROZEN_SCHEMA_PATH = REPO_ROOT / "contracts" / "worker" / "os.cu.worker-task.v1.schema.json"

SYNTH_REPRO_SHA = "a" * 64
SYNTH_OTHER_SHA = "b" * 64


def make_synthetic_speech(
    act: str = "finding",
    seq: int = 0,
    repro_sha256: str | None = SYNTH_REPRO_SHA,
    affected: list[str] | None = None,
    status: str | None = None,
    body: str = "synthetic passive observation",
) -> dict:
    return {
        "schema": SPEECH_ACT_SCHEMA,
        "act": act,
        "wave_id": "wave-synth-01",
        "task_id": "task-synth-01",
        "attempt_id": "a1",
        "lease_id": "lease-synth-01",
        "seq": seq,
        "repro_sha256": repro_sha256,
        "affected": list(affected) if affected is not None else [],
        "status": status,
        "body": body,
    }


def make_synthetic_harness(
    epoch: int = 2,
    native_available: bool = True,
    data_slots: int = 4,
    control_slots: int = 2,
    max_stop_delay_ms: int = 2000,
) -> tuple[HostStreamBoundaryPort, AuthenticatedVerifierPort, InMemoryBodyStore, FakeMonotonicClock, HostSenderBinding]:
    binding = HostSenderBinding(
        sender_extension="ext.host.capsule.synth01",
        wave_id="wave-synth-01",
        task_id="task-synth-01",
        attempt_id="a1",
        lease_id="lease-synth-01",
        epoch=epoch,
    )
    verifier = AuthenticatedVerifierPort(
        bindings={binding.sender_extension: binding},
        qualified_repros={SYNTH_REPRO_SHA},
    )
    store = InMemoryBodyStore(native_available=native_available)
    clock = FakeMonotonicClock(initial_ms=1000)
    port = HostStreamBoundaryPort(
        verifier=verifier,
        body_store=store,
        clock=clock,
        data_slots=data_slots,
        control_slots=control_slots,
        max_stop_delay_ms=max_stop_delay_ms,
    )
    return port, verifier, store, clock, binding


def make_synthetic_sidecar(
    payload: dict,
    store: InMemoryBodyStore | None = None,
    sender_extension: str = "ext.host.capsule.synth01",
    epoch: int = 2,
    **extra: object,
) -> tuple[dict, bytes]:
    raw_bytes = canonical_json_bytes(payload)
    digest = sha256_hex(raw_bytes)
    if store is not None:
        store.blobs[digest] = raw_bytes
    sidecar = {
        "sender_extension": sender_extension,
        "wave_id": payload["wave_id"],
        "task_id": payload["task_id"],
        "attempt_id": payload["attempt_id"],
        "lease_id": payload["lease_id"],
        "epoch": epoch,
        "seq": payload["seq"],
        "body_sha256": digest,
        "body_length": len(raw_bytes),
    }
    sidecar.update(extra)
    return sidecar, raw_bytes


class TestSwitchboardVectors(unittest.TestCase):
    """Unit-level offline contract test suite for the host-owned per-capsule stream boundary."""

    def test_01_all_vectors_in_vectors_json_pass_contract(self) -> None:
        rc, results = run_vectors_file(VECTORS_PATH)
        self.assertEqual(rc, EXIT_SUCCESS)
        self.assertGreaterEqual(len(results), 30)
        pos_names = {r["name"] for r in results if r["verdict"] == "PASS"}
        fail_names = {r["name"] for r in results if r["verdict"] == "EXPECTED_FAIL"}
        self.assertIn("pos_finding_maps_to_claim", pos_names)
        self.assertIn("pos_repro_maps_to_evidence", pos_names)
        self.assertIn("pos_affected_maps_to_query", pos_names)
        self.assertIn("pos_status_working_maps_to_commit", pos_names)
        self.assertIn("pos_status_done_maps_to_commit", pos_names)
        self.assertIn("pos_status_blocked_maps_to_yield", pos_names)
        self.assertIn("pos_status_stuck_maps_to_yield", pos_names)
        self.assertIn("pos_idempotent_same_sequence_replay", pos_names)
        self.assertIn("pos_data_flood_stop_independently_deliverable", pos_names)
        for required_fail in (
            "must_fail_stale_epoch",
            "must_fail_future_epoch",
            "must_fail_forged_sender_unknown_extension",
            "must_fail_forged_sender_mismatched_lease",
            "must_fail_guest_direct_switchboard_connect",
            "must_fail_capsule_self_named_extension",
            "must_fail_sequence_gap",
            "must_fail_changed_replay",
            "must_fail_path_traversal_dotdot",
            "must_fail_path_traversal_git_dir",
            "must_fail_truncated_body",
            "must_fail_preview_not_authoritative",
            "must_fail_missing_native_full_body_unavailable",
            "must_fail_file_bus_fallback_forbidden",
            "must_fail_unqualified_repro_on_finding",
            "must_fail_unqualified_repro_on_repro_act",
            "must_fail_duplicate_json_key",
            "must_fail_float_in_json",
            "must_fail_bool_as_integer_seq",
            "must_fail_unknown_key_in_speech_payload",
            "must_fail_flood_starving_stop",
        ):
            self.assertIn(required_fail, fail_names)

    def test_02_frozen_schema_alignment_and_ausente_marker(self) -> None:
        self.assertEqual(LIVE_SWITCHBOARD_PACKAGE, "AUSENTE")
        if FROZEN_SCHEMA_PATH.is_file():
            schema_doc = strict_parse_json(FROZEN_SCHEMA_PATH.read_bytes())
            speech_def = schema_doc["$defs"]["speech_act"]
            self.assertEqual(set(speech_def["required"]), set(SPEECH_ACT_REQUIRED_KEYS))
            self.assertEqual(set(speech_def["properties"].keys()), set(SPEECH_ACT_REQUIRED_KEYS))
            self.assertIs(speech_def["additionalProperties"], False)
            self.assertEqual(speech_def["properties"]["schema"]["const"], SPEECH_ACT_SCHEMA)
            self.assertEqual(set(speech_def["properties"]["act"]["enum"]), set(ALLOWED_ACTS))
            self.assertEqual(speech_def["properties"]["seq"]["minimum"], SEQ_MIN)
            self.assertEqual(speech_def["properties"]["seq"]["maximum"], SEQ_MAX)
            self.assertEqual(speech_def["properties"]["affected"]["maxItems"], AFFECTED_MAX_ITEMS)
            self.assertEqual(speech_def["properties"]["body"]["maxLength"], BODY_MAX_LEN)
            self.assertEqual(schema_doc["$defs"]["id"]["maxLength"], ID_MAX_LEN)
            self.assertEqual(schema_doc["$defs"]["sha256"]["maxLength"], SHA256_LEN)
            self.assertEqual(schema_doc["$defs"]["path"]["maxLength"], PATH_MAX_LEN)

    def test_03_frozen_speech_payload_exact_keys_and_sidecar_separation(self) -> None:
        payload = make_synthetic_speech(act="finding")
        validated = validate_speech_payload(payload)
        self.assertEqual(set(validated.keys()), set(SPEECH_ACT_REQUIRED_KEYS))

        # Mixing any sidecar transport key into the frozen speech payload must fail
        for sidecar_key in ("sender", "sender_extension", "epoch", "body_sha256", "body_length"):
            bad = copy.deepcopy(payload)
            bad[sidecar_key] = "forged"
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(bad)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)
            self.assertEqual(ctx.exception.class_name, "UNKNOWN_KEY")

        # Missing any of the 11 required keys must fail
        for req_key in SPEECH_ACT_REQUIRED_KEYS:
            bad = copy.deepcopy(payload)
            del bad[req_key]
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(bad)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)
            self.assertEqual(ctx.exception.class_name, "MISSING_KEY")

    def test_04_strict_json_rejects_duplicates_floats_bool_seq_bom_and_surrogates(self) -> None:
        # 1. Duplicate keys rejected
        dup_raw = (
            '{"schema":"os.cu.speech-act/v1","act":"status","wave_id":"w1","task_id":"t1",'
            '"attempt_id":"a1","lease_id":"l1","seq":0,"seq":0,"repro_sha256":null,'
            '"affected":[],"status":"working","body":"x"}'
        )
        with self.assertRaises(BoundaryRefusal) as ctx:
            strict_parse_json(dup_raw)
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(ctx.exception.class_name, "DUPLICATE_KEY")

        # 2. Floats, -0, NaN, Infinity rejected
        for bad_num in ("0.0", "1.25", "1e3", "-0", "NaN", "Infinity", "-Infinity"):
            raw = (
                '{"schema":"os.cu.speech-act/v1","act":"status","wave_id":"w1","task_id":"t1",'
                f'"attempt_id":"a1","lease_id":"l1","seq":{bad_num},"repro_sha256":null,'
                '"affected":[],"status":"working","body":"x"}'
            )
            with self.assertRaises(BoundaryRefusal) as ctx:
                strict_parse_json(raw)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)
            self.assertEqual(ctx.exception.class_name, "NONCANONICAL_NUMBER")

        # 3. Bool-as-integer sequences rejected in speech payload and sidecar
        for bool_val in (True, False):
            bad_speech = make_synthetic_speech(act="status", repro_sha256=None, status="working")
            bad_speech["seq"] = bool_val  # type: ignore[assignment]
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(bad_speech)
            self.assertEqual(ctx.exception.code, EXIT_INPUT)
            self.assertEqual(ctx.exception.class_name, "BOOL_AS_INTEGER")

            good_speech = make_synthetic_speech(act="status", repro_sha256=None, status="working")
            sidecar, _ = make_synthetic_sidecar(good_speech)
            for int_field in ("seq", "epoch", "body_length"):
                bad_sc = copy.deepcopy(sidecar)
                bad_sc[int_field] = bool_val
                with self.assertRaises(BoundaryRefusal) as ctx:
                    validate_sidecar_envelope(bad_sc)
                self.assertEqual(ctx.exception.code, EXIT_INPUT)
                self.assertEqual(ctx.exception.class_name, "BOOL_AS_INTEGER")

        # 4. UTF-8 BOM and lone UTF-16 surrogates rejected
        with self.assertRaises(BoundaryRefusal) as ctx:
            strict_parse_json(b'\xef\xbb\xbf{"a":1}')
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(ctx.exception.class_name, "MALFORMED")

        with self.assertRaises(BoundaryRefusal) as ctx:
            strict_parse_json('{"body":"\\ud800"}')
        self.assertEqual(ctx.exception.code, EXIT_INPUT)
        self.assertEqual(ctx.exception.class_name, "MALFORMED")

        # 5. Nesting depth > 128 rejected
        deep = "{" + '"a":' * (MAX_JSON_DEPTH + 2) + "1" + "}" * (MAX_JSON_DEPTH + 2)
        with self.assertRaises(BoundaryRefusal) as ctx:
            strict_parse_json(deep)
        self.assertEqual(ctx.exception.code, EXIT_INPUT)

    def test_05_act_dependent_constraints_and_field_bounds(self) -> None:
        # finding and repro require non-null repro_sha256 and null status
        for act in ("finding", "repro"):
            p_no_sha = make_synthetic_speech(act=act, repro_sha256=None, status=None)
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(p_no_sha)
            self.assertEqual(ctx.exception.class_name, "MISSING_REPRO_SHA256")

            p_with_status = make_synthetic_speech(act=act, repro_sha256=SYNTH_REPRO_SHA, status="working")
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(p_with_status)
            self.assertEqual(ctx.exception.class_name, "INVALID_STATUS")

        # affected requires minItems 1 in affected and null status
        p_empty_aff = make_synthetic_speech(act="affected", repro_sha256=None, affected=[], status=None)
        with self.assertRaises(BoundaryRefusal) as ctx:
            validate_speech_payload(p_empty_aff)
        self.assertEqual(ctx.exception.class_name, "EMPTY_AFFECTED")

        # duplicate affected items rejected
        p_dup_aff = make_synthetic_speech(
            act="affected",
            repro_sha256=None,
            affected=["src/workerbridge/json.go", "src/workerbridge/json.go"],
            status=None,
        )
        with self.assertRaises(BoundaryRefusal) as ctx:
            validate_speech_payload(p_dup_aff)
        self.assertEqual(ctx.exception.class_name, "DUPLICATE_AFFECTED_PATH")

        # status act requires status in ("working", "blocked", "done", "stuck")
        p_null_status = make_synthetic_speech(act="status", repro_sha256=None, status=None)
        with self.assertRaises(BoundaryRefusal) as ctx:
            validate_speech_payload(p_null_status)
        self.assertEqual(ctx.exception.class_name, "INVALID_STATUS")

        # seq bounds 0..1000000 and body length 0..2048
        for bad_seq in (-1, SEQ_MAX + 1):
            p_seq = make_synthetic_speech(act="status", seq=bad_seq, repro_sha256=None, status="working")
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(p_seq)
            self.assertEqual(ctx.exception.class_name, "OUT_OF_RANGE")

        p_long_body = make_synthetic_speech(
            act="status",
            seq=0,
            repro_sha256=None,
            status="working",
            body="x" * (BODY_MAX_LEN + 1),
        )
        with self.assertRaises(BoundaryRefusal) as ctx:
            validate_speech_payload(p_long_body)
        self.assertEqual(ctx.exception.class_name, "INVALID_LENGTH")

    def test_06_speech_act_mapping_and_verifier_qualification(self) -> None:
        verifier = AuthenticatedVerifierPort(qualified_repros={SYNTH_REPRO_SHA})

        # finding -> claim and repro -> evidence when verifier qualifies repro
        self.assertEqual(
            map_speech_act_to_bus_intent(make_synthetic_speech(act="finding", repro_sha256=SYNTH_REPRO_SHA), verifier),
            "claim",
        )
        self.assertEqual(
            map_speech_act_to_bus_intent(make_synthetic_speech(act="repro", repro_sha256=SYNTH_REPRO_SHA), verifier),
            "evidence",
        )

        # finding and repro MUST FAIL when verifier is absent or does not qualify repro
        for act in ("finding", "repro"):
            with self.assertRaises(BoundaryRefusal) as ctx_none:
                map_speech_act_to_bus_intent(make_synthetic_speech(act=act, repro_sha256=SYNTH_REPRO_SHA), None)
            self.assertEqual(ctx_none.exception.code, EXIT_UNAVAILABLE_PREREQ)
            self.assertEqual(ctx_none.exception.class_name, "VERIFIER_UNAVAILABLE")

            with self.assertRaises(BoundaryRefusal) as ctx_unq:
                map_speech_act_to_bus_intent(make_synthetic_speech(act=act, repro_sha256=SYNTH_OTHER_SHA), verifier)
            self.assertEqual(ctx_unq.exception.code, EXIT_GATE_CONTAINMENT)
            self.assertEqual(ctx_unq.exception.class_name, "UNQUALIFIED_REPRO")

        # affected -> query
        self.assertEqual(
            map_speech_act_to_bus_intent(
                make_synthetic_speech(act="affected", repro_sha256=None, affected=["src/workerbridge/json.go"]),
                verifier=None,
            ),
            "query",
        )

        # working/done -> commit; blocked/stuck -> yield
        self.assertEqual(
            map_speech_act_to_bus_intent(
                make_synthetic_speech(act="status", repro_sha256=None, status="working"),
                verifier=None,
            ),
            "commit",
        )
        self.assertEqual(
            map_speech_act_to_bus_intent(
                make_synthetic_speech(act="status", repro_sha256=None, status="done"),
                verifier=None,
            ),
            "commit",
        )
        self.assertEqual(
            map_speech_act_to_bus_intent(
                make_synthetic_speech(act="status", repro_sha256=None, status="blocked"),
                verifier=None,
            ),
            "yield",
        )
        self.assertEqual(
            map_speech_act_to_bus_intent(
                make_synthetic_speech(act="status", repro_sha256=None, status="stuck"),
                verifier=None,
            ),
            "yield",
        )

    def test_07_must_fail_stale_epoch_future_epoch_and_forged_sender(self) -> None:
        port, _, store, _, _ = make_synthetic_harness(epoch=2)
        payload = make_synthetic_speech(act="finding", seq=0)

        # Stale epoch (1 < 2)
        sc_stale, _ = make_synthetic_sidecar(payload, store=store, epoch=1)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc_stale)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "STALE_EPOCH")

        # Future epoch (3 > 2)
        sc_future, _ = make_synthetic_sidecar(payload, store=store, epoch=3)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc_future)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "FUTURE_EPOCH")

        # Forged sender (unregistered extension)
        sc_forged, _ = make_synthetic_sidecar(payload, store=store, sender_extension="ext.forged.guest.99")
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc_forged)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "FORGED_SENDER")

        # Guest/capsule attempting direct switchboard connect or naming its own extension
        sc_direct, _ = make_synthetic_sidecar(payload, store=store, direct_switchboard_connect=True)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc_direct)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "CONTAINMENT_GUEST_SWITCHBOARD_CONNECT")

        sc_self_ext, _ = make_synthetic_sidecar(payload, store=store, guest_named_extension="ext.capsule.self")
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc_self_ext)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "CONTAINMENT_SELF_NAMED_EXTENSION")

    def test_08_must_fail_gap_changed_replay_traversal_and_truncated_body(self) -> None:
        port, _, store, _, _ = make_synthetic_harness(epoch=2)

        # Commit seq 0
        p0 = make_synthetic_speech(act="finding", seq=0)
        sc0, _ = make_synthetic_sidecar(p0, store=store)
        res0 = port.receive(sc0)
        self.assertEqual(res0["status"], "APPLIED")
        self.assertEqual(res0["bus_intent"], "claim")

        # Byte-identical replay at seq 0 is idempotent
        res0_replay = port.receive(sc0)
        self.assertEqual(res0_replay["status"], "IDEMPOTENT_REPLAY")

        # Changed replay at seq 0 -> quarantine (code 6)
        p0_changed = make_synthetic_speech(act="finding", seq=0, body="different body at seq 0")
        sc0_changed, _ = make_synthetic_sidecar(p0_changed, store=store)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc0_changed)
        self.assertEqual(ctx.exception.code, EXIT_INDETERMINATE_QUARANTINE)
        self.assertEqual(ctx.exception.class_name, "CHANGED_REPLAY")

        # Sequence gap (seq 2 when expected_seq is 1) -> refused (code 5)
        p2_gap = make_synthetic_speech(act="finding", seq=2)
        sc2_gap, _ = make_synthetic_sidecar(p2_gap, store=store)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc2_gap)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "SEQUENCE_GAP")

        # Truncated body at seq 1 -> quarantine (code 6)
        p1 = make_synthetic_speech(act="finding", seq=1)
        sc1, raw1 = make_synthetic_sidecar(p1, store=store)
        store.blobs[sc1["body_sha256"]] = raw1[:12]
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.receive(sc1)
        self.assertEqual(ctx.exception.code, EXIT_INDETERMINATE_QUARANTINE)
        self.assertEqual(ctx.exception.class_name, "TRUNCATED_BODY")

        # Traversal paths in affected -> refused (code 5)
        for unsafe_path in ("../secret.txt", "a/../b.py", "/etc/passwd", ".git/HEAD", "sub/.git/config", "a\\b.py"):
            p_trav = make_synthetic_speech(act="affected", seq=1, repro_sha256=None, affected=[unsafe_path])
            with self.assertRaises(BoundaryRefusal) as ctx:
                validate_speech_payload(p_trav)
            self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
            self.assertEqual(ctx.exception.class_name, "PATH_TRAVERSAL")

    def test_09_missing_native_full_body_and_preview_refusal(self) -> None:
        port_unavail, _, store_unavail, _, _ = make_synthetic_harness(native_available=False)
        p0 = make_synthetic_speech(act="finding", seq=0)
        sc0, _ = make_synthetic_sidecar(p0, store=store_unavail)

        # Missing native full-body capability -> code 4 unavailable
        with self.assertRaises(BoundaryRefusal) as ctx:
            port_unavail.receive(sc0)
        self.assertEqual(ctx.exception.code, EXIT_UNAVAILABLE_PREREQ)
        self.assertEqual(ctx.exception.class_name, "UNAVAILABLE_NATIVE_FULL_BODY")

        # File-bus fallback attempt -> code 4 forbidden
        port_ok, _, store_ok, _, _ = make_synthetic_harness(native_available=True)
        sc_fallback, _ = make_synthetic_sidecar(p0, store=store_ok, file_bus_fallback=True)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port_ok.receive(sc_fallback)
        self.assertEqual(ctx.exception.code, EXIT_UNAVAILABLE_PREREQ)
        self.assertEqual(ctx.exception.class_name, "FILE_BUS_FALLBACK_FORBIDDEN")

        # Preview is never authoritative -> code 5
        sc_preview, _ = make_synthetic_sidecar(p0, store=store_ok, is_preview=True)
        with self.assertRaises(BoundaryRefusal) as ctx:
            port_ok.receive(sc_preview)
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "PREVIEW_NOT_AUTHORITATIVE")

    def test_10_data_flood_transcript_stop_remains_independently_deliverable(self) -> None:
        port, _, store, clock, binding = make_synthetic_harness(
            data_slots=2,
            control_slots=2,
            max_stop_delay_ms=2000,
        )
        # Flood data lane with 6 frames (data_slots=2 -> 2 queued, 4 throttled)
        enqueued_flags = []
        for seq in range(6):
            p = make_synthetic_speech(act="status", seq=seq, repro_sha256=None, status="working", body=f"flood {seq}")
            sc, _ = make_synthetic_sidecar(p, store=store)
            enqueued_flags.append(port.enqueue_data(sc))

        self.assertEqual(enqueued_flags, [True, True, False, False, False, False])
        self.assertEqual(port.throttled_data_count, 4)

        # Issue STOP on the independent control lane while data lane is 100% full
        port.enqueue_stop(binding.lease_id, reason="flood_containment_stop")
        clock.advance(10)

        # Next step must deliver STOP first, independent of the saturated data queue
        step_out = port.step_lane()
        self.assertEqual(step_out["kind"], "STOP")
        self.assertEqual(step_out["exit_code"], EXIT_SUCCESS)
        self.assertEqual(step_out["elapsed_ms"], 10)
        self.assertTrue(step_out["independent_of_data_flood"])
        self.assertIn(binding.lease_id, port.stopped_leases)

        # Subsequent data frames for the stopped lease are fenced and refused
        with self.assertRaises(BoundaryRefusal) as ctx:
            port.step_lane()
        self.assertEqual(ctx.exception.code, EXIT_GATE_CONTAINMENT)
        self.assertEqual(ctx.exception.class_name, "LEASE_STOPPED")

    def test_11_cli_exit_codes_0_2_3_4_5_6_7_and_never_1(self) -> None:
        self.assertNotIn(EXIT_FORBIDDEN_ONE, ALLOWED_EXIT_CODES)
        self.assertEqual(cli_main(["--vectors", str(VECTORS_PATH)]), EXIT_SUCCESS)
        self.assertEqual(cli_main(["--help"]), EXIT_SUCCESS)
        self.assertEqual(cli_main(["--unknown-flag-xyz"]), EXIT_INPUT)
        self.assertEqual(cli_main(["--vectors", str(HERE / "nonexistent_vectors.json")]), EXIT_EXECUTION_SETUP)
        self.assertEqual(cli_main(["--simulate-exit", "0"]), EXIT_SUCCESS)
        self.assertEqual(cli_main(["--simulate-exit", "2"]), EXIT_INPUT)
        self.assertEqual(cli_main(["--simulate-exit", "3"]), EXIT_EXECUTION_SETUP)
        self.assertEqual(cli_main(["--simulate-exit", "4"]), EXIT_UNAVAILABLE_PREREQ)
        self.assertEqual(cli_main(["--simulate-exit", "5"]), EXIT_GATE_CONTAINMENT)
        self.assertEqual(cli_main(["--simulate-exit", "6"]), EXIT_INDETERMINATE_QUARANTINE)
        self.assertEqual(cli_main(["--simulate-exit", "7"]), EXIT_OPERATOR)
        self.assertEqual(cli_main(["--simulate-exit", "unexpected"]), EXIT_EXECUTION_SETUP)
        for sim in ("0", "2", "3", "4", "5", "6", "7", "unexpected"):
            self.assertNotEqual(cli_main(["--simulate-exit", sim]), EXIT_FORBIDDEN_ONE)

    def test_12_af_unix_socket_short_tmp_w8_path_bound(self) -> None:
        """Any test binding an AF_UNIX socket must use a short temporary directory under /tmp
        with prefix w8, remove it in cleanup, and assert full socket path <= 100 TARGET bytes.
        """
        port, _, store, _, _ = make_synthetic_harness()
        payload = make_synthetic_speech(act="finding", seq=0)
        sidecar, raw_body = make_synthetic_sidecar(payload, store=store)

        tmp_dir_obj = tempfile.TemporaryDirectory(prefix="w8", dir="/tmp")
        tmp_dir_path = tmp_dir_obj.name
        try:
            self.assertTrue(tmp_dir_path.startswith("/tmp/w8"))
            sock_path = os.path.join(tmp_dir_path, "sb.sock")
            sock_bytes = sock_path.encode("utf-8")
            self.assertLessEqual(len(sock_bytes), MAX_UNIX_SOCKET_PATH_BYTES)

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(sock_path)
                server.listen(1)
                client.connect(sock_path)
                conn, _ = server.accept()
                try:
                    frame_bytes = canonical_json_bytes(sidecar)
                    client.sendall(struct.pack(">I", len(frame_bytes)) + frame_bytes)
                    client.sendall(struct.pack(">I", len(raw_body)) + raw_body)

                    hdr1 = conn.recv(4)
                    len1 = struct.unpack(">I", hdr1)[0]
                    recv_sidecar_bytes = conn.recv(len1)

                    hdr2 = conn.recv(4)
                    len2 = struct.unpack(">I", hdr2)[0]
                    recv_body_bytes = conn.recv(len2)

                    res = port.receive(recv_sidecar_bytes, override_body_bytes=recv_body_bytes)
                    self.assertEqual(res["exit_code"], EXIT_SUCCESS)
                    self.assertEqual(res["bus_intent"], "claim")
                finally:
                    conn.close()
            finally:
                client.close()
                server.close()
        finally:
            tmp_dir_obj.cleanup()
        self.assertFalse(os.path.exists(tmp_dir_path))

    def test_13_fixtures_contain_synthetic_identities_only_no_credentials(self) -> None:
        raw_text = VECTORS_PATH.read_text(encoding="utf-8")
        forbidden_markers = (
            "PRIVATE KEY",
            "ghp_",
            "github_pat_",
            "sk-live-",
            "sk-proj-",
            "AIza",
            "AKIA",
            "Bearer ",
            "os.environ",
            "/proc/self/cmdline",
            "/proc/self/environ",
        )
        for marker in forbidden_markers:
            self.assertNotIn(marker, raw_text)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    raise SystemExit(EXIT_SUCCESS if result.wasSuccessful() else EXIT_GATE_CONTAINMENT)
