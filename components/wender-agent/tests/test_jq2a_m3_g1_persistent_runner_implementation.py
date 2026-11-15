"""Unit tests for RealVisionEngine.describe_many persistent runner path.

Exercising production public paths with both the named mutant distinctions
(gate G M3 3: item failure isolated, runner reused, reject mutant spawn per frame
or retry completed item, positive control fake runner serves repeated batches)
and healthy controls in offline RAM environment.
"""
import sys
import types
import unittest
import numpy as np

# Ensure canonical session_registry export compatibility
import wender.perceive.vision_engine_real as vision_engine_real
from wender.perceive.vision_engine_real import (
    RealVisionEngine,
    Handle,
    TargetRecord,
    FrameRef,
    Observation,
    Receipt,
    ActionRequest,
    ActorLease,
    FocusLease,
    StreamSpec,
    StreamToken,
    Ticket,
    ChangeResult,
    PerceptionResult,
    WindowFrames,
    WindowDescription,
    SUPPORTED_TARGET_KINDS,
    FrameValidationError,
    InvalidDprError,
    InvalidFrameSequenceError,
    InvalidTargetError,
    FrameDtypeError,
    RunnerDeathError,
    validate_window_frames,
)

# Export to sys.modules for any canonical session_registry imports
if "wender.perceive.session_registry" not in sys.modules:
    _sr_mod = types.ModuleType("wender.perceive.session_registry")
    for _name in (
        "Handle", "TargetRecord", "FrameRef", "Observation", "Receipt",
        "ActionRequest", "ActorLease", "FocusLease", "StreamSpec", "StreamToken",
        "Ticket", "ChangeResult", "PerceptionResult", "WindowFrames", "WindowDescription",
    ):
        setattr(_sr_mod, _name, getattr(vision_engine_real, _name))
    sys.modules["wender.perceive.session_registry"] = _sr_mod
    import wender.perceive
    wender.perceive.session_registry = _sr_mod


class FakePersistentRunner:
    """Fake persistent worker runner for offline testing."""

    def __init__(self, fail_handles=None, die_on_handle=None):
        self.invocations = 0
        self.dispatched_items = []
        self.spawn_count = 1
        self.alive = True
        self.fail_handles = fail_handles or set()
        self.die_on_handle = die_on_handle

    def is_alive(self):
        return self.alive

    def describe_window(self, item: WindowFrames) -> WindowDescription:
        self.invocations += 1
        self.dispatched_items.append(item.handle.canonical_id)

        if not self.alive:
            raise RunnerDeathError("Runner is dead")

        if self.die_on_handle and item.handle.canonical_id == self.die_on_handle:
            self.alive = False
            raise RunnerDeathError(f"Runner crashed on target {item.handle.canonical_id}")

        if item.handle.canonical_id in self.fail_handles:
            raise ValueError(f"Per-item processing error for {item.handle.canonical_id}")

        # Produce deterministic descriptors and regions
        descriptors = tuple(f"desc_{seq}" for seq in item.frame_seqs)
        regions = tuple(
            [{"bbox_px": [0, 0, 10, 10], "n_tiles": 1}] for _ in item.frame_seqs
        )
        return WindowDescription(
            handle=item.handle,
            epoch=item.epoch,
            frame_seqs=item.frame_seqs,
            descriptors=descriptors,
            regions=regions,
            error=None,
        )


def _make_frame(h=20, w=30, fill=128):
    arr = np.full((h, w, 3), fill, dtype=np.uint8)
    return arr


def _make_window(kind="cdp_tab", canonical_id="win-1", generation=1, epoch=0,
                 n_frames=2, dpr_ppm=1000000, h=20, w=30, fill=128):
    handle = Handle(kind=kind, canonical_id=canonical_id, generation=generation)
    frames = tuple(_make_frame(h, w, fill + i) for i in range(n_frames))
    seqs = tuple(range(n_frames))
    return WindowFrames(
        handle=handle,
        epoch=epoch,
        frames=frames,
        frame_seqs=seqs,
        dpr_ppm=dpr_ppm,
    )


