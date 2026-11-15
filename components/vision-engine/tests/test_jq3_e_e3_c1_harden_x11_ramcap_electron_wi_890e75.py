"""G-E3-3: real capture/reader paths with bounded, RAM-only test doubles.

No X server, frame files, network, sibling contracts, skips or expected failures.
Catalogue mutants are compiled in memory and are never written to a module file.
"""
import ast
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import random
import stat
import struct
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import numpy as np

from vadapt import xfb
from vadapt import x11_ramcap as ramcap


PROPERTY_CASES = 64
SCHEDULE_CASES = 32
VM_BUDGET_SECONDS = 15
_vm_started = None


def setUpModule():
    global _vm_started
    _vm_started = time.monotonic()


def tearDownModule():
    elapsed = time.monotonic() - _vm_started
    if elapsed >= VM_BUDGET_SECONDS:
        raise AssertionError("VM gate exceeded 15 second budget: %.3fs" % elapsed)
    print("VM gate elapsed: %.3fs (target <15s)" % elapsed)


def xwd_from_rgb(rgb, *, byte_order=0, pad=0, name=b"jq3\0", ncolors=0):
    """Independent tiny fixture encoder; integer pixels, not decoder slicing."""
    height, width, channels = rgb.shape
    if rgb.dtype != np.uint8 or channels != 3 or pad < 0 or pad % 4:
        raise ValueError("fixture requires uint8 RGB and aligned padding")
    size, stride = 100 + len(name), width * 4 + pad
    fields = (size, 7, 2, 24, width, height, 0, byte_order, 32,
              byte_order, 32, 32, stride, 4, 0xFF0000, 0xFF00, 0xFF,
              8, ncolors, ncolors, width, height, 0, 0, 0)
    raw = bytearray(struct.pack(">25I", *fields) + name)
    raw.extend(b"\x91" * (ncolors * 12))
    for row in rgb:
        for red, green, blue in row:
            word = (int(red) << 16) | (int(green) << 8) | int(blue)
            raw.extend(word.to_bytes(4, "little" if byte_order == 0 else "big"))
        raw.extend(b"\xa5" * pad)
    return bytes(raw)


class FakeClock:
    def __init__(self):
        self.value = 1000
        self.lock = threading.Lock()
        self.waits = []

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, amount):
        with self.lock:
            self.value += amount

    def wait(self, event, delay):
        self.waits.append(delay)
        self.advance(delay)
        return event.is_set()


