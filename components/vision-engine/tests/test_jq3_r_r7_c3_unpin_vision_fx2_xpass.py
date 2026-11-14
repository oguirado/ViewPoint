"""G-R7-3: explicit original-method audit and isolated source contrasts.

35 verified originals are executed by exact unittest IDs. Two unchanged
originals remain external blockers; their source and pins are preserved and
are never counted as repairs or mutation kills. All fixtures and mutation
machinery are local to this gate, offline, and use stdlib plus numpy.
"""

"""Embed this module's contents in the owned R7 gate; never import a test sibling.

An anchor is either an expression (parsed in eval mode) or exactly one complete
statement (parsed in exec mode).  Matching uses qualified scope and AST shape,
without source-line or file-wide ordinal selectors.  A structurally identical
replacement is explicitly equivalent; a different AST that passes is survived.
"""

import ast
import copy
from contextvars import copy_context
from dataclasses import dataclass, replace
import hashlib
import importlib
import importlib.util
import itertools
from pathlib import Path
import sys
import types
import unittest

import numpy as np


@dataclass(frozen=True)
class Mutation:
    name: str
    module: str
    scope: str
    anchor: str
    selector: tuple
    replacement: ast.AST


class BehavioralAssertion(AssertionError):
    """Only an explicit check after behavior is eligible to kill a mutant."""

    def __init__(self, message, **metadata):
        super().__init__(message)
        self.metadata = metadata


def check(condition, message="behavioral condition failed", **metadata):
    if not condition:
        raise BehavioralAssertion(message, **metadata)


def equal(actual, expected, message="behavioral equality failed", **metadata):
    if actual != expected:
        raise BehavioralAssertion(
            f"{message}: actual={actual!r}, expected={expected!r}", **metadata)


def array_equal(actual, expected, message="behavioral array equality failed", **metadata):
    if not np.array_equal(actual, expected):
        raise BehavioralAssertion(
            f"{message}: actual shape={np.shape(actual)}, expected shape={np.shape(expected)}",
            **metadata)


MUTATION_RESULTS = []
_MUTATION_SERIAL = itertools.count()
_MISSING_REGISTRATION = object()


def _scope_node(tree, qualified):
    current = tree
    parent = field = index = None
    for component in qualified.split("."):
        matches = [(i, node) for i, node in enumerate(getattr(current, "body", ()))
                   if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == component]
        if len(matches) != 1:
            raise ValueError(f"scope {qualified!r}: {component!r} has {len(matches)} matches")
        parent, field = current, "body"
        index, current = matches[0]
    return current, parent, field, index


def _scoped_nodes(node, parent=None, field=None, index=None, root=True):
    yield node, parent, field, index
    if not root and isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    for child_field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            yield from _scoped_nodes(value, node, child_field, None, False)
        elif isinstance(value, list):
            for child_index, child in enumerate(value):
                if isinstance(child, ast.AST):
                    yield from _scoped_nodes(child, node, child_field, child_index, False)


def _anchor_ast(text):
    try:
        return ast.parse(text, mode="eval").body
    except SyntaxError:
        statements = ast.parse(text, mode="exec").body
        if len(statements) != 1:
            raise ValueError("a statement anchor must contain exactly one statement")
        return statements[0]


def _selected_node(match, selector):
    node, parent, field, index = match
    for step in selector:
        if isinstance(step, str):
            if not isinstance(node, ast.AST) or step not in node._fields:
                raise ValueError(f"invalid AST field {step!r}")
            parent, field, index, node = node, step, None, getattr(node, step)
        elif type(step) is int:
            if not isinstance(node, list) or not 0 <= step < len(node):
                raise ValueError(f"invalid AST list index {step!r}")
            # The preceding field owns this list; keep that parent and field.
            index, node = step, node[step]
        else:
            raise ValueError("selectors contain only AST field names and integer list indices")
    if not isinstance(node, ast.AST) or parent is None:
        raise ValueError("selector must identify a replaceable AST node")
    return node, parent, field, index


def _restore_namespace(module, namespace):
    for name in tuple(module.__dict__):
        if name not in namespace:
            del module.__dict__[name]
    module.__dict__.update(namespace)


def _isolated_assertion(module, assertion):
    namespace = dict(module.__dict__)
    try:
        return copy_context().run(assertion, module)
    finally:
        # Restore even an incorrectly written fixture's direct module patches.
        # ContextVar values are confined to the discarded context above.
        _restore_namespace(module, namespace)


def _record_error(result, status, phase, error):
    result.update(status=status, phase=phase,
                  error_type=type(error).__name__, error=str(error))
    if isinstance(error, BehavioralAssertion):
        result["counterexample"] = dict(error.metadata)
    return result


def evaluate_mutation(spec, assertion):
    """Run the same explicit behavioral assertion on real then private source.

    A generic AssertionError raised by a constructor, callback or import is an
    error, never a kill.  Only BehavioralAssertion from check/equal/array_equal
    is a kill, and only after the real assertion passed.  These small offline
    cases have bounded loops; TimeoutError remains distinct if a seam raises it.
    No mutated source or bytecode is written to disk.
    """
    result = {"name": spec.name, "module": spec.module, "scope": spec.scope,
              "positive": False, "status": "unstarted", "phase": "positive",
              "anchor_count": None, "counterexample": None,
              "mutant_executed": False, "clean_teardown": False}
    previous_bytecode = sys.dont_write_bytecode
    private_name = None
    previous_private = _MISSING_REGISTRATION
    real = None
    real_registration = _MISSING_REGISTRATION
    sys.dont_write_bytecode = True
    try:
        try:
            real = importlib.import_module(spec.module)
            real_registration = sys.modules.get(spec.module, _MISSING_REGISTRATION)
        except TimeoutError as error:
            return _record_error(result, "timeout", "positive_import", error)
        except Exception as error:
            return _record_error(result, "positive_import_error", "positive_import", error)
        try:
            positive_metadata = _isolated_assertion(real, assertion)
        except BehavioralAssertion as error:
            return _record_error(result, "positive_failure", "positive", error)
        except TimeoutError as error:
            return _record_error(result, "timeout", "positive", error)
        except Exception as error:
            return _record_error(result, "positive_error", "positive", error)
        result["positive"] = True
        if isinstance(positive_metadata, dict):
            result["positive_metadata"] = positive_metadata
        try:
            source = Path(real.__file__).read_bytes()
            result["source_sha256"] = hashlib.sha256(source).hexdigest()
            tree = copy.deepcopy(ast.parse(source.decode("utf-8"), filename=real.__file__))
        except Exception as error:
            return _record_error(result, "source_error", "source", error)
        try:
            scoped = _scope_node(tree, spec.scope)
        except Exception as error:
            return _record_error(result, "invalid_scope", "anchor", error)
        try:
            anchor = _anchor_ast(spec.anchor)
            shape = ast.dump(anchor, include_attributes=False)
            matches = [entry for entry in _scoped_nodes(*scoped)
                       if ast.dump(entry[0], include_attributes=False) == shape]
        except Exception as error:
            return _record_error(result, "invalid_anchor", "anchor", error)
        result["anchor_count"] = len(matches)
        if not matches:
            result.update(status="missing_anchor", phase="anchor")
            return result
        if len(matches) != 1:
            result.update(status="nonunique_anchor", phase="anchor")
            return result
        try:
            selected, parent, field, index = _selected_node(matches[0], spec.selector)
            replacement = copy.deepcopy(spec.replacement)
            if not isinstance(replacement, ast.AST):
                raise ValueError("replacement must be an AST node")
            if ast.dump(selected, include_attributes=False) == ast.dump(replacement, include_attributes=False):
                result.update(status="equivalent", phase="selection",
                              equivalence="structurally_identical")
                return result
            replacement = ast.copy_location(replacement, selected)
            if index is None:
                setattr(parent, field, replacement)
            else:
                getattr(parent, field)[index] = replacement
            ast.fix_missing_locations(tree)
        except Exception as error:
            return _record_error(result, "invalid_selector", "selection", error)
        try:
            code = compile(tree, f"<r7-private:{spec.name}>", "exec")
        except Exception as error:
            return _record_error(result, "compile_error", "compile", error)

        package = real.__package__ or spec.module.rpartition(".")[0]
        private_leaf = f"_r7_private_mutant_{next(_MUTATION_SERIAL)}"
        private_name = f"{package}.{private_leaf}" if package else private_leaf
        previous_private = sys.modules.get(private_name, _MISSING_REGISTRATION)
        mutant = types.ModuleType(private_name)
        mutant.__package__ = package
        mutant.__file__ = real.__file__
        mutant.__spec__ = importlib.util.spec_from_loader(private_name, loader=None,
                                                        origin=real.__file__)
        sys.modules[private_name] = mutant
        try:
            copy_context().run(exec, code, mutant.__dict__)
        except TimeoutError as error:
            return _record_error(result, "timeout", "mutant_import", error)
        except Exception as error:
            return _record_error(result, "import_error", "mutant_import", error)
        result["mutant_executed"] = True
        try:
            _isolated_assertion(mutant, assertion)
        except BehavioralAssertion as error:
            return _record_error(result, "killed", "mutant_assertion", error)
        except TimeoutError as error:
            return _record_error(result, "timeout", "mutant_assertion", error)
        except Exception as error:
            return _record_error(result, "mutant_error", "mutant_assertion", error)
        result.update(status="survived", phase="mutant_assertion")
        return result
    finally:
        if private_name is not None:
            if previous_private is _MISSING_REGISTRATION:
                sys.modules.pop(private_name, None)
            else:
                sys.modules[private_name] = previous_private
        if real_registration is not _MISSING_REGISTRATION:
            sys.modules[spec.module] = real_registration
        sys.dont_write_bytecode = previous_bytecode
        private_clean = (private_name is None or
                         sys.modules.get(private_name, _MISSING_REGISTRATION) is previous_private)
        real_clean = (real is None or sys.modules.get(spec.module) is real_registration)
        result["clean_teardown"] = (private_clean and real_clean
                                    and sys.dont_write_bytecode is previous_bytecode)
        if not result["clean_teardown"]:
            result.update(status="teardown_error", phase="teardown")
        MUTATION_RESULTS.append(copy.deepcopy(result))


