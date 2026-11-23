import base64
import io
import json
import sys
import unittest
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from itertools import count
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from frames.cdp import CDPRaster
from frames.source import FrameSource, FrameUnavailable, HostFrameState, Raster, inspect_png
from schemas.validation import canonical_bytes, validate_contract


def png(color=None):
    image = Image.new("RGB", (4, 3), "white")
    if color is not None:
        image.putpixel((1, 1), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class Fixture:
    def __init__(self):
        self.state = HostFrameState("surface", "instance", 3, 4, "window:witness",
                                    (10, 20, 480, 360), (4, 3), ":0", True, "cdp", "tab")
        self.clock = count(100).__next__
        self.demand = {
            "schema": "vp.demand/v1",
            "demand_id": "capture",
            "surface_id": self.state.surface_id,
            "consumer": "s5",
            "interest": "VERIFIED",
            "purpose": "observe",
            "lease": "lease-fixture",
            "credits": 1,
        }
        self.events = []
        self.requests = []
        self.data = png((255, 0, 0))
        self.nodes = [{"nodeId": "node", "name": {"type": "computedString", "value": "TICK_1"}}]
        self.target_id = self.state.cdp_target_id
        self.loader = "doc"
        self.last = None

    @contextmanager
    def register(self, demand):
        self.events.append(("register", demand))
        try:
            yield
        finally:
            self.events.append(("release", demand))

    def send(self, method, params=None, *, session_id):
        self.requests.append((method, session_id))
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": self.target_id, "type": "page"}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "root", "loaderId": self.loader}}}
        if method == "Page.captureScreenshot":
            self.events.append(("capture", params))
            return {"data": base64.b64encode(self.data).decode("ascii")}
        if method == "Accessibility.getFullAXTree" and params == {"frameId": "root"}:
            return {"nodes": self.nodes}
        raise AssertionError(method)

    def acquire(self, state):
        self.last = CDPRaster(self.send, "flat-session", clock=self.clock)(state)
        return self.last

    def source(self, acquire=None, register=None):
        return FrameSource(lambda surface: self.state, {
            "cdp": acquire or self.acquire,
            "xwd": self.acquire,
            "xpra": self.acquire,
            "ax_only": self.acquire,
        }, register or self.register, clock=self.clock)


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def assert_fixture_changes(self, source):
        fixture = self.fixture
        first = source.capture(fixture.demand)
        first_data = source.read_ram(first["ram_ref"])
        fixture.data = png((0, 255, 0))
        fixture.nodes[0]["name"]["value"] = "TICK_2"
        second = source.capture(fixture.demand)
        second_data = source.read_ram(second["ram_ref"])
        self.assertNotEqual(inspect_png(first_data, (4, 3)), inspect_png(second_data, (4, 3)))
        self.assertNotEqual(first["witness"]["ax_sha256"], second["witness"]["ax_sha256"])
        self.assertGreater(second["captured_mono_ns"], first["captured_mono_ns"])
        for frame, data in ((first, first_data), (second, second_data)):
            validate_contract("vp_capture_call.screenshot.result/v1", frame)
            self.assertEqual(frame["frame_kind"], "cdp_png")
            self.assertEqual(frame["witness"]["cdp_target_id"], "tab")
            self.assertEqual(frame["frame_sha256"], sha256(data).hexdigest())
            self.assertTrue(frame["ram_ref"].startswith("ram:"))
        self.assertEqual(second["witness"]["ax_sha256"], sha256(canonical_bytes(fixture.nodes)).hexdigest())
        self.assertEqual({session for _, session in fixture.requests}, {"flat-session"})
        source.release(first["ram_ref"])
        source.release(second["ram_ref"])
        with self.assertRaises(FrameUnavailable):
            source.read_ram(first["ram_ref"])

    def test_changed_dom_yields_distinct_verified_pixels_and_ram_descriptors(self):
        self.assert_fixture_changes(self.fixture.source())

    def test_demand_is_held_during_capture_and_released_on_refusal(self):
        fixture = self.fixture
        fixture.data = png()
        with self.assertRaisesRegex(FrameUnavailable, "blank"):
            fixture.source().capture(fixture.demand)
        self.assertEqual(fixture.events, [
            ("register", fixture.demand), ("capture", {"format": "png"}),
            ("release", fixture.demand),
        ])

    def test_denied_demand_never_acquires(self):
        @contextmanager
        def denied(_):
            raise FrameUnavailable("demand denied")
            yield

        fixture = self.fixture
        with self.assertRaisesRegex(FrameUnavailable, "demand denied"):
            fixture.source(register=denied).capture(fixture.demand)
        self.assertEqual(fixture.requests, [])

    def test_drift_on_demand_release_retires_unpublished_handle(self):
        fixture = self.fixture

        @contextmanager
        def resize_on_exit(demand):
            with fixture.register(demand):
                yield
                fixture.state = replace(fixture.state, geometry_epoch=7)

        source = fixture.source(register=resize_on_exit)
        with self.assertRaisesRegex(FrameUnavailable, "changed before delivery"):
            source.capture(fixture.demand)
        self.assertEqual(source._frames, {})

    def test_hidden_xwd_and_unqualified_hidden_cdp_never_acquire(self):
        fixture = self.fixture
        fixture.state = replace(fixture.state, backend="xwd")
        with self.assertRaises(FrameUnavailable):
            fixture.source().capture(fixture.demand)
        fixture.state = replace(fixture.state, backend="cdp")
        receipt = Mock(read_text=Mock(return_value=json.dumps({
            "schema": "vp.l0_receipt/v1", "steps": {"L0-4": {"status": "FAIL"}},
        })))
        source = FrameSource(lambda _: fixture.state, {"cdp": fixture.acquire},
                             fixture.register, clock=fixture.clock, receipt=receipt)
        with self.assertRaisesRegex(FrameUnavailable, "qualified CDP"):
            source.capture(fixture.demand)
        self.assertEqual(fixture.requests, [])

    def test_no_ax_witness_never_publishes(self):
        self.fixture.nodes.clear()
        with self.assertRaisesRegex(FrameUnavailable, "AX witness unavailable"):
            self.fixture.source().capture(self.fixture.demand)

    def test_ax_only_has_no_raster_and_cannot_be_relabelled(self):
        fixture = self.fixture
        fixture.state = replace(fixture.state, backend="ax_only", hidden=False)
        with self.assertRaises(FrameUnavailable):
            fixture.source().capture(fixture.demand)
        self.assertEqual(fixture.requests, [])
        fixture.state = replace(fixture.state, backend="cdp")
        with self.assertRaisesRegex(FrameUnavailable, "provenance"):
            fixture.source(lambda state: replace(fixture.acquire(state), kind="ax_only")).capture(fixture.demand)
        with self.assertRaisesRegex(FrameUnavailable, "invalid PNG"):
            fixture.source(lambda state: replace(
                fixture.acquire(state), png=canonical_bytes(fixture.nodes)
            )).capture(fixture.demand)

    def test_substituted_window_or_cdp_witness_is_refused(self):
        fixture = self.fixture

        def substituted(state):
            return replace(fixture.acquire(state), state=replace(state, window_witness="other"))

        with self.assertRaisesRegex(FrameUnavailable, "provenance"):
            fixture.source(substituted).capture(fixture.demand)
        fixture.target_id = "other"
        with self.assertRaisesRegex(FrameUnavailable, "session target"):
            fixture.source().capture(fixture.demand)

    def test_geometry_authority_or_incarnation_change_blocks_publication(self):
        fixture = self.fixture
        for field, value in (("window_rect", (50, 20, 480, 360)), ("geometry_epoch", 5),
                             ("authority_epoch", 4), ("incarnation", "replacement")):
            with self.subTest(field=field):
                def drift(state):
                    captured = fixture.acquire(state)
                    fixture.state = replace(state, **{field: value})
                    return captured

                with self.assertRaisesRegex(FrameUnavailable, "changed before publication"):
                    fixture.source(drift).capture(fixture.demand)

    def test_wrong_dimensions_and_stale_document_are_refused(self):
        fixture = self.fixture
        fixture.state = replace(fixture.state, raster_size=(10, 10))
        with self.assertRaisesRegex(FrameUnavailable, "raster size"):
            fixture.source().capture(fixture.demand)
        fixture.state = replace(fixture.state, raster_size=(4, 3))
        original_send = fixture.send

        def navigate(method, *args, **kwargs):
            if method == "Accessibility.getFullAXTree":
                fixture.loader = "new-doc"
            return original_send(method, *args, **kwargs)

        fixture.send = navigate
        with self.assertRaisesRegex(FrameUnavailable, "document changed"):
            fixture.source().capture(fixture.demand)

    def test_cached_frame_mutant_is_rejected_and_contrast_fails(self):
        fixture = self.fixture
        source = fixture.source()
        source.capture(fixture.demand)
        old = fixture.last
        fixture.data = png((0, 255, 0))
        with self.assertRaisesRegex(FrameUnavailable, "acquisition interval"):
            fixture.source(lambda _: old).capture(fixture.demand)
        original = fixture.data

        def relabelled_cache(state):
            return replace(fixture.acquire(state), png=original)

        with self.assertRaises(AssertionError):
            self.assert_fixture_changes(fixture.source(relabelled_cache))

    def test_blank_acceptance_mutant_fails_pixel_oracle(self):
        fixture = self.fixture
        fixture.data = png()
        with self.assertRaisesRegex(FrameUnavailable, "blank"):
            fixture.source().capture(fixture.demand)
        with patch("frames.source.inspect_png", return_value="ignored"):
            source = fixture.source()
            descriptor = source.capture(fixture.demand)
            with self.assertRaisesRegex(FrameUnavailable, "blank"):
                inspect_png(source.read_ram(descriptor["ram_ref"]), fixture.state.raster_size)

    def test_ram_only_oracle_kills_disk_png_mutant(self):
        fixture = self.fixture
        forbidden = Path(__file__).with_name("s5-mutant.png")
        original_open = io.open

        def no_disk_write(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("disk write")
            return original_open(file, mode, *args, **kwargs)

        def writes_png(state):
            raster = fixture.acquire(state)
            forbidden.write_bytes(raster.png)
            return raster

        with patch("io.open", side_effect=no_disk_write):
            self.assert_fixture_changes(fixture.source())
            with self.assertRaisesRegex(AssertionError, "disk write"):
                fixture.source(writes_png).capture(fixture.demand)
        self.assertFalse(forbidden.exists())

    def test_ax_to_png_mutant_fails_raster_oracle(self):
        fixture = self.fixture

        def labels_ax_as_png(state):
            return Raster(state, "cdp_png", canonical_bytes(fixture.nodes), fixture.clock(), "0" * 64)

        with self.assertRaisesRegex(FrameUnavailable, "invalid PNG"):
            fixture.source(labels_ax_as_png).capture(fixture.demand)


if __name__ == "__main__":
    unittest.main()