class TestPersistentRunnerPositiveControls(unittest.TestCase):
    """Positive controls: healthy single batch and multi-batch execution."""

    def test_describe_many_healthy_control(self):
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        items = [
            _make_window(kind="cdp_tab", canonical_id="tab-1", n_frames=2),
            _make_window(kind="x11_window", canonical_id="win-1", n_frames=3),
            _make_window(kind="godot_window", canonical_id="godot-1", n_frames=1),
        ]

        results = engine.describe_many(items)

        self.assertEqual(len(results), 3)
        for i, item in enumerate(items):
            res = results[i]
            self.assertEqual(res.handle, item.handle)
            self.assertIsNone(res.error)
            self.assertIsNotNone(res.observation)
            obs = res.observation
            self.assertEqual(obs.handle, item.handle)
            self.assertEqual(obs.epoch, item.epoch)
            self.assertEqual(obs.dpr_ppm, item.dpr_ppm)
            self.assertEqual(obs.frame_seq, item.frame_seqs[-1])
            self.assertIsInstance(obs.frame, FrameRef)
            self.assertEqual(obs.frame.width, 30)
            self.assertEqual(obs.frame.height, 20)
            self.assertFalse(obs.frame.rgb.flags.writeable)

        # Runner was invoked once per window sequence
        self.assertEqual(runner.invocations, 3)

    def test_runner_reused_across_batches(self):
        """Positive control: fake runner serves repeated batches without respawning."""
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        batch_1 = [_make_window(canonical_id="b1-1"), _make_window(canonical_id="b1-2")]
        batch_2 = [_make_window(canonical_id="b2-1"), _make_window(canonical_id="b2-2")]

        res_1 = engine.describe_many(batch_1)
        res_2 = engine.describe_many(batch_2)

        self.assertEqual(len(res_1), 2)
        self.assertEqual(len(res_2), 2)
        # Verify runner was reused across batches (4 total invocations on the same runner instance)
        self.assertEqual(runner.invocations, 4)
        self.assertEqual(runner.spawn_count, 1)

    def test_injected_runner_overrides_instance_runner_for_single_call(self):
        instance_runner = FakePersistentRunner()
        call_runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=instance_runner)

        items = [_make_window(canonical_id="win-test")]
        # Injected call-level runner override
        results = engine.describe_many(items, runner=call_runner)

        self.assertEqual(len(results), 1)
        self.assertEqual(call_runner.invocations, 1)
        self.assertEqual(instance_runner.invocations, 0)
        self.assertIs(engine.runner, instance_runner)

        # Subsequent call without runner uses instance_runner
        results_sub = engine.describe_many(items)
        self.assertEqual(len(results_sub), 1)
        self.assertEqual(instance_runner.invocations, 1)
        self.assertEqual(call_runner.invocations, 1)

    def test_admitted_frames_are_read_only_and_owned(self):
        """INV-M3-1, INV-M3-3: copy into owned RAM and make read-only after admission."""
        raw_frame = np.full((10, 10, 3), 50, dtype=np.uint8)
        self.assertTrue(raw_frame.flags.writeable)

        handle = Handle("cdp_tab", "tab-copy", 1)
        window = WindowFrames(handle=handle, epoch=0, frames=(raw_frame,), frame_seqs=(0,), dpr_ppm=1000000)

        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)
        results = engine.describe_many([window])

        self.assertIsNone(results[0].error)
        obs_frame = results[0].observation.frame
        self.assertFalse(obs_frame.rgb.flags.writeable)

        # Modifying original frame does NOT mutate admitted FrameRef
        raw_frame[0, 0, 0] = 255
        self.assertEqual(obs_frame.rgb[0, 0, 0], 50)


