"""Focused unit, property, and concurrency tests for vadapt.pslot_frame_ref (jq3-e-pslot-provider-transports)."""

from __future__ import annotations

import hashlib
import random
import struct
import threading
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np


def _lazy_pslot_frame_ref():
    from vadapt import pslot_frame_ref

    return pslot_frame_ref


def _lazy_xfb():
    from vadapt import xfb

    return xfb


def _grad_pixel(x: int, y: int) -> Tuple[int, int, int]:
    return ((x * 37 + y) % 256, (y * 53 + 7) % 256, (x * y) % 256)


def _build_rgb_array(width: int, height: int, pixel_fn: Callable[[int, int], Tuple[int, int, int]] = _grad_pixel) -> np.ndarray:
    arr = np.empty((height, width, 3), dtype=np.uint8, order="C")
    for y in range(height):
        for x in range(width):
            r, g, b = pixel_fn(x, y)
            arr[y, x, 0] = r
            arr[y, x, 1] = g
            arr[y, x, 2] = b
    return arr


def _build_xwd(
    w: int,
    h: int,
    pixel_fn: Callable[[int, int], Tuple[int, int, int]] = _grad_pixel,
    *,
    name: bytes = b"Xvfb main window\x00",
    ncolors: int = 0,
    byte_order: int = 0,
    pad: int = 0,
) -> bytes:
    xfb = _lazy_xfb()
    hs = xfb.MIN_HEADER + len(name)
    bpl = w * 4 + pad
    head = (
        struct.pack(
            ">25I",
            hs,
            7,
            2,
            24,
            w,
            h,
            0,
            byte_order,
            32,
            byte_order,
            32,
            32,
            bpl,
            4,
            0xFF0000,
            0xFF00,
            0xFF,
            8,
            ncolors,
            ncolors,
            w,
            h,
            0,
            0,
            0,
        )
        + name
    )
    body = bytearray(ncolors * xfb.COLOR_ENTRY)
    for y in range(h):
        row = bytearray(bpl)
        for x in range(w):
            r, g, b = pixel_fn(x, y)
            row[x * 4 : x * 4 + 4] = bytes((b, g, r, 0)) if byte_order == 0 else bytes((0, r, g, b))
        body += row
    return head + bytes(body)


def _find_minimal_counterexample(
    seed: int,
    candidates: List[Dict[str, Any]],
    predicate: Callable[[Dict[str, Any]], bool],
) -> Optional[Dict[str, Any]]:
    """Evaluate candidates in order and shrink to minimal failing case by area then coordinate sum."""
    failing: List[Dict[str, Any]] = []
    for case in candidates:
        if not predicate(case):
            failing.append(case)
    if not failing:
        return None
    failing.sort(
        key=lambda c: (
            c.get("w", 0) * c.get("h", 0),
            c.get("roi_w", 0) * c.get("roi_h", 0),
            c.get("roi_x", 0) + c.get("roi_y", 0),
            c.get("case_index", 0),
        )
    )
    minimal = dict(failing[0])
    minimal["seed"] = seed
    return minimal


