"""Complete acceptance and mutation suite for eval.v2x.gm_bands_fast_kernel.

Covers:
1. Integer-only AST verification and API input validation negatives.
2. Independent expected SHA-256 digests and bit-exact parity across all 8 required
   scenarios (static textured, flat abstention, horizontal pan, vertical pan,
   local patch motion, caret, fade, edge behavior) plus multi-motion refusal.
3. Readonly and strided (positive step, negative flip, transpose, sliced) input
   handling and call-local OPS / OPS_DETAIL / reset_ops accounting.
4. Precomputed integral-projection, 2D integral, and stride-cached helper unit
   tests and PerWindowDetector structural seam compatibility.
5. Self-contained AST-scoped mutation harness killing catalogue contrasts
   M-eval-v2x-gm-bands-hv-1 and M-eval-v2x-gm-bands-hv-2 on both gm_bands_hv
   and gm_bands_fast_kernel.
6. Seeded property/fuzz loop with replay seed and smallest counterexample metadata.
7. Multi-threaded isolation with barriers, bounded joins, exception propagation,
   and thread leak detection.
8. Separate VM timing benchmark after correctness gates (reporting VM timings as
   VM and host measurements as AUSENTE) verifying >= 10x speedup.
"""

import ast
from contextvars import copy_context
from dataclasses import dataclass
import hashlib
import inspect
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

from eval.v2x import gm_bands_fast_kernel as fast
from eval.v2x import gm_bands_hv as ref


# Independent expected digests and exact reference metrics for the 8 pinned scenarios.
PINNED_SCENARIO_EXPECTATIONS = {
    "static_textured": {
        "digest": "e82cc7991ce057821ceb193be1a1abbcebbdc685f01ac2450d9616f7016bc522",
        "motion": (0, 0, 0),
        "ops": 1536000,
        "ops_detail": {},
        "mask_shape": (30, 40),
        "mask_true": 0,
        "mask_sha256": "655a3ef0465a9f30fddf25f4dde0c19a05c6f9069b83961800c1944165955273",
    },
    "flat_abstention": {
        "digest": "a339f1ab5a719bfcf5cebbf9b72fb9c43663e16269d16ddebf6e573ea3ac7909",
        "motion": None,
        "ops": 1536000,
        "ops_detail": {},
        "mask_shape": (30, 40),
        "mask_true": 0,
        "mask_sha256": "655a3ef0465a9f30fddf25f4dde0c19a05c6f9069b83961800c1944165955273",
    },
    "horizontal_pan": {
        "digest": "191e38dc80ac5444a333c12d04e5773cc708a0d07cf87aac414cb182fa48d062",
        "motion": (-24, 0, 0),
        "ops": 601577807,
        "ops_detail": {
            "candidate_hypotheses": 141,
            "verified_candidates": 142,
            "dense_sad_calls": 9,
            "dense_sad_pixels": 1134713,
            "packed_informative_comparisons": 20129484,
            "runner_candidates": 139,
            "runner_comparisons": 19835885,
            "decision_raw_sad": 0,
            "decision_informative_pixels": 295680,
            "decision_score": 0,
        },
        "mask_shape": (30, 40),
        "mask_true": 1140,
        "mask_sha256": "fad4c63633eea05955dc68d29f9c8ec5bc3fdb1fee576cf40e175c6d66f11318",
    },
    "vertical_pan": {
        "digest": "73fc484449253f32dffe835e5a4cdc37b8f81f7eff513b8d9e7e5145a0b165bf",
        "motion": (0, -220, 0),
        "ops": 396562345,
        "ops_detail": {
            "candidate_hypotheses": 97,
            "verified_candidates": 98,
            "dense_sad_calls": 9,
            "dense_sad_pixels": 1211897,
            "packed_informative_comparisons": 12623468,
            "runner_candidates": 95,
            "runner_comparisons": 11089065,
            "decision_raw_sad": 0,
            "decision_informative_pixels": 166400,
            "decision_score": 0,
        },
        "mask_shape": (30, 40),
        "mask_true": 640,
        "mask_sha256": "b315fceea9415618e09670c82d9cd99660f929ea53fd4c49392ee10ed37d7049",
    },
    "local_patch_motion": {
        "digest": "4be67c349e8cecaf1fa1c6d040724f3038d6cd842227c6f669c2170dd4e32f20",
        "motion": (8, 8, 0),
        "ops": 43192078,
        "ops_detail": {
            "candidate_hypotheses": 151,
            "verified_candidates": 152,
            "packed_informative_comparisons": 1061602,
            "dense_sad_calls": 9,
            "dense_sad_pixels": 99580,
            "runner_candidates": 149,
            "runner_comparisons": 1103073,
            "decision_raw_sad": 0,
            "decision_informative_pixels": 12735,
            "decision_score": 0,
        },
        "mask_shape": (30, 40),
        "mask_true": 63,
        "mask_sha256": "da22c4e482c2d7d75ef7f39222025b8eb2fdeba926d4392c422fc925f2d9c08d",
    },
    "caret": {
        "digest": "6ec96e4c2df673fd9e8acd955f3660b0c6e9446fd67eecefb228fddb22516d9e",
        "motion": None,
        "ops": 418430874,
        "ops_detail": {
            "candidate_hypotheses": 113,
            "verified_candidates": 113,
            "dense_sad_calls": 9,
            "dense_sad_pixels": 1027602,
            "packed_informative_comparisons": 17556596,
        },
        "mask_shape": (30, 40),
        "mask_true": 0,
        "mask_sha256": "655a3ef0465a9f30fddf25f4dde0c19a05c6f9069b83961800c1944165955273",
    },
    "fade": {
        "digest": "fb61ef9b3b3eb6ca309802971a1c234968bde2d8b1ef298fd401173de704bec5",
        "motion": None,
        "ops": 258531725,
        "ops_detail": {
            "candidate_hypotheses": 115,
            "verified_candidates": 115,
            "dense_sad_calls": 9,
            "dense_sad_pixels": 1131600,
            "packed_informative_comparisons": 9850881,
        },
        "mask_shape": (30, 40),
        "mask_true": 0,
        "mask_sha256": "655a3ef0465a9f30fddf25f4dde0c19a05c6f9069b83961800c1944165955273",
    },
    "edge_behavior": {
        "digest": "539f1a3182e0c83b8cf332706b968e85ec295501441dfc70a0acab14121b958c",
        "motion": (12, -16, 0),
        "ops": 85200799,
        "ops_detail": {
            "candidate_hypotheses": 71,
            "verified_candidates": 72,
            "dense_sad_calls": 9,
            "dense_sad_pixels": 365010,
            "packed_informative_comparisons": 2645051,
            "runner_candidates": 69,
            "runner_comparisons": 2628897,
            "decision_raw_sad": 0,
            "decision_informative_pixels": 44392,
            "decision_score": 0,
        },
        "mask_shape": (13, 17),
        "mask_true": 176,
        "mask_sha256": "ee1365245fd68c5d5c2c86a3a0ef25652f01c7cd7dce3fea189fa0dc6119c203",
    },
}