GM_PROJECTION_MUTATION = Mutation(
    "M-eval-v2x-gm-bands-hv-1", "eval.v2x.gm_bands_hv", "_projection_pairs",
    "_add_ops((hi - lo) * w * 2)", ("args", 0, "left", "left", "op"), ast.Add())
GM_TILE_SUMS_MUTATION = Mutation(
    "M-eval-v2x-gm-bands-hv-2", "eval.v2x.gm_bands_hv", "_tile_sums",
    "w + s - 1", ("op",), ast.Add())
CONSTELLATION_SELECTION_MUTATION = Mutation(
    "M-constellation-selection-cell-cap", "eval.v2x.gm_constellation", "select_features",
    'np.argsort(-block.ravel()[indices], kind="stable")[:FEATURES_PER_CELL]',
    ("slice", "upper"), ast.BinOp(left=ast.Name(id="FEATURES_PER_CELL", ctx=ast.Load()),
                                  op=ast.Sub(), right=ast.Constant(value=1)))


def assert_projection_accounting(module):
    # Both dimensions exceed MIN_OVERLAP; the seam returns one dy=4 and dx=0.
    # It performs no accounting, so only the two independent reductions count.
    h, w = 40, 48
    previous = np.arange(h * w, dtype=np.uint16).reshape(h, w).astype(np.uint8)
    current = previous.copy()
    original = module.retained_axis_candidates
    calls = []

    def axis(cur, prev, max_d, minimum, keep):
        calls.append(len(cur))
        return (4,) if len(calls) == 1 else (0,)

    module.retained_axis_candidates = axis
    try:
        module.reset_ops()
        pairs = module._projection_pairs(previous, current, max_d=8, keep=2)
        actual_ops = module.OPS
    finally:
        module.retained_axis_candidates = original
    equal(calls, [h, w], "projection seam must exercise both axes")
    equal(pairs, {(0, 4)}, "projection pair")
    equal(actual_ops, 2 * h * w + 2 * (h - 4) * w,
          "independent OPS for row and overlapping-column sums",
          smallest_counterexample={"height": h, "width": w, "dy": 4, "dx": 0})


def assert_tile_sums_integer_oracle(module):
    seed = 730203
    rng = np.random.default_rng(seed)
    # Exhaust this bounded shape catalogue, smallest area first.  It includes
    # exact multiples and one-pixel partial final rows/columns for each side.
    cases = sorted({(h, w, s) for s in (1, 2, 3, 4, 16)
                    for h in (1, s, s + 1, 2 * s)
                    for w in (1, max(1, s - 1), s, s + 1, 2 * s)},
                   key=lambda item: (item[0] * item[1], item[0], item[1], item[2]))
    for h, w, side in cases:
        mask = rng.integers(0, 2, size=(h, w), dtype=np.uint8).astype(bool)
        rows = []
        for y in range(0, h, side):
            row = []
            for x in range(0, w, side):
                total = 0
                for yy in range(y, min(h, y + side)):
                    for xx in range(x, min(w, x + side)):
                        total += int(mask[yy, xx])
                row.append(total)
            rows.append(row)
        expected = np.array(rows, dtype=np.int64)
        actual = module._tile_sums(mask, side)
        metadata = {"replay_seed": seed, "smallest_counterexample": {
            "height": h, "width": w, "tile_side": side,
            "mask": mask.astype(int).tolist(), "ordering": "smallest tested area first"}}
        equal(actual.dtype, np.dtype(np.int64), "tile sum dtype", **metadata)
        array_equal(actual, expected, "independent nested integer tile sums", **metadata)
    return {"replay_seed": seed, "cases": len(cases), "smallest_counterexample": None}


def assert_constellation_cell_selection(module):
    frame = ((np.indices((32, 32)).sum(axis=0) % 2) * 170 + 30).astype(np.uint8)
    before = frame.tobytes()
    features = module.select_features(frame)
    origins = [(feature.y, feature.x) for feature in features]
    equal(origins, [(0, 0), (0, 1), (0, 16), (1, 16), (16, 0), (16, 1), (16, 16)],
          "independent per-cell two-origin cap and row-major ties",
          smallest_counterexample={"height": 32, "width": 32, "low": 30, "high": 200})
    for feature in features:
        equal(feature.gradient, 2 * 16 * 15 * 170, "integer checkerboard gradient")
        equal(feature.signature, frame[feature.y:feature.y + 16,
                                       feature.x:feature.x + 16].tobytes(),
              "signature must preserve all exact luma bytes")
    equal(frame.tobytes(), before, "selection input bytes unchanged")