class TestPslotFrameRefContract(unittest.TestCase):
    """Tests for vadapt.pslot_frame_ref public contract, digests, ROI bounds, and immutability."""

    def test_module_exports_and_scope_boundaries(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        self.assertEqual(pfr.AUSENTE, "AUSENTE")
        self.assertEqual(pfr.RGB_CHANNELS, 3)
        for name in (
            "FrameRef",
            "RamFrameRef",
            "RoiBox",
            "FrameProvenance",
            "FrameRefError",
            "FrameFormatError",
            "FrameGeometryError",
            "RoiValidationError",
            "RoiBoundsError",
            "FrameImmutabilityError",
            "admit_frame",
            "admit_xwd_frame",
            "validate_roi",
            "compute_rgb24_sha256",
        ):
            self.assertTrue(hasattr(pfr, name), f"missing export {name}")
        # Explicitly verify module does not own provider transport, PNG encoding, or registry.
        for forbidden in ("ProviderTransport", "encode_png", "ProviderRegistry"):
            self.assertFalse(hasattr(pfr, forbidden), f"unexpected out-of-scope export {forbidden}")

    def test_independent_byte_level_digest_expectations(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        xfb = _lazy_xfb()

        # 1x1 exact RGB pixel [0x12, 0x34, 0x56]
        raw_1x1 = bytes([0x12, 0x34, 0x56])
        expected_1x1 = hashlib.sha256(raw_1x1).hexdigest()
        arr_1x1 = np.array([[[0x12, 0x34, 0x56]]], dtype=np.uint8)
        ref_1x1 = pfr.FrameRef(arr_1x1)
        self.assertEqual(ref_1x1.sha256, expected_1x1)
        self.assertEqual(ref_1x1.frame_sha256, expected_1x1)
        self.assertEqual(ref_1x1.digest, expected_1x1)
        self.assertEqual(ref_1x1.root_sha256, expected_1x1)
        self.assertIsNone(ref_1x1.parent_sha256)
        self.assertEqual(ref_1x1.dimensions, (1, 1))
        self.assertEqual(ref_1x1.shape, (1, 1, 3))
        self.assertEqual(ref_1x1.rgb24_bytes(), raw_1x1)
        self.assertTrue(ref_1x1.verify_integrity())

        # 8x4 canonical XWD conformance vector from vadapt.xfb
        arr_8x4 = _build_rgb_array(8, 4, _grad_pixel)
        expected_8x4 = "c210bbb9913a93fa4c00c4e7af1781f2249d048e155b127d2f6e9663f5ca69ed"
        self.assertEqual(hashlib.sha256(arr_8x4.tobytes()).hexdigest(), expected_8x4)
        ref_8x4 = pfr.FrameRef.from_array(arr_8x4)
        self.assertEqual(ref_8x4.sha256, expected_8x4)

        # Single-bit perturbation negative control: flipping 1 bit changes sha256
        perturbed = arr_8x4.copy()
        perturbed[2, 5, 1] ^= 0x01
        ref_perturbed = pfr.FrameRef.from_array(perturbed)
        self.assertNotEqual(ref_perturbed.sha256, ref_8x4.sha256)
        self.assertFalse(ref_8x4.same_pixels(ref_perturbed))

        # Verify against xfb.rgb24 and xfb.frame_sha256 across different XWD layouts
        xwd_lsb = _build_xwd(8, 4, _grad_pixel, byte_order=0)
        xwd_msb = _build_xwd(8, 4, _grad_pixel, byte_order=1, pad=12, ncolors=16, name=b"alt\x00")
        ref_lsb = pfr.FrameRef.from_xwd(xwd_lsb)
        ref_msb = pfr.FrameRef.from_xwd(xwd_msb)
        self.assertEqual(ref_lsb.sha256, expected_8x4)
        self.assertEqual(ref_msb.sha256, expected_8x4)
        self.assertEqual(ref_lsb.sha256, xfb.frame_sha256(xwd_lsb))
        self.assertEqual(ref_msb.sha256, xfb.frame_sha256(xwd_msb))
        self.assertTrue(ref_lsb.same_pixels(ref_8x4))
        self.assertTrue(ref_msb.same_pixels(ref_8x4))

    def test_container_independent_equal_hashes(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        w, h = 7, 5
        arr = _build_rgb_array(w, h)
        raw_bytes = arr.tobytes()
        raw_ba = bytearray(raw_bytes)
        mv_1d = memoryview(raw_bytes)
        mv_3d = memoryview(arr)
        xwd_buf = _build_xwd(w, h, pad=8, byte_order=1)

        refs = [
            pfr.FrameRef(arr),
            pfr.FrameRef.from_array(arr, width=w, height=h),
            pfr.FrameRef.from_bytes(raw_bytes, w, h),
            pfr.FrameRef.from_rgb24(raw_ba, w, h),
            pfr.FrameRef(mv_1d, width=w, height=h),
            pfr.FrameRef(mv_3d, width=w, height=h),
            pfr.admit_xwd_frame(xwd_buf),
        ]
        expected_sha = hashlib.sha256(raw_bytes).hexdigest()
        for idx, ref in enumerate(refs):
            self.assertEqual(ref.sha256, expected_sha, f"container index {idx} digest mismatch")
            self.assertEqual(ref.rgb24_bytes(), raw_bytes, f"container index {idx} bytes mismatch")
            self.assertEqual(ref.dimensions, (w, h))
            self.assertEqual(pfr.compute_rgb24_sha256(ref), expected_sha)

    def test_mutation_after_admission_does_not_alter_evidence(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        w, h = 6, 4
        caller_arr = _build_rgb_array(w, h)
        expected_bytes = caller_arr.tobytes()
        expected_sha = hashlib.sha256(expected_bytes).hexdigest()

        ref_arr = pfr.FrameRef(caller_arr)
        # Mutate caller numpy array in place after admission
        caller_arr[:] = 255
        caller_arr[0, 0, :] = 0
        self.assertEqual(ref_arr.sha256, expected_sha)
        self.assertEqual(ref_arr.rgb24_bytes(), expected_bytes)
        self.assertEqual(ref_arr.pixel_at(0, 0), _grad_pixel(0, 0))
        self.assertTrue(ref_arr.verify_integrity())

        # Mutate caller bytearray in place after admission
        caller_ba = bytearray(expected_bytes)
        ref_ba = pfr.FrameRef.from_bytes(caller_ba, w, h)
        for i in range(len(caller_ba)):
            caller_ba[i] ^= 0xFF
        self.assertEqual(ref_ba.sha256, expected_sha)
        self.assertEqual(ref_ba.rgb24_bytes(), expected_bytes)
        self.assertTrue(ref_ba.verify_integrity())

        # Mutate caller memoryview backing buffer after admission
        caller_ba2 = bytearray(expected_bytes)
        caller_mv = memoryview(caller_ba2)
        ref_mv = pfr.FrameRef(caller_mv, width=w, height=h)
        caller_mv[0] = (caller_mv[0] + 1) % 256
        self.assertEqual(ref_mv.sha256, expected_sha)
        self.assertEqual(ref_mv.rgb24_bytes(), expected_bytes)

        # Returned array view and memoryview must be read-only
        with self.assertRaises(ValueError):
            ref_arr.pixels[0, 0, 0] = 99
        with self.assertRaises(ValueError):
            ref_arr.as_readonly_array()[1, 1, 2] = 42
        with self.assertRaises(TypeError):
            ref_arr.rgb_view[0] = 17

        # Detached copy_array() is writable without affecting FrameRef
        detached = ref_arr.copy_array()
        detached[:] = 123
        self.assertEqual(ref_arr.sha256, expected_sha)
        self.assertEqual(ref_arr.pixel_at(2, 1), _grad_pixel(2, 1))

        # Attribute mutation and deletion on FrameRef, RoiBox, and FrameProvenance are refused
        with self.assertRaises(pfr.FrameImmutabilityError) as cm_ref:
            ref_arr.sha256 = "0" * 64  # type: ignore[misc]
        self.assertEqual(cm_ref.exception.code, pfr.E_IMMUTABLE)
        with self.assertRaises(pfr.FrameImmutabilityError):
            del ref_arr.width  # type: ignore[misc]
        with self.assertRaises(pfr.FrameImmutabilityError) as cm_roi:
            ref_arr.roi.x = 99  # type: ignore[misc]
        self.assertEqual(cm_roi.exception.code, pfr.E_IMMUTABLE)
        with self.assertRaises(pfr.FrameImmutabilityError) as cm_prov:
            ref_arr.provenance.sha256 = "0" * 64  # type: ignore[misc]
        self.assertEqual(cm_prov.exception.code, pfr.E_IMMUTABLE)

    def test_input_validation_controls_and_single_condition_negatives(self) -> None:
        pfr = _lazy_pslot_frame_ref()

        # Passing real-module control
        valid_arr = _build_rgb_array(4, 3)
        control_ref = pfr.FrameRef(valid_arr)
        self.assertEqual(control_ref.dimensions, (4, 3))

        # Single-condition negative: None input
        with self.assertRaises(pfr.FrameFormatError) as cm:
            pfr.FrameRef(None)  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, pfr.E_FRAME_NULL)

        # Single-condition negative: non-buffer containers
        for bad_container in ([1, 2, 3], (1, 2, 3), {"a": 1}, "rgb", 123, True, False):
            with self.assertRaises(pfr.FrameFormatError, msg=f"container={bad_container!r}") as cm:
                pfr.FrameRef(bad_container)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, pfr.E_FRAME_TYPE)

        # Single-condition negative: non-uint8 dtypes (including bool)
        for bad_dtype in (np.bool_, np.int8, np.uint16, np.int32, np.int64, np.float32, np.float64, object):
            bad_arr = np.zeros((3, 4, 3), dtype=bad_dtype)
            with self.assertRaises(pfr.FrameFormatError, msg=f"dtype={bad_dtype}") as cm:
                pfr.FrameRef(bad_arr)
            self.assertEqual(cm.exception.code, pfr.E_FRAME_DTYPE)

        # Single-condition negative: non-C-contiguous arrays
        fortran_arr = np.asfortranarray(valid_arr)
        strided_arr = valid_arr[:, ::2, :]
        transposed_arr = valid_arr.transpose(1, 0, 2)
        reversed_arr = valid_arr[:, ::-1, :]
        for label, nc_arr in (
            ("fortran", fortran_arr),
            ("strided", strided_arr),
            ("transposed", transposed_arr),
            ("reversed", reversed_arr),
        ):
            with self.assertRaises(pfr.FrameFormatError, msg=label) as cm:
                pfr.FrameRef(nc_arr)
            self.assertEqual(cm.exception.code, pfr.E_FRAME_CONTIGUOUS)

        # Single-condition negative: non-contiguous or non-uint8 memoryview
        with self.assertRaises(pfr.FrameFormatError) as cm:
            pfr.FrameRef(memoryview(strided_arr), width=2, height=3)
        self.assertEqual(cm.exception.code, pfr.E_FRAME_CONTIGUOUS)

        int32_arr = np.zeros((3, 4, 3), dtype=np.int32)
        with self.assertRaises(pfr.FrameFormatError) as cm:
            pfr.FrameRef(memoryview(int32_arr), width=4, height=3)
        self.assertEqual(cm.exception.code, pfr.E_FRAME_DTYPE)

        # Single-condition negative: invalid ndim or channel count or empty geometry
        bad_shapes = [
            (12,),
            (3, 4),
            (1, 3, 4, 3),
            (3, 4, 1),
            (3, 4, 2),
            (3, 4, 4),
            (3, 4, 0),
            (0, 4, 3),
            (3, 0, 3),
            (0, 0, 3),
        ]
        for shape in bad_shapes:
            arr = np.zeros(shape, dtype=np.uint8)
            with self.assertRaises(pfr.FrameGeometryError, msg=f"shape={shape}") as cm:
                pfr.FrameRef(arr)
            self.assertEqual(cm.exception.code, pfr.E_FRAME_GEOMETRY)

        # Single-condition negative: raw bytes missing or invalid width/height (including bool!)
        raw_ok = valid_arr.tobytes()
        with self.assertRaises(pfr.FrameGeometryError) as cm:
            pfr.FrameRef(raw_ok)
        self.assertEqual(cm.exception.code, pfr.E_FRAME_GEOMETRY)

        for bad_w, bad_h in (
            (True, 3),
            (4, True),
            (False, 3),
            (0, 3),
            (4, 0),
            (-4, 3),
            (4, -3),
            (4.0, 3),
            (4, "3"),
            (pfr.MAX_SIDE + 1, 1),
            (1, pfr.MAX_SIDE + 1),
        ):
            with self.assertRaises(pfr.FrameGeometryError, msg=f"w={bad_w!r} h={bad_h!r}") as cm:
                pfr.FrameRef.from_bytes(raw_ok, bad_w, bad_h)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, pfr.E_FRAME_GEOMETRY)

        # Single-condition negative: byte length mismatch (+1 / -1 byte)
        for bad_raw in (raw_ok[:-1], raw_ok + b"\x00", b""):
            with self.assertRaises(pfr.FrameGeometryError) as cm:
                pfr.FrameRef.from_bytes(bad_raw, 4, 3)
            self.assertEqual(cm.exception.code, pfr.E_FRAME_GEOMETRY)

        # Single-condition negative: explicit width/height mismatching ndarray shape
        with self.assertRaises(pfr.FrameGeometryError) as cm:
            pfr.FrameRef(valid_arr, width=5, height=3)
        self.assertEqual(cm.exception.code, pfr.E_FRAME_GEOMETRY)

    def test_roi_boundaries_bool_rejection_and_nested_provenance(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        w, h = 10, 8
        root_arr = _build_rgb_array(w, h)
        root_ref = pfr.FrameRef(root_arr)

        # Full-frame crop boundary control
        full_crop = root_ref.crop(0, 0, w, h)
        self.assertEqual(full_crop.sha256, root_ref.sha256)
        self.assertEqual(full_crop.root_sha256, root_ref.sha256)
        self.assertEqual(full_crop.parent_sha256, root_ref.sha256)
        self.assertTrue(full_crop.is_crop)
        self.assertEqual(full_crop.crop_depth, 1)
        self.assertEqual(full_crop.roi_in_root, pfr.RoiBox(0, 0, w, h))

        # 4 exact 1x1 corners boundary control
        corners = [
            (0, 0, 1, 1),
            (w - 1, 0, 1, 1),
            (0, h - 1, 1, 1),
            (w - 1, h - 1, 1, 1),
        ]
        for cx, cy, cw, ch in corners:
            c_ref = root_ref.crop(cx, cy, cw, ch)
            self.assertEqual(c_ref.dimensions, (1, 1))
            self.assertEqual(c_ref.pixel_at(0, 0), _grad_pixel(cx, cy))
            expected_px_bytes = bytes(_grad_pixel(cx, cy))
            self.assertEqual(c_ref.sha256, hashlib.sha256(expected_px_bytes).hexdigest())
            self.assertEqual(c_ref.root_sha256, root_ref.sha256)
            self.assertEqual(c_ref.crop_to_root_point(0, 0), (cx, cy))
            self.assertEqual(c_ref.root_to_crop_point(cx, cy), (0, 0))

        # Multi-step nested crop provenance & coordinate transforms
        crop1 = root_ref.crop(2, 1, 6, 5)
        crop2 = crop1.crop({"x": 1, "y": 2, "width": 3, "height": 2})
        direct_crop2 = root_ref.crop(3, 3, 3, 2)
        standalone_crop2 = pfr.FrameRef(np.ascontiguousarray(root_arr[3:5, 3:6, :]))

        self.assertEqual(crop2.sha256, direct_crop2.sha256)
        self.assertEqual(crop2.sha256, standalone_crop2.sha256)
        self.assertEqual(crop2.root_sha256, root_ref.sha256)
        self.assertEqual(crop2.full_frame_sha256, root_ref.sha256)
        self.assertEqual(crop2.parent_sha256, crop1.sha256)
        self.assertEqual(crop2.root_dimensions, (w, h))
        self.assertEqual(crop2.dimensions, (3, 2))
        self.assertEqual(crop2.roi_in_root, pfr.RoiBox(3, 3, 3, 2))
        self.assertEqual(crop2.roi_in_parent, pfr.RoiBox(1, 2, 3, 2))
        self.assertEqual(crop2.crop_depth, 2)
        self.assertEqual(crop2.crop_chain, (pfr.RoiBox(2, 1, 6, 5), pfr.RoiBox(1, 2, 3, 2)))

        # Verify coordinate transforms for every pixel in crop2
        for ly in range(crop2.height):
            for lx in range(crop2.width):
                px, py = crop2.crop_to_parent_point(lx, ly)
                rx, ry = crop2.crop_to_root_point(lx, ly)
                self.assertEqual((px, py), (1 + lx, 2 + ly))
                self.assertEqual((rx, ry), (3 + lx, 3 + ly))
                self.assertEqual(crop2.parent_to_crop_point(px, py), (lx, ly))
                self.assertEqual(crop2.root_to_crop_point(rx, ry), (lx, ly))
                self.assertEqual(crop2.pixel_at(lx, ly), crop1.pixel_at(px, py))
                self.assertEqual(crop2.pixel_at(lx, ly), root_ref.pixel_at(rx, ry))

        # ROI box transforms between crop2 and root
        sub_local_roi = pfr.RoiBox(1, 0, 2, 2)
        sub_root_roi = crop2.crop_to_root_roi(sub_local_roi)
        self.assertEqual(sub_root_roi, pfr.RoiBox(4, 3, 2, 2))
        self.assertEqual(crop2.root_to_crop_roi(sub_root_roi), sub_local_roi)

        # Single-condition ROI negatives: bool values in any position
        bool_rois = [
            (True, 0, 2, 2),
            (0, False, 2, 2),
            (0, 0, True, 2),
            (0, 0, 2, True),
            (np.bool_(False), 0, 2, 2),
            {"x": True, "y": 0, "width": 2, "height": 2},
            {"x0": 0, "y0": 0, "x1": True, "y1": 2},
        ]
        for b_roi in bool_rois:
            with self.assertRaises(pfr.RoiBoundsError, msg=f"bool_roi={b_roi!r}") as cm:
                if isinstance(b_roi, tuple):
                    root_ref.crop(*b_roi)
                else:
                    root_ref.crop(b_roi)
            self.assertEqual(cm.exception.code, pfr.E_ROI_TYPE)

        # Single-condition ROI negatives: non-int types
        for non_int_roi in ((1.0, 0, 2, 2), (0, "1", 2, 2), (0, 0, None, 2), (1, 2, 3)):
            with self.assertRaises(pfr.RoiBoundsError, msg=f"non_int_roi={non_int_roi!r}") as cm:
                root_ref.crop(non_int_roi)
            self.assertEqual(cm.exception.code, pfr.E_ROI_TYPE)

        # Single-condition ROI negatives: empty geometry
        empty_rois = [
            (0, 0, 0, 2),
            (0, 0, 2, 0),
            (0, 0, -1, 2),
            (0, 0, 2, -1),
        ]
        for e_roi in empty_rois:
            with self.assertRaises(pfr.RoiBoundsError, msg=f"empty_roi={e_roi!r}") as cm:
                root_ref.crop(*e_roi)
            self.assertEqual(cm.exception.code, pfr.E_ROI_EMPTY)

        with self.assertRaises(pfr.RoiBoundsError) as cm:
            root_ref.crop_xyxy(4, 2, 4, 5)
        self.assertEqual(cm.exception.code, pfr.E_ROI_EMPTY)
        with self.assertRaises(pfr.RoiBoundsError) as cm:
            root_ref.crop_xyxy(4, 5, 6, 3)
        self.assertEqual(cm.exception.code, pfr.E_ROI_EMPTY)

        # Single-condition ROI negatives: negative or outside bounds
        oob_rois = [
            (-1, 0, 2, 2),
            (0, -1, 2, 2),
            (w, 0, 1, 1),
            (0, h, 1, 1),
            (0, 0, w + 1, h),
            (0, 0, w, h + 1),
            (w - 2, 0, 3, 2),
            (0, h - 2, 2, 3),
        ]
        for oob in oob_rois:
            with self.assertRaises(pfr.RoiBoundsError, msg=f"oob={oob!r}") as cm:
                root_ref.crop(*oob)
            self.assertEqual(cm.exception.code, pfr.E_ROI_BOUNDS)

        # Single-condition coordinate transform negatives: bool or out-of-bounds
        with self.assertRaises(pfr.RoiBoundsError) as cm:
            crop2.crop_to_root_point(True, 0)
        self.assertEqual(cm.exception.code, pfr.E_COORD_BOUNDS)
        with self.assertRaises(pfr.RoiBoundsError) as cm:
            crop2.crop_to_root_point(crop2.width, 0)
        self.assertEqual(cm.exception.code, pfr.E_COORD_BOUNDS)
        with self.assertRaises(pfr.RoiBoundsError) as cm:
            crop2.root_to_crop_point(2, 3)
        self.assertEqual(cm.exception.code, pfr.E_COORD_BOUNDS)
        with self.assertRaises(pfr.RoiBoundsError) as cm:
            crop2.root_to_crop_roi(0, 0, w, h)
        self.assertEqual(cm.exception.code, pfr.E_ROI_BOUNDS)

    def test_seeded_bounded_synthetic_properties_and_minimal_counterexample(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        seed = 20261020
        rng = random.Random(seed)
        candidates: List[Dict[str, Any]] = []

        for idx in range(64):
            w = rng.randint(2, 24)
            h = rng.randint(2, 24)
            rx = rng.randint(0, w - 1)
            ry = rng.randint(0, h - 1)
            rw = rng.randint(1, w - rx)
            rh = rng.randint(1, h - ry)
            sx = rng.randint(0, rw - 1)
            sy = rng.randint(0, rh - 1)
            sw = rng.randint(1, rw - sx)
            sh = rng.randint(1, rh - sy)
            candidates.append(
                {
                    "case_index": idx,
                    "w": w,
                    "h": h,
                    "roi_x": rx,
                    "roi_y": ry,
                    "roi_w": rw,
                    "roi_h": rh,
                    "sub_x": sx,
                    "sub_y": sy,
                    "sub_w": sw,
                    "sub_h": sh,
                }
            )

        def check_invariant(case: Dict[str, Any]) -> bool:
            w, h = case["w"], case["h"]
            rx, ry, rw, rh = case["roi_x"], case["roi_y"], case["roi_w"], case["roi_h"]
            sx, sy, sw, sh = case["sub_x"], case["sub_y"], case["sub_w"], case["sub_h"]

            arr = _build_rgb_array(
                w,
                h,
                lambda x, y: (
                    (x * 19 + y * 7 + case["case_index"]) % 256,
                    (x * 3 + y * 41) % 256,
                    (x ^ y ^ case["case_index"]) % 256,
                ),
            )
            expected_root_bytes = arr.tobytes()
            expected_root_sha = hashlib.sha256(expected_root_bytes).hexdigest()

            root = pfr.FrameRef(arr)
            # Mutate caller array immediately after admission
            arr ^= 0xAA

            if root.sha256 != expected_root_sha:
                return False
            c1 = root.crop(rx, ry, rw, rh)
            c2 = c1.crop(sx, sy, sw, sh)
            direct_c2 = root.crop(rx + sx, ry + sy, sw, sh)

            if c2.sha256 != direct_c2.sha256:
                return False
            if c2.root_sha256 != expected_root_sha or c2.parent_sha256 != c1.sha256:
                return False
            if c2.roi_in_root != pfr.RoiBox(rx + sx, ry + sy, sw, sh):
                return False
            if c2.crop_to_root_point(sw - 1, sh - 1) != (rx + sx + sw - 1, ry + sy + sh - 1):
                return False
            if c2.pixel_at(sw - 1, sh - 1) != root.pixel_at(rx + sx + sw - 1, ry + sy + sh - 1):
                return False
            return root.verify_integrity() and c1.verify_integrity() and c2.verify_integrity()

        counterexample = _find_minimal_counterexample(seed, candidates, check_invariant)
        self.assertIsNone(
            counterexample,
            f"Property failure (seed={seed}, minimal_counterexample={counterexample})",
        )

        # Negative control on shrinking / minimal counterexample recording:
        # if a guard is bypassed for crops with area >= 4, the shrinker records the minimal failing case.
        broken_ce = _find_minimal_counterexample(
            seed,
            candidates,
            lambda c: (c["roi_w"] * c["roi_h"]) < 4,
        )
        self.assertIsNotNone(broken_ce)
        assert broken_ce is not None
        self.assertEqual(broken_ce["seed"], seed)
        self.assertGreaterEqual(broken_ce["roi_w"] * broken_ce["roi_h"], 4)

    def test_concurrent_admission_and_cropping_with_barriers_and_leak_check(self) -> None:
        pfr = _lazy_pslot_frame_ref()
        worker_count = 6
        w, h = 16, 12
        base_arr = _build_rgb_array(w, h)
        expected_root_sha = hashlib.sha256(base_arr.tobytes()).hexdigest()
        shared_root = pfr.FrameRef(base_arr)

        start_barrier = threading.Barrier(worker_count + 1, timeout=5.0)
        done_event = threading.Event()
        errors: List[BaseException] = []
        errors_lock = threading.Lock()

        baseline_threads = set(threading.enumerate())

        def worker(worker_id: int) -> None:
            try:
                local_arr = base_arr.copy()
                start_barrier.wait(timeout=5.0)
                for step in range(8):
                    admitted = pfr.FrameRef(local_arr)
                    # Mutate caller buffer concurrently right after admission
                    local_arr[step % h, (worker_id + step) % w, 0] ^= 0xFF
                    if admitted.sha256 != expected_root_sha and step == 0:
                        raise AssertionError("initial admitted digest mismatch")
                    # Restore for next iteration check
                    local_arr = base_arr.copy()

                    rx = worker_id % (w - 4)
                    ry = step % (h - 4)
                    crop = shared_root.crop(rx, ry, 4, 4)
                    expected_sub = np.ascontiguousarray(base_arr[ry : ry + 4, rx : rx + 4, :]).tobytes()
                    if crop.sha256 != hashlib.sha256(expected_sub).hexdigest():
                        raise AssertionError("concurrent crop digest mismatch")
                    if crop.root_sha256 != expected_root_sha:
                        raise AssertionError("concurrent crop root_sha256 mismatch")
                    if not crop.verify_integrity():
                        raise AssertionError("concurrent crop integrity check failed")
                done_event.set()
            except BaseException as exc:  # pragma: no cover
                with errors_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,), name=f"pslot-frame-ref-worker-{i}", daemon=True)
            for i in range(worker_count)
        ]
        for t in threads:
            t.start()

        start_barrier.wait(timeout=5.0)
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), f"worker thread {t.name} leaked / timed out")

        self.assertTrue(done_event.is_set())
        if errors:
            raise errors[0]

        remaining_threads = set(threading.enumerate()) - baseline_threads
        self.assertEqual(len(remaining_threads), 0, f"leaked threads: {remaining_threads}")
        self.assertTrue(shared_root.verify_integrity())


if __name__ == "__main__":
    unittest.main()
