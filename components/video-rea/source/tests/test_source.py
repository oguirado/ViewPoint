# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import replace
from fractions import Fraction
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from source import FrameBlock, FrameQueue, ingest, media, open_frame_source
from source.proc import ChildError, require_namespace, run_child
from source.records import CONTRACTS, Contracts

INGEST = import_module("source.ingest")
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

ENGINE = Path(__file__).resolve().parents[3] / "vision-engine"
sys.path.insert(0, str(ENGINE))
from visioncore.frame import validate_manifest
from visioncore.pipeline import ManifestSource


def load(directory, name):
    return json.loads((directory / name).read_bytes())


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.movie = cls.root / "fixture.mkv"
        # Independent authored presentation times, including a repeated image.
        for i, color in enumerate(["red", "green", "green", "blue"]):
            Image.new("RGB", (64, 48), color).save(cls.root / f"frame{i}.png")
        run_child([FFMPEG, "-v", "error", "-framerate", "25", "-i",
                   str(cls.root / "frame%d.png"), "-vf",
                   "setpts='if(eq(N,0),0,if(eq(N,1),2,if(eq(N,2),9,10)))'",
                   "-fps_mode", "passthrough", "-c:v", "ffv1", str(cls.movie)])
        cls.out = cls.root / "admitted"
        ingest(cls.movie, cls.out, sample_ms=200)

    def test_independent_vfr_join_and_sampling(self):
        m = validate_manifest(load(self.out, "frames.manifest.json"))
        self.assertEqual([f["t_us"] for f in m["frames"]], [0, 80000, 360000, 400000])
        raw = run_child([FFMPEG, "-v", "error", "-i", str(self.movie), "-fps_mode",
                         "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
        actual = list(ManifestSource(self.out / "frames.manifest.json"))
        self.assertEqual(b"".join(f.rgb.tobytes() for f in actual), raw)
        records = load(self.out, "frames.json")
        for row, record in zip(m["frames"], records, strict=True):
            digest = hashlib.sha256((self.out / row["file"]).read_bytes()).hexdigest()
            self.assertEqual(row["file_sha256"], digest)
            self.assertEqual(record["provenance"]["frame_sha256"], digest)
        source = load(self.out, "video_source.json")
        self.assertEqual(source["provenance"], {"video_sha256": hashlib.sha256(self.movie.read_bytes()).hexdigest(),
                                                "byte_size": self.movie.stat().st_size})
        self.assertEqual(source["pts_source"], "pts")
        self.assertIs(source["vfr"], True)
        aggregate = load(self.out, "keyframes.json")
        self.assertEqual(aggregate["derived_from"], [hashlib.sha256((self.out / "frames.json").read_bytes()).hexdigest()])
        selected = aggregate["keyframes"]
        self.assertEqual(aggregate["evidence"], [f["provenance"] for f in selected])
        self.assertEqual([s["provenance"]["frame_index"] for s in selected], list(range(4)))
        self.assertIn("before-change", selected[0]["reason"])
        self.assertIn("after-change", selected[-1]["reason"])
        self.assertIn("periodic", selected[2]["reason"])
        self.assertIn("endpoint", selected[-1]["reason"])
        clock = load(self.out, "clock.json")
        self.assertTrue(clock["irregular_pts"])
        self.assertEqual(clock["gaps"][0]["after_index"], 2)
        self.assertEqual(clock["occurrences"][2]["duplicate_of"], 1)
        decoder = run_child([FFMPEG, "-version"]).decode().splitlines()[0]
        self.assertEqual(clock["decoder"], decoder)

    def test_sampling_skips_static_frames_and_reanchors(self):
        directory = self.root / "sampling-fixture"
        directory.mkdir()
        for i, color in enumerate(["red"] * 5 + ["green"] * 2 + ["blue"]):
            Image.new("RGB", (64, 48), color).save(directory / f"in{i}.png")
        movie = directory / "static-run.mkv"
        run_child([FFMPEG, "-v", "error", "-framerate", "25", "-i", str(directory / "in%d.png"),
                   "-vf", "setpts='if(lt(N,3),N,if(lt(N,5),N+3,if(eq(N,5),9,N+6)))'",
                   "-fps_mode", "passthrough", "-c:v", "ffv1", str(movie)])
        out = ingest(movie, directory / "admitted", sample_ms=200)
        manifest = load(out, "frames.manifest.json")
        self.assertEqual([f["t_us"] for f in manifest["frames"]],
                         [0, 40000, 80000, 240000, 280000, 360000, 480000, 520000])
        selected = load(out, "keyframes.json")["keyframes"]
        self.assertEqual([(f["provenance"]["frame_index"], f["reason"]) for f in selected],
                         [(0, "endpoint, periodic"), (3, "periodic"), (4, "before-change"),
                          (5, "after-change"), (6, "periodic, before-change"),
                          (7, "endpoint, after-change")])

    def test_resume_and_mutations(self):
        ingest(self.movie, self.out, sample_ms=200, resume=True)
        for name, field in [("file_sha256", "frame hashes"), ("t_us", "nominal FPS")]:
            with self.subTest(field=field):
                path = self.out / "frames.manifest.json"
                old = path.read_bytes()
                m = json.loads(old)
                if name == "t_us":
                    for i, f in enumerate(m["frames"]):
                        f[name] = i * 40000
                else:
                    a, b = m["frames"][:2]
                    a[name], b[name] = b[name], a[name]
                # Serialize identically to production so whitespace is not the contrast.
                from source.records import canonical
                path.write_bytes(canonical(m))
                try:
                    with self.assertRaisesRegex(ValueError, "resume"):
                        ingest(self.movie, self.out, sample_ms=200, resume=True)
                finally:
                    path.write_bytes(old)
        swapped = self.root / "swapped.mkv"
        run_child([FFMPEG, "-v", "error", "-i", str(self.movie), "-map", "0:v", "-c",
                   "copy", "-metadata", "title=other", str(swapped)])
        with self.assertRaisesRegex(ValueError, "resume"):
            ingest(swapped, self.out, sample_ms=200, resume=True)

    def test_resume_rejects_extra_file_and_symlink(self):
        unexpected = self.out / "foreign"
        expected = self.out / "frames.json"
        for artifact, create in (
            (unexpected, lambda: unexpected.write_bytes(b"uncited bytes")),
            (unexpected, lambda: unexpected.symlink_to(expected)),
        ):
            create()
            try:
                with self.assertRaisesRegex(ValueError, "unexpected artifact"):
                    ingest(self.movie, self.out, resume=True)
            finally:
                artifact.unlink()
        backup = self.root / "frames-backup.json"
        expected.replace(backup)
        try:
            expected.symlink_to(backup)
            with self.assertRaisesRegex(ValueError, "unexpected artifact"):
                ingest(self.movie, self.out, resume=True)
        finally:
            expected.unlink()
            backup.replace(expected)

    def test_cli_modes_and_missing_backend(self):
        for mode in ("off", "observe", "on"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"VIEWPOINT_TORO": mode}):
                out = self.root / mode
                run_child([sys.executable, "-B", "-m", "source", str(self.movie), "--out", str(out)])
                self.assertEqual(load(out, "frames.manifest.json"), load(self.out, "frames.manifest.json"))
                clock = load(out, "clock.json")
                self.assertEqual(clock["toro_mode"], mode)
                self.assertEqual(bool(clock["fallback_reason"]), mode == "on")

    def test_ambiguous_stream_is_unpublished(self):
        path = self.root / "ambiguous.mkv"
        run_child([FFMPEG, "-v", "error", "-i", str(self.movie), "-map", "0:v",
                   "-map", "0:v", "-c", "copy", str(path)])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            ingest(path, self.root / "ambiguous")
        self.assertFalse((self.root / "ambiguous").exists())

    def test_b_frames_preserve_presentation_order(self):
        movie = self.root / "reordered.mp4"
        run_child([FFMPEG, "-v", "error", "-i", str(self.movie), "-fps_mode", "passthrough",
                   "-enc_time_base", "1:1000", "-video_track_timescale", "1000", "-threads", "1",
                   "-c:v", "libx264", "-x264-params", "bframes=2:b-adapt=0:scenecut=0",
                   str(movie)])
        packets = json.loads(run_child([FFPROBE, "-v", "error", "-select_streams", "v",
                                       "-show_entries", "packet=pts", "-of", "json", str(movie)]))
        decode_order = [p["pts"] for p in packets["packets"]]
        self.assertNotEqual(decode_order, sorted(decode_order))
        destination = self.root / "reordered"
        ingest(movie, destination)
        clock = load(destination, "clock.json")
        self.assertEqual([r["pts"] for r in clock["occurrences"]], sorted(decode_order))
        self.assertEqual(sorted(decode_order), [0, 80, 360, 400])
        legacy_rgb = run_child([FFMPEG, "-v", "error", "-i", str(movie), "-filter_threads", "1",
                                "-sws_flags", "bicubic", "-vf", "format=rgb24", "-threads", "1",
                                "-fps_mode", "passthrough", "-c:v", "rawvideo", "-pix_fmt", "rgb24",
                                "-f", "rawvideo", "pipe:1"])
        manifest_rgb = b"".join(f.rgb.tobytes() for f in ManifestSource(destination / "frames.manifest.json"))
        self.assertEqual(manifest_rgb, legacy_rgb)

    def test_mp4_edit_list_discards_preroll_with_interleaved_audio(self):
        master = self.root / "long-gop.mp4"
        clip = self.root / "edit-list.mp4"
        run_child([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                   "testsrc2=size=64x48:rate=10:duration=1", "-f", "lavfi", "-i",
                   "sine=sample_rate=48000:duration=1", "-c:v", "libx264", "-g", "10",
                   "-bf", "0", "-threads", "1", "-c:a", "aac", str(master)])
        run_child([FFMPEG, "-v", "error", "-ss", "0.3", "-i", str(master), "-c", "copy", str(clip)])
        probe = json.loads(run_child([FFPROBE, "-v", "error", "-select_streams", "v",
                                     "-show_entries", "stream=nb_frames:packet=pts,flags",
                                     "-of", "json", str(clip)]))
        packets = probe["packets"]
        visible = [packet["pts"] for packet in packets if "D" not in packet["flags"]]
        self.assertLess(len(visible), len(packets))
        self.assertEqual(int(probe["streams"][0]["nb_frames"]), len(packets))
        with patch.object(media, "MAX_FRAMES", len(visible)):
            out = ingest(clip, self.root / "preroll")
        clock = load(out, "clock.json")
        self.assertEqual([f["pts"] for f in clock["occurrences"]], visible)

    def probe_fixture(self):
        return media.probe(self.movie, hashlib.sha256(self.movie.read_bytes()).hexdigest(),
                           self.movie.stat().st_size)

    def test_incomplete_decoder_join_fails(self):
        track = self.probe_fixture()
        for ticks in [track.pts + [450], [0, 40, 80, 120]]:
            with self.subTest(ticks=ticks), self.assertRaisesRegex(ValueError, "join|mismatch"):
                media.decode(self.movie, replace(track, pts=ticks), lambda i, pixels: None)

    def test_extra_decoder_frame_fails(self):
        track = self.probe_fixture()
        with self.assertRaisesRegex(ValueError, "extra decoded pixels"):
            media.decode(self.movie, replace(track, pts=track.pts[:-1]), lambda i, pixels: None)

    def test_decoder_rejects_checksum_alone(self):
        track = self.probe_fixture()

        def corrupt_rgb(argv, *, on_stdout, **kwargs):
            first = True

            def send(chunk):
                nonlocal first
                if first:
                    chunk = bytes([chunk[0] ^ 1]) + chunk[1:]
                    first = False
                on_stdout(chunk)

            return run_child(argv, on_stdout=send, **kwargs)

        with patch("source.media.run_child", side_effect=corrupt_rgb):
            with self.assertRaisesRegex(ValueError, "decoder pixel/PTS witness mismatch"):
                media.decode(self.movie, track, lambda i, pixels: None)

    def test_packet_pts_cannot_be_filled_from_frame_times(self):
        from source.media import check_packet_pts
        expected = [10, 10, 80]
        complete = [{"pts": tick, "stream_index": 1} for tick in [80, 10, 10]]
        check_packet_pts(complete, 1, expected)
        check_packet_pts([{"pts": -10, "stream_index": 1, "flags": "KD"}, *complete], 1, expected)
        unsorted_dts = [{"dts": tick, "stream_index": 1} for tick in [80, 10, 10]]
        with self.assertRaisesRegex(ValueError, "join"):
            check_packet_pts(unsorted_dts, 1, expected, ffv1_block_time=True)
        with self.assertRaisesRegex(ValueError, "missing"):
            check_packet_pts(unsorted_dts, 1, expected)
        for invalid in [complete[:-1], [*complete[:-1], {"stream_index": 1}],
                        [*complete[:-1], {"stream_index": 1, "pts": 80}]]:
            with self.assertRaisesRegex(ValueError, "PTS"):
                check_packet_pts(invalid, 1, expected)

    def test_storage_limits_count_cumulative_png_bytes_and_every_json(self):
        sizes = [p.stat().st_size for p in (self.out / "artifacts").iterdir()]
        total = sum(p.stat().st_size for p in self.out.rglob("*") if p.is_file())
        self.assertLess(max(sizes), sum(sizes) - 1)
        for budget, message in [(sum(sizes) - 1, r"artifacts/.*\.png"), (total - 1, r"refs\.json")]:
            out = self.root / f"storage-limited-{budget}"
            with self.subTest(budget=budget), patch("source.ingest.MAX_ARTIFACT_BYTES", budget):
                with self.assertRaisesRegex(ValueError, f"^FRAME_STORAGE_LIMIT:.*{message}"):
                    ingest(self.movie, out)
                self.assertFalse(out.exists())
        out = self.root / "storage-exact"
        with patch("source.ingest.MAX_ARTIFACT_BYTES", total):
            ingest(self.movie, out)
        self.assertEqual(sum(p.stat().st_size for p in out.rglob("*") if p.is_file()), total)

    def test_resource_codes_and_no_publication(self):
        for setting, limit, code in [
            ("MAX_INPUT", 1, "INPUT_BYTES_LIMIT"),
            ("MAX_FRAMES", 1, "FRAME_COUNT_LIMIT"),
            ("MAX_FRAME", 1, "FRAME_BYTES_LIMIT"),
            ("MAX_DECODED", 64 * 48 * 3 * 2, "DECODED_BYTES_LIMIT"),
        ]:
            with self.subTest(setting=setting), patch.object(media, setting, limit):
                out = self.root / "limited"
                with self.assertRaisesRegex(ValueError, f"^{code}:"):
                    ingest(self.movie, out)
                self.assertFalse(out.exists())

    def test_packet_limit_precedes_frame_decode(self):
        with patch.object(media, "MAX_FRAMES", 2), patch.object(media, "run_child", wraps=run_child) as child:
            with self.assertRaisesRegex(ValueError, "FRAME_COUNT_LIMIT"):
                self.probe_fixture()
        args = [call.args[0] for call in child.call_args_list]
        self.assertTrue(any(any(part.startswith("packet=") for part in cmd) for cmd in args))
        self.assertFalse(any(any(part.startswith("frame=") for part in cmd) for cmd in args))

    def test_packet_probe_stops_on_first_over_limit_row(self):
        def feed(_path, _options, *, output, on_stdout):
            packet = b"stream_index=0|pts=1|dts=1|flags=K__\n"
            on_stdout(packet * 2)
            self.fail("packet probe consumed rows beyond the limit")

        with patch.object(media, "MAX_FRAMES", 1), patch.object(media, "_probe", side_effect=feed):
            with self.assertRaisesRegex(ValueError, "FRAME_COUNT_LIMIT"):
                media._probe_rows(self.movie, 0, (64, 48), "packet=stream_index,pts,dts,flags", packets=True)

    def test_declared_count_checked_before_demux(self):
        probe = media._probe

        def declare_oversized(path, options, **kwargs):
            self.assertTrue(any(option.startswith("stream=index") for option in options))
            result = probe(path, options, **kwargs)
            result["streams"][0]["nb_frames"] = str(media.MAX_FRAMES + 1)
            return result

        with patch.object(media, "_probe", side_effect=declare_oversized):
            with self.assertRaisesRegex(ValueError, "FRAME_COUNT_LIMIT"):
                self.probe_fixture()

    def test_frame_probe_rejects_extra_or_truncated_metadata_rows(self):
        def probe_rows(extra):
            def feed(_path, _options, *, output, on_stdout):
                self.assertEqual(output, "compact=p=0:nk=0")
                row = b"stream_index=0|pts=0|width=64|height=48\n"
                on_stdout(row[:11])
                on_stdout(row[11:] + extra)
            return feed

        row = b"stream_index=0|pts=1|width=64|height=48\n"
        with patch.object(media, "MAX_FRAMES", 1):
            for extra, error in ((row, "FRAME_COUNT_LIMIT"), (row[:-1], "truncated metadata row")):
                with self.subTest(error=error), patch.object(media, "_probe", side_effect=probe_rows(extra)):
                    with self.assertRaisesRegex(ValueError, error):
                        media._probe_rows(self.movie, 0, (64, 48), "frame=pts,width,height,stream_index")

    def test_snapshot_refuses_nonregular_and_changed_source(self):
        target = self.root / "snapshot-target"
        fifo = self.root / "source-fifo"
        os.mkfifo(fifo)
        plausible_size = SimpleNamespace(st_mode=fifo.stat().st_mode, st_size=1)
        with patch.object(media.os, "fstat", return_value=plausible_size):
            with self.assertRaisesRegex(ValueError, "regular file"):
                media.snapshot(fifo, target)
        self.assertFalse(target.exists())
        true_fstat = os.fstat
        calls = 0

        def changed_mtime(fd):
            nonlocal calls
            calls += 1
            info = true_fstat(fd)
            if calls == 1:
                return info
            return SimpleNamespace(st_mtime_ns=info.st_mtime_ns + 1)

        with patch.object(media.os, "fstat", side_effect=changed_mtime):
            with self.assertRaisesRegex(ValueError, "changed during admission"):
                media.snapshot(self.movie, target)

    def test_optional_probe_metadata_uses_observed_frame_count(self):
        probe = media._probe
        for missing in ("N/A", None):
            def replace_metadata(path, options, **kwargs):
                result = probe(path, options, **kwargs)
                if result is not None and "streams" in result:
                    result["streams"][0]["nb_frames"] = missing
                    result["format"]["duration"] = missing
                return result

            with self.subTest(missing=missing), patch.object(media, "_probe", side_effect=replace_metadata):
                path = ingest(self.movie, self.root / f"missing-{str(missing).replace('/', '')}")
            source = load(path, "video_source.json")
            self.assertEqual(source["frame_count"], 4)
            self.assertEqual(source["duration_ms"],
                             {"value": None, "reason": "source does not declare a duration"})

    def test_attached_picture_and_incorrect_declared_count_are_refused(self):
        probe = media._probe
        for attached, count, error in [(1, None, "ambiguous"), (0, "6", "incomplete container frame count")]:
            def replace_metadata(path, options, **kwargs):
                result = probe(path, options, **kwargs)
                if result is not None and "streams" in result:
                    result["streams"][0]["disposition"]["attached_pic"] = attached
                    result["streams"][0]["nb_frames"] = count
                return result

            with self.subTest(error=error), patch.object(media, "_probe", side_effect=replace_metadata):
                with self.assertRaisesRegex(ValueError, error):
                    self.probe_fixture()

    def test_decoder_arguments_pin_rgb_conversion(self):
        track = self.probe_fixture()
        with patch.object(media, "run_child", wraps=run_child) as child:
            media.decode(self.movie, track, lambda i, pixels: None)
        argv = child.call_args.args[0]
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "rgb24")
        self.assertEqual([argv[i + 1] for i, arg in enumerate(argv) if arg == "-threads"], ["1", "1"])
        self.assertEqual(argv[argv.index("-filter_threads") + 1], "1")
        self.assertEqual(argv[argv.index("-vf") + 1], "sws_flags=bicubic;format=rgb24,showinfo")

    def test_failed_decode_is_unpublished(self):
        bad = self.root / "bad.mkv"
        bad.write_bytes(b"truncated")
        with self.assertRaisesRegex(ValueError, "decode"):
            ingest(bad, self.root / "failed")
        self.assertFalse((self.root / "failed").exists())