class GateMutationCoreTests(unittest.TestCase):
    def assert_killed_cleanly(self, spec, assertion):
        result = evaluate_mutation(spec, assertion)
        self.assertTrue(result["positive"], result)
        self.assertEqual(result["anchor_count"], 1, result)
        self.assertEqual(result["status"], "killed", result)
        self.assertTrue(result["clean_teardown"], result)
        self.assertTrue(result["clean_teardown"], result)
        return result

    def test_catalogue_projection_pairs_independent_ops(self):
        self.assert_killed_cleanly(GM_PROJECTION_MUTATION, assert_projection_accounting)

    def test_catalogue_private_tile_sums_integer_oracle(self):
        result = self.assert_killed_cleanly(GM_TILE_SUMS_MUTATION, assert_tile_sums_integer_oracle)
        self.assertEqual(result["counterexample"]["replay_seed"], 730203)
        self.assertEqual(result["counterexample"]["smallest_counterexample"]["height"], 1)

    def test_constellation_selection_cell_cap_contrast(self):
        self.assert_killed_cleanly(CONSTELLATION_SELECTION_MUTATION, assert_constellation_cell_selection)

    def test_harness_classifies_anchor_equivalent_and_survived(self):
        base = GM_TILE_SUMS_MUTATION
        cases = (
            (Mutation("missing", base.module, base.scope, "missing_anchor + 1", (), ast.Constant(0)), "missing_anchor"),
            (Mutation("nonunique", base.module, base.scope, "s", (), ast.Constant(1)), "nonunique_anchor"),
            (Mutation("scope", base.module, "absent_scope", base.anchor, base.selector, ast.Add()), "invalid_scope"),
            (Mutation("selector", base.module, base.scope, base.anchor, ("missing",), ast.Add()), "invalid_selector"),
            (Mutation("equivalent", base.module, base.scope, base.anchor, base.selector, ast.Sub()), "equivalent"),
            (Mutation("compile", base.module, base.scope, base.anchor, base.selector, ast.Constant(1)), "compile_error"),
        )
        for spec, expected in cases:
            with self.subTest(case=spec.name):
                result = evaluate_mutation(spec, lambda module: None)
                self.assertEqual(result["status"], expected, result)
                self.assertTrue(result["clean_teardown"], result)
        survivor = evaluate_mutation(replace(base, name="SELFTEST-survivor"),
                                     lambda module: equal(module.TILE, 16))
        self.assertEqual(survivor["status"], "survived", survivor)

    def test_harness_failed_positive_and_unrelated_errors_never_kill(self):
        def positive_failure(module):
            check(False, "intentional positive failure")

        failed = evaluate_mutation(replace(GM_TILE_SUMS_MUTATION, name="SELFTEST-positive-failure"),
                                   positive_failure)
        self.assertFalse(failed["positive"], failed)
        self.assertFalse(failed["mutant_executed"], failed)
        self.assertEqual(failed["status"], "positive_failure", failed)

        def constructor_error(module):
            raise AssertionError("unrelated constructor assertion")

        errored = evaluate_mutation(replace(GM_TILE_SUMS_MUTATION, name="SELFTEST-positive-constructor"),
                                    constructor_error)
        self.assertEqual(errored["status"], "positive_error", errored)
        self.assertFalse(errored["positive"], errored)

        def mutant_constructor_error(module):
            if "_r7_private_mutant_" in module.__name__:
                raise AssertionError("unrelated mutant constructor assertion")

        errored = evaluate_mutation(replace(GM_TILE_SUMS_MUTATION, name="SELFTEST-mutant-constructor"),
                                    mutant_constructor_error)
        self.assertTrue(errored["positive"], errored)
        self.assertEqual(errored["status"], "mutant_error", errored)

        def timeout(module):
            if "_r7_private_mutant_" in module.__name__:
                raise TimeoutError("bounded synthetic timeout classification")

        timed_out = evaluate_mutation(replace(GM_TILE_SUMS_MUTATION, name="SELFTEST-timeout"), timeout)
        self.assertEqual(timed_out["status"], "timeout", timed_out)
        self.assertEqual(timed_out["phase"], "mutant_assertion", timed_out)

    def test_harness_import_error_is_separate_and_teardown_is_clean(self):
        real = importlib.import_module("eval.v2x.gm_bands_hv")
        original_axis = real.retained_axis_candidates
        original_bytecode = sys.dont_write_bytecode
        modules_before = {name for name in sys.modules if "_r7_private_mutant_" in name}
        token = real._ops.set(37)
        diagnostic_token = real._diagnostics.set({"outer": 9})
        try:
            result = self.assert_killed_cleanly(
                replace(GM_PROJECTION_MUTATION, name="SELFTEST-projection-cleanup"),
                assert_projection_accounting,
            )
            self.assertEqual(real.OPS, 37)
            self.assertEqual(real.OPS_DETAIL, {"outer": 9})
            self.assertIs(real.retained_axis_candidates, original_axis)
            spec = Mutation("import-error", "eval.v2x.gm_bands_hv", "Prepared", "dataclass", (),
                            ast.Name(id="_missing_decorator_for_gate_selftest", ctx=ast.Load()))
            result = evaluate_mutation(spec, lambda module: None)
            self.assertEqual(result["status"], "import_error", result)
            self.assertTrue(result["positive"], result)
            self.assertTrue(result["clean_teardown"], result)
        finally:
            real._diagnostics.reset(diagnostic_token)
            real._ops.reset(token)
        self.assertIs(sys.dont_write_bytecode, original_bytecode)
        self.assertEqual({name for name in sys.modules if "_r7_private_mutant_" in name}, modules_before)
        self.assertIs(sys.modules[real.__name__], real)




"""Embedded FL and chain contrasts; no dependency on sibling test fixtures."""
import ast
import copy
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np


