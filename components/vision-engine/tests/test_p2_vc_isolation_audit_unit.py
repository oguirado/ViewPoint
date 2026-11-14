"""P2/VC: real unittest bodies and predicate-specific process-state poisons.

`load_tests` deliberately keeps poison *fixtures* out of the outer gate. They
are executed explicitly by the audit in bounded, fresh Python processes.
No X, transport, network, file media, sleeps, or external application runs.
"""
from dataclasses import fields, replace
import ast
import hashlib
import os
from pathlib import Path
import sys
import threading
import types
import unittest
import weakref
from unittest.mock import patch

from eval.v2eval import p2_isolation_audit as iso


FIXTURE_PREFIX = "tests.test_p2_vc_isolation_audit_unit._OriginalBodies."
REAL_POISON = (
    "tests.test_jq3_r_r1_j23_mutkill_xfb_ramws_rtring_str_fda281."
    "TestRealAssertions.test_m_vadapt_xfb_2_real"
)
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ("def combine(a, b):\n"
          "    return a - b\n"
          "\n"
          "def witness(value):\n"
          "    if value != 4:\n"
          "        return combine(value, 2) + 1\n"
          "    return 0\n").encode("utf-8")
SRC_SHA = hashlib.sha256(SOURCE).hexdigest()
FIXTURE_SHA = hashlib.sha256(b"p2-vc-isolation-fixed-fixture-01").hexdigest()
_LEAK_OBJECTS = []


class _OriginalBodies(unittest.TestCase):
    """Unmodified TestCase methods executed through ordinary unittest protocol."""

    def setUp(self):
        self.prepared = True
        self.addCleanup(self._finish)

    def _finish(self):
        self.prepared = False

    def test_clean(self):
        self.assertTrue(self.prepared)
        self.assertEqual((2 + 3), 5)

    def test_scoped_patch(self):
        original = os.open
        def fake(*a, **kw):
            return -1
        with patch.object(os, "open", fake):
            self.assertIs(os.open, fake)
        self.assertIs(os.open, original)

    def test_scoped_patch_exception(self):
        original = os.open
        def fake(*a, **kw):
            return -1
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            with patch.object(os, "open", fake):
                self.assertIs(os.open, fake)
                raise RuntimeError("deliberate")
        self.assertIs(os.open, original)

    def test_leak_os_open(self):
        # Exactly the aliasing mechanism of the pinned xfb poison.
        proxy = types.SimpleNamespace(os=os)
        proxy.os.open = lambda *a, **kw: -99
        self.assertEqual(proxy.os.open("unused", 0), -99)

    def test_leak_environment(self):
        os.environ["P2_VC_LOCAL_SENTINEL"] = "drift"
        self.assertEqual(os.environ["P2_VC_LOCAL_SENTINEL"], "drift")

    def test_leak_module(self):
        sys.modules["p2_vc_leaked_module"] = types.ModuleType("p2_vc_leaked_module")
        self.assertIn("p2_vc_leaked_module", sys.modules)

    def test_leak_weak_reference(self):
        class Sentinel:
            pass
        sentinel = Sentinel()
        _LEAK_OBJECTS.append((sentinel, weakref.ref(sentinel)))
        self.assertIs(_LEAK_OBJECTS[-1][1](), sentinel)

    def test_leak_lock(self):
        lock = threading.Lock()
        self.assertTrue(lock.acquire(blocking=False))
        _LEAK_OBJECTS.append(lock)
        self.assertTrue(lock.locked())

    def test_leak_thread(self):
        started = threading.Event()
        release = threading.Event()
        worker = threading.Thread(target=lambda: (started.set(), release.wait()), daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=2))  # event barrier, never sleep
        self.assertTrue(worker.is_alive())

    def test_semantic_failure(self):
        self.assertEqual(7, 8)  # designated body AssertionError

    def test_bad_error(self):
        raise RuntimeError("not a semantic AssertionError")

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(7, 8)

    @unittest.expectedFailure
    def test_unexpected_success(self):
        self.assertEqual(7, 7)

    @unittest.skip("explicitly disallowed in mandatory audit suites")
    def test_skipped(self):
        self.fail("unreachable")


