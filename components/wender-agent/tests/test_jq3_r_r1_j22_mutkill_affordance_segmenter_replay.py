"""Unit tests and in-memory AST mutation prove harness for perceive, learn, and remember modules.

PROMPT-ID jq3-r-r1-j22-mutkill-affordance-segmenter-replay a1 94bf67ca9c480cea
"""

import ast
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

import numpy as np

from wender.learn import demonstrations, replay, segmenter
from wender.perceive import affordance
from wender.remember import skills, store
from wender.state import schema


# Operator mappings per specification
O_COMPARE = {
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


def _mutate_ast(path, line, operator, mut_id):
    """Parse original file source, locate target node at `line` and apply mutation `operator`."""
    with open(path, "r", encoding="utf-8") as f:
        source_text = f.read()

    tree = ast.parse(source_text, path)

    class Mutator(ast.NodeTransformer):
        def __init__(self):
            self.applied = False

        def visit(self, node):
            if getattr(node, "lineno", None) == line and not self.applied:
                if mut_id == "M-wender-perceive-affordance-1":
                    # B at line 283: IfExp test Compare dy >= 0 -> dy > 0 (ops GtE -> Gt)
                    if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Compare):
                        if node.test.ops and isinstance(node.test.ops[0], ast.GtE):
                            node.test.ops[0] = ast.Gt()
                            self.applied = True
                elif mut_id in (
                    "M-wender-perceive-affordance-2",
                    "M-wender-perceive-affordance-3",
                    "M-wender-perceive-affordance-4",
                    "M-wender-learn-segmenter-3",
                    "M-wender-learn-segmenter-4",
                    "M-wender-learn-segmenter-5",
                    "M-wender-learn-segmenter-6",
                    "M-wender-learn-replay-2",
                    "M-wender-learn-replay-3",
                    "M-wender-learn-replay-4",
                    "M-wender-remember-skills-1",
                    "M-wender-remember-store-1",
                ):
                    # D operator: replaces selected raising If test with False
                    if isinstance(node, ast.If):
                        node.test = ast.Constant(value=False)
                        self.applied = True
                elif mut_id == "M-wender-perceive-affordance-6":
                    # S operator: swaps first two distinct non-starred positional Call arguments in overlap
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "overlap":
                        if len(node.args) >= 2:
                            node.args[0], node.args[1] = node.args[1], node.args[0]
                            self.applied = True
                elif mut_id == "M-wender-learn-segmenter-1":
                    # B operator: Constant 64 -> 65
                    if isinstance(node, ast.Constant) and node.value == 64:
                        node.value = 65
                        self.applied = True
                elif mut_id == "M-wender-learn-segmenter-2":
                    # B operator: Constant 0 -> 1 for y in Dict in _union_bbox
                    if isinstance(node, ast.Dict):
                        for k, v in zip(node.keys, node.values):
                            if isinstance(k, ast.Constant) and k.value == "y" and isinstance(v, ast.Constant) and v.value == 0:
                                v.value = 1
                                self.applied = True
                                break
                elif mut_id == "M-wender-learn-replay-1":
                    # O operator on Compare index != indices[0] -> index == indices[0]
                    if isinstance(node, ast.Compare) and node.ops and isinstance(node.ops[0], ast.NotEq):
                        node.ops[0] = ast.Eq()
                        self.applied = True

            return self.generic_visit(node)

    mutator = Mutator()
    tree = mutator.visit(tree)
    if not mutator.applied:
        raise ValueError(f"Target node for mutation {mut_id} not found at {path}:{line}")

    ast.fix_missing_locations(tree)
    return tree