class TestItemFailureIsolation(unittest.TestCase):
    """Gate G M3 3: Item failure isolated."""

    def test_item_failure_isolated_invalid_frames(self):
        """Item with non-uint8 float array is isolated; sibling items succeed."""
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        item1 = _make_window(canonical_id="item-1-ok")
        # item2 has float32 frames
        bad_frame = np.full((20, 30, 3), 128.0, dtype=np.float32)
        item2 = types.SimpleNamespace(
            handle=Handle("cdp_tab", "item-2-bad", 1),
            epoch=0,
            frames=(bad_frame,),
            frame_seqs=(0,),
            dpr_ppm=1000000,
        )
        item3 = _make_window(canonical_id="item-3-ok")

        results = engine.describe_many([item1, item2, item3])

        self.assertEqual(len(results), 3)
        self.assertIsNone(results[0].error)
        self.assertIsNotNone(results[0].observation)

        # item2 error is typed and isolated
        self.assertIsNotNone(results[1].error)
        self.assertIn("FrameDtypeError", results[1].error)
        self.assertIsNone(results[1].observation)

        # item3 was not affected
        self.assertIsNone(results[2].error)
        self.assertIsNotNone(results[2].observation)

    def test_item_failure_isolated_invalid_dpr(self):
        """Item with non-positive DPR fails with InvalidDprError; sibling items succeed."""
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        item1 = _make_window(canonical_id="item-1-ok")
        # Attempt negative or zero dpr
        with self.assertRaises(ValueError):
            WindowFrames(
                handle=Handle("cdp_tab", "item-2-dpr0", 1),
                epoch=0,
                frames=(_make_frame(),),
                frame_seqs=(0,),
                dpr_ppm=0,
            )

        # Duck-typed object with dpr_ppm=-1 bypassing constructor
        class DuckWindow:
            handle = Handle("cdp_tab", "item-2-dpr-neg", 1)
            epoch = 0
            frames = (_make_frame(),)
            frame_seqs = (0,)
            dpr_ppm = -500

        item3 = _make_window(canonical_id="item-3-ok")
        results = engine.describe_many([item1, DuckWindow(), item3])

        self.assertIsNone(results[0].error)
        self.assertIn("InvalidDprError", results[1].error)
        self.assertIsNone(results[2].error)

    def test_item_failure_isolated_invalid_sequence(self):
        """Item with non-increasing frame sequences fails; siblings succeed."""
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        item1 = _make_window(canonical_id="item-1-ok")

        class DuckBadSeq:
            handle = Handle("cdp_tab", "item-bad-seq", 1)
            epoch = 0
            frames = (_make_frame(), _make_frame())
            frame_seqs = (5, 3)  # decreasing
            dpr_ppm = 1000000

        item3 = _make_window(canonical_id="item-3-ok")
        results = engine.describe_many([item1, DuckBadSeq(), item3])

        self.assertIsNone(results[0].error)
        self.assertIn("InvalidFrameSequenceError", results[1].error)
        self.assertIsNone(results[2].error)

    def test_item_failure_isolated_runner_exception(self):
        """Runner raises item-level error on target B; targets A and C succeed."""
        runner = FakePersistentRunner(fail_handles={"target-B"})
        engine = RealVisionEngine(runner=runner)

        itemA = _make_window(canonical_id="target-A")
        itemB = _make_window(canonical_id="target-B")
        itemC = _make_window(canonical_id="target-C")

        results = engine.describe_many([itemA, itemB, itemC])

        self.assertEqual(len(results), 3)
        self.assertIsNone(results[0].error)
        self.assertIsNotNone(results[0].observation)

        self.assertIn("ValueError: Per-item processing error for target-B", results[1].error)
        self.assertIsNone(results[1].observation)

        self.assertIsNone(results[2].error)
        self.assertIsNotNone(results[2].observation)
        self.assertTrue(runner.alive)


