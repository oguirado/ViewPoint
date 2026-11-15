"""Focused unit tests and semantic mutation verification for vadapt.proto1_ram_policy (p1-va-ram-policy)."""

from __future__ import annotations

import ast
import builtins
import copy
from dataclasses import fields
import hashlib
import io
import os
import pathlib
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np

from vadapt import proto1_ram_policy as prp, ramws, rtring, w8u_memory as wm, window_stream_bank as wsb


# Independent handwritten literal RGB24 byte patterns (4x2 pixels = 24 bytes each)
# Computed outside production code with stdlib hashlib.sha256; no encoder round-trip.
HANDWRITTEN_RGB24_FRAME_1 = (
    b"\x10\x20\x30\x40\x50\x60\x70\x80\x90\xa0\xb0\xc0"
    b"\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\xcc"
)
HANDWRITTEN_RGB24_FRAME_2 = (
    b"\xfe\xdc\xba\x98\x76\x54\x32\x10\x01\x23\x45\x67"
    b"\x89\xab\xcd\xef\x0f\x1e\x2d\x3c\x4b\x5a\x69\x78"
)
ORACLE_SHA256_FRAME_1 = hashlib.sha256(HANDWRITTEN_RGB24_FRAME_1).hexdigest()
ORACLE_SHA256_FRAME_2 = hashlib.sha256(HANDWRITTEN_RGB24_FRAME_2).hexdigest()

# Handwritten rtring byte-size oracle for slots=2, width=4, height=2:
# data_off = align_4096(64 + 2 * 32) = 4096; payload = 2 * (4 * 2 * 3) = 48; total = 4144 bytes.
ORACLE_RING_BYTES_2_4_2 = 4096 + 2 * (4 * 2 * 3)


def _make_accepted_contract(*, include_producer: bool = True) -> wm.ContractEvidence:
    w, h = 4, 2
    valid_xwd = wm.build_synthetic_xwd(HANDWRITTEN_RGB24_FRAME_1, w, h, byte_order=0, bitmap_bit_order=0)
    oversized_xwd = wm.build_synthetic_xwd(
        HANDWRITTEN_RGB24_FRAME_1, w, h, byte_order=0, bitmap_bit_order=0, header_extra_bytes=b"Z" * 512
    )
    bad_bbo_xwd = wm.build_synthetic_xwd(HANDWRITTEN_RGB24_FRAME_1, w, h, byte_order=0, bitmap_bit_order=1)
    return wm.ContractEvidence(
        revision="rev-p1-va-ram-policy-001",
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
        closed_error_numeric_map={code: idx for idx, code in enumerate(wm.CLOSED_ERROR_CODES, start=1)},
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
            "accepted": [{"width": w, "height": h, "xwd": valid_xwd, "rgb24_sha256": ORACLE_SHA256_FRAME_1}],
            "rejected": {
                "oversized_header": {"width": w, "height": h, "xwd": oversized_xwd, "expected_code": "E_LENGTH"},
                "invalid_bitmap_bit_order": {"width": w, "height": h, "xwd": bad_bbo_xwd, "expected_code": "E_WIRE"},
            },
        },
        merged_production_owners=("w8u-ve-memory",) if include_producer else (),
    )


def _make_binding(
    *,
    run_id: str = "run-b-001",
    canonical_id: str = "win-b-01",
    generation: int = 1,
    epoch: int = 0,
    width: int = 4,
    height: int = 2,
    frames_root: str = "/dev/shm/vqa-ramws",
    frame_run_root: str | None = None,
) -> dict:
    actual_run_root = (
        frame_run_root
        if frame_run_root is not None
        else ("/dev/shm/vqa-ramws/opensuperficies/os-cu-0/%s" % run_id)
    )
    return {
        "schema": "w8u.binding/v1",
        "run_id": run_id,
        "lease_id": "lease-b-001",
        "seat_grant_sha256": "a" * 64,
        "container_id": "os-cu-0-ctr",
        "target_handle": {
            "kind": "x11_window",
            "canonical_id": canonical_id,
            "generation": generation,
        },
        "epoch": epoch,
        "display": ":99",
        "xid": 4096,
        "width": width,
        "height": height,
        "frames_root": frames_root,
        "frame_run_root": actual_run_root,
        "image_sha256": "b" * 64,
        "reader_sha256": "c" * 64,
        "exec_allowlist_sha256": "d" * 64,
        "max_record_bytes": 65536,
        "max_inflight_frames": 2,
        "tick_mode": "committed",
    }