class MemoryCapture:
    """Actual RamCapture lifecycle, with only its storage/source seams replaced.

    The writer intentionally accepts large indices so the *capture* guard must
    reject overflow. No ring allocation depends on the seeded frame counter.
    Directory verification is injected; real workspace containment and quota
    policy still execute, but no filesystem object stores a pixel.
    """

    def __init__(self, module=ramcap, raw=None):
        self.module = module
        self.rgb = np.array([[[7, 23, 251], [11, 199, 37]]], dtype=np.uint8)
        self.raw = xwd_from_rgb(self.rgb) if raw is None else raw
        self.clock = FakeClock()
        self.paths, self.rings = set(), {}
        self.workspaces, self.sources, self.events, self.writers, self.readers = [], [], [], [], []
        self.read_hook = self.check_hook = self.publish_hook = None
        self.operations = []
        self.workers = []
        self.config = module.SourceConfig(
            root="/__jq3_ram__/source", path="Xvfb_screen0", width=2, height=1,
            nocursor_evidence="trusted offline fixture: -nocursor", kind="xvfb-xwd")
        owner = self

        class Source:
            def __init__(self, config, max_bytes, tries):
                if config is not owner.config or max_bytes != len(owner.raw) or tries != 4:
                    raise AssertionError("capture did not use configured source/limits")
                self.closed = False
                owner.sources.append(self)

            def read(self, guard):
                guard()
                owner.operations.append("read")
                if owner.read_hook is not None:
                    owner.read_hook()
                return owner.raw

            def check(self):
                owner.operations.append("source.check")
                if owner.check_hook is not None:
                    owner.check_hook()

            def close(self):
                self.closed = True

        class Workspace:
            def __init__(self, session_id, quota):
                self.root = "/__jq3_ram__/sessions/" + session_id
                self.quota, self.closed = quota, False
                owner.paths.add(self.root)
                owner.workspaces.append(self)

            def path(self, rel):
                return self.root + "/" + rel

            def release(self, path):
                owner.paths.discard(str(path))

            def close(self):
                self.closed = True
                owner.paths.discard(self.root)

        class Writer:
            def __init__(self, workspace, slots, width, height):
                self.workspace, self.slots = workspace, slots
                self.path = workspace.path(module.rtring.REL)
                self.shape, self.index, self.published = (height, width, 3), -1, []
                self._mm = SimpleNamespace(closed=False)
                owner.paths.add(self.path)
                owner.rings[self.path] = self
                owner.writers.append(self)

            def publish(self, frame):
                if frame.dtype != np.uint8 or frame.shape != self.shape:
                    raise ValueError("wrong frame declaration reached writer")
                if owner.publish_hook is not None:
                    owner.publish_hook()
                self.index += 1
                self.published.append((self.index, bytes(frame.tobytes())))
                owner.operations.append("publish")
                return self.index

            def close(self):
                self._mm.closed = True
                self.workspace.release(self.path)

        class Reader:
            def __init__(self, path, *, root):
                self.writer = owner.rings[str(path)]
                if root != self.writer.workspace.root:
                    raise AssertionError("reader lost its workspace binding")
                self._mm = SimpleNamespace(closed=False)
                owner.readers.append(self)

            def acquire(self, index):
                actual, payload = self.writer.published[-1]
                if actual != index:
                    raise AssertionError("reader acquired wrong render index")
                view = np.frombuffer(payload, dtype=np.uint8).reshape(self.writer.shape)
                return SimpleNamespace(render_index=index, view=view,
                                       valid=lambda: self.writer.index == index)

            def close(self):
                self._mm.closed = True

        self.Source, self.Workspace, self.Writer, self.Reader = Source, Workspace, Writer, Reader
        self.stack, self.cap = ExitStack(), None

    def cancel_factory(self):
        event = threading.Event()
        self.events.append(event)
        return event

    def __enter__(self):
        m = self.module
        try:
            self.stack.enter_context(mock.patch.object(m, "_Source", self.Source))
            self.stack.enter_context(mock.patch.object(m.rtring, "RingWriter", self.Writer))
            self.stack.enter_context(mock.patch.object(m.rtring, "RingReader", self.Reader))
            self.stack.enter_context(mock.patch.object(m, "_directory", return_value=987654))
            self.stack.enter_context(mock.patch.object(m, "_ram_descriptor"))
            self.stack.enter_context(mock.patch.object(m.os, "close"))
            self.stack.enter_context(mock.patch.object(m.os.path, "lexists",
                                                      side_effect=lambda p: str(p) in self.paths))
            # Prohibit accidental storage fallback; injected classes use neither.
            self.stack.enter_context(mock.patch.object(m.os, "open", side_effect=AssertionError("filesystem open")))
            self.stack.enter_context(mock.patch("builtins.open", side_effect=AssertionError("persistent pixels")))
            size = m.rtring.ring_bytes(2, 2, 1)
            self.cap = m.RamCapture(
                {"screen": self.config}, max_frame_bytes=len(self.raw), max_session_bytes=size,
                max_total_bytes=size, max_sessions=1, ring_slots=2, interval_ns=10,
                ttl_ns=10000, tries=4, workspace_factory=self.Workspace,
                clock=self.clock, wait=self.clock.wait, cancel_factory=self.cancel_factory)
            return self
        except BaseException:
            self.stack.close()
            raise

    def start(self):
        return self.cap.Start("s", "screen", background=False)

    @property
    def session(self):
        return self.cap._sessions["s"]

    @property
    def writer(self):
        return self.writers[-1]

    def due(self):
        self.clock.advance(max(0, self.session.next_due_ns - self.clock()))

    def __exit__(self, *exc):
        try:
            alive = [worker.name for worker in self.workers if worker.is_alive()]
            if alive:
                # Never enter Reap's session lock after a bounded join failed.
                # The schedule already releases every owned Event and barrier.
                raise AssertionError("RAM fixture leaked threads: " + repr(alive))
            if self.cap is not None:
                result = self.cap.close()
                if result["retained"] or self.cap.SizeBytes() or self.paths:
                    raise AssertionError("RAM fixture leaked retained resources")
                if any(not source.closed for source in self.sources):
                    raise AssertionError("RAM fixture leaked a source")
                if any(not obj._mm.closed for obj in self.writers + self.readers):
                    raise AssertionError("RAM fixture leaked a ring")
        finally:
            self.stack.close()


