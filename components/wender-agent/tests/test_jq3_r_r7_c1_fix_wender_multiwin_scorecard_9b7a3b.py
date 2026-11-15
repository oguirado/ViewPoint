"""Offline regression: landed multi-window fixtures and catalogued AST mutations.

A private-module variant is compiled in RAM; never writes mutated sources or
imports pre-merge siblings. Original legacy assertions are exercised by ID.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import copy
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
import random
import re
import sys
import time
import types
import unittest

import numpy as np

from wender.learn import multiwin_scorecard as real

SOURCE_SHA256 = "c0f74d0faf4387c5873068c7439ac18719bf33cf3fc62d61a64f2835cabf646a"
SEED_O = 0xB150C4
SEED_B = 0xB40017
CASES = 64
SHRINK_LIMIT = 32
TIMEOUT_SECONDS = 2.0
SOURCE_PATH = "wender/learn/multiwin_scorecard.py"
C1_FILE = "tests/test_jq2a_m7_c1_replay_scorecard_implementation.py"
C1_PRE_EDIT_SHA256 = "63ac569ede6732187481f48f45d4d1443c633f824bf5f68b5a42c27063c67de4"
C1_POST_EDIT_SHA256 = "51eba7b194292043063f1454b9ee3cec24e5bb42b535f1c1c14f92fbbc500fc0"
C1_METHOD_DIGESTS = {
    "TestMultiwinScorecard.test_missing_duplicate_foreign_rows_and_seal_mutants": (
        "c0a6b69fa1e67227173a2938eea0e17e867d10baf43611be7f4b283ce1a83e39",
        "2dfbc946e8d3c2eccfd01ab149346e8cd1b4a4c23f5770272938bd4b2b8069ef"),
    "TestMultiwinScorecard.test_fault_exception_quarantine_and_acknowledged_new_generation_recovery": (
        "a2956a8b9bd16e083a69a2c7cce98fa1cb735dc44f7f245fb54a25aa078774da",
        "c90e137b0643965e1cc145945bfa4506999e0607f9093b86472f14b71dd54799"),
    "TestMultiwinScorecard.test_capsule_child_share_resource_and_timeout_fences_destination": (
        "6e9849033e233c29b72e956cf9077d54ac8fccecd291cf122dc75ff30a18d145",
        "4266ed0e6cf910a8f99f80fcb807be5ec892bf65d712ac3b14489ef69be582c7"),
    "TestMultiwinScorecard.test_replayed_or_stale_recovery_cannot_clear_a_newer_fence": (
        "33a4cb07efa2f4afc640d139371ba9724d84819a87c84d8964d048db1abbe966",
        "9ace0a1713f469a2e941abee780ea5a61f21d5c079451195cca25ca918919199"),
}
LEGACY_FILE = "tests/test_jq2a_m7_j2_cross_window_bleed_and_safety_audit.py"
LEGACY_PRE_EDIT_SHA256 = "4c805e7c095ee2603d64701951fd6c3b8aeeeacbf1f0f9d7a063c79d08ca0bfa"
LEGACY_POST_EDIT_SHA256 = "b7487d4fa905c15f40ec704030fa10f2b81ace7974d187876ccef909c8682448"
METHOD_DIGESTS = {
    "TestSharedRecordSchemasAndContracts.test_record_schemas_and_field_order": (
        "1751f2ff591a5f3791bd42b2faee6a80a159d31567fe9e844a974c5da491f19b",
        "5586bc0a43bd0069d6d2c62df66cb4657261b8b45f8e927955467f3091bb70eb"),
    "TestGateGM72CrossWindowBleedAndSafetyAudit.test_gate_g_m7_2_score_windows_positive_control": (
        "d674e77c57eba165e075fbcbadd84c1214b0441370fab4cdf5cd89786c74bb64",
        "f181a94aa5469451ddf80566ecef14574f2752ea6b5b5e6784add2426d993379"),
    "TestGateGM72CrossWindowBleedAndSafetyAudit.test_gate_g_m7_2_score_windows_injected_bleed_fails": (
        "eb7d47c2fa968fef6aa8617b256e667c220dd13728c4b097f857862c38d21a92",
        "b5f31dfca76960f713ffadd1d5025e9b22258046c65f02bc005511b2a58f3dd7"),
    "TestMultiwinScorecardOwnerAPIs.test_run_trial_heterogeneous_targets_contract": (
        "81d6fdd2c9eb79645e3ad39d5ebe83aab63d8b0a90031d82ccac1d67b57b5fb0",
        "f8e590afee34c1df767e76a049b3dece4ff15c62976ae499791470d8adb7530a"),
}

CATALOGUE = (
    {
        "id": "M-wender-learn-multiwin-scorecard-1",
        "source": SOURCE_PATH,
        "function": "score_windows",
        "line": 1109,
        "operator": "O",
        "type": ast.BoolOp,
        "selector": "generator element at line 1109",
        "original": (
            'r.ops.get("admitted") is True and _terminal(r.terminal) in _SUCCESS\n'
            '                     and r.terminal == r.expected_terminal'
        ),
    },
    {
        "id": "M-wender-learn-multiwin-scorecard-2",
        "source": SOURCE_PATH,
        "function": "_validate_scenario",
        "line": 434,
        "operator": "B",
        "type": ast.Constant,
        "selector": "body[13].body[17].test.values[5].operand.slice",
        "original": "1",
    },
)

ORIGINAL_METHOD_IDS = (
    "tests.test_jq2a_m7_c1_replay_scorecard_implementation."
    "TestMultiwinScorecard.test_frame_identity_torn_pixels_and_false_negative_controls",
    "tests.test_jq2a_m7_c1_replay_scorecard_implementation."
    "TestMultiwinScorecard.test_missing_duplicate_foreign_rows_and_seal_mutants",
    "tests.test_jq2a_m7_c1_replay_scorecard_implementation."
    "TestMultiwinScorecard.test_fault_exception_quarantine_and_acknowledged_new_generation_recovery",
    "tests.test_jq2a_m7_c1_replay_scorecard_implementation."
    "TestMultiwinScorecard.test_capsule_child_share_resource_and_timeout_fences_destination",
    "tests.test_jq2a_m7_c1_replay_scorecard_implementation."
    "TestMultiwinScorecard.test_replayed_or_stale_recovery_cannot_clear_a_newer_fence",
    "tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit."
    "TestSharedRecordSchemasAndContracts.test_record_schemas_and_field_order",
    "tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit."
    "TestGateGM72CrossWindowBleedAndSafetyAudit.test_gate_g_m7_2_score_windows_positive_control",
    "tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit."
    "TestGateGM72CrossWindowBleedAndSafetyAudit.test_gate_g_m7_2_score_windows_injected_bleed_fails",
    "tests.test_jq2a_m7_j2_cross_window_bleed_and_safety_audit."
    "TestMultiwinScorecardOwnerAPIs.test_run_trial_heterogeneous_targets_contract",
)

O_COMPARE = {
    ast.Lt: ast.Gt, ast.Gt: ast.Lt,
    ast.LtE: ast.GtE, ast.GtE: ast.LtE,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
O_BOOL = {ast.And: ast.Or, ast.Or: ast.And}
O_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add,
         ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}
B_COMPARE = {ast.Lt: ast.LtE, ast.LtE: ast.Lt,
             ast.Gt: ast.GtE, ast.GtE: ast.Gt}


class AnchorError(AssertionError):
    """Source drift, ambiguity, or an invalid mutation selector."""


def _exchange(operator, mapping):
    replacement = mapping.get(type(operator))
    if replacement is None:
        raise AnchorError("unsupported operator: " + type(operator).__name__)
    return replacement()


def _apply_mutation(selected, operation):
    """Mutation operations O/B/D/S; a compare selector is never a constant."""
    if operation == "O":
        if isinstance(selected, ast.Compare):
            if len(selected.ops) != 1:
                raise AnchorError("comparison is not uniquely selected")
            selected.ops[0] = _exchange(selected.ops[0], O_COMPARE)
        elif isinstance(selected, ast.BoolOp):
            selected.op = _exchange(selected.op, O_BOOL)
        elif isinstance(selected, ast.BinOp):
            selected.op = _exchange(selected.op, O_BIN)
        else:
            raise AnchorError("O requires Compare, BoolOp or BinOp")
    elif operation == "B":
        if isinstance(selected, ast.Compare):
            if len(selected.ops) != 1:
                raise AnchorError("boundary comparison must be unique")
            selected.ops[0] = _exchange(selected.ops[0], B_COMPARE)
        elif isinstance(selected, ast.Constant) and type(selected.value) is int:
            selected.value += 1
        else:
            raise AnchorError("B requires a comparison or exact-int Constant")
    elif operation == "D":
        if isinstance(selected, ast.If):
            if not any(isinstance(stmt, ast.Raise) for stmt in selected.body):
                raise AnchorError("D-If must guard an actual raise")
            selected.test = ast.Constant(value=False)
        elif isinstance(selected, ast.Assert):
            selected.test = ast.Constant(value=True)
        else:
            raise AnchorError("D requires a raising If or an Assert")
    elif operation == "S":
        if not isinstance(selected, ast.Call):
            raise AnchorError("S requires Call")
        positions = [i for i, arg in enumerate(selected.args)
                     if not isinstance(arg, ast.Starred)]
        if len(positions) < 2:
            raise AnchorError("S needs two non-starred positional arguments")
        pair = next(((i, j) for x, i in enumerate(positions)
                     for j in positions[x + 1:]
                     if ast.dump(selected.args[i]) != ast.dump(selected.args[j])), None)
        if pair is None:
            raise AnchorError("S arguments must be distinct")
        i, j = pair
        selected.args[i], selected.args[j] = selected.args[j], selected.args[i]
    else:
        raise AnchorError("unknown mutation operation " + operation)


def _source_and_ast():
    raw = Path(real.__file__).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise AnchorError("bound original source SHA-256 changed")
    text = raw.decode("utf-8")
    return text, ast.parse(text, filename=str(real.__file__))


def _select(tree, source, item):
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == item["function"]]
    if len(functions) != 1:
        raise AnchorError("qualified function missing or ambiguous: " + item["function"])
    function = functions[0]
    if item["selector"].startswith("body["):
        # Every index is part of the catalogued structural selector.
        selected = function.body[13].body[17].test.values[5].operand.slice
        matches = [selected]
    else:
        matches = [n for n in ast.walk(function)
                   if isinstance(n, item["type"]) and getattr(n, "lineno", -1) == item["line"]
                   and ast.get_source_segment(source, n) == item["original"]]
    if len(matches) != 1:
        raise AnchorError("source anchor absent or ambiguous: " + item["id"])
    selected = matches[0]
    if not isinstance(selected, item["type"]):
        raise AnchorError("structural selector has wrong AST type")
    if getattr(selected, "lineno", None) != item["line"]:
        raise AnchorError("structural selector moved")
    if ast.get_source_segment(source, selected) != item["original"]:
        raise AnchorError("original source text mismatch")
    return selected


def _mutated_tree(item):
    original, tree = _source_and_ast()
    selected = _select(tree, original, item)
    _apply_mutation(selected, item["operator"])
    ast.fix_missing_locations(tree)
    return tree


@contextmanager
def _private_module(tree, marker):
    """Allow dataclass annotation resolution and restore all interpreter state."""
    name = "wender.learn._jq3_r7_scorecard_" + marker
    spec = importlib.util.spec_from_loader(name, loader=None)
    module = types.ModuleType(name)
    module.__package__ = "wender.learn"
    module.__file__ = real.__file__
    module.__spec__ = spec
    absent = object()
    previous = sys.modules.get(name, absent)
    old_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        sys.modules[name] = module
        exec(compile(tree, real.__file__, "exec", dont_inherit=True),
             module.__dict__)
        yield module
    finally:
        sys.dont_write_bytecode = old_bytecode
        if previous is absent:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def _trial_rows(module, case, *, all_success=False):
    """Schema-valid offline evidence with an authentic whole-trial seal."""
    kind = case["kind"]
    other = "godot_window" if kind != "godot_window" else "cdp_tab"
    handles = (
        module.Handle(kind, case["tag"] + "-a", case["generation"]),
        module.Handle(other, case["tag"] + "-b", case["generation"] + 1),
    )
    keys = [list(module.handle_key(h)) for h in handles]
    statuses = ("SUCCESS", "SUCCESS" if all_success else "TIMEOUT")
    actions = {
        "action-" + str(i): {"handle": keys[i], "expected": status, "cost": 1}
        for i, status in enumerate(statuses)
    }
    rows = []
    for i, (h, status) in enumerate(zip(handles, statuses)):
        admitted = status == "SUCCESS"
        ops = {
            "counts": {"pixels": 1}, "expected_counts": {"pixels": 1},
            "isolated_match": True, "pixel_mismatches": 0,
            "torn": False, "drop_expected": False,
            "action_id": "action-" + str(i), "admitted": admitted,
            "scheduler_attempted": admitted,
            "stall_reason": None if admitted else "deadline_expired",
            "dispatch": ({
                "eligible": True,
                "telemetry": {"wait_opportunities": 0, "queue_depth": 1,
                              "fairness_cost": 1, "quantum": 1, "urgent_cap": 1},
            } if admitted else None),
        }
        rows.append(module.WindowEvidence(
            h, "same-scene", 0, i, 1, 0, 0, 1,
            ops, True, False, True, status, status, "", (),
        ))
    manifest = {
        "version": "multiwin-scorecard-v1",
        "scenario": "seeded-offline-scorecard",
        "seed": case["case"],
        "targets": keys,
        "identities": [list(module._identity(r)) for r in rows],
        "actions": actions,
        "gates": {g: {"status": "PASS", "evidence": "offline-fixture"}
                  for g in module.PREREQUISITE_GATES},
        "replay_match": True,
        "replay_sha256": "e" * 64,
        "repeat_sha256": "e" * 64,
        "isolated_support": len(rows),
        "elapsed_ns": case["elapsed_ns"],
        "recoveries": [],
    }
    rows = tuple(replace(r, ops={**r.ops, "_trial": manifest}) for r in rows)
    seal = module._seal(rows, manifest)
    return tuple(replace(r, seal=seal) for r in rows)


def _bad_width_scenario(module, case):
    handle = module.Handle(case["kind"], case["tag"], case["generation"])
    rgb = np.zeros((case["height"], 0, 3), dtype=np.uint8)
    event = {
        "handle": handle, "scene": "empty-width", "epoch": 0,
        "frame_seq": 0, "at_ns": 1, "rgb": rgb,
        "truth": np.zeros((case["height"], 0), dtype=np.bool_),
    }
    scenario = module.Scenario("strict-empty-width", (handle,), (event,), {})
    adapters = {module.handle_key(handle): object()}
    return scenario, adapters


def _seeded_cases(seed):
    rng = random.Random(seed)
    kinds = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
    for index in range(CASES):
        yield {
            "case": index, "tag": "f-" + str(index) + "-" + str(rng.randrange(100000)),
            "kind": kinds[rng.randrange(len(kinds))],
            "generation": rng.randrange(1, 10000),
            "elapsed_ns": rng.randrange(1, 20000),
            "height": rng.randrange(1, 9),
        }


def _method_blocks(source):
    """Stable pre/post inventory: decorators and definition through next sibling."""
    lines = source.split("\n")
    found, cls, i = {}, "", 0
    while i < len(lines):
        match_class = re.match(r"^class (\w+)\(", lines[i])
        if match_class:
            cls = match_class.group(1)
        match = re.match(r"^    def (test_\w+)\(", lines[i])
        if match is None:
            i += 1
            continue
        start = i
        while start > 0 and lines[start - 1].startswith("    @"):
            start -= 1
        end = i + 1
        while end < len(lines) and not re.match(
                r"^(?:    (?:@|def )|class |if __name__)", lines[end]):
            end += 1
        body = "\n".join(lines[start:end]).rstrip() + "\n"
        found[cls + "." + match.group(1)] = hashlib.sha256(
            body.encode("utf-8")).hexdigest()
        i = end
    return found


class TestOriginalMethodRegression(unittest.TestCase):
    def test_method_inventory_matches_pre_edit_and_reviewed_post_edit(self):
        current = Path(real.__file__).resolve().parents[2] / LEGACY_FILE
        source_bytes = current.read_bytes()
        self.assertNotEqual(hashlib.sha256(source_bytes).hexdigest(),
                            LEGACY_PRE_EDIT_SHA256)
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
                         LEGACY_POST_EDIT_SHA256)
        blocks = _method_blocks(source_bytes.decode("utf-8"))
        self.assertEqual(len(blocks), 12)
        j2_identifiers = {
            method_id.rsplit(".", 2)[-2] + "." + method_id.rsplit(".", 1)[-1]
            for method_id in ORIGINAL_METHOD_IDS
            if ".test_jq2a_m7_j2_" in method_id
        }
        self.assertEqual(set(METHOD_DIGESTS), j2_identifiers)
        for method, (before, after) in METHOD_DIGESTS.items():
            self.assertNotEqual(before, after, method)
            self.assertEqual(blocks[method], after, method)

        c1_file = Path(real.__file__).resolve().parents[2] / C1_FILE
        c1_bytes = c1_file.read_bytes()
        self.assertNotEqual(hashlib.sha256(c1_bytes).hexdigest(), C1_PRE_EDIT_SHA256)
        self.assertEqual(hashlib.sha256(c1_bytes).hexdigest(), C1_POST_EDIT_SHA256)
        c1_blocks = _method_blocks(c1_bytes.decode("utf-8"))
        self.assertEqual(len(c1_blocks), 24)
        affected_c1 = {
            method_id.rsplit(".", 2)[-2] + "." + method_id.rsplit(".", 1)[-1]
            for method_id in ORIGINAL_METHOD_IDS
            if ".test_jq2a_m7_c1_" in method_id
        }
        self.assertEqual(
            affected_c1,
            set(C1_METHOD_DIGESTS) | {
                "TestMultiwinScorecard.test_frame_identity_torn_pixels_and_false_negative_controls"})
        for method, (before, after) in C1_METHOD_DIGESTS.items():
            self.assertNotEqual(before, after, method)
            self.assertEqual(c1_blocks[method], after, method)
    def test_every_changed_legacy_method_runs_by_exact_id(self):
        loader = unittest.TestLoader()
        for method_id in ORIGINAL_METHOD_IDS:
            with self.subTest(method=method_id):
                suite = loader.loadTestsFromName(method_id)
                self.assertEqual(suite.countTestCases(), 1, method_id)
                result = unittest.TestResult()
                suite.run(result)
                self.assertEqual(result.testsRun, 1, method_id)
                self.assertEqual(result.failures, [], (method_id, result.failures))
                self.assertEqual(result.errors, [], (method_id, result.errors))
                self.assertEqual(result.skipped, [], (method_id, result.skipped))
                self.assertEqual(result.expectedFailures, [],
                                 (method_id, result.expectedFailures))
                self.assertEqual(result.unexpectedSuccesses, [],
                                 (method_id, result.unexpectedSuccesses))


class TestMutatorOperators(unittest.TestCase):
    def test_o_maps_all_comparators_boolean_and_arithmetic(self):
        for old, new in O_COMPARE.items():
            node = ast.Compare(left=ast.Constant(1), ops=[old()],
                               comparators=[ast.Constant(2)])
            _apply_mutation(node, "O")
            self.assertIsInstance(node.ops[0], new)
        for old, new in O_BOOL.items():
            node = ast.BoolOp(op=old(), values=[ast.Constant(True), ast.Constant(False)])
            _apply_mutation(node, "O")
            self.assertIsInstance(node.op, new)
        for old, new in O_BIN.items():
            node = ast.BinOp(left=ast.Constant(4), op=old(), right=ast.Constant(2))
            _apply_mutation(node, "O")
            self.assertIsInstance(node.op, new)

    def test_boundary_delete_and_swap_have_distinct_structural_semantics(self):
        for old, new in B_COMPARE.items():
            node = ast.Compare(left=ast.Constant(1), ops=[old()],
                               comparators=[ast.Constant(2)])
            _apply_mutation(node, "B")
            self.assertIsInstance(node.ops[0], new)
        integer = ast.Constant(1)
        _apply_mutation(integer, "B")
        self.assertEqual(integer.value, 2)
        with self.assertRaises(AnchorError):
            _apply_mutation(ast.Constant(True), "B")
        raising = ast.If(test=ast.Constant(True), body=[ast.Raise(exc=None)],
                         orelse=[])
        _apply_mutation(raising, "D")
        self.assertIs(raising.test.value, False)
        guard = ast.Assert(test=ast.Constant(False), msg=None)
        _apply_mutation(guard, "D")
        self.assertIs(guard.test.value, True)
        call = ast.Call(func=ast.Name(id="f", ctx=ast.Load()),
                        args=[ast.Name(id="left", ctx=ast.Load()),
                              ast.Name(id="right", ctx=ast.Load())], keywords=[])
        _apply_mutation(call, "S")
        self.assertEqual([x.id for x in call.args], ["right", "left"])


class TestTornFrameTransportRegression(unittest.TestCase):
    def test_torn_digest_is_post_constructor_corruption_and_fences_actor(self):
        from tests.test_jq2a_m7_c1_replay_scorecard_implementation import _key, _scenario

        scenario, adapters = _scenario(count=1)
        handle = scenario.targets[0]
        adapter = adapters[_key(handle)]
        adapter.stream_mode = "torn"
        event = scenario.events[0]
        pristine = real.FrameRef.from_rgb(handle, event["epoch"], event["frame_seq"],
                                           event["rgb"])
        corrupted = adapter.stream.deliver(pristine)
        self.assertIsNot(corrupted, pristine)
        self.assertEqual(pristine.digest, hashlib.sha256(pristine.rgb.tobytes()).hexdigest())
        self.assertNotEqual(corrupted.digest,
                            hashlib.sha256(corrupted.rgb.tobytes()).hexdigest())
        report = real.run_trial(scenario, seed=7, clock=real.FakeClock(), adapters=adapters)
        card = real.score_windows(report.rows)
        self.assertFalse(card.passed)
        self.assertIn("torn_frame", card.violations)
        self.assertTrue(any(row.ops.get("torn") for row in report.rows))
        self.assertFalse(any(call[0] == "execute" for call in adapter.calls))


class TestCatalogueMutations(unittest.TestCase):
    def _assert_operator_case(self, module, case):
        card = module.score_windows(_trial_rows(module, case))
        self.assertTrue(card.passed, card.violations)
        throughput = card.metrics["terminal_completeness"]["simulated_throughput"]
        self.assertEqual(throughput["numerator"], 1)
        self.assertEqual(throughput["denominator"], case["elapsed_ns"])

    def _assert_boundary_case(self, module, case):
        scenario, adapters = _bad_width_scenario(module, case)
        with self.assertRaises(module.EvidenceError):
            module._validate_scenario(scenario, 11, module.FakeClock(), adapters)

    def _invoke(self, callback, module, case):
        start = time.perf_counter()
        try:
            callback(module, case)
        except AssertionError as exc:
            status, detail = "ASSERTION", str(exc)
        except Exception as exc:
            status, detail = "ERROR", type(exc).__name__ + ": " + str(exc)
        else:
            status, detail = "PASS", ""
        elapsed = time.perf_counter() - start
        if elapsed > TIMEOUT_SECONDS:
            return "TIMEOUT", f"VM {elapsed:.3f}s", elapsed
        return status, detail, elapsed

    def _prove(self, catalogue_item, seed, callback):
        try:
            tree = _mutated_tree(catalogue_item)
        except (SyntaxError, AnchorError) as exc:
            self.fail("MISSING_ANCHOR: " + str(exc))
        cases = list(_seeded_cases(seed))
        self.assertEqual(len(cases), CASES)
        control = []
        for case in cases:
            status, detail, elapsed = self._invoke(callback, real, case)
            if status != "PASS":
                self.fail(f"FAILED_CONTROL {catalogue_item['id']} seed={seed} "
                          f"case={case} status={status} detail={detail}")
            control.append(elapsed)
        try:
            with _private_module(tree, catalogue_item["id"][-1]) as mutant:
                statuses = {"ASSERTION": 0, "PASS": 0,
                            "TIMEOUT": 0, "ERROR": 0}
                first_kill = None
                vm_elapsed = 0.0
                for case in cases:
                    status, detail, elapsed = self._invoke(callback, mutant, case)
                    vm_elapsed += elapsed
                    statuses[status] += 1
                    if status == "ASSERTION" and first_kill is None:
                        first_kill = case
                if statuses["ERROR"]:
                    disposition = "RUNTIME_ERROR"
                elif statuses["TIMEOUT"]:
                    disposition = "TIMEOUT"
                elif statuses["PASS"]:
                    disposition = "SURVIVOR"
                elif statuses["ASSERTION"] == CASES:
                    disposition = "KILL"
                else:
                    disposition = "FAILED_CONTROL"
                minimal_case = first_kill
                attempts = 0
                if disposition == "KILL":
                    # At most 32 validity-preserving reductions; never turn
                    # empty width into a positive width or remove the fault row.
                    smaller = dict(minimal_case)
                    smaller.update(tag="f", generation=1, elapsed_ns=1, height=1)
                    for _ in range(SHRINK_LIMIT):
                        attempts += 1
                        status_real, _, _ = self._invoke(callback, real, smaller)
                        status_mutant, _, _ = self._invoke(callback, mutant, smaller)
                        if status_real == "PASS" and status_mutant == "ASSERTION":
                            minimal_case = smaller
                        break
        except (SyntaxError, ImportError) as exc:
            self.fail("COMPILE_OR_IMPORT_ERROR: " + type(exc).__name__ + ": " + str(exc))
        self.assertEqual(
            disposition, "KILL",
            (catalogue_item["id"], disposition, f"seed={seed}",
             f"first_case={cases[0]}", f"minimal_fixture={minimal_case}",
             f"shrink_attempts={attempts}", f"statuses={statuses}", f"VM_seconds={vm_elapsed:.6f}",
             "PROVEN_EQUIVALENT requires independent proof, not a surviving test"),
        )

    def test_catalogue_o_success_requires_admission_and_terminal_match(self):
        self._prove(CATALOGUE[0], SEED_O, self._assert_operator_case)

    def test_catalogue_b_rejects_empty_rgb_width_at_exact_selector(self):
        self._prove(CATALOGUE[1], SEED_B, self._assert_boundary_case)

    def test_full_seal_missing_support_identity_and_resealed_order(self):
        case = next(_seeded_cases(SEED_O))
        rows = _trial_rows(real, case, all_success=True)
        self.assertTrue(real.score_windows(rows).passed)
        self.assertEqual(real.score_windows(rows).metrics["pooled_confusion"]["numerator"], 4)
        self.assertFalse(real.score_windows(rows[:-1]).passed)
        self.assertIn("missing_or_foreign_rows", real.score_windows(rows[:-1]).violations)
        self.assertFalse(real.score_windows((replace(rows[0], tp=2), rows[1])).passed)
        self.assertIn(
            "missing_mixed_or_invalid_seal",
            real.score_windows((replace(rows[0], tp=2), rows[1])).violations,
        )
        swapped = rows[::-1]
        digest = real._seal(swapped, swapped[0].ops["_trial"])
        swapped = tuple(replace(row, seal=digest) for row in swapped)
        self.assertTrue(real.score_windows(swapped).passed)
        empty = real.score_windows(())
        self.assertFalse(empty.passed)
        self.assertIsNone(empty.metrics["frame_exactness"]["denominator"])