def _fl_trace_assertion(module):
    """Independent exact trace: S1 fast alternation, S2 redraw, S3 hp=8, static."""
    tracker = module.FlickerTracker(n_tiles=4)
    for frame in range(48):
        hashes = np.array([
            11 + frame % 2 if frame < 32 else 1000 + frame,
            2000 + frame,
            101 if (frame // 8) % 2 == 0 else 202,
            303,
        ], dtype=np.uint64)
        original = hashes.copy()
        hashes.flags.writeable = False
        states, halves = tracker.update(hashes)
        expected_states = [0, 0, 0, 0]
        if frame:
            expected_states = [2, 2, 0, 0]
            if frame in (8, 16):
                expected_states[2] = 2
            elif frame in (24, 32, 40):
                expected_states[2] = 4
        metadata = {
            "replay_seed": 0,
            "smallest_counterexample": {"frame": frame,
                                        "hashes": hashes.tolist()},
        }
        array_equal(states, expected_states,
                    "S1/S2/static must abstain; S3 declares only on departures",
                    **metadata)
        array_equal(halves, [0, 0, 8 if frame >= 24 else 0, 0],
                    "S3 half-period persists without contaminating other slots",
                    **metadata)
        array_equal(hashes, original, "tracker must not mutate caller hashes",
                    **metadata)


def _chain_order_assertion(module):
    """Hard-coded expectations for both priority orders and an explicit veto."""
    baseline = {
        "cls": np.array([4, 0, 2, 0], dtype=np.int64),
        "mv_dx": np.array([7, 0, 9, 1], dtype=np.int64),
        "mv_dy": np.array([-2, 0, 9, -1], dtype=np.int64),
    }
    gm = {
        "mask": np.array([False, False, True, False]),
        "cls": 3,
        "mv_dx": np.array([0, 0, -3, 0], dtype=np.int64),
        "mv_dy": np.array([0, 0, 5, 0], dtype=np.int64),
    }
    fl = {
        "mask": np.array([False, True, True, False]),
        "cls": 4,
        "mv_dx": np.zeros(4, dtype=np.int64),
        "mv_dy": np.zeros(4, dtype=np.int64),
    }
    snapshots = copy.deepcopy((baseline, gm, fl))
    veto = np.array([True, False, True, False], dtype=np.bool_)
    expected = {
        "gm_then_fl": ([2, 4, 3, 0], [7, 0, -3, 1], [-2, 0, 5, -1]),
        "fl_then_gm": ([2, 4, 4, 0], [7, 0, 0, 1], [-2, 0, 0, -1]),
    }
    for order in ("gm_then_fl", "fl_then_gm"):
        actual = module.compose_chain(baseline, gm, fl, order,
                                      flicker_veto=veto)
        for field, value in zip(("cls", "mv_dx", "mv_dy"), expected[order]):
            array_equal(actual[field], value,
                        "first positive claim must win after baseline-only veto",
                        order=order, field=field)
    for mapping, snapshot in zip((baseline, gm, fl), snapshots):
        for field in mapping:
            array_equal(mapping[field], snapshot[field],
                        "chain must preserve input mappings", field=field)
    array_equal(veto, [True, False, True, False],
                "chain must preserve its explicit veto mask")


def _baseline_veto_assertion(module):
    """Veto acts on original class four only; vectors and later claims survive."""
    baseline = {
        "cls": np.array([4, 3, 2, 4, 0], dtype=np.int64),
        "mv_dx": np.array([8, 9, 10, 11, 12], dtype=np.int64),
        "mv_dy": np.array([-8, -9, -10, -11, -12], dtype=np.int64),
    }
    claim = {
        "mask": np.array([False, False, False, False, True], dtype=np.bool_),
        "cls": 4,
        "mv_dx": np.zeros(5, dtype=np.int64),
        "mv_dy": np.zeros(5, dtype=np.int64),
    }
    veto = np.array([True, True, True, False, False], dtype=np.bool_)
    before = copy.deepcopy((baseline, claim, veto))
    actual = module.apply_claims(baseline, [claim], flicker_veto=veto)
    array_equal(actual["cls"], [2, 3, 2, 4, 4],
                "only vetoed baseline FLICKER is demoted")
    array_equal(actual["mv_dx"], [8, 9, 10, 11, 0],
                "veto preserves baseline x vectors and later claim vectors")
    array_equal(actual["mv_dy"], [-8, -9, -10, -11, 0],
                "veto preserves baseline y vectors and later claim vectors")
    for current, snapshot in zip((baseline, claim), before[:2]):
        for field in current:
            array_equal(current[field], snapshot[field],
                        "veto composition must preserve input mappings", field=field)
    array_equal(veto, before[2], "veto composition must preserve caller mask")


def _probe_tp_assertion(module):
    """Two fixed RAM-only scenes distinguish TP, FP, and unpredicted flicker."""
    scenes = [
        {"scene_id": "DEV:static_clean:gate_mixed", "kind": "static_clean",
         "w": 32, "h": 32, "n": 1, "split": "DEV"},
        {"scene_id": "DEV:canvas_anim:gate_tp", "kind": "canvas_anim",
         "w": 32, "h": 32, "n": 1, "split": "DEV"},
    ]
    scene_snapshot = copy.deepcopy(scenes)
    truth = {
        scenes[0]["scene_id"]: [
            {"f0": 0, "f1": 1, "ideal": 4, "tiles": [0, 3]},
            {"f0": 0, "f1": 1, "ideal": 2, "tiles": [1]},
        ],
        scenes[1]["scene_id"]: [
            {"f0": 0, "f1": 1, "ideal": 4, "tiles": [0]},
        ],
    }
    original_derive = module.derive
    for structured in (False, True):
        predictions = {}
        for scene, classes in zip(scenes, ([4, 4, 4, 0], [4, 0, 0, 0])):
            if structured:
                tiles = np.zeros(4, dtype=[("cls", np.uint8)])
                tiles["cls"] = classes
            else:
                tiles = {"cls": np.array(classes, dtype=np.uint8)}
            predictions[scene["scene_id"]] = tiles
        snapshots = copy.deepcopy(predictions)

        def analyze_in_memory(source, workers=1, cpus=None):
            return SimpleNamespace(
                tiles=[predictions[source.scene["scene_id"]]],
                frame_rows=[{"global_mv": None}],
            )

        def derive_events(scene, grid):
            return truth[scene["scene_id"]]

        with mock.patch.object(module, "derive", side_effect=derive_events):
            result = module.analyze_false_flicker(scenes, analyze_fn=analyze_in_memory)
        check(module.derive is original_derive, "probe fixture must restore derive")
        summary = result["summary"]
        equal(summary["total_false_flicker_tile_frames"], 2,
              "only two false-positive predictions are countable", structured=structured)
        equal(summary["counts_by_truth_class"], {
            "UNCHANGED": 1, "JITTER": 0, "CHANGED": 1, "MOVED": 0,
            "FLICKER": 0, "FROZEN": 0, "DRIFT": 0,
        }, "true positives must not enter any truth-class total", structured=structured)
        equal(summary["counts_by_cause_bucket"], {
            "moved_large_displacement": 0, "moved_small": 0,
            "changed_texture": 1, "other": 1,
        }, "true positives must not be rebucketed as other", structured=structured)
        mixed = result["kinds"]["static_clean"]
        records = [(r["tile"], r["truth_class"], r["cause_bucket"])
                   for r in mixed["records"]]
        equal(records, [(1, "CHANGED", "changed_texture"),
                        (2, "UNCHANGED", "other")],
              "only independently specified false positives appear in records",
              structured=structured)
        all_tp = result["kinds"]["canvas_anim"]
        equal(all_tp["total_false_flicker_tile_frames"], 0,
              "all-TP kind retains an explicit zero total", structured=structured)
        equal(all_tp["records"], [], "all-TP kind has no false-positive records")
        check(all(value == 0 for value in all_tp["counts_by_truth_class"].values()),
              "all-TP kind truth counters stay zero")
        check(all(value == 0 for value in all_tp["counts_by_cause_bucket"].values()),
              "all-TP kind cause counters stay zero")
        equal(scenes, scene_snapshot, "probe must preserve scene inputs")
        for scene_id, tiles in predictions.items():
            if structured:
                array_equal(tiles, snapshots[scene_id], "probe preserves structured tiles")
            else:
                array_equal(tiles["cls"], snapshots[scene_id]["cls"],
                            "probe preserves dictionary tiles")


_FL_MIN_HALF_MUTATION = Mutation(
    name="FL-minimum-half-run", module="eval.v2x.fl_blinkprior",
    scope="FlickerTracker.__init__", anchor="7 if self.max_half >= 7 else 1",
    selector=("body",), replacement=ast.Constant(value=1),
)
_CHAIN_FIRST_WINS_MUTATION = Mutation(
    name="chain-first-claim-wins", module="eval.v2eval.chain",
    scope="compose_chain", anchor='second["mask"] & ~first["mask"]',
    selector=("right",), replacement=ast.parse('first["mask"]', mode="eval").body,
)
_BASELINE_ONLY_VETO_MUTATION = Mutation(
    name="baseline-only-flicker-veto", module="eval.v2eval.postpass",
    scope="apply_claims", anchor='flicker_veto & (baseline["cls"] == 4)',
    selector=("right", "ops", 0), replacement=ast.NotEq(),
)
_PROBE_TP_MUTATION = Mutation(
    name="probe-true-positive-exclusion", module="eval.v2eval.false_flicker_probe",
    scope="analyze_false_flicker",
    anchor="(cls_array == C_FLICKER) & (truth_cls[f] != C_FLICKER)",
    selector=("right", "ops", 0), replacement=ast.Eq(),
)


class GateFlChainTests(unittest.TestCase):
    def _require_behavioral_kill(self, spec, assertion):
        report = evaluate_mutation(spec, assertion)
        self.assertIs(report["positive"], True, report)
        self.assertEqual(report["anchor_count"], 1, report)
        self.assertEqual(report["status"], "killed", report)
        self.assertTrue(report["clean_teardown"], report)
        self.assertTrue(report["clean_teardown"], report)

    def test_fl_minimum_half_run_preserves_s1_s2_s3_trace(self):
        self._require_behavioral_kill(_FL_MIN_HALF_MUTATION, _fl_trace_assertion)

    def test_chain_first_claim_wins_in_both_orders(self):
        self._require_behavioral_kill(_CHAIN_FIRST_WINS_MUTATION, _chain_order_assertion)

    def test_explicit_veto_changes_only_baseline_flicker(self):
        self._require_behavioral_kill(_BASELINE_ONLY_VETO_MUTATION, _baseline_veto_assertion)

    def test_probe_excludes_true_positives_before_all_accounting(self):
        self._require_behavioral_kill(_PROBE_TP_MUTATION, _probe_tp_assertion)


# Embedded by the owned gate; this fragment imports no sibling test helpers.
import contextlib
import os
import stat
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _tiny_gate_xwd():
    """One 2x2 TrueColor XWD, padded rows, built independently of xfb constants."""
    name = b"gate\x00"
    fields = [
        105, 7, 2, 24, 2, 2, 0, 0, 32, 0, 32, 32, 12,
        4, 0xFF0000, 0xFF00, 0xFF, 8, 0, 0, 2, 2, 0, 0, 0,
    ]
    pixels = bytes([
        3, 2, 1, 0, 6, 5, 4, 0, 0, 0, 0, 0,
        9, 8, 7, 0, 12, 11, 10, 0, 0, 0, 0, 0,
    ])
    return struct.pack(">25I", *fields) + name + pixels


def _xwd_code(module, action, expected, message):
    try:
        action()
    except module.XwdError as error:
        equal(error.code, expected, message, observed_error=str(error))
    else:
        check(False, message, expected_code=expected)


def _assert_xwd_read_bounds(module):
    frame = _tiny_gate_xwd()
    equal(len(frame), 129, "independent fixture size")
    equal(module.validate_frame(frame).size, 129, "valid exact frame")
    fd = 83
    calls = []
    valid = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=129, st_dev=3, st_ino=7)

    def observed_stat(received_fd):
        calls.append(("stat", received_fd))
        return valid

    def observed_read(received_fd, count, offset):
        calls.append(("pread", received_fd, count, offset))
        return frame

    with mock.patch.object(module.os, "fstat", side_effect=observed_stat), \
            mock.patch.object(module.os, "pread", side_effect=observed_read):
        try:
            actual = module.read_screen_fd(fd, max_bytes=129, tries=2, width=2, height=2)
        except module.XwdError as error:
            check(False, "exact budget must admit two coherent bounded snapshots", code=error.code)
        equal(actual, frame, "reader returns the complete independent fixture")
    equal(calls, [
        ("stat", fd), ("stat", fd), ("pread", fd, 129, 0), ("stat", fd),
        ("stat", fd), ("pread", fd, 129, 0), ("stat", fd), ("stat", fd),
    ], "fstat bounds every read and the final return")

    # One-condition negative: same regular-file identity, size exceeds cap by one.
    over = SimpleNamespace(st_mode=valid.st_mode, st_size=130, st_dev=3, st_ino=7)
    with mock.patch.object(module.os, "fstat", return_value=over), \
            mock.patch.object(module.os, "pread") as pread:
        _xwd_code(module, lambda: module.read_screen_fd(fd, max_bytes=129, tries=2),
                  "E_XWD_LIMIT", "over-budget fstat must refuse before allocation")
        equal(pread.call_count, 0, "initial over-budget source must not be read")

    # Same size and identity; only regular-file type is removed.
    special = SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_size=129, st_dev=3, st_ino=7)
    with mock.patch.object(module.os, "fstat", return_value=special), \
            mock.patch.object(module.os, "pread") as pread:
        _xwd_code(module, lambda: module.read_screen_fd(fd, max_bytes=129, tries=2),
                  "E_XWD_SOURCE", "special-node frame source must be refused")
        equal(pread.call_count, 0, "nonregular source cannot allocate a snapshot")

    # Stable first read; only st_size grows before the second allocation.
    with mock.patch.object(module.os, "fstat", side_effect=[valid, valid, valid, over]), \
            mock.patch.object(module.os, "pread", return_value=frame) as pread:
        _xwd_code(module, lambda: module.read_screen_fd(fd, max_bytes=129, tries=2),
                  "E_XWD_LIMIT", "growth cannot trigger an over-budget allocation")
        equal(pread.call_args_list, [mock.call(fd, 129, 0)], "only the first bounded read occurs")


