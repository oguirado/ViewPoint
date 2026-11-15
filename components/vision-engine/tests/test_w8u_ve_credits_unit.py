"""Unit and state-machine tests for owner w8u-ve-credits (vadapt/w8u_credits.py).

Covers gates:
- U3 Reverse control and backpressure
- Frame state machine
"""

import ast
import hashlib
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock, patch

from vadapt import rtring
from vadapt import w8u_credits as wc
from vadapt import window_stream_bank as wsb


def make_accepted_contract(*, accepted: bool = True) -> wc.ContractEvidence:
    """Create a complete revision-bound w8u-os-contract fixture."""
    return wc.ContractEvidence(
        revision="w8u-os-contract-rev-20261020-01",
        accepted=accepted,
        binding_schema_id="w8u.binding/v1",
        action_result_schema_id="w8u.action-result/v1",
        control_specs={
            (wc.CONTROL_MAGIC_HELLO, wc.CONTROL_KIND_HELLO): {
                "version": 1,
                "min_body_bytes": 2,
                "max_body_bytes": wc.HELLO_MAX_BODY_BYTES,
                "role": "hello",
            },
            (wc.CONTROL_MAGIC_CONFIRM, 1): {
                "version": 1,
                "min_body_bytes": 4,
                "max_body_bytes": 4,
                "expected_body": b"OK\x00\x01",
                "role": "hello_confirm",
            },
            (wc.CONTROL_MAGIC_HELLO, wc.CONTROL_KIND_ACK): {
                "version": 1,
                "min_body_bytes": 16,
                "max_body_bytes": 16,
                "role": "ack",
            },
            (wc.CONTROL_MAGIC_HELLO, wc.CONTROL_KIND_STOP): {
                "version": 1,
                "min_body_bytes": 0,
                "max_body_bytes": 0,
                "role": "stop",
            },
            (wc.CONTROL_MAGIC_END, 4): {
                "version": 1,
                "min_body_bytes": 20,
                "max_body_bytes": 20,
                "role": "end",
            },
        },
        end_error_codes={
            "OK": 0,
            "E_TORN": 1,
            "E_WIRE": 2,
            "E_LENGTH": 3,
            "E_HASH": 4,
            "E_SEQ": 5,
            "E_TICK": 6,
            "E_BINDING": 7,
            "E_GEOMETRY": 8,
            "E_CREDIT": 9,
            "E_BACKPRESSURE": 10,
            "E_TIMEOUT": 11,
            "E_ROOT": 12,
            "E_RAM_QUOTA": 13,
        },
        digest_domains={
            "ui": b"w8u.ui.v1\x00",
            "sim": b"w8u.sim.v1\x00",
            "rgb24": b"w8u.rgb24.v1\x00",
            "binding": b"w8u.binding.v1\x00",
        },
        native_error_map={
            "NAT_ERR_BAD_ORDER": "E_WIRE",
            "NAT_ERR_OVERSIZED_HDR": "E_LENGTH",
            "NAT_ERR_BAD_GEOMETRY": "E_GEOMETRY",
            "NAT_ERR_BAD_HASH": "E_HASH",
            "NAT_ERR_TORN_PAIR": "E_TORN",
        },
        xwd_subset={
            "byte_order": 1,
            "bitmap_bit_order": 1,
            "min_header_bytes": 100,
            "max_header_bytes": 512,
            "max_ncolors": 256,
            "allowed_bpp": (24, 32),
            "bitmap_pad_bits": (8, 16, 32),
            "red_mask": 0x00FF0000,
            "green_mask": 0x0000FF00,
            "blue_mask": 0x000000FF,
        },
        receipt_closed_fields={
            "top": ("schema", "contract_revision", "identity", "populations", "first_frame", "last_frame", "foreign", "verdict", "evidence"),
            "identity": ("run_id", "lease_id", "container_id", "binding_sha256", "target_handle", "epoch"),
            "populations": ("sent_frames", "admitted_frames", "acked_frames", "spent_credits", "cumulative_credit_total", "outstanding_consumer_leases", "actions_admitted", "actions_applied"),
            "frame_summary": wc.FRAME_METADATA_KEYS,
            "foreign": ("action_result_schema", "admitted", "applied", "t2", "state"),
            "verdict": ("status", "code", "controller_status"),
            "evidence": ("retention_mode", "pixel_replay", "reserved_bytes", "retained_ambiguous_charges", "listener", "exit", "cleanup"),
        },
        vectors={
            "accepted": [
                {"id": "vec_accept_4x2_bgrx_to_rgb24", "width": 4, "height": 2},
            ],
            "rejected": [
                {"id": "vec_reject_oversized_header", "kind": "oversized_header", "expected_error": "E_LENGTH"},
                {"id": "vec_reject_invalid_bit_order", "kind": "invalid_bitmap_bit_order", "expected_error": "E_WIRE"},
            ],
        },
    )