class BoundaryMismatch(AssertionError):
    """Only intentional boundary semantic assertions can kill a mutant."""


def boundary_probe(module):
    """Exercise the final legal and first illegal head via public capture_once."""
    with MemoryCapture(module) as harness:
        harness.start()
        # Independent wire oracle: unsigned <Q head stores render_index + 1.
        max_head = int.from_bytes(b"\xff" * struct.calcsize("<Q"), "little")
        harness.session.frames = max_head - 1
        harness.writer.index = max_head - 2
        harness.due()
        try:
            frame = harness.cap.capture_once("s")
        except module.CaptureError as exc:
            if exc.code != "E_BUDGET":
                raise
            raise BoundaryMismatch("last legal frame refused") from exc
        if (frame is None or frame.render_index != max_head - 1
                or harness.session.frames != max_head
                or len(harness.writer.published) != 2):
            raise BoundaryMismatch("last legal frame did not preserve head/count association")
        harness.due()
        try:
            harness.cap.capture_once("s")
        except module.CaptureError as exc:
            if exc.code != "E_BUDGET" or str(exc) != "E_BUDGET ring render index exhausted":
                raise
        else:
            raise BoundaryMismatch("exhausted counter published a frame")
        if len(harness.writer.published) != 2 or harness.session.frames != max_head:
            raise BoundaryMismatch("exhaustion changed publication/count")
        if harness.session.state != "failed" or not harness.events[0].is_set():
            raise BoundaryMismatch("exhaustion did not fail closed")


@contextmanager
def catalogue_module(mutant):
    """Compile exactly one qualified AST change; preserve package/dataclass context."""
    source = Path(ramcap.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=ramcap.__file__)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RamCapture"]
    if len(classes) != 1:
        raise AssertionError("RamCapture class anchor must be unique")
    methods = [node for node in classes[0].body
               if isinstance(node, ast.FunctionDef) and node.name == "_capture"]
    if len(methods) != 1:
        raise AssertionError("RamCapture._capture anchor must be unique")
    target = ast.dump(ast.parse("s.frames >= (1 << 64) - 1", mode="eval").body)
    anchors = [node for node in ast.walk(methods[0])
               if isinstance(node, ast.Compare) and ast.dump(node) == target]
    if len(anchors) != 1:
        raise AssertionError("RamCapture._capture counter comparison anchor must be unique")
    bound = anchors[0].comparators[0]
    if mutant == "M-vadapt-x11-ramcap-1":
        bound.left.left.value += 1
    elif mutant == "M-vadapt-x11-ramcap-2":
        bound.right.value += 1
    elif mutant is not None:
        raise ValueError("unknown catalogue mutant")
    ast.fix_missing_locations(tree)
    name = "vadapt._jq3_counter_" + ("control" if mutant is None else mutant[-1])
    if name in sys.modules:
        raise AssertionError("mutant module name already in use")
    module = ModuleType(name)
    module.__package__, module.__file__ = "vadapt", ramcap.__file__
    sys.modules[name] = module
    try:
        # Compilation/import errors propagate as gate failures, never as kills.
        exec(compile(tree, ramcap.__file__, "exec"), module.__dict__)
        yield module
    finally:
        del sys.modules[name]


