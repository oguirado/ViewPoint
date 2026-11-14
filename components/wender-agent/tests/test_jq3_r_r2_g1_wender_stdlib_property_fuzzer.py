"""Tests for wender.learn.property_fuzzer and in-memory AST mutation prove harness."""
import ast
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys
import threading
import time
import types
import unittest

import numpy as np

from wender.act import coordinates
from wender.learn import property_fuzzer as pf
from wender.state import schema

ROOT = Path(__file__).resolve().parents[1]

# Dispositions for the in-memory AST prove harness (none except KILL is a kill)
DISP_KILL = "KILL"
DISP_SURVIVOR = "SURVIVOR"
DISP_FAILED_CONTROL = "FAILED_CONTROL"
DISP_MISSING_ANCHOR = "MISSING_ANCHOR"
DISP_COMPILE_OR_IMPORT_ERROR = "COMPILE_OR_IMPORT_ERROR"
DISP_TIMEOUT = "TIMEOUT"
DISP_PROVEN_EQUIVALENT = "PROVEN_EQUIVALENT"


class AnchorResolutionError(LookupError):
    """Raised when a mutation target anchor or structural selector is missing or ambiguous."""


@dataclass(frozen=True)
class MutationSpec:
    """Catalogue or structural AST mutation specification."""
    mutation_id: str
    rel_path: str
    lineno: int
    qualname: str
    operator: str
    node_text: str
    selector: str = "root"
    catalogue_node: str | None = None
    equivalence_proof: str | None = None


@dataclass(frozen=True)
class MutationProofResult:
    """Outcome of running a control + mutated assertion in the in-memory AST prove harness."""
    mutation_id: str
    disposition: str
    control_passed: bool
    mutated_source_segment: str | None
    detail: str
    elapsed_vm_ms: int


def _normalize_verbalized_anchor(text):
    out = text
    replacements = (
        (" left square bracket ", "["),
        ("left square bracket ", "["),
        (" left square bracket", "["),
        (" right square bracket ", "]"),
        ("right square bracket ", "]"),
        (" right square bracket", "]"),
        (" , ", ", "),
        (" is at most ", " <= "),
        (" is at least ", " >= "),
    )
    for old, new in replacements:
        out = out.replace(old, new)
    return out


_COMPARE_O_MAP = {
    ast.Lt: ast.Gt,
    ast.Gt: ast.Lt,
    ast.LtE: ast.GtE,
    ast.GtE: ast.LtE,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

_BOOLOP_O_MAP = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}

_BINOP_O_MAP = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}

_COMPARE_B_MAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}


def _collect_qualname_ranges(tree):
    """Map qualified function/method names to AST FunctionDef/AsyncFunctionDef nodes."""
    found = {}

    def _visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{prefix}.{child.name}" if prefix else child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.setdefault(qname, []).append(child)
                _visit(child, qname)
            else:
                _visit(child, prefix)

    _visit(tree, "")
    return found


def _parse_ops_selector(selector):
    """Parse 'ops[i]' or 'ops element i' into integer index i, or return None."""
    if not isinstance(selector, str):
        return None
    s = selector.strip()
    if s.startswith("ops[") and s.endswith("]"):
        idx_str = s[4:-1].strip()
        if idx_str.isdigit():
            return int(idx_str)
    if s.startswith("ops element "):
        idx_str = s[len("ops element "):].strip()
        if idx_str.isdigit():
            return int(idx_str)
    return None


def _contains_raise(stmts):
    for stmt in stmts:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                return True
    return False