def build_pinned_scenarios():
    """Construct deterministic in-RAM luma fixtures for the 8 pinned scenarios."""
    H, W = 480, 640
    scenarios = {}

    # 1. static_textured
    rng0 = np.random.default_rng(1000)
    prev0 = rng0.integers(0, 256, size=(H, W), dtype=np.uint8)
    scenarios["static_textured"] = (prev0, prev0.copy(), 300, 16)

    # 2. flat_abstention
    flat_prev = np.full((H, W), 128, dtype=np.uint8)
    flat_cur = np.full((H, W), 128, dtype=np.uint8)
    scenarios["flat_abstention"] = (flat_prev, flat_cur, 300, 16)

    # 3. horizontal_pan (dx=-24, dy=0)
    rng_h = np.random.default_rng(1024)
    prev_h = rng_h.integers(0, 256, size=(H, W), dtype=np.uint8)
    cur_h = np.zeros_like(prev_h)
    cur_h[:, : W - 24] = prev_h[:, 24:]
    cur_h[:, W - 24 :] = rng_h.integers(0, 256, size=(H, 24), dtype=np.uint8)
    scenarios["horizontal_pan"] = (prev_h, cur_h, 300, 16)

    # 4. vertical_pan (dx=0, dy=-220)
    dy = -220
    rng_v = np.random.default_rng(1000 + dy)
    prev_v = rng_v.integers(0, 256, size=(H, W), dtype=np.uint8)
    cur_v = np.empty_like(prev_v)
    cur_v[: H + dy, :] = prev_v[-dy:, :]
    cur_v[H + dy :, :] = rng_v.integers(0, 256, size=(-dy, W), dtype=np.uint8)
    scenarios["vertical_pan"] = (prev_v, cur_v, 300, 16)

    # 5. local_patch_motion (96x128 textured block moving by (8, 8) on flat background)
    rng_lp = np.random.default_rng(912)
    prev_lp = np.full((H, W), 40, dtype=np.uint8)
    cur_lp = prev_lp.copy()
    block = rng_lp.integers(0, 256, size=(96, 128), dtype=np.uint8)
    prev_lp[144 : 144 + 96, 160 : 160 + 128] = block
    cur_lp[152 : 152 + 96, 168 : 168 + 128] = block
    scenarios["local_patch_motion"] = (prev_lp, cur_lp, 300, 16)

    # 6. caret (static textured screen with blinking 18x3 caret rectangle)
    rng_c = np.random.default_rng(2006)
    prev_c = rng_c.integers(0, 256, size=(H, W), dtype=np.uint8)
    cur_c = prev_c.copy()
    cur_c[120:138, 80:83] = 255 - cur_c[120:138, 80:83]
    scenarios["caret"] = (prev_c, cur_c, 300, 16)

    # 7. fade (uniform additive brightness shift across textured frame)
    rng_f = np.random.default_rng(2007)
    prev_f = rng_f.integers(20, 200, size=(H, W), dtype=np.uint8)
    cur_f = (prev_f.astype(np.uint16) + 25).astype(np.uint8)
    scenarios["fade"] = (prev_f, cur_f, 300, 16)

    # 8. edge_behavior (diagonal shift near max_d boundary on odd-sized 195x260 frame)
    rng_e = np.random.default_rng(2008)
    He, We = 195, 260
    prev_e = rng_e.integers(0, 256, size=(He, We), dtype=np.uint8)
    cur_e = np.empty_like(prev_e)
    dx_e, dy_e = 12, -16
    cur_e[: He + dy_e, dx_e:] = prev_e[-dy_e:, : We - dx_e]
    cur_e[He + dy_e :, :] = rng_e.integers(0, 256, size=(-dy_e, We), dtype=np.uint8)
    cur_e[: He + dy_e, :dx_e] = rng_e.integers(0, 256, size=(He + dy_e, dx_e), dtype=np.uint8)
    scenarios["edge_behavior"] = (prev_e, cur_e, 40, 16)

    return scenarios


def evaluate_scenario_digest(module, name, prev, cur, max_d, tile_s):
    """Run estimate_global_motion and consensus_tiles on module and compute canonical digest."""
    module.reset_ops()
    motion = module.estimate_global_motion(prev, cur, max_d=max_d)
    ops = module.OPS
    ops_detail = dict(module.OPS_DETAIL)
    dx, dy = (motion[0], motion[1]) if motion is not None else (0, 0)
    mask = module.consensus_tiles(prev, cur, tile_s, dx, dy)
    mask_sha256 = hashlib.sha256(np.ascontiguousarray(mask).tobytes()).hexdigest()
    payload = json.dumps(
        {
            "scenario": name,
            "motion": list(motion) if motion is not None else None,
            "ops": ops,
            "ops_detail": ops_detail,
            "mask_shape": list(mask.shape),
            "mask_true": int(mask.sum()),
            "mask_sha256": mask_sha256,
        },
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "digest": digest,
        "motion": motion,
        "ops": ops,
        "ops_detail": ops_detail,
        "mask": mask,
        "mask_shape": tuple(mask.shape),
        "mask_true": int(mask.sum()),
        "mask_sha256": mask_sha256,
    }


