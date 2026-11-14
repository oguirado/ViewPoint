"""G-R7-5: exact original-method replay and the two named ramws contrasts.

Only the four owned fixture modules are loaded, by exact TestLoader IDs.  The
broad ramws/C1/C2 suites are not imported.  Mutated Python exists only as AST
and code objects in fresh registered private modules; production files stay
read-only.  Every mutation runs the same independent assertion on real ramws
first.  Invocation/import errors are never counted as behavioral kills.
"""

import ast
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import io
import itertools
import json
from pathlib import Path
import random
import signal
import sys
import threading
import types
import unittest
from unittest import mock


# Pre-edit inventory: every original method in each owned legacy module.
# Shared imports/helpers were repaired, so all 30 methods are affected.
ORIGINAL_METHODS = {
    "tests.test_jq2a_m4_j1_window_stream_bank_lifecycle": {
        "TestWindowStreamBankLifecycle": (
            "test_identical_geometry_different_handles_separate_rings",
            "test_close_and_respawn_target_and_resize_sibling",
            "test_gate_g_m4_1_closed_incarnation_negative_mutant_vs_positive_control",
            "test_transactional_quota_admission_and_rollback",
            "test_canonical_handle_ordering_for_multi_stream_lock",
            "test_step_hook_invocation_and_copy_revalidation",
        ),
    },
    "tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock": {
        "TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock": (
            "test_valid_crop_byte_exact_per_window_signatures",
            "test_negative_and_overflowing_crop_bounds_refused",
            "test_odd_and_changing_seqlock_sequence_refused",
            "test_unsupported_source_mode_and_target_kinds_refused",
            "test_quota_admission_transactional_rollback",
        ),
    },
    "tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads": {
        "TestZeroTornConcurrentWindowReads": (
            "test_positive_control_stable_ram_slots_delivered",
            "test_gate_g_m4_4_step_hook_torn_frame_detected_and_dropped",
            "test_inv_m4_1_separate_single_writer_rings_and_supported_kinds",
            "test_inv_m4_1_generation_change_advances_epoch",
            "test_inv_m4_2_transactional_quota_admission_exact_accounting",
            "test_inv_m4_3_bounded_retries_yield_explicit_drops_no_sleep",
            "test_canonical_handle_order_and_lock_order_checks",
            "test_record_classes_field_order_and_immutability",
        ),
    },
    "tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order": {
        "TestCanonicalLockOrderHarness": (
            "test_check_lock_order_predicate_canonical_cases",
            "test_negative_mutant_deadlock_on_reversed_caller_order",
            "test_positive_control_rejection_of_reversed_handle_order",
            "test_positive_control_concurrent_canonical_order_safety",
            "test_dataclass_field_order_and_immutability",
        ),
        "TestProductionStreamBankConformance": (
            "test_prod_owner_module_presence",
            "test_prod_check_lock_order_predicate",
            "test_prod_stream_bank_group_operations_lock_order",
            "test_prod_step_hook_invocation_before_and_after_copy",
            "test_prod_resize_epoch_advancement_and_stale_token_rejection",
            "test_prod_no_actor_or_focus_locks_acquired",
        ),
    },
}

RESULT_CATEGORIES = (
    "failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses",
)
REPLAY_SEED = 0x50968953
_PRIVATE_PREFIX = "vadapt._jq3_r7_ramws_"
_MODULE_SERIAL = itertools.count()
_RETURN_SHAPE = ast.dump(ast.parse("return (t - f) * 1024").body[0],
                         include_attributes=False)
SITES = {
    "M-vadapt-ramws-1": "_swap_used.return.value.left.op: Sub -> Add",
    "M-vadapt-ramws-2": "_swap_used.return.value.right: Constant(1024) -> Constant(1025)",
}


def _exact_ids(module_name):
    return tuple(f"{module_name}.{class_name}.{method_name}"
                 for class_name, methods in ORIGINAL_METHODS[module_name].items()
                 for method_name in methods)