def _apply_mutation_to_node(target, operator, selector):
    """Mutate `target` in place according to operator (O, B, D, S) and selector."""
    if operator == "O":
        if isinstance(target, ast.BoolOp):
            if selector not in ("root", "op", "boolop"):
                raise AnchorResolutionError(f"invalid selector {selector!r} for BoolOp O")
            op_cls = _BOOLOP_O_MAP.get(type(target.op))
            if op_cls is None:
                raise AnchorResolutionError(f"unsupported BoolOp {type(target.op).__name__}")
            target.op = op_cls()
            return target
        if isinstance(target, ast.Compare):
            op_idx = _parse_ops_selector(selector)
            if op_idx is None:
                if selector in ("root", "op", "compare") and len(target.ops) == 1:
                    op_idx = 0
                else:
                    raise AnchorResolutionError(f"Compare O requires unambiguous ops selector, got {selector!r}")
            if op_idx < 0 or op_idx >= len(target.ops):
                raise AnchorResolutionError(f"Compare ops index {op_idx} out of range")
            op_cls = _COMPARE_O_MAP.get(type(target.ops[op_idx]))
            if op_cls is None:
                raise AnchorResolutionError(f"unsupported Compare op {type(target.ops[op_idx]).__name__}")
            target.ops[op_idx] = op_cls()
            return target
        if isinstance(target, ast.BinOp):
            if selector not in ("root", "op", "binop"):
                raise AnchorResolutionError(f"invalid selector {selector!r} for BinOp O")
            op_cls = _BINOP_O_MAP.get(type(target.op))
            if op_cls is None:
                raise AnchorResolutionError(f"unsupported BinOp {type(target.op).__name__}")
            target.op = op_cls()
            return target
        raise AnchorResolutionError(f"operator O unsupported on AST node {type(target).__name__}")

    if operator == "B":
        # Never substitute a constant increment for a comparison selector or vice versa.
        op_idx = _parse_ops_selector(selector)
        if op_idx is not None or selector == "comparison":
            if not isinstance(target, ast.Compare):
                raise AnchorResolutionError(
                    f"comparison selector {selector!r} cannot target {type(target).__name__}"
                )
            idx = 0 if op_idx is None else op_idx
            if len(target.ops) == 0 or idx < 0 or idx >= len(target.ops):
                raise AnchorResolutionError(f"Compare ops index {idx} out of range")
            op_cls = _COMPARE_B_MAP.get(type(target.ops[idx]))
            if op_cls is None:
                raise AnchorResolutionError(f"operator B unsupported on comparison op {type(target.ops[idx]).__name__}")
            target.ops[idx] = op_cls()
            return target
        if selector == "constant":
            if not (isinstance(target, ast.Constant) and type(target.value) is int):
                raise AnchorResolutionError(
                    f"constant selector requires exact int Constant, got {type(target).__name__}"
                )
            target.value = target.value + 1
            return target
        raise AnchorResolutionError(
            f"operator B requires explicit comparison ('ops[i]'/'ops element i') or 'constant' selector, got {selector!r}"
        )

    if operator == "D":
        if isinstance(target, ast.If):
            if selector not in ("root", "test", "if_test"):
                raise AnchorResolutionError(f"invalid selector {selector!r} for If D")
            if not _contains_raise(target.body):
                raise AnchorResolutionError("operator D on If requires a raising If body")
            target.test = ast.copy_location(ast.Constant(value=False), target.test)
            return target
        if isinstance(target, ast.Assert):
            if selector not in ("root", "test", "assert_test"):
                raise AnchorResolutionError(f"invalid selector {selector!r} for Assert D")
            target.test = ast.copy_location(ast.Constant(value=True), target.test)
            return target
        raise AnchorResolutionError(f"operator D requires raising If or Assert, got {type(target).__name__}")

    if operator == "S":
        if not isinstance(target, ast.Call):
            raise AnchorResolutionError(f"operator S requires Call node, got {type(target).__name__}")
        if selector not in ("root", "args", "call_args"):
            raise AnchorResolutionError(f"invalid selector {selector!r} for Call S")
        non_starred_indices = [
            i for i, arg in enumerate(target.args) if not isinstance(arg, ast.Starred)
        ]
        if len(non_starred_indices) < 2:
            raise AnchorResolutionError("Call S requires at least two non-starred positional arguments")
        first_idx = non_starred_indices[0]
        first_dump = ast.dump(target.args[first_idx])
        second_idx = None
        for cand_idx in non_starred_indices[1:]:
            if ast.dump(target.args[cand_idx]) != first_dump:
                second_idx = cand_idx
                break
        if second_idx is None:
            raise AnchorResolutionError("Call S requires two distinct non-starred positional arguments")
        target.args[first_idx], target.args[second_idx] = (
            target.args[second_idx],
            target.args[first_idx],
        )
        return target

    raise AnchorResolutionError(f"unknown mutation operator {operator!r}")