class TestCaptureContracts(unittest.TestCase):
    def test_exact_ram_pair_cadence_and_cancellation_cleanup(self):
        with MemoryCapture() as harness:
            metadata = harness.start()
            self.assertEqual(metadata["frames"], 1)
            self.assertEqual(harness.writer.published[0][1], harness.rgb.tobytes())
            self.assertIs(harness.session.cancel, harness.events[0])
            self.assertIs(harness.session.config, harness.config)
            self.assertIsNone(harness.cap.capture_once("s"))
            frame = harness.cap.capture_once("s", wait=True)
            self.assertEqual(harness.clock.waits, [10])
            self.assertEqual(frame.render_index, 1)
            self.assertEqual(frame.view.tobytes(), harness.rgb.tobytes())
            self.assertFalse(frame.view.flags.writeable)
            self.assertTrue(frame.valid())
            self.assertEqual(harness.operations[-3:], ["read", "source.check", "publish"])
            charge = harness.cap.SizeBytes()
            self.assertTrue(harness.cap.Stop("s"))
            self.assertTrue(harness.sources[0].closed)
            self.assertTrue(harness.events[0].is_set())
            self.assertEqual(harness.cap.SizeBytes(), charge)
            self.assertEqual(harness.cap.Reap("s"), {"reaped": ["s"], "retained": []})
            self.assertTrue(harness.workspaces[0].closed)

    def test_final_source_and_cancel_guards_refuse_same_pixels(self):
        for failure in ("source", "cancel"):
            with self.subTest(failure=failure), MemoryCapture() as harness:
                harness.start()
                harness.due()
                before = list(harness.writer.published)
                def changed():
                    if failure == "source":
                        raise ramcap.CaptureError("E_SOURCE_CHANGED", "stale revision")
                    harness.events[0].set()
                harness.check_hook = changed
                with self.assertRaises(ramcap.CaptureError) as caught:
                    harness.cap.capture_once("s")
                self.assertEqual(caught.exception.code, "E_SOURCE_CHANGED" if failure == "source" else "E_CANCELLED")
                self.assertEqual(harness.writer.published, before)
                self.assertEqual(harness.session.frames, 1)

    def test_uint64_last_legal_and_first_exhausted_frame(self):
        boundary_probe(ramcap)


class TestCatalogueMutants(unittest.TestCase):
    def test_semantic_kills_with_real_and_compiled_positive_controls(self):
        original_source = Path(ramcap.__file__).read_bytes()
        original_globals = (ramcap._Source, ramcap.rtring.RingWriter, ramcap.rtring.RingReader)
        boundary_probe(ramcap)
        with catalogue_module(None) as control:
            boundary_probe(control)
        killed = []
        expected_kills = {
            "M-vadapt-x11-ramcap-1": "exhausted counter published a frame",
            "M-vadapt-x11-ramcap-2": "last legal frame refused",
        }
        for mutant, expected_reason in expected_kills.items():
            with self.subTest(mutant=mutant):
                with catalogue_module(mutant) as module:
                    # Only this semantic AssertionError subtype counts; runtime,
                    # setup, cleanup, import, compile and timeout errors fail.
                    with self.assertRaises(BoundaryMismatch) as semantic:
                        boundary_probe(module)
                    self.assertEqual(str(semantic.exception), expected_reason)
                killed.append(mutant)
                print("VM mutant %s: semantic AssertionError: %s" % (mutant, semantic.exception))
                self.assertEqual(original_globals,
                                 (ramcap._Source, ramcap.rtring.RingWriter, ramcap.rtring.RingReader))
        self.assertEqual(len(killed), 2)
        self.assertEqual(Path(ramcap.__file__).read_bytes(), original_source)
        self.assertFalse(any(name.startswith("vadapt._jq3_counter_") for name in sys.modules))
        print("VM semantic mutant kills: " + ", ".join(killed))


