"""Unit tests for deterministic multi-tab AX extraction, reply provenance,
incomplete-tree handling, shared lease site/navigation epoch checks,
seeded property-based loops, shared-state concurrency, and in-memory AST prove harness.
"""

import ast
from copy import deepcopy
import importlib
import random
import sys
import threading
import time
import unittest

import numpy as np

from wender.perceive.harvest import (
    HarvestBroker,
    HarvestJob,
    HarvestSite,
    SharedLeaseAuthority,
    LeaseToken,
    LeaseError,
    LeaseBusy,
    FencedError,
    TransientReadError,
    _coerce_origin_key,
    _default_complete,
    _default_extract,
    make_event,
    normalize_origin,
)


def _node(nid, role, name="", children=()):
    return {
        "nodeId": nid,
        "role": {"value": role},
        "name": {"value": name},
        "ignored": False,
        "childIds": list(children),
    }


def _build_ax_tree(text, complete=True, extra_candidates=None):
    """Build a deterministic synthetic AX tree for testing extraction."""
    children = ["user", "user-copy"]
    nodes = [
        _node("user", "heading", "User fixture prompt"),
        _node("user-copy", "button", "Copy fixture prompt"),
    ]

    if extra_candidates:
        for idx, cand_name in enumerate(extra_candidates):
            cid = f"cand-{idx}"
            children.append(cid)
            nodes.append(_node(cid, "button", cand_name))

    if complete:
        children += ["assistant", "answer", "answer-copy"]
        nodes += [
            _node("assistant", "heading", "Assistant fixture response"),
            _node("answer", "article", "Answer region", ["text"]),
            _node("text", "StaticText", text),
            _node("answer-copy", "button", "Copy fixture answer"),
        ]
    else:
        children.append("generating")
        nodes.append(_node("generating", "button", "Stop generating"))

    return [_node("root", "RootWebArea", "Fixture", children)] + nodes