class TestRunnerDeathAndUnfinishedMarking(unittest.TestCase):
    """Gate G M3 3: Mark unfinished work after runner death without replaying completed items."""

    def test_runner_death_marks_unfinished_without_replaying_completed(self):
        """Runner dies on Item B after Item A completes:
        - Item A remains completed (never replayed or retried).
        - Item B is marked with runner death error.
        - Items C and D are marked as unfinished runner death errors without dispatch.
        """
        runner = FakePersistentRunner(die_on_handle="target-B")
        engine = RealVisionEngine(runner=runner)

        itemA = _make_window(canonical_id="target-A")
        itemB = _make_window(canonical_id="target-B")
        itemC = _make_window(canonical_id="target-C")
        itemD = _make_window(canonical_id="target-D")

        results = engine.describe_many([itemA, itemB, itemC, itemD])

        self.assertEqual(len(results), 4)

        # Item A completed successfully
        self.assertEqual(results[0].handle.canonical_id, "target-A")
        self.assertIsNone(results[0].error)
        self.assertIsNotNone(results[0].observation)

        # Item B encountered runner crash
        self.assertEqual(results[1].handle.canonical_id, "target-B")
        self.assertIn("RunnerDeathError", results[1].error)
        self.assertIsNone(results[1].observation)

        # Items C and D marked as unfinished without dispatch
        self.assertEqual(results[2].handle.canonical_id, "target-C")
        self.assertIn("RunnerDeathError", results[2].error)
        self.assertIn("unfinished", results[2].error)

        self.assertEqual(results[3].handle.canonical_id, "target-D")
        self.assertIn("RunnerDeathError", results[3].error)
        self.assertIn("unfinished", results[3].error)

        # Verify dispatched list: target-A and target-B were dispatched; target-C and target-D were NEVER dispatched!
        self.assertEqual(runner.dispatched_items, ["target-A", "target-B"])

        # Crucially: target-A was dispatched EXACTLY ONCE and NEVER replayed!
        self.assertEqual(runner.dispatched_items.count("target-A"), 1)


class TestMutantDistinctions(unittest.TestCase):
    """Gate G M3 3: Reject mutant spawn per frame or retry completed item."""

    def test_reject_mutant_spawn_per_frame(self):
        """Reject mutant that spawns a worker process/runner per frame.
        A 5-frame window must be processed as a single temporal sequence.
        """
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        item = _make_window(canonical_id="multi-frame-win", n_frames=5)
        results = engine.describe_many([item])

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].error)
        # Exactly one invocation for the window sequence, NOT 5 spawns/invocations
        self.assertEqual(runner.invocations, 1)

    def test_reject_mutant_retry_completed_item(self):
        """Reject mutant that retries completed items when an error occurs later in the batch."""
        runner = FakePersistentRunner(die_on_handle="target-2")
        engine = RealVisionEngine(runner=runner)

        items = [
            _make_window(canonical_id="target-1"),
            _make_window(canonical_id="target-2"),
            _make_window(canonical_id="target-3"),
        ]
        results = engine.describe_many(items)

        self.assertEqual(len(results), 3)
        self.assertIsNone(results[0].error)
        # target-1 was dispatched exactly once
        self.assertEqual(runner.dispatched_items.count("target-1"), 1)


