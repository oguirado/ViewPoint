"""Independent offline G J2 2 controls for the real committed_frame API.

There are deliberately no expected failures, skips, live systems, or pixel files.
"""

from dataclasses import FrozenInstanceError
import hashlib
import json
from unittest import TestCase, main
from unittest.mock import patch
import zlib

import numpy as np

from vadapt.committed_frame import (
    BoundaryIdentity, BoundFrame, CaptureObservation, CommittedFrameError,
    PixelDeclaration, bind_committed_frame,
)

# 2x2 BGR, bottom-up, 8-byte stride: two padding bytes per row.
# Written inline so expected results never invoke the production normalizer.
RAW = bytes((90, 80, 70, 120, 110, 100, 238, 239,
             30, 20, 10, 60, 50, 40, 240, 241))
CANONICAL = bytes((10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120))
LATEST = bytes((1, 1, 1, 2, 2, 2, 238, 239, 3, 3, 3, 4, 4, 4, 240, 241))
STATE = hashlib.sha256(b"state:commit-3").hexdigest()
STATE_NEXT = hashlib.sha256(b"state:commit-4").hexdigest()


def pixel_declaration(**override):
    decl = dict(channel_order="BGR", stride_bytes=8, orientation="bottom-up",
                alpha="none", color_space="sRGB", width=2, height=2,
                dpr_ppm=1_000_000, zoom_ppm=1_000_000,
                environment_id="test-env", binding_id="publication-channel")
    decl.update(override)
    return decl


def request_identity(**override):
    ident = dict(source_kind="godot", handle_id="viewport-71", handle_generation=12,
                 epoch=7, width=2, height=2, environment_id="test-env",
                 binding_id="publication-channel", publication_id="pub-3",
                 presentation_revision=20, frame_seq=3, committed_tick=91,
                 state_sha256=STATE, observer_ref="observer@tick91")
    ident.update(override)
    return ident


def declaration_hash(decl):
    # Independent JSON canonicalization oracle for the C-PIXEL declaration.
    return hashlib.sha256(json.dumps(decl, separators=(",", ":"), sort_keys=True).encode("ascii")).hexdigest()


class Broker:
    """In-memory publisher and ring-sequence lease, no process/network/storage."""

    def __init__(self, *, raw=RAW, expected=CANONICAL, binding=None, decl=None,
                 provenance="godot"):
        self.decl = pixel_declaration() if decl is None else decl
        self.binding = request_identity() if binding is None else binding
        self.raw = np.frombuffer(bytearray(raw), dtype=np.uint8).copy()
        self.sequence = 18
        self.crc = zlib.crc32(raw)
        self.release_count = 0
        self.lookup_count = 0
        self.capture_count = 0
        self.revalidations = 0
        self.before_return = None
        self.on_lookup = None
        self.on_revalidate = None
        self.residency_verified = True
        self.coherent = True
        self.provenance = provenance
        self.publication = dict(
            identity=dict(self.binding), complete=True, residency_verified=True,
            pixel_decl_sha256=declaration_hash(self.decl),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            normalized_sha256=hashlib.sha256(expected).hexdigest(),
        )
        self.captured_identity = dict(self.binding)

    def lookup(self):
        self.lookup_count += 1
        if self.on_lookup is not None:
            self.on_lookup(self.lookup_count)
        return self.publication

    def capture(self):
        self.capture_count += 1

        def revalidate():
            self.revalidations += 1
            if self.on_revalidate:
                self.on_revalidate(self.revalidations)
            return (self.sequence, zlib.crc32(self.raw))

        def release():
            self.release_count += 1

        return CaptureObservation(self.raw, self.captured_identity, self.sequence, self.crc,
                                  revalidate, release, self.provenance,
                                  self.coherent, self.residency_verified)

    def bind(self, *, binding=None, pixel_decl=None):
        return bind_committed_frame(binding=self.binding if binding is None else binding,
                                    pixel_decl=self.decl if pixel_decl is None else pixel_decl,
                                    capture=self.capture, lookup_publication=self.lookup)