def _assert_xwd_snapshot_copy(module):
    storage = bytearray(b"Aa")
    reads = []

    def changing_storage():
        storage[:] = b"Aa" if not reads else b"Bb"
        reads.append(bytes(storage))
        return storage

    actual = module.consistent_read(changing_storage, tries=3, max_bytes=2)
    check(type(actual) is bytes, "consistent snapshots must be immutable bytes")
    equal(actual, b"Bb", "stable pair is B/B after the unequal A/B prefix")
    equal(reads, [b"Aa", b"Bb", b"Bb"], "mutable alias cannot manufacture early equality")
    storage[:] = b"Cc"
    equal(actual, b"Bb", "returned snapshot must survive later writer changes")

    # Same callback and buffer type, only the cap becomes one byte too small.
    read = mock.Mock(return_value=bytearray(b"Bb"))
    _xwd_code(module, lambda: module.consistent_read(read, tries=3, max_bytes=1),
              "E_XWD_LIMIT", "mutable snapshot is bounded before conversion")
    equal(read.call_count, 1, "oversized first snapshot ends the read immediately")


def _assert_xwd_retry_limits(module):
    read = mock.Mock(return_value=b"Aa")
    equal(module.consistent_read(read, tries=2, max_bytes=2), b"Aa",
          "two identical reads are sufficient")
    equal(read.call_count, 2, "coherent positive consumes exactly two reads")
    for invalid in (1, 0, -1, True, False, 2.0, "2", None):
        read = mock.Mock(return_value=b"Aa")
        _xwd_code(module, lambda: module.consistent_read(read, tries=invalid, max_bytes=2),
                  "E_XWD_TRIES", "invalid tries must be rejected before callback")
        equal(read.call_count, 0, "invalid tries cannot perform reads", invalid_tries=repr(invalid))


def _xwd_protected_color_table_diagnostic(module):
    """Describe the preserved parser blocker without allocating a giant frame."""
    source = _tiny_gate_xwd()
    equal(module.validate_frame(source).size, 129, "unmodified tiny positive")
    forged = bytearray(source)
    struct.pack_into(">I", forged, 19 * 4, 0x1000000)
    equal(len(forged), 129, "forged count does not allocate the declared color table")
    equal(forged[:76], source[:76], "only the ncolors word changes")
    equal(forged[80:], source[80:], "all data after ncolors remains unchanged")
    try:
        header = module.parse_header(forged)
    except module.XwdError as error:
        return {"status": "NEEDS_REAUDIT", "code": error.code,
                "reason": "protected parse_header now refuses the formerly accepted count"}
    equal(header.ncolors, 0x1000000, "diagnostic observed the forged color count")
    equal(header.pixels_offset, 105 + 0x1000000 * 12, "independent declared offset")
    _xwd_code(module, lambda: module.validate_frame(forged), "E_XWD_SIZE",
              "frame validation must still refuse the missing declared table")
    return {
        "status": "BLOCKED", "owner": "fx2-f8-d11-x11-framebuffer-ram-capture",
        "scope": "vadapt.xfb.parse_header", "invariant": "D11-BOUNDS color-table budget",
        "observed": "parse_header accepts ncolors=16777216 with pixels_offset=201326697",
        "expected": "XwdError at the original parse_header refusal assertion",
        "fixture_bytes": 129,
    }


@contextlib.contextmanager
def _gate_ram_workspace(module, quota=64):
    """Real open_run admission on verified tmpfs; only inert metadata is written."""
    before_open = dict(module._OPEN)
    with tempfile.TemporaryDirectory(prefix="jq3-ram-", dir="/dev/shm") as host:
        free = SimpleNamespace(f_bavail=module.FREE_MIN + 1024 * 1024, f_frsize=1)
        # Signal/atexit registration belongs to the harness, not this local case.
        with mock.patch.object(module, "_install_handlers", return_value=None):
            ws = module.open_run("case", "run", quota, tool="jq3-gate", root=host,
                                 statvfs=lambda _path: free)
        try:
            yield ws, Path(host)
        finally:
            ws.close()
            if ws in module._OPEN or os.path.lexists(ws.root):
                raise RuntimeError("workspace registration or metadata leaked during teardown")
    if module._OPEN != before_open:
        raise RuntimeError("workspace registry changed during teardown")


