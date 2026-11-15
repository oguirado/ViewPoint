"""Contract tests for jq2a-m3-j1-describe-many-windows-batch.

Validates batch perception, RealVisionEngine.describe_many,
reverse completion ordering, error isolation, invariant rules (INV-M3-1..3),
and Gate G M3 1.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from types import SimpleNamespace
import unittest

import numpy as np

from wender.perceive.session_registry import (
    FrameRef, Handle, Observation, PerceptionResult, SessionRegistry,
    WindowDescription, WindowFrames,
)
from wender.perceive.vision_adapter import FakeVisionEngine


# --- Mocks and Test Helpers ---

class ReverseCompletionRunner:
    """Mock runner that completes tasks in reverse order of submission."""

    def map(self, func, items):
        items_list = list(items)
        results = [None] * len(items_list)
        # Reverse execution order
        for idx in reversed(range(len(items_list))):
            results[idx] = func(items_list[idx])
        return results


class AssembleCompletionRunner:
    """Mock runner that completes tasks in assemble order (straight map)."""

    def map(self, func, items):
        return list(map(func, items))


def create_valid_rgb(width=10, height=10, val=128) -> np.ndarray:
    """Immutable, contiguous uint8 RGB with real geometry."""
    return np.frombuffer(bytes([val]) * (width * height * 3), dtype=np.uint8).reshape(height, width, 3)


def issued_observation(handle, epoch, seq, rgb, *, dpr_ppm=1000000, state=None):
    """Use issued handles and the registry's current epoch/sealed frame admission."""
    registry = SessionRegistry()
    for index in range(handle.generation - 1):
        registry.register(handle.kind, f"{handle.canonical_id}-prior-{index}", object(), owned=False)
    issued = registry.register(handle.kind, handle.canonical_id, object(), owned=False)
    for _ in range(epoch):
        registry.advance_epoch(issued)
    frame = registry.admit_frame(issued, seq, rgb, epoch=epoch)
    bound_state = {"nodes": [], "viewport": {"dpr_ppm": dpr_ppm}}
    bound_state.update({} if state is None else state)
    return registry.admit_observation(issued, frame, state=bound_state, dpr_ppm=dpr_ppm)


class FakeWindowRunner:
    """Inline offline collaborator implementing the landed runner protocol."""

    def describe_window(self, item):
        descriptors = tuple(hashlib.sha256(frame.tobytes()).digest() for frame in item.frames)
        return WindowDescription(item.handle, item.epoch, item.frame_seqs, descriptors, (), None)


class DescriptorAdapter(FakeVisionEngine):
    """Exercise descriptor equality with real bytes alongside region equality."""

    def screen_fingerprint(self, frame):
        return hashlib.sha256(frame.data).digest()


def result_payload(results):
    """Compare stable descriptor, region, identity and RGB bytes, never ndarray truth values."""
    return [(result.handle, result.error,
             None if result.observation is None else (
                 result.observation.epoch, result.observation.frame_seq,
                 result.observation.dpr_ppm, result.observation.state,
                 result.observation.frame.digest, result.observation.frame.rgb.tobytes()))
            for result in results]


# --- Contract Test Class ---