def build_mutated_ast(source_text, spec):
    """Locate the unique anchor in `source_text` matching `spec` and apply the mutation in memory."""
    tree = ast.parse(source_text, filename=spec.rel_path)
    qmap = _collect_qualname_ranges(tree)
    func_nodes = qmap.get(spec.qualname, [])
    if len(func_nodes) != 1:
        raise AnchorResolutionError(
            f"qualname {spec.qualname!r} resolved to {len(func_nodes)} functions in {spec.rel_path}"
        )
    func_node = func_nodes[0]

    expected_texts = {spec.node_text}
    if spec.catalogue_node:
        expected_texts.add(spec.catalogue_node)
        expected_texts.add(_normalize_verbalized_anchor(spec.catalogue_node))

    matches = []
    for node in ast.walk(func_node):
        if getattr(node, "lineno", None) != spec.lineno:
            continue
        seg = ast.get_source_segment(source_text, node)
        if seg in expected_texts:
            matches.append(node)

    if len(matches) != 1:
        raise AnchorResolutionError(
            f"anchor ({spec.rel_path}:{spec.lineno}, {spec.qualname}, {spec.node_text!r}) matched {len(matches)} nodes"
        )

    target = matches[0]
    _apply_mutation_to_node(target, spec.operator, spec.selector)
    ast.fix_missing_locations(tree)
    return tree, ast.unparse(target)


def prove_mutation(spec, assertion_fn, *, timeout_sec=2.0, source_override=None):
    """Execute the in-memory AST prove harness for `spec` with `assertion_fn(module)`.

    1. Runs `assertion_fn` against the real module first (fails with FAILED_CONTROL if it does not pass).
    2. Parses and mutates the AST in memory without writing files or bytecode.
    3. Executes the mutated module in an isolated private module registered in `sys.modules`
       during execution (so dataclass registration works), restoring `sys.modules` in `finally`.
    4. Classifies only a semantic `AssertionError` on the mutated module as `DISP_KILL`.
    """
    t0 = time.perf_counter()
    source_path = ROOT / spec.rel_path
    if not source_path.is_file():
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_MISSING_ANCHOR,
            control_passed=False,
            mutated_source_segment=None,
            detail=f"missing source file {spec.rel_path}",
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )

    dotted_mod = ".".join(Path(spec.rel_path).with_suffix("").parts)
    real_mod = __import__(dotted_mod, fromlist=["*"])

    # Step 1: Run control on real code first
    try:
        assertion_fn(real_mod)
    except BaseException as exc:
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_FAILED_CONTROL,
            control_passed=False,
            mutated_source_segment=None,
            detail=f"control failed on unmutated code: {type(exc).__name__}: {exc}",
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Step 2: Locate anchor and mutate AST
    raw_source = source_override if source_override is not None else source_path.read_text(encoding="utf-8")
    try:
        mutated_tree, mutated_segment = build_mutated_ast(raw_source, spec)
    except AnchorResolutionError as exc:
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_MISSING_ANCHOR,
            control_passed=True,
            mutated_source_segment=None,
            detail=str(exc),
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )
    except SyntaxError as exc:
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_COMPILE_OR_IMPORT_ERROR,
            control_passed=True,
            mutated_source_segment=None,
            detail=f"syntax error: {exc}",
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Step 3: Compile & exec in isolated in-memory module, restoring sys.modules in finally
    orig_dont_write_bytecode = sys.dont_write_bytecode
    orig_sys_modules = dict(sys.modules)
    private_mod_name = f"_ast_mutant_{spec.mutation_id.replace('-', '_')}"
    outcome = {}

    def _run_mutant():
        try:
            code_obj = compile(mutated_tree, filename=f"<in-memory:{spec.rel_path}>", mode="exec")
            mutant_mod = types.ModuleType(private_mod_name)
            mutant_mod.__file__ = str(source_path)
            mutant_mod.__package__ = dotted_mod.rpartition(".")[0]
            sys.modules[private_mod_name] = mutant_mod
            exec(code_obj, mutant_mod.__dict__)
        except BaseException as exc:
            outcome["phase"] = "compile_or_import"
            outcome["exc"] = exc
            return

        try:
            assertion_fn(mutant_mod)
            outcome["phase"] = "survived"
        except AssertionError as exc:
            outcome["phase"] = "killed"
            outcome["exc"] = exc
        except BaseException as exc:
            outcome["phase"] = "runtime_error"
            outcome["exc"] = exc

    try:
        sys.dont_write_bytecode = True
        worker = threading.Thread(target=_run_mutant, name=f"prove-{spec.mutation_id}", daemon=True)
        worker.start()
        worker.join(timeout=timeout_sec)
        if worker.is_alive():
            return MutationProofResult(
                mutation_id=spec.mutation_id,
                disposition=DISP_TIMEOUT,
                control_passed=True,
                mutated_source_segment=mutated_segment,
                detail=f"timed out after {timeout_sec}s",
                elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
            )
    finally:
        sys.modules.clear()
        sys.modules.update(orig_sys_modules)
        sys.dont_write_bytecode = orig_dont_write_bytecode

    phase = outcome.get("phase")
    if phase == "compile_or_import" or phase == "runtime_error":
        exc = outcome.get("exc")
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_COMPILE_OR_IMPORT_ERROR,
            control_passed=True,
            mutated_source_segment=mutated_segment,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )
    if phase == "killed":
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_KILL,
            control_passed=True,
            mutated_source_segment=mutated_segment,
            detail=str(outcome.get("exc")),
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )
    if spec.equivalence_proof:
        return MutationProofResult(
            mutation_id=spec.mutation_id,
            disposition=DISP_PROVEN_EQUIVALENT,
            control_passed=True,
            mutated_source_segment=mutated_segment,
            detail=spec.equivalence_proof,
            elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
        )
    return MutationProofResult(
        mutation_id=spec.mutation_id,
        disposition=DISP_SURVIVOR,
        control_passed=True,
        mutated_source_segment=mutated_segment,
        detail="assertion did not raise AssertionError on mutated module",
        elapsed_vm_ms=int((time.perf_counter() - t0) * 1000),
    )