def make_valid_binding_dict(
    *,
    width: int = 4,
    height: int = 2,
    max_inflight_frames: int = 2,
    max_record_bytes: int = 65536,
    tick_mode: str = "committed",
    generation: int = 1,
    epoch: int = 0,
) -> dict:
    """Build a valid closed 19-field W8U binding dictionary."""
    return {
        "schema": "w8u.binding/v1",
        "run_id": "run-w8u-001",
        "lease_id": "lease-580590849aaf35c3",
        "seat_grant_sha256": "a" * 64,
        "container_id": "os-cu-0-box-1",
        "target_handle": {
            "kind": "x11_window",
            "canonical_id": "win-0x200001",
            "generation": generation,
        },
        "epoch": epoch,
        "display": ":99",
        "xid": 0x200001,
        "width": width,
        "height": height,
        "frames_root": "/dev/shm/vqa-ramws",
        "frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-w8u-001",
        "image_sha256": "b" * 64,
        "reader_sha256": "c" * 64,
        "exec_allowlist_sha256": "d" * 64,
        "max_record_bytes": max_record_bytes,
        "max_inflight_frames": max_inflight_frames,
        "tick_mode": tick_mode,
    }


def make_rgb24_pattern(width: int, height: int, seed: int) -> bytes:
    """Create deterministic in-RAM RGB24 bytes."""
    out = bytearray(width * height * 3)
    for i in range(len(out)):
        out[i] = (seed * 37 + i * 13) & 0xFF
    return bytes(out)


def build_valid_wire_frame(
    binding: wc.ConfirmedBinding,
    contract: wc.ContractEvidence,
    *,
    seq: int,
    wire_tick: int = 10,
    tick_valid: bool | None = None,
    capture_mono_ns: int = 1_000_000,
    seed: int = 1,
    header_diff_b: bool = False,
) -> bytes:
    """Build a valid big-endian W8UF frame with two matching canonical RGB24 XWD captures."""
    rgb = make_rgb24_pattern(binding.width, binding.height, seed)
    xwd_a = wc.build_xwd_bytes(
        rgb,
        width=binding.width,
        height=binding.height,
        contract=contract,
        extra_header_bytes=b"winA\x00\x00\x00\x00",
        alpha_fill=0x00,
    )
    xwd_b = wc.build_xwd_bytes(
        rgb,
        width=binding.width,
        height=binding.height,
        contract=contract,
        extra_header_bytes=(b"window_title_B\x00\x00" if header_diff_b else b"winA\x00\x00\x00\x00"),
        alpha_fill=(0xFF if header_diff_b else 0x00),
    )
    digest = wc.compute_rgb24_sha256(rgb, contract)
    tv = (binding.tick_mode == "committed") if tick_valid is None else tick_valid
    wt = wire_tick if binding.tick_mode == "committed" else 0
    return wc.encode_wire_frame(
        seq=seq,
        wire_tick=wt,
        tick_valid=tv,
        pair_under_grab=True,
        capture_mono_ns=capture_mono_ns,
        width=binding.width,
        height=binding.height,
        sha256_rgb24=digest,
        binding_sha256=binding.binding_sha256,
        xwd_a=xwd_a,
        xwd_b=xwd_b,
    )


class _PinnedRing:
    """Test ring owner whose close() can simulate an unproven unmap until released."""

    def __init__(self, size: int, *, allow_close: bool = False):
        self.buffer = bytearray(size)
        self.allow_close = allow_close

    def close(self) -> bool:
        if not self.allow_close:
            return False
        self.buffer = None
        return True