class TestIsolationAudit(unittest.TestCase):
    def run_ids(self, *methods):
        return iso.run_originals(tuple(FIXTURE_PREFIX + m for m in methods), time_limit_s=20)

    def test_schema_ordered_and_outer_budget_refusal_before_spawn(self):
        self.assertEqual(tuple(x.name for x in fields(iso.AstSite)), iso.AST_SITE_FIELDS)
        self.assertEqual(tuple(x.name for x in fields(iso.MutationTrial)), iso.MUTATION_TRIAL_FIELDS)
        self.assertEqual(iso.MAX_SPECIMEN_SECONDS, 60)
        self.assertEqual(iso.MAX_SPECIMEN_BYTES, 2 * 1024**3)
        with patch.object(iso.multiprocessing, "get_context", side_effect=AssertionError("spawn unauthorized")):
            for ids, seconds, memory, code in (
                ([], 60, iso.MAX_SPECIMEN_BYTES, "E_AUDIT_TESTS"),
                ([FIXTURE_PREFIX + "test_clean"] * 2, 60, iso.MAX_SPECIMEN_BYTES, "E_AUDIT_TESTS"),
                ([FIXTURE_PREFIX + "test_clean"], 61, iso.MAX_SPECIMEN_BYTES, "E_AUDIT_BUDGET"),
                ([FIXTURE_PREFIX + "test_clean"], 60, iso.MAX_SPECIMEN_BYTES + 1, "E_AUDIT_BUDGET"),
                ([FIXTURE_PREFIX + "test_clean"], 60, True, "E_AUDIT_BUDGET"),
            ):
                with self.subTest(code=code, seconds=seconds, memory=memory):
                    with self.assertRaises(iso.AuditRefusal) as cm:
                        iso.run_originals(ids, time_limit_s=seconds, memory_limit_bytes=memory)
                    self.assertEqual(cm.exception.code, code)

    def test_positive_real_original_lifecycle_and_scoped_patch(self):
        original = os.open
        for method in ("test_clean", "test_scoped_patch", "test_scoped_patch_exception"):
            with self.subTest(method=method):
                result = self.run_ids(method)
                self.assertEqual(result.status, "PASS", result)
                self.assertEqual(result.executed, (FIXTURE_PREFIX + method,))
                self.assertEqual(result.cases, ((FIXTURE_PREFIX + method, "PASS"),))
                self.assertTrue(result.restored, result)
                self.assertEqual(dict(result.leaks), {})
                self.assertIs(os.open, original)

    def test_single_factor_isolation_failures_not_mutant_kills(self):
        intended = {
            "test_leak_os_open": "os.open",
            "test_leak_environment": "environment",
            "test_leak_module": "module_cache",
            "test_leak_thread": "threads",
        }
        original = os.open
        pristine = self.run_ids("test_clean")
        for method, predicate in intended.items():
            with self.subTest(method=method):
                result = self.run_ids(method)
                self.assertEqual(result.status, "ISOLATION_FAILURE", result)
                self.assertEqual(result.cases, ((FIXTURE_PREFIX + method, "PASS"),))
                self.assertGreater(result.leaks.get(predicate, 0), 0, result)
                self.assertTrue(result.restored, result)
                self.assertIs(os.open, original)
                self.assertNotEqual(iso.classify_trial(pristine, result,
                                   FIXTURE_PREFIX + method), "KILLED")

    def test_direct_snapshot_weak_refs_and_locks_are_audited(self):
        class Holder:
            pass
        snap = iso._Snapshot()  # independently test the snapshot predicates
        keeper = Holder()
        ref = weakref.ref(keeper)
        lock = threading.Lock()
        lock.acquire()
        try:
            self.assertIs(ref(), keeper)
            effects = snap.compare()
            self.assertGreater(effects.get("weak_references", 0), 0, effects)
            self.assertGreater(effects.get("locks", 0), 0, effects)
        finally:
            lock.release()
            snap.restore()

    def test_preserves_assertion_errors_skips_xfail_xpass(self):
        expected = {
            "test_semantic_failure": ("ASSERTION_FAILURE", "AssertionError"),
            "test_bad_error": ("ERROR", "RuntimeError"),
            "test_expected_failure": ("XFAIL", ""),
            "test_unexpected_success": ("XPASS", ""),
            "test_skipped": ("SKIPPED", ""),
        }
        for method, (status, error_type) in expected.items():
            with self.subTest(method=method):
                res = self.run_ids(method)
                self.assertEqual(res.status, status, res)
                self.assertEqual(len(res.executed), 1)
                if status == "ASSERTION_FAILURE":
                    self.assertEqual(res.failures, ((FIXTURE_PREFIX + method, error_type, True),))
                elif status == "ERROR":
                    self.assertEqual(res.errors, ((FIXTURE_PREFIX + method, error_type),))
                elif status == "XPASS":
                    self.assertEqual(res.unexpected_successes, (FIXTURE_PREFIX + method,))
                elif status == "XFAIL":
                    self.assertEqual(res.expected_failures, (FIXTURE_PREFIX + method,))
                else:
                    self.assertEqual(res.skipped, (FIXTURE_PREFIX + method,))

    def test_solo_coupled_clean_agree_poison_disagrees(self):
        clean = iso.run_orders((FIXTURE_PREFIX + "test_clean",
                                FIXTURE_PREFIX + "test_scoped_patch"), seed=21,
                               time_limit_s=20)
        self.assertTrue(clean.agreement, clean)
        self.assertGreaterEqual(len(clean.orders), 2)
        self.assertTrue(all(run.clean_pass for run in clean.solo + clean.coupled))
        coupled_poison = iso.run_originals((FIXTURE_PREFIX + "test_clean",
                                           FIXTURE_PREFIX + "test_leak_environment"),
                                          time_limit_s=20)
        self.assertEqual(coupled_poison.status, "ISOLATION_FAILURE")
        self.assertEqual(tuple(x[1] for x in coupled_poison.cases), ("PASS", "PASS"))
        self.assertGreater(coupled_poison.leaks.get("environment", 0), 0)


    def test_real_pinned_original_poison_survives_as_isolation_failure(self):
        # The paired repair owns the real test file; this worker only executes it.
        result = iso.run_originals((REAL_POISON,), time_limit_s=30)
        self.assertEqual(result.executed, (REAL_POISON,), result)
        self.assertEqual(result.cases, ((REAL_POISON, "PASS"),), result)
        self.assertEqual(result.status, "ISOLATION_FAILURE", result)
        self.assertGreater(result.leaks.get("os.open", 0), 0, result)
        self.assertTrue(result.restored, result)


