"""Unit tests for production owner w8u-ve-memory (vadapt/w8u_memory.py).

Covers Gate 1 (Shared-memory accounting), Gate 2 (Storage law), and
Gate 3 (Purge settlement), plus wire/XWD/control/credit/binding/receipt
invariants in an offline-only, RAM-only environment.
"""

import ast
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from vadapt import ramws, rtring, w8u_memory as wm, window_stream_bank as wsb


MANDATORY_CASES = (
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_actual_rtring_bytes_and_separate_reservations",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_physical_shared_mappings_no_double_counting",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_telemetry_injection_absent_unknown_and_tighter_limit",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_overflow_bool_and_insufficient_headroom_fail_e_ram_quota",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_cleanup_holds_failed_unmap_reader_lease_unjoined_channel",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_ring_close_raising_preserves_ledger_and_settles_once",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_simultaneous_capsules_failed_staging_rollback_and_epoch_retention",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_contract_evidence_ausente_and_prepared_vs_accept",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_frame_wire_caps_canonical_rgb24_hashes_and_no_stale_success",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_xwd_subset_wrapper_checks_and_cross_language_vectors",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_controls_credit_flow_single_frame_duplicate_ack_and_stop_bypass",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_binding_canonical_json_grant_auth_and_confined_root",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_receipt_projection_domains_metadata_only_and_no_input_authority",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_must_fail_unmap_drops_charge_and_positive_control_budget_hold",
    "tests.test_w8u_ve_memory_unit.TestW8UVeMemoryUnit.test_offline_stdlib_and_storage_law_compliance",
)


def _make_accepted_contract() -> wm.ContractEvidence:
    w, h = 4, 2
    rgb = bytes((i * 17) & 0xFF for i in range(w * h * 3))
    valid_xwd = wm.build_synthetic_xwd(rgb, w, h, byte_order=0, bitmap_bit_order=0, bits_per_pixel=32, pixmap_depth=24)
    oversized_hdr_xwd = wm.build_synthetic_xwd(
        rgb, w, h, byte_order=0, bitmap_bit_order=0, header_extra_bytes=b"X" * 512
    )
    bad_bbo_xwd = wm.build_synthetic_xwd(rgb, w, h, byte_order=0, bitmap_bit_order=1)

    return wm.ContractEvidence(
        revision="rev-w8u-contract-001",
        accepted=True,
        binding_schema_id="w8u.binding/v1",
        action_result_schema_id="w8u.action-result/v1",
        receipt_schema_id=wm.RECEIPT_SCHEMA,
        receipt_closed_fields=wm.DEFAULT_RECEIPT_SECTIONS,
        control_combinations={
            "HELLO": {"magic": "W8UC", "version": 1, "kind": 1, "max_body_bytes": 16384},
            "HELLO_CONFIRM": {"magic": "W8UH", "version": 1, "kind": 1, "body_bytes": 0},
            "ACK": {"magic": "W8UC", "version": 1, "kind": 2, "body_bytes": 16},
            "STOP": {"magic": "W8UC", "version": 1, "kind": 3, "body_bytes": 0},
            "END": {"magic": "W8UE", "version": 1, "kind": 4, "body_bytes": 20},
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
            "unknown_code_refusal": True,
        },
        closed_error_numeric_map={
            code: idx for idx, code in enumerate(wm.CLOSED_ERROR_CODES, start=1)
        },
        digest_byte_domains={
            "rgb24": "raw_canonical_rgb24",
            "binding": "raw_canonical_binding_json",
            "ui": "w8u.ui.v1\\x00",
            "simulation": "w8u.simulation.v1\\x00",
        },
        xwd_subset={
            "byte_order": (0, 1),
            "bitmap_bit_order": (0,),
            "max_header_bytes": 256,
            "max_ncolors": 0,
            "max_colormap_entries": 256,
            "allowed_bpp": (24, 32),
            "allowed_depth": (24,),
            "stride_alignment": 4,
            "red_mask": 0x00FF0000,
            "green_mask": 0x0000FF00,
            "blue_mask": 0x000000FF,
        },
        native_error_translation={
            "NATIVE_ERR_TORN": "E_TORN",
            "NATIVE_ERR_HEADER_OVERSIZE": "E_LENGTH",
            "NATIVE_ERR_BIT_ORDER": "E_WIRE",
            "NATIVE_ERR_GEOM": "E_GEOMETRY",
        },
        cross_language_vectors={
            "accepted": [{"width": w, "height": h, "xwd": valid_xwd, "rgb24_sha256": hashlib.sha256(rgb).hexdigest()}],
            "rejected": {
                "oversized_header": {"width": w, "height": h, "xwd": oversized_hdr_xwd, "expected_code": "E_LENGTH"},
                "invalid_bitmap_bit_order": {"width": w, "height": h, "xwd": bad_bbo_xwd, "expected_code": "E_WIRE"},
            },
        },
        merged_production_owners=("w8u-ve-memory",),
    )


