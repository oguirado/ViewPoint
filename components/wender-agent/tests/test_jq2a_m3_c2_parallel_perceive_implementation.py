"""M3 C2 public API gates: identity isolation, independent time, DPR/AX fencing."""

import builtins
from concurrent.futures import ThreadPoolExecutor
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from wender.perceive.parallel_perceive import Handle, FrameRef, Observation, perceive_many
from wender.perceive.session_registry import SessionRegistry
from wender.perceive.vision_adapter import FakeVisionEngine, fuse_regions
from wender.perceive.vision_bridge import screen_identities


class DescriptorEngine(FakeVisionEngine):
    def screen_fingerprint(self, frame):
        return hashlib.sha256(frame.data).digest()


class ReverseRunner:
    """Completion-order perturbation: unlike ThreadPoolExecutor.map."""

    def map(self, fn, jobs):
        return iter(list(reversed([fn(job) for job in jobs])))


class MutatingRunner:
    def __init__(self, arrays):
        self.arrays = arrays

    def map(self, fn, jobs):
        for arr in self.arrays:
            arr.fill(250)
        return (fn(job) for job in jobs)


class PartialDeathRunner:
    def map(self, fn, jobs):
        jobs = list(jobs)
        yield fn(jobs[0])
        raise RuntimeError("runner exited")


def observation(name, seq, value, *, kind="cdp_tab", generation=1, epoch=3,
                dpr=1000000, node=None, frame=None, handle=None):
    registry = SessionRegistry()
    # Produce the requested incarnation through the real local issuer.
    for index in range(generation - 1):
        registry.register(kind, f"{name}-prior-{index}", object(), owned=False)
    issued = registry.register(kind, name, object(), owned=False)
    if handle is not None and handle != issued:
        raise ValueError("fixture handle differs from issued incarnation")
    handle = issued
    for _ in range(epoch):
        registry.advance_epoch(handle)
    pixels = np.full((16, 16, 3), value, dtype=np.uint8) if frame is None else frame
    boxes = [{"id": node or name, "box": [0, 0, 16 if dpr == 1000000 else 8,
                                           16 if dpr == 1000000 else 8], "actionable": True}]
    state = {"nodes": boxes, "viewport": {"dpr_ppm": dpr}}
    frame_ref = registry.admit_frame(handle, seq, pixels, epoch=epoch)
    return registry.admit_observation(handle, frame_ref, state=state, dpr_ppm=dpr)


def structural(record, **changes):
    """Cross-boundary invalid input without bypassing strict record constructors."""
    return SimpleNamespace(**(vars(record) | changes))


def view(result):
    return result.observation.state["_perception"]


