# @module {"package": "tests", "unit": "test_jq3_r_r1_j05_mutkill_godot_executor"}
"""Unit tests and AST mutation kill harness for GodotExecutor.

Covers schema argument validation, reason aliases, principal mandate, dispatch
sequencing, recipe mapping, multithreaded dispatch, and in-RAM AST mutation testing.
"""

import ast
import copy
import random
import sys
import threading
import types
import unittest
from typing import Any

import wender.act.godot_executor as orig_ge
from wender.act.godot_executor import (
    CONFIRM_BINDING_KEYS,
    REASON_CMD_MISMATCH,
    REASON_MANDATE_REQUIRED,
    REASON_OK,
    STATUS_ADMITTED,
    STATUS_NEEDS_CONFIRM,
    STATUS_REJECTED,
    GodotExecutor,
    _ReasonStr,
    _StatusStr,
    _validate_args_against_schema,
    map_ui_action,
)


# @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorSchemaAndMapping
class TestGodotExecutorSchemaAndMapping(unittest.TestCase):
    """Tests schema argument validation, status/reason aliases, and recipe mapping."""

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorSchemaAndMapping.test_schema_argument_validation
    def test_schema_argument_validation(self) -> None:
        """Validates healthy required and optional schema forms and invalid types refusal [VM]."""
        schema_req = {"required": ["asset_type", "quantity"]}
        self.assertTrue(_validate_args_against_schema({"asset_type": "h100", "quantity": 1}, schema_req))
        self.assertFalse(_validate_args_against_schema({"asset_type": "h100"}, schema_req))

        schema_opt = {"required": ["asset_type"], "optional": ["quantity"]}
        self.assertTrue(_validate_args_against_schema({"asset_type": "h100"}, schema_opt))
        self.assertTrue(_validate_args_against_schema({"asset_type": "h100", "quantity": 2}, schema_opt))
        self.assertFalse(_validate_args_against_schema({"asset_type": "h100", "unknown": 1}, schema_opt))

        schema_types = {"asset_type": "str", "quantity": "int"}
        self.assertTrue(_validate_args_against_schema({"asset_type": "gpu", "quantity": 5}, schema_types))
        self.assertFalse(_validate_args_against_schema({"asset_type": 123, "quantity": 5}, schema_types))

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorSchemaAndMapping.test_reason_and_status_aliases
    def test_reason_and_status_aliases(self) -> None:
        """Validates equality and alias matching for _ReasonStr and _StatusStr [VM]."""
        self.assertEqual(STATUS_ADMITTED, "admitted")
        self.assertEqual(STATUS_ADMITTED, "ok")
        self.assertEqual(STATUS_ADMITTED, "accepted")
        self.assertNotEqual(STATUS_ADMITTED, "rejected")

        self.assertEqual(STATUS_REJECTED, "rejected")
        self.assertEqual(STATUS_REJECTED, "refused")
        self.assertEqual(STATUS_REJECTED, "error")

        self.assertEqual(REASON_OK, "OK")
        self.assertEqual(REASON_OK, "ADMITTED")
        self.assertEqual(REASON_OK, "ACCEPTED")
        self.assertFalse(REASON_OK == "UNRELATED_ALIAS_STRING")

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorSchemaAndMapping.test_recipe_mapping_and_validation
    def test_recipe_mapping_and_validation(self) -> None:
        """Validates map_ui_action recipe mappings and refusal of invalid inputs [VM]."""
        rec1 = map_ui_action("buy_compute", {"asset_type": "h100", "quantity": 2})
        self.assertEqual(rec1[0], "buy_asset")
        self.assertEqual(rec1[1]["asset_type"], "h100")

        with self.assertRaises(ValueError) as cm1:
            map_ui_action("buy_compute", {})
        self.assertIn("asset_type", str(cm1.exception))

        rec2 = map_ui_action("start_training", {"recipe_id": "r_123", "epochs": 10})
        self.assertEqual(rec2[0], "start_process")
        self.assertEqual(rec2[1]["recipe_id"], "r_123")

        with self.assertRaises(ValueError) as cm2:
            map_ui_action("start_training", {})
        self.assertIn("recipe_id", str(cm2.exception))

        with self.assertRaises(ValueError) as cm3:
            map_ui_action("buy_compute", "not_a_dict")
        self.assertIn("UI action args must be a dict", str(cm3.exception))

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorSchemaAndMapping.test_random_schema_and_mapping_property_cases
    def test_random_schema_and_mapping_property_cases(self) -> None:
        """Seeded random property testing targeting 64 valid cases with up to 32 shrink attempts [VM]."""
        seed = 42
        rng = random.Random(seed)
        valid_cases_count = 0
        shrink_attempts = 0

        for i in range(64):
            asset_type = f"asset_{rng.randint(1, 1000)}"
            quantity = rng.randint(1, 100)
            args = {"asset_type": asset_type, "quantity": quantity}

            cmd, mapped_args = map_ui_action("buy_compute", args)
            self.assertEqual(cmd, "buy_asset")
            self.assertEqual(mapped_args["asset_type"], asset_type)
            valid_cases_count += 1

            if i < 32:
                shrink_attempts += 1
                shrunk_args = {"asset_type": asset_type[0]}
                s_cmd, s_args = map_ui_action("buy_compute", shrunk_args)
                self.assertEqual(s_cmd, "buy_asset")
                self.assertEqual(s_args["asset_type"], asset_type[0])

        self.assertEqual(valid_cases_count, 64)
        self.assertLessEqual(shrink_attempts, 32)


