# @module {"id":"wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff","layer":1,"owner":"STATE","deps":["wa.state.schema","wa.state.diff"],"book":"HB-WA-a0-arch"}
"""@anchor wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.main
Mutation kill tests and property verification for wender.state.schema and wender.state.diff.
"""
import ast
import hashlib
import importlib
import importlib.util
import random
import sys
import unittest

import numpy as np

from wender.state import diff, schema


def _make_valid_node(num, role="button", name="Run", attrs=None, depth=0, parent=None, ordinal=0, actionable=True, box=None):
    if attrs is None:
        attrs = {}
    return {
        "id": "%016x" % num,
        "role": role,
        "name": name,
        "depth": depth,
        "parent": parent,
        "ordinal": ordinal,
        "states": attrs,
        "actionable": actionable,
        "box": box,
    }


def _make_valid_state(nodes, kind="cdp", url="http://example.com", title="Title", ax_sha256=None, ax_stable_sha256=None, viewport=None):
    if ax_sha256 is None:
        ax_sha256 = "a" * 64
    if ax_stable_sha256 is None:
        ax_stable_sha256 = "b" * 64
    actionable_ids = [n["id"] for n in nodes if n["actionable"]]
    return {
        "schema": "wender.state/v1",
        "source": {"kind": kind, "url": url, "title": title},
        "prune": {"name": "prune_ax_v1", "version": 1},
        "ax_sha256": ax_sha256,
        "ax_stable_sha256": ax_stable_sha256,
        "viewport": viewport,
        "nodes": nodes,
        "actionable": actionable_ids,
        "counts": {
            "raw": len(nodes),
            "nodes": len(nodes),
            "actionable": len(actionable_ids),
        },
    }


def _make_valid_descriptor(handle=None, epoch=0, frame_seq=0, dpr_ppm=1000000, state=None):
    if handle is None:
        handle = {"kind": "cdp_tab", "canonical_id": "tab-1", "generation": 1}
    if state is None:
        state = _make_valid_state([_make_valid_node(1)])
    return {
        "handle": handle,
        "epoch": epoch,
        "frame_seq": frame_seq,
        "dpr_ppm": dpr_ppm,
        "state": state,
    }


class ASTMutator(ast.NodeTransformer):
    """AST mutation transformer per catalogue specifications.

    O on Compare/BoolOp/BinOp.
    B on Compare toggles Lt with LtE or Gt with GtE.
    B on exact integer Constant increments it by one.
    """

    def __init__(self, target_line, mut_type, target_func):
        self.target_line = target_line
        self.mut_type = mut_type
        self.target_func = target_func
        self.current_func = None
        self.applied = False

    def visit_FunctionDef(self, node):
        prev = self.current_func
        self.current_func = node.name
        self.generic_visit(node)
        self.current_func = prev
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self.current_func == self.target_func and getattr(node, "lineno", None) == self.target_line and self.mut_type == "O":
            if not self.applied:
                if isinstance(node.op, ast.Or):
                    node.op = ast.And()
                    self.applied = True
                elif isinstance(node.op, ast.And):
                    node.op = ast.Or()
                    self.applied = True
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.current_func == self.target_func and getattr(node, "lineno", None) == self.target_line and self.mut_type == "B":
            if not self.applied and node.ops:
                new_ops = []
                for op in node.ops:
                    if isinstance(op, ast.LtE):
                        new_ops.append(ast.Lt())
                        self.applied = True
                    elif isinstance(op, ast.Lt):
                        new_ops.append(ast.LtE())
                        self.applied = True
                    elif isinstance(op, ast.GtE):
                        new_ops.append(ast.Gt())
                        self.applied = True
                    elif isinstance(op, ast.Gt):
                        new_ops.append(ast.GtE())
                        self.applied = True
                    else:
                        new_ops.append(op)
                node.ops = new_ops
        return node

    def visit_Constant(self, node):
        self.generic_visit(node)
        if self.current_func == self.target_func and getattr(node, "lineno", None) == self.target_line and self.mut_type == "B":
            if not self.applied and type(node.value) is int:
                node.value = node.value + 1
                self.applied = True
        return node


