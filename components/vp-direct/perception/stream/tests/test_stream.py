"""Offline contracts using the real XWD decoder, mmap ring, producer and reaper."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from perception.stream import PerceptionStream, StreamError
from perception.stream import storage
from vadapt import ramws, rtring, x11_ramcap


def put_screen(path, pixels):
    width = len(pixels) // 3
    header = (100, 7, 2, 24, width, 1, 0, 0, 32, 0, 32, 32, 4 * width,
              4, 0xff0000, 0x00ff00, 0x0000ff, 8, 0, 0, width, 1, 0, 0, 0)
    bgrx = b"".join(pixels[i:i + 3][::-1] + b"\0" for i in range(0, len(pixels), 3))
    path.write_bytes(struct.pack(">25I", *header) + bgrx)


class StreamTests(unittest.TestCase):
    def setUp(self):
        scratch = os.environ["P3_TEST_RAM"]
        self.assertIn(ramws.is_ram_fs(scratch), ramws.RAM_FS)
        folder = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        fixture = json.loads((DIRECT / "fixtures/privacy/canaries.json").read_text())
        self.canary = next(row["token"].encode() for row in fixture["canaries"]
                           if row["id"] == "canary-frame-001")
        self.pixels = self.canary.ljust(96, b".")
        self.stream = self.make_stream("left", self.pixels)

    def make_stream(self, name, pixels):
        path = self.root / name
        put_screen(path, pixels)
        binding = json.loads((DIRECT / "schemas/tests/examples/vp.surface_binding_v1.valid.json").read_text())
        binding.update(surface_id=name, endpoint=f"xvfb://fixture/{name}")
        source = x11_ramcap.SourceConfig(str(self.root), name, len(pixels) // 3, 1,
                                         "offline synthetic XWD")
        stream = PerceptionStream(binding, source, ring_slots=2, interval_ns=1000,
                                  ttl_ns=10**10, max_frame_bytes=4096, max_ring_bytes=8192,
                                  workspace_root=self.root / "rings")
        self.addCleanup(stream.close)
        return stream

    def test_wake_requires_new_publication_after_each_pause(self):
        s = self.stream
        retired = []
        ids = set()
        for generation in range(1, 6):
            obs = s.wake()
            self.assertEqual(obs.stream_generation, generation)
            self.assertGreaterEqual(obs.render_index, 1)
            self.assertNotIn(obs.observation_id, ids)
            ids.add(obs.observation_id)
            self.assertEqual(s.read(obs.token, obs.observation_id), self.pixels)
            new = s.observe(obs.token)
            self.assertGreater(new.render_index, obs.render_index)
            for old in retired:
                with self.assertRaises(StreamError):
                    s.observe(old)
            s.pause(obs.token)
            retired.append(obs.token)
            with self.assertRaises(StreamError):
                s.read(obs.token, new.observation_id)

    def test_pause_retains_ring_and_exact_charge(self):
        s = self.stream
        obs = s.wake()
        before = s.status()
        path = s._workspace.path(rtring.REL)
        s.pause(obs.token)
        after = s.status()
        self.assertEqual(after["state"], "ASLEEP")
        self.assertEqual(before["ring_id"], after["ring_id"])
        self.assertEqual(after["reserved_bytes"], path.stat().st_size)
        self.assertGreater(after["reserved_bytes"], 0)
        self.assertIn(self.canary, path.read_bytes())
        self.assertIsNone(after["fresh_observation_id"])

    def test_retained_mapping_stays_quarantined_and_charged_until_reap(self):
        s = self.stream
        obs = s.wake()
        borrowed = s._frame.view[:, :1, :]
        old = s.status()
        s.pause(obs.token)
        with self.assertRaisesRegex(StreamError, "retained"):
            s.wake()
        for _ in range(2):
            with self.assertRaises(StreamError):
                s.close()
            status = s.status()
            self.assertEqual(status["state"], "QUARANTINED")
            self.assertEqual(status["reserved_bytes"], old["reserved_bytes"])
            self.assertEqual(status["ring_id"], old["ring_id"])
            self.assertIsNone(status["last_scrubbed_ring"])
            with self.assertRaises(StreamError):
                s.observe(obs.token)
        del borrowed
        s.close()
        self.assertEqual(s.status()["reserved_bytes"], 0)
        self.assertEqual(s.status()["last_scrubbed_ring"], old["ring_id"])
        self.assertGreater(s.wake().stream_generation, obs.stream_generation)

    def test_held_start_without_production_refused(self):
        s = self.stream
        with patch.object(s._capture, "capture_once", return_value=None):
            with self.assertRaisesRegex(StreamError, "no fresh"):
                s.wake()
        status = s.status()
        self.assertIsNone(status["fresh_observation_id"])
        self.assertEqual(status["stream_generation"], 0)
        self.assertGreater(status["reserved_bytes"], 0)
        s.close()
        self.assertEqual(s.wake().stream_generation, 1)

    def test_idempotent_start_of_stopped_producer_is_refused(self):
        s = self.stream
        start = s._capture.Start

        def held_start(sid, source, **options):
            start(sid, source, **options)
            s._capture.Stop(sid)
            return start(sid, source, **options)

        with patch.object(s._capture, "Start", side_effect=held_start):
            with self.assertRaisesRegex(StreamError, "new running producer"):
                s.wake()
        self.assertEqual(s.status()["stream_generation"], 0)
        self.assertIsNone(s.status()["fresh_observation_id"])

    def test_partial_writer_creation_keeps_charge_until_verified_cleanup(self):
        s = self.stream
        with patch.object(rtring, "_map", side_effect=OSError("mapping failed")), \
                patch.object(storage, "_scrub", side_effect=OSError("temporary scrub failure")):
            with self.assertRaises(x11_ramcap.CaptureError):
                s.wake()
            self.assertEqual(s.status()["state"], "QUARANTINED")
            with self.assertRaises(StreamError):
                s.close()
            self.assertGreater(s.status()["reserved_bytes"], 0)
        s.close()
        self.assertEqual(s.status()["reserved_bytes"], 0)
        self.assertEqual(s.wake().stream_generation, 1)

    def test_cached_start_frame_cannot_prove_progress(self):
        s = self.stream
        def initial(sid, *, wait):
            return s._capture._get(sid).reader.acquire(0)
        with patch.object(s._capture, "capture_once", side_effect=initial):
            with self.assertRaisesRegex(StreamError, "no fresh"):
                s.wake()

    def test_fictitious_generation_and_cross_surface_token_refused(self):
        a = self.stream
        b = self.make_stream("right", b"xyz" * 32)
        left, right = a.wake(), b.wake()
        for forged in (replace(left.token, generation=2), replace(left.token, generation=True),
                       replace(left.token, nonce="bogus"), replace(left.token, owner="other"),
                       replace(left.token, incarnation="old"), replace(left.token, authority_epoch=True),
                       right.token):
            with self.assertRaises(StreamError):
                a.observe(forged)
        with self.assertRaises(StreamError):
            a.read(left.token, right.observation_id)
        a.close()
        self.assertEqual(b.read(right.token, right.observation_id), b"xyz" * 32)

    def test_obsolete_observation_refused(self):
        s = self.stream
        first = s.wake()
        put_screen(self.root / "left", b"123" * 32)
        second = s.observe(first.token)
        self.assertNotEqual(first.frame_sha256, second.frame_sha256)
        with self.assertRaises(StreamError):
            s.read(first.token, first.observation_id)
        self.assertEqual(s.read(second.token, second.observation_id), b"123" * 32)

    def test_scrub_readback_fails_on_surviving_canary(self):
        s = self.stream
        obs = s.wake()
        path = s._workspace.path(rtring.REL)
        s.pause(obs.token)
        with patch.object(storage, "_overwrite", return_value=None):
            with self.assertRaisesRegex(StreamError, "retained"):
                s.wake()
            self.assertEqual(s.status()["reserved_bytes"], path.stat().st_size)
            self.assertEqual(s.status()["state"], "QUARANTINED")
            self.assertIn(self.canary, path.read_bytes())
        s.close()
        self.assertEqual(s.status()["reserved_bytes"], 0)

    def test_concurrent_wakes_admit_one_producer(self):
        start = threading.Barrier(2)
        def attempt():
            start.wait(timeout=5)
            try:
                return self.stream.wake()
            except StreamError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.stream.status()["stream_generation"], 1)

    def assert_retired(self, stream, old):
        with self.assertRaises(StreamError):
            stream.observe(old.token)

    def test_mutant_reuse_token(self):
        s = self.stream
        old = s.wake()
        s.pause(old.token)
        sample = s._sample
        # Broken reopen publishes the first observation under the old token.
        with patch.object(s, "_sample", side_effect=lambda token, index: sample(old.token, index)):
            s.wake()
        with self.assertRaises(AssertionError):
            self.assert_retired(s, old)

    def test_mutant_release_charge_on_stop(self):
        s = self.stream
        obs = s.wake()
        charge = s._workspace.path(rtring.REL).stat().st_size
        pause = s.pause

        def early_uncharge(token):
            pause(token)
            s._charge = 0

        with patch.object(s, "pause", side_effect=early_uncharge):
            s.pause(obs.token)
            with self.assertRaises(AssertionError):
                self.assertEqual(s.status()["reserved_bytes"], charge)

    def assert_scrubbed_inode(self, stream):
        obs = stream.wake()
        path = stream._workspace.path(rtring.REL)
        with path.open("rb") as old:
            self.assertIn(self.canary, old.read())
            size = old.tell()
            stream.pause(obs.token)
            stream.wake()
            old.seek(0)
            # Independent witness on the old inode survives unlink and reopen.
            self.assertEqual(old.read(), bytes(size), "recycled ring retained pixels")

    def test_mutant_recycle_without_scrub(self):
        self.assert_scrubbed_inode(self.stream)
        other = self.make_stream("other", self.pixels)
        with patch.object(storage, "_scrub", return_value=None):
            with self.assertRaisesRegex(AssertionError, "recycled ring retained pixels"):
                self.assert_scrubbed_inode(other)

    def test_failed_start_can_recover_after_proven_rollback(self):
        (self.root / "left").unlink()
        with self.assertRaises(x11_ramcap.CaptureError):
            self.stream.wake()
        self.assertIsNone(self.stream.status()["fresh_observation_id"])
        self.stream.close()
        put_screen(self.root / "left", self.pixels)
        self.assertEqual(self.stream.wake().stream_generation, 1)
