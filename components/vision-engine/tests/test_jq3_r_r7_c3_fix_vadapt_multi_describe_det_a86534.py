"""G-R7-8: isolated multi_describe fixture repair and named mutation gate.

Offline: uses RAM RGB, deterministic seeds, stdlib + numpy; never rewrites the
production source. Original legacy test methods are replayed by exact IDs.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass, fields, FrozenInstanceError
from hashlib import sha256
import importlib.machinery
from pathlib import Path
from unittest import mock
import random
import sys
import threading
import types
import unittest

import numpy as np

from vadapt import multi_describe as md


SEED = 0xA86534
RGB_ERROR = "E_M3_FRAME_SCHEMA: RGB must be C-contiguous uint8; no implicit conversion"
DPR_ERROR = "E_M3_SCHEMA: dpr_ppm must be an integer >= 1"
MUTANT_PREFIX = "vadapt._jq3_r7_c3_private_"

# Exact pre-edit source inventory, from bridge/integration at 5ac4fa8b9e924c4cfa6f440bf481a667621a0d3b.
# Replaying all methods from a changed shared setup/runner is deliberately stricter
# than replaying only methods whose own bodies changed.
ORIGINAL_METHODS = {
    "tests.test_jq2a_m3_c1_multi_describe_implementation.TestMultiDescribe": (
        "test_public_canonical_frozen_field_order",
        "test_m3_1_reversed_completions_keep_identity_and_serial_bytes",
        "test_m3_5_same_dimensions_never_share_unrelated_predecessor",
        "test_m3_5_generation_and_geometry_epoch_isolation",
        "test_m3_5_same_stream_slices_share_only_own_predecessor",
        "test_m3_1_invalid_inputs_are_isolated_before_runner_admission",
        "test_frame_ref_structural_compatibility_and_guards",
        "test_m3_1_own_readonly_snapshot_before_injected_runner_executes",
        "test_runner_death_retains_completed_without_dispatch_retries",
        "test_core_failure_isolated_by_target",
        "test_geometry_conflict_sequence_overlap_and_bad_workers_refused",
        "test_zero_disk_activity_when_out_dir_none",
        "test_worker_count_parity_and_repeatable_bytes",
    ),
    "tests.test_jq2a_m3_j5_vadapt_multi_describe_determinism.TestVadaptMultiDescribeDeterminism": (
        "test_gate_g_m3_5_independent_predecessor_histories_match_serial",
        "test_negative_mutant_unrelated_frames_grouped_as_temporal_sequence_fails_isolation",
        "test_inv_m3_1_validation_non_contiguous_or_non_uint8_rgb_and_positive_dpr",
        "test_ram_input_ownership_read_only_and_offline_io_safety",
        "test_canonical_dataclass_field_order_and_structures",
    ),
    "tests.test_jq2a_m3_j6_vadapt_multi_describe_batch_and_validation.TestJq2aM3J6VadaptMultiDescribeBatchAndValidation": (
        "test_harness_positive_control_preserves_submit_identity_and_serial_bytes",
        "test_harness_negative_mutant_completion_order_detected",
        "test_harness_negative_mutant_accepts_non_positive_dpr_detected",
        "test_owner_multi_describe_rejects_non_positive_dpr_before_dispatch",
        "test_owner_multi_describe_rejects_non_contiguous_or_non_uint8_rgb",
        "test_owner_multi_describe_reversed_fake_runner_preserves_submit_identity",
        "test_owner_multi_describe_isolates_per_item_failures_without_retry",
    ),
}
EDITED_LEGACY = tuple(ORIGINAL_METHODS)[1:]


@dataclass(frozen=True)
class _StructuralFrame:
    """Private structural provider seam, NOT a canonical/imported FrameRef type."""
    handle: object
    epoch: int
    frame_seq: int
    height: int
    width: int
    rgb: object
    digest: str


class _ObservingRunner:
    def __init__(self, *, reverse=False, double_tag=False, synthetic=False,
                 description_type=None):
        self.reverse = reverse
        self.double_tag = double_tag
        self.synthetic = synthetic
        self.description_type = description_type or md.WindowDescription
        self.calls = 0
        self.submitted = ()
        self.completed = ()
        self.snapshots = ()

    def map(self, fn, items):
        self.calls += 1
        jobs = tuple(items)
        self.submitted = tuple(job.index for job in jobs)
        self.snapshots = tuple(arr for job in jobs for entry in job.entries
                               for arr in entry.frames)
        order = tuple(reversed(jobs)) if self.reverse else jobs
        if self.synthetic:
            payload = [
                (job.index, tuple((e.index, self.description_type(
                    e.handle, e.epoch, e.frame_seqs,
                    tuple(b"owned" for _ in e.frames),
                    tuple(() for _ in e.frames), None)) for e in job.entries))
                for job in order
            ]
        else:
            payload = [fn(job) for job in order]
        self.completed = tuple(tag for tag, _ in payload)
        if self.double_tag:
            return list(enumerate(payload))  # old stale fixture: wrong nested tag
        return payload


def _sample(name, seed=SEED, *, kind="cdp_tab", epoch=2, generation=1,
            seqs=(10, 11), size=(48, 64)):
    rng = np.random.default_rng(seed)
    h, w = size
    frames = tuple(np.ascontiguousarray(
        rng.integers(0, 256, (h, w, 3), dtype=np.uint8)) for _ in seqs)
    return md.WindowFrames(md.Handle(kind, name, generation), epoch, frames,
                           tuple(seqs), 1_000_000)


def _frame_ref(item, index=0):
    rgb = item.frames[index]
    return _StructuralFrame(
        item.handle, item.epoch, item.frame_seqs[index], rgb.shape[0],
        rgb.shape[1], rgb, sha256(rgb.tobytes()).hexdigest())


class AnchorError(RuntimeError):
    """Missing, ambiguous, or structurally mismatched catalogue anchor."""


def _only(items, label):
    if len(items) != 1:
        raise AnchorError(f"{label}: expected 1 AST anchor, got {len(items)}")
    return items[0]


def _mutate(tree, site):
    fn_name = {"channels": "_run_batch", "identity": "_identity"}[site]
    scope = _only([node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == fn_name],
                  f"function {fn_name}")
    if site == "channels":
        # Qualified scope + unique MemoryFrameSource manifest + literal key.
        calls = [n for n in ast.walk(scope) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "MemoryFrameSource"
                 and len(n.args) == 2 and isinstance(n.args[1], ast.Dict)]
        manifest = _only(calls, "M-vadapt-multi-describe-1 call").args[1]
        slots = [i for i, key in enumerate(manifest.keys)
                 if isinstance(key, ast.Constant) and key.value == "channels"]
        slot = _only(slots, "M-vadapt-multi-describe-1 channels key")
        node = manifest.values[slot]
        if not isinstance(node, ast.Constant) or type(node.value) is not int or node.value != 3:
            raise AnchorError("M-vadapt-multi-describe-1 expected channels: 3")
        manifest.values[slot] = ast.copy_location(ast.Constant(value=4), node)
    else:
        # Only remove the named membership predicate. Other identity conditions stay.
        expr = ("not isinstance(kind, str) or kind not in _SUPPORTED_KINDS or "
                "not isinstance(canonical_id, str) or not canonical_id")
        structural = ast.dump(ast.parse(expr, mode="eval").body,
                              include_attributes=False)
        guards = [n for n in ast.walk(scope) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.BoolOp)
                  and isinstance(n.test.op, ast.Or)
                  and ast.dump(n.test, include_attributes=False) == structural]
        guard = _only(guards, "M-vadapt-multi-describe-2 containing expression")
        conditions = [n for n in guard.test.values
                      if isinstance(n, ast.Compare)
                      and ast.unparse(n) == "kind not in _SUPPORTED_KINDS"]
        old = _only(conditions, "M-vadapt-multi-describe-2 membership condition")
        guard.test.values[guard.test.values.index(old)] = ast.copy_location(
            ast.Constant(value=False), old)


@contextmanager
def _isolated_mutant(site):
    # Recompile in a fresh, registered package module. No source write or .pyc.
    source = Path(md.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    _mutate(tree, site)  # anchor errors never count as mutant kills
    ast.fix_missing_locations(tree)
    name = MUTANT_PREFIX + site
    if name in sys.modules:
        raise RuntimeError(f"private module collision: {name}")
    mod = types.ModuleType(name)
    mod.__package__ = "vadapt"
    mod.__file__ = str(source)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    original_bytecode_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[name] = mod  # dataclasses needs its defining module registered
    try:
        exec(compile(tree, str(source), "exec"), mod.__dict__)
        yield mod
    finally:
        sys.modules.pop(name, None)
        sys.dont_write_bytecode = original_bytecode_flag
        for pool in tuple(mod.__dict__.get("_POOLS", {}).values()):
            pool.shutdown(wait=False, cancel_futures=True)
            for thread in tuple(getattr(pool, "_threads", ())):
                thread.join(timeout=3)
                if thread.is_alive():
                    raise RuntimeError("mutant thread leak")


class TestR7FixtureAndMutationGate(unittest.TestCase):
    def test_live_records_and_structural_frame_ownership(self):
        self.assertEqual(tuple(f.name for f in fields(md.Handle)),
                         ("kind", "canonical_id", "generation"))
        self.assertEqual(tuple(f.name for f in fields(md.WindowFrames)),
                         ("handle", "epoch", "frames", "frame_seqs", "dpr_ppm"))
        self.assertEqual(tuple(f.name for f in fields(md.WindowDescription)),
                         ("handle", "epoch", "frame_seqs", "descriptors", "regions", "error"))
        with self.assertRaises(FrozenInstanceError):
            md.Handle("cdp_tab", "frozen", 1).generation = 2

        src = _sample("frame-metadata")
        frame = _frame_ref(src)
        item = md.WindowFrames(src.handle, src.epoch, (frame,),
                               (frame.frame_seq,), src.dpr_ppm)
        runner = _ObservingRunner(synthetic=True)
        original = frame.rgb.copy()
        actual = md.describe_windows((item,), runner=runner)
        self.assertIsNone(actual[0].error)
        self.assertIs(actual[0].handle, src.handle)
        self.assertEqual(actual[0].epoch, src.epoch)
        self.assertEqual(actual[0].frame_seqs, (frame.frame_seq,))
        self.assertEqual(runner.calls, 1)
        self.assertEqual(len(runner.snapshots), 1)
        owned = runner.snapshots[0]
        self.assertFalse(owned.flags.writeable)
        self.assertFalse(np.shares_memory(owned, frame.rgb))
        frame.rgb[:] = 0
        np.testing.assert_array_equal(owned, original)

        for broken, code in (
            (_StructuralFrame(frame.handle, frame.epoch + 1, frame.frame_seq,
                              frame.height, frame.width, original, frame.digest),
             "E_M3_FRAME_IDENTITY"),
            (_StructuralFrame(frame.handle, frame.epoch, frame.frame_seq + 1,
                              frame.height, frame.width, original, frame.digest),
             "E_M3_FRAME_IDENTITY"),
            (_StructuralFrame(frame.handle, frame.epoch, frame.frame_seq,
                              frame.height, frame.width + 1, original, frame.digest),
             "E_M3_FRAME_SHAPE"),
            (_StructuralFrame(frame.handle, frame.epoch, frame.frame_seq,
                              frame.height, frame.width, original, "0" * 64),
             "E_M3_DIGEST"),
        ):
            invalid = md.WindowFrames(src.handle, src.epoch, (broken,),
                                      (frame.frame_seq,), src.dpr_ppm)
            spy = _ObservingRunner(synthetic=True)
            response = md.describe_windows((invalid,), runner=spy)
            self.assertTrue(response[0].error.startswith(code + ": "))
            self.assertEqual(spy.calls, 0)

    def test_failing_before_repair_double_tag_reproduction_and_reverse(self):
        first = _sample("one", seed=1)
        second = _sample("two", seed=2)
        stale = _ObservingRunner(reverse=True, double_tag=True)
        stale_output = md.describe_windows((first, second), runner=stale)
        self.assertEqual(stale.submitted, (0, 1))
        self.assertEqual(stale.completed, (1, 0))
        # The old runner's nested enumeration is a demonstrably failing fixture.
        self.assertTrue(all(x.error.startswith("E_M3_RUNNER_PROTOCOL") for x in stale_output))
        with self.assertRaises(AssertionError):
            self.assertIsNone(stale_output[0].error)

        correct = _ObservingRunner(reverse=True)
        outputs = md.describe_windows((first, second), runner=correct)
        self.assertEqual(correct.completed, (1, 0))
        self.assertEqual([r.handle for r in outputs], [first.handle, second.handle])
        self.assertTrue(all(r.error is None for r in outputs))
        self.assertNotEqual(outputs[0].descriptors, outputs[1].descriptors)

    def test_seeded_schema_refusal_before_any_runner_submission(self):
        rng = random.Random(SEED)
        counterexamples = []
        for trial in range(18):
            size = (rng.randrange(6, 12), rng.randrange(6, 12))
            base = _sample(f"invalid-{trial}", seed=SEED + trial,
                           seqs=(rng.randrange(1, 1000),), size=size)
            mode = trial % 3
            if mode == 0:
                bad = md.WindowFrames(base.handle, base.epoch, base.frames,
                                      base.frame_seqs, 0)
                expected = DPR_ERROR
            elif mode == 1:
                bad = md.WindowFrames(base.handle, base.epoch,
                                      (base.frames[0].astype(np.float32),),
                                      base.frame_seqs, base.dpr_ppm)
                expected = RGB_ERROR
            else:
                bad = md.WindowFrames(base.handle, base.epoch,
                                      (base.frames[0][:, ::2, :],),
                                      base.frame_seqs, base.dpr_ppm)
                expected = RGB_ERROR
            runner = _ObservingRunner(synthetic=True)
            response = md.describe_windows([bad], runner=runner)
            if not (len(response) == 1 and response[0].error == expected
                    and response[0].descriptors == () and runner.calls == 0):
                counterexamples.append((size[0] * size[1], trial, mode, size,
                                        response[0].error, runner.calls))
        smallest = min(counterexamples, default=None)
        self.assertEqual(counterexamples, [], f"replay_seed={SEED} smallest={smallest}")

    def test_channels_manifest_mutant_killed_at_submission_boundary(self):
        def observe(mod):
            item = _sample("channels-gate", seed=SEED + 3)
            own = mod.WindowFrames(mod.Handle(item.handle.kind, item.handle.canonical_id,
                                              item.handle.generation), item.epoch,
                                   item.frames, item.frame_seqs, item.dpr_ppm)
            submitted = []
            original_analyze = mod.pipeline.analyze

            def inspect_manifest(source, **kwargs):
                submitted.append(dict(source.meta))
                return original_analyze(source, **kwargs)

            with mock.patch.object(mod.pipeline, "analyze", side_effect=inspect_manifest):
                runner = _ObservingRunner()
                result = mod.describe_windows((own,), runner=runner)
            self.assertEqual(runner.calls, 1)
            self.assertIsNone(result[0].error)
            self.assertEqual(len(result[0].descriptors), len(item.frame_seqs))
            self.assertEqual(len(submitted), 1)
            return submitted[0]["channels"]

        def assert_behavior(channels):
            self.assertEqual(channels, 3)

        real = observe(md)
        assert_behavior(real)  # failed positive never qualifies as a kill
        with _isolated_mutant("channels") as mutant:
            mutated = observe(mutant)
            with self.assertRaises(AssertionError):
                assert_behavior(mutated)
        self.assertNotIn(MUTANT_PREFIX + "channels", sys.modules)

    def test_identity_membership_mutant_killed_before_submission(self):
        def observe(mod):
            item = _sample("bad-kind", kind="unknown_transport", seed=SEED + 4)
            own = mod.WindowFrames(mod.Handle(item.handle.kind, item.handle.canonical_id,
                                              item.handle.generation), item.epoch,
                                   item.frames, item.frame_seqs, item.dpr_ppm)
            runner = _ObservingRunner(synthetic=True,
                                      description_type=mod.WindowDescription)
            response = mod.describe_windows((own,), runner=runner)
            return response[0].error, runner.calls

        def assert_behavior(observation):
            self.assertEqual(observation[1], 0)  # one-condition dispatch boundary

        real = observe(md)
        self.assertTrue(real[0].startswith("E_M3_HANDLE: "))
        assert_behavior(real)
        with _isolated_mutant("identity") as mutant:
            mutated = observe(mutant)
            with self.assertRaises(AssertionError):
                assert_behavior(mutated)
        self.assertNotIn(MUTANT_PREFIX + "identity", sys.modules)


class TestOriginalMethodReplay(unittest.TestCase):
    def test_inventory_and_every_affected_original_id(self):
        # AST inventory comparisons are independent from unittest discovery.
        for cls_path, expected in ORIGINAL_METHODS.items():
            filename = cls_path.rsplit(".", 1)[0].replace(".", "/") + ".py"
            src = Path(filename).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=filename)
            class_name = cls_path.rsplit(".", 1)[1]
            cls = next(n for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name == class_name)
            found = {n.name for n in cls.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name.startswith("test_")}
            self.assertEqual(found, set(expected), f"changed original inventory: {cls_path}")

        loader = unittest.TestLoader()
        prior_pools = set(md._POOLS)
        prior_threads = {id(t) for t in threading.enumerate()}
        try:
            for cls_path in EDITED_LEGACY:
                for method in ORIGINAL_METHODS[cls_path]:
                    test_id = f"{cls_path}.{method}"
                    suite = loader.loadTestsFromName(test_id)
                    self.assertEqual(suite.countTestCases(), 1, f"missing {test_id}")
                    result = unittest.TestResult()
                    suite.run(result)
                    self.assertEqual(result.testsRun, 1, test_id)
                    for attribute in ("failures", "errors", "skipped",
                                      "expectedFailures", "unexpectedSuccesses"):
                        actual = getattr(result, attribute)
                        self.assertFalse(actual, f"{test_id}: {attribute}: {actual}")
        finally:
            # Legacy methods intentionally use persistent bounded pools. Restore
            # the pre-gate state rather than leave their test-created threads.
            with md._POOL_GUARD:
                created = {key: md._POOLS.pop(key) for key in tuple(md._POOLS)
                           if key not in prior_pools}
            for pool in created.values():
                pool.shutdown(wait=False, cancel_futures=True)
                for thread in tuple(getattr(pool, "_threads", ())):
                    thread.join(timeout=3)
                    if thread.is_alive():
                        self.fail("thread leak in original-method replay")
        leaked = [t.name for t in threading.enumerate()
                  if t.name.startswith("multi-describe-") and id(t) not in prior_threads]
        self.assertEqual(leaked, [], f"unexpected persistent threads: {leaked}")


if __name__ == "__main__":
    unittest.main()