def block(epoch, text):
    return FrameBlock(text.encode().ljust(3, b" "), 0, 0, 1, 1, epoch, False)


class SeamTests(unittest.TestCase):
    def test_release_and_expired_epoch(self):
        released = []
        q = FrameQueue(mode="on")
        epoch = q.epoch
        q.offer(epoch, block(epoch, "a"), lambda: released.append("a"))
        with q.borrow(epoch) as frame:
            self.assertEqual(frame().pixels, b"a  ")
            q.offer(epoch, block(epoch, "b"), lambda: released.append("b"))
            with self.assertRaises(BufferError):
                q.offer(epoch, block(epoch, "c"), lambda: released.append("c"))
            q.expire()
            with self.assertRaisesRegex(ValueError, "epoch"):
                frame()
        self.assertCountEqual(released, ["a", "b", "c"])
        with self.assertRaisesRegex(ValueError, "epoch"):
            q.offer(epoch, block(epoch, "d"), lambda: released.append("d"))
        self.assertEqual(released.count("d"), 1)

    def test_namespace_timeout_and_output_limits(self):
        with patch.dict("os.environ", {"AF_PROC_BACKEND": ""}):
            with self.assertRaisesRegex(ChildError, "namespace"):
                run_child(["true"])
        with self.assertRaisesRegex(ChildError, "^CHILD_TIMEOUT:"):
            run_child(["sleep", "10"], timeout=0.1)
        with self.assertRaisesRegex(ChildError, "^CHILD_TIMEOUT:"):
            run_child([sys.executable, "-B", "-c", "import os,time;os.close(1);os.close(2);time.sleep(5)"],
                      timeout=0.5)
        with self.assertRaisesRegex(ChildError, "^CHILD_OUTPUT_LIMIT:.*stdout"):
            run_child(["printf", "12345"], max_stdout=4)
        with self.assertRaisesRegex(ChildError, "^CHILD_OUTPUT_LIMIT:.*stderr"):
            run_child([sys.executable, "-B", "-c", "import os;os.write(2, b'12345')"], max_stderr=4)

    def test_namespace_requires_fd9_pid_chain_and_different_inode(self):
        with patch.dict(os.environ, {"AF_PROC_PIDNS_PROC": "/proc"}):
            with self.assertRaisesRegex(ChildError, "namespace"):
                require_namespace()
        pid = os.getpid()
        for outer in ((pid,), (pid + 1, pid + 2)):
            with self.subTest(outer=outer), patch("source.proc._namespace_pids",
                                                  side_effect=[(pid,), outer]):
                with self.assertRaisesRegex(ChildError, "namespace"):
                    require_namespace()
        with patch("source.proc.os.stat", return_value=SimpleNamespace(st_ino=42)):
            with self.assertRaisesRegex(ChildError, "namespace"):
                require_namespace()

    def test_toro_mode_provider_and_open_failure(self):
        backend = object()
        provider = Mock(return_value=backend)
        self.assertIs(open_frame_source("on", latest=True, toro_open=provider), backend)
        provider.assert_called_once_with(latest=True)
        provider.reset_mock()
        for mode in ("off", "observe"):
            self.assertEqual(open_frame_source(mode, toro_open=provider).mode, mode)
        with self.assertRaisesRegex(ValueError, "invalid Toro mode"):
            open_frame_source("invalid", toro_open=provider)
        provider.assert_not_called()

        def missing(**_options):
            raise OSError("backend refused")

        fallback = open_frame_source("on", latest=True, toro_open=missing)
        self.assertIsInstance(fallback, FrameQueue)
        self.assertTrue(fallback.latest)
        self.assertEqual(fallback.fallback_reason, "Toro open failed: backend refused")
        fallback = open_frame_source("on", toro_open=lambda **_options: None)
        self.assertEqual(fallback.fallback_reason, "Toro backend unavailable")

    def test_noncallable_release_cannot_queue_a_frame(self):
        q = FrameQueue()
        with self.assertRaisesRegex(TypeError, "release must be callable"):
            q.offer(q.epoch, block(q.epoch, "x"), None)
        with self.assertRaisesRegex(BufferError, "unavailable"):
            with q.borrow(q.epoch):
                self.fail("invalid offer occupied the waiting slot")

    def test_active_borrow_and_pixel_format_are_checked(self):
        q = FrameQueue()
        done = []
        with self.assertRaisesRegex(ValueError, "RGB"):
            q.offer(q.epoch, replace(block(q.epoch, "x"), pixel_format="bgr24"),
                    lambda: done.append("bad"))
        q.offer(q.epoch, block(q.epoch, "a"), lambda: done.append("a"))
        with q.borrow(q.epoch):
            q.offer(q.epoch, block(q.epoch, "b"), lambda: done.append("b"))
            with self.assertRaisesRegex(BufferError, "unavailable"):
                with q.borrow(q.epoch):
                    self.fail("an active borrow was displaced")
        with q.borrow(q.epoch) as read:
            self.assertEqual(read().pixels, b"b  ")
        self.assertEqual(done, ["bad", "a", "b"])

    def test_latest_releases_and_consumer_exception(self):
        q = FrameQueue(mode="observe", latest=True)
        settled = []
        for label in ("old", "new"):
            q.offer(q.epoch, block(q.epoch, label), lambda item=label: settled.append(item))
        self.assertEqual(settled, ["old"])
        with self.assertRaises(ArithmeticError):
            with q.borrow(q.epoch) as read:
                self.assertEqual(read().pixels, b"new")
                raise ArithmeticError()
        q.expire()
        q.expire()
        self.assertEqual(settled, ["old", "new"])
        with self.assertRaises(ValueError):
            read()

    def test_only_owned_child_reaped(self):
        # A real, harmless child reports its PID and group before it times out.
        reported = bytearray()
        parent_group = os.getpgrp()
        script = ("import os,signal,time;"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                  "print(os.getpid(), os.getpgrp(), flush=True);"
                  "time.sleep(1.2);print('survived', flush=True)")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "survived"
            script += ";open(__import__('sys').argv[1], 'x').close()"
            with patch("source.proc.os.killpg", side_effect=AssertionError("group signal attempted")):
                with self.assertRaisesRegex(ChildError, "timeout"):
                    run_child([sys.executable, "-B", "-c", script, str(marker)],
                              timeout=0.5, on_stdout=reported.extend)
            self.assertFalse(marker.exists())
        pid, group = map(int, reported.split())
        self.assertEqual(pid, group)
        self.assertNotEqual(group, parent_group)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        self.assertEqual(os.getpgrp(), parent_group)

    def test_foreign_block_and_bad_extent_release(self):
        q = FrameQueue()
        foreign = FrameQueue()
        released = []
        with self.assertRaisesRegex(ValueError, "epoch"):
            q.offer(q.epoch, block(foreign.epoch, "old"), lambda: released.append("foreign"))
        with self.assertRaisesRegex(ValueError, "extent"):
            q.offer(q.epoch, block(q.epoch, "oversized"), lambda: released.append("size"))
        self.assertEqual(released, ["foreign", "size"])