class TestParallelPerceive(unittest.TestCase):
    def test_completion_reversal_preserves_input_order_and_serial_bytes(self):
        items = [observation("a", 1, 0), observation("b", 1, 0),
                 observation("a", 2, 255), observation("b", 2, 255)]
        normal = screen_identities(items, adapter=DescriptorEngine(), runner=ReverseRunner())
        with ThreadPoolExecutor(max_workers=1) as pool:
            serial = perceive_many(items, adapter=DescriptorEngine(), runner=pool)
        with ThreadPoolExecutor(max_workers=4) as pool:
            four = perceive_many(items, adapter=DescriptorEngine(), runner=pool)
        for batch in (normal, serial, four):
            self.assertEqual([r.handle.canonical_id for r in batch], ["a", "b", "a", "b"])
            self.assertTrue(all(r.error is None for r in batch))
        self.assertEqual([view(r) for r in normal], [view(r) for r in serial])
        self.assertEqual([view(r) for r in serial], [view(r) for r in four])
        self.assertEqual([r.observation.frame.rgb.tobytes() for r in serial],
                         [r.observation.frame.rgb.tobytes() for r in four])
        self.assertEqual(view(normal[0])["descriptor_hex"], hashlib.sha256(items[0].frame.rgb.tobytes()).hexdigest())
        self.assertEqual(view(normal[0])["regions"], [])
        self.assertEqual(view(normal[1])["regions"], [])
        self.assertEqual(view(normal[2])["regions"],
                         [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1}])

    def test_sibling_window_cannot_supply_predecessor(self):
        a = observation("one", 1, 0)
        b = observation("two", 1, 255)
        results = perceive_many([a, b], adapter=FakeVisionEngine(), runner=ReverseRunner())
        self.assertEqual([view(r)["regions"] for r in results], [[], []])
        self.assertNotEqual(view(results[0])["screen_identity"],
                            view(results[1])["screen_identity"])

    def test_epoch_and_generation_split_temporal_sequences(self):
        first = observation("a", 1, 0)
        different_epoch = observation("a", 1, 255, epoch=4)
        different_generation = observation("a", 1, 128, generation=2)
        results = perceive_many([first, different_epoch, different_generation],
                                adapter=FakeVisionEngine(), runner=ReverseRunner())
        self.assertEqual([view(r)["regions"] for r in results], [[], [], []])
        self.assertTrue(all(r.error is None for r in results))

    def test_partial_runner_failure_keeps_completed_indices(self):
        items = [observation("left", 1, 0), observation("right", 1, 255)]
        results = perceive_many(items, adapter=FakeVisionEngine(), runner=PartialDeathRunner())
        self.assertIsNone(results[0].error)
        self.assertTrue(results[1].error.startswith("RUNNER_ERROR:RuntimeError:"))
        self.assertIsNone(results[1].observation)
        self.assertIs(results[0].handle.kind == "cdp_tab", True)

    def test_invalid_pixels_dpr_digest_and_sequences_are_typed_partial_errors(self):
        good = observation("ok", 1, 0)
        wrong_dpr = structural(observation("bad", 1, 0), dpr_ppm=0)
        noncontig = np.zeros((16, 32, 3), dtype=np.uint8)[:, ::2, :]
        wrong_array = observation("nc", 1, 0)
        wrong_array = structural(wrong_array, frame=structural(wrong_array.frame, rgb=noncontig))
        mismatched = observation("mis", 1, 0)
        mismatched = structural(mismatched, frame=structural(mismatched.frame, digest="0" * 64))
        out_of_order = observation("ok", 1, 255)
        results = perceive_many([good, wrong_dpr, wrong_array, mismatched, out_of_order],
                                adapter=FakeVisionEngine(), runner=ReverseRunner())
        self.assertIsNone(results[0].error)
        self.assertTrue(all(r.error.startswith("VALIDATION_ERROR:") for r in results[1:]))
        self.assertTrue(all(r.observation is None for r in results[1:]))

    def test_pixels_are_owned_before_runner_executes(self):
        caller_pixels = np.zeros((16, 16, 3), dtype=np.uint8)
        obs = observation("a", 1, 0, frame=caller_pixels)
        # Model a raw external buffer at the batch boundary; the canonical
        # FrameRef remains sealed, while _prepare must own this input before map.
        obs = structural(obs, frame=structural(obs.frame, rgb=caller_pixels))
        baseline = screen_identities([obs], adapter=FakeVisionEngine())
        result = screen_identities([obs], adapter=FakeVisionEngine(),
                                   runner=MutatingRunner([caller_pixels]))[0]
        self.assertEqual(view(baseline[0])["screen_identity"], view(result)["screen_identity"])
        self.assertEqual(result.observation.frame.rgb.tobytes(), bytes(16 * 16 * 3))
        self.assertFalse(result.observation.frame.rgb.flags.writeable)
        self.assertTrue(np.all(caller_pixels == 250))

    def test_same_target_fusion_and_sibling_ax_namespace(self):
        cases = [observation("a", 1, 0, dpr=1000000, node="AX-A"),
                 observation("b", 1, 0, dpr=2000000, node="AX-B"),
                 observation("a", 2, 255, dpr=1000000, node="AX-A"),
                 observation("b", 2, 255, dpr=2000000, node="AX-B")]
        r = screen_identities(cases, adapter=FakeVisionEngine(), runner=ReverseRunner())
        self.assertEqual([h["node"] for h in view(r[2])["fusion"]["hits"]], ["AX-A"])
        self.assertEqual([h["node"] for h in view(r[3])["fusion"]["hits"]], ["AX-B"])
        self.assertEqual(view(r[2])["fusion"]["hits"][0]["overlap"], 256)
        self.assertEqual(view(r[3])["fusion"]["hits"][0]["overlap"], 64)
        self.assertEqual(view(r[2])["dpr_ppm"], 1000000)
        self.assertEqual(view(r[3])["dpr_ppm"], 2000000)

    def test_direct_fusion_fails_closed_on_target_epoch_and_dpr_mismatch(self):
        a = Handle("cdp_tab", "a", 1)
        b = Handle("cdp_tab", "b", 1)
        state = {"state": {"nodes": [{"id": "a-node", "box": [0, 0, 8, 8],
                                       "actionable": False}]},
                 "handle": a, "epoch": 3, "dpr_ppm": 2000000}
        regs = [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1}]
        expected = {"hits": [{"region": 0, "node": "a-node", "overlap": 64,
                              "actionable": False}], "unmatched_regions": []}
        self.assertEqual(fuse_regions(state, regs, 2000000, handle=a, epoch=3), expected)
        for kw in ({"handle": b, "epoch": 3}, {"handle": a, "epoch": 4}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                fuse_regions(state, regs, 2000000, **kw)
        with self.assertRaises(ValueError):
            fuse_regions(state, regs, 1000000, handle=a, epoch=3)
        with self.assertRaises(ValueError):
            fuse_regions(state["state"], regs, 2000000, handle=a, epoch=3)

    def test_runner_and_adapter_errors_are_per_item_without_disk_io(self):
        class OneBroken(FakeVisionEngine):
            def screen_identity(self, frame):
                if frame.data[0] == 33:
                    raise RuntimeError("isolated")
                return super().screen_identity(frame)
        items = [observation("a", 1, 0), observation("b", 1, 33),
                 observation("c", 1, 0)]
        with patch.object(builtins, "open", side_effect=AssertionError("disk IO forbidden")):
            results = screen_identities(items, adapter=OneBroken(), runner=ReverseRunner())
        self.assertEqual([r.error is None for r in results], [True, False, True])
        self.assertTrue(results[1].error.startswith("PERCEPTION_ERROR:RuntimeError:"))


if __name__ == "__main__":
    unittest.main()