@contextlib.contextmanager
def _gate_socket_metadata(module, node):
    """Empty inert pathname with faithful socket stat/lstat classification."""
    node = Path(node)
    with node.open("xb"):
        pass
    key = os.path.abspath(os.fspath(node))
    original_stat, original_lstat = module.os.stat, module.os.lstat

    def metadata(real, path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if isinstance(path, (str, bytes, os.PathLike)):
            normalized = os.path.abspath(os.fsdecode(os.fspath(path)))
            if normalized == key:
                values = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
                values.update(st_mode=stat.S_IFSOCK | 0o600, st_size=0, st_blocks=0)
                return SimpleNamespace(**values)
        return result

    with mock.patch.object(module.os, "stat", side_effect=lambda path, *a, **kw: metadata(original_stat, path, *a, **kw)), \
            mock.patch.object(module.os, "lstat", side_effect=lambda path, *a, **kw: metadata(original_lstat, path, *a, **kw)):
        yield


def _ram_code(module, action, expected, message):
    try:
        action()
    except module.RamError as error:
        equal(error.code, expected, message, observed_error=str(error))
    else:
        check(False, message, expected_code=expected)


def _assert_ram_special_release(module):
    with _gate_ram_workspace(module) as (ws, host):
        outside = host / "sentinel.txt"
        outside.write_text("untouched", encoding="ascii")
        before = outside.stat().st_mtime_ns
        node = ws.path("bound.sock")
        ws.reserve(37)
        with _gate_socket_metadata(module, node):
            check(stat.S_ISSOCK(module.os.lstat(node).st_mode), "lstat sees the socket itself")
            check(stat.S_ISSOCK(module.os.stat(node).st_mode), "stat preserves socket type")
            check(not node.is_file() and not node.is_dir() and not node.is_symlink(),
                  "special socket must not pass a regular-file-only unlink predicate")
            equal(ws.release("bound.sock"), 0, "zero-block special node has no byte refund")
            check(not os.path.lexists(node), "release must unlink the socket pathname")
            equal((ws.used, ws.peak, ws._released), (37, 37, 0),
                  "zero-block unlink cannot manufacture accounting credit")
            equal(ws.release("bound.sock"), 0, "second release is idempotent")
            equal((ws.used, ws.peak, ws._released), (37, 37, 0),
                  "second release cannot refund bytes again")
        _ram_code(module, lambda: ws.release(str(outside)), "E_RAM_ESCAPE",
                  "outside sibling path must remain fenced")
        _ram_code(module, lambda: ws.release(str(ws.root)), "E_RAM_ESCAPE",
                  "release cannot remove the workspace root itself")
        equal(outside.read_text(encoding="ascii"), "untouched", "outside sentinel content retained")
        equal(outside.stat().st_mtime_ns, before, "outside sentinel timestamp retained")


def _assert_ram_reserve_boundary(module):
    with _gate_ram_workspace(module, quota=64) as (ws, _host):
        ws.reserve(63)
        equal((ws.used, ws.peak), (63, 63), "initial reservation accounting")
        try:
            ws.reserve(1)
        except module.RamError as error:
            check(False, "exact quota equality must be accepted", code=error.code)
        equal((ws.used, ws.peak), (64, 64), "exact quota reservation")
        _ram_code(module, lambda: ws.reserve(1), "E_RAM_QUOTA",
                  "one byte above quota must be refused")
        equal((ws.used, ws.peak), (64, 64), "refused reservation is atomic")
        ws.reserve(0)
        equal((ws.used, ws.peak), (64, 64), "zero reservation at exact quota is harmless")
        for invalid in (-1, True, False, 1.0, "1", None):
            _ram_code(module, lambda: ws.reserve(invalid), "E_RAM_QUOTA",
                      "invalid reserve amount must be refused")
            equal((ws.used, ws.peak), (64, 64), "invalid reservation preserves accounting",
                  invalid_amount=repr(invalid))


def _gate_xwd_ram_cases():
    return (
        (Mutation("XWD-fstat-exact-budget", "vadapt.xfb", "_bounded_stat",
                  "st.st_size > max_bytes", ("ops", 0), ast.GtE()), _assert_xwd_read_bounds),
        (Mutation("XWD-mutable-snapshot-copy", "vadapt.xfb", "consistent_read.snapshot",
                  "raw if isinstance(raw, bytes) else bytes(raw)", (), ast.Name(id="raw", ctx=ast.Load())),
         _assert_xwd_snapshot_copy),
        (Mutation("XWD-two-read-minimum", "vadapt.xfb", "_check_read_limits",
                  "type(tries) is not int or tries < 2", ("values", 1, "comparators", 0), ast.Constant(1)),
         _assert_xwd_retry_limits),
        (Mutation("RAM-special-node-unlink", "vadapt.ramws", "RamWorkspace.release",
                  "os.unlink(tgt)", (), ast.Constant(None)), _assert_ram_special_release),
        (Mutation("RAM-exact-quota", "vadapt.ramws", "RamWorkspace.reserve",
                  "self.used + n > self.quota", ("ops", 0), ast.GtE()), _assert_ram_reserve_boundary),
    )


class GateXwdRamTests(unittest.TestCase):
    def _verify_case(self, index):
        spec, assertion = _gate_xwd_ram_cases()[index]
        result = evaluate_mutation(spec, assertion)
        equal(result.get("anchor_count"), 1, "unique qualified source anchor", case=spec.name, result=result)
        check(result.get("positive") is True, "real assertion passes first", case=spec.name, result=result)
        equal(result.get("status"), "killed", "only the mutant behavioral assertion fails", case=spec.name, result=result)
        check(result.get("clean_teardown") is True, "mutation teardown must be clean", result=result)

    def test_xwd_fstat_exact_budget(self):
        self._verify_case(0)

    def test_xwd_mutable_snapshot_copy(self):
        self._verify_case(1)

    def test_xwd_two_read_minimum(self):
        self._verify_case(2)

    def test_ram_special_node_unlink(self):
        self._verify_case(3)

    def test_ram_exact_quota(self):
        self._verify_case(4)

    def test_xwd_protected_color_table_diagnostic(self):
        import vadapt.xfb as xfb
        diagnostic = _xwd_protected_color_table_diagnostic(xfb)
        equal(diagnostic["status"], "BLOCKED", "preserved external parser defect requires re-audit if changed",
              diagnostic=diagnostic)


# Embedded original-method verification and RAM-only ring source seam.
import contextlib
import contextvars
import hashlib
import importlib
import os
import pathlib
import sys
import types
import unittest
from unittest import mock


class _RamMapping:
    """Buffer-protocol view over a RAM array, with independent reader lifetime."""

    def __init__(self, data, write):
        view = memoryview(data)
        self.view = view if write else view.toreadonly()
        self.closed = False

    def __buffer__(self, flags):
        return self.view

    def close(self):
        if not self.closed:
            self.view.release()
            self.closed = True


@contextlib.contextmanager
def _memory_ring(module, legacy=None):
    """Keep real ring constructors/protocol; replace only file allocation/map I/O.

    The legacy reader tests are run by their exact IDs. Their workspace and
    temporary-path operations are redirected to these arrays, so no frame file
    exists. Real readonly buffer views preserve the reader's buffer contract.
    This is a Python protocol model, not an ARM memory-fence measurement.
    """
    buffers, descriptors, mappings = {}, {}, []
    serial = [100]

    def open_file(path, flags, mode=0o600):
        path = str(path)
        if flags & os.O_CREAT:
            if path in buffers:
                raise FileExistsError(path)
            buffers[path] = bytearray()
        if path not in buffers:
            raise FileNotFoundError(path)
        serial[0] += 1
        descriptors[serial[0]] = path
        return serial[0]

    def map_file(fd, size, write):
        data = buffers[descriptors[fd]]
        if len(data) != size:
            raise ValueError("mapping size differs from the allocated RAM array")
        mapped = _RamMapping(data, write)
        mappings.append(mapped)
        return mapped, "RAM array"

    def truncate(fd, size):
        buffers[descriptors[fd]][:] = bytes(size)

    facade = types.SimpleNamespace(**{key: getattr(os, key) for key in dir(os)})
    facade.open = open_file
    facade.close = lambda fd: descriptors.pop(fd)
    facade.ftruncate = truncate
    facade.fstat = lambda fd: types.SimpleNamespace(
        st_size=len(buffers[descriptors[fd]])
    )

    @contextlib.contextmanager
    def temporary(**kwargs):
        serial[0] += 1
        yield str(pathlib.Path(module.ramws.ROOT) / ("jq3_memory_%d" % serial[0]))

    class Workspace:
        def __init__(self):
            self.root = pathlib.Path(module.ramws.ROOT) / "jq3_gate_ring"
            self.reserved = 0

        def path(self, rel):
            if pathlib.PurePath(rel).is_absolute() or ".." in pathlib.PurePath(rel).parts:
                raise module.ramws.RamError("E_RAM_ESCAPE", "invalid fixture path")
            return self.root / rel

        def reserve(self, count):
            if type(count) is not int or not 0 <= count <= 65536:
                raise module.ramws.RamError("E_RAM_QUOTA", "RAM fixture cap")
            self.reserved += count

        def release(self, path):
            buffers.pop(str(path), None)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(module, "os", facade))
        stack.enter_context(mock.patch.object(module, "_map", map_file))
        if legacy is not None:
            stack.enter_context(mock.patch.object(
                legacy, "BASE_TEST_DIR", types.SimpleNamespace(mkdir=lambda **kw: None)
            ))
            stack.enter_context(mock.patch.object(
                legacy, "tempfile", types.SimpleNamespace(TemporaryDirectory=temporary)
            ))
            stack.enter_context(mock.patch.object(
                legacy, "shutil", types.SimpleNamespace(rmtree=lambda *a, **kw: None)
            ))
            stack.enter_context(mock.patch.object(
                legacy.WS, "path", lambda self, rel: self.root / rel
            ))
            stack.enter_context(mock.patch.object(
                legacy.WS, "release", lambda self, path: buffers.pop(str(path), None)
            ))
        try:
            yield Workspace
        finally:
            # Also clean failed mutant assertions which interrupt legacy bodies
            # before their explicit close calls. Cleanup cannot count as a kill.
            for mapped in mappings:
                mapped.close()
            leaked_descriptors = tuple(descriptors)
            descriptors.clear()
            buffers.clear()
            if leaked_descriptors:
                raise RuntimeError("fixture descriptor leak: %r" % (leaked_descriptors,))
            if not all(mapped.closed for mapped in mappings):
                raise RuntimeError("fixture mapping leak")


@contextlib.contextmanager
def _original_environment(module):
    original_bytecode_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    ramws = importlib.import_module("vadapt.ramws")
    original_open = dict(ramws._OPEN)
    original_stat, original_lstat = os.stat, os.lstat
    try:
        with contextlib.ExitStack() as stack:
            # Admission and all workspace methods remain real. Process-wide
            # atexit/signal installation is unrelated to these local assertions.
            stack.enter_context(mock.patch.object(ramws, "_install_handlers", lambda: None))
            if module.__name__.endswith("rtring_seqlock_reorder_and_wrap"):
                stack.enter_context(_memory_ring(module.rtring, module))
            yield
    finally:
        sys.dont_write_bytecode = original_bytecode_flag
        if ramws._OPEN != original_open:
            for workspace in set(ramws._OPEN) - set(original_open):
                workspace.close()
            raise RuntimeError("original method leaked a RAM workspace")
        if os.stat is not original_stat or os.lstat is not original_lstat:
            raise RuntimeError("original method leaked stat/lstat patches")


def _flatten_suite(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten_suite(item)
        else:
            yield item


def _source_method_digest(module, class_name, method_name):
    source = pathlib.Path(module.__file__).read_bytes()
    tree = ast.parse(source)
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == method_name)
    start = min([method.lineno] + [deco.lineno for deco in method.decorator_list])
    return hashlib.sha256(b"".join(source.splitlines(keepends=True)[start - 1:method.end_lineno])).hexdigest()