class RepairedOriginalMethods(unittest.TestCase):
    """Each family is a new regression which failed on the pre-edit snapshot."""

    def _run_original_family(self, module_name):
        old_bytecode = sys.dont_write_bytecode
        threads_before = set(threading.enumerate())
        completed = []
        try:
            sys.dont_write_bytecode = True
            module = importlib.import_module(module_name)
            expected_ids = _exact_ids(module_name)
            discovered_ids = []
            for value in vars(module).values():
                if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                        and value.__module__ == module_name):
                    for name in unittest.TestLoader().getTestCaseNames(value):
                        discovered_ids.append(f"{module_name}.{value.__name__}.{name}")
            # Missing/renamed methods or an unlisted added test cannot silently
            # bypass the exact-ID replay after a shared fixture changes.
            self.assertEqual(sorted(discovered_ids), sorted(expected_ids))
            for test_id in expected_ids:
                with self.subTest(original_method=test_id):
                    loader = unittest.TestLoader()
                    suite = loader.loadTestsFromName(test_id)
                    self.assertEqual(loader.errors, [])
                    self.assertEqual(suite.countTestCases(), 1)
                    case, = suite
                    self.assertEqual(case.id(), test_id)
                    method = getattr(case, case._testMethodName)
                    for owner in (*type(case).__mro__, method):
                        self.assertFalse(getattr(owner, "__unittest_skip__", False))
                        self.assertFalse(getattr(owner, "__unittest_expecting_failure__", False))
                    inner = unittest.TestResult()
                    suite.run(inner)
                    self.assertEqual(inner.testsRun, 1)
                    for category in RESULT_CATEGORIES:
                        self.assertEqual(getattr(inner, category), [],
                                         f"{test_id}: {category}")
                    completed.append(test_id)
        finally:
            sys.dont_write_bytecode = old_bytecode
            leaked = [t.name for t in threading.enumerate()
                      if t not in threads_before and t.is_alive()]
            self.assertEqual(leaked, [], "original-method replay leaked threads")
        self.assertEqual(completed, list(_exact_ids(module_name)))
        print(json.dumps({"original_module": module_name,
                          "original_methods_passed": len(completed),
                          "skips": 0, "expected_failures": 0}, sort_keys=True))

    def test_repaired_lifecycle_original_methods(self):
        self._run_original_family("tests.test_jq2a_m4_j1_window_stream_bank_lifecycle")

    def test_repaired_crop_original_methods(self):
        self._run_original_family("tests.test_jq2a_m4_j2_multi_x11_per_window_crop_and_seqlock")

    def test_repaired_concurrent_read_original_methods(self):
        self._run_original_family("tests.test_jq2a_m4_j4_zero_torn_concurrent_window_reads")

    def test_repaired_lock_order_original_methods(self):
        self._run_original_family("tests.test_jq2a_m4_j6_stream_bank_canonical_lock_order")


def _swap_cases():
    """Build expectations from allocated KiB, independently of t-f parsing.

    Cases are sorted by (total, free), retaining the smallest *sampled*
    counterexample for deterministic replay.  No global minimality is claimed.
    """
    cases = {(0, 0, 0), (1, 0, 1024), (1, 1, 0), (9, 4, 5120),
             (16384, 4096, 12582912)}
    rng = random.Random(REPLAY_SEED)
    for _ in range(128):
        used_kib = rng.randrange(0, 1 << 33)
        free_kib = rng.randrange(0, 1 << 33)
        cases.add((used_kib + free_kib, free_kib, used_kib << 10))
    return tuple(sorted(cases))


def _observe_swap(module, cases):
    """Only this observation phase invokes production code or constructs fakes.

    Its exceptions, including AssertionError, cannot be mutation kills.
    The pure behavioral assertion is deliberately a separate phase below.
    """
    actual = []
    for total, free, _ in cases:
        text = (f"MemTotal: 999999 kB\nSwapFree: {free} kB\n"
                f"Cached: 123 kB\nSwapTotal: {total} kB\n")
        streams = []

        def fake_open(path, *, encoding):
            if path != "/proc/meminfo" or encoding != "ascii":
                raise RuntimeError("unexpected meminfo fixture access")
            stream = io.StringIO(text)
            streams.append(stream)
            return stream

        try:
            with mock.patch.object(module, "open", fake_open, create=True):
                value = module._swap_used()
            if len(streams) != 1 or not streams[0].closed:
                raise RuntimeError("meminfo observation did not close its only stream")
            actual.append(value)
        finally:
            for stream in streams:
                stream.close()
    return tuple(actual)