class TestJq2aM3J1DescribeManyWindowsBatch(unittest.TestCase):
    """Test suite for jq2a-m3-j1-describe-many-windows-batch contract and gates."""

    # --- Healthy Controls ---

    def test_healthy_control_dataclass_field_order_and_immutability(self):
        """Verify field orders and immutability of proposed canonical dataclasses."""
        h = Handle("x11_window", "win1", 1)
        self.assertEqual(h.kind, "x11_window")
        self.assertEqual(h.canonical_id, "win1")
        self.assertEqual(h.generation, 1)

        with self.assertRaises(Exception):
            h.generation = 2  # frozen check

        rgb = create_valid_rgb(10, 10, 255)
        fr = FrameRef.from_rgb(h, 1, 100, rgb)
        self.assertEqual(fr.digest, hashlib.sha256(rgb.tobytes()).hexdigest())

        obs = Observation(h, 1, 100, 96, {"nodes": [], "active": True}, fr)
        self.assertEqual(obs.dpr_ppm, 96)

        pr = PerceptionResult(h, obs, None)
        self.assertEqual(pr.handle, h)
        self.assertEqual(pr.observation, obs)
        self.assertIsNone(pr.error)

        wf = WindowFrames(h, 1, (rgb,), (100,), 96)
        self.assertEqual(wf.handle, h)

        wd = WindowDescription(h, 1, (100,), ({"desc": 1},), ({"x": 0, "y": 0, "w": 10, "h": 10},), None)
        self.assertEqual(wd.error, None)

    def test_healthy_control_reverse_runner_mechanics(self):
        """Verify ReverseCompletionRunner processes items in reverse order."""
        executed_order = []

        def worker(item):
            executed_order.append(item)
            return item * 10

        runner = ReverseCompletionRunner()
        res = runner.map(worker, [1, 2, 3])
        self.assertEqual(res, [10, 20, 30])  # Result remains input-indexed!
        self.assertEqual(executed_order, [3, 2, 1])  # Worker order was reversed!

    def test_healthy_control_negative_mutant_vs_positive_control(self):
        """Positive control uses reversed runner; negative mutant uses assemble runner."""
        executed_positive = []
        executed_negative = []

        def worker_pos(x):
            executed_positive.append(x)
            return x

        def worker_neg(x):
            executed_negative.append(x)
            return x

        ReverseCompletionRunner().map(worker_pos, ['A', 'B', 'C'])
        AssembleCompletionRunner().map(worker_neg, ['A', 'B', 'C'])

        self.assertEqual(executed_positive, ['C', 'B', 'A'])
        self.assertEqual(executed_negative, ['A', 'B', 'C'])

    # --- Landed Component Contracts ---

    def test_perceive_many_reverse_completion_mixed_success(self):
        """perceive_many reverses worker order and preserves input indexing on mixed success."""
        from wender.perceive.parallel_perceive import perceive_many

        h1 = Handle("x11_window", "win1", 1)
        h2 = Handle("x11_window", "win2", 1)
        o1 = issued_observation(h1, 1, 1, create_valid_rgb(10, 10, 10))
        o2 = issued_observation(h2, 1, 1, create_valid_rgb(10, 10, 20))

        class ItemFailureAdapter(FakeVisionEngine):
            def screen_identity(self, frame):
                if frame.data[0] == 20:
                    raise ValueError("Target window failure")
                return super().screen_identity(frame)

        adapter = ItemFailureAdapter()
        runner = ReverseCompletionRunner()
        results = perceive_many([o1, o2], adapter=adapter, runner=runner)

        self.assertEqual(len(results), 2)
        # Input indexing preserved
        self.assertEqual(results[0].handle, h1)
        self.assertIsNotNone(results[0].observation)
        self.assertIsNone(results[0].error)

        self.assertEqual(results[1].handle, h2)
        self.assertIsNone(results[1].observation)
        self.assertIn("Target window failure", str(results[1].error))

    def test_describe_windows_batch_identities_and_serial_bytes_agree(self):
        """Gate G M3 1 uses the landed perceive_many batch API."""
        from wender.perceive.parallel_perceive import perceive_many

        h1 = Handle("cdp_tab", "tab1", 1)
        h2 = Handle("xvfb_screen", "screen1", 1)
        o1 = issued_observation(h1, 10, 1, create_valid_rgb(20, 20, 50))
        o2 = issued_observation(h2, 10, 1, create_valid_rgb(20, 20, 100))
        with ThreadPoolExecutor(max_workers=2) as runner:
            results = perceive_many([o1, o2], adapter=DescriptorAdapter(), runner=runner)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].handle, h1)
        self.assertEqual(results[0].observation.epoch, 10)
        self.assertEqual(results[1].handle, h2)
        self.assertEqual(results[1].observation.epoch, 10)
        serial = perceive_many([o1, o2], adapter=DescriptorAdapter(), runner=AssembleCompletionRunner())
        self.assertEqual(result_payload(results), result_payload(serial))
        self.assertTrue(all(result.error is None for result in results))
        self.assertTrue(all(result.observation.state["_perception"]["descriptor_hex"] is not None
                            for result in results))

    def test_real_vision_engine_describe_many(self):
        """RealVisionEngine.describe_many describes multiple WindowFrames in batch."""
        from wender.perceive.vision_engine_real import RealVisionEngine

        engine = RealVisionEngine(runner=FakeWindowRunner())
        h1 = Handle("godot_window", "g1", 1)
        admitted = issued_observation(h1, 1, 1, create_valid_rgb(10, 10, 10))
        wf1 = WindowFrames(admitted.handle, admitted.epoch, (admitted.frame.rgb,),
                           (admitted.frame_seq,), admitted.dpr_ppm)

        results = engine.describe_many([wf1])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].handle, h1)
        self.assertIsNone(results[0].error)
        self.assertIsInstance(results[0], PerceptionResult)
        self.assertEqual(results[0].observation.frame.digest,
                         hashlib.sha256(wf1.frames[0].tobytes()).hexdigest())

    def test_vision_bridge_screen_identities_delegates(self):
        """vision_bridge.screen_identities delegates to perceive_many."""
        from wender.perceive.vision_bridge import screen_identities

        h1 = Handle("capsule", "cap1", 1)
        o1 = issued_observation(h1, 1, 1, create_valid_rgb(10, 10, 0))
        results = screen_identities([o1])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].handle, h1)
        self.assertIsNone(results[0].error)

    # --- Invariant Verifications (INV-M3-1, INV-M3-2, INV-M3-3) ---

    def test_inv_m3_1_validate_contiguous_uint8_rgb_and_positive_dpr(self):
        """INV-M3-1: strict constructors and batch receipts both reject invalid DPR."""
        from wender.perceive.parallel_perceive import perceive_many

        h1 = Handle("x11_window", "w1", 1)
        valid = issued_observation(h1, 1, 1, create_valid_rgb(10, 10))
        with self.assertRaises(ValueError):
            Observation(h1, 1, 1, 0, valid.state, valid.frame)
        # An external structural item reaches the batch admission boundary.
        obs_bad_dpr = SimpleNamespace(**(vars(valid) | {"dpr_ppm": 0}))
        results = perceive_many([obs_bad_dpr, valid], adapter=None)
        self.assertTrue(results[0].error.startswith("VALIDATION_ERROR:"))
        self.assertIsNone(results[0].observation)
        self.assertIsNone(results[1].error)

    def test_inv_m3_2_temporal_sequence_target_isolation(self):
        """INV-M3-2: each target sequence is isolated; same shape does not share state."""
        from wender.perceive.parallel_perceive import perceive_many

        h1 = Handle("x11_window", "target_a", 1)
        h2 = Handle("x11_window", "target_b", 1)
        o1 = issued_observation(h1, 1, 1, create_valid_rgb(10, 10, 100))
        o2 = issued_observation(h2, 1, 1, create_valid_rgb(10, 10, 100))
        results = perceive_many([o1, o2], adapter=FakeVisionEngine())
        self.assertEqual(results[0].handle.canonical_id, "target_a")
        self.assertEqual(results[1].handle.canonical_id, "target_b")
        self.assertEqual([result.observation.state["_perception"]["regions"] for result in results], [[], []])

    def test_inv_m3_3_worker_count_variation_preserves_descriptors(self):
        """INV-M3-3: worker-count variation preserves descriptors/regions byte-for-byte."""
        from wender.perceive.parallel_perceive import perceive_many

        h1 = Handle("cdp_tab", "t1", 1)
        observations = [issued_observation(h1, 1, 1, create_valid_rgb(10, 10, 0)),
                        issued_observation(h1, 1, 2, create_valid_rgb(10, 10, 255))]
        with ThreadPoolExecutor(max_workers=1) as runner:
            batch_w1 = perceive_many(observations, adapter=DescriptorAdapter(), runner=runner)
        with ThreadPoolExecutor(max_workers=4) as runner:
            batch_w4 = perceive_many(observations, adapter=DescriptorAdapter(), runner=runner)
        res_w1 = result_payload(batch_w1)
        res_w4 = result_payload(batch_w4)

        self.assertEqual(res_w1, res_w4)
        self.assertTrue(all(result.observation.state["_perception"]["descriptor_hex"] is not None
                            for result in batch_w1 + batch_w4))
        self.assertTrue(batch_w1[1].observation.state["_perception"]["regions"])


if __name__ == "__main__":
    unittest.main()