class TestMutkillStateSchemaAndDiff(unittest.TestCase):
    """Unit tests and mutation kill harness for schema and diff modules."""

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_canonical_session_registry_constructors
    def test_canonical_session_registry_constructors(self):
        """Verify session registry dataclasses work cleanly with valid inputs."""
        handle = schema.Handle(kind="cdp_tab", canonical_id="tab-101", generation=1)
        self.assertEqual(getattr(handle, "kind"), "cdp_tab")
        self.assertEqual(getattr(handle, "canonical_id"), "tab-101")
        self.assertEqual(getattr(handle, "generation"), 1)

        rgb_bytes = np.zeros((100, 100, 3), dtype=np.uint8)
        digest = hashlib.sha256(rgb_bytes.tobytes()).hexdigest()
        frame = schema.FrameRef(
            handle=handle,
            epoch=0,
            frame_seq=5,
            width=100,
            height=100,
            rgb=rgb_bytes,
            digest=digest,
        )
        self.assertEqual(frame.frame_seq, 5)

        st = _make_valid_state([_make_valid_node(1)])
        obs = schema.Observation(
            handle=handle,
            epoch=0,
            frame_seq=5,
            dpr_ppm=1000000,
            state=st,
            frame=frame,
        )
        self.assertEqual(obs.dpr_ppm, 1000000)

        wf = schema.WindowFrames(
            handle=handle,
            epoch=0,
            frames=(frame,),
            frame_seqs=(5,),
            dpr_ppm=1000000,
        )
        self.assertEqual(len(wf.frames), 1)

        wd = schema.WindowDescription(
            handle=handle,
            epoch=0,
            frame_seqs=(5,),
            descriptors=(_make_valid_descriptor(handle=handle, frame_seq=5),),
            regions=({"x": 0, "y": 0, "w": 800, "h": 600},),
            error=None,
        )
        self.assertEqual(len(wd.descriptors), 1)

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_positive_dpr_and_frame_seq_validation
    def test_positive_dpr_and_frame_seq_validation(self):
        """Verify invalid DPR, frame_seq, or epoch are rejected by session registry dataclasses."""
        handle = schema.Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        rgb_bytes = np.zeros((10, 10, 3), dtype=np.uint8)
        digest = hashlib.sha256(rgb_bytes.tobytes()).hexdigest()
        frame = schema.FrameRef(handle=handle, epoch=0, frame_seq=0, width=10, height=10, rgb=rgb_bytes, digest=digest)
        st = _make_valid_state([_make_valid_node(1)])

        with self.assertRaises(Exception):
            schema.Observation(handle=handle, epoch=0, frame_seq=0, dpr_ppm=0, state=st, frame=frame)
        with self.assertRaises(Exception):
            schema.Observation(handle=handle, epoch=0, frame_seq=-1, dpr_ppm=1000, state=st, frame=frame)
        with self.assertRaises(Exception):
            schema.Observation(handle=handle, epoch=-1, frame_seq=0, dpr_ppm=1000, state=st, frame=frame)

        with self.assertRaises(Exception):
            schema.WindowFrames(handle=handle, epoch=0, frames=(frame, frame), frame_seqs=(1, 1), dpr_ppm=1000000)
        with self.assertRaises(Exception):
            schema.WindowFrames(handle=handle, epoch=0, frames=(frame, frame), frame_seqs=(2, 1), dpr_ppm=1000000)

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_nested_float_and_bool_refusal
    def test_nested_float_and_bool_refusal(self):
        """Verify floats anywhere and booleans in int slots are refused."""
        obj_with_float = {"a": [1, 2, {"b": 3.14}]}
        with self.assertRaises(schema.StateError):
            schema.dumps(obj_with_float)

        st_float = _make_valid_state([_make_valid_node(1)])
        st_float["nodes"][0]["ordinal"] = 1.0
        errs = schema.validate_state(st_float)
        self.assertTrue(any("float" in e or "non-negative int" in e for e in errs))

        st_bool = _make_valid_state([_make_valid_node(1)])
        st_bool["nodes"][0]["ordinal"] = True
        errs_bool = schema.validate_state(st_bool)
        self.assertTrue(any("non-negative int" in e for e in errs_bool))

        desc_float = _make_valid_descriptor(dpr_ppm=1000000.0)
        errs_desc = schema.validate_target_descriptor(desc_float)
        self.assertTrue(any("float" in e or "positive int" in e for e in errs_desc))

        desc_bool = _make_valid_descriptor(dpr_ppm=True)
        errs_desc_bool = schema.validate_target_descriptor(desc_bool)
        self.assertTrue(any("positive int" in e for e in errs_desc_bool))

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_unequal_role_and_name_types
    def test_unequal_role_and_name_types(self):
        """Test validation when role or name are not strings (or one is string and other is not)."""
        st1 = _make_valid_state([_make_valid_node(1, role=123, name="Button")])
        errs1 = schema.validate_state(st1)
        self.assertTrue(any("role/name must be strings" in e for e in errs1))

        st2 = _make_valid_state([_make_valid_node(1, role="button", name=456)])
        errs2 = schema.validate_state(st2)
        self.assertTrue(any("role/name must be strings" in e for e in errs2))

        st3 = _make_valid_state([_make_valid_node(1, role=None, name=None)])
        errs3 = schema.validate_state(st3)
        self.assertTrue(any("role/name must be strings" in e for e in errs3))

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_parent_and_actionable_ordering
    def test_parent_and_actionable_ordering(self):
        """Verify strict node parent ordering and document-order actionable listing."""
        # Parent does not precede child
        n1 = _make_valid_node(1, depth=0, parent=None)
        n2 = _make_valid_node(2, depth=1, parent="%016x" % 3)
        n3 = _make_valid_node(3, depth=0, parent=None)
        st_parent = _make_valid_state([n1, n2, n3])
        errs_p = schema.validate_state(st_parent)
        self.assertTrue(any("parent must precede the node" in e for e in errs_p))

        # Actionable list out of document order
        st_act = _make_valid_state([_make_valid_node(1, actionable=True), _make_valid_node(2, actionable=True)])
        st_act["actionable"] = ["%016x" % 2, "%016x" % 1]
        errs_a = schema.validate_state(st_act)
        self.assertTrue(any("actionable must list actionable node ids in document order" in e for e in errs_a))

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_diff_and_summarize_boundary
    def test_diff_and_summarize_boundary(self):
        """Test state diffing and strict 200-byte summary truncation."""
        st_before = _make_valid_state([_make_valid_node(1, name="A"), _make_valid_node(2, name="B")])
        st_after = _make_valid_state([_make_valid_node(1, name="A_changed"), _make_valid_node(3, name="C")])

        res = diff.diff_states(st_before, st_after)
        self.assertEqual(res["added"], ["%016x" % 3])
        self.assertEqual(res["removed"], ["%016x" % 2])
        self.assertEqual(len(res["changed"]), 1)

        summary_str = diff.summarize(res)
        self.assertEqual(summary_str, "plus 1 minus 1 changed 1")
        self.assertLessEqual(len(summary_str.encode("utf-8")), 200)

        # Force massive count to verify boundary
        large_diff = {"counts": {"added": 10**300, "removed": 10**300, "changed": 10**300}}
        large_sum = diff.summarize(large_diff)
        self.assertLessEqual(len(large_sum.encode("utf-8")), 200)

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_seeded_random_domain_properties
    def test_seeded_random_domain_properties(self):
        """Seeded random property testing loop over 64 valid cases and shrink attempts."""
        rng = random.Random(1928374)
        for i in range(64):
            num_nodes = rng.randint(1, 10)
            nodes = []
            for j in range(num_nodes):
                nodes.append(_make_valid_node(
                    num=j + 1,
                    role=rng.choice(["button", "link", "heading", "textbox"]),
                    name="node_%d" % j,
                    ordinal=j,
                    actionable=rng.choice([True, False]),
                    box=[0, 0, rng.randint(0, 500), rng.randint(0, 500)] if rng.choice([True, False]) else None
                ))
            st = _make_valid_state(nodes)
            errs = schema.validate_state(st)
            self.assertEqual(errs, [], "Case %d failed: %s" % (i, errs))

            desc = _make_valid_descriptor(epoch=i, frame_seq=i * 2, dpr_ppm=1000000 + i * 100, state=st)
            desc_errs = schema.validate_target_descriptor(desc)
            self.assertEqual(desc_errs, [], "Desc case %d failed: %s" % (i, desc_errs))

            # Validate diff against identical state
            d_res = diff.diff_states(st, st)
            self.assertEqual(d_res["counts"], {"added": 0, "removed": 0, "changed": 0})

    # @anchor:wa.tests.test_jq3_r_r1_j19_mutkill_state_schema_and_diff.test_ast_mutation_kills
    def test_ast_mutation_kills(self):
        """Prove Baseline passes on real code, and AST mutations produce semantic AssertionError (kill)."""
        mutations = [
            {
                "id": "M-wender-state-schema-1",
                "file": "wender/state/schema.py",
                "module": "wender.state.schema",
                "func": "validate_state",
                "line": 159,
                "type": "O",
                "trigger": lambda mod: self._trigger_schema_1(mod),
            },
            {
                "id": "M-wender-state-schema-2",
                "file": "wender/state/schema.py",
                "module": "wender.state.schema",
                "func": "validate_target_descriptor",
                "line": 212,
                "type": "B",
                "trigger": lambda mod: self._trigger_schema_2(mod),
            },
            {
                "id": "M-wender-state-diff-1",
                "file": "wender/state/diff.py",
                "module": "wender.state.diff",
                "func": "summarize",
                "line": 79,
                "type": "B",
                "trigger": lambda mod: self._trigger_diff_1(mod),
            },
        ]

        for mut in mutations:
            with self.subTest(mutation=mut["id"]):
                # 1. Positive control on unmutated real code first
                real_mod = sys.modules[mut["module"]]
                mut["trigger"](real_mod)  # Must pass without AssertionError

                # 2. Parse original AST source
                with open(mut["file"], "r", encoding="utf-8") as f:
                    source = f.read()

                tree = ast.parse(source, filename=mut["file"])
                transformer = ASTMutator(target_line=mut["line"], mut_type=mut["type"], target_func=mut["func"])
                mutated_tree = transformer.visit(tree)
                self.assertTrue(transformer.applied, "Failed to locate/apply mutation %s" % mut["id"])
                ast.fix_missing_locations(mutated_tree)

                # 3. Compile in-memory mutated module
                code_obj = compile(mutated_tree, filename=mut["file"], mode="exec")
                mutated_mod = importlib.util.module_from_spec(
                    importlib.util.spec_from_loader(mut["module"], loader=None)
                )

                # Save original sys.modules entry
                orig_mod = sys.modules.get(mut["module"])

                try:
                    sys.modules[mut["module"]] = mutated_mod
                    exec(code_obj, mutated_mod.__dict__)

                    # Re-link schema in diff if we mutated schema
                    if mut["module"] == "wender.state.schema" and "wender.state.diff" in sys.modules:
                        diff_mod = sys.modules["wender.state.diff"]
                        diff_mod.validate_state = mutated_mod.validate_state

                    # 4. Assert that mutated code raises semantic AssertionError (kill)
                    with self.assertRaises(AssertionError, msg="Mutant %s survived!" % mut["id"]):
                        mut["trigger"](mutated_mod)

                finally:
                    # Restore original module in sys.modules
                    if orig_mod is not None:
                        sys.modules[mut["module"]] = orig_mod
                        if "wender.state.diff" in sys.modules and mut["module"] == "wender.state.schema":
                            sys.modules["wender.state.diff"].validate_state = orig_mod.validate_state

    def _trigger_schema_1(self, mod):
        # M-wender-state-schema-1: 'not isinstance(n["role"], str) or not isinstance(n["name"], str)' -> 'And'
        # With 'And', if role is int (123) and name is str ("Button"),
        # 'not isinstance(role, str)' is True, but 'not isinstance(name, str)' is False.
        # Original (Or) returns True -> appends error.
        # Mutated (And) returns False -> no error appended.
        st = _make_valid_state([_make_valid_node(1, role=123, name="Button")])
        errs = mod.validate_state(st)
        assert any("role/name must be strings" in e for e in errs), "schema-1 trigger expected error string"

    def _trigger_schema_2(self, mod):
        # M-wender-state-schema-2: B on 'dpr_ppm <= 0' toggles LtE with Lt ('dpr_ppm < 0') or B on Constant 0 increments to 1 ('dpr_ppm <= 1').
        # For dpr_ppm = 0:
        # Original (dpr_ppm <= 0): True -> error appended ("dpr_ppm must be a positive int").
        # If mutated to dpr_ppm < 0: False -> no error.
        #
        # For dpr_ppm = 1:
        # Original (dpr_ppm <= 0): False -> no error.
        # If mutated to dpr_ppm <= 1: True -> error appended.
        #
        # To kill both possible B operator/constant mutations, check both 0 and 1.
        desc0 = _make_valid_descriptor(dpr_ppm=0)
        errs0 = mod.validate_target_descriptor(desc0)
        assert any("dpr_ppm must be a positive int" in e for e in errs0), "schema-2 trigger expected error for dpr_ppm=0"

        desc1 = _make_valid_descriptor(dpr_ppm=1)
        errs1 = mod.validate_target_descriptor(desc1)
        assert len(errs1) == 0, "schema-2 trigger expected 0 errors for dpr_ppm=1, got: %s" % errs1

    def _trigger_diff_1(self, mod):
        # M-wender-state-diff-1: 200 -> 201
        # Truncate summary string at boundary.
        # We construct diff with counts such that str length is exactly 201 chars before slicing.
        # "plus 1 minus 1 changed " is 23 chars.
        # If changed is 10**177 (178 digits), total length = 23 + 178 = 201 chars.
        # Slicing [:200] gives 200 chars.
        # Slicing [:201] gives 201 chars.
        big_diff = {"counts": {"added": 1, "removed": 1, "changed": 10**177}}
        res = mod.summarize(big_diff)
        assert len(res) == 200, "diff-1 trigger expected summary string length 200, got %d" % len(res)


if __name__ == "__main__":
    unittest.main()