class GateOriginalMethodsTests(unittest.TestCase):
    def test_inventory_is_explicit_and_complete(self):
        discovered = []
        for module_name in sorted({name.rsplit(".", 2)[0] for name in ORIGINAL_METHOD_IDS}):
            module = importlib.import_module(module_name)
            discovered.extend(case.id() for case in _flatten_suite(
                unittest.TestLoader().loadTestsFromModule(module)
            ))
        self.assertEqual(sorted(discovered), sorted(ORIGINAL_METHOD_IDS))
        self.assertEqual(len(discovered), 37)
        self.assertEqual(set(VERIFIED_ORIGINAL_IDS) | set(BLOCKED_ORIGINALS), set(discovered))
        self.assertFalse(set(VERIFIED_ORIGINAL_IDS) & set(BLOCKED_ORIGINALS))

    def test_unresolved_originals_keep_their_assertions_and_pins(self):
        for test_id, evidence in BLOCKED_ORIGINALS.items():
            module_name, class_name, method_name = test_id.rsplit(".", 2)
            module = importlib.import_module(module_name)
            method = getattr(getattr(module, class_name), method_name)
            self.assertTrue(getattr(method, "__unittest_expecting_failure__", False), test_id)
            self.assertEqual(_source_method_digest(module, class_name, method_name),
                             evidence["original_method_sha256"], test_id)
            self.assertIn("BLOCKED", evidence["diagnostic"])


def _make_original_test(test_id):
    def verify(self):
        module_name, class_name, method_name = test_id.rsplit(".", 2)
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        method = getattr(cls, method_name)
        source_tree = ast.parse(pathlib.Path(module.__file__).read_bytes())
        source_class = next(node for node in source_tree.body
                            if isinstance(node, ast.ClassDef) and node.name == class_name)
        source_method = next(node for node in source_class.body
                             if isinstance(node, ast.FunctionDef) and node.name == method_name)
        self.assertEqual(source_class.decorator_list, [], test_id)
        self.assertEqual(source_method.decorator_list, [], test_id)
        for owner in (*cls.__mro__, method):
            self.assertFalse(getattr(owner, "__unittest_expecting_failure__", False), test_id)
            self.assertFalse(getattr(owner, "__unittest_skip__", False), test_id)
        suite = unittest.TestLoader().loadTestsFromName(test_id)
        cases = tuple(_flatten_suite(suite))
        self.assertEqual([case.id() for case in cases], [test_id])
        result = unittest.TestResult()
        with _original_environment(module):
            contextvars.copy_context().run(suite.run, result)
        self.assertEqual(result.testsRun, 1, test_id)
        for category in ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses"):
            self.assertEqual(getattr(result, category), [], "%s: %s" % (test_id, category))
    verify.__doc__ = "Run exact original ID: " + test_id
    return verify


def _ring_reconfirm_assertion(module):
    for query in (0, 1):
        with _memory_ring(module) as Workspace:
            writer = module.RingWriter(Workspace(), 2, 8, 8)
            if query == 0:
                writer.publish(np.full((8, 8, 3), 10, np.uint8), 0)
                writer._begin(2)
            else:
                writer._begin(1)
            reader = module.RingReader(writer.path)
            scalar = module._Q
            offset = module.HDR + query * module.SLOT_HDR
            count = [0]

            class ScalarReads:
                def unpack_from(self, buf, at=0):
                    value = scalar.unpack_from(buf, at)
                    if at == offset:
                        count[0] += 1
                        if count[0] <= 2:
                            return (0,)
                    elif query == 1 and at == offset + 8 and count[0] == 1:
                        return (0,)
                    return value

                def __getattr__(self, name):
                    return getattr(scalar, name)

            try:
                with mock.patch.object(module, "_Q", ScalarReads()):
                    frame, status = reader._try(query, retries=4)
                check(count[0] > 2, "stale scalar sequence pair must be retried", query=query)
                equal(status, "torn", "in-flight tuple cannot be classified stable", query=query)
                check(frame is None, "torn slot cannot yield a frame", query=query)
            finally:
                reader.close()
                writer.close()
    # Healthy different-index controls differ only in coherent committed state.
    with _memory_ring(module) as Workspace:
        with module.RingWriter(Workspace(), 2, 8, 8) as writer:
            writer.publish(np.full((8, 8, 3), 10, np.uint8), 0)
            reader = module.RingReader(writer.path)
            try:
                frame, status = reader._try(1, retries=4)
                equal((frame, status), (None, "absent"), "stable unpublished index")
                writer.publish(np.full((8, 8, 3), 12, np.uint8), 2)
                frame, status = reader._try(0, retries=4)
                equal((frame, status), (None, "lapped"), "stable overwritten index")
            finally:
                reader.close()


def _ring_wrap_assertion(module):
    with _memory_ring(module) as Workspace:
        writer = module.RingWriter(Workspace(), 2, 2, 2, _seq0=(1 << 64) - 2)
        reader = module.RingReader(writer.path)
        frame = None
        try:
            pixels = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
            writer.publish(pixels, 0)
            frame, status = reader._try(0, retries=3)
            equal(status, None, "64-bit sequence wrap remains a committed even state")
            check(frame is not None, "wrapped commit must produce a frame")
            equal(frame.render_index, 0, "frame identity survives sequence wrap")
            equal(reader._seq(0), 0, "max-even + 2 wraps to zero")
            array_equal(frame.view, pixels, "wrapped frame payload")
            check(not frame.view.flags.writeable, "reader view remains readonly")
            # Changing only the sequence to odd must refuse the same payload.
            module._Q.pack_into(writer._mm, module.HDR, 1)
            bad_frame, bad_status = reader._try(0, retries=3)
            equal((bad_frame, bad_status), (None, "torn"), "odd state is not committed")
        finally:
            frame = None
            reader.close()
            writer.close()


class GateRingTests(unittest.TestCase):
    def test_scalar_reconfirmation_mutant(self):
        spec = Mutation(
            "M-rtring-stale-scalar-tuple", "vadapt.rtring", "RingReader._try",
            "_QQ.unpack_from(self._mm, off) != (s1, ri)",
            ("ops", 0), ast.Eq(),
        )
        result = evaluate_mutation(spec, _ring_reconfirm_assertion)
        self.assertTrue(result["positive"], result)
        self.assertEqual(result["status"], "killed", result)
        self.assertTrue(result["clean_teardown"], result)

    def test_sequence_wrap_commit_mutant(self):
        spec = Mutation(
            "M-rtring-even-commit-wrap", "vadapt.rtring", "RingWriter._commit",
            "(s + 2) & MASK", ("left", "right"), ast.Constant(value=1),
        )
        result = evaluate_mutation(spec, _ring_wrap_assertion)
        self.assertTrue(result["positive"], result)
        self.assertEqual(result["status"], "killed", result)
        self.assertTrue(result["clean_teardown"], result)


ORIGINAL_METHOD_IDS = ('tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_clock_span_and_separation_sweep',
 'tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_competing_motions_capped_tile_support_vetoes',
 'tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_sequential_state_leak_independence',
 'tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_top_and_bottom_chrome_clock_symmetry',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_equal_sum_impostor_glyphs_no_false_correspondence',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_internal_selection_check_located_and_contract_documented',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_production_ast_no_floats_or_true_division',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_repeated_identical_glyphs_competing_modes_veto_refusal',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_symmetric_gradient_read_only_and_strided_views',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fixture_preconditions_and_eager_hashes',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_independent_tracker_slot_equivalence',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_reset_isolation_on_slot_replacement',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_simultaneous_multi_stream_vector_tracking',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_slot_permutation_invariance',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_chain_execution_preconditions_and_immutability',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_chain_veto_desired_demotion_contract',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_accounting_invariants_and_format_equivalence',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_accounting_reordering_determinism',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_cause_bucket_other_accounting',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_per_kind_records_and_truth_class',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_summary_exact_fp_totals',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_combined_callback_overwrite_post_use_rejection_recovery_and_crc_corruption',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_f02_stale_second_seq_load_reordering_absent',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_f02_stale_second_seq_load_reordering_lapped',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_recovery_from_finite_stale_observation_prefix_followed_by_coherent_commit',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_slot_reuse_across_ring_laps_and_exact_accounting',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_stable_different_render_index_controls',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_keep_frames_capped_sorted_prefix_and_inert_metadata',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_quota_refusal_release_and_successful_retry',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_release_bound_socket_node_removal_defect',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_release_mixed_special_node_directory_and_sentinel_safety',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_sweep_kept_expiry_equality_and_immediately_afterward',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_header_size_and_ncolors_arithmetic_overflow',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_invalid_byte_order_rejection',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_max_bytes_preallocation_budget_bounds',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_read_screen_refuses_over_budget_fstat_before_mmap',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_retry_count_tries_rejected_inputs')

BLOCKED_ORIGINALS = {'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_symmetric_gradient_read_only_and_strided_views': {'diagnostic': 'BLOCKED: '
                                                                                                                                                                          'original '
                                                                                                                                                                          'fixture '
                                                                                                                                                                          'broadcasts '
                                                                                                                                                                          '64x64 '
                                                                                                                                                                          'into '
                                                                                                                                                                          '60x60; '
                                                                                                                                                                          'fixing '
                                                                                                                                                                          'only '
                                                                                                                                                                          'clipping '
                                                                                                                                                                          'exposes '
                                                                                                                                                                          'None '
                                                                                                                                                                          'instead '
                                                                                                                                                                          'of '
                                                                                                                                                                          'demanded '
                                                                                                                                                                          '(4,4) '
                                                                                                                                                                          'from '
                                                                                                                                                                          'gm_constellation. '
                                                                                                                                                                          'Original '
                                                                                                                                                                          'assertion '
                                                                                                                                                                          'and '
                                                                                                                                                                          'marker '
                                                                                                                                                                          'retained '
                                                                                                                                                                          'for '
                                                                                                                                                                          'external '
                                                                                                                                                                          'owner '
                                                                                                                                                                          'fx2-f5-gm-bands-hv-v21-and-constellation.',
                                                                                                                                                            'original_method_sha256': '5eac4072252a74fb8ec29ecd162ba171bcae144456efb8347ec361c8bcc1a9f3'},
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_header_size_and_ncolors_arithmetic_overflow': {'diagnostic': 'BLOCKED: '
                                                                                                                                                                  'vadapt.xfb.parse_header '
                                                                                                                                                                  'lacks '
                                                                                                                                                                  'color-table/header-offset '
                                                                                                                                                                  'budget '
                                                                                                                                                                  'validation; '
                                                                                                                                                                  'production '
                                                                                                                                                                  'owner '
                                                                                                                                                                  'fx2-f8-d11-x11-framebuffer-ram-capture.',
                                                                                                                                                    'original_method_sha256': 'd15dd90a670e9dadb72439c63ddc69a12a995b3df39689fb259f0a9ec0afe27e'}}

