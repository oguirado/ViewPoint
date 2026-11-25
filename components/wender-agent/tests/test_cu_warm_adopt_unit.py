# @module {"id":"wa.tests.test_cu_warm_adopt_unit","layer":2,"owner":"LEARN","deps":["wa.learn.cu_loop"],"book":"HB-WA-s4-learn"}
"""Offline product-path checks for an episode-owned vserve child."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_cu_loop_unit import proposal
from wender.learn.cu_loop import (
    BridgeDescriber, FakeCapsule, ReplayDecider, WarmBridgeDescriber,
    _encode_png, run_loop,
)
from wender.perceive.vision_bridge import SubprocessVisionEngine, VisionBridgeError


_SERVER = """\
import hashlib, json, os, signal, sys, time
from pathlib import Path
from PIL import Image

with open('starts', 'a') as starts:
    starts.write(str(os.getpid()) + '\\n')
for line in sys.stdin:
    request = json.loads(line)
    op = request['op']
    if op == 'hello':
        result = dict(schema='ve.vserve.hello/v1', capabilities=['describe', 'frame_raw'],
                      raw_shm=True, frame_raw=True, ring=False, max_control_line_bytes=65536,
                      max_frame_bytes=33554432, max_sessions=16)
    else:
        mode = Path('mode').read_text()
        if mode == 'exit':
            raise SystemExit(5)
        if mode == 'hold':
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if 'marks_out' in request:
                Path(request['marks_out']).write_bytes(b'unfinished')
            Path('waiting').touch()
            while Path('mode').read_text() == 'hold':
                time.sleep(.01)
        with Image.open(request['frame']) as frame:
            rgb = frame.convert('RGB').tobytes()
            width, height = frame.size
        result = dict(schema='ve.cu_describe/v1', frame_sha256=hashlib.sha256(rgb).hexdigest(),
                      width=width, height=height, timing_us=1, changed=None,
                      changed_regions=[], crops=[])
        if 'marks_out' in request:
            Path(request['marks_out']).write_bytes(Path(request['frame']).read_bytes())
            result.update(marks=[], marks_png=request['marks_out'])
    print(json.dumps(dict(schema='ve.vserve.reply/v1', request_id=request['request_id'],
                          op=op, ok=True, rc=0, code='OK', detail={}, result=result)), flush=True)