class TestReaderContracts(unittest.TestCase):
    SEED = 0xE3C1890E75

    @staticmethod
    def file_stat(size, *, inode=73, mode=stat.S_IFREG | 0o600):
        return SimpleNamespace(st_dev=9, st_ino=inode, st_size=size, st_mode=mode)

    def test_64_seeded_layout_and_digest_cases(self):
        rng = random.Random(self.SEED)
        completed = 0
        for case in range(PROPERTY_CASES):
            width, height = rng.randint(1, 7), rng.randint(1, 5)
            triples = [tuple(rng.randrange(256) for _ in range(3))
                       for _ in range(width * height)]
            expected = bytes(channel for triple in triples for channel in triple)
            pixels = np.array(triples, dtype=np.uint8).reshape(height, width, 3)
            pad, ncolors = 4 * rng.randrange(4), rng.randrange(6)
            replay = f"seed={self.SEED:#x} case={case} size={width}x{height} pad={pad} ncolors={ncolors}"
            with self.subTest(replay=replay):
                first = xwd_from_rgb(pixels, byte_order=0, pad=pad,
                                     name=b"left\0", ncolors=ncolors)
                second = xwd_from_rgb(pixels, byte_order=1, pad=pad + 4,
                                      name=b"other declaration\0", ncolors=ncolors + 1)
                expected_hash = hashlib.sha256(expected).hexdigest()
                self.assertNotEqual(hashlib.sha256(first).digest(),
                                    hashlib.sha256(second).digest(), replay)
                for raw in (first, second):
                    header = xfb.validate_frame(raw, width=width, height=height)
                    self.assertEqual(xfb.rgb24(raw, header), expected, replay)
                    self.assertEqual(xfb.frame_sha256(raw, header), expected_hash, replay)
                    with self.assertRaises(xfb.XwdError) as rejected:
                        xfb.validate_frame(raw, width=width + 1, height=height)
                    self.assertEqual(rejected.exception.code, "E_XWD_GEOMETRY", replay)
                completed += 1
        self.assertEqual(completed, PROPERTY_CASES, f"seed={self.SEED:#x}: exhausted case budget")
        print(f"VM property cases: {completed}; seed={self.SEED:#x}")

    def test_same_pixels_do_not_override_declared_layout_or_cached_header(self):
        pixels = np.array([[[17, 39, 61], [83, 105, 127]]], dtype=np.uint8)
        horizontal = xwd_from_rgb(pixels)
        vertical = xwd_from_rgb(pixels.reshape(2, 1, 3))
        self.assertEqual(xfb.frame_sha256(horizontal), xfb.frame_sha256(vertical))
        self.assertNotEqual(hashlib.sha256(horizontal).digest(),
                            hashlib.sha256(vertical).digest())
        header = xfb.validate_frame(horizontal, width=2, height=1)
        self.assertEqual(xfb.rgb24(horizontal, header), bytes((17, 39, 61, 83, 105, 127)))
        for operation in (lambda: xfb.validate_frame(vertical, width=2, height=1),
                          lambda: xfb.rgb24(horizontal, header._replace(width=1, height=2))):
            with self.assertRaises(xfb.XwdError) as rejected:
                operation()
            self.assertEqual(rejected.exception.code, "E_XWD_GEOMETRY")
        chromium_bgra = np.zeros((8, 8, 4), dtype=np.uint8).tobytes()
        with self.assertRaises(xfb.XwdError):
            xfb.validate_frame(chromium_bgra)
        with self.assertRaises(ramcap.CaptureError) as rejected:
            ramcap.SourceConfig("/trusted", "screen0", 8, 8, "operator -nocursor",
                                kind="chromium-bgra")
        self.assertEqual(rejected.exception.code, "E_SOURCE_KIND")

    def test_real_read_screen_flags_exact_cap_and_owned_fd_cleanup(self):
        pixels = np.array([[[3, 7, 11], [13, 17, 19]]], dtype=np.uint8)
        raw = xwd_from_rgb(pixels)
        with mock.patch.object(xfb.os, "open", return_value=501) as opened, \
                mock.patch.object(xfb.os, "fstat", return_value=self.file_stat(len(raw))), \
                mock.patch.object(xfb.os, "pread", return_value=raw) as pread, \
                mock.patch.object(xfb.os, "close") as closed:
            self.assertEqual(xfb.read_screen("declared-screen", max_bytes=len(raw),
                                             width=2, height=1), raw)
            (path, flags), kwargs = opened.call_args
            self.assertEqual((path, kwargs), ("declared-screen", {}))
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            for flag in (os.O_NOFOLLOW, os.O_NONBLOCK, os.O_CLOEXEC):
                self.assertEqual(flags & flag, flag)
            self.assertEqual(pread.call_args_list,
                             [mock.call(501, len(raw), 0), mock.call(501, len(raw), 0)])
            closed.assert_called_once_with(501)

    def test_real_read_screen_failure_paths_close_without_publication(self):
        raw = xwd_from_rgb(np.zeros((2, 2, 3), dtype=np.uint8))
        good = self.file_stat(len(raw))
        cases = (("fifo", [self.file_stat(len(raw), mode=stat.S_IFIFO)], "E_XWD_SOURCE", 0),
                 ("over-cap", [self.file_stat(len(raw) + 1)], "E_XWD_LIMIT", 0),
                 ("replacement", [good, good, self.file_stat(len(raw), inode=74)], "E_XWD_SOURCE", 1))
        for label, stats, code, reads in cases:
            with self.subTest(case=label), \
                    mock.patch.object(xfb.os, "open", return_value=502), \
                    mock.patch.object(xfb.os, "fstat", side_effect=stats), \
                    mock.patch.object(xfb.os, "pread", return_value=raw) as pread, \
                    mock.patch.object(xfb.os, "close") as closed:
                with self.assertRaises(xfb.XwdError) as rejected:
                    xfb.read_screen("declared-screen", max_bytes=len(raw))
                self.assertEqual(rejected.exception.code, code)
                self.assertEqual(pread.call_count, reads)
                closed.assert_called_once_with(502)

    def test_real_fd_reader_rechecks_cancellation_and_retains_caller_fd(self):
        raw = xwd_from_rgb(np.zeros((1, 2, 3), dtype=np.uint8))
        cancelled = mock.Mock(side_effect=[None, None, ramcap.CaptureError("E_CANCELLED")])
        with mock.patch.object(xfb.os, "fstat", return_value=self.file_stat(len(raw))), \
                mock.patch.object(xfb.os, "pread", return_value=raw) as pread, \
                mock.patch.object(xfb.os, "close") as closed:
            with self.assertRaises(ramcap.CaptureError) as rejected:
                xfb.read_screen_fd(503, max_bytes=len(raw), before_read=cancelled)
            self.assertEqual(rejected.exception.code, "E_CANCELLED")
            self.assertEqual(pread.call_count, 2)
            self.assertEqual(cancelled.call_count, 3)
            closed.assert_not_called()

    def test_real_open_source_nested_flags_and_regular_file_cleanup(self):
        config = ramcap.SourceConfig("/trusted", "nested/screen0", 2, 1, "operator -nocursor")
        for mode, accepted in ((stat.S_IFREG | 0o600, True), (stat.S_IFIFO, False)):
            with self.subTest(mode=mode), \
                    mock.patch.object(ramcap, "_directory", return_value=601) as directory, \
                    mock.patch.object(ramcap, "_ram_descriptor") as attest, \
                    mock.patch.object(ramcap.os, "open", side_effect=[602, 603]) as opened, \
                    mock.patch.object(ramcap.os, "fstat", return_value=self.file_stat(128, mode=mode)), \
                    mock.patch.object(ramcap.os, "close") as closed:
                if accepted:
                    self.assertEqual(ramcap._open_source(config), 603)
                else:
                    with self.assertRaises(ramcap.CaptureError) as rejected:
                        ramcap._open_source(config)
                    self.assertEqual(rejected.exception.code, "E_SOURCE_KIND")
                directory.assert_called_once_with("/trusted")
                self.assertEqual(attest.call_args_list, [mock.call(601), mock.call(602), mock.call(603)])
                self.assertEqual(opened.call_count, 2)
                for call, component, parent in zip(opened.call_args_list,
                                                   ("nested", "screen0"), (601, 602)):
                    self.assertEqual(call.args[0], component)
                    self.assertEqual(call.kwargs, {"dir_fd": parent})
                    self.assertEqual(call.args[1] & os.O_ACCMODE, os.O_RDONLY)
                    for flag in (os.O_NOFOLLOW, os.O_CLOEXEC):
                        self.assertEqual(call.args[1] & flag, flag)
                self.assertTrue(opened.call_args_list[0].args[1] & os.O_DIRECTORY)
                self.assertTrue(opened.call_args_list[1].args[1] & os.O_NONBLOCK)
                expected = [mock.call(601), mock.call(602)] if accepted else [
                    mock.call(601), mock.call(603), mock.call(602)]
                self.assertEqual(closed.call_args_list, expected)

    def source_environment(self, raw):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            state = SimpleNamespace(raw=raw, inode=701, descriptors={}, closed=[], reads=[])

            def opened(config):
                fd = 800 + len(state.descriptors)
                state.descriptors[fd] = state.inode
                return fd

            def fstated(fd):
                return self.file_stat(len(state.raw), inode=state.descriptors[fd])

            def pread(fd, size, offset):
                state.reads.append((fd, size, offset))
                return state.raw[offset:offset + size]

            with mock.patch.object(ramcap, "_open_source", side_effect=opened), \
                    mock.patch.object(ramcap, "_ram_descriptor"), \
                    mock.patch.object(ramcap.os, "fstat", side_effect=fstated), \
                    mock.patch.object(ramcap.os, "pread", side_effect=pread), \
                    mock.patch.object(ramcap.os, "close", side_effect=state.closed.append):
                try:
                    yield state
                finally:
                    self.assertCountEqual(state.closed, state.descriptors)
                    self.assertEqual(len(state.closed), len(set(state.closed)), "descriptor closed twice")
        return scope()

    def test_real_source_rechecks_identical_pixels_with_stale_inode_or_layout(self):
        raw = xwd_from_rgb(np.array([[[2, 4, 6], [8, 10, 12]]], dtype=np.uint8))
        config = ramcap.SourceConfig("/trusted", "screen0", 2, 1, "operator -nocursor")
        with self.source_environment(raw) as state:
            source = ramcap._Source(config, len(raw), 2)
            try:
                self.assertEqual(source.read(lambda: None), raw)
                source.check()
                changed = bytearray(raw)
                struct.pack_into(">I", changed, 9 * 4, 1)
                state.raw = bytes(changed)
                self.assertEqual(xfb.frame_sha256(state.raw), xfb.frame_sha256(raw))
                with self.assertRaises(ramcap.CaptureError) as rejected:
                    source.read(lambda: None)
                self.assertEqual(rejected.exception.code, "E_SOURCE_GEOMETRY")
                state.raw, state.inode = raw, 702
                with self.assertRaises(ramcap.CaptureError) as rejected:
                    source.read(lambda: None)
                self.assertEqual(rejected.exception.code, "E_SOURCE_CHANGED")
                self.assertEqual(sum(size == len(raw) for _, size, _ in state.reads), 2)
            finally:
                source.close()
                source.close()

    def test_real_fbread_final_cancellation_closes_all_source_handles(self):
        raw = xwd_from_rgb(np.zeros((1, 2, 3), dtype=np.uint8))
        config = ramcap.SourceConfig("/trusted", "screen0", 2, 1, "operator -nocursor")
        # Initial guard, two snapshots, descriptor-reader return, source-reader return.
        cancelled = mock.Mock(side_effect=[False, False, False, False, True])
        with self.source_environment(raw) as state:
            with self.assertRaises(ramcap.CaptureError) as rejected:
                ramcap.fbread(config, max_bytes=len(raw), tries=2, cancelled=cancelled)
            self.assertEqual(rejected.exception.code, "E_CANCELLED")
            self.assertEqual(cancelled.call_count, 5)
            self.assertEqual(sum(size == len(raw) for _, size, _ in state.reads), 2)