CATALOGUE_MUTATIONS = (
    MutationSpec(
        mutation_id="M-wender-state-schema-1",
        rel_path="wender/state/schema.py",
        lineno=159,
        qualname="validate_state",
        operator="O",
        node_text='not isinstance(n["role"], str) or not isinstance(n["name"], str)',
        selector="op",
        catalogue_node=(
            'not isinstance(n left square bracket "role" right square bracket , str) '
            'or not isinstance(n left square bracket "name" right square bracket , str)'
        ),
    ),
    MutationSpec(
        mutation_id="M-wender-state-schema-2",
        rel_path="wender/state/schema.py",
        lineno=212,
        qualname="validate_target_descriptor",
        operator="B",
        node_text="dpr_ppm <= 0",
        selector="ops element 0",
        catalogue_node="dpr_ppm is at most 0",
    ),
)


class TestCatalogueAstMutationProver(unittest.TestCase):
    """Verify the in-memory AST prove harness and kill all catalogue obligations."""

    def _base_valid_state(self):
        tree = pf.SeedTree(101)
        return pf.generate_ui_tree(tree.case("ui_tree", 0), min_nodes=3, max_nodes=5)

    def test_kill_M_wender_state_schema_1_role_or_name_type_guard(self):
        spec = CATALOGUE_MUTATIONS[0]

        def check_role_and_name_independently(mod):
            valid = self._base_valid_state()
            self.assertEqual(mod.validate_state(valid), [])

            bad_role_only = deepcopy(valid)
            bad_role_only["nodes"][0]["role"] = 123
            errs_role = mod.validate_state(bad_role_only)
            assert any("role/name must be strings" in e for e in errs_role), (
                f"expected role/name error when role is int and name is str, got {errs_role!r}"
            )

            bad_name_only = deepcopy(valid)
            bad_name_only["nodes"][0]["name"] = 456
            errs_name = mod.validate_state(bad_name_only)
            assert any("role/name must be strings" in e for e in errs_name), (
                f"expected role/name error when role is str and name is int, got {errs_name!r}"
            )

        res = prove_mutation(spec, check_role_and_name_independently)
        self.assertEqual(res.disposition, DISP_KILL, res.detail)
        self.assertTrue(res.control_passed)
        self.assertIn("and", res.mutated_source_segment)

    def test_kill_M_wender_state_schema_2_dpr_ppm_zero_boundary(self):
        spec = CATALOGUE_MUTATIONS[1]

        def check_dpr_ppm_zero_boundary(mod):
            valid_state = self._base_valid_state()
            handle = mod.Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
            valid_env = mod.build_target_descriptor(handle, epoch=1, state=valid_state, frame_seq=0, dpr_ppm=1)
            assert mod.validate_target_descriptor(valid_env) == []

            zero_env = dict(valid_env, dpr_ppm=0)
            errs_zero = mod.validate_target_descriptor(zero_env)
            assert "dpr_ppm must be a positive int" in errs_zero, (
                f"expected dpr_ppm <= 0 boundary violation at dpr_ppm=0, got {errs_zero!r}"
            )

        res = prove_mutation(spec, check_dpr_ppm_zero_boundary)
        self.assertEqual(res.disposition, DISP_KILL, res.detail)
        self.assertTrue(res.control_passed)
        self.assertEqual(res.mutated_source_segment, "dpr_ppm < 0")

    def test_operators_O_B_D_S_on_coordinates_and_schema(self):
        # 1. Operator O on BinOp (Add -> Sub and FloorDiv -> Mult in box_center)
        spec_binop = MutationSpec(
            mutation_id="M-coord-box-center-add",
            rel_path="wender/act/coordinates.py",
            lineno=27,
            qualname="box_center",
            operator="O",
            node_text="x + w // 2",
            selector="op",
        )
        res_binop = prove_mutation(
            spec_binop,
            lambda mod: self.assertEqual(mod.box_center([10, 20, 8, 6]), (14, 23)),
        )
        self.assertEqual(res_binop.disposition, DISP_KILL)

        # 2. Operator B on exact integer Constant (+1 on 4 in len(box) != 4)
        spec_const = MutationSpec(
            mutation_id="M-coord-box-len-const",
            rel_path="wender/act/coordinates.py",
            lineno=22,
            qualname="box_center",
            operator="B",
            node_text="4",
            selector="constant",
        )

        def assert_box_len_four_accepted(mod):
            try:
                center = mod.box_center([0, 0, 4, 4])
            except mod.CoordRefused as exc:
                raise AssertionError(f"4-element box refused: {exc}") from exc
            assert center == (2, 2)

        res_const = prove_mutation(spec_const, assert_box_len_four_accepted)
        self.assertEqual(res_const.disposition, DISP_KILL)

        # 3. Verify B never substitutes constant increment for comparison selector
        spec_mismatch = MutationSpec(
            mutation_id="M-mismatch-selector",
            rel_path="wender/act/coordinates.py",
            lineno=22,
            qualname="box_center",
            operator="B",
            node_text="4",
            selector="ops[0]",
        )
        res_mismatch = prove_mutation(spec_mismatch, assert_box_len_four_accepted)
        self.assertEqual(res_mismatch.disposition, DISP_MISSING_ANCHOR)

        # 4. Operator D on raising If in css_to_device
        spec_disable_if = MutationSpec(
            mutation_id="M-coord-css-to-device-guard",
            rel_path="wender/act/coordinates.py",
            lineno=33,
            qualname="css_to_device",
            operator="D",
            node_text='if not (_is_int(x) and _is_int(y) and _is_int(dpr_ppm) and dpr_ppm > 0):\n        raise CoordRefused("invalid css_to_device parameters")',
            selector="if_test",
        )

        def assert_css_refuses_zero_dpr(mod):
            assert mod.css_to_device(10, 20, 1000000) == (10, 20)
            try:
                mod.css_to_device(10, 20, 0)
            except mod.CoordRefused:
                return
            raise AssertionError("expected CoordRefused for dpr_ppm=0")

        res_disable = prove_mutation(spec_disable_if, assert_css_refuses_zero_dpr)
        self.assertEqual(res_disable.disposition, DISP_KILL)

        # 5. Operator S swapping first two distinct non-starred positional Call args in _no_float
        spec_swap = MutationSpec(
            mutation_id="M-schema-no-float-swap",
            rel_path="wender/state/schema.py",
            lineno=51,
            qualname="_no_float",
            operator="S",
            node_text="isinstance(o, float)",
            selector="call_args",
        )
        res_swap = prove_mutation(
            spec_swap,
            lambda mod: self.assertEqual(mod.dumps({"a": 1}), '{"a":1}'),
        )
        self.assertEqual(res_swap.disposition, DISP_COMPILE_OR_IMPORT_ERROR)

    def test_separate_dispositions_for_failed_control_survivor_equivalent_and_timeout(self):
        spec = CATALOGUE_MUTATIONS[1]

        res_failed_ctrl = prove_mutation(
            spec,
            lambda mod: (_ for _ in ()).throw(AssertionError("deliberate control failure")),
        )
        self.assertEqual(res_failed_ctrl.disposition, DISP_FAILED_CONTROL)
        self.assertFalse(res_failed_ctrl.control_passed)

        res_survivor = prove_mutation(
            spec,
            lambda mod: self.assertEqual(mod.SCHEMA_ID, "wender.state/v1"),
        )
        self.assertEqual(res_survivor.disposition, DISP_SURVIVOR)

        eq_spec = MutationSpec(
            mutation_id="M-eq-demo",
            rel_path="wender/state/schema.py",
            lineno=212,
            qualname="validate_target_descriptor",
            operator="B",
            node_text="dpr_ppm <= 0",
            selector="ops[0]",
            equivalence_proof="Equivalent when inputs are restricted to dpr_ppm >= 1.",
        )
        res_eq = prove_mutation(
            eq_spec,
            lambda mod: self.assertEqual(mod.SCHEMA_ID, "wender.state/v1"),
        )
        self.assertEqual(res_eq.disposition, DISP_PROVEN_EQUIVALENT)

        def slow_assertion(mod):
            if mod.__name__.startswith("_ast_mutant_"):
                time.sleep(0.25)

        res_timeout = prove_mutation(spec, slow_assertion, timeout_sec=0.05)
        self.assertEqual(res_timeout.disposition, DISP_TIMEOUT)

    def test_canonical_session_registry_dataclasses_and_strict_constructors(self):
        import wender.perceive.session_registry as sr
        self.assertIs(schema.Handle, sr.Handle)
        self.assertIs(schema.FrameRef, sr.FrameRef)
        self.assertIs(schema.Observation, sr.Observation)

        h = schema.Handle("cdp_tab", "tab-strict", 1)
        bad_rgb = np.zeros((4, 4, 3), dtype=np.float32)
        with self.assertRaises(ValueError):
            schema.FrameRef.from_rgb(h, 0, 0, bad_rgb)

        good_rgb = np.zeros((4, 4, 3), dtype=np.uint8)
        fref = schema.FrameRef.from_rgb(h, 0, 0, good_rgb)
        self.assertFalse(fref.rgb.flags.writeable)
        st = self._base_valid_state()

        with self.assertRaises(ValueError):
            schema.Observation(handle=h, epoch=0, frame_seq=0, dpr_ppm=0, state=st, frame=fref)

        with self.assertRaises(ValueError):
            schema.Observation(handle=h, epoch=0, frame_seq=-1, dpr_ppm=1000000, state=st, frame=fref)