VERIFIED_ORIGINAL_IDS = ('tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_clock_span_and_separation_sweep',
 'tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_competing_motions_capped_tile_support_vetoes',
 'tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_sequential_state_leak_independence',
 'tests.test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.TestChromeClockAndCompetingMotions.test_top_and_bottom_chrome_clock_symmetry',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_equal_sum_impostor_glyphs_no_false_correspondence',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_internal_selection_check_located_and_contract_documented',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_production_ast_no_floats_or_true_division',
 'tests.test_fx2_f5_j3_gm_constellation_voter_adversarial.TestFx2F5J3GmConstellationVoterAdversarial.test_repeated_identical_glyphs_competing_modes_veto_refusal',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fixture_preconditions_and_eager_hashes',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_independent_tracker_slot_equivalence',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_reset_isolation_on_slot_replacement',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_simultaneous_multi_stream_vector_tracking',
 'tests.test_fx2_f6_j1_fl_v3_unit_scenes_s1_s2_s3.TestFLV3UnitScenesS1S2S3.test_fl_v3_slot_permutation_invariance',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_chain_execution_preconditions_and_immutability',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_chain_veto_desired_demotion_contract',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_accounting_invariants_and_format_equivalence',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_accounting_reordering_determinism',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_cause_bucket_other_accounting',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_per_kind_records_and_truth_class',
 'tests.test_fx2_f6_j4_chain_veto_and_probe_accounting.TestChainVetoAndProbeAccounting.test_probe_tp_exclusion_summary_exact_fp_totals',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_combined_callback_overwrite_post_use_rejection_recovery_and_crc_corruption',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_f02_stale_second_seq_load_reordering_absent',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_f02_stale_second_seq_load_reordering_lapped',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_recovery_from_finite_stale_observation_prefix_followed_by_coherent_commit',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_slot_reuse_across_ring_laps_and_exact_accounting',
 'tests.test_fx2_f7_j3_rtring_seqlock_reorder_and_wrap.TestFx2F7J3RtringSeqlockReorderAndWrap.test_stable_different_render_index_controls',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_keep_frames_capped_sorted_prefix_and_inert_metadata',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_quota_refusal_release_and_successful_retry',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_release_bound_socket_node_removal_defect',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_release_mixed_special_node_directory_and_sentinel_safety',
 'tests.test_fx2_f7_j4_ramws_special_nodes_and_quota_sweep.TestRamwsSpecialNodesAndQuotaSweep.test_sweep_kept_expiry_equality_and_immediately_afterward',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_invalid_byte_order_rejection',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_max_bytes_preallocation_budget_bounds',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_read_screen_refuses_over_budget_fstat_before_mmap',
 'tests.test_fx2_f8_j1_xwd_header_and_byte_order_parser.TestXwdHeaderAndByteOrderParserContract.test_retry_count_tries_rejected_inputs')

for _index, _test_id in enumerate(VERIFIED_ORIGINAL_IDS):
    setattr(GateOriginalMethodsTests, "test_original_%02d_%s" % (_index, _test_id.rsplit(".", 1)[1]), _make_original_test(_test_id))


def _linear_gradient_diagnostic():
    """Reproduce the protected recovery gap after changing only fixture clipping."""
    module = importlib.import_module("eval.v2x.gm_constellation")
    y, x = np.ogrid[:64, :64]
    gradient = (255 - np.minimum(255, np.abs(y - 32) * 4 + np.abs(x - 32) * 4)).astype(np.uint8)
    prev_storage = np.full((128, 128), 20, dtype=np.uint8)
    cur_storage = np.full((128, 128), 20, dtype=np.uint8)
    prev_storage[::2, ::2] = gradient
    # This is the sole repair needed to get beyond the original broadcasting
    # exception. The source scene and demanded (4,4) recovery remain unchanged.
    cur_storage[8:128:2, 8:128:2] = gradient[:60, :60]
    prev, cur = prev_storage[::2, ::2], cur_storage[::2, ::2]
    prev.flags.writeable = cur.flags.writeable = False
    before = prev.tobytes(), cur.tobytes()
    actual = module.estimate_global_motion(prev, cur, max_d=16)
    equal((prev.tobytes(), cur.tobytes()), before, "blocked scene must remain read-only")
    return {
        "status": "BLOCKED" if actual is None else "NEEDS_REAUDIT",
        "owner": "fx2-f5-gm-bands-hv-v21-and-constellation",
        "scope": "gm_constellation.estimate_global_motion",
        "observed": actual,
        "expected_original_vector": (4, 4),
        "fixture_repair": "crop shifted source to the 60x60 destination only",
        "accepted_as_repair": False,
    }


NAMED_CONTRASTS = frozenset({
    "M-eval-v2x-gm-bands-hv-1", "M-eval-v2x-gm-bands-hv-2",
    "M-constellation-selection-cell-cap", "FL-minimum-half-run",
    "chain-first-claim-wins", "baseline-only-flicker-veto",
    "probe-true-positive-exclusion", "XWD-fstat-exact-budget",
    "XWD-mutable-snapshot-copy", "XWD-two-read-minimum",
    "RAM-special-node-unlink", "RAM-exact-quota",
    "M-rtring-stale-scalar-tuple", "M-rtring-even-commit-wrap",
})


def _count_valid_kills(results):
    return sum(result.get("positive") is True
               and result.get("status") == "killed"
               and result.get("anchor_count") == 1
               and result.get("clean_teardown") is True
               for result in results)


class GateZSummaryTests(unittest.TestCase):
    def test_preserved_gradient_is_an_external_blocker(self):
        diagnostic = contextvars.copy_context().run(_linear_gradient_diagnostic)
        self.assertEqual(diagnostic["status"], "BLOCKED", diagnostic)
        self.assertFalse(diagnostic["accepted_as_repair"])

    def test_failed_positives_and_bad_teardown_never_count(self):
        good = {"positive": True, "status": "killed", "anchor_count": 1,
                "clean_teardown": True}
        controls = [dict(good, positive=False), dict(good, clean_teardown=False),
                    dict(good, anchor_count=0), dict(good, anchor_count=2),
                    dict(good, status="survived"), dict(good, status="equivalent"),
                    dict(good, status="import_error"), dict(good, status="mutant_error"),
                    dict(good, status="timeout")]
        self.assertEqual(_count_valid_kills([good]), 1)
        self.assertEqual(_count_valid_kills(controls), 0)

    def test_named_contrasts_all_have_real_assertions_and_clean_kills(self):
        by_name = {result["name"]: result for result in MUTATION_RESULTS
                   if result["name"] in NAMED_CONTRASTS}
        self.assertEqual(set(by_name), NAMED_CONTRASTS)
        self.assertEqual(_count_valid_kills(by_name.values()), 14, by_name)
        for result in by_name.values():
            self.assertEqual(result["error_type"], "BehavioralAssertion", result)
            self.assertEqual(result["phase"], "mutant_assertion", result)


if __name__ == "__main__":
    unittest.main()