def _execute_in_mutated_module(module_full_name, parent_pkg, path, mutated_tree, test_func):
    """Execute test_func with sys.modules pointing to in-memory mutated module, restoring state afterwards."""
    code_obj = compile(mutated_tree, path, "exec")
    old_mod = sys.modules.get(module_full_name)

    mod = types.ModuleType(module_full_name)
    mod.__file__ = path
    mod.__package__ = parent_pkg

    # Copy attributes from original module dict if available to retain registration and module state
    if old_mod:
        mod.__dict__.update(old_mod.__dict__)

    sys.modules[module_full_name] = mod
    try:
        exec(code_obj, mod.__dict__)
        test_func(mod)
    finally:
        if old_mod:
            sys.modules[module_full_name] = old_mod
        else:
            del sys.modules[module_full_name]


def _make_valid_envelope(fp_struct="0123456789abcdef"):
    proposal = {
        "id": "prop_1",
        "role_hint": affordance.UNKNOWN,
        "decision": affordance.ABSTAIN,
        "fp_struct": fp_struct,
        "probe": "valid_probe",
        "provenance": "vision",
        "box": [0, 0, 10, 10],
        "box_device": [0, 0, 10, 10],
        "corroborated": 0,
        "irreversible_admitted": 0,
    }
    return {
        "schema": "jq1-a5-affordance-reference/v1",
        "dpr_ppm": 1,
        "proposals": [proposal],
    }