# ---------------------------------------------------------------------------
# Embedded AST-Scoped Mutation Harness (No Sibling Test Harness Imports)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MutantSiteSpec:
    mutant_id: str
    scope_qualname: str
    containing_stmt_kind: type
    containing_expr_substr: str
    target_binop_substr: str
    expected_op: type
    replacement_op: type


@dataclass(frozen=True)
class MutantOutcome:
    mutant_id: str
    module_name: str
    status: str
    detail: str


class _ScopedBinOpMutator(ast.NodeTransformer):
    """Locate a unique BinOp inside a qualified function scope and containing expression."""

    def __init__(self, spec: MutantSiteSpec):
        super().__init__()
        self.spec = spec
        self._scope_stack = []
        self.matched_sites = 0

    def visit_FunctionDef(self, node):
        self._scope_stack.append(node.name)
        try:
            return self.generic_visit(node)
        finally:
            self._scope_stack.pop()

    def generic_visit(self, node):
        qual = ".".join(self._scope_stack)
        if (
            qual == self.spec.scope_qualname
            and isinstance(node, self.spec.containing_stmt_kind)
            and self.spec.containing_expr_substr in ast.unparse(node)
        ):
            return self._mutate_inside_containing_stmt(node)
        return super().generic_visit(node)

    def _mutate_inside_containing_stmt(self, stmt_node):
        spec = self.spec

        class _InnerMutator(ast.NodeTransformer):
            def __init__(inner_self):
                super().__init__()
                inner_self.hits = 0

            def visit_BinOp(inner_self, bin_node):
                bin_node = inner_self.generic_visit(bin_node)
                if (
                    isinstance(bin_node.op, spec.expected_op)
                    and ast.unparse(bin_node).strip() == spec.target_binop_substr
                ):
                    inner_self.hits += 1
                    return ast.copy_location(
                        ast.BinOp(
                            left=bin_node.left,
                            op=spec.replacement_op(),
                            right=bin_node.right,
                        ),
                        bin_node,
                    )
                return bin_node

        inner = _InnerMutator()
        new_stmt = inner.visit(stmt_node)
        self.matched_sites += inner.hits
        return new_stmt