"""


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for fixture event")
        time.sleep(.01)


class WarmAdoptTests(unittest.TestCase):
    def setUp(self):
        self.ram = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/dev/shm")))
        self.ve = self.ram / "ve"
        package = self.ve / "vadapt"
        package.mkdir(parents=True)
        (package / "__init__.py").touch()
        (package / "vserve.py").write_text(_SERVER)
        (self.ve / "mode").write_text("ok")

    def capsule(self, name, paths=()):
        directory = self.ram / name
        directory.mkdir(exist_ok=True)
        if not paths:
            frame = directory / "frame.png"
            frame.write_bytes(_encode_png(bytes(range(48)), 4, 4))
            paths = (frame,)
        (directory / "index.json").write_text(json.dumps([
            {"seq": i, "png": path.name} for i, path in enumerate(paths)
        ]))
        return FakeCapsule(directory)

    def warm(self, capsule, *, seat_id="seat-a", trust_domain="domain-a", **kwargs):
        scope = WarmBridgeDescriber(capsule, seat_id=seat_id, trust_domain=trust_domain,
                                    ve_root=kwargs.pop("ve_root", self.ve), **kwargs)
        self.addCleanup(scope.fence)
        return scope

    def run_episode(self, capsule, describer, decider=None, *,
                    seat_id="seat-a", trust_domain="domain-a"):
        return run_loop(capsule, describer, decider or ReplayDecider([proposal("done")]),
                        goal="fixture", trace=io.StringIO(), pixel_only=True,
                        seat_id=seat_id, trust_domain=trust_domain)

    def observe_child(self, scope):
        child = scope._engine._client._proc
        self.assertIsNotNone(child)
        self.assertIsNone(child.poll())
        return child.pid

    def assert_reaped(self, *pids):
        for pid in pids:
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)

    def test_default_builds_the_old_adapter(self):
        with patch("wender.learn.cu_loop.SubprocessVisionEngine", wraps=SubprocessVisionEngine) as engine:
            cold = BridgeDescriber(self.ve)
            engine.assert_called_once_with(self.ve, timeout=30)
        self.assertIsNone(cold.engine._client)

    def test_real_png_parity(self):
        for marks in (False, True):
            with self.subTest(marks=marks):
                self._assert_real_png_parity(marks)

    def _assert_real_png_parity(self, marks):
        fixed_root = os.getenv("LV05_TEST_VE_ROOT")
        if not fixed_root:
            self.skipTest("set LV05_TEST_VE_ROOT to a pinned Vision Engine checkout")
        from PIL import Image, ImageDraw

        directory = self.ram / f"parity-{marks}"
        directory.mkdir()
        pairs = []
        for index in range(7):
            background = (22, 24, 28) if index % 2 else (144, 172, 200)
            previous = Image.new("RGB", (320, 240), background)
            drawing = ImageDraw.Draw(previous)
            for column in range(9):
                x = (32 * column + 19 * index) % 288
                y = (23 * column + 29 * index) % 208
                drawing.rectangle((x, y, x + 30, y + 30), fill=(180, 32 + 20 * column, 92))
            current = previous.copy()
            if index % 3 != 1:
                x, y = ((32 * index) % 216, (24 * index) % 136)
                ImageDraw.Draw(current).rectangle((x, y, x + 103, y + 103), fill=(255, 253, 251))
            previous_path = directory / f"{index}-prev.png"
            current_path = directory / f"{index}-frame.png"
            previous.save(previous_path)
            current.save(current_path)
            pairs.append((previous_path, current_path))
        paths = [path for pair in pairs for path in pair]
        capsule = self.capsule(directory.name, paths)
        mark_options = {} if marks else {"marks": False}
        cold = BridgeDescriber(fixed_root, **mark_options)
        warm = self.warm(capsule, ve_root=fixed_root, **mark_options)
        original = SubprocessVisionEngine.describe
        records = {engine: [] for engine in (cold.engine, warm._engine)}
        warm_pids = set()

        def describe(engine, frame, prev=None, **kwargs):
            result = original(engine, frame, prev=prev, **kwargs)
            png = None
            if marks:
                self.assertEqual(result.marks_png, str(kwargs["marks_out"]))
                output = Path(result.marks_png)
                png = output.read_bytes()
                self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
                output.unlink()
            records[engine].append((frame, prev, result, png))
            self.assertEqual(engine.last_transport, "vserve" if engine is warm._engine else "c2")
            if engine is warm._engine:
                warm_pids.add(self.observe_child(warm))
            return result

        actions = [proposal()] * (len(pairs) * 2 - 1) + [proposal("done")]
        with warm, patch.object(SubprocessVisionEngine, "describe", describe):
            for active, describe_scope in ((self.capsule(directory.name, paths), cold), (capsule, warm)):
                if describe_scope is warm and marks:
                    # Share the pathname across sequential arms so marks_png
                    # is part of the exact comparison, along with the PNG.
                    warm._marks_dir, cold._marks_dir = cold._marks_dir, None
                # The generated cases include unchanged pairs. Keep the poll
                # short while still passing each explicit previous frame.
                result = run_loop(active, describe_scope, ReplayDecider(actions), goal="fixture",
                                  trace=io.StringIO(), pixel_only=True, after_timeout_ms=1,
                                  no_progress_steps=100, seat_id="seat-a", trust_domain="domain-a")
                self.assertEqual(result["stop_reason"], "done", result)

        def canonical(rows):
            records = []
            for frame, prev, result, png in rows:
                data = asdict(result)
                del data["timing_us"]
                records.append((frame, prev, json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), png))
            return records

        self.assertEqual(canonical(records[warm._engine]), canonical(records[cold.engine]))
        self.assertTrue(all(any(frame == str(current) and prev == str(previous)
                                for frame, prev, _, _ in records[warm._engine])
                            for previous, current in pairs))
        self.assertEqual(len(warm_pids), 1)
        self.assert_reaped(*warm_pids)

    def test_two_seats_and_foreign_capsule_refusal(self):
        cap_a, cap_b = self.capsule("a"), self.capsule("b")
        warm_a = self.warm(cap_a)
        warm_b = self.warm(cap_b, seat_id="seat-b", trust_domain="domain-b")
        duplicate = self.warm(cap_a, seat_id="seat-b", trust_domain="domain-b")
        self.assertFalse(hasattr(warm_a, "engine"))
        entered = threading.Event()
        release = threading.Event()
        pids = []
        fixture = self

        class ParkedDecider:
            backend = "fixture"

            def decide(self, **kwargs):
                pids.append(fixture.observe_child(warm_a))
                entered.set()
                assert release.wait(5)
                return proposal("done")

        with ThreadPoolExecutor(1) as pool:
            episode = pool.submit(self.run_episode, cap_a, warm_a, ParkedDecider())
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(VisionBridgeError, "another capsule"):
                    self.run_episode(cap_b, warm_a)
                for seat, domain, field in (("seat-b", "domain-a", "seat"),
                                            ("seat-a", "domain-b", "trust domain"),
                                            (None, "domain-a", "seat"),
                                            ("seat-a", None, "trust domain")):
                    with self.subTest(seat=seat, domain=domain):
                        with self.assertRaisesRegex(VisionBridgeError, "authenticated " + field):
                            self.run_episode(cap_a, warm_a, seat_id=seat, trust_domain=domain)
                with self.assertRaisesRegex(VisionBridgeError, "used or fenced"):
                    self.run_episode(cap_a, warm_a)
                with self.assertRaisesRegex(VisionBridgeError, "already has an active"):
                    self.run_episode(cap_a, duplicate, seat_id="seat-b", trust_domain="domain-b")
                with self.assertRaisesRegex(VisionBridgeError, "bound run_loop"):
                    warm_a.describe(cap_b.frame())

                class OtherDecider:
                    backend = "fixture"

                    def decide(self, **kwargs):
                        pids.append(fixture.observe_child(warm_b))
                        fixture.assertEqual(fixture.observe_child(warm_a), pids[0])
                        return proposal("done")

                other = self.run_episode(cap_b, warm_b, OtherDecider(),
                                         seat_id="seat-b", trust_domain="domain-b")
                self.assertEqual(other["stop_reason"], "done")
                self.assertEqual(len(set(pids)), 2)
                self.assertEqual(len((self.ve / "starts").read_text().splitlines()), 2)
            finally:
                release.set()
            self.assertEqual(episode.result(timeout=5)["stop_reason"], "done")
        self.assert_reaped(*pids)
        fresh = self.run_episode(cap_a, duplicate, seat_id="seat-b", trust_domain="domain-b")
        self.assertEqual(fresh["stop_reason"], "done")
        self.assert_reaped(int((self.ve / "starts").read_text().splitlines()[-1]))

    def test_context_exit_fences_lazy_scope_on_error(self):
        capsule = self.capsule("context")
        warm = self.warm(capsule)
        with self.assertRaisesRegex(RuntimeError, "caller abort"):
            with warm as scope:
                self.assertIs(scope, warm)
                raise RuntimeError("caller abort")
        self.assertFalse((self.ve / "starts").exists())
        with self.assertRaisesRegex(VisionBridgeError, "used or fenced"):
            self.run_episode(capsule, warm)

    def test_fence_discards_inflight_reply_and_removes_marks(self):
        capsule = self.capsule("fence")
        warm = self.warm(capsule)
        (self.ve / "mode").write_text("hold")
        with ThreadPoolExecutor(2) as pool:
            episode = pool.submit(self.run_episode, capsule, warm)
            _wait_for((self.ve / "waiting").exists)
            pid = self.observe_child(warm)
            marks_dir = Path(warm._marks_dir.name)
            self.assertEqual((marks_dir / "marks.png").read_bytes(), b"unfinished")
            started = time.monotonic()
            fence = pool.submit(warm.fence)
            fence.result(timeout=2)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
            self.assertEqual((self.ve / "mode").read_text(), "hold")
            self.assertFalse(marks_dir.exists())
            self.assert_reaped(pid)
            print(f"warm fence cancellation and cleanup: {elapsed * 1000:.3f} ms")
            result = episode.result(timeout=2)
        self.assertEqual(result["failure"]["stage"], "describe")
        self.assertEqual(result["stop_reason"], "failure")
        self.assertEqual(capsule.inputs, [])
        with self.assertRaisesRegex(VisionBridgeError, "used or fenced"):
            self.run_episode(capsule, warm)

    def test_decision_error_reaps_child(self):
        capsule = self.capsule("failure")
        warm = self.warm(capsule)
        result = self.run_episode(capsule, warm, ReplayDecider([]))
        self.assertEqual(result["failure"]["stage"], "decision")
        self.assertEqual(capsule.inputs, [])
        self.assert_reaped(int((self.ve / "starts").read_text()))

    def test_timeout_and_service_exit_fail_closed(self):
        for mode, diagnostic, reason in (("hold", "timed out", "timeout"), ("exit", "vserve EOF", "failure")):
            with self.subTest(mode=mode):
                capsule = self.capsule(mode)
                warm = self.warm(capsule, timeout=1)
                (self.ve / "starts").unlink(missing_ok=True)
                (self.ve / "mode").write_text(mode)
                with patch("wender.perceive.vision_bridge.subprocess.run") as cold:
                    result = self.run_episode(capsule, warm)
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(result["failure"]["stage"], "describe")
                self.assertEqual(capsule.inputs, [])
                self.assertIsNone(warm._engine.last_transport)
                self.assertIn(diagnostic, str(warm._engine.last_vserve_error))
                cold.assert_not_called()
                self.assert_reaped(int((self.ve / "starts").read_text()))