def _assert_swap_observation(actual, cases):
    """The identical, single-condition behavioral assertion for real and mutant."""
    expected = tuple(case[2] for case in cases)
    if actual != expected:
        counterexample = _smallest_counterexample(actual, cases)
        raise AssertionError(json.dumps({"seed": REPLAY_SEED,
                                         "smallest_sampled_counterexample": counterexample},
                                        sort_keys=True))


def _smallest_counterexample(actual, cases):
    for (total, free, expected), observed in zip(cases, actual):
        if observed != expected:
            return {"total_kib": total, "free_kib": free,
                    "expected_bytes": expected, "actual_bytes": observed}
    return {"expected_count": len(cases), "actual_count": len(actual)}


class _AnchorError(ValueError):
    def __init__(self, status, count):
        self.status, self.count = status, count
        super().__init__(f"{status}: {count}")


def _mutated_tree(source, site):
    tree = ast.parse(source)
    scopes = [node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name == "_swap_used"]
    if len(scopes) != 1:
        raise _AnchorError("missing_anchor" if not scopes else "nonunique_anchor", len(scopes))
    # Qualified top-level scope + unique complete expression + structural
    # selector.  No source-wide ordinal or substring replacement is used.
    anchors = [node for node in scopes[0].body
               if isinstance(node, ast.Return)
               and ast.dump(node, include_attributes=False) == _RETURN_SHAPE]
    if len(anchors) != 1:
        raise _AnchorError("missing_anchor" if not anchors else "nonunique_anchor", len(anchors))
    expression = anchors[0].value
    if site == "M-vadapt-ramws-1":
        expression.left.op = ast.Add()
    elif site == "M-vadapt-ramws-2":
        expression.right = ast.copy_location(ast.Constant(1025), expression.right)
    else:
        raise ValueError("unnamed mutation site")
    return ast.fix_missing_locations(tree)


@contextmanager
def _private_module(tree, source_file):
    """Register before exec (including package/dataclass import semantics)."""
    name = f"{_PRIVATE_PREFIX}{next(_MODULE_SERIAL)}"
    parent = importlib.import_module("vadapt")
    short_name = name.rpartition(".")[2]
    missing = object()
    previous_module = sys.modules.get(name, missing)
    previous_attribute = getattr(parent, short_name, missing)
    previous_bytecode = sys.dont_write_bytecode
    module = types.ModuleType(name)
    module.__file__ = source_file
    module.__package__ = "vadapt"
    module.__spec__ = importlib.util.spec_from_loader(name, loader=None,
                                                    origin=source_file)
    try:
        sys.dont_write_bytecode = True
        sys.modules[name] = module
        setattr(parent, short_name, module)
        # The synthetic filename is diagnostic only, never opened or written.
        code = compile(tree, f"<{name}>", "exec")
        exec(code, module.__dict__)
        yield module
    finally:
        if previous_module is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous_module
        if previous_attribute is missing:
            if hasattr(parent, short_name):
                delattr(parent, short_name)
        else:
            setattr(parent, short_name, previous_attribute)
        sys.dont_write_bytecode = previous_bytecode


