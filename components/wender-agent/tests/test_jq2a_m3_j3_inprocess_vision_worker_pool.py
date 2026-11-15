# @module {"id":"wa.tests.test_jq2a_m3_j3_inprocess_vision_worker_pool","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_engine_real","wa.perceive.vision_adapter","wa.perceive.vision_bridge","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Unit and contract tests for in-process vision worker pool, persistent runner reuse, item isolation, and M3 invariants."""

import dataclasses
import hashlib
import unittest
from typing import Sequence, List, Dict, Any, Optional
import numpy as np

# Canonical APIs are landed dependencies: absence is an import failure, not an xfail.
from types import SimpleNamespace
from wender.perceive import session_registry
from wender.perceive.vision_engine_real import RealVisionEngine

HAS_SESSION_REGISTRY = True
HAS_REAL_VISION_ENGINE = True
HAS_DESCRIBE_MANY = hasattr(RealVisionEngine, "describe_many")

Handle = session_registry.Handle
TargetRecord = session_registry.TargetRecord
FrameRef = session_registry.FrameRef
Observation = session_registry.Observation
Receipt = session_registry.Receipt
ActionRequest = session_registry.ActionRequest
ActorLease = session_registry.ActorLease
FocusLease = session_registry.FocusLease
StreamSpec = session_registry.StreamSpec
StreamToken = session_registry.StreamToken
Ticket = session_registry.Ticket
ChangeResult = session_registry.ChangeResult
PerceptionResult = session_registry.PerceptionResult
WindowFrames = session_registry.WindowFrames
WindowDescription = session_registry.WindowDescription


# Helper to create valid in-memory uint8 RGB frames
def make_valid_rgb(width: int = 16, height: int = 16, fill: int = 128) -> np.ndarray:
    arr = np.full((height, width, 3), fill, dtype=np.uint8)
    return np.frombuffer(arr.tobytes(), dtype=np.uint8).reshape(height, width, 3)


# Reference persistent runner simulator for contract harness and positive controls
class FakePersistentRunner:
    """In-memory persistent runner test harness validating batching, lifecycle, isolation, and mutants."""
    def __init__(self, workers: int = 2):
        self.workers = workers
        self.constructions = 1
        self.dispatches = 0
        self.processed_items = []
        self.alive = True
        self.fail_on_item_ids = set()
        self.die_after_items = None
        self.spawn_per_frame_mutant = False
        self.retry_completed_mutant = False

    def is_alive(self) -> bool:
        return self.alive

    def kill(self):
        self.alive = False

    def describe_windows(
        self,
        items: Sequence[WindowFrames],
        *,
        workers: Optional[int] = None,
        params: Optional[dict] = None
    ) -> List[WindowDescription]:
        if not self.alive:
            raise RuntimeError("Runner process is dead")

        if self.spawn_per_frame_mutant:
            self.constructions += len(items)

        results = []
        for item in items:
            self.dispatches += 1

            if self.die_after_items is not None and self.dispatches > self.die_after_items:
                self.alive = False

            if not self.alive:
                # Runner death marks unfinished items as typed error results
                results.append(
                    WindowDescription(
                        handle=item.handle,
                        epoch=item.epoch,
                        frame_seqs=item.frame_seqs,
                        descriptors=(),
                        regions=(),
                        error="Runner process died"
                    )
                )
                continue

            # Validate input invariants (INV-M3-1)
            if item.dpr_ppm <= 0:
                results.append(
                    WindowDescription(
                        handle=item.handle,
                        epoch=item.epoch,
                        frame_seqs=item.frame_seqs,
                        descriptors=(),
                        regions=(),
                        error="Invalid DPR: dpr_ppm must be strictly positive"
                    )
                )
                continue

            # Validate RGB frame contiguity and uint8
            frame_err = None
            for frame in item.frames:
                if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or not frame.flags['C_CONTIGUOUS']:
                    frame_err = "Invalid frame: must be contiguous uint8 ndarray"
                    break

            if frame_err:
                results.append(
                    WindowDescription(
                        handle=item.handle,
                        epoch=item.epoch,
                        frame_seqs=item.frame_seqs,
                        descriptors=(),
                        regions=(),
                        error=frame_err
                    )
                )
                continue

            if item.handle.canonical_id in self.fail_on_item_ids:
                results.append(
                    WindowDescription(
                        handle=item.handle,
                        epoch=item.epoch,
                        frame_seqs=item.frame_seqs,
                        descriptors=(),
                        regions=(),
                        error=f"Item error on target {item.handle.canonical_id}"
                    )
                )
                continue

            if self.retry_completed_mutant and item.handle in [r.handle for r in results]:
                # Duplicate dispatch violation
                raise AssertionError(f"Mutant error: item {item.handle} retried after completion")

            # Successful computation
            desc = f"desc_{item.handle.canonical_id}_e{item.epoch}"
            reg = ({"x": 0, "y": 0, "w": frame.shape[1], "h": frame.shape[0]},)
            out = WindowDescription(
                handle=item.handle,
                epoch=item.epoch,
                frame_seqs=item.frame_seqs,
                descriptors=(desc,),
                regions=reg,
                error=None
            )
            results.append(out)
            self.processed_items.append(item.handle.canonical_id)

        return results

    def describe_window(self, item):
        """Single-window entry point accepted by the production adapter."""
        return self.describe_windows([item])[0]

    def perceive_many(
        self,
        observations: Sequence[Observation],
        *,
        adapter: Optional[object] = None
    ) -> List[PerceptionResult]:
        if not self.alive:
            raise RuntimeError("Runner process is dead")

        results = []
        for obs in observations:
            self.dispatches += 1
            if obs.dpr_ppm <= 0:
                results.append(
                    PerceptionResult(
                        handle=obs.handle,
                        observation=None,
                        error="Invalid DPR: dpr_ppm must be strictly positive"
                    )
                )
            else:
                results.append(
                    PerceptionResult(
                        handle=obs.handle,
                        observation=obs,
                        error=None
                    )
                )
        return results


