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
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

DIRECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DIRECT), str(DIRECT.parents[1] / "agents")]
from channel.core.tests.wl0_fixtures import receipt_cases
from frames.cdp import CDPRaster
from frames.source import FrameSource, FrameUnavailable, HostFrameState, Raster, inspect_png
from perception.demand import DemandCoordinator, DemandError
from privacy import PrivacyMetadata, PrivacySentinel, PrivacyState
from schemas.validation import canonical_bytes, validate_contract
from vision import VisionFrames


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
        self.privacy_state = PrivacyState.CLEAR
        self.revision = 1
        self.privacy_events = []
        self.sentinels = {}

    def privacy_metadata(self, binding):
        self.privacy_events.append("metadata")
        return PrivacyMetadata(binding, self.revision, self.privacy_state)

    def privacy_for(self, state):
        binding = state.privacy_binding()
        if binding not in self.sentinels:
            self.sentinels[binding] = PrivacySentinel(
                binding, self.privacy_metadata, clock=self.clock,
                record_gap=lambda active, _: self.privacy_events.append(("gap", active)),
                request_human=lambda reason: self.privacy_events.append(("handoff", reason)),
            )
        return self.sentinels[binding]

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
            self.privacy_events.append("capture")
            self.events.append(("capture", params))
            return {"data": base64.b64encode(self.data).decode("ascii")}
        if method == "Accessibility.getFullAXTree" and params == {"frameId": "root"}:
            return {"nodes": self.nodes}
        raise AssertionError(method)

    def acquire(self, state):
        self.last = CDPRaster(self.send, "flat-session", clock=self.clock)(state)
        return self.last

    def source(self, acquire=None, register=None, *, receipt=None):
        return FrameSource(lambda surface: self.state, {
            "cdp": acquire or self.acquire,
            "xwd": acquire or self.acquire,
            "xpra": self.acquire,
            "ax_only": self.acquire,
        }, register or self.register, privacy_for=self.privacy_for, clock=self.clock, receipt=receipt)


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
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
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
        receipt = json.loads((DIRECT / "config/wl0.json").read_bytes())
        receipt["steps"]["L0-4"]["status"] = "FAIL"
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "wl0.json"
            path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(FrameUnavailable, "qualified CDP"):
                fixture.source(receipt=path).capture(fixture.demand)
        self.assertEqual(fixture.requests, [])

    def test_frames_consume_shared_wl0_fixtures(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for index, (name, data, qualified) in enumerate(receipt_cases()):
                with self.subTest(name=name):
                    path = Path(directory) / f"receipt-{index}.json"
                    if data is not None:
                        path.write_bytes(data)
                    fixture = Fixture()
                    source = fixture.source(receipt=path)
                    if qualified:
                        frame = source.capture(fixture.demand)
                        self.assertEqual(source.read_ram(frame["ram_ref"]), fixture.data)
                    else:
                        with self.assertRaisesRegex(FrameUnavailable, "qualified CDP"):
                            source.capture(fixture.demand)
                        self.assertEqual(fixture.events, [])
                        self.assertEqual(fixture.requests, [])

    def test_unavailable_host_receipt_preserves_private_xpra_frames(self):
        fixture = self.fixture
        fixture.state = replace(fixture.state, display="xpra:7", backend="xpra", cdp_target_id=None)
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = FrameSource(lambda _: fixture.state, {
                "xpra": lambda state: Raster(state, "xpra", fixture.data, fixture.clock(), "a" * 64),
            }, fixture.register, clock=fixture.clock, receipt=Path(directory) / "missing.json")
            frame = source.capture(fixture.demand)
            self.assertEqual(frame["frame_kind"], "xpra")
            self.assertEqual(source.read_ram(frame["ram_ref"]), fixture.data)

    def test_mutant_ignoring_qualification_reaches_frame_acquisition(self):
        fixture = self.fixture
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "missing.json"

            def refuses_capture():
                with self.assertRaisesRegex(FrameUnavailable, "qualified CDP"):
                    fixture.source(receipt=path).capture(fixture.demand)
                self.assertEqual(fixture.requests, [])

            refuses_capture()

            with patch("frames.source._qualified_hidden_cdp", return_value=True):
                with self.assertRaises(AssertionError):
                    refuses_capture()
            self.assertIn(("Page.captureScreenshot", "flat-session"), fixture.requests)

    def test_no_ax_witness_never_publishes(self):
        self.fixture.nodes.clear()
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            self.fixture.source().capture(self.fixture.demand)

    def test_ax_only_has_no_raster_and_cannot_be_relabelled(self):
        fixture = self.fixture
        fixture.state = replace(fixture.state, backend="ax_only", hidden=False)
        with self.assertRaises(FrameUnavailable):
            fixture.source().capture(fixture.demand)
        self.assertEqual(fixture.requests, [])
        fixture.state = replace(fixture.state, backend="cdp")
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source(lambda state: replace(fixture.acquire(state), kind="ax_only")).capture(fixture.demand)
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source(lambda state: replace(
                fixture.acquire(state), png=canonical_bytes(fixture.nodes)
            )).capture(fixture.demand)

    def test_substituted_window_or_cdp_witness_is_refused(self):
        fixture = self.fixture

        def substituted(state):
            return replace(fixture.acquire(state), state=replace(state, window_witness="other"))

        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source(substituted).capture(fixture.demand)
        fixture.target_id = "other"
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source().capture(fixture.demand)

    def test_geometry_authority_or_incarnation_change_blocks_publication(self):
        fixture = self.fixture
        for field, value in (("window_rect", (50, 20, 480, 360)), ("geometry_epoch", 5),
                             ("authority_epoch", 4), ("incarnation", "replacement"),
                             ("window_witness", "substituted")):
            with self.subTest(field=field):
                def drift(state):
                    captured = fixture.acquire(state)
                    fixture.state = replace(state, **{field: value})
                    return captured

                source = fixture.source(drift)
                with self.assertRaisesRegex(FrameUnavailable, "changed before publication") as raised:
                    source.capture(fixture.demand)
                self.assertEqual(raised.exception.code, "STALE_FRAME")
                self.assertFalse(source._frames)

    def test_wrong_dimensions_and_stale_document_are_refused(self):
        fixture = self.fixture
        fixture.state = replace(fixture.state, raster_size=(10, 10))
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source().capture(fixture.demand)
        fixture.state = replace(fixture.state, raster_size=(4, 3))
        original_send = fixture.send

        def navigate(method, *args, **kwargs):
            if method == "Accessibility.getFullAXTree":
                fixture.loader = "new-doc"
            return original_send(method, *args, **kwargs)

        fixture.send = navigate
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source().capture(fixture.demand)

    def test_cached_frame_mutant_is_rejected_and_contrast_fails(self):
        fixture = self.fixture
        source = fixture.source()
        source.capture(fixture.demand)
        old = fixture.last
        fixture.data = png((0, 255, 0))
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source(lambda _: old).capture(fixture.demand)
        original = fixture.data

        def relabelled_cache(state):
            return replace(fixture.acquire(state), png=original)

        with self.assertRaises(AssertionError):
            self.assert_fixture_changes(fixture.source(relabelled_cache))

    def test_blank_acceptance_mutant_fails_pixel_oracle(self):
        fixture = self.fixture
        fixture.data = png()
        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
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

        with patch("io.open", side_effect=no_disk_write) as attempted:
            self.assert_fixture_changes(fixture.source())
            self.assertFalse(any(entry.args[:2] == (forbidden, "wb")
                                 for entry in attempted.call_args_list))
            with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
                fixture.source(writes_png).capture(fixture.demand)
            self.assertTrue(any(entry.args[:2] == (forbidden, "wb")
                                for entry in attempted.call_args_list))
        self.assertFalse(forbidden.exists())

    def test_ax_to_png_mutant_fails_raster_oracle(self):
        fixture = self.fixture

        def labels_ax_as_png(state):
            return Raster(state, "cdp_png", canonical_bytes(fixture.nodes), fixture.clock(), "0" * 64)

        with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
            fixture.source(labels_ax_as_png).capture(fixture.demand)


class GuardedFrameTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def test_p2_demand_is_active_through_publication_and_recording_survives(self):
        fixture = self.fixture
        recording = dict(fixture.demand, demand_id="recording", consumer="recorder")
        drains = Mock(return_value=True)
        coordinator = DemandCoordinator(
            fixture.state.surface_id, interests={"VERIFIED"},
            authorize=lambda row: row in (fixture.demand, recording), drain=drains,
        )

        def active():
            return [row["demand_id"] for row in coordinator.snapshot()["demands"] if row["active"]]

        def acquire(state):
            self.assertEqual(active(), ["capture", "recording"])
            return fixture.acquire(state)

        class CheckPublication(dict):
            def __setitem__(storage, key, value):
                self.assertEqual(active(), ["capture", "recording"])
                super().__setitem__(key, value)

        source = fixture.source(acquire, coordinator.register)
        source._frames = CheckPublication()
        with coordinator.register(recording):
            descriptor = source.capture(fixture.demand)
            self.assertEqual(active(), ["recording"])
            drains.assert_not_called()
            self.assertEqual(source.read_ram(descriptor["ram_ref"]), fixture.data)
            source.release(descriptor["ram_ref"])
        drains.assert_called_once_with()
        self.assertEqual(coordinator.snapshot()["state"], "idle")
        self.assertEqual(fixture.privacy_events[:3], ["metadata", "capture", "metadata"])

        with self.assertRaisesRegex(DemandError, "NO_LEASE"):
            source.capture(dict(fixture.demand, lease="unadmitted"))
        self.assertEqual(fixture.privacy_events.count("capture"), 1)

    def test_host_requires_a_matching_a2_sentinel_before_backend(self):
        fixture = self.fixture
        acquire = Mock(side_effect=fixture.acquire)
        wrong_target = replace(fixture.state, cdp_target_id="other")
        for provider in (None, lambda _: fixture.privacy_for(wrong_target)):
            with self.subTest(provider=provider):
                source = FrameSource(lambda _: fixture.state, {"cdp": acquire},
                                     fixture.register, privacy_for=provider, clock=fixture.clock)
                with self.assertRaisesRegex(FrameUnavailable, "HOLD|unavailable"):
                    source.capture(fixture.demand)
                self.assertFalse(source._frames)
        acquire.assert_not_called()

    def test_r2_host_frame_readback_with_provisioned_privacy(self):
        fixture = self.fixture
        source = fixture.source()
        atspi = Mock(return_value={"atspi_capture": "polling_only", "observation_id": "readback",
                                   "ax_sha256": "b" * 64, "raw_count": 1})
        frames = VisionFrames(source, lambda _: fixture.state, fixture.demand,
                              atspi=atspi, clock=fixture.clock)
        frame = frames.capture(fixture.state)
        self.assertEqual(frame.atspi_sha256, "b" * 64)
        atspi.assert_called_once_with(fixture.state)
        self.assertFalse(source._frames)

    def test_login_and_reauthentication_raise_handoff_and_gap_without_capture(self):
        for privacy in (PrivacyState.LOGIN, PrivacyState.REAUTHENTICATION):
            with self.subTest(privacy=privacy):
                fixture = Fixture()
                fixture.privacy_state = privacy
                acquire = Mock(side_effect=fixture.acquire)
                source = fixture.source(acquire)
                with self.assertRaises(FrameUnavailable) as raised:
                    source.capture(fixture.demand)
                self.assertEqual(raised.exception.code, "HUMAN_NEEDED")
                self.assertEqual(fixture.privacy_events, ["metadata", ("gap", True), ("handoff", "login")])
                acquire.assert_not_called()
                self.assertFalse(source._frames)

    def assert_xwd_unknown_blocks_before_capture(self, implementation):
        fixture = Fixture()
        fixture.state = replace(fixture.state, hidden=False, backend="xwd", cdp_target_id=None)
        fixture.privacy_state = PrivacyState.UNKNOWN
        acquire = Mock(side_effect=lambda state: Raster(
            state, "xwd", fixture.data, fixture.clock(), "a" * 64
        ))
        source = fixture.source(acquire)
        with patch.object(source, "_observe", side_effect=implementation):
            with self.assertRaisesRegex(FrameUnavailable, "HOLD"):
                source.capture(fixture.demand)
        acquire.assert_not_called()
        self.assertFalse(source._frames)

    def test_unknown_privacy_refuses_full_xwd_and_kills_capture_then_filter_mutants(self):
        self.assert_xwd_unknown_blocks_before_capture(FrameSource._observe)

        def capture_then_filter(sentinel, stage, publish):
            raster = stage(None)
            return FrameSource._observe(sentinel, lambda _: raster, publish)

        def xwd_as_exclusion(_sentinel, stage, publish):
            return publish(stage(None))

        with self.assertRaisesRegex(AssertionError, "Expected .* to not have been called"):
            self.assert_xwd_unknown_blocks_before_capture(capture_then_filter)
        with self.assertRaisesRegex(AssertionError, "FrameUnavailable not raised"):
            self.assert_xwd_unknown_blocks_before_capture(xwd_as_exclusion)

    def assert_no_publication_after_privacy_change(self, implementation):
        fixture = Fixture()
        canaries = DIRECT / "fixtures" / "privacy" / "canaries.json"
        token = json.loads(canaries.read_text())["canaries"][1]["token"]
        writes = []

        def protect_while_validating(data, size):
            self.assertTrue(data.endswith(token.encode()))
            fixture.revision += 1
            fixture.privacy_state = PrivacyState.LOGIN
            return inspect_png(data, size)

        def acquire(state):
            raster = fixture.acquire(state)
            # PNG allows trailing bytes. Keep this synthetic canary in staging.
            return replace(raster, png=raster.png + token.encode())

        class TracePublication(dict):
            def __setitem__(storage, key, value):
                writes.append(value.raster.png)
                super().__setitem__(key, value)

        source = fixture.source(acquire)
        source._frames = TracePublication()
        with patch.object(source, "_observe", side_effect=implementation), patch(
            "frames.source.inspect_png", side_effect=protect_while_validating
        ):
            with self.assertRaises(FrameUnavailable) as raised:
                source.capture(fixture.demand)
        self.assertEqual(raised.exception.code, "HUMAN_NEEDED")
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(fixture.privacy_events.count("capture"), 1)
        self.assertEqual(writes, [], "protected bytes reached the publication store")
        self.assertFalse(source._frames)

    def test_privacy_change_during_validation_kills_missing_prepublication_check(self):
        self.assert_no_publication_after_privacy_change(FrameSource._observe)

        def skip_privacy_recheck(sentinel, stage, publish):
            # Corrupt only the capture gate. Delivery still rechecks privacy.
            if stage.__name__ == "stage":
                return publish(stage(sentinel._read()))
            return FrameSource._observe(sentinel, stage, publish)

        with self.assertRaisesRegex(AssertionError, "protected bytes reached the publication store"):
            self.assert_no_publication_after_privacy_change(skip_privacy_recheck)

    def test_clear_revision_change_during_capture_refuses_publication(self):
        fixture = self.fixture

        def navigate_and_return(state):
            raster = fixture.acquire(state)
            fixture.revision += 2
            return raster

        source = fixture.source(navigate_and_return)
        with self.assertRaises(FrameUnavailable) as raised:
            source.capture(fixture.demand)
        self.assertEqual(raised.exception.code, "STALE_FRAME")
        self.assertFalse(source._frames)

    def test_privacy_change_on_demand_exit_retires_frame_before_delivery(self):
        fixture = self.fixture

        @contextmanager
        def protect_on_exit(demand):
            with fixture.register(demand):
                yield
            fixture.revision += 1
            fixture.privacy_state = PrivacyState.REAUTHENTICATION

        source = fixture.source(register=protect_on_exit)
        with self.assertRaises(FrameUnavailable) as raised:
            source.capture(fixture.demand)
        self.assertEqual(raised.exception.code, "HUMAN_NEEDED")
        self.assertFalse(source._frames)

    def test_ram_read_refuses_superseded_privacy_even_after_clear_returns(self):
        fixture = self.fixture
        source = fixture.source()
        descriptor = source.capture(fixture.demand)
        fixture.revision += 1
        fixture.privacy_state = PrivacyState.LOGIN
        with self.assertRaisesRegex(FrameUnavailable, "HUMAN_NEEDED"):
            source.read_ram(descriptor["ram_ref"])
        fixture.revision += 1
        fixture.privacy_state = PrivacyState.CLEAR
        with self.assertRaisesRegex(FrameUnavailable, "privacy revision changed"):
            source.read_ram(descriptor["ram_ref"])
        self.assertIn(("gap", False), fixture.privacy_events)
        source.release(descriptor["ram_ref"])


if __name__ == "__main__":
    unittest.main()
