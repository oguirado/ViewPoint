from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


VP_DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(VP_DIRECT))
from recorder.timeline import TimelineError, TimelinePosition, TimelineRecorder
from schemas.validation import canonical_bytes, validate_contract


CAPSULE = "capsule-fixture"
INCARNATION = "incarnation-fixture"
PROFILE_PATH = "/synthetic/browser/profiles/fixture"
TEXT = "synthetic text é"
DEMAND = {
    "schema": "vp.demand/v1", "demand_id": "demo-demand", "surface_id": "surface-fixture",
    "consumer": "fixture-recorder", "interest": "VERIFIED", "purpose": "recording",
    "lease": "synthetic-lease", "credits": 3,
}


def position(when, *, capsule_id=CAPSULE, incarnation=INCARNATION):
    return TimelinePosition(when, capsule_id, incarnation)


def raster(when=10):
    return {
        "frame_kind": "cdp_png", "frame_sha256": "a" * 64, "width": 640, "height": 480,
        "captured_mono_ns": when, "witness": {"cdp_target_id": "fixture-tab", "ax_sha256": "b" * 64},
        "ram_ref": "ram:fixture-frame",
    }


def channel():
    request = {
        "schema": "vp.channel.req/v1", "request_id": "fixture-request", "session_id": "fixture-session",
        "capsule_id": CAPSULE, "incarnation": INCARNATION, "authority_epoch": 1,
        "backend": "cdp", "op": "act", "observation_id": "fixture-observation", "target": {},
        "action": "type", "input_text": TEXT + PROFILE_PATH, "dry_run": False,
        "risk_class": "preparar", "require_vision_witness": False,
    }
    payload = request["input_text"].encode()
    reply = {"schema": "vp.channel.reply/v1", "ok": True, "code": "OK",
             "value_len": len(payload), "value_sha256": sha256(payload).hexdigest()}
    return request, reply


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.registrations = []
        self.verify = Mock(return_value=True)

    @contextmanager
    def register(self, demand):
        self.registrations.append(("acquire", demand))
        try:
            yield
        finally:
            self.registrations.append(("release", demand))

    def make_recorder(self, recorder_type=TimelineRecorder, *, register=None):
        return recorder_type(surface_id=DEMAND["surface_id"], capsule_id=CAPSULE,
                             incarnation=INCARNATION, register=register or self.register,
                             verify_ram_frame=self.verify)

    def assert_valid_snapshot(self, recorder):
        try:
            timeline = recorder.snapshot()
        except TimelineError as error:
            self.fail(str(error))
        validate_contract("rec.timeline/v1", timeline)
        return timeline

    def test_join_orders_by_clock_and_preserves_equal_timestamp_arrival(self):
        recorder = self.make_recorder()
        with recorder.recording(DEMAND):
            recorder.add_demo(position(10), {"mono_ns": 10, "kind": "click", "b": 3})
            recorder.add_cdp(position(8), text=TEXT)
            recorder.add_atspi(position(10), atspi_capture="polling_only", text=TEXT)
            request, reply = channel()
            recorder.add_channel(position(10), request, reply)
            recorder.add_cdp(position(10), frame=raster())
        rows = self.assert_valid_snapshot(recorder)["events"]
        self.assertEqual([row["source"] for row in rows], ["cdp", "demo", "atspi", "channel", "cdp"])
        self.assertEqual([row["mono_ns"] for row in rows], [8, 10, 10, 10, 10])
        self.assertEqual({(row["capsule_id"], row["incarnation"]) for row in rows}, {(CAPSULE, INCARNATION)})
        self.assertEqual(rows[1]["action"], "right_click")
        self.assertEqual(rows[3]["value_sha256"], reply["value_sha256"])

    def test_mismatched_identity_or_demo_timestamp_is_refused(self):
        recorder = self.make_recorder()
        with recorder.recording(DEMAND):
            for wrong in (position(9, capsule_id="other"), position(9, incarnation="stale")):
                with self.assertRaisesRegex(TimelineError, "binding"):
                    recorder.add_cdp(wrong, text=TEXT)
            with self.assertRaisesRegex(TimelineError, "timestamp"):
                recorder.add_demo(position(9), {"mono_ns": 10, "kind": "click"})
            request, reply = channel()
            with self.assertRaisesRegex(TimelineError, "binding"):
                recorder.add_channel(position(9), dict(request, incarnation="stale"), reply)
        self.assertEqual(recorder.snapshot()["events"], [])

    def test_recording_holds_verified_demand_until_delivery_scope_exits(self):
        recorder = self.make_recorder()
        with self.assertRaisesRegex(TimelineError, "not active"):
            recorder.add_cdp(position(10), frame=raster())
        with recorder.recording(DEMAND):
            recorder.set_gap(position(8), "sleep", active=True)
            self.assertEqual(self.registrations, [("acquire", DEMAND)])
            recorder.set_gap(position(9), "sleep", active=False)
            recorder.add_cdp(position(10), frame=raster())
            self.assertEqual(len(recorder.snapshot()["events"]), 3)
            self.assertEqual(self.registrations, [("acquire", DEMAND)])
        self.assertEqual(self.registrations, [("acquire", DEMAND), ("release", DEMAND)])
        with self.assertRaisesRegex(TimelineError, "not active"):
            recorder.add_cdp(position(15), text=TEXT)

    def test_wrong_demand_and_coordinator_refusal_publish_nothing(self):
        recorder = self.make_recorder()
        for demand in (dict(DEMAND, interest="DISPLAY"), dict(DEMAND, surface_id="foreign")):
            with self.assertRaisesRegex(TimelineError, "demand"):
                with recorder.recording(demand):
                    self.fail("inadmissible demand acquired")
        self.assertEqual(self.registrations, [])
        refusal = Mock(side_effect=RuntimeError("QUEUE_FULL"))
        recorder = self.make_recorder(register=refusal)
        with self.assertRaisesRegex(RuntimeError, "QUEUE_FULL"):
            with recorder.recording(DEMAND):
                self.fail("refused demand acquired")
        self.assertEqual(recorder.snapshot()["events"], [])

    def test_recording_releases_demand_when_feed_raises(self):
        recorder = self.make_recorder()
        with self.assertRaisesRegex(RuntimeError, "feed interrupted"):
            with recorder.recording(DEMAND):
                raise RuntimeError("feed interrupted")
        self.assertEqual(self.registrations, [("acquire", DEMAND), ("release", DEMAND)])

    def test_ram_descriptor_requires_positive_proof_and_is_copied_as_metadata(self):
        recorder = self.make_recorder()
        frame = raster()
        original = deepcopy(frame)
        with recorder.recording(DEMAND):
            self.verify.return_value = False
            with self.assertRaisesRegex(TimelineError, "ownership"):
                recorder.add_cdp(position(10), frame=frame)
            self.verify.return_value = True
            self.assertTrue(recorder.add_cdp(position(10), frame=frame))
            frame["witness"]["cdp_target_id"] = "different-tab"
        self.verify.assert_called_with(position(10), original)
        timeline = self.assert_valid_snapshot(recorder)
        self.assertEqual(timeline["events"][0]["frame"], original)
        timeline["events"][0]["frame"].clear()
        self.assertEqual(recorder.snapshot()["events"][0]["frame"], original)

    def assert_no_frame_write(self, recorder):
        with patch.object(Path, "write_bytes") as write:
            with recorder.recording(DEMAND):
                recorder.add_cdp(position(10), frame=raster())
                self.assertEqual(recorder.snapshot()["events"][0]["frame"], raster())
            write.assert_not_called()

    def test_verified_frame_is_retained_without_writing_bytes(self):
        self.assert_no_frame_write(self.make_recorder())

    def test_demo_can_join_a_verified_private_display_raster(self):
        recorder = self.make_recorder()
        descriptor = dict(raster(), frame_kind="xpra", witness={"ax_sha256": "b" * 64})
        with recorder.recording(DEMAND):
            recorder.add_demo(position(10), {"kind": "click", "mono_ns": 10}, frame=descriptor)
            with self.assertRaisesRegex(TimelineError, "CDP raster"):
                recorder.add_cdp(position(10), frame=descriptor)
        self.assertEqual(recorder.snapshot()["events"][0]["frame"], descriptor)
        self.verify.assert_called_once_with(position(10), descriptor)

    def assert_disk_frames_refused(self, recorder):
        with recorder.recording(DEMAND):
            with self.assertRaises(TimelineError):
                recorder.add_cdp(position(10), frame=dict(raster(), ram_ref="file:///synthetic/frame.png"))
            with self.assertRaises(TimelineError):
                recorder.add_cdp(position(10), frame=dict(raster(), frame_bytes=b"synthetic-pixels"))
            with self.assertRaises(TimelineError):
                recorder.add_demo(position(10), {"kind": "click", "mono_ns": 10, "after": "001_after.png"})
            self.assertEqual(recorder.snapshot()["events"], [])

    def test_disk_and_inline_frames_are_refused_before_ram_verification(self):
        self.assert_disk_frames_refused(self.make_recorder())
        self.verify.assert_not_called()

    def assert_text_redacted(self, recorder):
        with recorder.recording(DEMAND):
            recorder.add_cdp(position(10), text=TEXT + PROFILE_PATH)
        timeline = self.assert_valid_snapshot(recorder)
        raw = (TEXT + PROFILE_PATH).encode("utf-8")
        row, = timeline["events"]
        self.assertEqual(row["text_len"], len(raw))
        self.assertEqual(row["text_sha256"], sha256(raw).hexdigest())
        wire = canonical_bytes(timeline)
        self.assertNotIn(raw, wire)
        self.assertNotIn(PROFILE_PATH.encode(), wire)

    def test_text_is_redacted_with_utf8_length_and_digest(self):
        self.assert_text_redacted(self.make_recorder())

    def test_profile_paths_and_raw_channel_fields_never_escape(self):
        recorder = self.make_recorder()
        with recorder.recording(DEMAND):
            recorder.add_demo(position(9), {"mono_ns": 9, "kind": "key", "window": PROFILE_PATH,
                                            "keysym": TEXT, "reason": PROFILE_PATH})
            request, reply = channel()
            recorder.add_channel(position(10), request, reply)
            navigation = {name: value for name, value in request.items() if name != "input_text"}
            recorder.add_channel(position(11), dict(navigation, action="navigate", url=PROFILE_PATH), reply)
            with self.assertRaises(TimelineError) as error:
                recorder.add_channel(position(12), dict(request, extra=PROFILE_PATH), reply)
            self.assertNotIn(PROFILE_PATH, str(error.exception))
            with self.assertRaisesRegex(TimelineError, "opaque"):
                recorder.add_cdp(position(10), frame=dict(
                    raster(), witness=dict(raster()["witness"], cdp_target_id=PROFILE_PATH),
                ))
        wire = canonical_bytes(self.assert_valid_snapshot(recorder))
        self.assertNotIn(PROFILE_PATH.encode(), wire)
        self.assertNotIn(TEXT.encode(), wire)

    def test_demo_count_alone_never_fabricates_digest_or_replay_action(self):
        recorder = self.make_recorder()
        digest = sha256(b"abcd").hexdigest()
        with recorder.recording(DEMAND):
            recorder.add_demo(position(10), {"mono_ns": 10, "kind": "text", "text_len": 4})
            recorder.add_demo(position(12), {"mono_ns": 12, "kind": "text", "text_len": 4},
                              text_sha256=digest)
        first, second = self.assert_valid_snapshot(recorder)["events"]
        self.assertEqual(set(first), {"mono_ns", "capsule_id", "incarnation", "source", "op"})
        self.assertEqual((second["action"], second["text_len"], second["text_sha256"]), ("type", 4, digest))

    def assert_human_content_suppressed(self, recorder):
        with recorder.recording(DEMAND):
            recorder.add_cdp(position(9), text="before")
            recorder.add_cdp(position(10), text=TEXT)
            recorder.set_gap(position(10), "human", active=True)
            self.assertFalse(recorder.add_cdp(position(11), text=TEXT, frame=raster(11)))
            self.assertFalse(recorder.add_demo(position(12), {}))
            recorder.set_gap(position(14), "human", active=False)
            self.assertFalse(recorder.add_cdp(position(13), text=TEXT))
            self.assertFalse(recorder.add_cdp(position(15), frame=raster(11)))
            recorder.add_cdp(position(14), text="after")
        rows = self.assert_valid_snapshot(recorder)["events"]
        self.assertEqual([row["mono_ns"] for row in rows], [9, 10, 14, 14])
        self.assertEqual([row["op"] for row in rows], ["observe", "human_gap_start", "human_gap_end", "observe"])
        for row in rows[1:3]:
            self.assertTrue(row["gap"])
            self.assertEqual(row["source"], "human")
            self.assertEqual(set(row), {"mono_ns", "capsule_id", "incarnation", "source", "gap", "op"})

    def test_human_interval_keeps_only_gap_boundaries_and_suppresses_late_content(self):
        self.assert_human_content_suppressed(self.make_recorder())
        self.verify.assert_not_called()

    def test_overlapping_sleep_and_privacy_gaps_keep_recording_demand(self):
        recorder = self.make_recorder()
        with recorder.recording(DEMAND):
            recorder.set_gap(position(10), "sleep", active=True)
            recorder.set_gap(position(12), "privacy", active=True)
            recorder.set_gap(position(13), "sleep", active=False)
            self.assertFalse(recorder.add_atspi(position(14), atspi_capture="polling_only", text=TEXT))
            recorder.set_gap(position(15), "privacy", active=False)
            self.assertFalse(recorder.add_cdp(position(12), frame=raster(12)))
            self.assertTrue(recorder.add_cdp(position(15), frame=raster(15)))
            self.assertEqual(self.registrations, [("acquire", DEMAND)])
        rows = self.assert_valid_snapshot(recorder)["events"]
        self.assertEqual([row["op"] for row in rows], [
            "sleep_gap_start", "privacy_gap_start", "sleep_gap_end", "privacy_gap_end", "observe",
        ])
        self.assertTrue(all(row["gap"] for row in rows[:-1]))
        self.verify.assert_called_once_with(position(15), raster(15))

    def assert_polling_only(self, recorder):
        with recorder.recording(DEMAND):
            recorder.add_atspi(position(10), atspi_capture="polling_only", text=TEXT)
            with self.assertRaises(TimelineError):
                recorder.add_atspi(position(11), atspi_capture="subscribed", text=TEXT)
        row, = self.assert_valid_snapshot(recorder)["events"]
        self.assertEqual(row["atspi_capture"], "polling_only")

    def test_atspi_snapshot_preserves_polling_only(self):
        self.assert_polling_only(self.make_recorder())

    def test_mutant_disk_backed_frame_fails_refusal_oracle(self):
        class DiskBackedFrame(TimelineRecorder):
            def _with_frame(self, position, row, frame, **options):
                if frame and frame["ram_ref"].startswith("file:"):
                    return dict(row, frame=frame)
                return super()._with_frame(position, row, frame, **options)

        with self.assertRaisesRegex(AssertionError, "TimelineError not raised"):
            self.assert_disk_frames_refused(self.make_recorder(DiskBackedFrame))

    def test_mutant_persist_frame_bytes_fails_ram_only_oracle(self):
        class PersistFrame(TimelineRecorder):
            def _with_frame(self, position, row, frame, **options):
                result = super()._with_frame(position, row, frame, **options)
                if frame is not None and result is not None:
                    Path("synthetic-frame.png").write_bytes(b"synthetic-pixels")
                return result

        with self.assertRaisesRegex(AssertionError, "Expected 'write_bytes' to not have been called"):
            self.assert_no_frame_write(self.make_recorder(PersistFrame))

    def test_mutant_raw_text_fails_redaction_oracle(self):
        class RawText(TimelineRecorder):
            def add_cdp(self, position, *, text=None, frame=None):
                accepted = super().add_cdp(position, text=text, frame=frame)
                if accepted:
                    self._rows[-1]["raw_text"] = text
                return accepted

        with self.assertRaisesRegex(AssertionError, "invalid rec.timeline/v1 record"):
            self.assert_text_redacted(self.make_recorder(RawText))

    def test_mutant_capture_during_human_mode_fails_gap_oracle(self):
        class IgnoreHumanLease(TimelineRecorder):
            def _record(self, position, source, project):
                start = self._open_gaps.pop("human", None)
                try:
                    return super()._record(position, source, project)
                finally:
                    if start is not None:
                        self._open_gaps["human"] = start

        with self.assertRaisesRegex(AssertionError, "True is not false"):
            self.assert_human_content_suppressed(self.make_recorder(IgnoreHumanLease))

    def test_mutant_missing_polling_label_fails_snapshot_oracle(self):
        class DropPollingLabel(TimelineRecorder):
            def _append(self, position, source, fields):
                super()._append(position, source, fields)
                self._rows[-1].pop("atspi_capture", None)

        with self.assertRaisesRegex(AssertionError, "invalid rec.timeline/v1 record"):
            self.assert_polling_only(self.make_recorder(DropPollingLabel))


if __name__ == "__main__":
    unittest.main()