class TestLogicalSchedules(unittest.TestCase):
    def test_32_recorded_capture_stop_schedules(self):
        seed = 0xE3C132
        rng = random.Random(seed)
        modes = ["publish-first", "cancel-read", "cancel-check", "cancel-wait"] * 8
        rng.shuffle(modes)
        self.assertEqual(len(modes), SCHEDULE_CASES)
        completed, remaining = 0, SCHEDULE_CASES
        deadline = (_vm_started if _vm_started is not None else time.monotonic()) + VM_BUDGET_SECONDS
        for case, mode in enumerate(modes):
            self.assertGreater(remaining, 0, "schedule case budget exhausted: seed=%d case=%d" % (seed, case))
            self.assertLess(time.monotonic(), deadline,
                            "VM schedule budget exhausted: seed=%d case=%d" % (seed, case))
            trace, outcomes, errors = [], {}, []
            lock = threading.Lock()
            ready = threading.Barrier(3, timeout=2)
            entered, release = threading.Event(), threading.Event()
            captured, stopped = threading.Event(), threading.Event()
            workers = []

            def record(operation):
                with lock:
                    trace.append(operation)

            def diagnostic():
                return "seed=%d case=%d mode=%s remaining=%d schedule=%r" % (
                    seed, case, mode, remaining, trace)

            def require(event, label):
                if not event.wait(2):
                    raise AssertionError(label + ": " + diagnostic())

            with self.subTest(seed=seed, case=case, mode=mode), MemoryCapture() as h:
                h.workers = workers
                metadata = h.start()
                charge = h.cap.SizeBytes("s")
                self.assertGreater(charge, 0, diagnostic())
                cancel = h.events[0]
                if mode != "cancel-wait":
                    h.due()

                def gate():
                    record("gate.enter")
                    entered.set()
                    require(release, "capture gate was not released")
                    record("gate.exit")

                def wait_gate(event, delay):
                    if event is not cancel or delay != 10:
                        raise AssertionError("wrong cadence/cancel binding: " + diagnostic())
                    gate()
                    return event.is_set()

                def publication():
                    if cancel.is_set():
                        raise AssertionError("publication after cancellation: " + diagnostic())
                    record("publish")

                h.publish_hook = publication
                if mode == "cancel-read":
                    h.read_hook = gate
                elif mode == "cancel-check":
                    h.check_hook = gate
                elif mode == "cancel-wait":
                    h.cap.wait = wait_gate

                def capture():
                    try:
                        ready.wait(2)
                        record("capture.call")
                        try:
                            frame = h.cap.capture_once("s", wait=mode == "cancel-wait")
                        except ramcap.CaptureError as exc:
                            outcomes["capture"] = exc.code
                        else:
                            outcomes["capture"] = None if frame is None else frame.render_index
                        record("capture.return")
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        captured.set()

                def stop():
                    try:
                        ready.wait(2)
                        require(captured if mode == "publish-first" else entered,
                                "stop prerequisite missing")
                        record("stop.call")
                        outcomes["stop"] = h.cap.Stop("s")
                        record("stop.return")
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        stopped.set()

                functions = [capture, stop]
                rng.shuffle(functions)
                try:
                    for fn in functions:
                        worker = threading.Thread(target=fn, name="jq3-%d-%s" % (case, fn.__name__),
                                                  daemon=True)
                        workers.append(worker)
                        worker.start()
                    ready.wait(2)
                    if mode != "publish-first":
                        require(stopped if mode == "cancel-wait" else cancel,
                                "cancellation did not reach logical boundary")
                        self.assertTrue(cancel.is_set(), diagnostic())
                        record("cancel.observed")
                        record("gate.release")
                        release.set()
                    for worker in workers:
                        worker.join(2)
                    self.assertFalse(any(worker.is_alive() for worker in workers), diagnostic())
                    self.assertEqual(errors, [], diagnostic())
                    expected = 1 if mode == "publish-first" else (
                        "E_SESSION_STOPPED" if mode == "cancel-wait" else "E_CANCELLED")
                    self.assertEqual(outcomes, {"capture": expected, "stop": True}, diagnostic())
                    self.assertEqual(trace.count("publish"), int(mode == "publish-first"), diagnostic())
                    if mode == "publish-first":
                        self.assertLess(trace.index("publish"), trace.index("capture.return"), diagnostic())
                        self.assertLess(trace.index("capture.return"), trace.index("stop.call"), diagnostic())
                    else:
                        ordered = ["gate.enter", "stop.call", "cancel.observed", "gate.release", "gate.exit"]
                        self.assertEqual([trace.index(op) for op in ordered],
                                         sorted(trace.index(op) for op in ordered), diagnostic())
                    self.assertEqual(len(h.writer.published), 1 + int(mode == "publish-first"), diagnostic())
                    self.assertEqual(h.session.frames, metadata["frames"] + int(mode == "publish-first"), diagnostic())
                    self.assertEqual(h.session.state, "stopped", diagnostic())
                    self.assertEqual(h.cap.SizeBytes("s"), charge, diagnostic())
                    self.assertTrue(all(source.closed for source in h.sources), diagnostic())
                    self.assertEqual(h.cap.Reap("s"), {"reaped": ["s"], "retained": []}, diagnostic())
                    self.assertEqual(h.cap.SizeBytes(), 0, diagnostic())
                    completed, remaining = completed + 1, remaining - 1
                finally:
                    for event in (release, entered, captured, stopped, cancel):
                        event.set()
                    ready.abort()
                    for worker in workers:
                        if worker.ident is not None:
                            worker.join(2)
                    self.assertFalse(any(worker.is_alive() for worker in workers), diagnostic())
            # A subTest records ordinary failures and continues. A leaked worker
            # must abort the loop before another schedule can create threads.
            self.assertFalse(any(worker.is_alive() for worker in workers), diagnostic())
        self.assertEqual((completed, remaining), (SCHEDULE_CASES, 0))
        print(f"VM logical schedules: {completed}; seed={seed:#x}; 4 modes x 8; max 3 threads")


if __name__ == "__main__":
    unittest.main()