class TestCanonicalDataclassesAndContracts(unittest.TestCase):
    """Verify frozen dataclass field order, immutability, and supported target kinds."""

    def test_canonical_dataclasses_frozen_and_field_order(self):
        handle = Handle("cdp_tab", "tab-1", 1)
        with self.assertRaises(Exception):
            handle.kind = "other"  # frozen

        self.assertEqual(list(handle.__dataclass_fields__.keys()), ["kind", "canonical_id", "generation"])

        tr = TargetRecord(handle, None, True, 0, "sess-1", True, False)
        self.assertEqual(
            list(tr.__dataclass_fields__.keys()),
            ["handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"],
        )

        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        fref = FrameRef.from_rgb(handle, 0, 1, rgb)
        self.assertEqual(
            list(fref.__dataclass_fields__.keys()),
            ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"],
        )

        obs = Observation(handle, 0, 1, 1000000, {"key": "val"}, fref)
        self.assertEqual(
            list(obs.__dataclass_fields__.keys()),
            ["handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"],
        )

        rcpt = Receipt("act-1", handle, "ok", None)
        self.assertEqual(
            list(rcpt.__dataclass_fields__.keys()),
            ["action_id", "handle", "status", "reason"],
        )

        act_req = ActionRequest(handle, "act-1", 0, {}, 10, 1000)
        self.assertEqual(
            list(act_req.__dataclass_fields__.keys()),
            ["handle", "action_id", "epoch", "payload", "cost", "deadline_ns"],
        )

        lease = ActorLease(handle, "owner1", 1, b"nonce", 2000)
        self.assertEqual(
            list(lease.__dataclass_fields__.keys()),
            ["handle", "owner", "generation", "nonce", "expires_ns"],
        )

        flease = FocusLease(lease, "dom", 0)
        self.assertEqual(
            list(flease.__dataclass_fields__.keys()),
            ["lease", "domain", "epoch"],
        )

        spec = StreamSpec(1920, 1080, 4, 24883200)
        self.assertEqual(
            list(spec.__dataclass_fields__.keys()),
            ["width", "height", "slots", "quota_bytes"],
        )

        stok = StreamToken(handle, 0)
        self.assertEqual(
            list(stok.__dataclass_fields__.keys()),
            ["handle", "epoch"],
        )

        tkt = Ticket("act-1", handle)
        self.assertEqual(
            list(tkt.__dataclass_fields__.keys()),
            ["action_id", "handle"],
        )

        cres = ChangeResult(handle, 0, True, np.zeros(2), {})
        self.assertEqual(
            list(cres.__dataclass_fields__.keys()),
            ["handle", "epoch", "settled", "claims", "ops"],
        )

        pres = PerceptionResult(handle, obs, None)
        self.assertEqual(
            list(pres.__dataclass_fields__.keys()),
            ["handle", "observation", "error"],
        )

        wf = WindowFrames(handle, 0, (rgb,), (0,), 1000000)
        self.assertEqual(
            list(wf.__dataclass_fields__.keys()),
            ["handle", "epoch", "frames", "frame_seqs", "dpr_ppm"],
        )

        wd = WindowDescription(handle, 0, (0,), ("d1",), ([],), None)
        self.assertEqual(
            list(wd.__dataclass_fields__.keys()),
            ["handle", "epoch", "frame_seqs", "descriptors", "regions", "error"],
        )

    def test_supported_target_kinds(self):
        for kind in ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"):
            self.assertIn(kind, SUPPORTED_TARGET_KINDS)
            w = _make_window(kind=kind)
            admitted = validate_window_frames(w)
            self.assertEqual(admitted.handle.kind, kind)

        bad_w = types.SimpleNamespace(
            handle=types.SimpleNamespace(kind="unsupported_surface", canonical_id="win-1", generation=1),
            epoch=0, frames=(np.zeros((20, 30, 3), dtype=np.uint8),),
            frame_seqs=(0,), dpr_ppm=1000000)
        with self.assertRaises(InvalidTargetError):
            validate_window_frames(bad_w)


class TestLegacySingleFrameMethods(unittest.TestCase):
    """Verify legacy single-frame methods remain available on RealVisionEngine."""

    def test_legacy_methods_retained(self):
        runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=runner)

        self.assertTrue(hasattr(engine, "screen_identity"))
        self.assertTrue(hasattr(engine, "screen_fingerprint"))
        self.assertTrue(hasattr(engine, "same_screen"))
        self.assertTrue(hasattr(engine, "fingerprint_distance"))
        self.assertTrue(hasattr(engine, "changed_regions"))
        self.assertTrue(hasattr(engine, "_buffer"))
        self.assertTrue(hasattr(engine, "_run"))


if __name__ == "__main__":
    unittest.main()
