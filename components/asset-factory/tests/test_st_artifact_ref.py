"""Focused artifact transport conformance and negative controls."""
from dataclasses import replace
from hashlib import sha256
import json
import mmap
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from jsonschema import Draft202012Validator

from tools.transport.artifact_ref import Access, ArtifactError, ArtifactRef, ArtifactStore
from tools.transport.seam import JsonlTelemetry, Transport


_TELEMETRY_SCHEMA = (Path(__file__).resolve().parents[1]
                     / "contracts/s_toro/schemas/telemetry.schema.json")


class ArtifactTransportTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 10
        self.store = ArtifactStore(self.root, clock=lambda: self.now)
        self.reader = Access("reader", "rehearsal")
        self.payload = b"immutable fixture"
        self.digest = sha256(self.payload).hexdigest()

    def put(self, transport=None, data=None, **options):
        target = self.store if transport is None else transport
        return target.put(self.payload if data is None else data, reader=self.reader,
                          deadline_ns=20, media_type="application/octet-stream", **options)

    def test_reference_round_trip_and_deduplication(self):
        ref = self.put(expected_sha256=self.digest)
        duplicate = self.put()
        self.assertEqual(ref.locator, f"af://sha256/{self.digest}")
        self.assertEqual(ref.sha256, duplicate.sha256)
        self.assertNotEqual(ref.lifetime_id, duplicate.lifetime_id)
        self.assertEqual(ArtifactRef.from_record(ref.to_record()), ref)
        with self.store.open(ref, self.reader) as view:
            self.assertTrue(view.readonly)
            self.assertIsInstance(view.obj, mmap.mmap)
            self.assertEqual(sha256(view).hexdigest(), self.digest)
        with self.store.open(ref, self.reader) as view:
            self.assertEqual(view, self.payload)
        self.store.release(ref, self.reader)
        with self.store.open(duplicate, self.reader) as view:
            self.assertEqual(view, self.payload)

    def test_refuses_wrong_digest_and_noncanonical_locator(self):
        with self.assertRaisesRegex(ArtifactError, "digest_mismatch"):
            self.put(expected_sha256="0" * 64)
        self.assertEqual(list(self.root.iterdir()), [])
        ref = self.put()
        for locator in (ref.locator.upper(), ref.locator + "\n", ref.locator + "?x",
                        ref.locator + "/extra", ref.locator[:-1]):
            with self.subTest(locator=locator), self.assertRaisesRegex(ArtifactError, "reference_invalid"):
                replace(ref, locator=locator)

    def test_refuses_swapped_backing_even_when_bytes_match(self):
        ref = self.put()
        path = self.root / ref.sha256
        other = self.root / "replacement"
        other.write_bytes(self.payload)
        other.chmod(0o400)
        other.replace(path)
        with self.assertRaisesRegex(ArtifactError, "backing_stale"):
            with self.store.open(ref, self.reader):
                self.fail("replacement was exposed")

    def test_refuses_backing_changes_during_custody_and_symlink(self):
        ref = self.put()
        path = self.root / ref.sha256
        other = self.root / "replacement"
        other.write_bytes(self.payload)
        other.chmod(0o400)
        with self.assertRaisesRegex(ArtifactError, "backing_stale"):
            with self.store.open(ref, self.reader) as view:
                other.replace(path)
                self.assertEqual(view, self.payload)
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(ArtifactError):
            self.put()
        self.assertEqual(other.read_bytes(), self.payload)

    def test_authority_namespace_scope_and_forged_metadata(self):
        ref = self.put()
        for forged, access in (
            (ref, Access("stranger", "rehearsal")),
            (ref, Access("reader", "another")),
            (replace(ref, size_bytes=ref.size_bytes + 1), self.reader),
            (replace(ref, read_authority="forged"), self.reader),
        ):
            with self.subTest(ref=forged, access=access), self.assertRaisesRegex(
                    ArtifactError, "read_authority"):
                with self.store.open(forged, access):
                    self.fail("unauthorized grant")

    def test_premature_release_and_expiry_during_custody(self):
        ref = self.put()
        with self.assertRaisesRegex(ArtifactError, "lifetime_expired"):
            with self.store.open(ref, self.reader) as view:
                with self.assertRaisesRegex(ArtifactError, "borrow_active"):
                    self.store.release(ref, self.reader)
                self.assertEqual(view, self.payload)
                self.now = 20
        with self.assertRaisesRegex(ArtifactError, "read_authority|lifetime_expired"):
            with self.store.open(ref, self.reader):
                self.fail("expired lease was exposed")
        self.store.release(ref, self.reader)
        with self.assertRaisesRegex(ArtifactError, "read_authority"):
            self.store.release(ref, self.reader)

    def test_released_view_and_escaped_slice(self):
        ref = self.put()
        with self.store.open(ref, self.reader) as view:
            pass
        with self.assertRaises(ValueError):
            len(view)
        with self.assertRaisesRegex(ArtifactError, "view_escaped"):
            with self.store.open(ref, self.reader) as view:
                escaped = view[:1]
        escaped.release()
        with self.assertRaisesRegex(ArtifactError, "read_authority"):
            with self.store.open(ref, self.reader):
                self.fail("broken lifetime was reused")

    def test_mmap_memoryview_fd_and_zero_length(self):
        source = self.root / "producer"
        source.write_bytes(self.payload)
        with source.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as source_map:
            with memoryview(source_map) as view:
                refs = [self.put(data=data) for data in (source_map, view, stream.fileno())]
        refs.append(self.put(data=b""))
        for ref, expected in zip(refs, (self.payload, self.payload, self.payload, b"")):
            with self.store.open(ref, self.reader) as view:
                self.assertEqual(view, expected)

    def test_switch_and_boundary_telemetry(self):
        for mode in ("off", "observe", "on"):
            events = []
            with patch.dict(os.environ, {"FACTORY_TORO": mode}):
                transport = Transport(self.store, emit=events.append)
            ref = self.put(transport)
            with transport.open(ref, self.reader) as view:
                self.assertEqual(sha256(view).hexdigest(), self.digest)
            if mode == "off":
                self.assertEqual(events, [])
                self.assertIsNone(transport.latest_event)
                continue
            self.assertEqual([event.operation for event in events], ["artifact_put", "artifact_open"])
            self.assertEqual([event.copy_bytes for event in events], [len(self.payload), 0])
            self.assertEqual([event.mapped_bytes for event in events], [0, len(self.payload)])
            self.assertEqual([event.fallback_reason for event in events],
                             ["toro_unavailable" if mode == "on" else None] * 2)
            self.assertEqual(transport.backend, "normal")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Transport(self.store).mode, "off")
        with self.assertRaisesRegex(ArtifactError, "mode_invalid"):
            Transport(self.store, mode="invalid")

    def test_jsonl_toro_shape_and_negative_outcome(self):
        log = self.root / "capture.jsonl"
        session = "48db8074-bdb1-4c9e-b854-ed4b9a22a137"
        with JsonlTelemetry(log, session) as sink:
            transport = Transport(self.store, mode="on", emit=sink)
            ref = self.put(transport)
            with transport.open(ref, self.reader):
                live_rows = list(map(json.loads, log.read_text().splitlines()))
                self.assertEqual([row["operation"] for row in live_rows], ["artifact_put", "artifact_open"])
            with self.assertRaisesRegex(ArtifactError, "digest_mismatch"):
                self.put(transport, expected_sha256="0" * 64)
        put, opened, refused, checkpoint = map(json.loads, log.read_text().splitlines())
        validator = Draft202012Validator(json.loads(_TELEMETRY_SCHEMA.read_bytes()))
        for row in (put, opened, refused, checkpoint):
            validator.validate(row)
        for row in (put, opened, refused):
            self.assertEqual(row["schema"], "af.telemetry/v1")
            self.assertEqual(row["component"], "fabrica")
            self.assertEqual(row["session_id"], session)
            self.assertEqual(row["toro_session"], UUID(session).int % ((1 << 64) - 1) + 1)
            self.assertIn("extension", row)
            extension = row["extension"]
            self.assertEqual(extension["copied_bytes"]["value"], row["copy_bytes"])
            self.assertLessEqual(extension["start_ns"], extension["end_ns"])
        self.assertEqual(put["extension"]["materialized_bytes"]["value"], len(self.payload))
        self.assertEqual(opened["extension"]["mapped_bytes"]["value"], len(self.payload))
        self.assertEqual(opened["extension"]["useful_bytes"]["value"], len(self.payload))
        self.assertEqual(refused["copy_bytes"], len(self.payload))
        self.assertEqual([put["sequence"], opened["sequence"], refused["sequence"]], [1, 2, 3])
        self.assertEqual((put["source"], put["destination"]), ("cpu", "file"))
        self.assertEqual((opened["copy_bytes"], opened["hash"]), (0, "sha256_ok"))
        self.assertEqual((refused["status"], refused["hash"]), (4, "sha256_mismatch"))
        self.assertEqual((checkpoint["final"], checkpoint["record_count"]), (True, 3))
        self.assertEqual([row["extension"].get("fallback_reason")
                          for row in (put, opened, refused)], ["toro_unavailable"] * 3)
        self.assertNotIn("reason", put["extension"])
        self.assertNotIn("reason", opened["extension"])
        self.assertEqual(refused["extension"].get("reason"), "digest_mismatch")
        self.assertFalse(log.with_name(log.name + ".reasons.jsonl").exists())
        with self.assertRaises(FileExistsError):
            JsonlTelemetry(log, session)

    def test_schema_rejects_invalid_extension_reasons(self):
        log = self.root / "schema-reasons.jsonl"
        with JsonlTelemetry(log, "48db8074-bdb1-4c9e-b854-ed4b9a22a137") as sink:
            self.put(Transport(self.store, mode="on", emit=sink))
        event = json.loads(log.read_text().splitlines()[0])
        validator = Draft202012Validator(json.loads(_TELEMETRY_SCHEMA.read_bytes()))
        validator.validate(event)
        for key, value in (("fallback_reason", "unknown_fallback"),
                           ("reason", ""), ("reason", "x" * 257), ("reason", 1)):
            with self.subTest(key=key, value=value):
                altered = dict(event, extension=dict(event["extension"], **{key: value}))
                self.assertFalse(validator.is_valid(altered))

        valid = dict(event, extension=dict(event["extension"], fallback_reason="toro_unavailable",
                                           reason="x"))
        self.assertTrue(validator.is_valid(valid))

    def test_bounded_wire_reason_preserves_backend_exception(self):
        log = self.root / "bounded-reasons.jsonl"
        failures = [type(name, (ValueError,), {})() for name in ("x" * 257, "")]
        with JsonlTelemetry(log, "48db8074-bdb1-4c9e-b854-ed4b9a22a137") as sink:
            transport = Transport(self.store, mode="observe", emit=sink)
            for failure in failures:
                with patch.object(self.store, "_install", side_effect=failure):
                    with self.assertRaises(ValueError) as raised:
                        self.put(transport)
                    self.assertIs(raised.exception, failure)
        long_reason, empty_reason, checkpoint = map(json.loads, log.read_text().splitlines())
        validator = Draft202012Validator(json.loads(_TELEMETRY_SCHEMA.read_bytes()))
        for row in (long_reason, empty_reason, checkpoint):
            validator.validate(row)
        self.assertEqual(long_reason["extension"].get("reason"), "x" * 256)
        self.assertEqual(empty_reason["extension"].get("reason"), "unspecified")

    def test_nested_refusal_belongs_to_put(self):
        events = []
        transport = Transport(self.store, mode="observe", emit=events.append)
        ref = self.put()
        with self.assertRaisesRegex(ArtifactError, "digest_mismatch"):
            with transport.open(ref, self.reader):
                self.put(transport, expected_sha256="0" * 64)
        self.assertEqual([(event.operation, event.status, event.reason) for event in events],
                         [("artifact_open", 0, None), ("artifact_put", 4, "digest_mismatch")])

    def test_consumer_value_error_preserves_open_success(self):
        events = []
        transport = Transport(self.store, mode="observe", emit=events.append)
        ref = self.put()
        failure = ValueError("consumer error")
        with self.assertRaises(ValueError) as raised:
            with transport.open(ref, self.reader):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual([(event.operation, event.status) for event in events], [("artifact_open", 0)])
        self.store.release(ref, self.reader)

    def test_open_row_precedes_slow_consumer_and_excludes_its_time(self):
        events = []
        transport = Transport(self.store, mode="observe", emit=events.append)
        ref = self.put()
        clock_ns = 100
        with patch("tools.transport.seam.monotonic_ns", side_effect=lambda: clock_ns):
            with transport.open(ref, self.reader) as view:
                self.assertEqual(view, self.payload)
                self.assertEqual(len(events), 1)
                clock_ns += 60_000_000_000
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].elapsed_ns, 0)

    def test_unexpected_backend_failures_are_persisted_and_reraised(self):
        log = self.root / "unexpected.jsonl"
        session = "f4fb0f0b-8fce-47f8-90da-9f6f72ad81cd"
        ref = self.put()
        failure = ValueError("backend error with an arbitrary message")
        with JsonlTelemetry(log, session) as sink:
            transport = Transport(self.store, mode="observe", emit=sink)
            with patch.object(self.store, "_install", side_effect=failure):
                with self.assertRaises(ValueError) as raised:
                    self.put(transport)
                self.assertIs(raised.exception, failure)
            with patch.object(self.store, "_map_blob", side_effect=failure):
                with self.assertRaises(ValueError) as raised:
                    with transport.open(ref, self.reader):
                        self.fail("failed backend exposed a view")
                self.assertIs(raised.exception, failure)
        rows = list(map(json.loads, log.read_text().splitlines()))
        self.assertEqual(len(rows), 3)
        put, opened, checkpoint = rows
        validator = Draft202012Validator(json.loads(_TELEMETRY_SCHEMA.read_bytes()))
        for row in (put, opened, checkpoint):
            validator.validate(row)
        self.assertEqual([(row["operation"], row["status"], row["hash"]) for row in (put, opened)],
                         [("artifact_put", 5, "absent"), ("artifact_open", 5, "absent")])
        self.assertEqual(checkpoint["record_count"], 2)
        for row in (put, opened):
            self.assertNotIn("fallback_reason", row["extension"])
            self.assertEqual(row["extension"].get("reason"), "ValueError")

    def test_partial_write_counts_only_transferred_bytes(self):
        events = []
        transport = Transport(self.store, mode="observe", emit=events.append)
        write = os.write
        writes = []

        def write_then_fail(fd, buffer):
            if writes:
                raise OSError("interrupted write")
            count = write(fd, buffer[:3])
            writes.append(count)
            return count

        with patch("tools.transport.artifact_ref.os.write", side_effect=write_then_fail):
            with self.assertRaisesRegex(ArtifactError, "storage_io"):
                self.put(transport)
        event, = events
        self.assertEqual(event.logical_bytes, len(self.payload))
        self.assertEqual(event.copy_bytes, sum(writes))
        self.assertEqual(event.materialized_bytes, sum(writes))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_event_write_prevents_checkpoint_and_view_handoff(self):
        log = self.root / "failed-capture.jsonl"
        ref = self.put()
        session = "48db8074-bdb1-4c9e-b854-ed4b9a22a137"
        with JsonlTelemetry(log, session) as sink:
            transport = Transport(self.store, mode="on", emit=sink)
            with self.assertRaisesRegex(OSError, "event stream failed"):
                with patch.object(sink._file, "write", side_effect=OSError("event stream failed")):
                    with transport.open(ref, self.reader):
                        self.fail("view exposed before required telemetry was written")
            with self.assertRaisesRegex(ArtifactError, "telemetry_stream_unavailable"):
                with transport.open(ref, self.reader):
                    self.fail("view exposed after capture became incomplete")
        self.assertTrue(sink._file.closed)
        self.assertEqual(log.read_text(), "")
        self.store.release(ref, self.reader)

    def test_open_success_survives_a_custody_close_refusal(self):
        events = []
        transport = Transport(self.store, mode="observe", emit=events.append)
        ref = self.put()
        with self.assertRaisesRegex(ArtifactError, "lifetime_expired"):
            with transport.open(ref, self.reader):
                self.now = 20
        self.assertEqual([(event.operation, event.status) for event in events], [("artifact_open", 0)])
        self.store.release(ref, self.reader)

    def test_closed_telemetry_rejects_an_event(self):
        log = self.root / "closed.jsonl"
        session = "48db8074-bdb1-4c9e-b854-ed4b9a22a137"
        with JsonlTelemetry(log, session) as sink:
            transport = Transport(self.store, mode="observe", emit=sink)
            self.put(transport)
        with self.assertRaises(Exception) as raised:
            sink(transport.latest_event)
        self.assertIsInstance(raised.exception, ArtifactError)
        self.assertEqual(raised.exception.reason, "telemetry_stream_unavailable")