class TestW8UVeCreditsUnit(unittest.TestCase):
    """Comprehensive unit and state-machine suite for w8u-ve-credits."""

    def setUp(self):
        self.clock = wc.InjectedClock(1_000_000)
        self.contract = make_accepted_contract()
        self.bank = wsb.WindowStreamBank(quota_bytes=16 * 1024 * 1024)
        self.auth_calls = []

        def grant_auth(b_dict):
            self.auth_calls.append(b_dict["seat_grant_sha256"])
            return b_dict.get("seat_grant_sha256") == "a" * 64

        self.grant_auth = grant_auth

    def _new_controller(self, **kwargs) -> wc.W8UCreditController:
        params = {
            "clock": self.clock,
            "contract": self.contract,
            "bank": self.bank,
            "grant_authenticator": self.grant_auth,
            "consumer_timeout_ns": 50_000_000,
            "capture_timeout_ns": 50_000_000,
        }
        params.update(kwargs)
        return wc.W8UCreditController(**params)

    def test_contract_authority_ausente_and_pending_rejected(self):
        """Omitted (AUSENTE) or PREPARED-only contracts must be rejected without defaults."""
        for bad_contract in (None, wc.AUSENTE, make_accepted_contract(accepted=False)):
            with self.assertRaises(wc.W8UCreditError) as ctx:
                self._new_controller(contract=bad_contract)
            self.assertEqual(ctx.exception.code, "E_BINDING")

        # Missing oversized_header or invalid_bitmap_bit_order vector is rejected
        incomplete_vecs = make_accepted_contract()
        bad_vec_contract = wc.ContractEvidence(
            **{
                **incomplete_vecs.__dict__,
                "vectors": {"accepted": [{"id": "ok"}], "rejected": [{"kind": "oversized_header"}]},
            }
        )
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_contract_evidence(bad_vec_contract)
        self.assertEqual(ctx.exception.code, "E_BINDING")

    def test_binding_canonical_json_roots_and_independent_grant_auth(self):
        """Canonical JSON rejects bool/float/LF/unsorted keys; roots and grant auth are enforced."""
        b = make_valid_binding_dict()
        raw = wc.canonical_json_bytes(b)
        self.assertNotIn(b"\n", raw)
        self.assertNotIn(b" ", raw)

        # Trailing LF rejected
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.parse_canonical_binding_json(raw + b"\n")
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Unsorted keys rejected
        unsorted = b'{"z":1,"a":2}'
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.parse_canonical_binding_json(unsorted)
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Bool and float rejected in integer-only canonical JSON
        for bad_val in (True, False, 1.5):
            bad_b = dict(b, epoch=bad_val)
            with self.assertRaises(wc.W8UCreditError) as ctx:
                wc.canonical_json_bytes(bad_b)
            self.assertEqual(ctx.exception.code, "E_BINDING")

        # Root confinement checks: wrong root, alias/escape, or non-confined child -> E_ROOT
        for bad_roots in (
            ("/tmp/vqa-ramws", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-w8u-001"),
            ("/dev/shm/vqa-ramws/", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-w8u-001"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/other/os-cu-0/run-w8u-001"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/../run-w8u-001"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run/run-w8u-001"),
        ):
            bad_b = dict(b, frames_root=bad_roots[0], frame_run_root=bad_roots[1])
            with self.assertRaises(wc.W8UCreditError) as ctx:
                wc.validate_binding(
                    bad_b, contract=self.contract, grant_authenticator=self.grant_auth
                )
            self.assertEqual(ctx.exception.code, "E_ROOT")

        # Independent grant authentication failure even with valid canonical JSON digest -> E_BINDING
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_binding(
                b, contract=self.contract, grant_authenticator=lambda _: False
            )
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Target/generation/epoch/geometry mismatch -> E_BINDING / E_GEOMETRY
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_binding(
                b,
                contract=self.contract,
                grant_authenticator=self.grant_auth,
                expected_epoch=99,
            )
        self.assertEqual(ctx.exception.code, "E_BINDING")

        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_binding(
                b,
                contract=self.contract,
                grant_authenticator=self.grant_auth,
                expected_geometry=(16, 16),
            )
        self.assertEqual(ctx.exception.code, "E_GEOMETRY")

    def test_roots_require_one_child_and_match_available_run_id(self):
        root = wc.FRAMES_ROOT
        prefix = root + "/opensuperficies/os-cu-0/"
        for run_id in ("w8u-vec-0001", "run-w8u-001", "run"):
            with self.subTest(run_id=run_id):
                self.assertEqual(wc.validate_roots(root, prefix + run_id), (root, prefix + run_id))
                self.assertEqual(wc.validate_roots(root, prefix + run_id, run_id=run_id),
                                 (root, prefix + run_id))
        for child in ("", ".", "..", ".hidden", "run/w8u-vec-0001",
                      "w8u-vec-0001/nested", "w8u-vec-0001/", "bad\x00child"):
            with self.subTest(child=child), self.assertRaises(wc.W8UCreditError) as ctx:
                wc.validate_roots(root, prefix + child)
            self.assertEqual(ctx.exception.code, "E_ROOT")
        for run_id in ("", ".", "..", "nested/run", "bad\x00id", "another-run", False):
            with self.subTest(run_id=run_id), self.assertRaises(wc.W8UCreditError) as ctx:
                wc.validate_roots(root, prefix + "w8u-vec-0001", run_id=run_id)
            self.assertEqual(ctx.exception.code, "E_ROOT")

    def test_binding_rejects_symlinked_components_before_grant(self):
        binding = make_valid_binding_dict()
        for linked_path in (binding["frames_root"],
                            binding["frames_root"] + "/opensuperficies",
                            binding["frames_root"] + "/opensuperficies/os-cu-0",
                            binding["frame_run_root"]):
            with self.subTest(linked_path=linked_path):
                authenticator = Mock(return_value=True)
                with patch("vadapt.w8u_credits.os.path.islink", side_effect=lambda p: p == linked_path):
                    with self.assertRaises(wc.W8UCreditError) as ctx:
                        wc.validate_binding(binding, contract=self.contract, grant_authenticator=authenticator)
                self.assertEqual(ctx.exception.code, "E_ROOT")
                authenticator.assert_not_called()

    def test_hello_grants_no_credit_and_requires_capacity_reservation(self):
        """HELLO grants 0 credit, cannot start capture, and requires host capacity before initial grant."""
        ctrl = self._new_controller()
        b_dict = make_valid_binding_dict(max_inflight_frames=2)
        confirmed = ctrl.receive_hello(b_dict)
        self.assertEqual(ctrl.available_credits, 0)
        self.assertEqual(ctrl.cumulative_credit_total, 0)
        self.assertEqual(ctrl.spent_total, 0)
        self.assertEqual(ctrl.sent_seq, 0)

        # W8UH confirmation round-trips cleanly against contract
        w8uh = ctrl.encode_hello_confirmation()
        dec_confirm = wc.decode_control_frame(
            w8uh, contract=self.contract, expected_binding_sha256=confirmed.binding_sha256
        )
        self.assertEqual(dec_confirm.magic, wc.CONTROL_MAGIC_CONFIRM)
        self.assertTrue(dec_confirm.payload["confirmed"])

        # Attempting capture right after HELLO pauses without calling grab or alloc
        calls = []
        outcome = ctrl.capture_frame(
            grab_fn=lambda: calls.append("grab"),
            alloc_pair_fn=lambda s: (calls.append("alloc"), b"")[1],
            ungrab_fn=lambda: True,
        )
        self.assertTrue(outcome.paused)
        self.assertIsNone(outcome.seq)
        self.assertEqual(calls, [])

        # Initial ACK before reserve_host_capacity raises E_CREDIT and leaves credits at 0
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.issue_initial_ack()
        self.assertEqual(ctx.exception.code, "E_CREDIT")
        self.assertEqual(ctrl.available_credits, 0)
        self.assertFalse(ctrl.initial_ack_done)

        # Reserving capacity then issuing initial ACK grants min(2, 2) == 2 credits
        ctrl.reserve_host_capacity()
        self.assertTrue(ctrl.capacity_reserved)
        self.assertGreater(self.bank.reserved_bytes, 0)
        ack_seq, grant = ctrl.issue_initial_ack()
        self.assertEqual((ack_seq, grant), (0, 2))
        self.assertEqual(ctrl.available_credits, 2)

    def test_single_frame_capacity_exhaustion_and_no_evidence_overwrite(self):
        """Single-slot capacity (max_inflight_frames=1) grants 1 credit, pauses at 0, and never overwrites evidence."""
        ctrl = self._new_controller()
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=1))
        ctrl.reserve_host_capacity()
        ack_seq, grant = ctrl.issue_initial_ack()
        self.assertEqual((ack_seq, grant), (0, 1))
        self.assertEqual(ctrl.available_credits, 1)

        events = []
        out1 = ctrl.capture_frame(
            grab_fn=lambda: events.append("grab1"),
            alloc_pair_fn=lambda s: build_valid_wire_frame(confirmed, self.contract, seq=s, seed=11),
            ungrab_fn=lambda: (events.append("ungrab1"), True)[1],
            output_fn=lambda _: events.append("out1"),
        )
        self.assertFalse(out1.paused)
        self.assertEqual(out1.seq, 1)
        self.assertEqual(ctrl.available_credits, 0)
        self.assertEqual(events, ["grab1", "ungrab1", "out1"])

        # Exhausted (0 credits): next capture pauses BEFORE grabbing or allocating
        out2 = ctrl.capture_frame(
            grab_fn=lambda: events.append("grab2_forbidden"),
            alloc_pair_fn=lambda s: (events.append("alloc2_forbidden"), b"")[1],
            ungrab_fn=lambda: True,
        )
        self.assertTrue(out2.paused)
        self.assertIsNone(out2.seq)
        self.assertEqual(events, ["grab1", "ungrab1", "out1"])

        # Stream bank still holds intact frame 1 evidence
        read_ref = self.bank.read(ctrl.stream_token)
        self.assertIsInstance(read_ref, wsb.FrameRef)
        self.assertEqual(read_ref.frame_seq, 0)

        # Complete consumer lease and ACK seq 1 -> grants cumulative 2 (1 new credit)
        ctrl.complete_consumer_lease(1)
        avail = ctrl.apply_ack(1, 2)
        self.assertEqual(avail, 1)
        self.assertFalse(ctrl.paused)

    def test_must_fail_ack_inflation_unsent_ack_regression_and_duplicate_spend(self):
        """Must-FAIL fixture: initial overgrant, capacity inflation, unsent ACK, conflicting duplicate, regression, and duplicate spend raise E_CREDIT without state mutation."""
        ctrl = self._new_controller()
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=2))
        ctrl.reserve_host_capacity()

        # 1. Initial overgrant (3 > min(2, 2)) raises E_CREDIT
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.apply_ack(0, 3)
        self.assertEqual(ctx.exception.code, "E_CREDIT")
        self.assertFalse(ctrl.initial_ack_done)
        self.assertEqual(ctrl.available_credits, 0)

        # Valid initial ACK (0, 2)
        ctrl.issue_initial_ack()
        self.assertEqual(ctrl.available_credits, 2)

        # 2. Identical duplicate initial ACK (0, 2) is idempotent (does not inflate budget)
        self.assertEqual(ctrl.apply_ack(0, 2), 2)
        self.assertEqual(ctrl.cumulative_credit_total, 2)

        # 3. Conflicting duplicate ACK (0, 1) or (0, 3) for already-acked seq 0 raises E_CREDIT
        for bad_total in (1, 3):
            with self.assertRaises(wc.W8UCreditError) as ctx:
                ctrl.apply_ack(0, bad_total)
            self.assertEqual(ctx.exception.code, "E_CREDIT")
            self.assertEqual(ctrl.cumulative_credit_total, 2)

        # 4. Unsent acknowledgment (ack_seq=1 when sent_seq=0) raises E_CREDIT
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.apply_ack(1, 3)
        self.assertEqual(ctx.exception.code, "E_CREDIT")
        self.assertEqual(ctrl.highest_acked_seq, 0)
        self.assertEqual(ctrl.cumulative_credit_total, 2)

        # Capture seq 1
        ctrl.capture_frame(
            grab_fn=lambda: None,
            alloc_pair_fn=lambda s: build_valid_wire_frame(confirmed, self.contract, seq=s, seed=1),
            ungrab_fn=lambda: True,
        )
        self.assertEqual(ctrl.sent_seq, 1)
        self.assertEqual(ctrl.spent_total, 1)

        # 5. Duplicate spend for seq 1 raises E_CREDIT and does not change spent_total
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.spend_for_seq(1)
        self.assertEqual(ctx.exception.code, "E_CREDIT")
        self.assertEqual(ctrl.spent_total, 1)

        # 6. ACK before recording-consumer lease completion raises E_CREDIT
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.apply_ack(1, 3)
        self.assertEqual(ctx.exception.code, "E_CREDIT")
        self.assertEqual(ctrl.highest_acked_seq, 0)

        # Complete consumer lease for seq 1
        ctrl.complete_consumer_lease(1)

        # 7. Capacity inflation (ack_seq=1 + max_inflight=2 == 3; granting 4 > 3) raises E_CREDIT
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.apply_ack(1, 4)
        self.assertEqual(ctx.exception.code, "E_CREDIT")
        self.assertEqual(ctrl.highest_acked_seq, 0)
        self.assertEqual(ctrl.cumulative_credit_total, 2)

        # 8. u64 overflow in ACK raises E_CREDIT
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.apply_ack(1, wc.U64_MAX + 1)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Valid ACK(1, 3)
        self.assertEqual(ctrl.apply_ack(1, 3), 2)
        self.assertEqual(ctrl.highest_acked_seq, 1)

        # 9. Regression (ack_seq=0 < 1 or cumulative_credit_total=2 < 3) raises E_CREDIT
        for reg_seq, reg_tot in ((0, 3), (1, 2)):
            with self.assertRaises(wc.W8UCreditError) as ctx:
                ctrl.apply_ack(reg_seq, reg_tot)
            self.assertEqual(ctx.exception.code, "E_CREDIT")
            self.assertEqual(ctrl.highest_acked_seq, 1)
            self.assertEqual(ctrl.cumulative_credit_total, 3)

    def test_positive_control_cumulative_idempotent_bounded_credits(self):
        """Independent positive control: cumulative idempotent bounded credits across multi-frame cycle."""
        ctrl = self._new_controller()
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=3))
        ctrl.reserve_host_capacity()

        # Initial ACK grants min(2, 3) == 2
        self.assertEqual(ctrl.issue_initial_ack(), (0, 2))
        self.assertEqual(ctrl.available_credits, 2)

        # Capture frames 1 and 2, spending both initial credits
        for expected_seq in (1, 2):
            out = ctrl.capture_frame(
                grab_fn=lambda: None,
                alloc_pair_fn=lambda s: build_valid_wire_frame(
                    confirmed, self.contract, seq=s, wire_tick=s * 5, seed=s
                ),
                ungrab_fn=lambda: True,
            )
            self.assertFalse(out.paused)
            self.assertEqual(out.seq, expected_seq)

        self.assertEqual(ctrl.available_credits, 0)
        self.assertEqual(ctrl.spent_total, 2)

        # Complete recording consumer for 1 and 2, then send cumulative ACK(2, 5) (within 2 + 3 = 5)
        ctrl.complete_consumer_lease(1)
        ctrl.complete_consumer_lease(2)
        ack_wire = ctrl.encode_ack_wire(2, 5)
        self.assertEqual(ctrl.apply_ack_wire(ack_wire), 3)
        # Re-applying identical ACK wire is idempotent
        self.assertEqual(ctrl.apply_ack_wire(ack_wire), 3)
        self.assertEqual(ctrl.highest_acked_seq, 2)
        self.assertEqual(ctrl.cumulative_credit_total, 5)

    def test_grab_ungrab_order_and_stop_bypasses_full_queue(self):
        """Acquire credit before grab, complete checked ungrab before blocking output; STOP bypasses full queue."""
        ctrl = self._new_controller()
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=1))
        ctrl.reserve_host_capacity()
        ctrl.issue_initial_ack()

        order = []

        def grab():
            # Credit must already be spent before grab executes
            self.assertEqual(ctrl.spent_total, 1)
            self.assertTrue(ctrl.under_grab)
            order.append("grab")

        def alloc_pair(s):
            self.assertTrue(ctrl.under_grab)
            order.append("alloc")
            return build_valid_wire_frame(confirmed, self.contract, seq=s, seed=7)

        def ungrab():
            self.assertTrue(ctrl.under_grab)
            order.append("ungrab")
            return True

        def blocking_output(_wire):
            # Checked ungrab must already have completed before blocking output executes
            self.assertFalse(ctrl.under_grab)
            order.append("output")

        out = ctrl.capture_frame(
            grab_fn=grab,
            alloc_pair_fn=alloc_pair,
            ungrab_fn=ungrab,
            output_fn=blocking_output,
        )
        self.assertFalse(out.paused)
        self.assertEqual(order, ["grab", "alloc", "ungrab", "output"])
        self.assertEqual(ctrl.available_credits, 0)
        self.assertEqual(len(ctrl.outstanding_leases), 1)

        # Queue is now completely full (max_inflight_frames=1, 1 lease outstanding, 0 credits).
        # STOP bypasses the full queue immediately!
        stop_wire = ctrl.trigger_stop()
        self.assertTrue(ctrl.stopped)
        self.assertEqual(ctrl.status, "stopped")
        dec_stop = wc.decode_control_frame(
            stop_wire, contract=self.contract, expected_binding_sha256=confirmed.binding_sha256
        )
        self.assertEqual(dec_stop.kind, wc.CONTROL_KIND_STOP)
        self.assertEqual(dec_stop.body_bytes, 0)

        # Emit W8UE END and verify round-trip
        end_wire, end_meta = ctrl.emit_end("OK")
        self.assertTrue(end_meta["eof"])
        self.assertTrue(end_meta["joined_exit"])
        dec_end = wc.decode_control_frame(
            end_wire, contract=self.contract, expected_binding_sha256=confirmed.binding_sha256
        )
        self.assertEqual(dec_end.magic, wc.CONTROL_MAGIC_END)
        self.assertEqual(dec_end.payload["error_name"], "OK")
        self.assertEqual(dec_end.payload["sent"], 1)

    def test_backpressure_deadline_and_timeout_retained_charges(self):
        """Consumer deadline expiry raises E_BACKPRESSURE; capture timeout raises E_TIMEOUT with incomplete status and retained charges."""
        # 1. Consumer deadline expiry -> E_BACKPRESSURE
        ctrl = self._new_controller(consumer_timeout_ns=10_000)
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=2))
        ctrl.reserve_host_capacity()
        ctrl.issue_initial_ack()

        ctrl.capture_frame(
            grab_fn=lambda: None,
            alloc_pair_fn=lambda s: build_valid_wire_frame(confirmed, self.contract, seq=s, seed=1),
            ungrab_fn=lambda: True,
        )
        self.clock.advance_ns(10_001)
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.complete_consumer_lease(1)
        self.assertEqual(ctx.exception.code, "E_BACKPRESSURE")
        self.assertEqual(ctx.exception.status, "incomplete")
        self.assertEqual(ctrl.status, "incomplete")

        # 2. Capture/channel timeout with unproven unmap -> E_TIMEOUT, incomplete status, retained ambiguous charges
        pinned_rings = []

        def pinned_factory(_handle, spec):
            ring = _PinnedRing(rtring.ring_bytes(spec.slots, spec.width, spec.height), allow_close=False)
            pinned_rings.append(ring)
            return ring

        bank2 = wsb.WindowStreamBank(quota_bytes=16 * 1024 * 1024, ring_factory=pinned_factory)
        ctrl2 = self._new_controller(bank=bank2, capture_timeout_ns=5_000)
        confirmed2 = ctrl2.receive_hello(make_valid_binding_dict(max_inflight_frames=2))
        ctrl2.reserve_host_capacity()
        ctrl2.issue_initial_ack()

        with self.assertRaises(wc.W8UCreditError) as ctx2:
            ctrl2.capture_frame(
                grab_fn=lambda: None,
                alloc_pair_fn=lambda s: build_valid_wire_frame(confirmed2, self.contract, seq=s, seed=2),
                ungrab_fn=lambda: True,
                capture_duration_ns=6_000,
            )
        self.assertEqual(ctx2.exception.code, "E_TIMEOUT")
        self.assertEqual(ctx2.exception.status, "incomplete")
        self.assertGreater(ctx2.exception.retained_charges, 0)
        self.assertEqual(ctrl2.retained_ambiguous_charges, bank2.reserved_bytes)

        # Once pinned ring allows unmap, retry_cleanup frees the retained charge
        pinned_rings[0].allow_close = True
        self.assertEqual(bank2.retry_cleanup(), 1)
        self.assertEqual(bank2.reserved_bytes, 0)

    def test_deterministic_schedule_runner_injected_clock(self):
        """Deterministic schedule of pause, consume, complete, duplicate ACK, cancellation, and release."""
        pinned_rings = []

        def factory(_handle, spec):
            ring = _PinnedRing(rtring.ring_bytes(spec.slots, spec.width, spec.height), allow_close=True)
            pinned_rings.append(ring)
            return ring

        bank = wsb.WindowStreamBank(quota_bytes=16 * 1024 * 1024, ring_factory=factory)
        ctrl = self._new_controller(bank=bank, consumer_timeout_ns=100_000)
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=2))
        ctrl.reserve_host_capacity()

        steps = [
            {"op": "capture"},  # paused before initial ACK
            {"op": "initial_ack"},  # grants 2 credits
            {"op": "capture", "capture_duration_ns": 1_000},  # seq 1
            {"op": "capture", "capture_duration_ns": 1_000},  # seq 2
            {"op": "capture"},  # 0 credits -> paused
            {"op": "advance_clock", "delta_ns": 5_000},
            {"op": "complete", "seq": 1},
            {"op": "ack", "ack_seq": 1, "cumulative_credit_total": 3},  # unpauses, 1 credit available
            {"op": "duplicate_ack", "ack_seq": 1, "cumulative_credit_total": 3},  # idempotent
            {"op": "capture", "capture_duration_ns": 1_000},  # seq 3
            {"op": "complete", "seq": 2},
            {"op": "complete", "seq": 3},
            {"op": "ack", "ack_seq": 3, "cumulative_credit_total": 5},
            {"op": "cancel"},
            {"op": "release"},
        ]
        trace = ctrl.run_schedule(
            steps,
            grab_fn=lambda: None,
            alloc_pair_fn=lambda s: build_valid_wire_frame(confirmed, self.contract, seq=s, seed=s),
            ungrab_fn=lambda: True,
        )
        self.assertTrue(trace[0]["paused"])
        self.assertEqual(trace[1]["available_credits"], 2)
        self.assertEqual(trace[2]["seq"], 1)
        self.assertEqual(trace[3]["seq"], 2)
        self.assertTrue(trace[4]["paused"])
        self.assertEqual(trace[7]["available_credits"], 1)
        self.assertEqual(trace[8]["available_credits"], 1)  # duplicate ACK idempotent
        self.assertEqual(trace[9]["seq"], 3)
        self.assertTrue(trace[13]["stopped"])

    def test_wire_frame_xwd_subset_caps_hashes_and_tick_modes(self):
        """Verify W8UF wire format, caps, XWD subset vectors, canonical RGB24 comparison, and tick modes."""
        ctrl = self._new_controller()
        confirmed = ctrl.receive_hello(make_valid_binding_dict(width=4, height=2, tick_mode="committed"))

        # 1. Header differences encoding identical RGB24 pixels succeed
        wire_ok = build_valid_wire_frame(
            confirmed, self.contract, seq=1, wire_tick=0, capture_mono_ns=999_999, seed=42, header_diff_b=True
        )
        vf = wc.validate_wire_frame(wire_ok, binding=confirmed, expected_seq=1, contract=self.contract)
        self.assertEqual(vf.seq, 1)
        self.assertEqual(vf.tick, 0)  # committed zero is valid
        self.assertTrue(vf.tick_valid)
        self.assertEqual(vf.capture_mono_ns, 999_999)
        self.assertEqual(tuple(vf.metadata_dict().keys()), wc.FRAME_METADATA_KEYS)

        # 2. Torn pair (XWD A and XWD B have different RGB24 pixels) -> E_TORN
        rgb_a = make_rgb24_pattern(4, 2, 1)
        rgb_b = make_rgb24_pattern(4, 2, 2)
        xwd_a = wc.build_xwd_bytes(rgb_a, width=4, height=2, contract=self.contract)
        xwd_b = wc.build_xwd_bytes(rgb_b, width=4, height=2, contract=self.contract)
        digest_a = wc.compute_rgb24_sha256(rgb_a, self.contract)
        torn_wire = wc.encode_wire_frame(
            seq=1,
            wire_tick=1,
            tick_valid=True,
            pair_under_grab=True,
            capture_mono_ns=100,
            width=4,
            height=2,
            sha256_rgb24=digest_a,
            binding_sha256=confirmed.binding_sha256,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
        )
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_wire_frame(torn_wire, binding=confirmed, expected_seq=1, contract=self.contract)
        self.assertEqual(ctx.exception.code, "E_TORN")

        # 3. Advertised digest mismatch -> E_HASH
        bad_hash_wire = wc.encode_wire_frame(
            seq=1,
            wire_tick=1,
            tick_valid=True,
            pair_under_grab=True,
            capture_mono_ns=100,
            width=4,
            height=2,
            sha256_rgb24=b"\xee" * 32,
            binding_sha256=confirmed.binding_sha256,
            xwd_a=xwd_a,
            xwd_b=xwd_a,
        )
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_wire_frame(bad_hash_wire, binding=confirmed, expected_seq=1, contract=self.contract)
        self.assertEqual(ctx.exception.code, "E_HASH")

        # 4. Cross-language XWD rejected vectors: oversized_header -> E_LENGTH, invalid_bitmap_bit_order -> E_WIRE
        oversized_xwd = wc.build_xwd_bytes(
            rgb_a, width=4, height=2, contract=self.contract, extra_header_bytes=b"x" * 600
        )
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_and_convert_xwd(
                oversized_xwd, expected_width=4, expected_height=2, contract=self.contract
            )
        self.assertEqual(ctx.exception.code, "E_LENGTH")

        bad_bit_order_xwd = wc.build_xwd_bytes(
            rgb_a, width=4, height=2, contract=self.contract, bitmap_bit_order=0
        )
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_and_convert_xwd(
                bad_bit_order_xwd, expected_width=4, expected_height=2, contract=self.contract
            )
        self.assertEqual(ctx.exception.code, "E_WIRE")

        # 5. Native validator error translation
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_and_convert_xwd(
                xwd_a,
                expected_width=4,
                expected_height=2,
                contract=self.contract,
                native_validator=lambda _: "NAT_ERR_BAD_GEOMETRY",
            )
        self.assertEqual(ctx.exception.code, "E_GEOMETRY")

        # 6. Unavailable tick_mode: wire zero with tick_valid=False projects None; non-zero or tick_valid=True -> E_TICK
        ctrl_unavail = self._new_controller()
        conf_unavail = ctrl_unavail.receive_hello(
            make_valid_binding_dict(width=4, height=2, tick_mode="unavailable")
        )
        wire_unavail_ok = build_valid_wire_frame(conf_unavail, self.contract, seq=1, wire_tick=0, tick_valid=False)
        vf_u = wc.validate_wire_frame(
            wire_unavail_ok, binding=conf_unavail, expected_seq=1, contract=self.contract
        )
        self.assertIsNone(vf_u.tick)
        self.assertFalse(vf_u.tick_valid)

        wire_unavail_bad = wc.encode_wire_frame(
            seq=1,
            wire_tick=5,
            tick_valid=False,
            pair_under_grab=True,
            capture_mono_ns=100,
            width=4,
            height=2,
            sha256_rgb24=digest_a,
            binding_sha256=conf_unavail.binding_sha256,
            xwd_a=xwd_a,
            xwd_b=xwd_a,
        )
        with self.assertRaises(wc.W8UCreditError) as ctx:
            wc.validate_wire_frame(
                wire_unavail_bad, binding=conf_unavail, expected_seq=1, contract=self.contract
            )
        self.assertEqual(ctx.exception.code, "E_TICK")

    def test_receipt_projection_domains_purge_and_no_input_authority(self):
        """Verify w8u.receipt/v1 closed projection, domain separation, metadata_only purge, and input authority refusal."""
        ctrl = self._new_controller()
        confirmed = ctrl.receive_hello(make_valid_binding_dict(max_inflight_frames=2))
        ctrl.reserve_host_capacity()
        ctrl.issue_initial_ack()

        ctrl.capture_frame(
            grab_fn=lambda: None,
            alloc_pair_fn=lambda s: build_valid_wire_frame(confirmed, self.contract, seq=s, seed=5),
            ungrab_fn=lambda: True,
        )
        ctrl.complete_consumer_lease(1)
        ctrl.apply_ack(1, 2)

        # UI, simulation, and RGB24 hashes over identical input bytes are strictly distinct
        sample = b"identical-payload-bytes"
        h_ui = wc.compute_ui_sha256(sample, self.contract)
        h_sim = wc.compute_sim_sha256(sample, self.contract)
        h_rgb = wc.compute_rgb24_sha256(sample, self.contract).hex()
        self.assertEqual(len({h_ui, h_sim, h_rgb}), 3)

        # Admitted vs applied actions tracked separately
        ctrl.record_foreign_action("act-1", admitted=True, applied=True, ui_bytes=b"u1", sim_bytes=b"s1")
        ctrl.record_foreign_action("act-2", admitted=True, applied=False, ui_bytes=b"u2")

        # Capture controller grants no input authority
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.attempt_input_action({"type": "click", "x": 10, "y": 10})
        self.assertEqual(ctx.exception.code, "E_BINDING")

        ctrl.trigger_stop()
        ctrl.emit_end("OK")

        # Unknown fields remain None (never 0)
        receipt = ctrl.project_receipt(claim_pixel_replay=True)
        self.assertEqual(receipt["schema"], "w8u.receipt/v1")
        self.assertEqual(receipt["populations"]["actions_admitted"], 2)
        self.assertEqual(receipt["populations"]["actions_applied"], 1)
        self.assertIsNone(receipt["foreign"]["t2"])
        self.assertIsNone(receipt["foreign"]["state"])
        self.assertIsNone(receipt["evidence"]["listener"])
        self.assertIsNone(receipt["evidence"]["cleanup"])
        self.assertTrue(receipt["evidence"]["pixel_replay"])

        # After pixel purge, retention is metadata_only and cannot claim pixel replay
        ctrl.purge_pixel_retention()
        self.assertEqual(ctrl.retention_mode, "metadata_only")
        with self.assertRaises(wc.W8UCreditError) as ctx:
            ctrl.project_receipt(claim_pixel_replay=True)
        self.assertEqual(ctx.exception.code, "E_BINDING")
        r_meta = ctrl.project_receipt(claim_pixel_replay=False)
        self.assertEqual(r_meta["evidence"]["retention_mode"], "metadata_only")
        self.assertFalse(r_meta["evidence"]["pixel_replay"])

    def test_pure_stdlib_no_sleeps_and_no_sibling_w8u_imports(self):
        """Verify vadapt/w8u_credits.py has no sibling W8U imports, no top-level third-party imports, and no sleeps."""
        src_path = Path(wc.__file__).resolve()
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertIn(top, sys.stdlib_module_names | {"vadapt"})
                    self.assertNotIn("w8u", alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                self.assertIn(top, sys.stdlib_module_names | {"vadapt"})
                for alias in node.names:
                    self.assertIn(alias.name, {"rtring", "window_stream_bank"} if top == "vadapt" else {alias.name})
            elif isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, "sleep", "sleep calls are forbidden")


MANDATORY_CASES = (
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_contract_authority_ausente_and_pending_rejected",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_binding_canonical_json_roots_and_independent_grant_auth",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_hello_grants_no_credit_and_requires_capacity_reservation",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_single_frame_capacity_exhaustion_and_no_evidence_overwrite",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_must_fail_ack_inflation_unsent_ack_regression_and_duplicate_spend",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_positive_control_cumulative_idempotent_bounded_credits",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_grab_ungrab_order_and_stop_bypasses_full_queue",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_backpressure_deadline_and_timeout_retained_charges",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_deterministic_schedule_runner_injected_clock",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_wire_frame_xwd_subset_caps_hashes_and_tick_modes",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_receipt_projection_domains_purge_and_no_input_authority",
    "tests.test_w8u_ve_credits_unit.TestW8UVeCreditsUnit.test_pure_stdlib_no_sleeps_and_no_sibling_w8u_imports",
)


if __name__ == "__main__":
    unittest.main()