class TestCommittedFrameContract(TestCase):
    def assert_reject(self, broker, code, **kwargs):
        with self.assertRaises(CommittedFrameError) as raised:
            broker.bind(**kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_exact_pinned_control_owned_pixels_immutable_metadata_and_cleanup(self):
        broker = Broker()
        original_identity = dict(broker.binding)
        original_decl = dict(broker.decl)
        bound = broker.bind()
        self.assertIsInstance(bound, BoundFrame)
        self.assertEqual(bound.metadata.schema_version, "committed-frame-v1")
        self.assertEqual(bound.raw_bytes, RAW)
        self.assertEqual(bound.normalized_bytes, CANONICAL)
        self.assertEqual(bound.metadata.raw_sha256, hashlib.sha256(RAW).hexdigest())
        self.assertEqual(bound.metadata.normalized_sha256, hashlib.sha256(CANONICAL).hexdigest())
        self.assertEqual(bound.metadata.pixel_decl_sha256, declaration_hash(broker.decl))
        self.assertEqual((bound.metadata.frame_seq, bound.metadata.committed_tick), (3, 91))
        self.assertEqual((bound.metadata.state_sha256, bound.metadata.observer_ref),
                         (STATE, "observer@tick91"))
        self.assertEqual((bound.metadata.slot_sequence, bound.metadata.slot_crc32),
                         (18, zlib.crc32(RAW)))
        self.assertEqual((broker.capture_count, broker.release_count, broker.lookup_count),
                         (1, 1, 3))
        self.assertGreaterEqual(broker.revalidations, 3)
        broker.raw[:] = 222
        broker.binding["epoch"] = -1
        broker.decl["height"] = 3
        broker.captured_identity["frame_seq"] = 555
        self.assertEqual(bound.raw_bytes, RAW)
        self.assertEqual(bound.normalized_bytes, CANONICAL)
        self.assertEqual(bound.metadata.identity, BoundaryIdentity.validated(original_identity))
        self.assertEqual(bound.pixel_decl, PixelDeclaration.validated(original_decl))
        with self.assertRaises(FrozenInstanceError):
            bound.metadata.identity.frame_seq = 5
        with self.assertRaises(FrozenInstanceError):
            bound.pixel_decl.width = 8
        with self.assertRaises(TypeError):
            bound.raw_bytes[0] = 7

    def test_bad_declaration_or_absent_declaration_fails_before_looking_or_capturing(self):
        cases = [
            (dict(stride_bytes=5), "E_PIXEL_DECL"),
            (dict(orientation="sideways"), "E_PIXEL_DECL"),
            (dict(channel_order="JPEG"), "E_PIXEL_DECL"),
            (dict(alpha="straight"), "E_PIXEL_DECL"),
            (dict(color_space="display-p3"), "E_PIXEL_UNSUPPORTED"),
            (dict(width=True), "E_PIXEL_DECL"),
            (dict(dpr_ppm=0), "E_PIXEL_DECL"),
            (dict(zoom_ppm=False), "E_PIXEL_DECL"),
            (dict(binding_id=""), "E_PIXEL_DECL"),
        ]
        for override, code in cases:
            broker = Broker()
            with self.subTest(override=override):
                self.assert_reject(broker, code, pixel_decl=pixel_declaration(**override))
                self.assertEqual((broker.capture_count, broker.lookup_count), (0, 0))
        broker = Broker()
        self.assert_reject(broker, "E_PIXEL_DECL", pixel_decl={"width": 2})

    def test_declared_limit_and_declaration_binding_mismatch_have_no_capture(self):
        broker = Broker()
        with self.assertRaises(CommittedFrameError) as error:
            bind_committed_frame(binding=broker.binding, pixel_decl=broker.decl,
                                 capture=broker.capture, lookup_publication=broker.lookup,
                                 max_frame_bytes=len(RAW) - 1)
        self.assertEqual(error.exception.code, "E_PIXEL_LIMIT")
        self.assert_reject(broker, "E_PIXEL_BINDING", pixel_decl=pixel_declaration(binding_id="other"))
        self.assertEqual(broker.capture_count, 0)

    def test_missing_complete_publication_and_residency_are_blocked_before_capture(self):
        for changed, code in (({"complete": False}, "E_PUBLICATION_INCOMPLETE"),
                              ({"residency_verified": None}, "E_RESIDENCY_UNOBSERVED"),
                              ({"raw_sha256": None}, "E_IDENTITY")):
            broker = Broker()
            broker.publication.update(changed)
            with self.subTest(changed=changed):
                self.assert_reject(broker, code)
                self.assertEqual(broker.capture_count, 0)

    def test_stale_epoch_generation_geometry_commit_and_binding_refused_before_capture(self):
        for field, new_value in (("epoch", 8), ("handle_generation", 13), ("width", 3),
                                 ("publication_id", "pub-4"), ("committed_tick", 92),
                                 ("state_sha256", STATE_NEXT), ("observer_ref", "observer-next"),
                                 ("frame_seq", 4), ("presentation_revision", 21)):
            broker = Broker()
            broker.publication["identity"] = dict(broker.binding, **{field: new_value})
            with self.subTest(field=field):
                self.assert_reject(broker, "E_PUBLICATION_IDENTITY")
                self.assertEqual((broker.capture_count, broker.release_count), (0, 0))

    def test_raw_later_frame_cannot_be_relabeled_to_requested_commit(self):
        broker = Broker(raw=LATEST)
        # The trusted completed publication seals the earlier frame, regardless of
        # claimed identity carried by the capture adapter.
        broker.publication["raw_sha256"] = hashlib.sha256(RAW).hexdigest()
        broker.publication["normalized_sha256"] = hashlib.sha256(CANONICAL).hexdigest()
        self.assert_reject(broker, "E_FRAME_DIGEST")
        self.assertEqual(broker.release_count, 1)
        self.assertEqual(broker.binding["frame_seq"], 3)

    def test_captured_later_frame_identity_rejected_before_copy(self):
        broker = Broker()
        broker.captured_identity["frame_seq"] = 4
        self.assert_reject(broker, "E_CAPTURE_IDENTITY")
        self.assertEqual((broker.release_count, broker.revalidations), (1, 0))

    def test_same_tick_distinct_publication_or_revision_must_fail(self):
        broker = Broker()
        broker.on_lookup = lambda count: broker.publication["identity"].update(
            publication_id="pub-4") if count == 2 else None
        self.assert_reject(broker, "E_PUBLICATION_CHANGED")
        self.assertEqual(broker.release_count, 1)
        broker = Broker()
        broker.on_lookup = lambda count: broker.publication["identity"].update(
            presentation_revision=21) if count == 2 else None
        self.assert_reject(broker, "E_PUBLICATION_CHANGED")
        self.assertEqual(broker.binding["committed_tick"], 91)

    def test_race_epoch_changes_during_final_hash_never_return_frame(self):
        broker = Broker()
        broker.on_lookup = lambda count: broker.publication["identity"].update(
            epoch=8) if count == 3 else None
        self.assert_reject(broker, "E_PUBLICATION_CHANGED")
        self.assertEqual(broker.release_count, 1)

    def test_crc_mismatch_sequence_change_and_odd_sequence_fail_closed(self):
        broker = Broker()
        broker.crc ^= 1
        self.assert_reject(broker, "E_CAPTURE_TORN")
        self.assertEqual(broker.release_count, 1)
        broker = Broker()
        broker.on_revalidate = lambda count: setattr(broker, "sequence", 20) if count == 1 else None
        self.assert_reject(broker, "E_CAPTURE_TORN")
        broker = Broker()
        broker.sequence = 19
        self.assert_reject(broker, "E_CAPTURE_TORN")

    def test_unsupported_unobserved_source_and_provenance_fail_with_cleanup(self):
        for key, value, code in (("coherent", False, "E_CAPTURE_UNSUPPORTED"),
                                 ("residency_verified", False, "E_RESIDENCY_UNOBSERVED"),
                                 ("provenance", "cdp", "E_SOURCE_PROVENANCE")):
            broker = Broker()
            setattr(broker, key, value)
            with self.subTest(key=key):
                self.assert_reject(broker, code)
                self.assertEqual(broker.release_count, 1)

    def test_publisher_layout_cannot_be_substituted_after_declaration(self):
        broker = Broker()
        broker.publication["pixel_decl_sha256"] = hashlib.sha256(b"altered-layout").hexdigest()
        self.assert_reject(broker, "E_PIXEL_BINDING")
        self.assertEqual(broker.capture_count, 0)

    def test_raw_and_normalized_hashes_are_distinct_and_alpha_is_explicit(self):
        broker = Broker()
        bound = broker.bind()
        self.assertNotEqual(bound.metadata.raw_sha256, bound.metadata.normalized_sha256)
        rgba = bytes((20, 10, 5, 80, 255, 255, 255, 255))  # BGRA, two pixels
        want = bytes((5, 10, 20, 80, 255, 255, 255, 255))
        decl = pixel_declaration(channel_order="BGRA", stride_bytes=8, width=2,
                                 height=1, alpha="straight", orientation="top-down")
        ident = request_identity(height=1)
        trans = Broker(raw=rgba, expected=want, decl=decl, binding=ident)
        self.assertEqual(trans.bind().normalized_bytes, want)
        trans = Broker(raw=rgba, expected=want, decl=dict(decl, alpha="opaque"), binding=ident)
        self.assert_reject(trans, "E_PIXEL_ALPHA")

    def test_cdp_actual_fake_observation_requires_trusted_window_crop(self):
        class FakeCDP:
            """No network or screenshot API: explicit fake CDP DOM/AX responses."""
            def __init__(self):
                self.calls = []
            def call(self, method):
                self.calls.append(method)
                if method == "DOMSnapshot.captureSnapshot":
                    return {"documents": [{"nodes": {"nodeName": ["HTML", "BUTTON"]}}]}
                if method == "Accessibility.getFullAXTree":
                    return {"nodes": [{"role": {"value": "button"}}]}
                raise AssertionError("unexpected CDP method")

        fake = FakeCDP()
        dom = fake.call("DOMSnapshot.captureSnapshot")
        ax = fake.call("Accessibility.getFullAXTree")
        self.assertEqual(dom["documents"][0]["nodes"]["nodeName"][-1], "BUTTON")
        self.assertEqual(ax["nodes"][0]["role"]["value"], "button")
        self.assertEqual(len(fake.calls), 2)
        cdp_binding = request_identity(source_kind="cdp", window_id="win-77",
                                       crop=(0, 0, 2, 2))
        broker = Broker(binding=cdp_binding, provenance="cdp")
        self.assertEqual(broker.bind().metadata.identity.window_id, "win-77")
        bad = Broker(binding=cdp_binding, provenance="godot")
        self.assert_reject(bad, "E_SOURCE_PROVENANCE")
        bad = Broker(binding=cdp_binding, provenance="cdp")
        self.assert_reject(bad, "E_UNTRUSTED_CROP", binding=request_identity(
            source_kind="cdp", window_id="win-77", crop=(0, 0, 1, 2)))
        self.assertEqual(bad.capture_count, 0)
        bad = Broker(binding=cdp_binding, provenance="cdp")
        bad.captured_identity["window_id"] = "wrong-window"
        self.assert_reject(bad, "E_CAPTURE_IDENTITY")
        self.assertEqual(bad.release_count, 1)

    def test_write_spies_prohibit_disk_swap_or_screenshot_persistence(self):
        broker = Broker()
        with patch("builtins.open", side_effect=AssertionError("no disk pixel writes")), \
             patch("os.open", side_effect=AssertionError("no disk pixel writes")), \
             patch("numpy.save", side_effect=AssertionError("no numpy saves")), \
             patch("numpy.savez", side_effect=AssertionError("no numpy saves")):
            bound = broker.bind()
        self.assertEqual(bound.raw_bytes, RAW)
        self.assertEqual(broker.release_count, 1)

    def test_capture_errors_typed_and_rejected_without_acceptance(self):
        broker = Broker()
        with self.assertRaises(CommittedFrameError) as caught:
            bind_committed_frame(binding=broker.binding, pixel_decl=broker.decl,
                                 capture=lambda: (_ for _ in ()).throw(OSError("no adapter")),
                                 lookup_publication=broker.lookup)
        self.assertEqual(caught.exception.code, "E_CAPTURE_UNAVAILABLE")
        self.assertEqual(broker.lookup_count, 1)


if __name__ == "__main__":
    main()