def _make_valid_binding(
    *,
    run_id: str = "run-01",
    canonical_id: str = "cap-01",
    generation: int = 0,
    epoch: int = 0,
    width: int = 4,
    height: int = 2,
    max_record_bytes: int = 65536,
    max_inflight_frames: int = 2,
    tick_mode: str = "committed",
) -> dict:
    return {
        "schema": "w8u.binding/v1",
        "run_id": run_id,
        "lease_id": "lease-01",
        "seat_grant_sha256": "a" * 64,
        "container_id": "ctr-01",
        "target_handle": {
            "kind": "capsule",
            "canonical_id": canonical_id,
            "generation": generation,
        },
        "epoch": epoch,
        "display": ":99",
        "xid": 12345,
        "width": width,
        "height": height,
        "frames_root": "/dev/shm/vqa-ramws",
        "frame_run_root": "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/%s" % run_id,
        "image_sha256": "b" * 64,
        "reader_sha256": "c" * 64,
        "exec_allowlist_sha256": "d" * 64,
        "max_record_bytes": max_record_bytes,
        "max_inflight_frames": max_inflight_frames,
        "tick_mode": tick_mode,
    }


def _healthy_telemetry(
    *,
    cgroup_current: int = 100 * wm.MIB,
    cgroup_peak: int | None = 120 * wm.MIB,
    cgroup_ceiling: int = 1024 * wm.MIB,
    host_avail: int = 2048 * wm.MIB,
    tmpfs_alloc: int = 64 * wm.MIB,
    swap_used: int = 0,
) -> wm.TelemetryObservation:
    return wm.TelemetryObservation(
        cgroup_current_bytes=cgroup_current,
        cgroup_peak_bytes=cgroup_peak,
        cgroup_ceiling_bytes=cgroup_ceiling,
        host_mem_available_bytes=host_avail,
        tmpfs_allocated_bytes=tmpfs_alloc,
        swap_used_bytes=swap_used,
    )