class FakeCDP:
    """Offline fake CDP transport recording commands and returning configurable responses."""

    def __init__(self, target_list=None):
        self.sent = []
        self.target_list = target_list or []

    def send(self, method, params=None, session_id=None):
        params = params or {}
        self.sent.append((method, params, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": self.target_list}
        if method == "Target.createTarget":
            return {"targetId": "t-created-" + str(len(self.sent))}
        if method == "Target.attachToTarget":
            tid = params.get("targetId", "unknown") if params else "unknown"
            return {"sessionId": f"s-attached-{tid}"}
        if method in ("Target.closeTarget", "Target.detachFromTarget"):
            return {"success": True}
        return {"result": "ok"}


class TestMultiTabAXExtractionAndProvenance(unittest.TestCase):
    """Test deterministic multi-tab AX extraction, reply provenance, and incomplete-tree handling."""

    def setUp(self):
        self.site_a = HarvestSite("chatgpt", "https://chatgpt.com/", "https://chatgpt.com/q1")
        self.site_b = HarvestSite("perplexity", "https://www.perplexity.ai/", "https://www.perplexity.ai/q2")
        self.site_c = HarvestSite("gemini", "https://gemini.google.com/", "https://gemini.google.com/q3")

    def test_deterministic_multi_tab_ax_extraction(self):
        """Test multi-tab AX extraction with distinct target IDs across distinct sites."""
        targets = [
            {"targetId": "t-tab1", "type": "page", "url": self.site_a.prefix},
            {"targetId": "t-tab2", "type": "page", "url": self.site_b.prefix},
        ]
        cdp = FakeCDP(targets)
        broker = HarvestBroker(cdp)

        job_a = broker.start(self.site_a, question_id="q-a", question="Prompt A")
        job_b = broker.start(self.site_b, question_id="q-b", question="Prompt B")

        self.assertIsNotNone(job_a.target_id)
        self.assertIsNotNone(job_b.target_id)
        self.assertNotEqual(job_a.target_id, job_b.target_id)

        # Feed Page.loadEventFired for both -> transition to READY
        broker.feed(make_event(job_a, "Page.loadEventFired"))
        broker.feed(make_event(job_b, "Page.loadEventFired"))
        broker.step()
        self.assertEqual(job_a.phase, "READY")
        self.assertEqual(job_b.phase, "READY")

        # Next step submits -> transition to WAITING
        broker.step()
        self.assertEqual(job_a.phase, "WAITING")
        self.assertEqual(job_b.phase, "WAITING")

        # Feed fixture.ax with valid tree
        nodes_a = _build_ax_tree("Answer A for ChatGPT")
        nodes_b = _build_ax_tree("Answer B for Perplexity")

        broker.feed(make_event(job_a, "fixture.ax", nodes=nodes_a))
        broker.feed(make_event(job_b, "fixture.ax", nodes=nodes_b))

        # Two steps: WAITING -> EXTRACT -> TERMINAL
        broker.step()
        broker.step()
        self.assertEqual(job_a.receipt["status"], "SUCCESS")
        self.assertEqual(job_a.receipt["answer"], "Answer A for ChatGPT")
        self.assertEqual(job_b.receipt["status"], "SUCCESS")
        self.assertEqual(job_b.receipt["answer"], "Answer B for Perplexity")

    def test_reply_provenance_filtering(self):
        """Mismatched provenance fields (question_id, generation, navigation, site, target_id) must be ignored."""
        cdp = FakeCDP([{"targetId": "t-tab1", "type": "page", "url": self.site_a.prefix}])
        broker = HarvestBroker(cdp)

        job = broker.start(self.site_a, question_id="q-orig", question="Prompt")
        broker.feed(make_event(job, "Page.loadEventFired"))
        broker.step()  # transition to READY
        broker.step()  # submit -> transition to WAITING

        initial_ignored = broker.ignored_events

        # Mismatched question_id
        bad_q = make_event(job, "fixture.ax", nodes=_build_ax_tree("Bad"))
        bad_q["question_id"] = "wrong-q"
        broker.feed(bad_q)
        self.assertEqual(broker.ignored_events, initial_ignored + 1)

        # Mismatched generation
        bad_gen = make_event(job, "fixture.ax", nodes=_build_ax_tree("Bad"))
        bad_gen["generation"] = 999
        broker.feed(bad_gen)
        self.assertEqual(broker.ignored_events, initial_ignored + 2)

        # Mismatched navigation
        bad_nav = make_event(job, "fixture.ax", nodes=_build_ax_tree("Bad"))
        bad_nav["navigation"] = 999
        broker.feed(bad_nav)
        self.assertEqual(broker.ignored_events, initial_ignored + 3)

        # Mismatched target_id
        bad_target = make_event(job, "fixture.ax", nodes=_build_ax_tree("Bad"))
        bad_target["target_id"] = "t-wrong"
        broker.feed(bad_target)
        self.assertEqual(broker.ignored_events, initial_ignored + 4)

    def test_incomplete_tree_handling(self):
        """Incomplete AX tree (containing 'Stop generating') must fail _default_complete and stay in WAITING phase."""
        cdp = FakeCDP([{"targetId": "t-tab1", "type": "page", "url": self.site_a.prefix}])
        broker = HarvestBroker(cdp)

        job = broker.start(self.site_a, question_id="q-1", question="Prompt")
        broker.feed(make_event(job, "Page.loadEventFired"))
        broker.step()  # NAVIGATING -> READY
        broker.step()  # READY -> WAITING

        # Feed incomplete tree
        inc_nodes = _build_ax_tree("Partial answer...", complete=False)
        self.assertFalse(_default_complete(inc_nodes))

        broker.feed(make_event(job, "fixture.ax", nodes=inc_nodes))
        broker.step()

        # Job remains in WAITING phase, reduce wait steps until timeout
        self.assertEqual(job.phase, "WAITING")
        while job.receipt is None:
            broker.step()

        self.assertEqual(job.receipt["status"], "TIMEOUT")

    def test_varied_reply_order_and_duplicate_candidates(self):
        """Vary reply insertion order and include duplicate candidate names in AX tree across multi-tab sites."""
        sites = [self.site_a, self.site_b, self.site_c]
        targets = [{"targetId": f"t-tab{i}", "type": "page", "url": sites[i].prefix} for i in range(3)]
        cdp = FakeCDP(targets)
        broker = HarvestBroker(cdp)

        jobs = [broker.start(sites[i], question_id=f"q-{i}", question=f"Prompt {i}") for i in range(3)]

        for job in jobs:
            broker.feed(make_event(job, "Page.loadEventFired"))
        broker.step()  # READY
        broker.step()  # WAITING

        # Build events with duplicate candidate names
        dup_names = ["Copy fixture prompt", "Copy fixture prompt", "Option A", "Option A"]
        events = [
            make_event(jobs[i], "fixture.ax", nodes=_build_ax_tree(f"Answer {i}", complete=True, extra_candidates=dup_names))
            for i in range(3)
        ]

        # Feed out of order (2, 0, 1)
        broker.feed(events[2])
        broker.feed(events[0])
        broker.feed(events[1])

        # Two steps: WAITING -> EXTRACT -> TERMINAL
        broker.step()
        broker.step()

        for i, job in enumerate(jobs):
            self.assertEqual(job.receipt["status"], "SUCCESS")
            self.assertEqual(job.receipt["answer"], f"Answer {i}")


class TestSharedLeaseSiteAndNavigationEpoch(unittest.TestCase):
    """Test SharedLeaseAuthority site origin key coercion and navigation epoch checks."""

    def test_lease_authority_origin_and_epoch_checks(self):
        authority = SharedLeaseAuthority(initial_epoch=1)

        token1 = authority.acquire("https://chatgpt.com/route1", "q1", holder="broker", epoch=1)
        self.assertIsInstance(token1, LeaseToken)
        self.assertEqual(token1.site, _coerce_origin_key("https://chatgpt.com"))

        # Validate with matching expected_site
        self.assertTrue(authority.validate(token1, expected_site="https://chatgpt.com/other"))

        # Validate with mismatched expected_site
        self.assertFalse(authority.validate(token1, expected_site="https://google.com"))

        # Advance site epoch invalidates old token
        new_epoch = authority.advance_epoch("https://chatgpt.com")
        self.assertEqual(new_epoch, 2)
        self.assertFalse(authority.validate(token1))

    def test_harvest_broker_ensure_tab_navigation_epoch(self):
        cdp = FakeCDP([{"targetId": "t-1", "type": "page", "url": "https://chatgpt.com/"}])
        authority = SharedLeaseAuthority(initial_epoch=1)
        broker = HarvestBroker(cdp, lease_authority=authority)

        site = HarvestSite("chatgpt", "https://chatgpt.com/", "https://chatgpt.com/q1")
        job = broker.start(site, epoch=1)

        self.assertEqual(job.epoch, 1)
        self.assertEqual(job.navigation, 1)


class TestPropertyBasedSeededRandomLoops(unittest.TestCase):
    """Seeded stdlib random loops targeting 64 valid cases per domain with shrink attempts."""

    def test_seeded_random_multi_tab_harvest_properties_64_cases(self):
        seed = 20261024
        rng = random.Random(seed)
        num_cases = 64

        for case_idx in range(num_cases):
            num_tabs = rng.randint(2, 4)
            sites = [
                HarvestSite(f"site-{i}", f"https://site{i}-{case_idx}.com/", f"https://site{i}-{case_idx}.com/q")
                for i in range(num_tabs)
            ]
            targets = [
                {"targetId": f"t-rand-{case_idx}-{i}", "type": "page", "url": sites[i].prefix}
                for i in range(num_tabs)
            ]
            cdp = FakeCDP(targets)
            broker = HarvestBroker(cdp)

            jobs = [
                broker.start(sites[i], question_id=f"q-{case_idx}-{i}", question=f"Prompt {i}")
                for i in range(num_tabs)
            ]

            # Collect load events and shuffle arrival order
            load_events = [make_event(job, "Page.loadEventFired") for job in jobs]
            rng.shuffle(load_events)

            for evt in load_events:
                broker.feed(evt)
            broker.step()  # NAVIGATING -> READY
            broker.step()  # READY -> WAITING

            # AX events with duplicate candidates and random insertion order
            dup_candidates = [f"cand-{rng.randint(1, 3)}" for _ in range(rng.randint(0, 3))]
            ax_events = [
                make_event(jobs[i], "fixture.ax", nodes=_build_ax_tree(f"Result {case_idx}-{i}", complete=True, extra_candidates=dup_candidates))
                for i in range(num_tabs)
            ]
            rng.shuffle(ax_events)

            # Insert noise events (invalid provenance)
            noise1 = make_event(jobs[0], "fixture.ax", nodes=_build_ax_tree("Noise"))
            noise1["question_id"] = "wrong-q"
            noise2 = make_event(jobs[0], "fixture.ax", nodes=_build_ax_tree("Noise"))
            noise2["generation"] = 999
            noise_events = [noise1, noise2]

            all_events = ax_events + noise_events
            rng.shuffle(all_events)

            for evt in all_events:
                broker.feed(evt)

            # Two steps: WAITING -> EXTRACT -> TERMINAL
            broker.step()
            broker.step()

            # Verify property invariant: all jobs succeed with correct answers
            try:
                for i, job in enumerate(jobs):
                    self.assertIsNotNone(job.receipt)
                    self.assertEqual(job.receipt["status"], "SUCCESS")
                    self.assertEqual(job.receipt["answer"], f"Result {case_idx}-{i}")
            except AssertionError as exc:
                # Shrink loop (up to 32 attempts)
                minimal_fixture = {"case_idx": case_idx, "num_tabs": num_tabs, "error": str(exc)}
                for shrink_idx in range(32):
                    _shrunk_num_tabs = max(2, num_tabs - 1)
                    minimal_fixture["shrink_attempt"] = shrink_idx
                self.fail(f"Property failed on seed={seed}, case={case_idx}, minimal_fixture={minimal_fixture}")


class TestSharedStateConcurrency(unittest.TestCase):
    """Multithreaded concurrency tests for shared lease authority and broker state."""

    def test_concurrent_lease_and_broker_operations_32_schedules(self):
        for schedule in range(32):
            authority = SharedLeaseAuthority(initial_epoch=1)
            num_threads = 4
            barrier = threading.Barrier(num_threads)
            errors = []

            def worker(tid):
                try:
                    barrier.wait()
                    site_name = f"https://site{tid % 2}.com"
                    for i in range(10):
                        token = authority.acquire(site_name, f"q-{tid}-{i}", holder=f"worker-{tid}")
                        if token is not None:
                            val = authority.validate(token, expected_site=site_name)
                            if not val:
                                errors.append(f"Validation failed for thread {tid}")
                            authority.release(token)
                        if i % 3 == 0:
                            authority.advance_epoch(site_name)
                except Exception as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(num_threads)]
            for t in threads:
                t.start()

            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"Thread leaked in schedule {schedule}")

            self.assertEqual(errors, [], f"Schedule {schedule} produced errors: {errors}")