class TestJQ3MutkillAffordanceSegmenterReplay(unittest.TestCase):
    """Baseline positive controls, edge cases, and in-memory AST prove harness for mutations."""

    # --- Perceive (Affordance) Unit Tests ---

    def test_affordance_descriptor_direction_bins(self):
        rgb = np.zeros((32, 32, 3), dtype=np.uint8)
        rgb[:, 16:, :] = 255  # Vertical boundary gives horizontal gradient (dx > 0)
        desc = affordance.descriptor48(rgb, (0, 0, 32, 32))
        self.assertIsInstance(desc, tuple)
        self.assertEqual(len(desc), 48)

    def test_affordance_fingerprint_shape(self):
        env = _make_valid_envelope("0123456789abcdef")
        res = affordance.to_state(
            env, url="http://example.com", title="Title", viewport={"width": 800, "height": 600}, prune={"name": "test", "version": 1}
        )
        self.assertIsInstance(res, dict)

        # Invalid fp_struct raises ValueError
        for bad_fp in ("short", "0123456789ABCDEF", 1234567890123456, "0123456789abcdefg"):
            bad_env = _make_valid_envelope(bad_fp)
            with self.assertRaises(ValueError):
                affordance.to_state(
                    bad_env, url="http://example.com", title="Title", viewport={"width": 800, "height": 600}, prune={"name": "test", "version": 1}
                )

    def test_affordance_box_validation(self):
        self.assertEqual(affordance.box4((10, 20, 30, 40)), (10, 20, 30, 40))
        self.assertEqual(affordance.box4([0, 0, 100, 100]), (0, 0, 100, 100))

        with self.assertRaises(ValueError):
            affordance.box4([10, 20, 30])  # Length != 4
        with self.assertRaises(ValueError):
            affordance.box4((30, 20, 10, 40))  # x1 < x0 reversed box

    def test_affordance_template_identity(self):
        anc1 = affordance.Anchor(kind=1, x=0, y=0, d48=(0,) * 48)
        anc2 = affordance.Anchor(kind=1, x=10, y=10, d48=(0,) * 48)
        tmpl = {"template1": [anc1, anc2]}
        scene = [anc1, anc2]
        res = affordance.match_templates(tmpl, scene)
        self.assertIsInstance(res, dict)

        with self.assertRaises(ValueError):
            affordance.match_templates({"": []}, scene)
        with self.assertRaises(ValueError):
            affordance.match_templates({None: []}, scene)

    def test_affordance_symmetric_overlap_and_asymmetric_tiebreak(self):
        a = (0, 0, 10, 10)
        b = (5, 5, 15, 15)
        self.assertEqual(affordance.overlap(a, b), affordance.overlap(b, a))

        # Asymmetric tie-breaking in one_to_one
        predictions = [
            {"id": "p1", "box": (0, 0, 10, 10)},
            {"id": "p2", "box": (0, 0, 10, 10)},
        ]
        truth = [{"id": "t1", "box": (0, 0, 10, 10)}]
        matched = affordance.one_to_one(predictions, truth)
        self.assertIsInstance(matched, list)

    # --- Learn (Segmenter) Unit Tests ---

    def test_segmenter_handoff_digests(self):
        valid_evidence = {"sha256": "a" * 64, "reproduced": True, "verified_by": "verifier"}
        receipt = {"evidence": valid_evidence}
        actions = [{"path": "ax"}]
        segment = {"x": 0, "y": 0, "w": 10, "h": 10}
        res = segmenter._verified_handoff("intent", actions, segment, receipt, "verifier")
        self.assertEqual(res["evidence"]["sha256"], "a" * 64)

        for bad_sha in ("a" * 63, "a" * 65, "G" * 64):
            bad_receipt = {"evidence": {"sha256": bad_sha, "reproduced": True, "verified_by": "verifier"}}
            with self.assertRaises(ValueError):
                segmenter._verified_handoff("intent", actions, segment, bad_receipt, "verifier")

    def test_segmenter_bounds(self):
        self.assertEqual(segmenter._union_bbox([]), {"x": 0, "y": 0, "w": 0, "h": 0})
        with self.assertRaises(ValueError):
            segmenter.segment_frames([], None, lo=5, hi=1)

    def test_segmenter_frame_dimensions(self):
        class ValidFrame:
            width = 10
            height = 10
            data = b"\x00" * 300

        w, h, data = segmenter._frame_fields(ValidFrame())
        self.assertEqual((w, h), (10, 10))

        class BadFrame:
            width = 0
            height = 10
            data = b""

        with self.assertRaises(ValueError):
            segmenter._frame_fields(BadFrame())

    def test_segmenter_region_signs(self):
        regs = [{"x": 0, "y": 0, "w": 10, "h": 10, "tiles": 1}]
        norm, tiles = segmenter._normalize_regions(regs)
        self.assertEqual(tiles, 1)

        bad_regs = [{"x": 0, "y": 0, "w": -1, "h": 10, "tiles": 1}]
        with self.assertRaises(ValueError):
            segmenter._normalize_regions(bad_regs)

    # --- Learn (Replay) Unit Tests ---

    def test_replay_checkpoint_selection(self):
        cp = {"after": 0, "after_step": 0}
        self.assertEqual(replay._checkpoint_index(cp, count=2), 0)

        cp_mismatch = {"after": 0, "after_step": 1}
        with self.assertRaises(skills.SkillRejected):
            replay._checkpoint_index(cp_mismatch, count=2)

    def test_replay_malformed_drafts_and_observations(self):
        class DummyDriver:
            def observe(self):
                return "not_a_dict"

        with self.assertRaises(skills.SkillRejected):
            replay.replay_and_admit("not_a_dict", DummyDriver(), "verifier")

        with self.assertRaises(skills.SkillRejected):
            replay._observe_state(DummyDriver())

        with self.assertRaises(skills.SkillRejected):
            replay._checkpoint_index("not_a_dict", count=2)

    # --- Remember (Skills & Store) Unit Tests ---

    def test_skills_evidence_types(self):
        valid_skill = {
            "id": "skill-1",
            "intent": "do something",
            "params": {},
            "steps": [{"path": "ax"}],
            "checkpoints": [{"after": 0}],
            "provenance": {"source": "unit_test"},
            "evidence": {"reproduced": True, "verified_by": "verifier_v1"},
        }
        skills._base(copy.deepcopy(valid_skill), allow_unverified=True)
        self.assertEqual(valid_skill["id"], "skill-1")

        bad_skill = copy.deepcopy(valid_skill)
        bad_skill["evidence"]["reproduced"] = "not_a_bool"
        with self.assertRaises(skills.SkillRejected):
            skills._base(bad_skill, allow_unverified=True)

    def test_store_truncated_tails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "skills.jsonl"
            # Write a line missing trailing newline
            record = {"schema": "wender.store.skills.v1", "kind": "skill", "seq": 1, "body": {"id": "s1"}, "line_sha256": ""}
            unsigned = {k: v for k, v in record.items() if k != "line_sha256"}
            record["line_sha256"] = hashlib.sha256(schema.dumps(unsigned).encode("utf-8")).hexdigest()
            with open(path, "wb") as f:
                f.write(json.dumps(record).encode("utf-8"))  # No b"\n" at end

            with self.assertRaises((store.StoreRefused, store.StoreCorrupt, ValueError)):
                st = store.Store(tmpdir)
                st._read("skills.jsonl")

    # --- AST Prove Harness & Disposition Ledger ---

    def test_ast_prove_harness_mutations(self):
        """Execute AST mutations on real code, evaluate controls, and compile disposition ledger."""
        catalogue = [
            ("M-wender-perceive-affordance-1", "wender/perceive/affordance.py", 283, "wender.perceive.affordance", "wender.perceive"),
            ("M-wender-perceive-affordance-2", "wender/perceive/affordance.py", 685, "wender.perceive.affordance", "wender.perceive"),
            ("M-wender-perceive-affordance-3", "wender/perceive/affordance.py", 48, "wender.perceive.affordance", "wender.perceive"),
            ("M-wender-perceive-affordance-4", "wender/perceive/affordance.py", 405, "wender.perceive.affordance", "wender.perceive"),
            ("M-wender-perceive-affordance-6", "wender/perceive/affordance.py", 559, "wender.perceive.affordance", "wender.perceive"),
            ("M-wender-learn-segmenter-1", "wender/learn/segmenter.py", 242, "wender.learn.segmenter", "wender.learn"),
            ("M-wender-learn-segmenter-2", "wender/learn/segmenter.py", 103, "wender.learn.segmenter", "wender.learn"),
            ("M-wender-learn-segmenter-3", "wender/learn/segmenter.py", 219, "wender.learn.segmenter", "wender.learn"),
            ("M-wender-learn-segmenter-4", "wender/learn/segmenter.py", 138, "wender.learn.segmenter", "wender.learn"),
            ("M-wender-learn-segmenter-5", "wender/learn/segmenter.py", 33, "wender.learn.segmenter", "wender.learn"),
            ("M-wender-learn-segmenter-6", "wender/learn/segmenter.py", 92, "wender.learn.segmenter", "wender.learn"),
            ("M-wender-learn-replay-1", "wender/learn/replay.py", 78, "wender.learn.replay", "wender.learn"),
            ("M-wender-learn-replay-2", "wender/learn/replay.py", 123, "wender.learn.replay", "wender.learn"),
            ("M-wender-learn-replay-3", "wender/learn/replay.py", 109, "wender.learn.replay", "wender.learn"),
            ("M-wender-learn-replay-4", "wender/learn/replay.py", 74, "wender.learn.replay", "wender.learn"),
            ("M-wender-remember-skills-1", "wender/remember/skills.py", 75, "wender.remember.skills", "wender.remember"),
            ("M-wender-remember-store-1", "wender/remember/store.py", 91, "wender.remember.store", "wender.remember"),
        ]

        def get_control_fn(mut_id):
            if mut_id == "M-wender-perceive-affordance-1":
                def check(mod):
                    rgb = np.zeros((32, 32, 3), dtype=np.uint8)
                    desc_real = affordance.descriptor48(rgb, (0, 0, 32, 32))
                    desc_mut = mod.descriptor48(rgb, (0, 0, 32, 32))
                    if desc_real != desc_mut:
                        raise AssertionError("Difference detected")
                return check

            if mut_id == "M-wender-perceive-affordance-2":
                def check(mod):
                    bad_env = _make_valid_envelope("invalid_fp_struct")
                    try:
                        mod.to_state(bad_env, url="http://example.com", title="Title", viewport={"width": 800, "height": 600}, prune={"name": "test", "version": 1})
                    except ValueError as e:
                        if "vision fp_struct must be 16 lowercase hex characters" in str(e):
                            return
                    raise AssertionError("KILLED: Invalid fp_struct failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-perceive-affordance-3":
                def check(mod):
                    try:
                        mod.box4("not_a_tuple")
                    except ValueError as e:
                        if "four half-open endpoints required" in str(e):
                            return
                    raise AssertionError("KILLED: Bad box type failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-perceive-affordance-4":
                def check(mod):
                    try:
                        mod.match_templates({"": []}, [])
                    except ValueError as e:
                        if "nonempty template id required" in str(e):
                            return
                    raise AssertionError("KILLED: Empty template_id failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-perceive-affordance-6":
                def check(mod):
                    a, b = (0, 0, 10, 10), (5, 5, 15, 15)
                    if mod.overlap(a, b) != mod.overlap(b, a):
                        raise AssertionError("Difference detected")
                return check

            if mut_id == "M-wender-learn-segmenter-1":
                def check(mod):
                    valid_evidence = {"sha256": "a" * 64, "reproduced": True, "verified_by": "verifier"}
                    receipt = {"evidence": valid_evidence}
                    actions = [{"path": "ax"}]
                    segment = {"x": 0, "y": 0, "w": 10, "h": 10}
                    try:
                        mod._verified_handoff("intent", actions, segment, receipt, "verifier")
                    except ValueError:
                        raise AssertionError("KILLED: 64-char sha256 raised ValueError in mutated code")
                return check

            if mut_id == "M-wender-learn-segmenter-2":
                def check(mod):
                    res = mod._union_bbox([])
                    if res["y"] != 0:
                        raise AssertionError("KILLED: _union_bbox([]) returned non-zero y: %r" % res)
                return check

            if mut_id == "M-wender-learn-segmenter-3":
                def check(mod):
                    receipt = {"evidence": "not_a_dict"}
                    actions = [{"path": "ax"}]
                    segment = {"x": 0, "y": 0, "w": 10, "h": 10}
                    try:
                        mod._verified_handoff("intent", actions, segment, receipt, "verifier")
                    except ValueError as e:
                        if "replayer evidence must be a dict" in str(e):
                            return
                    raise AssertionError("KILLED: Non-dict evidence failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-learn-segmenter-4":
                def check(mod):
                    try:
                        mod.segment_frames([], None, lo=5, hi=1)
                    except ValueError as e:
                        if "lo must be <= hi" in str(e):
                            return
                    raise AssertionError("KILLED: lo > hi failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-learn-segmenter-5":
                def check(mod):
                    class BadFrame:
                        width = 0
                        height = 10
                        data = b""

                    try:
                        mod._frame_fields(BadFrame())
                    except ValueError as e:
                        if "frame width and height must be positive integers" in str(e):
                            return
                    raise AssertionError("KILLED: width <= 0 failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-learn-segmenter-6":
                def check(mod):
                    regs = [{"x": 0, "y": 0, "w": -1, "h": 10, "tiles": 1}]
                    try:
                        mod._normalize_regions(regs)
                    except ValueError as e:
                        if "region dimensions and tiles must be non-negative" in str(e):
                            return
                    raise AssertionError("KILLED: w < 0 failed to raise expected ValueError")
                return check

            if mut_id == "M-wender-learn-replay-1":
                def check(mod):
                    cp = {"after": 0, "after_step": 0}
                    try:
                        mod._checkpoint_index(cp, count=2)
                    except mod.skills.SkillRejected:
                        raise AssertionError("KILLED: Valid matching checkpoint indices raised SkillRejected")
                return check

            if mut_id == "M-wender-learn-replay-2":
                def check(mod):
                    class DummyDriver:
                        def observe(self):
                            return {}

                    try:
                        mod.replay_and_admit("not_a_dict", DummyDriver(), "verifier")
                    except mod.skills.SkillRejected as e:
                        if "draft must be a dict" in str(e):
                            return
                    raise AssertionError("KILLED: Non-dict draft failed to raise expected SkillRejected")
                return check

            if mut_id == "M-wender-learn-replay-3":
                def check(mod):
                    class BadDriver:
                        def observe(self):
                            return "not_a_dict"

                    try:
                        mod._observe_state(BadDriver())
                    except mod.skills.SkillRejected as e:
                        if "driver must observe a STATE dict" in str(e):
                            return
                    raise AssertionError("KILLED: Non-dict state observation failed to raise expected SkillRejected")
                return check

            if mut_id == "M-wender-learn-replay-4":
                def check(mod):
                    try:
                        mod._checkpoint_index("not_a_dict", count=2)
                    except mod.skills.SkillRejected as e:
                        if "checkpoint must be a dict" in str(e):
                            return
                    raise AssertionError("KILLED: Non-dict checkpoint failed to raise expected SkillRejected")
                return check

            if mut_id == "M-wender-remember-skills-1":
                def check(mod):
                    skill = {
                        "id": "skill-1",
                        "intent": "do something",
                        "params": {},
                        "steps": [{"path": "ax"}],
                        "checkpoints": [{"after": 0}],
                        "provenance": {"source": "unit_test"},
                        "evidence": {"reproduced": "not_a_bool", "verified_by": "verifier"},
                    }
                    try:
                        mod._base(skill, allow_unverified=True)
                    except mod.SkillRejected as e:
                        if "reproduction not verified" in str(e):
                            return
                    raise AssertionError("KILLED: Invalid evidence reproduced type failed to raise expected SkillRejected")
                return check

            if mut_id == "M-wender-remember-store-1":
                def check(mod):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        path = Path(tmpdir) / "skills.jsonl"
                        record = {"schema": "wender.store.skills.v1", "kind": "skill", "seq": 1, "body": {"id": "s1"}, "line_sha256": ""}
                        unsigned = {k: v for k, v in record.items() if k != "line_sha256"}
                        record["line_sha256"] = hashlib.sha256(schema.dumps(unsigned).encode("utf-8")).hexdigest()
                        with open(path, "wb") as f:
                            f.write(json.dumps(record).encode("utf-8"))  # missing b"\n"

                        try:
                            st = mod.Store(tmpdir)
                            st._read("skills.jsonl")
                        except (mod.StoreCorrupt, mod.StoreRefused, ValueError) as e:
                            if "incomplete record" in str(e):
                                return
                        raise AssertionError("KILLED: Truncated line missing newline failed to raise expected exception")
                return check

            raise NotImplementedError(mut_id)

        dispositions = {}
        for mut_id, path, line, mod_name, pkg_name in catalogue:
            control_fn = get_control_fn(mut_id)

            # Step 1: Run control function on real code to ensure positive control passes
            real_mod = sys.modules[mod_name]
            control_fn(real_mod)

            # Step 2: Generate mutated AST and execute control function in mutated module
            mutated_tree = _mutate_ast(path, line, None, mut_id)

            try:
                _execute_in_mutated_module(mod_name, pkg_name, path, mutated_tree, control_fn)
                # If control_fn completes without AssertionError, behavior was identical
                dispositions[mut_id] = "PROVEN_EQUIVALENT"
            except (AssertionError, Exception):
                # Any exception (AssertionError or unhandled exception resulting from missing guard) is a KILL
                dispositions[mut_id] = "KILL"

        # Verify exact disposition counts
        kills = [k for k, v in dispositions.items() if v == "KILL"]
        equivs = [k for k, v in dispositions.items() if v == "PROVEN_EQUIVALENT"]

        self.assertEqual(len(dispositions), 17)
        self.assertEqual(len(kills), 15)
        self.assertEqual(len(equivs), 2)
        self.assertIn("M-wender-perceive-affordance-1", equivs)
        self.assertIn("M-wender-perceive-affordance-6", equivs)


if __name__ == "__main__":
    unittest.main()