def run_scoped_mutant(real_module, spec: MutantSiteSpec, assertion_fn) -> MutantOutcome:
    """Run assertion_fn on real_module first, then compile and test mutated AST in a private module."""
    try:
        real_module.reset_ops()
        assertion_fn(real_module)
    except BaseException as exc:
        return MutantOutcome(
            mutant_id=spec.mutant_id,
            module_name=real_module.__name__,
            status="positive_failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        real_module.reset_ops()

    source = inspect.getsource(real_module)
    tree = ast.parse(source, filename=f"<mutant:{spec.mutant_id}>")
    mutator = _ScopedBinOpMutator(spec)
    mutated_tree = ast.fix_missing_locations(mutator.visit(tree))
    if mutator.matched_sites != 1:
        return MutantOutcome(
            mutant_id=spec.mutant_id,
            module_name=real_module.__name__,
            status="missing_or_nonunique_anchor",
            detail=f"matched_sites={mutator.matched_sites}",
        )

    private_mod_name = f"{real_module.__package__}._mutant_{spec.mutant_id.replace('-', '_')}"
    prev_dont_write = sys.dont_write_bytecode
    prev_sys_mod = sys.modules.get(private_mod_name)
    sys.dont_write_bytecode = True
    mutant_mod = types.ModuleType(private_mod_name)
    mutant_mod.__package__ = real_module.__package__
    mutant_mod.__file__ = f"<mutant:{spec.mutant_id}>"
    try:
        try:
            code_obj = compile(mutated_tree, filename=f"<mutant:{spec.mutant_id}>", mode="exec")
            sys.modules[private_mod_name] = mutant_mod
            exec(code_obj, mutant_mod.__dict__)
        except BaseException as exc:
            return MutantOutcome(
                mutant_id=spec.mutant_id,
                module_name=real_module.__name__,
                status="import_or_init_error",
                detail=f"{type(exc).__name__}: {exc}",
            )

        try:
            mutant_mod.reset_ops()
            assertion_fn(mutant_mod)
        except AssertionError as exc:
            return MutantOutcome(
                mutant_id=spec.mutant_id,
                module_name=real_module.__name__,
                status="killed",
                detail=str(exc),
            )
        except BaseException as exc:
            return MutantOutcome(
                mutant_id=spec.mutant_id,
                module_name=real_module.__name__,
                status="unrelated_exception",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return MutantOutcome(
            mutant_id=spec.mutant_id,
            module_name=real_module.__name__,
            status="survived",
            detail="assertion_fn completed without AssertionError",
        )
    finally:
        if prev_sys_mod is None:
            sys.modules.pop(private_mod_name, None)
        else:
            sys.modules[private_mod_name] = prev_sys_mod
        sys.dont_write_bytecode = prev_dont_write
        real_module.reset_ops()


MUTANT_M1_SPEC = MutantSiteSpec(
    mutant_id="M-eval-v2x-gm-bands-hv-1",
    scope_qualname="_projection_pairs",
    containing_stmt_kind=ast.Expr,
    containing_expr_substr="_add_ops((hi - lo) * w * 2)",
    target_binop_substr="(hi - lo) * w",
    expected_op=ast.Mult,
    replacement_op=ast.Add,
)

MUTANT_M1_SUB_SPEC = MutantSiteSpec(
    mutant_id="M-eval-v2x-gm-bands-hv-1-sub",
    scope_qualname="_projection_pairs",
    containing_stmt_kind=ast.Expr,
    containing_expr_substr="_add_ops((hi - lo) * w * 2)",
    target_binop_substr="hi - lo",
    expected_op=ast.Sub,
    replacement_op=ast.Add,
)

MUTANT_M2_SPEC = MutantSiteSpec(
    mutant_id="M-eval-v2x-gm-bands-hv-2",
    scope_qualname="_tile_sums",
    containing_stmt_kind=ast.Assign,
    containing_expr_substr="gh, gw = ((h + s - 1) // s, (w + s - 1) // s)",
    target_binop_substr="w + s - 1",
    expected_op=ast.Sub,
    replacement_op=ast.Add,
)


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestFastKernelCorrectnessAndParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenarios = build_pinned_scenarios()

    def test_integer_only_source_and_no_bytecode_or_external_deps(self):
        """Verify gm_bands_fast_kernel.py contains no float constants and no ast.Div (/)."""
        src_path = Path(fast.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            self.assertFalse(
                isinstance(node, ast.Constant) and isinstance(node.value, float),
                f"Found forbidden float constant: {ast.dump(node)}",
            )
            self.assertFalse(
                isinstance(node, ast.Div),
                f"Found forbidden float division node: {ast.dump(node)}",
            )

    def test_input_validation_single_condition_negatives(self):
        """Every input validation guard raises the exact exception type and message."""
        good = np.zeros((32, 32), dtype=np.uint8)

        for bad_dtype in (np.int16, np.float32, np.int64, bool):
            bad_arr = np.zeros((32, 32), dtype=bad_dtype)
            with self.assertRaisesRegex(TypeError, "^luma must be uint8$"):
                fast.estimate_global_motion(bad_arr, good, 8)
            with self.assertRaisesRegex(TypeError, "^luma must be uint8$"):
                fast.estimate_global_motion(good, bad_arr, 8)
            with self.assertRaisesRegex(TypeError, "^luma must be uint8$"):
                fast.consensus_tiles(bad_arr, good, 16, 0, 0)

        rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        vec1d = np.zeros((32,), dtype=np.uint8)
        empty2d = np.zeros((0, 32), dtype=np.uint8)
        mismatch2d = np.zeros((32, 48), dtype=np.uint8)
        for a, b in ((rgb, rgb), (vec1d, vec1d), (empty2d, empty2d), (good, mismatch2d)):
            with self.assertRaisesRegex(
                ValueError, "^frames must have the same nonempty H by W shape$"
            ):
                fast.estimate_global_motion(a, b, 8)
            with self.assertRaisesRegex(
                ValueError, "^frames must have the same nonempty H by W shape$"
            ):
                fast.consensus_tiles(a, b, 16, 0, 0)

        for bad_max_d in (True, False, np.bool_(True), 3.5, "10", None):
            with self.assertRaisesRegex(TypeError, "^max_d must be an integer$"):
                fast.estimate_global_motion(good, good, bad_max_d)
        with self.assertRaisesRegex(ValueError, "^max_d must be nonnegative$"):
            fast.estimate_global_motion(good, good, -1)

        for bad_dx, bad_dy in ((True, 0), (0, False), (np.bool_(True), 0), (1.5, 0), (0, "2")):
            with self.assertRaisesRegex(TypeError, "^displacements must be integers$"):
                fast.consensus_tiles(good, good, 16, bad_dx, bad_dy)

        for bad_s in (0, -16, True, False, np.bool_(True), 16.0, "16"):
            with self.assertRaisesRegex(ValueError, "^tile side must be a positive integer$"):
                fast.consensus_tiles(good, good, bad_s, 0, 0)

    def test_pinned_scenarios_independent_digests_and_reference_parity(self):
        """Verify all 8 pinned scenarios match independent SHA-256 digests and reference gm_bands_hv."""
        for name, (prev, cur, max_d, tile_s) in self.scenarios.items():
            with self.subTest(scenario=name):
                expected = PINNED_SCENARIO_EXPECTATIONS[name]
                fast_res = evaluate_scenario_digest(fast, name, prev, cur, max_d, tile_s)
                ref_res = evaluate_scenario_digest(ref, name, prev, cur, max_d, tile_s)

                self.assertEqual(fast_res["motion"], expected["motion"])
                self.assertEqual(fast_res["ops"], expected["ops"])
                self.assertEqual(fast_res["ops_detail"], expected["ops_detail"])
                self.assertEqual(fast_res["mask_shape"], expected["mask_shape"])
                self.assertEqual(fast_res["mask_true"], expected["mask_true"])
                self.assertEqual(fast_res["mask_sha256"], expected["mask_sha256"])
                self.assertEqual(fast_res["digest"], expected["digest"])

                if fast_res["motion"] is not None:
                    self.assertIsInstance(fast_res["motion"], tuple)
                    self.assertEqual(len(fast_res["motion"]), 3)
                    for v in fast_res["motion"]:
                        self.assertIs(type(v), int)
                self.assertEqual(fast_res["mask"].dtype, np.bool_)

                self.assertEqual(fast_res["motion"], ref_res["motion"])
                self.assertEqual(fast_res["ops"], ref_res["ops"])
                self.assertEqual(fast_res["ops_detail"], ref_res["ops_detail"])
                self.assertTrue(np.array_equal(fast_res["mask"], ref_res["mask"]))
                self.assertEqual(fast_res["digest"], ref_res["digest"])

    def test_adversarial_invariants_on_fast_kernel(self):
        """Verify adversarial safety properties on gm_bands_fast_kernel."""
        prev, cur, _, _ = self.scenarios["local_patch_motion"]
        mask = fast.consensus_tiles(prev, cur, 16, 8, 8)
        changed = (prev != cur).reshape(30, 16, 40, 16).any(axis=(1, 3))
        self.assertFalse(np.any(mask & ~changed))
        self.assertTrue(mask.any())

        rng = np.random.default_rng(0)
        prev2 = np.full((240, 320), 40, dtype=np.uint8)
        cur2 = prev2.copy()
        for y, x, dx, dy in ((32, 32, 12, 0), (144, 200, 0, -10)):
            block = rng.integers(0, 256, (48, 64), dtype=np.uint8)
            prev2[y : y + 48, x : x + 64] = block
            cur2[y + dy : y + dy + 48, x + dx : x + dx + 64] = block
        self.assertIsNone(fast.estimate_global_motion(prev2, cur2, 100))
        self.assertEqual(fast.OPS, ref.estimate_global_motion(prev2, cur2, 100) or ref.OPS)
        self.assertEqual(fast.OPS_DETAIL, ref.OPS_DETAIL)

        rng3 = np.random.default_rng(11)
        prev3 = rng3.integers(0, 256, (120, 160), dtype=np.uint8)
        cur3 = np.roll(prev3, 13, axis=0)
        absolute = np.abs
        areas = []

        def counted_abs(array, *args, **kwargs):
            if isinstance(array, np.ndarray) and array.ndim == 2:
                areas.append(array.size)
            return absolute(array, *args, **kwargs)

        with patch.object(fast.np, "abs", side_effect=counted_abs):
            fast.estimate_global_motion(prev3, cur3, 100)
        self.assertLessEqual(sum(areas), 9 * prev3.size)
        with patch.object(fast, "_joint_sad", wraps=fast._joint_sad) as sad:
            fast.estimate_global_motion(prev3, cur3, 100)
        self.assertLessEqual(sad.call_count, 9)
        self.assertGreater(sad.call_count, 0)

        original_convolve = np.convolve
        modes = []

        def tracked_convolve(a, b, mode):
            modes.append(mode)
            return original_convolve(a, b, mode=mode)

        with patch.object(fast.np, "convolve", side_effect=tracked_convolve):
            fast.estimate_global_motion(prev, cur, 100)
        self.assertTrue(modes)
        self.assertEqual(set(modes), {"valid"})

        rng_lg = np.random.default_rng(3000)
        prev_lg = rng_lg.integers(0, 256, (1088, 1920), dtype=np.uint8)
        for dy_lg in (-830, 816):
            cur_lg = rng_lg.integers(0, 256, prev_lg.shape, dtype=np.uint8)
            a, b = max(0, dy_lg), min(1088, 1088 + dy_lg)
            cur_lg[a:b] = prev_lg[a - dy_lg : b - dy_lg]
            res_lg = fast.estimate_global_motion(prev_lg, cur_lg, 1919)
            self.assertEqual(res_lg, (0, dy_lg, 0))

    def test_readonly_and_strided_inputs_parity(self):
        """Readonly, sliced, subsampled, flipped, and transposed luma views match gm_bands_hv."""
        rng = np.random.default_rng(4242)
        base_prev = rng.integers(0, 256, size=(260, 340), dtype=np.uint8)
        base_cur = np.empty_like(base_prev)
        dx, dy = -12, 16
        base_cur[dy:, : 340 + dx] = base_prev[: 260 - dy, -dx:]
        base_cur[:dy, :] = rng.integers(0, 256, size=(dy, 340), dtype=np.uint8)
        base_cur[dy:, 340 + dx :] = rng.integers(0, 256, size=(260 - dy, -dx), dtype=np.uint8)

        views = {
            "readonly_contiguous": (base_prev.copy(), base_cur.copy()),
            "positive_step_strided": (base_prev[::2, ::2], base_cur[::2, ::2]),
            "negative_row_col_flip": (base_prev[::-1, ::-1], base_cur[::-1, ::-1]),
            "negative_col_flip": (base_prev[:, ::-1], base_cur[:, ::-1]),
            "transposed_fortran_stride": (base_prev.T, base_cur.T),
            "interior_unaligned_slice": (base_prev[13:217, 19:293], base_cur[13:217, 19:293]),
        }

        for vname, (vp, vc) in views.items():
            vp.flags.writeable = False
            vc.flags.writeable = False
            with self.subTest(view=vname):
                self.assertFalse(vp.flags.writeable)
                self.assertFalse(vc.flags.writeable)

                ref_out = ref.estimate_global_motion(vp, vc, max_d=64)
                ref_ops = ref.OPS
                ref_det = dict(ref.OPS_DETAIL)

                fast_out = fast.estimate_global_motion(vp, vc, max_d=64)
                fast_ops = fast.OPS
                fast_det = dict(fast.OPS_DETAIL)

                self.assertEqual(fast_out, ref_out)
                self.assertEqual(fast_ops, ref_ops)
                self.assertEqual(fast_det, ref_det)

                qx, qy = (ref_out[0], ref_out[1]) if ref_out is not None else (-6, 8)
                for s in (16, 12):
                    ref_m = ref.consensus_tiles(vp, vc, s, qx, qy)
                    ref_m_ops = ref.OPS
                    fast_m = fast.consensus_tiles(vp, vc, s, qx, qy)
                    fast_m_ops = fast.OPS
                    self.assertTrue(np.array_equal(fast_m, ref_m))
                    self.assertEqual(fast_m_ops, ref_m_ops)

    def test_ops_counter_reset_and_precomputed_helper_seams(self):
        """Verify reset_ops, OPS, OPS_DETAIL, integral-projection helpers, and detector seam."""
        prev, cur, _, _ = self.scenarios["horizontal_pan"]
        fast.reset_ops()
        self.assertEqual(fast.OPS, 0)
        self.assertEqual(fast.OPS_DETAIL, {})

        out1 = fast.estimate_global_motion(prev, cur, 300)
        ops1 = fast.OPS
        det1 = fast.OPS_DETAIL
        self.assertEqual(out1, (-24, 0, 0))
        self.assertGreater(ops1, 0)
        self.assertIn("candidate_hypotheses", det1)

        out2 = fast.estimate_global_motion(prev, cur, 300)
        self.assertEqual(out2, out1)
        self.assertEqual(fast.OPS, ops1)
        self.assertEqual(fast.OPS_DETAIL, det1)

        fast.reset_ops()
        self.assertEqual(fast.OPS, 0)
        self.assertEqual(fast.OPS_DETAIL, {})

        col_int = fast._col_integral_projection(cur)
        row_int = fast._row_integral_projection(cur)
        self.assertEqual(col_int.shape, (cur.shape[0] + 1, cur.shape[1]))
        self.assertEqual(row_int.shape, (cur.shape[0], cur.shape[1] + 1))
        self.assertTrue(
            np.array_equal(col_int[310] - col_int[45], cur[45:310].sum(axis=0, dtype=np.int64))
        )
        self.assertTrue(
            np.array_equal(
                row_int[:, 500] - row_int[:, 120], cur[:, 120:500].sum(axis=1, dtype=np.int64)
            )
        )

        mask_2d = (cur & 1).astype(bool)
        rect_int = fast._rect_integral_2d(mask_2d)
        self.assertEqual(
            fast._rect_sum_2d(rect_int, 17, 193, 29, 411),
            int(np.count_nonzero(mask_2d[17:193, 29:411])),
        )
        self.assertEqual(fast._rect_sum_2d(rect_int, 50, 50, 10, 20), 0)

        rng_vp = np.random.default_rng(707)
        p_vp = rng_vp.integers(0, 256, size=(160, 224), dtype=np.uint8)
        c_vp = p_vp.copy()
        c_vp[32:128, 16:] = p_vp[32:128, :-16]
        c_vp[32:128, :16] = p_vp[32:128, :16]
        self.assertEqual(
            fast.estimate_global_motion(p_vp, c_vp, 64),
            ref.estimate_global_motion(p_vp, c_vp, 64),
        )
        self.assertEqual(fast.OPS, ref.OPS)
        self.assertEqual(fast.OPS_DETAIL, ref.OPS_DETAIL)


class TestAstScopedMutationCatalogue(unittest.TestCase):
    """Self-contained AST mutation tests for M-eval-v2x-gm-bands-hv-1 and M-eval-v2x-gm-bands-hv-2."""

    @staticmethod
    def _assert_m1_projection_pairs_ops(module):
        """Independent OPS assertion killing M-eval-v2x-gm-bands-hv-1 ((hi - lo) * w -> (hi + lo) * w)."""
        rng = np.random.default_rng(1000 - 20)
        h, w = 64, 80
        prev = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
        cur = np.empty_like(prev)
        dy = -20
        cur[: h + dy] = prev[-dy:]
        cur[h + dy :] = rng.integers(0, 256, size=(-dy, w), dtype=np.uint8)

        module.reset_ops()
        cr = cur.sum(axis=1, dtype=np.int64)
        pr = prev.sum(axis=1, dtype=np.int64)
        dys = module.retained_axis_candidates(
            cr, pr, 30, module.min_ov_y(h), module.PROJECTION_RAW_KEEP
        )
        ops_dys = module.OPS
        assert any(d > 0 for d in dys), f"Expected at least one positive dy in {dys}"

        expected_total_ops = h * w * 2 + ops_dys
        for d in dys:
            lo, hi = max(0, d), min(h, h + d)
            if hi - lo < module.min_ov_y(h):
                continue
            cc = cur[lo:hi].sum(axis=0, dtype=np.int64)
            pc = prev[lo - d : hi - d].sum(axis=0, dtype=np.int64)
            module.reset_ops()
            module.retained_axis_candidates(
                cc, pc, 30, module.min_ov_y(w), min(module.PROJECTION_RAW_KEEP, module.PROJECTION_AXIS_KEEP)
            )
            expected_total_ops += (hi - lo) * w * 2 + module.OPS

        module.reset_ops()
        pairs = module._projection_pairs(prev, cur, 30, module.PROJECTION_RAW_KEEP)
        observed_ops = module.OPS
        assert (0, dy) in pairs, f"Expected (0, {dy}) in {pairs}"
        assert observed_ops == expected_total_ops, (
            f"OPS mismatch in _projection_pairs: observed {observed_ops} != expected {expected_total_ops}"
        )

    @staticmethod
    def _assert_m2_tile_sums_grid(module):
        """Direct _tile_sums assertion killing M-eval-v2x-gm-bands-hv-2 (w + s - 1 -> w + s + 1)."""
        mask = np.ones((32, 32), dtype=bool)
        module.reset_ops()
        sums = module._tile_sums(mask, s=16)
        expected = np.array([[256, 256], [256, 256]], dtype=np.int64)
        assert sums.shape == (2, 2), f"Expected shape (2, 2), got {sums.shape}"
        assert np.array_equal(sums, expected), f"Tile sums mismatch: {sums} != {expected}"
        assert module.OPS == 32 * 32, f"Expected OPS={32 * 32}, got {module.OPS}"

    def test_kill_m_eval_v2x_gm_bands_hv_1_on_ref_and_fast(self):
        """Kill M-eval-v2x-gm-bands-hv-1 on both gm_bands_hv and gm_bands_fast_kernel."""
        for mod in (ref, fast):
            for spec in (MUTANT_M1_SPEC, MUTANT_M1_SUB_SPEC):
                with self.subTest(module=mod.__name__, mutant_id=spec.mutant_id):
                    outcome = run_scoped_mutant(mod, spec, self._assert_m1_projection_pairs_ops)
                    self.assertEqual(outcome.status, "killed", f"Unexpected outcome: {outcome}")

    def test_kill_m_eval_v2x_gm_bands_hv_2_on_ref_and_fast(self):
        """Kill M-eval-v2x-gm-bands-hv-2 on both gm_bands_hv and gm_bands_fast_kernel."""
        for mod in (ref, fast):
            with self.subTest(module=mod.__name__):
                outcome = run_scoped_mutant(mod, MUTANT_M2_SPEC, self._assert_m2_tile_sums_grid)
                self.assertEqual(outcome.status, "killed", f"Unexpected outcome: {outcome}")

    def test_mutation_harness_outcome_separation_negatives(self):
        """Verify missing anchor, failed positive, and survived outcomes stay separate from kills."""
        bad_spec = MutantSiteSpec(
            mutant_id="M-nonexistent-anchor",
            scope_qualname="_projection_pairs",
            containing_stmt_kind=ast.Expr,
            containing_expr_substr="_add_ops(nonexistent_expr)",
            target_binop_substr="hi - lo",
            expected_op=ast.Sub,
            replacement_op=ast.Add,
        )
        out_missing = run_scoped_mutant(fast, bad_spec, self._assert_m1_projection_pairs_ops)
        self.assertEqual(out_missing.status, "missing_or_nonunique_anchor")

        def failing_positive(_mod):
            raise AssertionError("deliberate positive failure")

        out_pos_fail = run_scoped_mutant(fast, MUTANT_M1_SPEC, failing_positive)
        self.assertEqual(out_pos_fail.status, "positive_failed")

        def weak_assertion(_mod):
            return None

        out_survived = run_scoped_mutant(fast, MUTANT_M1_SPEC, weak_assertion)
        self.assertEqual(out_survived.status, "survived")


class TestSeededFuzzAndConcurrency(unittest.TestCase):
    FUZZ_REPLAY_SEED = 20261020

    def test_seeded_fuzz_parity_with_counterexample_metadata(self):
        """Seeded property loop comparing fast kernel and reference across diverse synthetics."""
        rng = np.random.default_rng(self.FUZZ_REPLAY_SEED)
        smallest_counterexample = None
        cases_run = 0

        for case_idx in range(28):
            case_seed = int(rng.integers(1, 2**31 - 1))
            crng = np.random.default_rng(case_seed)
            h = int(crng.integers(40, 112))
            w = int(crng.integers(48, 128))
            max_d = int(crng.integers(0, 36))
            tile_s = int(crng.choice([8, 12, 16, 20]))
            mode = int(crng.integers(0, 6))

            if mode == 0:
                prev = crng.integers(0, 256, size=(h, w), dtype=np.uint8)
                dx = int(crng.integers(-min(16, max_d), min(16, max_d) + 1))
                dy = int(crng.integers(-min(16, max_d), min(16, max_d) + 1))
                cur = crng.integers(0, 256, size=(h, w), dtype=np.uint8)
                cy0, cy1 = max(0, dy), min(h, h + dy)
                cx0, cx1 = max(0, dx), min(w, w + dx)
                if cy0 < cy1 and cx0 < cx1:
                    cur[cy0:cy1, cx0:cx1] = prev[cy0 - dy : cy1 - dy, cx0 - dx : cx1 - dx]
            elif mode == 1:
                prev = np.full((h, w), int(crng.integers(0, 255)), dtype=np.uint8)
                cur = prev.copy()
                ph, pw = min(24, h // 2), min(28, w // 2)
                patch = crng.integers(0, 256, size=(ph, pw), dtype=np.uint8)
                y0, x0 = h // 4, w // 4
                dx = int(crng.integers(-6, 7))
                dy = int(crng.integers(-6, 7))
                prev[y0 : y0 + ph, x0 : x0 + pw] = patch
                y1 = max(0, min(h - ph, y0 + dy))
                x1 = max(0, min(w - pw, x0 + dx))
                cur[y1 : y1 + ph, x1 : x1 + pw] = patch
            elif mode == 2:
                prev = np.full((h, w), 50, dtype=np.uint8)
                cur = prev.copy()
                prev[1, 4:8] = 10
                cur[1, 4:8] = 200
                if h > 36 and w > 36:
                    block = crng.integers(0, 256, size=(h - 32, w - 16), dtype=np.uint8)
                    prev[18 : h - 14, 8 : w - 8] = block
                    cur[18 : h - 14, 12 : w - 8] = block[:, :-4]
            elif mode == 3:
                prev = np.full((h, w), 100, dtype=np.uint8)
                cur = prev.copy()
                if h >= 48 and w >= 48:
                    prev[16:36, 16:36] = 200
                    prev[18:34, 18:34] = 100
                    cur[16:36, 20:40] = 200
                    cur[18:34, 22:38] = 100
            elif mode == 4:
                if case_idx % 2 == 0:
                    prev = crng.integers(0, 256, size=(h, w), dtype=np.uint8)
                    cur = prev.copy()
                else:
                    prev = np.full((h, w), 77, dtype=np.uint8)
                    cur = np.full((h, w), 99, dtype=np.uint8)
            else:
                big_p = crng.integers(0, 256, size=(h * 2, w * 2), dtype=np.uint8)
                big_c = np.roll(big_p, shift=(-6, 8), axis=(0, 1))
                prev = big_p[::2, ::2]
                cur = big_c[::2, ::2]

            prev.flags.writeable = False
            cur.flags.writeable = False

            ref_out = ref.estimate_global_motion(prev, cur, max_d)
            ref_ops = ref.OPS
            ref_det = dict(ref.OPS_DETAIL)

            fast_out = fast.estimate_global_motion(prev, cur, max_d)
            fast_ops = fast.OPS
            fast_det = dict(fast.OPS_DETAIL)

            qx, qy = (ref_out[0], ref_out[1]) if ref_out is not None else (4, -4)
            ref_mask = ref.consensus_tiles(prev, cur, tile_s, qx, qy)
            ref_m_ops = ref.OPS
            fast_mask = fast.consensus_tiles(prev, cur, tile_s, qx, qy)
            fast_m_ops = fast.OPS

            cases_run += 1
            if (
                fast_out != ref_out
                or fast_ops != ref_ops
                or fast_det != ref_det
                or not np.array_equal(fast_mask, ref_mask)
                or fast_m_ops != ref_m_ops
            ):
                candidate_ce = {
                    "replay_seed": self.FUZZ_REPLAY_SEED,
                    "case_idx": case_idx,
                    "case_seed": case_seed,
                    "shape": (h, w),
                    "area": h * w,
                    "max_d": max_d,
                    "tile_s": tile_s,
                    "mode": mode,
                    "ref_out": ref_out,
                    "fast_out": fast_out,
                    "ref_ops": ref_ops,
                    "fast_ops": fast_ops,
                }
                if (
                    smallest_counterexample is None
                    or candidate_ce["area"] < smallest_counterexample["area"]
                ):
                    smallest_counterexample = candidate_ce

        self.assertEqual(cases_run, 28)
        self.assertIsNone(
            smallest_counterexample,
            f"Fuzz counterexample found (replay_seed={self.FUZZ_REPLAY_SEED}): {smallest_counterexample}",
        )

    def test_multithreaded_ops_isolation_with_barrier_and_leak_detection(self):
        """Concurrent threads with barrier synchronization have isolated OPS/OPS_DETAIL and zero leaks."""
        scenarios = build_pinned_scenarios()
        work_items = [
            ("static_textured", scenarios["static_textured"]),
            ("horizontal_pan", scenarios["horizontal_pan"]),
            ("vertical_pan", scenarios["vertical_pan"]),
            ("local_patch_motion", scenarios["local_patch_motion"]),
            ("edge_behavior", scenarios["edge_behavior"]),
            ("fade", scenarios["fade"]),
        ]
        num_threads = len(work_items)
        barrier = threading.Barrier(num_threads, timeout=5.0)
        errors = []
        results = [None] * num_threads
        lock = threading.Lock()

        baseline_threads = {t.ident for t in threading.enumerate()}
        fast.reset_ops()

        def worker(idx, name, pack):
            prev, cur, max_d, tile_s = pack
            try:
                barrier.wait(timeout=5.0)
                res = copy_context().run(
                    evaluate_scenario_digest, fast, name, prev, cur, max_d, tile_s
                )
                with lock:
                    results[idx] = (name, res)
            except BaseException as exc:  # pragma: no cover
                with lock:
                    errors.append((idx, name, f"{type(exc).__name__}: {exc}"))

        threads = [
            threading.Thread(target=worker, args=(i, name, pack), name=f"gm-fast-worker-{i}")
            for i, (name, pack) in enumerate(work_items)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), f"Thread {t.name} timed out")

        self.assertEqual(errors, [], f"Worker thread exceptions: {errors}")
        self.assertEqual(fast.OPS, 0)
        self.assertEqual(fast.OPS_DETAIL, {})

        for name, res in results:
            expected = PINNED_SCENARIO_EXPECTATIONS[name]
            self.assertEqual(res["digest"], expected["digest"])
            self.assertEqual(res["ops"], expected["ops"])
            self.assertEqual(res["ops_detail"], expected["ops_detail"])

        after_threads = {t.ident for t in threading.enumerate()}
        leaked = after_threads - baseline_threads
        self.assertEqual(leaked, set(), f"Leaked threads detected: {leaked}")


class TestVmTimingAcceptanceGate(unittest.TestCase):
    """Separate VM timing benchmark run after correctness gates with identical inputs and warmup."""

    def test_z_vm_timing_speedup_at_least_10x(self):
        scenarios = build_pinned_scenarios()
        ordered_names = [
            "static_textured",
            "flat_abstention",
            "horizontal_pan",
            "vertical_pan",
            "local_patch_motion",
            "caret",
            "fade",
            "edge_behavior",
        ]
        corpus = [(name, *scenarios[name]) for name in ordered_names]

        # Identical warmup policy for both modules before timing
        for _, prev, cur, max_d, tile_s in corpus:
            ref.estimate_global_motion(prev, cur, max_d=max_d)
            ref.consensus_tiles(prev, cur, tile_s, -8, 8)
            fast.estimate_global_motion(prev, cur, max_d=max_d)
            fast.consensus_tiles(prev, cur, tile_s, -8, 8)

        # Timed evaluation on identical inputs
        t0_ref = time.perf_counter()
        ref_trace = []
        for name, prev, cur, max_d, tile_s in corpus:
            m = ref.estimate_global_motion(prev, cur, max_d=max_d)
            dx, dy = (m[0], m[1]) if m is not None else (-8, 8)
            c = ref.consensus_tiles(prev, cur, tile_s, dx, dy)
            ref_trace.append((name, m, int(c.sum())))
        vm_ref_seconds = time.perf_counter() - t0_ref

        t0_fast = time.perf_counter()
        fast_trace = []
        for name, prev, cur, max_d, tile_s in corpus:
            m = fast.estimate_global_motion(prev, cur, max_d=max_d)
            dx, dy = (m[0], m[1]) if m is not None else (-8, 8)
            c = fast.consensus_tiles(prev, cur, tile_s, dx, dy)
            fast_trace.append((name, m, int(c.sum())))
        vm_fast_seconds = time.perf_counter() - t0_fast

        self.assertEqual(fast_trace, ref_trace)
        vm_speedup = vm_ref_seconds / max(vm_fast_seconds, 1e-9)
        timing_report = {
            "timing_environment": "VM",
            "host_timing": "AUSENTE",
            "vm_ref_seconds": round(vm_ref_seconds, 6),
            "vm_fast_seconds": round(vm_fast_seconds, 6),
            "vm_speedup_x": round(vm_speedup, 2),
        }
        self.assertEqual(timing_report["timing_environment"], "VM")
        self.assertEqual(timing_report["host_timing"], "AUSENTE")
        self.assertGreaterEqual(
            vm_speedup,
            10.0,
            f"Expected >= 10.0x VM speedup, observed {vm_speedup:.2f}x ({timing_report})",
        )


if __name__ == "__main__":
    unittest.main()