class TestP1VaRamPolicyBehavior(unittest.TestCase):
    """Behavioral unit tests for Area B RAM provenance and borrowed-view lifetime policy."""

    def tearDown(self) -> None:
        ramws._close_all()

    def test_positive_control_real_bank_workspace_and_borrow_release_close_witnesses(self) -> None:
        contract = _make_accepted_contract()
        adapter = prp.BStorageRamPolicyAdapter(
            contract=contract,
            workspace_quota_bytes=16 * wm.MIB,
            bank_quota_bytes=8 * wm.MIB,
        )
        binding = _make_binding(run_id="run-pos-001")
        binding_snapshot = copy.deepcopy(binding)
        expected_binding_sha = hashlib.sha256(wm.canonical_json_bytes(binding)).hexdigest()

        root_auth = adapter.open_run_root(binding, grant_authenticator=lambda b: True)
        self.assertEqual(binding, binding_snapshot)
        self.assertEqual(root_auth.run_id, "run-pos-001")
        self.assertEqual(root_auth.frames_root, "/dev/shm/vqa-ramws")
        self.assertEqual(root_auth.frame_run_root, "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-pos-001")
        self.assertIn(root_auth.fstype, ("tmpfs", "ramfs"))
        self.assertEqual(root_auth.binding_sha256, expected_binding_sha)
        self.assertEqual(adapter.reserved_bytes, 0)
        self.assertEqual(adapter.workspace_used_bytes, 0)

        handle = wsb.Handle("x11_window", "win-b-01", 1)
        spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)

        with prp.PersistenceWriteSpy(allow_ramws_control_files=False) as spy:
            token, stream_witness = adapter.admit_stream(
                root_auth,
                handle,
                spec,
                configured_reservation_bytes=ORACLE_RING_BYTES_2_4_2,
            )
            self.assertEqual(stream_witness["ring_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(stream_witness["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(stream_witness["workspace_used_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(adapter.workspace_used_bytes, ORACLE_RING_BYTES_2_4_2)

            arr1 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
            arr2 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_2, 4, 2)
            self.assertFalse(arr1.flags.writeable)
            self.assertFalse(arr2.flags.writeable)

            pipe = wm.FakeChannelPipe("ctrl-pos", max_queue_depth=4)
            admit1 = adapter.admit_frame(root_auth, token, arr1, frame_seq=1, channel=pipe)
            id1 = admit1["frame_identity"]
            self.assertEqual(
                tuple(f.name for f in fields(prp.FrameIdentity)),
                ("frame_seq", "width", "height", "rgb24_sha256", "binding_sha256"),
            )
            self.assertEqual(
                id1.as_tuple(),
                (1, 4, 2, ORACLE_SHA256_FRAME_1, expected_binding_sha),
            )
            self.assertEqual(admit1["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(admit1["frames_published"], 1)
            self.assertEqual(admit1["credit_granted"], 1)
            self.assertEqual(len(pipe.queue), 1)

            admit2 = adapter.admit_frame(root_auth, token, arr2, frame_seq=2, channel=pipe)
            id2 = admit2["frame_identity"]
            self.assertEqual(
                id2.as_tuple(),
                (2, 4, 2, ORACLE_SHA256_FRAME_2, expected_binding_sha),
            )
            self.assertEqual(admit2["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(admit2["frames_published"], 2)

            view1, bw1 = adapter.borrow_frame_view(root_auth, token, frame_seq=1, borrow_id="b-seq-1")
            self.assertEqual(bw1["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(bw1["workspace_used_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(bw1["active_borrows"], 1)
            self.assertEqual(bytes(view1.rgb_bytes_view), HANDWRITTEN_RGB24_FRAME_1)
            self.assertFalse(view1.rgb_array_view.flags.writeable)

            view2, bw2 = adapter.borrow_frame_view(root_auth, token, frame_seq=2, borrow_id="b-seq-2")
            self.assertEqual(bw2["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)
            self.assertEqual(bw2["active_borrows"], 2)
            self.assertEqual(bytes(view2.rgb_bytes_view), HANDWRITTEN_RGB24_FRAME_2)

            rw1 = adapter.release_borrowed_view(root_auth, view1)
            self.assertTrue(view1.released)
            self.assertEqual(rw1["active_borrows"], 1)
            self.assertEqual(rw1["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)

            rw2 = adapter.release_borrowed_view(root_auth, view2)
            self.assertTrue(view2.released)
            self.assertEqual(rw2["active_borrows"], 0)
            self.assertEqual(rw2["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)

            cw = adapter.close_stream(root_auth, token)
            self.assertTrue(cw["settled"])
            self.assertFalse(cw["obligations_retained"])
            self.assertEqual(cw["bank_reserved_bytes"], 0)
            self.assertEqual(cw["active_borrows"], 0)
            self.assertEqual(adapter.reserved_bytes, 0)
            self.assertEqual(len(spy.intercepted_attempts), 0)

        cleanup_witness, ws_receipt = adapter.purge_run(root_auth)
        self.assertTrue(cleanup_witness.workspace_closed)
        self.assertEqual(cleanup_witness.bank_reserved_bytes, 0)
        self.assertEqual(cleanup_witness.active_borrows, 0)
        self.assertEqual(ws_receipt["ram_peak_bytes"], ORACLE_RING_BYTES_2_4_2)

        first_meta = {
            "seq": 1,
            "tick": 0,
            "tick_valid": True,
            "capture_mono_ns": 1000,
            "width": 4,
            "height": 2,
            "stride": 12,
            "pixel_format": 1,
            "sha256": ORACLE_SHA256_FRAME_1,
            "binding_sha256": expected_binding_sha,
        }
        last_meta = dict(first_meta, seq=2, capture_mono_ns=2000, sha256=ORACLE_SHA256_FRAME_2)
        receipt = adapter.project_metadata_receipt(
            cleanup_witness=cleanup_witness,
            ws_receipt=ws_receipt,
            first_frame=first_meta,
            last_frame=last_meta,
            admitted_actions=None,
            applied_actions=None,
            frames_captured=None,
        )
        self.assertEqual(receipt["schema"], "w8u.receipt/v1")
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual(receipt["code"], "OK")
        self.assertEqual(receipt["populations"]["frames_published"], 2)
        self.assertIsNone(receipt["populations"]["frames_captured"])
        self.assertIsNone(receipt["storage_evidence"]["host_timing_ns"]["value"])
        self.assertEqual(receipt["storage_evidence"]["host_timing_ns"]["claim_label"], "AUSENTE")

    def test_close_with_retained_view_holds_obligation_until_actual_release(self) -> None:
        contract = _make_accepted_contract()
        adapter = prp.BStorageRamPolicyAdapter(contract=contract)
        root_auth = adapter.open_run_root(_make_binding(run_id="run-retain-001"), grant_authenticator=lambda b: True)
        handle = wsb.Handle("x11_window", "win-retain", 1)
        spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
        token, _ = adapter.admit_stream(root_auth, handle, spec)

        arr1 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
        adapter.admit_frame(root_auth, token, arr1, frame_seq=1)
        view, _ = adapter.borrow_frame_view(root_auth, token, frame_seq=1)

        cw = adapter.close_stream(root_auth, token)
        self.assertFalse(cw["settled"])
        self.assertTrue(cw["obligations_retained"])
        self.assertEqual(cw["bank_reserved_bytes"], ORACLE_RING_BYTES_2_4_2)
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)
        self.assertEqual(adapter.bank.retry_cleanup(), 0)
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)

        rw = adapter.release_borrowed_view(root_auth, view)
        self.assertTrue(rw["settled_on_release"])
        self.assertEqual(rw["bank_reserved_bytes"], 0)
        self.assertEqual(adapter.reserved_bytes, 0)
        adapter.purge_run(root_auth)

    def test_window_stream_bank_native_messages_lock_order_and_epoch_resize(self) -> None:
        contract = _make_accepted_contract()
        adapter = prp.BStorageRamPolicyAdapter(contract=contract)
        root_auth = adapter.open_run_root(_make_binding(run_id="run-wsb-msg-001"), grant_authenticator=lambda b: True)
        h_a = wsb.Handle("x11_window", "win-a", 1)
        h_b = wsb.Handle("x11_window", "win-b", 1)
        spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)

        with self.assertRaises(prp.RamPolicyError) as ctx_order:
            adapter.admit_streams_many(root_auth, ((h_b, spec), (h_a, spec)))
        self.assertEqual(ctx_order.exception.code, "E_BINDING")
        self.assertIn("stream handles must be unique and in canonical order", str(ctx_order.exception))
        self.assertEqual(adapter.reserved_bytes, 0)

        tok_e0, _ = adapter.admit_stream(root_auth, h_a, spec)
        arr1 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
        adapter.admit_frame(root_auth, tok_e0, arr1, frame_seq=1)
        view_e0, _ = adapter.borrow_frame_view(root_auth, tok_e0, frame_seq=1)

        tok_e1, resize_w = adapter.resize_stream(root_auth, tok_e0, spec)
        self.assertEqual(tok_e1.epoch, 1)
        self.assertFalse(resize_w["old_settled"])
        self.assertEqual(adapter.reserved_bytes, 2 * ORACLE_RING_BYTES_2_4_2)

        with self.assertRaises(prp.RamPolicyError) as ctx_stale_epoch:
            adapter.resize_stream(root_auth, tok_e0, spec)
        self.assertEqual(ctx_stale_epoch.exception.code, "E_BINDING")
        self.assertIn("closed generation or stale geometry epoch", str(ctx_stale_epoch.exception))

        rw_e0 = adapter.release_borrowed_view(root_auth, view_e0)
        self.assertTrue(rw_e0["settled_on_release"])
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)

        adapter.purge_stream(root_auth, tok_e1)
        self.assertEqual(adapter.reserved_bytes, 0)
        adapter.workspace.close()

    def test_must_fail_file_backed_or_unproven_root_and_generational_lifetime_e_root(self) -> None:
        contract = _make_accepted_contract()
        tmp_dir = tempfile.mkdtemp(prefix="p1_va_non_tmpfs_")
        try:
            fake_mountinfo = os.path.join(tmp_dir, "mountinfo_ext4")
            Path(fake_mountinfo).write_text(
                "25 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
                "26 25 8:1 /dev/shm /dev/shm rw,relatime - ext4 /dev/sda1 rw\n",
                encoding="utf-8",
            )
            adapter_ext4 = prp.BStorageRamPolicyAdapter(contract=contract, mountinfo=fake_mountinfo)
            b_input = _make_binding(run_id="run-ext4-001")
            b_snap = copy.deepcopy(b_input)
            with self.assertRaises(prp.RamPolicyError) as ctx_ext4:
                adapter_ext4.open_run_root(b_input, grant_authenticator=lambda b: True)
            self.assertEqual(ctx_ext4.exception.code, "E_ROOT")
            self.assertEqual(b_input, b_snap)
            self.assertEqual(adapter_ext4.reserved_bytes, 0)
            self.assertEqual(adapter_ext4.workspace_used_bytes, 0)

            adapter = prp.BStorageRamPolicyAdapter(contract=contract)
            for bad_binding in (
                _make_binding(run_id="run-bad-1", frames_root=tmp_dir, frame_run_root=os.path.join(tmp_dir, "opensuperficies/os-cu-0/run-bad-1")),
                _make_binding(run_id="run-bad-2", frame_run_root="/dev/shm/vqa-ramws/other/os-cu-0/run-bad-2"),
                _make_binding(run_id="run-bad-3", frame_run_root="/dev/shm/vqa-ramws/opensuperficies/os-cu-0/../run-bad-3"),
                _make_binding(run_id="run-bad-4", frame_run_root="/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run/run-bad-4"),
                _make_binding(run_id="run-bad-5", frame_run_root="/dev/shm/vqa-ramws/opensuperficies/os-cu-0/run-bad-5/nested"),
                _make_binding(run_id="nested/run-bad-6"),
            ):
                with self.assertRaises(prp.RamPolicyError) as ctx_root:
                    adapter.open_run_root(bad_binding, grant_authenticator=lambda b: True)
                self.assertEqual(ctx_root.exception.code, "E_ROOT")
                self.assertEqual(adapter.reserved_bytes, 0)

            e_not_tmpfs = prp.translate_storage_error(ramws.RamError("E_RAM_NOT_TMPFS", "not tmpfs"))
            e_escape = prp.translate_storage_error(ramws.RamError("E_RAM_ESCAPE", "escaped root"))
            self.assertEqual(e_not_tmpfs.code, "E_ROOT")
            self.assertEqual(e_escape.code, "E_ROOT")

            non_existent_parent = os.path.join(tmp_dir, "never_created_parent")
            with self.assertRaises(prp.RamPolicyError) as ctx_pre:
                prp.preflight_workspace_relpath(tmp_dir, "../escape/file.bin")
            self.assertEqual(ctx_pre.exception.code, "E_ROOT")
            self.assertFalse(os.path.exists(non_existent_parent))

            valid_adapter = prp.BStorageRamPolicyAdapter(contract=contract)
            root_auth = valid_adapter.open_run_root(_make_binding(run_id="run-gen-001"), grant_authenticator=lambda b: True)
            unissued_auth = prp.AuthenticatedRunRoot(
                run_id="run-gen-001",
                frames_root=root_auth.frames_root,
                frame_run_root=root_auth.frame_run_root,
                fstype="tmpfs",
                quota_bytes=root_auth.quota_bytes,
                binding_sha256=root_auth.binding_sha256,
                root_generation=999,
                adapter_id=root_auth.adapter_id,
                capability_nonce="f" * 64,
            )
            handle = wsb.Handle("x11_window", "win-gen", 1)
            spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
            with self.assertRaises(prp.RamPolicyError) as ctx_unissued:
                valid_adapter.admit_stream(unissued_auth, handle, spec)
            self.assertEqual(ctx_unissued.exception.code, "E_ROOT")
            self.assertEqual(valid_adapter.reserved_bytes, 0)

            valid_adapter.purge_run(root_auth)
            with self.assertRaises(prp.RamPolicyError) as ctx_stale:
                valid_adapter.admit_stream(root_auth, handle, spec)
            self.assertEqual(ctx_stale.exception.code, "E_ROOT")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_must_fail_reservation_shortage_and_over_reservation_e_ram_quota(self) -> None:
        contract = _make_accepted_contract()

        with self.assertRaises(prp.RamPolicyError) as ctx_init_over:
            prp.BStorageRamPolicyAdapter(
                contract=contract,
                workspace_quota_bytes=4 * wm.MIB,
                bank_quota_bytes=8 * wm.MIB,
            )
        self.assertEqual(ctx_init_over.exception.code, "E_RAM_QUOTA")

        adapter = prp.BStorageRamPolicyAdapter(
            contract=contract,
            workspace_quota_bytes=16384,
            bank_quota_bytes=8192,
        )
        root_auth = adapter.open_run_root(_make_binding(run_id="run-quota-001"), grant_authenticator=lambda b: True)
        handle = wsb.Handle("x11_window", "win-quota", 1)

        short_spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2 - 1)
        with self.assertRaises(prp.RamPolicyError) as ctx_short_spec:
            adapter.admit_stream(root_auth, handle, short_spec)
        self.assertEqual(ctx_short_spec.exception.code, "E_RAM_QUOTA")
        self.assertIn("ring_bytes exceeds StreamSpec.quota_bytes", str(ctx_short_spec.exception))
        self.assertEqual(adapter.reserved_bytes, 0)
        self.assertEqual(adapter.workspace_used_bytes, 0)

        valid_spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
        with self.assertRaises(prp.RamPolicyError) as ctx_short_cfg:
            adapter.admit_stream(
                root_auth,
                handle,
                valid_spec,
                configured_reservation_bytes=ORACLE_RING_BYTES_2_4_2 - 16,
            )
        self.assertEqual(ctx_short_cfg.exception.code, "E_RAM_QUOTA")
        self.assertEqual(adapter.reserved_bytes, 0)
        self.assertEqual(adapter.workspace_used_bytes, 0)

        over_spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2 + 1024)
        with self.assertRaises(prp.RamPolicyError) as ctx_over_cfg:
            adapter.admit_stream(
                root_auth,
                handle,
                over_spec,
                configured_reservation_bytes=ORACLE_RING_BYTES_2_4_2,
            )
        self.assertEqual(ctx_over_cfg.exception.code, "E_RAM_QUOTA")
        self.assertEqual(adapter.reserved_bytes, 0)
        self.assertEqual(adapter.workspace_used_bytes, 0)

        huge_quota_spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=9000)
        with self.assertRaises(prp.RamPolicyError) as ctx_over_bank:
            adapter.admit_stream(root_auth, handle, huge_quota_spec)
        self.assertEqual(ctx_over_bank.exception.code, "E_RAM_QUOTA")
        self.assertEqual(adapter.reserved_bytes, 0)

        tok1, _ = adapter.admit_stream(root_auth, handle, valid_spec)
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)
        handle2 = wsb.Handle("x11_window", "win-quota-2", 1)
        with self.assertRaises(prp.RamPolicyError) as ctx_shared_shortage:
            adapter.admit_stream(root_auth, handle2, valid_spec)
        self.assertEqual(ctx_shared_shortage.exception.code, "E_RAM_QUOTA")
        self.assertIn("transaction exceeds remaining shared quota", str(ctx_shared_shortage.exception))
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)
        self.assertEqual(adapter.workspace_used_bytes, ORACLE_RING_BYTES_2_4_2)

        adapter.close_stream(root_auth, tok1)
        self.assertEqual(adapter.reserved_bytes, 0)
        adapter.workspace.close()

    def test_must_fail_purge_with_borrowed_view_and_fabricated_cleanup(self) -> None:
        contract = _make_accepted_contract()
        adapter = prp.BStorageRamPolicyAdapter(contract=contract)
        root_auth = adapter.open_run_root(_make_binding(run_id="run-purge-fail-001"), grant_authenticator=lambda b: True)
        handle = wsb.Handle("x11_window", "win-purge", 1)
        spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
        token, _ = adapter.admit_stream(root_auth, handle, spec)

        arr1 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
        adapter.admit_frame(root_auth, token, arr1, frame_seq=1)
        view, _ = adapter.borrow_frame_view(root_auth, token, frame_seq=1)

        with self.assertRaises(prp.RamPolicyError) as ctx_purge_stream:
            adapter.purge_stream(root_auth, token)
        self.assertEqual(ctx_purge_stream.exception.code, "E_RAM_QUOTA")
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)
        self.assertEqual(adapter.total_active_borrows, 1)
        self.assertFalse(view.released)

        with self.assertRaises(prp.RamPolicyError) as ctx_purge_run:
            adapter.purge_run(root_auth)
        self.assertEqual(ctx_purge_run.exception.code, "E_RAM_QUOTA")
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)

        adapter.release_borrowed_view(root_auth, view)
        with self.assertRaises(prp.RamPolicyError) as ctx_unpurged_stream:
            adapter.purge_run(root_auth)
        self.assertEqual(ctx_unpurged_stream.exception.code, "E_RAM_QUOTA")
        self.assertEqual(adapter.reserved_bytes, ORACLE_RING_BYTES_2_4_2)

        adapter.purge_stream(root_auth, token)
        self.assertEqual(adapter.reserved_bytes, 0)

        with self.assertRaises(prp.RamPolicyError) as ctx_fab:
            adapter.purge_run(root_auth, fabricated_cleanup_evidence={"purged": True})
        self.assertEqual(ctx_fab.exception.code, "E_RAM_QUOTA")

        forged_witness = prp.CleanupAuthorityWitness(
            run_id=root_auth.run_id,
            root_generation=root_auth.root_generation,
            adapter_id=root_auth.adapter_id,
            bank_reserved_bytes=0,
            active_borrows=0,
            workspace_closed=True,
            witness_digest="0" * 64,
        )
        ws_rec = adapter.workspace.receipt()
        with self.assertRaises(prp.RamPolicyError) as ctx_no_mint:
            adapter.project_metadata_receipt(
                cleanup_witness=forged_witness,
                ws_receipt=ws_rec,
                verdict="PASS",
                code="OK",
            )
        self.assertEqual(ctx_no_mint.exception.code, "E_BINDING")
        adapter.workspace.close()

    def test_must_fail_write_attempts_spy_and_isolated_process_audit_hook(self) -> None:
        contract = _make_accepted_contract()
        adapter = prp.BStorageRamPolicyAdapter(contract=contract)
        root_auth = adapter.open_run_root(_make_binding(run_id="run-write-fail-001"), grant_authenticator=lambda b: True)
        handle = wsb.Handle("x11_window", "win-write", 1)
        spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
        token, _ = adapter.admit_stream(root_auth, handle, spec)

        writable_arr = np.frombuffer(bytearray(HANDWRITTEN_RGB24_FRAME_1), dtype=np.uint8).reshape(2, 4, 3)
        self.assertTrue(writable_arr.flags.writeable)
        pipe = wm.FakeChannelPipe("ctrl-write", max_queue_depth=4)
        with self.assertRaises(prp.RamPolicyError) as ctx_writable:
            adapter.admit_frame(root_auth, token, writable_arr, frame_seq=1, channel=pipe)
        self.assertEqual(ctx_writable.exception.code, "E_WIRE")
        self.assertEqual(len(pipe.queue), 0)

        imm_arr = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
        adapter.admit_frame(root_auth, token, imm_arr, frame_seq=1, channel=pipe)
        self.assertEqual(len(pipe.queue), 1)
        view, _ = adapter.borrow_frame_view(root_auth, token, frame_seq=1)

        with self.assertRaises(prp.RamPolicyError) as ctx_borrowed_admit:
            adapter.admit_frame(root_auth, token, view.rgb_array_view, frame_seq=2, channel=pipe)
        self.assertEqual(ctx_borrowed_admit.exception.code, "E_WIRE")
        self.assertEqual(len(pipe.queue), 1)

        with self.assertRaises(prp.RamPolicyError) as ctx_view_set:
            view[0] = 0xFF
        self.assertEqual(ctx_view_set.exception.code, "E_WIRE")
        with self.assertRaises(prp.RamPolicyError) as ctx_view_write:
            view.write(b"\x00")
        self.assertEqual(ctx_view_write.exception.code, "E_WIRE")
        with self.assertRaises(TypeError):
            view.rgb_bytes_view[0] = 0xFF
        with self.assertRaises(ValueError):
            view.rgb_array_view[0, 0, 0] = 0xFF
        self.assertEqual(bytes(view.rgb_bytes_view), HANDWRITTEN_RGB24_FRAME_1)

        adapter.release_borrowed_view(root_auth, view)
        adapter.purge_stream(root_auth, token)
        adapter.workspace.close()

        dummy_arr = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
        entry_calls = [
            ("builtins.open", lambda: builtins.open("/tmp/forbid_b.bin", "wb")),
            ("os.open", lambda: os.open("/tmp/forbid_o.bin", os.O_WRONLY | os.O_CREAT)),
            ("io.open", lambda: io.open("/tmp/forbid_i.bin", "wb")),
            ("io.FileIO", lambda: io.FileIO("/tmp/forbid_fio.bin", "w")),
            ("pathlib.Path.open", lambda: Path("/tmp/forbid_p.bin").open("wb")),
            ("pathlib.Path.write_bytes", lambda: Path("/tmp/forbid_wb.bin").write_bytes(b"123")),
            ("pathlib.Path.write_text", lambda: Path("/tmp/forbid_wt.txt").write_text("123")),
            ("numpy.save", lambda: np.save("/tmp/forbid_np.npy", dummy_arr)),
            ("numpy.savez", lambda: np.savez("/tmp/forbid_npz.npz", a=dummy_arr)),
            ("numpy.savez_compressed", lambda: np.savez_compressed("/tmp/forbid_npzc.npz", a=dummy_arr)),
            ("numpy.savetxt", lambda: np.savetxt("/tmp/forbid_txt.txt", dummy_arr.reshape(-1, 3))),
        ]
        with prp.PersistenceWriteSpy(allow_ramws_control_files=False) as spy:
            for expected_ep, fn in entry_calls:
                with self.assertRaises(prp.RamPolicyError) as ctx_spy:
                    fn()
                self.assertEqual(ctx_spy.exception.code, "E_ROOT")
                self.assertEqual(spy.intercepted_attempts[-1]["entry_point"], expected_ep)
        self.assertEqual(len(spy.intercepted_attempts), len(entry_calls))

        def _isolated_child_workload():
            hook_events = prp.install_irreversible_persistence_audit_hook(refusal_code="E_ROOT")
            bank = wsb.WindowStreamBank(quota_bytes=16384)
            h = wsb.Handle("x11_window", "iso-win", 1)
            s = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
            tok = bank.open(h, s)
            arr = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
            bank.publish(tok, arr, frame_seq=1)
            ref = bank.read(tok)
            bank.close(tok)

            blocked = []
            for write_fn in (
                lambda: open("/tmp/iso_forbid1.raw", "wb"),
                lambda: os.open("/tmp/iso_forbid2.raw", os.O_WRONLY | os.O_CREAT),
                lambda: Path("/tmp/iso_forbid3.png").write_bytes(b"px"),
                lambda: np.save("/tmp/iso_forbid4.npy", arr),
                lambda: arr.tofile("/tmp/iso_forbid5.raw"),
            ):
                try:
                    write_fn()
                except prp.RamPolicyError as exc:
                    blocked.append(exc.code)
            return {
                "digest": ref.digest,
                "bank_reserved_after_close": bank.reserved_bytes,
                "blocked_codes": blocked,
                "hook_event_count": len(hook_events),
            }

        iso_res = prp.run_in_isolated_audit_process(_isolated_child_workload)
        self.assertTrue(iso_res["ok"])
        self.assertEqual(iso_res["result"]["digest"], ORACLE_SHA256_FRAME_1)
        self.assertEqual(iso_res["result"]["bank_reserved_after_close"], 0)
        self.assertEqual(iso_res["result"]["blocked_codes"], ["E_ROOT"] * 5)
        self.assertEqual(iso_res["result"]["hook_event_count"], 5)

    def test_frame_identity_seq_binding_join_seat_schema_no_pixels_and_unknown_measurements(self) -> None:
        self.assertEqual(
            tuple(f.name for f in fields(prp.FrameIdentity)),
            ("frame_seq", "width", "height", "rgb24_sha256", "binding_sha256"),
        )
        fid = prp.FrameIdentity(1, 4, 2, ORACLE_SHA256_FRAME_1, "b" * 64)
        self.assertEqual(tuple(fid.to_dict().keys()), prp.FRAME_IDENTITY_FIELDS)

        for bad_args, exp_code in (
            ((0, 4, 2, ORACLE_SHA256_FRAME_1, "b" * 64), "E_SEQ"),
            ((True, 4, 2, ORACLE_SHA256_FRAME_1, "b" * 64), "E_SEQ"),
            ((1, 0, 2, ORACLE_SHA256_FRAME_1, "b" * 64), "E_GEOMETRY"),
            ((1, 4, False, ORACLE_SHA256_FRAME_1, "b" * 64), "E_GEOMETRY"),
            ((1, 4, 2, "UPPER" + "a" * 59, "b" * 64), "E_HASH"),
            ((1, 4, 2, ORACLE_SHA256_FRAME_1, "short"), "E_BINDING"),
        ):
            with self.assertRaises(prp.RamPolicyError) as ctx_fid:
                prp.FrameIdentity(*bad_args)
            self.assertEqual(ctx_fid.exception.code, exp_code)

        contract = _make_accepted_contract()
        adapter = prp.BStorageRamPolicyAdapter(contract=contract)
        root_auth = adapter.open_run_root(_make_binding(run_id="run-seq-001"), grant_authenticator=lambda b: True)
        handle = wsb.Handle("x11_window", "win-seq", 1)
        spec = wsb.StreamSpec(width=4, height=2, slots=2, quota_bytes=ORACLE_RING_BYTES_2_4_2)
        token, _ = adapter.admit_stream(root_auth, handle, spec)
        arr1 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_1, 4, 2)
        arr2 = prp.make_immutable_rgb24(HANDWRITTEN_RGB24_FRAME_2, 4, 2)

        with self.assertRaises(prp.RamPolicyError) as ctx_seq0:
            adapter.admit_frame(root_auth, token, arr1, frame_seq=0)
        self.assertEqual(ctx_seq0.exception.code, "E_SEQ")

        bad_geom_arr = prp.make_immutable_rgb24(b"\x00" * 12, 2, 2)
        with self.assertRaises(prp.RamPolicyError) as ctx_geom:
            adapter.admit_frame(root_auth, token, bad_geom_arr, frame_seq=1)
        self.assertEqual(ctx_geom.exception.code, "E_GEOMETRY")
        self.assertIn("source must be exact uint8 HxWx3 RGB24", str(ctx_geom.exception))

        res1 = adapter.admit_frame(root_auth, token, arr1, frame_seq=1)
        self.assertEqual(res1["frames_published"], 1)

        with self.assertRaises(prp.RamPolicyError) as ctx_seq3:
            adapter.admit_frame(root_auth, token, arr2, frame_seq=3)
        self.assertEqual(ctx_seq3.exception.code, "E_SEQ")
        self.assertIn("frame_seq must increase monotonically per stream", str(ctx_seq3.exception))

        valid_seat = adapter.build_seat_record(root_auth, token, frame_seq=1)
        self.assertEqual(frozenset(valid_seat.keys()), prp.CLOSED_SEAT_RECORD_KEYS)
        with self.assertRaises(prp.RamPolicyError) as ctx_extra_seat:
            adapter.build_seat_record(root_auth, token, frame_seq=1, extra_keys={"unauthorized_field": 123})
        self.assertEqual(ctx_extra_seat.exception.code, "E_BINDING")

        adapter.purge_stream(root_auth, token)
        adapter.workspace.close()

        bref1 = res1["bound_reference"]
        joined = prp.join_bound_references([bref1], [bref1], join_mode="exact_binding")
        self.assertEqual(len(joined), 1)

        with self.assertRaises(prp.RamPolicyError) as ctx_ts_join:
            prp.join_bound_references([bref1], [bref1], join_mode="nearest_timestamp")
        self.assertEqual(ctx_ts_join.exception.code, "E_BINDING")

        mismatched_epoch_ref = prp.BoundFrameReference(
            run_id=bref1.run_id,
            target_handle=bref1.target_handle,
            epoch=bref1.epoch + 1,
            identity=bref1.identity,
        )
        with self.assertRaises(prp.RamPolicyError) as ctx_mismatch_join:
            prp.join_bound_references([bref1], [mismatched_epoch_ref])
        self.assertEqual(ctx_mismatch_join.exception.code, "E_BINDING")

        for bad_meta in (
            {"rgb": HANDWRITTEN_RGB24_FRAME_1},
            {"raw_bytes": b"\x00\x01\x02"},
            {"nested": {"png_data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="}},
            {"reconstructible": list(range(24))},
            {"long_hex_dump": "ab" * 64},
        ):
            with self.assertRaises(prp.RamPolicyError) as ctx_px:
                prp.validate_metadata_no_pixels(bad_meta, width=4, height=2)
            self.assertEqual(ctx_px.exception.code, "E_WIRE")

        m_unknown = prp.MeasurementRecord(
            value=None,
            reason="AUSENTE: VM timing is not host timing",
            claim_label="AUSENTE",
        )
        self.assertIsNone(m_unknown.value)
        with self.assertRaises(prp.RamPolicyError) as ctx_zero_unknown:
            prp.MeasurementRecord(value=0, reason="AUSENTE: unmeasured on host", claim_label="AUSENTE")
        self.assertEqual(ctx_zero_unknown.exception.code, "E_WIRE")
        with self.assertRaises(prp.RamPolicyError) as ctx_no_reason:
            prp.MeasurementRecord(value=None, reason="", claim_label="MEASURED")
        self.assertEqual(ctx_no_reason.exception.code, "E_WIRE")

        unmerged_contract = _make_accepted_contract(include_producer=False)
        with self.assertRaises(prp.RamPolicyError) as ctx_prod:
            prp.BStorageRamPolicyAdapter(contract=unmerged_contract)
        self.assertEqual(ctx_prod.exception.code, "E_BINDING")

    def test_code_hygiene_offline_imports_and_no_disk_frames(self) -> None:
        for mod_path in (Path(prp.__file__).resolve(), Path(__file__).resolve()):
            tree = ast.parse(mod_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual(node.func.id, "hasattr", "hasattr guard found in %s" % mod_path.name)
                    if node.func.id == "getattr":
                        self.assertLessEqual(
                            len(node.args), 2, "3-arg getattr default found in %s" % mod_path.name
                        )
        self.assertEqual(ramws.check_disk(Path(prp.__file__).resolve().parent), [])


def run_semantic_mutation_suite() -> dict[str, object]:
    """Run fresh behavior suites against 16 semantic mutants of vadapt.proto1_ram_policy.

    Requires healthy green before mutants, counts only semantic AssertionError kills,
    restores each mutant, and runs a healthy reproof after completion.
    """
    loader = unittest.defaultTestLoader

    def _run_behavior_suite() -> unittest.TestResult:
        suite = loader.loadTestsFromTestCase(TestP1VaRamPolicyBehavior)
        result = unittest.TestResult()
        suite.run(result)
        return result

    pre = _run_behavior_suite()
    if (
        not pre.wasSuccessful()
        or pre.testsRun == 0
        or len(pre.skipped) > 0
        or len(pre.expectedFailures) > 0
        or len(pre.unexpectedSuccesses) > 0
    ):
        raise RuntimeError("Pre-mutant behavior suite is not healthy green")

    orig_translate = prp.translate_storage_error
    orig_validate_residency = prp.validate_ram_root_residency
    orig_preflight = prp.preflight_workspace_relpath
    orig_validate_no_pixels = prp.validate_metadata_no_pixels
    orig_validate_seat = prp.validate_closed_seat_record
    orig_join_refs = prp.join_bound_references
    orig_close = prp._ManagedRingStorage.close
    orig_admit_stream = prp.BStorageRamPolicyAdapter.admit_stream
    orig_admit_frame = prp.BStorageRamPolicyAdapter.admit_frame
    orig_purge_stream = prp.BStorageRamPolicyAdapter.purge_stream
    orig_purge_run = prp.BStorageRamPolicyAdapter.purge_run
    orig_project_receipt = prp.BStorageRamPolicyAdapter.project_metadata_receipt
    orig_require_live_root = prp.BStorageRamPolicyAdapter._require_live_root
    orig_fid_as_tuple = prp.FrameIdentity.as_tuple
    orig_meas_post_init = prp.MeasurementRecord.__post_init__

    def _mut_1_allow_file_backed_root():
        prp.validate_ram_root_residency = lambda frames_root, frame_run_root, run_id, **kw: "tmpfs"

    def _restore_1():
        prp.validate_ram_root_residency = orig_validate_residency

    def _mut_2_wrong_escape_error_code():
        def _bad_translate(exc, *, default_code="E_BINDING"):
            if isinstance(exc, ramws.RamError) and exc.code in ("E_RAM_NOT_TMPFS", "E_RAM_ESCAPE"):
                return prp.RamPolicyError("E_WIRE", str(exc))
            return orig_translate(exc, default_code=default_code)
        prp.translate_storage_error = _bad_translate

    def _restore_2():
        prp.translate_storage_error = orig_translate

    def _mut_3_wrong_quota_error_code():
        def _bad_translate(exc, *, default_code="E_BINDING"):
            if isinstance(exc, wsb.QuotaError):
                return prp.RamPolicyError("E_BINDING", str(exc))
            return orig_translate(exc, default_code=default_code)
        prp.translate_storage_error = _bad_translate

    def _restore_3():
        prp.translate_storage_error = orig_translate

    def _mut_4_ignore_over_reservation():
        def _lax_admit_stream(self, root_auth, handle, spec, *, configured_reservation_bytes=None, relative_workspace_dir=None):
            return orig_admit_stream(
                self,
                root_auth,
                handle,
                wsb.StreamSpec(spec.width, spec.height, spec.slots, rtring.ring_bytes(spec.slots, spec.width, spec.height)),
                configured_reservation_bytes=None,
                relative_workspace_dir=relative_workspace_dir,
            )
        prp.BStorageRamPolicyAdapter.admit_stream = _lax_admit_stream

    def _restore_4():
        prp.BStorageRamPolicyAdapter.admit_stream = orig_admit_stream

    def _mut_5_drop_charge_on_close_with_borrow():
        def _buggy_close(self):
            self._released = True
            return True
        prp._ManagedRingStorage.close = _buggy_close

    def _restore_5():
        prp._ManagedRingStorage.close = orig_close

    def _mut_6_allow_purge_with_borrowed_view():
        def _lax_purge_stream(self, root_auth, token):
            return {"event": "purge_stream", "purged": True, "bank_reserved_bytes": 0, "workspace_used_bytes": 0, "active_borrows": 0}
        prp.BStorageRamPolicyAdapter.purge_stream = _lax_purge_stream

    def _restore_6():
        prp.BStorageRamPolicyAdapter.purge_stream = orig_purge_stream

    def _mut_7_accept_fabricated_cleanup():
        def _lax_purge_run(self, root_auth, *, fabricated_cleanup_evidence=None):
            if fabricated_cleanup_evidence is not None:
                w = prp.CleanupAuthorityWitness(root_auth.run_id, root_auth.root_generation, self._adapter_id, 0, 0, True, "0" * 64)
                return w, self.workspace.receipt()
            return orig_purge_run(self, root_auth, fabricated_cleanup_evidence=fabricated_cleanup_evidence)
        prp.BStorageRamPolicyAdapter.purge_run = _lax_purge_run

    def _restore_7():
        prp.BStorageRamPolicyAdapter.purge_run = orig_purge_run

    def _mut_8_mint_receipt_after_refusal():
        def _lax_project(self, **kwargs):
            saved = self._refusal_count
            self._refusal_count = 0
            try:
                return orig_project_receipt(self, **kwargs)
            finally:
                self._refusal_count = saved
        prp.BStorageRamPolicyAdapter.project_metadata_receipt = _lax_project

    def _restore_8():
        prp.BStorageRamPolicyAdapter.project_metadata_receipt = orig_project_receipt

    def _mut_9_send_on_refusal_in_admit_frame():
        def _leaky_admit_frame(self, root_auth, token, rgb, *, frame_seq, grant_credit_on_admit=1, channel=None):
            if channel is not None and isinstance(rgb, np.ndarray) and rgb.flags.writeable:
                channel.send({"type": "LEAKED_ON_REFUSAL", "frame_seq": frame_seq})
            return orig_admit_frame(
                self, root_auth, token, rgb, frame_seq=frame_seq, grant_credit_on_admit=grant_credit_on_admit, channel=channel
            )
        prp.BStorageRamPolicyAdapter.admit_frame = _leaky_admit_frame

    def _restore_9():
        prp.BStorageRamPolicyAdapter.admit_frame = orig_admit_frame

    def _mut_10_swap_frame_identity_digests():
        prp.FrameIdentity.as_tuple = lambda self: (
            self.frame_seq,
            self.width,
            self.height,
            self.binding_sha256,
            self.rgb24_sha256,
        )

    def _restore_10():
        prp.FrameIdentity.as_tuple = orig_fid_as_tuple

    def _mut_11_allow_nearest_timestamp_join():
        def _lax_join(producer_refs, consumer_refs, *, join_mode="exact_binding"):
            return tuple(zip(producer_refs, consumer_refs))
        prp.join_bound_references = _lax_join

    def _restore_11():
        prp.join_bound_references = orig_join_refs

    def _mut_12_allow_pixels_in_metadata():
        prp.validate_metadata_no_pixels = lambda record, **kw: True

    def _restore_12():
        prp.validate_metadata_no_pixels = orig_validate_no_pixels

    def _mut_13_allow_extra_seat_keys():
        prp.validate_closed_seat_record = lambda record, **kw: record

    def _restore_13():
        prp.validate_closed_seat_record = orig_validate_seat

    def _mut_14_allow_zero_for_unknown_measurement():
        prp.MeasurementRecord.__post_init__ = lambda self: None

    def _restore_14():
        prp.MeasurementRecord.__post_init__ = orig_meas_post_init

    def _mut_15_accept_stale_root_generation():
        def _lax_live_root(self, root_auth):
            if not isinstance(root_auth, prp.AuthenticatedRunRoot):
                raise prp.RamPolicyError("E_ROOT", "invalid root")
            return root_auth
        prp.BStorageRamPolicyAdapter._require_live_root = _lax_live_root

    def _restore_15():
        prp.BStorageRamPolicyAdapter._require_live_root = orig_require_live_root

    def _mut_16_skip_preflight_relpath():
        prp.preflight_workspace_relpath = lambda ws_root, rel: rel

    def _restore_16():
        prp.preflight_workspace_relpath = orig_preflight

    mutants = (
        ("mut_01_allow_file_backed_root", _mut_1_allow_file_backed_root, _restore_1),
        ("mut_02_wrong_escape_error_code", _mut_2_wrong_escape_error_code, _restore_2),
        ("mut_03_wrong_quota_error_code", _mut_3_wrong_quota_error_code, _restore_3),
        ("mut_04_ignore_over_reservation", _mut_4_ignore_over_reservation, _restore_4),
        ("mut_05_drop_charge_on_close_with_borrow", _mut_5_drop_charge_on_close_with_borrow, _restore_5),
        ("mut_06_allow_purge_with_borrowed_view", _mut_6_allow_purge_with_borrowed_view, _restore_6),
        ("mut_07_accept_fabricated_cleanup", _mut_7_accept_fabricated_cleanup, _restore_7),
        ("mut_08_mint_receipt_after_refusal", _mut_8_mint_receipt_after_refusal, _restore_8),
        ("mut_09_send_on_refusal_in_admit_frame", _mut_9_send_on_refusal_in_admit_frame, _restore_9),
        ("mut_10_swap_frame_identity_digests", _mut_10_swap_frame_identity_digests, _restore_10),
        ("mut_11_allow_nearest_timestamp_join", _mut_11_allow_nearest_timestamp_join, _restore_11),
        ("mut_12_allow_pixels_in_metadata", _mut_12_allow_pixels_in_metadata, _restore_12),
        ("mut_13_allow_extra_seat_keys", _mut_13_allow_extra_seat_keys, _restore_13),
        ("mut_14_allow_zero_for_unknown_measurement", _mut_14_allow_zero_for_unknown_measurement, _restore_14),
        ("mut_15_accept_stale_root_generation", _mut_15_accept_stale_root_generation, _restore_15),
        ("mut_16_skip_preflight_relpath", _mut_16_skip_preflight_relpath, _restore_16),
    )

    killed = 0
    survivors: list[str] = []
    details: list[dict[str, object]] = []

    for name, apply_fn, restore_fn in mutants:
        try:
            apply_fn()
            res = _run_behavior_suite()
        finally:
            restore_fn()
            ramws._close_all()

        semantic_kill = len(res.failures) > 0 and len(res.errors) == 0
        if semantic_kill:
            killed += 1
        else:
            survivors.append(name)
        details.append({
            "mutant": name,
            "semantic_kill": semantic_kill,
            "failures": len(res.failures),
            "errors": len(res.errors),
        })

    reproof = _run_behavior_suite()
    if (
        not reproof.wasSuccessful()
        or reproof.testsRun != pre.testsRun
        or len(reproof.skipped) > 0
        or len(reproof.expectedFailures) > 0
        or len(reproof.unexpectedSuccesses) > 0
    ):
        raise RuntimeError("Post-mutant restoration reproof failed")

    return {
        "pre_green_tests": pre.testsRun,
        "killed": killed,
        "applicable": len(mutants),
        "survivors": survivors,
        "equivalent_exclusions": 0,
        "reproof_green_tests": reproof.testsRun,
        "details": details,
    }


class TestP1VaRamPolicySemanticMutants(unittest.TestCase):
    """Driver test executing the semantic mutation suite and verifying 16/16 kills and healthy reproof."""

    def test_semantic_mutation_kills_and_reproof(self) -> None:
        report = run_semantic_mutation_suite()
        self.assertEqual(report["pre_green_tests"], 9)
        self.assertEqual(report["killed"], 16)
        self.assertEqual(report["applicable"], 16)
        self.assertEqual(report["survivors"], [])
        self.assertEqual(report["equivalent_exclusions"], 0)
        self.assertEqual(report["reproof_green_tests"], 9)


if __name__ == "__main__":
    unittest.main()