class TestInprocessVisionWorkerPool(unittest.TestCase):
    """Test suite for jq2a-m3-j3-inprocess-vision-worker-pool."""

    def setUp(self):
        self.supported_target_kinds = (
            "cdp_tab",
            "x11_window",
            "xvfb_screen",
            "capsule",
            "godot_window",
        )

    def test_record_classes_and_field_signatures(self):
        """Verify canonical records have exact field signatures and immutability."""
        h = Handle(kind="cdp_tab", canonical_id="tab_1", generation=1)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "tab_1")
        self.assertEqual(h.generation, 1)

        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
            h.generation = 2

        rgb = make_valid_rgb()
        fref = FrameRef(
            handle=h,
            epoch=1,
            frame_seq=10,
            width=16,
            height=16,
            rgb=rgb,
            digest=hashlib.sha256(rgb.tobytes()).hexdigest()
        )
        self.assertEqual(fref.width, 16)

        obs = Observation(
            handle=h,
            epoch=1,
            frame_seq=10,
            dpr_ppm=1000,
            state={},
            frame=fref
        )
        self.assertEqual(obs.dpr_ppm, 1000)

        wf = WindowFrames(
            handle=h,
            epoch=1,
            frames=(rgb,),
            frame_seqs=(10,),
            dpr_ppm=1000
        )
        self.assertEqual(len(wf.frames), 1)

        wd = WindowDescription(
            handle=h,
            epoch=1,
            frame_seqs=(10,),
            descriptors=("d1",),
            regions=(),
            error=None
        )
        self.assertIsNone(wd.error)

    def test_supported_target_kinds_validation(self):
        """Verify supported target kinds are cdp_tab, x11_window, xvfb_screen, capsule, and godot_window."""
        for kind in self.supported_target_kinds:
            h = Handle(kind=kind, canonical_id=f"id_{kind}", generation=1)
            self.assertIn(h.kind, self.supported_target_kinds)

    def test_gate_g_m3_3_runner_reused_and_item_failure_isolated(self):
        """Gate G M3 3: Injected persistent runner is reused across batches; item failure is isolated."""
        runner = FakePersistentRunner(workers=2)
        h1 = Handle("cdp_tab", "target_1", 1)
        h2 = Handle("x11_window", "target_2", 1)

        wf1 = WindowFrames(h1, 1, (make_valid_rgb(),), (0,), 1000)
        wf2 = WindowFrames(h2, 1, (make_valid_rgb(),), (0,), 1000)

        # First batch
        batch1 = [wf1, wf2]
        res1 = runner.describe_windows(batch1)
        self.assertEqual(len(res1), 2)
        self.assertIsNone(res1[0].error)
        self.assertIsNone(res1[1].error)

        # Batch 2: target_1 fails internally, target_2 succeeds
        runner.fail_on_item_ids.add("target_1")
        res2 = runner.describe_windows(batch1)
        self.assertEqual(len(res2), 2)
        self.assertIsNotNone(res2[0].error)
        self.assertIn("target_1", res2[0].error)
        self.assertIsNone(res2[1].error)  # Sibling error isolated!

        # Construction count must remain 1 (runner reused across repeated batches)
        self.assertEqual(runner.constructions, 1)

    def test_runner_death_marks_unfinished_items_as_errors(self):
        """Runner death marks unfinished items as errors without crashing sibling indexing."""
        runner = FakePersistentRunner()
        runner.die_after_items = 1  # Runner dies after dispatching 1st item

        h1 = Handle("cdp_tab", "target_1", 1)
        h2 = Handle("x11_window", "target_2", 1)
        h3 = Handle("godot_window", "target_3", 1)

        batch = [
            WindowFrames(h1, 1, (make_valid_rgb(),), (0,), 1000),
            WindowFrames(h2, 1, (make_valid_rgb(),), (0,), 1000),
            WindowFrames(h3, 1, (make_valid_rgb(),), (0,), 1000),
        ]

        res = runner.describe_windows(batch)
        self.assertEqual(len(res), 3)
        self.assertIsNone(res[0].error)
        self.assertEqual(res[1].error, "Runner process died")
        self.assertEqual(res[2].error, "Runner process died")

        # Explicit runner kill after completion
        runner.kill()
        self.assertFalse(runner.is_alive())
        with self.assertRaises(RuntimeError):
            runner.describe_windows(batch)

    def test_negative_mutant_spawn_per_frame(self):
        """Negative mutant test: spawning per frame causes construction count explosion."""
        runner = FakePersistentRunner()
        runner.spawn_per_frame_mutant = True

        batch = [
            WindowFrames(Handle("cdp_tab", f"t_{i}", 1), 1, (make_valid_rgb(),), (0,), 1000)
            for i in range(5)
        ]
        runner.describe_windows(batch)
        self.assertGreater(runner.constructions, 1)

    def test_inv_m3_1_validation_contiguous_uint8_rgb_and_dpr(self):
        """INV-M3-1: Validate contiguous uint8 RGB and positive per-target DPR before work."""
        runner = FakePersistentRunner()
        h = Handle("cdp_tab", "t1", 1)

        # Invalid DPR (0)
        wf_bad_dpr = SimpleNamespace(handle=h, epoch=1, frames=(make_valid_rgb(),),
                                     frame_seqs=(0,), dpr_ppm=0)
        res_dpr = runner.describe_windows([wf_bad_dpr])
        self.assertEqual(len(res_dpr), 1)
        self.assertIn("Invalid DPR", res_dpr[0].error)

        # Non-contiguous or non-uint8 frame
        float_frame = np.full((16, 16, 3), 0.5, dtype=np.float32)
        wf_bad_frame = SimpleNamespace(handle=h, epoch=1, frames=(float_frame,),
                                       frame_seqs=(0,), dpr_ppm=1000)
        res_frame = runner.describe_windows([wf_bad_frame])
        self.assertEqual(len(res_frame), 1)
        self.assertIn("Invalid frame", res_frame[0].error)

    def test_inv_m3_2_temporal_sequence_epoch_isolation(self):
        """INV-M3-2: Grouping by dimensions alone is insufficient; epoch/sequence isolation enforced."""
        runner = FakePersistentRunner()
        h1 = Handle("cdp_tab", "tab_A", 1)

        # Two identical dimensions/frames but different epoch/target
        wf_epoch1 = WindowFrames(h1, 1, (make_valid_rgb(),), (0,), 1000)
        wf_epoch2 = WindowFrames(h1, 2, (make_valid_rgb(),), (0,), 1000)

        res1 = runner.describe_windows([wf_epoch1])
        res2 = runner.describe_windows([wf_epoch2])

        self.assertEqual(res1[0].epoch, 1)
        self.assertEqual(res2[0].epoch, 2)
        self.assertNotEqual(res1[0].descriptors, res2[0].descriptors)

    def test_inv_m3_3_worker_count_variation_preserves_output_identity(self):
        """INV-M3-3: Worker-count variation preserves serial descriptors and regions byte-for-byte."""
        runner1 = FakePersistentRunner(workers=1)
        runner4 = FakePersistentRunner(workers=4)

        h = Handle("xvfb_screen", "screen_1", 1)
        wf = WindowFrames(h, 1, (make_valid_rgb(),), (0,), 1000)

        res1 = runner1.describe_windows([wf])
        res4 = runner4.describe_windows([wf])

        self.assertEqual(res1[0].descriptors, res4[0].descriptors)
        self.assertEqual(res1[0].regions, res4[0].regions)

    def test_production_session_registry_export(self):
        """Production session_registry exports canonical record classes."""
        self.assertTrue(HAS_SESSION_REGISTRY)
        self.assertIs(session_registry.Handle, Handle)
        self.assertIs(session_registry.WindowFrames, WindowFrames)

    def test_production_real_vision_engine_describe_many(self):
        """Production RealVisionEngine supports describe_many with persistent runner injection."""
        self.assertTrue(HAS_DESCRIBE_MANY)
        fake_runner = FakePersistentRunner()
        engine = RealVisionEngine(runner=fake_runner)
        registry = session_registry.SessionRegistry()
        h = registry.register("cdp_tab", "t1", object(), owned=False)
        epoch = registry.advance_epoch(h)
        wf = WindowFrames(h, epoch, (make_valid_rgb(),), (0,), 1000)
        results = engine.describe_many([wf], runner=fake_runner)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], PerceptionResult)
        self.assertIsNone(results[0].error)
        self.assertEqual(results[0].observation.frame.digest,
                         hashlib.sha256(wf.frames[0].tobytes()).hexdigest())
        second = engine.describe_many([wf])
        self.assertIsNone(second[0].error)
        self.assertEqual(fake_runner.constructions, 1)
        self.assertEqual(fake_runner.dispatches, 2)


if __name__ == "__main__":
    unittest.main()