def _mutation_probe(real, site, cases):
    source_bytes = Path(real.__file__).read_bytes()
    source = source_bytes.decode("utf-8")
    evidence = {
        "site": site, "scope": "vadapt.ramws._swap_used",
        "selector": SITES[site], "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "replay_seed": REPLAY_SEED, "case_count": len(cases),
        "positive_status": "not_run", "mutant_status": "not_run",
        "anchor_count": 0, "smallest_sampled_counterexample": None,
    }
    try:
        positive = _observe_swap(real, cases)
    except Exception as exc:
        evidence.update(positive_status="timeout" if isinstance(exc, TimeoutError)
                        else "observation_error", error=f"{type(exc).__name__}: {exc}")
        return evidence
    try:
        _assert_swap_observation(positive, cases)
    except AssertionError as exc:
        evidence.update(positive_status="failed", mutant_status="not_run_failed_positive",
                        error=str(exc))
        return evidence  # A failed positive never enters the killed numerator.
    except Exception as exc:
        evidence.update(positive_status="timeout" if isinstance(exc, TimeoutError)
                        else "unrelated_assertion_error", error=f"{type(exc).__name__}: {exc}")
        return evidence
    evidence["positive_status"] = "passed"
    try:
        tree = _mutated_tree(source, site)
    except _AnchorError as exc:
        evidence.update(mutant_status=exc.status, anchor_count=exc.count)
        return evidence
    evidence["anchor_count"] = 1
    if ast.dump(tree, include_attributes=False) == ast.dump(ast.parse(source), include_attributes=False):
        evidence["mutant_status"] = "equivalent"
        return evidence
    entered = False
    try:
        with _private_module(tree, real.__file__) as mutant:
            entered = True
            try:
                observed = _observe_swap(mutant, cases)
            except Exception as exc:
                evidence.update(mutant_status="timeout" if isinstance(exc, TimeoutError)
                                else "observation_error", error=f"{type(exc).__name__}: {exc}")
                return evidence
            try:
                _assert_swap_observation(observed, cases)
            except AssertionError:
                evidence.update(mutant_status="killed",
                                smallest_sampled_counterexample=_smallest_counterexample(observed, cases))
            except Exception as exc:
                evidence.update(mutant_status="timeout" if isinstance(exc, TimeoutError)
                                else "unrelated_assertion_error",
                                error=f"{type(exc).__name__}: {exc}")
            else:
                evidence["mutant_status"] = "survived"
    except Exception as exc:
        status = ("timeout" if isinstance(exc, TimeoutError)
                  else "teardown_error" if entered else "constructor_or_import_error")
        evidence.update(mutant_status=status,
                        error=f"{type(exc).__name__}: {exc}")
    return evidence


class NamedRamwsMutationContrasts(unittest.TestCase):
    def _assert_site(self, site):
        from vadapt import ramws

        parent = importlib.import_module("vadapt")
        source_before = Path(ramws.__file__).read_bytes()
        bytecode_before = sys.dont_write_bytecode
        modules_before = {name: value for name, value in sys.modules.items()
                          if name.startswith(_PRIVATE_PREFIX)}
        attributes_before = {name: value for name, value in vars(parent).items()
                             if name.startswith(_PRIVATE_PREFIX.rpartition(".")[2])}
        state_before = (dict(ramws._OPEN), list(ramws._SIG_DONE),
                        dict(ramws._SIG_DONE_PREV),
                        signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        evidence = None
        try:
            evidence = _mutation_probe(ramws, site, _swap_cases())
        finally:
            # Cleanup is part of the gate, including the failed-probe paths.
            self.assertEqual(Path(ramws.__file__).read_bytes(), source_before)
            self.assertIs(sys.dont_write_bytecode, bytecode_before)
            self.assertEqual({name: value for name, value in sys.modules.items()
                              if name.startswith(_PRIVATE_PREFIX)}, modules_before)
            self.assertEqual({name: value for name, value in vars(parent).items()
                              if name.startswith(_PRIVATE_PREFIX.rpartition(".")[2])},
                             attributes_before)
            self.assertEqual((dict(ramws._OPEN), list(ramws._SIG_DONE),
                              dict(ramws._SIG_DONE_PREV),
                              signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)),
                             state_before)
            self.assertNotIn("open", vars(ramws))
        evidence["cleanup"] = "clean"
        print(json.dumps(evidence, sort_keys=True))
        self.assertEqual(evidence["positive_status"], "passed", evidence)
        self.assertEqual(evidence["anchor_count"], 1, evidence)
        self.assertEqual(evidence["mutant_status"], "killed", evidence)
        self.assertIsNotNone(evidence["smallest_sampled_counterexample"])

    def test_m_vadapt_ramws_1_operator_flip(self):
        self._assert_site("M-vadapt-ramws-1")

    def test_m_vadapt_ramws_2_boundary_increment(self):
        self._assert_site("M-vadapt-ramws-2")


if __name__ == "__main__":
    unittest.main()