class RecordTests(unittest.TestCase):
    def test_contract_pin_mismatch_refuses_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ("frame", "video_source", "keyframe_set"):
                filename = f"rea.{name}_v1.schema.json"
                (path / filename).write_bytes((CONTRACTS / filename).read_bytes())
            pins = load(CONTRACTS, "PINS.json")
            pins["files"]["rea.frame_v1.schema.json"] = "0" * 64
            (path / "PINS.json").write_text(json.dumps(pins))
            with patch("source.records.CONTRACTS", path):
                with self.assertRaisesRegex(ValueError, "contract pin mismatch: frame"):
                    Contracts()

    def test_negative_origin_and_repeated_pts_are_preserved(self):
        cases = [
            ([-40, -40, -40], [0, 0, 0], {"value": None, "reason":
             "insufficient distinct presentation times for cadence"}),
            ([-40, -40, 0], [0, 0, 40000], {"value": None, "reason":
             "insufficient distinct presentation times for cadence"}),
        ]
        for pts, times, vfr in cases:
            with self.subTest(pts=pts), tempfile.TemporaryDirectory() as directory:
                track = media.Track("file", "video/mp4", 0, Fraction(1, 1000), 1, 1, pts,
                                    "h264", "mov", "pts", None, "test", "a" * 64, 1, "packet_pts")
                path = Path(directory)
                record = INGEST.Recording(path, track, Contracts(), FrameQueue(), 200)
                for i in range(len(pts)):
                    record.accept(i, b"rgb")
                record.finish()
                self.assertIs(load(path, "clock.json")["irregular_pts"], True)
                self.assertEqual(load(path, "video_source.json")["vfr"], vfr)
                manifest = load(path, "frames.manifest.json")
                self.assertEqual(manifest["provenance"]["origin_pts"], pts[0])
                self.assertEqual([f["t_us"] for f in manifest["frames"]], times)