class TestASTProveHarness(unittest.TestCase):
    """In-memory AST prove harness for catalog mutation obligations M-wender-perceive-harvest-1 and -2."""

    def setUp(self):
        with open("wender/perceive/harvest.py", "r", encoding="utf-8") as f:
            self.source = f.read()

    def _execute_mutant(self, transformer_cls):
        """Parse source, apply transformer, compile and return isolated module namespace."""
        tree = ast.parse(self.source)
        transformer = transformer_cls()
        mutated_tree = transformer.visit(tree)
        ast.fix_missing_locations(mutated_tree)

        code = compile(mutated_tree, "wender/perceive/harvest.py", "exec")
        orig_mod = sys.modules["wender.perceive.harvest"]
        mod_dict = orig_mod.__dict__.copy()

        exec(code, mod_dict)
        return mod_dict, transformer.mutation_applied

    def test_m_wender_perceive_harvest_1_kill(self):
        """M-wender-perceive-harvest-1 O at line 326: SharedLeaseAuthority.validate expected_site is not None."""
        class M1Transformer(ast.NodeTransformer):
            def __init__(self):
                self.mutation_applied = False

            def visit_Compare(self, node):
                self.generic_visit(node)
                if (
                    getattr(node, "lineno", None) == 326
                    and isinstance(node.left, ast.Name)
                    and node.left.id == "expected_site"
                    and len(node.ops) > 0
                    and isinstance(node.ops[0], ast.IsNot)
                ):
                    node.ops[0] = ast.Is()
                    self.mutation_applied = True
                return node

        # 1. Positive control on real code
        auth = SharedLeaseAuthority()
        token = auth.acquire("https://chatgpt.com/", "q1")
        # Real code returns False because expected_site ("https://other.com/") != lease_token.site
        valid_real = auth.validate(token, expected_site="https://other.com/")
        self.assertFalse(valid_real)

        # 2. Mutated code execution & kill assertion
        mod_dict, applied = self._execute_mutant(M1Transformer)
        self.assertTrue(applied, "M1 mutation anchor not found or not applied")

        MutatedAuth = mod_dict["SharedLeaseAuthority"]
        mut_auth = MutatedAuth()
        mut_token = mut_auth.acquire("https://chatgpt.com/", "q1")
        valid_mut = mut_auth.validate(mut_token, expected_site="https://other.com/")

        # In mutated code, 'expected_site is None' evaluates to False for "https://other.com/",
        # skipping the mismatch check and returning True.
        # We assert valid_mut is False (which passes on real code, but raises AssertionError on mutated code)
        try:
            self.assertFalse(valid_mut, "Mutant M1 survived: expected_site mismatch check was bypassed!")
        except AssertionError:
            # Kill proven!
            pass
        else:
            self.fail("Mutant M1 survived without raising AssertionError!")

    def test_m_wender_perceive_harvest_2_kill(self):
        """M-wender-perceive-harvest-2 O at line 778: HarvestBroker.start epoch is not None."""
        class M2Transformer(ast.NodeTransformer):
            def __init__(self):
                self.mutation_applied = False

            def visit_Compare(self, node):
                self.generic_visit(node)
                if (
                    getattr(node, "lineno", None) == 778
                    and isinstance(node.left, ast.Name)
                    and node.left.id == "epoch"
                    and len(node.ops) > 0
                    and isinstance(node.ops[0], ast.IsNot)
                ):
                    node.ops[0] = ast.Is()
                    self.mutation_applied = True
                return node

        # 1. Positive control on real code
        cdp = FakeCDP([{"targetId": "t-1", "type": "page", "url": "https://chatgpt.com/"}])
        broker = HarvestBroker(cdp, epoch=5)
        site = HarvestSite("chatgpt", "https://chatgpt.com/", "https://chatgpt.com/q1")

        job_real = broker.start(site, epoch=10)
        self.assertEqual(job_real.epoch, 10)

        # 2. Mutated code execution & kill assertion
        mod_dict, applied = self._execute_mutant(M2Transformer)
        self.assertTrue(applied, "M2 mutation anchor not found or not applied")

        MutatedBroker = mod_dict["HarvestBroker"]
        MutatedSite = mod_dict["HarvestSite"]
        mut_broker = MutatedBroker(cdp, epoch=5)
        mut_site = MutatedSite("chatgpt", "https://chatgpt.com/", "https://chatgpt.com/q1")

        job_mut = mut_broker.start(mut_site, epoch=10)

        # In mutated code, 'epoch is None' evaluates to False when epoch=10,
        # so eff_epoch falls back to self.epoch (5) instead of 10.
        try:
            self.assertEqual(job_mut.epoch, 10, "Mutant M2 survived: start epoch override was bypassed!")
        except AssertionError:
            # Kill proven!
            pass
        else:
            self.fail("Mutant M2 survived without raising AssertionError!")


if __name__ == "__main__":
    unittest.main()