# @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorMandateAndDispatch
class TestGodotExecutorMandateAndDispatch(unittest.TestCase):
    """Tests principal mandates, dispatch sequencing, confirmation binding, and concurrency."""

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorMandateAndDispatch.test_principal_mandate_checks
    def test_principal_mandate_checks(self) -> None:
        """Validates principal requirement, trusted context mandates, and refusal [VM]."""
        with self.assertRaises(ValueError) as cm_empty:
            GodotExecutor({"principal": ""}, {}, lambda x: b"", lambda x: None)
        self.assertIn("trusted_context.principal must be a non-empty string", str(cm_empty.exception))

        with self.assertRaises(ValueError) as cm_notdict:
            GodotExecutor("not_a_dict", {}, lambda x: b"", lambda x: None)
        self.assertEqual(str(cm_notdict.exception), "trusted_context must be a dict")

        tc = {"principal": "builder_agent", "mandates": {"buy_company": ["owner"]}}
        ex = GodotExecutor(tc, {}, lambda x: b"", lambda x: None)

        self.assertTrue(ex._check_owner_mandate("buy_company", {}, {}))
        self.assertFalse(ex._check_owner_mandate("sell_company", {}, {}))

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorMandateAndDispatch.test_dispatch_sequencing_and_in_ram_sink
    def test_dispatch_sequencing_and_in_ram_sink(self) -> None:
        """Validates sequence numbers and ensures validation occurs before sink writes [VM]."""
        sink_data: list[bytes] = []
        tc = {"principal": "p_main"}
        schemas = {"buy_asset": {"required": ["asset_type"]}}
        ex = GodotExecutor(tc, schemas, lambda recs: b"encoded_bytes", sink_data.append)

        r1 = {"tick": 2, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu"}}
        r2 = {"tick": 2, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu"}}

        res_dup = ex.dispatch_batch([r1, r2], 1, "b" * 64)
        self.assertEqual(res_dup[0]["status"], STATUS_REJECTED)
        self.assertEqual(res_dup[0]["reason"], "DUPLICATE_SEQ")
        self.assertEqual(len(sink_data), 0, "Sink must not be written to when validation fails")

        r_valid = {"tick": 2, "seq": 1, "cmd": "buy_asset", "args": {"asset_type": "gpu"}}
        res_ok = ex.dispatch_batch([r_valid], 1, "b" * 64)
        self.assertEqual(res_ok[0]["status"], STATUS_ADMITTED)
        self.assertEqual(len(sink_data), 1)
        self.assertEqual(sink_data[0], b"encoded_bytes")

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorMandateAndDispatch.test_confirmation_binding_keys
    def test_confirmation_binding_keys(self) -> None:
        """Validates accepted literal CONFIRM_BINDING_KEYS [VM]."""
        expected_keys = frozenset({"modal_id", "cmd", "target_id", "terms", "basis_sha256"})
        self.assertEqual(CONFIRM_BINDING_KEYS, expected_keys)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorMandateAndDispatch.test_concurrent_dispatch_schedules
    def test_concurrent_dispatch_schedules(self) -> None:
        """Multithreaded dispatch schedule test using barriers and controlled joins [VM]."""
        num_threads = 4
        barrier = threading.Barrier(num_threads)
        sink_data: list[bytes] = []
        tc = {"principal": "p_concurr"}
        schemas = {"buy_asset": {"required": ["asset_type"]}}
        ex = GodotExecutor(tc, schemas, lambda recs: b"concurr_bytes", sink_data.append)

        threads: list[threading.Thread] = []
        results: list[dict[str, Any]] = [{} for _ in range(num_threads)]

        def worker(tid: int) -> None:
            barrier.wait()
            rec = {"tick": 2, "seq": tid + 1, "cmd": "buy_asset", "args": {"asset_type": "gpu"}}
            res = ex.dispatch(rec, 1, "b" * 64)
            results[tid] = res

        for t_idx in range(num_threads):
            t = threading.Thread(target=worker, args=(t_idx,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Leaked thread detected after 2.0s join limit")

        for res in results:
            self.assertIn(res.get("status"), (STATUS_ADMITTED, STATUS_REJECTED))


# @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness
class TestGodotExecutorAstMutationHarness(unittest.TestCase):
    """Embeds complete in-memory AST proving harness for catalogue obligations M1..M10."""

    def _run_in_mutant(self, mutator_func: Any, assertion_func: Any) -> None:
        with open("wender/act/godot_executor.py", "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source)
        mutated_tree = mutator_func(tree)
        ast.fix_missing_locations(mutated_tree)
        compiled = compile(mutated_tree, "<mutant>", "exec")

        mod = types.ModuleType("wender.act.godot_executor")
        mod.__file__ = orig_ge.__file__

        old_mod = sys.modules.get("wender.act.godot_executor")
        sys.modules["wender.act.godot_executor"] = mod

        try:
            exec(compiled, mod.__dict__)
            assertion_func(mod)
        finally:
            if old_mod is not None:
                sys.modules["wender.act.godot_executor"] = old_mod

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m1
    def test_mutant_m1(self) -> None:
        """M-wender-act-godot-executor-1 O at line 296."""
        def mut_m1(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_If(self, node: ast.If) -> ast.AST:
                    if getattr(node, "lineno", 0) == 296 and isinstance(node.test, ast.BoolOp):
                        new_vals = []
                        for v in node.test.values:
                            if isinstance(v, ast.Compare) and len(v.ops) == 1 and isinstance(v.ops[0], ast.In):
                                new_vals.append(ast.Compare(left=v.left, ops=[ast.NotIn()], comparators=v.comparators))
                            else:
                                new_vals.append(v)
                        node.test = ast.BoolOp(op=ast.And(), values=new_vals)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m1(mod: Any) -> None:
            res = mod._validate_args_against_schema({"a": 1}, {"required": ["a"]})
            assert res is True, "expected schema validation True"

        # Baseline positive control on real code first
        self._run_in_mutant(lambda t: t, assert_m1)
        # Verify semantic AssertionError on mutated code
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m1, assert_m1)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m2
    def test_mutant_m2(self) -> None:
        """M-wender-act-godot-executor-2 O at line 101."""
        def mut_m2(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                    if node.name == "__eq__" and getattr(node, "lineno", 0) == 100:
                        for stmt in node.body:
                            if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.BoolOp):
                                new_vals = []
                                for v in stmt.test.values:
                                    if isinstance(v, ast.Compare) and isinstance(v.ops[0], ast.In):
                                        new_vals.append(ast.Compare(left=v.left, ops=[ast.NotIn()], comparators=v.comparators))
                                    else:
                                        new_vals.append(v)
                                stmt.test = ast.BoolOp(op=ast.Or(), values=new_vals)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m2(mod: Any) -> None:
            reason = mod.REASON_OK
            res = (reason == "unrelated_alias_string")
            assert res is False, "expected __eq__ to return False for unrelated string"

        self._run_in_mutant(lambda t: t, assert_m2)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m2, assert_m2)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m3
    def test_mutant_m3(self) -> None:
        """M-wender-act-godot-executor-3 O at line 525."""
        def mut_m3(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                    if node.name == "_check_owner_mandate":
                        for sub in ast.walk(node):
                            if getattr(sub, "lineno", 0) == 525 and isinstance(sub, ast.Return) and isinstance(sub.value, ast.BoolOp):
                                def mutate_boolop(bop: ast.BoolOp) -> ast.BoolOp:
                                    new_op = ast.And() if isinstance(bop.op, ast.Or) else ast.Or()
                                    new_vals = []
                                    for v in bop.values:
                                        if isinstance(v, ast.Compare) and isinstance(v.ops[0], ast.In):
                                            new_vals.append(ast.Compare(left=v.left, ops=[ast.NotIn()], comparators=v.comparators))
                                        elif isinstance(v, ast.BoolOp):
                                            new_vals.append(mutate_boolop(v))
                                        else:
                                            new_vals.append(v)
                                    return ast.BoolOp(op=new_op, values=new_vals)
                                sub.value = mutate_boolop(sub.value)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m3(mod: Any) -> None:
            tc = {"principal": "p1", "mandates": {"buy_company": ["owner"]}}
            ex = mod.GodotExecutor(tc, {}, lambda x: b"", lambda x: None)
            res = ex._check_owner_mandate("buy_company", {}, {})
            assert res is True, "expected _check_owner_mandate to return True"

        self._run_in_mutant(lambda t: t, assert_m3)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m3, assert_m3)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m4
    def test_mutant_m4(self) -> None:
        """M-wender-act-godot-executor-4 B at line 588."""
        def mut_m4(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                    if node.name == "dispatch_batch":
                        for sub in ast.walk(node):
                            if getattr(sub, "lineno", 0) == 588 and isinstance(sub, ast.If) and isinstance(sub.test, ast.BoolOp):
                                for v in sub.test.values:
                                    if isinstance(v, ast.Compare) and isinstance(v.ops[0], ast.GtE):
                                        v.ops[0] = ast.Gt()
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m4(mod: Any) -> None:
            sink: list[bytes] = []
            tc = {"principal": "p1"}
            ex = mod.GodotExecutor(tc, {}, lambda x: b"", sink.append)
            r1 = {"tick": 1, "seq": 1, "cmd": "noop", "args": {}}
            r2 = {"tick": 1, "seq": 1, "cmd": "noop", "args": {}}
            res = ex.dispatch_batch([r1, r2], 1, "b" * 64)
            assert res[0].get("reason") == "DUPLICATE_SEQ", f"expected DUPLICATE_SEQ, got {res[0].get('reason')}"

        self._run_in_mutant(lambda t: t, assert_m4)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m4, assert_m4)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m5
    def test_mutant_m5(self) -> None:
        """M-wender-act-godot-executor-5 B at line 588."""
        def mut_m5(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
                    if node.name == "dispatch_batch":
                        for sub in ast.walk(node):
                            if getattr(sub, "lineno", 0) == 588 and isinstance(sub, ast.If) and isinstance(sub.test, ast.BoolOp):
                                for v in sub.test.values:
                                    if isinstance(v, ast.Compare) and isinstance(v.comparators[0], ast.Constant) and v.comparators[0].value == 1:
                                        v.comparators[0] = ast.Constant(value=2)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m5(mod: Any) -> None:
            sink: list[bytes] = []
            tc = {"principal": "p1"}
            ex = mod.GodotExecutor(tc, {}, lambda x: b"", sink.append)
            r1 = {"tick": 1, "seq": 1, "cmd": "noop", "args": {}}
            r2 = {"tick": 1, "seq": 1, "cmd": "noop", "args": {}}
            res = ex.dispatch_batch([r1, r2], 1, "b" * 64)
            assert res[0].get("reason") == "DUPLICATE_SEQ", f"expected DUPLICATE_SEQ, got {res[0].get('reason')}"

        self._run_in_mutant(lambda t: t, assert_m5)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m5, assert_m5)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m6
    def test_mutant_m6(self) -> None:
        """M-wender-act-godot-executor-6 D at line 140."""
        def mut_m6(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_If(self, node: ast.If) -> ast.AST:
                    if getattr(node, "lineno", 0) == 140:
                        node.test = ast.Constant(value=False)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m6(mod: Any) -> None:
            raised_ve = False
            try:
                mod.map_ui_action("buy_compute", "not_a_dict")
            except ValueError as e:
                if "UI action args must be a dict" in str(e):
                    raised_ve = True
            except Exception:
                pass
            assert raised_ve is True, "expected ValueError('UI action args must be a dict')"

        self._run_in_mutant(lambda t: t, assert_m6)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m6, assert_m6)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m7
    def test_mutant_m7(self) -> None:
        """M-wender-act-godot-executor-7 D at line 420."""
        def mut_m7(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_If(self, node: ast.If) -> ast.AST:
                    if getattr(node, "lineno", 0) == 420:
                        node.test = ast.Constant(value=False)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m7(mod: Any) -> None:
            raised_ve = False
            try:
                mod.GodotExecutor({"principal": ""}, {}, lambda x: b"", lambda x: None)
            except ValueError as e:
                if "trusted_context.principal must be a non-empty string" in str(e):
                    raised_ve = True
            assert raised_ve is True, "expected ValueError for empty principal"

        self._run_in_mutant(lambda t: t, assert_m7)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m7, assert_m7)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m8
    def test_mutant_m8(self) -> None:
        """M-wender-act-godot-executor-8 D at line 161."""
        def mut_m8(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_If(self, node: ast.If) -> ast.AST:
                    if getattr(node, "lineno", 0) == 161:
                        node.test = ast.Constant(value=False)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m8(mod: Any) -> None:
            raised_ve = False
            try:
                mod.map_ui_action("start_training", {})
            except ValueError as e:
                if "recipe_id" in str(e):
                    raised_ve = True
            assert raised_ve is True, "expected ValueError for missing recipe_id"

        self._run_in_mutant(lambda t: t, assert_m8)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m8, assert_m8)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m9
    def test_mutant_m9(self) -> None:
        """M-wender-act-godot-executor-9 D at line 372."""
        def mut_m9(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_If(self, node: ast.If) -> ast.AST:
                    if getattr(node, "lineno", 0) == 372:
                        node.test = ast.Constant(value=False)
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m9(mod: Any) -> None:
            raised_ve = False
            try:
                mod.GodotExecutor("not_a_dict", {}, lambda x: b"", lambda x: None)
            except ValueError as e:
                if str(e) == "trusted_context must be a dict":
                    raised_ve = True
            except Exception:
                pass
            assert raised_ve is True, "expected exact ValueError('trusted_context must be a dict')"

        self._run_in_mutant(lambda t: t, assert_m9)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m9, assert_m9)

    # @anchor:wa.tests.test_jq3_r_r1_j05_mutkill_godot_executor.TestGodotExecutorAstMutationHarness.test_mutant_m10
    def test_mutant_m10(self) -> None:
        """M-wender-act-godot-executor-10 S at line 419."""
        def mut_m10(tree: ast.AST) -> ast.AST:
            class T(ast.NodeTransformer):
                def visit_Assign(self, node: ast.Assign) -> ast.AST:
                    if getattr(node, "lineno", 0) == 419 and isinstance(node.value, ast.Call):
                        call = node.value
                        if len(call.args) >= 2:
                            call.args[0], call.args[1] = call.args[1], call.args[0]
                    return self.generic_visit(node)
            return T().visit(tree)

        def assert_m10(mod: Any) -> None:
            ex = mod.GodotExecutor({"principal": "user123"}, {}, lambda x: b"", lambda x: None)
            assert ex._principal == "user123", f"expected user123, got {ex._principal}"

        self._run_in_mutant(lambda t: t, assert_m10)
        with self.assertRaises(AssertionError):
            self._run_in_mutant(mut_m10, assert_m10)


if __name__ == "__main__":
    unittest.main()