class TestW8UVeMemoryUnit(unittest.TestCase):
    """Comprehensive unit tests for w8u-ve-memory."""

    def test_actual_rtring_bytes_and_separate_reservations(self):
        slots, width, height = 4, 64, 32
        expected_ring = rtring.ring_bytes(slots, width, height)
        self.assertEqual(rtring.data_off(slots), 4096)
        self.assertEqual(wm.actual_ring_bytes(slots, width, height), expected_ring)

        spec = wm.ReservationSpec(
            slots=slots,
            width=width,
            height=height,
            max_record_bytes=65536,
            max_inflight_frames=2,
            decoding_copy_frames=2,
            consumer_view_slots=3,
            control_buffer_bytes=20480,
        )
        breakdown = spec.compute_breakdown()
        rgb_bytes = width * height * 3
        self.assertEqual(breakdown["ring_storage"], expected_ring)
        self.assertEqual(breakdown["pending_raw_pair"], 2 * 65536)
        self.assertEqual(breakdown["canonical_decoding_copies"], 2 * rgb_bytes)
        self.assertEqual(breakdown["held_consumer_views"], 3 * rgb_bytes)
        self.assertEqual(breakdown["control_buffers"], 20480)
        self.assertEqual(
            breakdown["total_ownership_bytes"],
            expected_ring + 2 * 65536 + 2 * rgb_bytes + 3 * rgb_bytes + 20480,
        )

        acc = wm.HostAdmissionAccountant(
            quota_bytes=64 * wm.MIB,
            telemetry_source=lambda: _healthy_telemetry(),
        )
        handle = wsb.Handle("capsule", "cap-sep", 0)
        tok = acc.admit(handle, spec)
        self.assertEqual(acc.ownership_reserved_bytes, breakdown["total_ownership_bytes"])
        self.assertEqual(acc.category_totals(), {k: breakdown[k] for k in wm.RESERVATION_CATEGORIES})
        acc.witness_channel_joined(tok, "ctrl")
        self.assertTrue(acc.close_claim(tok))
        self.assertEqual(acc.ownership_reserved_bytes, 0)

    def test_physical_shared_mappings_no_double_counting(self):
        spec = wm.ReservationSpec(
            slots=2,
            width=32,
            height=16,
            max_record_bytes=32768,
            max_inflight_frames=2,
            decoding_copy_frames=2,
            consumer_view_slots=2,
            control_buffer_bytes=20480,
            views_share_ring_mapping=True,
        )
        breakdown = spec.compute_breakdown()
        acc = wm.HostAdmissionAccountant(
            quota_bytes=64 * wm.MIB,
            telemetry_source=lambda: _healthy_telemetry(),
        )
        tok = acc.admit(wsb.Handle("capsule", "cap-map", 0), spec)
        ring_b = breakdown["ring_storage"]
        self.assertEqual(acc.physical_shared_bytes, ring_b)
        expected_phys = (
            ring_b
            + breakdown["pending_raw_pair"]
            + breakdown["canonical_decoding_copies"]
            + breakdown["control_buffers"]
        )
        self.assertEqual(acc.physical_total_bytes, expected_phys)

        # Attaching multiple readers and zero-copy consumer leases must NOT double-count physical mappings
        acc.attach_reader(tok, "reader-1")
        acc.attach_reader(tok, "reader-2")
        acc.acquire_consumer_lease(tok, "lease-view-1", frame_seq=1)
        acc.acquire_consumer_lease(tok, "lease-view-2", frame_seq=2)
        self.assertEqual(acc.physical_shared_bytes, ring_b)
        self.assertEqual(acc.physical_total_bytes, expected_phys)
        self.assertEqual(acc.ownership_reserved_bytes, breakdown["total_ownership_bytes"])

    def test_telemetry_injection_absent_unknown_and_tighter_limit(self):
        spec = wm.ReservationSpec(
            slots=2,
            width=32,
            height=32,
            max_record_bytes=65536,
            max_inflight_frames=2,
        )
        needed = spec.compute_breakdown()["total_ownership_bytes"]
        handle = wsb.Handle("capsule", "cap-tel", 0)

        # Absent cgroup current or ceiling is unknown (None, never 0) and refuses admission
        for bad_obs in (
            _healthy_telemetry(cgroup_current=None),
            _healthy_telemetry(cgroup_ceiling=None),
            _healthy_telemetry(host_avail=None),
            _healthy_telemetry(tmpfs_alloc=None),
            _healthy_telemetry(swap_used=None),
        ):
            acc = wm.HostAdmissionAccountant(quota_bytes=64 * wm.MIB, telemetry_source=lambda obs=bad_obs: obs)
            with self.assertRaises(wm.W8UMemoryError) as ctx:
                acc.admit(handle, spec)
            self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")
            self.assertEqual(acc.ownership_reserved_bytes, 0)

        # Case A: cgroup slice is tighter than host MemAvailable and refuses admission
        tight_slice = _healthy_telemetry(
            cgroup_current=100 * wm.MIB,
            cgroup_peak=100 * wm.MIB,
            cgroup_ceiling=100 * wm.MIB + (needed - 1),
            host_avail=2048 * wm.MIB,
        )
        acc_slice = wm.HostAdmissionAccountant(quota_bytes=64 * wm.MIB, telemetry_source=lambda: tight_slice)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc_slice.admit(handle, spec)
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")
        self.assertIn("cgroup_slice", str(ctx.exception))
        self.assertEqual(acc_slice.ownership_reserved_bytes, 0)

        # Case B: host MemAvailable is tighter than cgroup slice and refuses admission
        tight_host = _healthy_telemetry(
            cgroup_current=100 * wm.MIB,
            cgroup_peak=100 * wm.MIB,
            cgroup_ceiling=1024 * wm.MIB,
            host_avail=needed - 1,
        )
        acc_host = wm.HostAdmissionAccountant(quota_bytes=64 * wm.MIB, telemetry_source=lambda: tight_host)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc_host.admit(handle, spec)
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")
        self.assertIn("host_mem_available", str(ctx.exception))
        self.assertEqual(acc_host.ownership_reserved_bytes, 0)

    def test_overflow_bool_and_insufficient_headroom_fail_e_ram_quota(self):
        # Bool inputs rejected with E_RAM_QUOTA
        for bad_kwargs in (
            {"slots": True, "width": 16, "height": 16, "max_record_bytes": 4096, "max_inflight_frames": 1},
            {"slots": 2, "width": False, "height": 16, "max_record_bytes": 4096, "max_inflight_frames": 1},
            {"slots": 2, "width": 16, "height": 16, "max_record_bytes": True, "max_inflight_frames": 1},
            {"slots": 2, "width": 16, "height": 16, "max_record_bytes": 4096, "max_inflight_frames": True},
        ):
            with self.assertRaises(wm.W8UMemoryError) as ctx:
                wm.ReservationSpec(**bad_kwargs).compute_breakdown()
            self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

        # Geometry / ring multiplication overflow rejected with E_RAM_QUOTA
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.actual_ring_bytes((1 << 32) - 1, (1 << 32) - 1, (1 << 32) - 1)
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.geometry_record_cap((1 << 32) - 1, (1 << 32) - 1)
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

        # Sum overflow across reservation categories rejected with E_RAM_QUOTA
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.ReservationSpec(
                slots=4,
                width=64,
                height=64,
                max_record_bytes=65536,
                max_inflight_frames=2,
                control_buffer_bytes=(1 << 64) - 64,
            ).compute_breakdown()
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

        # Bool in telemetry rejected with E_RAM_QUOTA
        bad_tel = _healthy_telemetry(cgroup_current=True)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.validate_telemetry_for_admission(bad_tel)
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

    def test_cleanup_holds_failed_unmap_reader_lease_unjoined_channel(self):
        spec = wm.ReservationSpec(
            slots=2,
            width=16,
            height=16,
            max_record_bytes=16384,
            max_inflight_frames=2,
        )
        charge = spec.compute_breakdown()["total_ownership_bytes"]
        acc = wm.HostAdmissionAccountant(
            quota_bytes=64 * wm.MIB,
            telemetry_source=lambda: _healthy_telemetry(),
            ring_factory=lambda h, ep, sz: wm.FakeRingBuffer(sz, fail_close_times=1),
        )
        tok = acc.admit(wsb.Handle("capsule", "cap-hold", 0), spec, channel_ids=("ctrl-ch",))
        acc.attach_reader(tok, "r-1")
        acc.acquire_consumer_lease(tok, "lease-1", frame_seq=1)
        acc.cancel_channel(tok, "ctrl-ch")

        # Initiate close: reader, lease, and unjoined channel all block settlement
        self.assertFalse(acc.close_claim(tok))
        self.assertEqual(acc.ownership_reserved_bytes, charge)
        self.assertEqual(acc.retry_cleanup(), 0)
        self.assertEqual(acc.ownership_reserved_bytes, charge)

        # 1. Reader exits -> still held by lease + unjoined channel
        self.assertFalse(acc.witness_reader_exit(tok, "r-1"))
        self.assertEqual(acc.ownership_reserved_bytes, charge)

        # 2. Lease released -> still held by cancelled-but-unjoined channel
        self.assertFalse(acc.release_consumer_lease(tok, "lease-1", frame_seq=1))
        self.assertEqual(acc.ownership_reserved_bytes, charge)

        # 3. Channel joined -> attempts ring close, which fails unmap on first try (fail_close_times=1)
        self.assertFalse(acc.witness_channel_joined(tok, "ctrl-ch", eof_seen=True, exit_joined=True))
        self.assertEqual(acc.ownership_reserved_bytes, charge)

        # 4. Retry cleanup succeeds now that unmap succeeds -> settles to 0
        self.assertEqual(acc.retry_cleanup(), 1)
        self.assertEqual(acc.ownership_reserved_bytes, 0)
        self.assertEqual(acc.physical_shared_bytes, 0)

    def test_ring_close_raising_preserves_ledger_and_settles_once(self):
        spec = wm.ReservationSpec(
            slots=2,
            width=16,
            height=16,
            max_record_bytes=16384,
            max_inflight_frames=2,
        )
        charge = spec.compute_breakdown()["total_ownership_bytes"]
        ring_buf = wm.FakeRingBuffer(
            spec.compute_breakdown()["ring_storage"],
            fail_close_times=1,
            raise_on_close=RuntimeError("simulated unmap fault"),
        )
        acc = wm.HostAdmissionAccountant(
            quota_bytes=64 * wm.MIB,
            telemetry_source=lambda: _healthy_telemetry(),
            ring_factory=lambda h, ep, sz: ring_buf,
        )
        tok = acc.admit(wsb.Handle("capsule", "cap-raise", 0), spec, channel_ids=())

        # close_claim propagates RuntimeError from ring.close() but MUST NOT erase the ledger
        with self.assertRaises(RuntimeError):
            acc.close_claim(tok)
        self.assertEqual(acc.ownership_reserved_bytes, charge)
        self.assertEqual(acc.physical_shared_bytes, spec.compute_breakdown()["ring_storage"])

        # Subsequent retry_cleanup settles exactly once
        self.assertEqual(acc.retry_cleanup(), 1)
        self.assertEqual(acc.ownership_reserved_bytes, 0)
        self.assertEqual(acc.physical_shared_bytes, 0)

        # Repeated retry_cleanup calls must be idempotent (0 freed, never negative)
        self.assertEqual(acc.retry_cleanup(), 0)
        self.assertEqual(acc.ownership_reserved_bytes, 0)

    def test_simultaneous_capsules_failed_staging_rollback_and_epoch_retention(self):
        spec_small = wm.ReservationSpec(
            slots=2,
            width=16,
            height=16,
            max_record_bytes=16384,
            max_inflight_frames=2,
        )
        spec_large = wm.ReservationSpec(
            slots=4,
            width=32,
            height=32,
            max_record_bytes=32768,
            max_inflight_frames=2,
        )
        charge_small = spec_small.compute_breakdown()["total_ownership_bytes"]
        charge_large = spec_large.compute_breakdown()["total_ownership_bytes"]

        rings = {}

        def factory(h, ep, sz):
            if h.canonical_id == "cap-fail":
                raise RuntimeError("factory staging failure")
            rb = wm.FakeRingBuffer(sz, fail_close_times=(1 if (h.canonical_id == "cap-a" and ep == 0) else 0))
            rings[(h.canonical_id, ep)] = rb
            return rb

        acc = wm.HostAdmissionAccountant(
            quota_bytes=64 * wm.MIB,
            telemetry_source=lambda: _healthy_telemetry(),
            ring_factory=factory,
        )

        # 1. Admit two simultaneous capsules with separate claims
        h_a = wsb.Handle("capsule", "cap-a", 0)
        h_b = wsb.Handle("capsule", "cap-b", 0)
        tok_a, tok_b = acc.admit_many(((h_a, spec_small, ()), (h_b, spec_small, ())))
        self.assertEqual(acc.ownership_reserved_bytes, 2 * charge_small)

        # 2. Failed staging rollback on a new batch (cap-c succeeds staging, cap-fail raises)
        h_c = wsb.Handle("capsule", "cap-c", 0)
        h_fail = wsb.Handle("capsule", "cap-fail", 0)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.admit_many(((h_c, spec_small, ()), (h_fail, spec_small, ())))
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")
        # Both cap-c (which closed cleanly during rollback) and cap-fail (uncreated) rolled back;
        # existing cap-a and cap-b claims remain intact!
        self.assertEqual(acc.ownership_reserved_bytes, 2 * charge_small)

        # 3. Resize cap-a across epoch change while old epoch 0 ring fails immediate unmap
        tok_a1 = acc.resize(tok_a, spec_large, channel_ids=())
        self.assertEqual(tok_a1.epoch, 1)
        # Old epoch 0 of cap-a is retained alongside new epoch 1 of cap-a and epoch 0 of cap-b!
        self.assertEqual(acc.ownership_reserved_bytes, charge_small + charge_large + charge_small)

        # Retry cleanup settles old epoch 0 of cap-a while leaving epoch 1 of cap-a and cap-b active
        self.assertEqual(acc.retry_cleanup(), 1)
        self.assertEqual(acc.ownership_reserved_bytes, charge_large + charge_small)

        # Closing cap-b leaves only cap-a epoch 1
        self.assertTrue(acc.close_claim(tok_b))
        self.assertEqual(acc.ownership_reserved_bytes, charge_large)

    def test_contract_evidence_ausente_and_prepared_vs_accept(self):
        # Missing contract is PREPARED and reports AUSENTE
        rep_none = wm.inspect_contract_evidence(None)
        self.assertEqual(rep_none["status"], wm.PREPARED)
        self.assertFalse(rep_none["ready"])
        self.assertEqual(rep_none["missing"]["revision"], wm.AUSENTE)

        # Pending contract (accepted=False) is PREPARED, never ACCEPT
        pending = wm.ContractEvidence(revision="r1", accepted=False)
        rep_pend = wm.inspect_contract_evidence(pending)
        self.assertEqual(rep_pend["status"], wm.PREPARED)
        self.assertEqual(rep_pend["missing"]["accepted"], wm.AUSENTE)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.require_accepted_contract(pending)
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Complete accepted contract is ACCEPT and translates native errors
        contract = _make_accepted_contract()
        rep_ok = wm.inspect_contract_evidence(contract)
        self.assertEqual(rep_ok["status"], wm.ACCEPT)
        self.assertTrue(rep_ok["ready"])
        self.assertEqual(wm.translate_native_error("NATIVE_ERR_TORN", contract), "E_TORN")
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.translate_native_error("UNKNOWN_NATIVE_ERROR", contract)
        self.assertEqual(ctx.exception.code, "E_WIRE")

    def test_frame_wire_caps_canonical_rgb24_hashes_and_no_stale_success(self):
        contract = _make_accepted_contract()
        binding_dict = _make_valid_binding(width=4, height=2, max_record_bytes=65536, tick_mode="committed")
        binding, _, binding_sha256 = wm.validate_binding(
            binding_dict, contract=contract, grant_authenticator=lambda b: True, check_fs_symlinks=False
        )
        w, h = 4, 2
        rgb = bytes((i * 29) & 0xFF for i in range(w * h * 3))
        rgb_sha = hashlib.sha256(rgb).digest()

        # XWD A and XWD B with different headers and different BGRX padding byte (0x00 vs 0xFF)
        # still encode identical canonical RGB24 pixels and must succeed!
        xwd_a = wm.build_synthetic_xwd(rgb, w, h, header_extra_bytes=b"winA\x00\x00\x00\x00", pad_byte=0x00)
        xwd_b = wm.build_synthetic_xwd(rgb, w, h, header_extra_bytes=b"windowB_diff", pad_byte=0xFF)

        frame_ok = wm.pack_frame_record(
            seq=1,
            tick=0,  # committed zero is valid and projects 0
            capture_mono_ns=987654321,
            width=w,
            height=h,
            flags=wm.FLAG_PAIR_UNDER_GRAB | wm.FLAG_TICK_VALID,
            sha256_rgb24=rgb_sha,
            binding_sha256=binding_sha256,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
        )
        meta, decoded_rgb = wm.validate_frame_record(
            frame_ok, binding=binding, binding_sha256=binding_sha256, expected_seq=1, contract=contract
        )
        self.assertEqual(decoded_rgb, rgb)
        self.assertEqual(meta["tick"], 0)
        self.assertTrue(meta["tick_valid"])
        self.assertEqual(meta["capture_mono_ns"], 987654321)
        self.assertEqual(tuple(meta.keys()), wm.FRAME_METADATA_KEYS)

        # Unavailable tick_mode with wire tick=0 and tick_valid=False projects tick=None
        b_unavail, _, b_unavail_sha = wm.validate_binding(
            _make_valid_binding(width=w, height=h, tick_mode="unavailable"),
            contract=contract,
            grant_authenticator=lambda b: True,
            check_fs_symlinks=False,
        )
        frame_unavail = wm.pack_frame_record(
            seq=1,
            tick=0,
            capture_mono_ns=555,
            width=w,
            height=h,
            flags=wm.FLAG_PAIR_UNDER_GRAB,
            sha256_rgb24=rgb_sha,
            binding_sha256=b_unavail_sha,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
        )
        meta_u, _ = wm.validate_frame_record(
            frame_unavail, binding=b_unavail, binding_sha256=b_unavail_sha, expected_seq=1, contract=contract
        )
        self.assertIsNone(meta_u["tick"])
        self.assertFalse(meta_u["tick_valid"])

        # Mismatched pair pixels -> E_TORN (and never returns stale last-good frame)
        rgb_alt = bytes(((i * 29) ^ 1) & 0xFF for i in range(w * h * 3))
        xwd_b_diff = wm.build_synthetic_xwd(rgb_alt, w, h)
        frame_torn = wm.pack_frame_record(
            seq=2,
            tick=1,
            capture_mono_ns=1000,
            width=w,
            height=h,
            flags=wm.FLAG_PAIR_UNDER_GRAB | wm.FLAG_TICK_VALID,
            sha256_rgb24=rgb_sha,
            binding_sha256=binding_sha256,
            xwd_a=xwd_a,
            xwd_b=xwd_b_diff,
        )
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.validate_frame_record(
                frame_torn, binding=binding, binding_sha256=binding_sha256, expected_seq=2, contract=contract
            )
        self.assertEqual(ctx.exception.code, "E_TORN")

        # Advertised raw BGRX hash instead of canonical RGB24 hash -> E_HASH
        frame_bad_hash = wm.pack_frame_record(
            seq=2,
            tick=1,
            capture_mono_ns=1000,
            width=w,
            height=h,
            flags=wm.FLAG_PAIR_UNDER_GRAB | wm.FLAG_TICK_VALID,
            sha256_rgb24=hashlib.sha256(xwd_a).digest(),
            binding_sha256=binding_sha256,
            xwd_a=xwd_a,
            xwd_b=xwd_b,
        )
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.validate_frame_record(
                frame_bad_hash, binding=binding, binding_sha256=binding_sha256, expected_seq=2, contract=contract
            )
        self.assertEqual(ctx.exception.code, "E_HASH")

    def test_xwd_subset_wrapper_checks_and_cross_language_vectors(self):
        contract = _make_accepted_contract()
        vecs = contract.cross_language_vectors

        # Accepted cross-language vector
        for acc_vec in vecs["accepted"]:
            out_rgb = wm.validate_and_convert_xwd(
                acc_vec["xwd"],
                expected_width=acc_vec["width"],
                expected_height=acc_vec["height"],
                contract=contract,
            )
            self.assertEqual(hashlib.sha256(out_rgb).hexdigest(), acc_vec["rgb24_sha256"])

        # Rejected cross-language vectors: oversized_header and invalid_bitmap_bit_order
        for name in ("oversized_header", "invalid_bitmap_bit_order"):
            rej = vecs["rejected"][name]
            with self.assertRaises(wm.W8UMemoryError) as ctx:
                wm.validate_and_convert_xwd(
                    rej["xwd"],
                    expected_width=rej["width"],
                    expected_height=rej["height"],
                    contract=contract,
                )
            self.assertEqual(ctx.exception.code, rej["expected_code"], name)

    def test_controls_credit_flow_single_frame_duplicate_ack_and_stop_bypass(self):
        contract = _make_accepted_contract()
        binding_dict = _make_valid_binding(max_inflight_frames=1)
        _, raw_binding, binding_sha256 = wm.validate_binding(
            binding_dict, contract=contract, grant_authenticator=lambda b: True, check_fs_symlinks=False
        )

        # HELLO control frame parses and grants 0 credit
        hello_wire = wm.pack_control_frame(wm.CONTROL_MAGIC_CLIENT, wm.CONTROL_KIND_HELLO, binding_sha256, raw_binding)
        hello_parsed = wm.parse_control_frame(hello_wire, expected_binding_sha256=binding_sha256, contract=contract)
        self.assertEqual(hello_parsed["type"], "HELLO")
        self.assertEqual(hello_parsed["credit_granted"], 0)

        # W8UH confirmation and W8UE END (normal completion & unknown error refusal)
        w8uh_wire = wm.pack_control_frame(wm.CONTROL_MAGIC_HELLO_ACK, 1, binding_sha256, b"")
        self.assertEqual(
            wm.parse_control_frame(w8uh_wire, expected_binding_sha256=binding_sha256, contract=contract)["type"],
            "HELLO_CONFIRM",
        )
        end_ok_wire = wm.pack_control_frame(
            wm.CONTROL_MAGIC_END, 4, binding_sha256, wm._END_BODY_STRUCT.pack(1, 1, 0)
        )
        end_parsed = wm.parse_control_frame(end_ok_wire, expected_binding_sha256=binding_sha256, contract=contract)
        self.assertEqual(end_parsed["error_name"], "OK")

        end_unknown_wire = wm.pack_control_frame(
            wm.CONTROL_MAGIC_END, 4, binding_sha256, wm._END_BODY_STRUCT.pack(1, 1, 9999)
        )
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.parse_control_frame(end_unknown_wire, expected_binding_sha256=binding_sha256, contract=contract)
        self.assertEqual(ctx.exception.code, "E_WIRE")

        # Single-frame capacity (max_inflight_frames=1): initial ACK grants min(2, 1) = 1; grant=2 fails E_CREDIT
        spec_1 = wm.ReservationSpec(
            slots=1, width=4, height=2, max_record_bytes=65536, max_inflight_frames=1
        )
        acc = wm.HostAdmissionAccountant(
            quota_bytes=16 * wm.MIB, telemetry_source=lambda: _healthy_telemetry()
        )
        tok = acc.admit(wsb.Handle("capsule", "cap-cred1", 0), spec_1, channel_ids=())
        self.assertEqual(acc.receive_hello(tok), 0)
        self.assertEqual(acc.available_credit(tok), 0)

        # Grab before initial ACK fails E_CREDIT
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.acquire_credit_and_grab(tok, 1)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Initial overgrant (2 > 1) fails E_CREDIT
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.send_initial_ack(tok, grant=2)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        init_ack = acc.send_initial_ack(tok)
        self.assertEqual(init_ack, {"ack_seq": 0, "cumulative_credit_total": 1})
        self.assertEqual(acc.available_credit(tok), 1)

        # Identical duplicate ACK is idempotent
        dup_ack = acc.apply_ack(tok, 0, 1)
        self.assertTrue(dup_ack["duplicate"])
        self.assertEqual(acc.available_credit(tok), 1)

        # Acquire credit -> grab seq 1 -> credit now exhausted (0 available)
        acc.acquire_credit_and_grab(tok, 1)
        self.assertEqual(acc.available_credit(tok), 0)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.acquire_credit_and_grab(tok, 2)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Publication before checked ungrab fails E_TORN
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.record_validated_publication(tok, 1)
        self.assertEqual(ctx.exception.code, "E_TORN")

        acc.complete_checked_ungrab(tok, 1, ungrab_ok=True)

        # ACK before validated publication fails E_CREDIT
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.apply_ack(tok, 1, 2)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        acc.acquire_consumer_lease(tok, "rec-lease-1", frame_seq=1)
        acc.record_validated_publication(tok, 1)

        # ACK while recording-consumer lease is still held fails E_CREDIT
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.apply_ack(tok, 1, 2)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        acc.release_consumer_lease(tok, "rec-lease-1", frame_seq=1)

        # Capacity inflation (granting 3 when ack_seq=1 + capacity=1 == 2) fails E_CREDIT
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            acc.apply_ack(tok, 1, 3)
        self.assertEqual(ctx.exception.code, "E_CREDIT")

        # Valid ACK after publication and lease release succeeds
        ack_1 = acc.apply_ack(tok, 1, 2)
        self.assertEqual(ack_1["available_credit"], 1)

        # STOP bypasses full queues
        pipe = wm.FakeChannelPipe("ctrl", max_queue_depth=2)
        pipe.send({"type": "ACK", "seq": 1})
        pipe.send({"type": "ACK", "seq": 2})
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            pipe.send({"type": "ACK", "seq": 3})
        self.assertEqual(ctx.exception.code, "E_BACKPRESSURE")
        self.assertTrue(pipe.send({"type": "STOP"}))
        self.assertEqual(pipe.queue[0]["type"], "STOP")

    def test_binding_canonical_json_grant_auth_and_confined_root(self):
        contract = _make_accepted_contract()
        valid_b = _make_valid_binding()
        _, raw_b, _ = wm.validate_binding(
            valid_b,
            contract=contract,
            grant_authenticator=lambda b: True,
            expected_target_handle=wsb.Handle("capsule", "cap-01", 0),
            expected_epoch=0,
            expected_geometry=(4, 2),
            check_fs_symlinks=False,
        )
        self.assertNotIn(b"\n", raw_b)

        # Non-canonical JSON with newline or whitespace fails E_BINDING
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.parse_canonical_json_bytes(raw_b + b"\n")
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Bool in integer field fails E_BINDING
        bad_bool_b = dict(valid_b, epoch=True)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.validate_binding(bad_bool_b, contract=contract, grant_authenticator=lambda b: True, check_fs_symlinks=False)
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Independent grant authentication failure fails E_BINDING even when digest is valid
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.validate_binding(raw_b, contract=contract, grant_authenticator=lambda b: False, check_fs_symlinks=False)
        self.assertEqual(ctx.exception.code, "E_BINDING")

        # Root confinement checks: wrong root, wrong child, or traversal fail E_ROOT
        for bad_root, bad_child in (
            ("/tmp/vqa-ramws", "/tmp/vqa-ramws/opensuperficies/os-cu-0/run-01"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/other/os-cu-0/run-01"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/../run-01"),
            ("/dev/shm/vqa-ramws", "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-01/"),
        ):
            b_bad = dict(valid_b, frames_root=bad_root, frame_run_root=bad_child)
            with self.assertRaises(wm.W8UMemoryError) as ctx:
                wm.validate_binding(b_bad, contract=contract, grant_authenticator=lambda b: True, check_fs_symlinks=False)
            self.assertEqual(ctx.exception.code, "E_ROOT")

    def test_confined_run_root_matches_exact_direct_child(self):
        contract = _make_accepted_contract()
        for run_id in ("w8u-vec-0001", "run-01", "run"):
            with self.subTest(run_id=run_id):
                binding = _make_valid_binding(run_id=run_id)
                validated, _, _ = wm.validate_binding(
                    binding, contract=contract, grant_authenticator=lambda b: True,
                )
                self.assertEqual(validated["frame_run_root"],
                                 "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/" + run_id)

    def test_confined_run_root_refuses_legacy_nested_and_mismatched_paths(self):
        prefix = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/"
        contract = _make_accepted_contract()
        for run_id, frame_run_root in (
            ("w8u-vec-0001", prefix + "run/w8u-vec-0001"),
            ("w8u-vec-0001", prefix + "w8u-vec-0001/nested"),
            ("nested/run", prefix + "nested/run"),
            ("w8u-vec-0001", "/dev/shm/vqa-ramws/opensuperficies/os-cu-1/w8u-vec-0001"),
            ("run-01", prefix + "run"),
            ("w8u-vec-0001", prefix + "other"),
            (".", prefix + "."),
            ("..", prefix + ".."),
            ("run\x00bad", prefix + "run\x00bad"),
        ):
            with self.subTest(run_id=run_id, frame_run_root=frame_run_root):
                binding = _make_valid_binding(run_id=run_id)
                binding["frame_run_root"] = frame_run_root
                with self.assertRaises(wm.W8UMemoryError) as ctx:
                    wm.validate_binding(binding, contract=contract, grant_authenticator=lambda b: True)
                self.assertEqual(ctx.exception.code, "E_ROOT")

    def test_confined_run_root_refuses_invalid_run_id_components(self):
        prefix = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/"
        for run_id in ("", ".", "..", "nested/run", "run\x00bad", "run\n"):
            with self.subTest(run_id=run_id), self.assertRaises(wm.W8UMemoryError) as ctx:
                wm.validate_confined_roots("/dev/shm/vqa-ramws", prefix + run_id, run_id)
            self.assertEqual(ctx.exception.code, "E_ROOT")

    def test_confined_run_root_refuses_symlink_in_any_component(self):
        binding = _make_valid_binding(run_id="w8u-vec-0001")
        contract = _make_accepted_contract()
        for linked_path in (binding["frames_root"],
                            "/dev/shm/vqa-ramws/opensuperficies/os-cu-0",
                            binding["frame_run_root"]):
            with self.subTest(linked_path=linked_path), patch.object(
                Path, "is_symlink", autospec=True,
                side_effect=lambda path: str(path) == linked_path,
            ):
                with self.assertRaises(wm.W8UMemoryError) as ctx:
                    wm.validate_binding(binding, contract=contract, grant_authenticator=lambda b: True)
                self.assertEqual(ctx.exception.code, "E_ROOT")

    def test_receipt_projection_domains_metadata_only_and_no_input_authority(self):
        contract = _make_accepted_contract()
        payload = b"identical-raw-bytes"
        h_ui = wm.domain_sha256("ui", payload)
        h_sim = wm.domain_sha256("simulation", payload)
        h_rgb = wm.domain_sha256("rgb24", payload)
        self.assertEqual(len({h_ui, h_sim, h_rgb}), 3)

        receipt = wm.project_receipt(
            contract=contract,
            identity={"run_id": "run-01", "lease_id": "lease-01"},
            admitted_actions=5,
            applied_actions=3,
            frames_captured=None,  # unknown remains None, never 0
            frames_published=10,
            first_frame=None,
            last_frame=None,
            foreign_action=None,
            foreign_t2=None,
            foreign_state=None,
            verdict="PASS",
            code="OK",
            storage_evidence={"ram_peak_bytes": 4096},
            listener_evidence={"eof": True},
            exit_evidence={"joined": True},
            cleanup_evidence={"purged": True},
            retention_mode="metadata_only",
            pixel_replay_claimed=False,
            input_authority_claimed=False,
        )
        self.assertEqual(receipt["schema"], "w8u.receipt/v1")
        self.assertIsNone(receipt["populations"]["frames_captured"])
        self.assertEqual(receipt["populations"]["admitted_actions"], 5)
        self.assertEqual(receipt["populations"]["applied_actions"], 3)
        self.assertFalse(receipt["storage_evidence"]["pixel_replay_available"])
        self.assertFalse(receipt["storage_evidence"]["input_authority_granted"])

        # Hash-only retention is metadata_only and cannot claim pixel replay after purge
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.project_receipt(
                contract=contract,
                identity={"run_id": "run-01"},
                cleanup_evidence={"purged": True},
                retention_mode="metadata_only",
                pixel_replay_claimed=True,
            )
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

        # Capture grants no input authority
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.project_receipt(
                contract=contract,
                identity={"run_id": "run-01"},
                input_authority_claimed=True,
            )
        self.assertEqual(ctx.exception.code, "E_BINDING")

    def test_must_fail_unmap_drops_charge_and_positive_control_budget_hold(self):
        spec = wm.ReservationSpec(
            slots=2,
            width=16,
            height=16,
            max_record_bytes=16384,
            max_inflight_frames=2,
            consumer_view_slots=2,
        )
        expected_charge = spec.compute_breakdown()["total_ownership_bytes"]

        # 1. Required row must-FAIL fixture: unmap error drops outstanding charge
        faulty = wm._FaultyUnmapDropsChargeLedger(expected_charge)
        faulty.close_with_unmap_error()
        self.assertEqual(faulty.reserved_bytes, 0)
        self.assertFalse(faulty.unmap_proven)
        with self.assertRaises(wm.W8UMemoryError) as ctx:
            wm.verify_purge_settlement_invariant(
                faulty, expected_charge=expected_charge, unmap_proven=faulty.unmap_proven
            )
        self.assertEqual(ctx.exception.code, "E_RAM_QUOTA")

        # 2. Independent positive control: ring+pending+view budget with cleanup hold
        acc = wm.HostAdmissionAccountant(
            quota_bytes=64 * wm.MIB,
            telemetry_source=lambda: _healthy_telemetry(),
            ring_factory=lambda h, ep, sz: wm.FakeRingBuffer(sz, fail_close_times=1),
        )
        tok = acc.admit(wsb.Handle("capsule", "cap-pos-ctrl", 0), spec, channel_ids=())
        acc.acquire_consumer_lease(tok, "view-hold-1", frame_seq=1)

        # Close while view lease is held -> charge stays intact
        self.assertFalse(acc.close_claim(tok))
        wm.verify_purge_settlement_invariant(acc, expected_charge=expected_charge, unmap_proven=False)
        self.assertEqual(acc.ownership_reserved_bytes, expected_charge)

        # Release view lease -> attempts unmap, which fails once -> charge STILL stays intact
        self.assertFalse(acc.release_consumer_lease(tok, "view-hold-1", frame_seq=1))
        wm.verify_purge_settlement_invariant(acc, expected_charge=expected_charge, unmap_proven=False)
        self.assertEqual(acc.ownership_reserved_bytes, expected_charge)

        # Next retry_cleanup proves unmap and settles charge to 0
        self.assertEqual(acc.retry_cleanup(), 1)
        wm.verify_purge_settlement_invariant(acc, expected_charge=expected_charge, unmap_proven=True)
        self.assertEqual(acc.ownership_reserved_bytes, 0)

    def test_offline_stdlib_and_storage_law_compliance(self):
        src_path = Path(wm.__file__).resolve()
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        imported_roots = set()
        vadapt_submodules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                root_mod = node.module.split(".")[0]
                imported_roots.add(root_mod)
                if root_mod == "vadapt":
                    for alias in node.names:
                        vadapt_submodules.add(alias.name)

        forbidden = {"numpy", "visioncore", "vmem", "subprocess", "socket", "http", "urllib"}
        self.assertFalse(imported_roots & forbidden)
        self.assertTrue(imported_roots <= (set(sys.stdlib_module_names) | {"vadapt"}))
        self.assertEqual(vadapt_submodules, {"ramws", "rtring", "window_stream_bank"})
        self.assertEqual(ramws.check_disk(src_path.parent), [])


if __name__ == "__main__":
    unittest.main()