class TestSplitMix64AndPropertyFuzzer(unittest.TestCase):
    """Test SplitMix64 seed tree, 64-case domain loops, and 32-attempt hierarchical shrinker."""

    def test_splitmix64_determinism_and_seed_tree_stability(self):
        tree_a = pf.SeedTree(0x123456789ABCDEF0)
        tree_b = pf.SeedTree(0x123456789ABCDEF0)
        self.assertEqual(tree_a.stream_u64(8), tree_b.stream_u64(8))

        c1_first = tree_a.case("ui_tree", 17).seed
        _ = tree_a.case("bbox", 3)
        _ = tree_a.case("action_sequence", 42)
        c1_later = tree_b.case("ui_tree", 17).seed
        self.assertEqual(c1_first, c1_later)

        domain_seeds = {tree_a.domain(d).seed for d in pf.DOMAINS}
        self.assertEqual(len(domain_seeds), len(pf.DOMAINS))

        for bad_seed in (True, False, 1.5, -1):
            with self.assertRaises(pf.FuzzerError):
                pf.SeedTree(bad_seed)
        with self.assertRaises(pf.FuzzerError):
            pf.splitmix64_next(True)
        with self.assertRaises(pf.FuzzerError):
            pf.SeedTree(1).domain("invalid_domain")

    def test_all_four_domains_run_64_valid_cases(self):
        root_seed = 20261020
        res_ui = pf.run_domain_campaign(
            root_seed,
            "ui_tree",
            lambda st: (
                schema.validate_state(st) == []
                and len(hashlib.sha256(schema.dumps(st).encode("utf-8")).hexdigest()) == 64
            ),
            cases_per_domain=64,
        )
        self.assertEqual(res_ui["status"], "ok")
        self.assertEqual(res_ui["valid_cases"], 64)

        res_bbox = pf.run_domain_campaign(
            root_seed,
            "bbox",
            lambda box: (
                coordinates.box_center(box) == (box[0] + box[2] // 2, box[1] + box[3] // 2)
                and box[0] <= coordinates.box_center(box)[0] < box[0] + box[2]
                and box[1] <= coordinates.box_center(box)[1] < box[1] + box[3]
            ),
            cases_per_domain=64,
        )
        self.assertEqual(res_bbox["status"], "ok")
        self.assertEqual(res_bbox["valid_cases"], 64)

        res_dpr = pf.run_domain_campaign(
            root_seed,
            "dpr_scale",
            lambda case: (
                coordinates.css_to_device(case["x"], case["y"], case["dpr_ppm"])
                == ((case["x"] * case["dpr_ppm"]) // 1000000, (case["y"] * case["dpr_ppm"]) // 1000000)
            ),
            cases_per_domain=64,
        )
        self.assertEqual(res_dpr["status"], "ok")
        self.assertEqual(res_dpr["valid_cases"], 64)

        def verify_action_sequence(fixture):
            st = fixture["state"]
            for act in fixture["actions"]:
                op = act["op"]
                if op == "click":
                    ev = coordinates.plan_click(st, act["target"], irreversible=act["irreversible"])
                    if len(ev) != 3 or ev[0]["type"] != "mouseMoved":
                        return False
                elif op == "hover":
                    ev = coordinates.plan_hover(st, act["target"], irreversible=act["irreversible"])
                    if len(ev) != 1 or ev[0]["type"] != "mouseMoved":
                        return False
                elif op == "lpath_click":
                    ev = coordinates.plan_lpath_click(
                        st,
                        act["target"],
                        act["origin"],
                        irreversible=act["irreversible"],
                        waypoint_policy=act["waypoint_policy"],
                    )
                    if len(ev) < 3 or ev[-1]["type"] != "mouseReleased":
                        return False
                elif op == "type":
                    ev = coordinates.plan_type(act["text"])
                    if ev != [{"type": "insertText", "text": act["text"]}]:
                        return False
            return True

        res_act = pf.run_domain_campaign(
            root_seed,
            "action_sequence",
            verify_action_sequence,
            cases_per_domain=64,
        )
        self.assertEqual(res_act["status"], "ok")
        self.assertEqual(res_act["valid_cases"], 64)

    def test_hierarchical_shrinking_preserves_validity_and_failing_predicate(self):
        res_tree = pf.run_domain_campaign(
            4242,
            "ui_tree",
            lambda st: len(st["nodes"]) < 3,
            cases_per_domain=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(res_tree["status"], "counterexample")
        self.assertLessEqual(res_tree["shrink_attempts"], 32)
        self.assertGreater(res_tree["shrink_steps"], 0)
        min_tree = res_tree["minimal_fixture"]
        orig_tree = res_tree["original_fixture"]
        self.assertTrue(pf.is_valid_ui_tree(min_tree))
        self.assertGreaterEqual(len(min_tree["nodes"]), 3)
        self.assertLess(pf.fixture_measure("ui_tree", min_tree), pf.fixture_measure("ui_tree", orig_tree))
        self.assertTrue(res_tree["counterexample"].reproducible)

        res_bbox = pf.run_domain_campaign(
            4242,
            "bbox",
            lambda box: box[2] < 10,
            cases_per_domain=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(res_bbox["status"], "counterexample")
        self.assertLessEqual(res_bbox["shrink_attempts"], 32)
        min_box = res_bbox["minimal_fixture"]
        self.assertTrue(pf.is_valid_bbox(min_box))
        self.assertGreaterEqual(min_box[2], 10)
        self.assertLess(pf.fixture_measure("bbox", min_box), pf.fixture_measure("bbox", res_bbox["original_fixture"]))

        res_dpr = pf.run_domain_campaign(
            4242,
            "dpr_scale",
            lambda case: case["dpr_ppm"] < 500000,
            cases_per_domain=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(res_dpr["status"], "counterexample")
        self.assertLessEqual(res_dpr["shrink_attempts"], 32)
        min_dpr = res_dpr["minimal_fixture"]
        self.assertTrue(pf.is_valid_dpr_scale(min_dpr))
        self.assertGreaterEqual(min_dpr["dpr_ppm"], 500000)
        self.assertLess(
            pf.fixture_measure("dpr_scale", min_dpr),
            pf.fixture_measure("dpr_scale", res_dpr["original_fixture"]),
        )

        res_seq = pf.run_domain_campaign(
            4242,
            "action_sequence",
            lambda fix: len(fix["actions"]) < 2,
            cases_per_domain=64,
            max_shrink_attempts=32,
        )
        self.assertEqual(res_seq["status"], "counterexample")
        self.assertLessEqual(res_seq["shrink_attempts"], 32)
        min_seq = res_seq["minimal_fixture"]
        self.assertTrue(pf.is_valid_action_sequence(min_seq))
        self.assertGreaterEqual(len(min_seq["actions"]), 2)
        self.assertLess(
            pf.fixture_measure("action_sequence", min_seq),
            pf.fixture_measure("action_sequence", res_seq["original_fixture"]),
        )

    def test_budget_exhaustion_and_lazy_collaborator_guard(self):
        with self.assertRaises(pf.FuzzerBudgetExhausted):
            pf.run_domain_campaign(
                99,
                "bbox",
                lambda box: True,
                cases_per_domain=64,
                validity_fn=lambda box: False,
            )

        with self.assertRaises(RuntimeError) as ctx:
            pf.require_future_collaborator("wender.learn.nonexistent_future_sibling_module")
        self.assertIn("AUSENTE", str(ctx.exception))

    def test_concurrent_replayable_schedules_no_leaked_threads(self):
        baseline_threads = {t.ident for t in threading.enumerate()}
        for schedule_idx in range(32):
            seed = 9000 + schedule_idx
            expected_stream = pf.SeedTree(seed).stream_u64(4)
            barrier = threading.Barrier(4, timeout=2.0)
            start_event = threading.Event()
            results = [None] * 4
            errors = []

            def worker(tid, s=seed):
                try:
                    barrier.wait(timeout=2.0)
                    start_event.wait(timeout=2.0)
                    t = pf.SeedTree(s)
                    st = pf.generate_ui_tree(t.case("ui_tree", tid), min_nodes=2, max_nodes=4)
                    if schema.validate_state(st) != []:
                        raise AssertionError("concurrent state validation failed")
                    results[tid] = t.stream_u64(4)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(i,), name=f"sched-{schedule_idx}-{i}")
                for i in range(4)
            ]
            for t in threads:
                t.start()
            start_event.set()
            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"leaked thread {t.name}")

            self.assertEqual(errors, [])
            for r in results:
                self.assertEqual(r, expected_stream)

        after_threads = {t.ident for t in threading.enumerate()}
        self.assertTrue(after_threads.issubset(baseline_threads))


if __name__ == "__main__":
    unittest.main()