class TestASTMutationLedger(unittest.TestCase):
    def get_site(self, category):
        sites = iso.enumerate_sites("vadapt/demo.py", SOURCE)[category]
        self.assertTrue(sites, category)
        return sites[0]

    def trial(self, campaign, site, disposition, proof=None):
        return iso.MutationTrial(
            iso.MUTATION_TRIAL_SCHEMA, campaign.campaign_id, site, campaign.fixture_sha256,
            (FIXTURE_PREFIX + "test_clean",), campaign.seed, "PASS", "PASS",
            FIXTURE_PREFIX + "test_clean", "a" * 64, 1, disposition, proof)

    def test_pinned_ast_expression_one_change_per_category(self):
        for category in iso.CATEGORIES:
            with self.subTest(category=category):
                site = self.get_site(category)
                self.assertEqual(tuple(site.to_dict()), iso.AST_SITE_FIELDS)
                mutated = iso.mutate_source(SOURCE, site)
                self.assertNotEqual(mutated, SOURCE)
                compiled = compile(mutated, site.module_path, "exec")
                self.assertIsNotNone(compiled)
                before = ast.parse(SOURCE)
                after = ast.parse(mutated)
                self.assertNotEqual(ast.dump(before), ast.dump(after))
                self.assertIn(category, iso.CATEGORIES)

    def test_qualified_scope_and_distinct_raw_eligible_populations(self):
        by_category = iso.enumerate_sites("vadapt/demo.py", SOURCE)
        for sites in by_category.values():
            self.assertTrue(all(s.qualified_ast_scope in ("combine", "witness")
                                for s in sites), sites)
        augmented = SOURCE + b"\ndef extras():\n    return repr('raw')\n"
        campaign = iso.freeze_campaign("raw", {"vadapt/demo.py": augmented}, b"fixture")
        self.assertTrue(any(p.raw_sites > len(p.eligible_sites) for p in campaign.populations))
        for population in campaign.populations:
            self.assertGreaterEqual(population.raw_sites, len(population.eligible_sites))

    def test_anchor_stale_changed_source_and_ambiguous_wildcard(self):
        site = self.get_site("OPERATOR_FLIP")
        with self.assertRaises(iso.AuditRefusal) as cm:
            iso.mutate_source(SOURCE + b"# drift\n", site)
        self.assertEqual(cm.exception.code, "ANCHOR_STALE")
        duplicate = b"def demo():\n    if 1 == 2:\n        return 3\n    if 1 == 2:\n        return 4\n"
        candidate = iso.AstSite(
            iso.AST_SITE_SCHEMA, "vadapt/duplicate.py", hashlib.sha256(duplicate).hexdigest(),
            "demo", "1 == 2", "1 == 2", "body[*].test", "OPERATOR_FLIP", "site-dupe")
        with self.assertRaises(iso.AuditRefusal) as cm:
            iso.mutate_source(duplicate, candidate)
        self.assertEqual(cm.exception.code, "ANCHOR_STALE")
        with self.assertRaises(iso.AuditRefusal) as cm:
            replace(site, structural_selector="17")
        self.assertEqual(cm.exception.code, "ANCHOR_STALE")

    def test_population_freeze_reconcile_and_missing_receipts(self):
        campaign = iso.freeze_campaign("sample", {"vadapt/demo.py": SOURCE}, b"fixed-fixture", seed=23, max_per_category=2)
        self.assertEqual(campaign.source_digests["vadapt/demo.py"], SRC_SHA)
        all_selected = [site for p in campaign.populations for site in p.selected_sites]
        self.assertTrue(all_selected)
        self.assertLessEqual(len(all_selected), len(iso.CATEGORIES) * 2)
        receipts = [self.trial(campaign, site, "KILLED" if index == 0 else "SURVIVED")
                    for index, site in enumerate(all_selected)]
        summary = iso.reduce_campaign(campaign, receipts)
        totals = summary["totals"]
        self.assertEqual(totals["selected"], len(receipts))
        self.assertEqual(totals["generated"], len(receipts))
        self.assertEqual(totals["executed"], len(receipts))
        self.assertEqual(totals["killed"], 1)
        self.assertEqual(totals["survived"], len(receipts) - 1)
        self.assertEqual(summary["gross_score"], 1 / len(receipts))
        self.assertEqual(summary["semantic_score"], 1 / len(receipts))
        for missing in (receipts[:-1], receipts + receipts[:1]):
            with self.assertRaises(iso.AuditRefusal) as cm:
                iso.reduce_campaign(campaign, missing)
            self.assertEqual(cm.exception.code, "E_AUDIT_RECEIPT")
        empty = iso.freeze_campaign("zero", {"vadapt/empty.py": b"pass\n"}, b"notempty")
        absent = iso.reduce_campaign(empty, ())
        self.assertIsNone(absent["gross_score"])
        self.assertIsNone(absent["semantic_score"])

    def test_counter_partitions_invalid_stale_deferred_and_proof(self):
        frozen = iso.freeze_campaign("sample2", {"vadapt/demo.py": SOURCE}, b"fixture", max_per_category=2)
        selected = [s for p in frozen.populations for s in p.selected_sites]
        states = ["ANCHOR_STALE", "DEFERRED_OWNER", "INVALID_MUTANT", "INFRASTRUCTURE",
                  "BASELINE_RED", "KILLED", "SURVIVED", "EQUIVALENT"]
        trials = [self.trial(frozen, s, states[i % len(states)],
                             f"reviewed:{s.source_sha256}:current-tree-proof" if states[i % len(states)] == "EQUIVALENT" else None)
                  for i, s in enumerate(selected)]
        counts = iso.reduce_campaign(frozen, trials)["totals"]
        self.assertEqual(counts["selected"], counts["generated"] + counts["stale"] + counts["deferred"])
        self.assertEqual(counts["generated"], counts["invalid"] + counts["executed"])
        self.assertEqual(counts["executed"], counts["killed"] + counts["survived"] + counts["equivalent"]
                         + counts["infrastructure"] + counts["baseline_red"])
        with self.assertRaises(iso.AuditRefusal) as cm:
            self.trial(frozen, selected[0], "EQUIVALENT")
        self.assertEqual(cm.exception.code, "E_AUDIT_PROOF")

    def test_semantic_kill_requires_real_assertion_identity_not_exception(self):
        test_id = FIXTURE_PREFIX + "test_clean"
        pristine = iso.AuditRun((test_id,), (test_id,), "PASS", ((test_id, "PASS"),),
                                (), (), (), (), (), {}, True, 10)
        valid_failure = replace(pristine, status="ASSERTION_FAILURE",
                                cases=((test_id, "ASSERTION_FAILURE"),),
                                failures=((test_id, "AssertionError", True),))
        self.assertEqual(iso.classify_trial(pristine, valid_failure, test_id), "KILLED")
        self.assertEqual(iso.classify_trial(pristine, replace(valid_failure,
                                failures=((test_id, "AssertionError", False),)), test_id), "INFRASTRUCTURE")
        self.assertEqual(iso.classify_trial(pristine, replace(valid_failure,
                                failures=((test_id, "ValueError", True),)), test_id), "INFRASTRUCTURE")
        self.assertEqual(iso.classify_trial(pristine, replace(valid_failure,
                                status="ISOLATION_FAILURE", leaks={"os.open": 1}), test_id), "INFRASTRUCTURE")
        self.assertEqual(iso.classify_trial(pristine, pristine, test_id), "SURVIVED")
        self.assertEqual(iso.classify_trial(replace(pristine, status="XPASS"),
                                            valid_failure, test_id), "BASELINE_RED")


def load_tests(loader, standard_tests, pattern):
    """Only audit assertions are gate tests; leaked fixture bodies are specimens."""
    return unittest.TestSuite((loader.loadTestsFromTestCase(TestIsolationAudit),
                               loader.loadTestsFromTestCase(TestASTMutationLedger)))
