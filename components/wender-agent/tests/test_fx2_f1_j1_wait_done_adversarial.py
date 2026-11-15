"""Adversarial claim-pinning tests for production wender.perceive.wait.wait_done.

Defect WP1-02 / fx2-f1-perceive-wait-text-stopstate:
Production wender/perceive/wait.py is AUSENTE on bridge/integration branch.

This module tests production wait_done using offline single-root AX fixtures,
fake clock and sleep callables, tracking snapshot readers, and permuted flat AX node lists.
Healthy control tests pass normally. Production contract tests are decorated with
@unittest.expectedFailure and import wender.perceive.wait.wait_done locally.
"""

import copy
import random
import unittest


def _node(nid, role, name="", child_ids=(), ignored=False, hidden=False):
    """Build a standard CDP AX node dictionary."""
    d = {
        "nodeId": str(nid),
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "childIds": [str(c) for c in child_ids],
        "ignored": ignored,
    }
    if hidden:
        d["properties"] = [{"name": "hidden", "value": {"type": "boolean", "value": True}}]
    return d


def make_single_root_ax_doc(body_text="Progressing...", assistant_copy=True, streaming=False):
    """Build a complete single-root AX document.

    Document Order:
    - RootWebArea (root) -> main
      - main -> user1 (article), assistant1 (article)
        - user1 -> user1-text (StaticText "User question"), user1-copy (button "Copiar mensaje")
        - assistant1 -> anchor (heading "ChatGPT dijo:"), body (StaticText body_text),
                        [optional] assistant1-copy (button "Copiar mensaje"),
                        [optional] stop (button "Detener")
    """
    assistant_children = ["anchor", "body"]
    if assistant_copy:
        assistant_children.append("assistant1-copy")
    if streaming:
        assistant_children.append("stop")

    nodes = [
        _node("root", "RootWebArea", "ChatGPT", child_ids=["main"]),
        _node("main", "main", "", child_ids=["user1", "assistant1"]),
        _node("user1", "article", "", child_ids=["user1-text", "user1-copy"]),
        _node("user1-text", "StaticText", "User question"),
        _node("user1-copy", "button", "Copiar mensaje"),
        _node("assistant1", "article", "", child_ids=assistant_children),
        _node("anchor", "heading", "ChatGPT dijo:"),
        _node("body", "StaticText", body_text),
    ]
    if assistant_copy:
        nodes.append(_node("assistant1-copy", "button", "Copiar mensaje"))
    if streaming:
        nodes.append(_node("stop", "button", "Detener"))

    return nodes


class FakeClock:
    """Fake clock and sleep recorder operating in integer milliseconds."""

    def __init__(self, initial_ms=0):
        self.ms = initial_ms
        self.sleep_calls = []

    def clock(self):
        return self.ms

    def sleep(self, delay_ms):
        self.sleep_calls.append(delay_ms)
        self.ms += delay_ms


class TrackingReader:
    """Snapshot reader tracking exact call counts and returned snapshots."""

    def __init__(self, snapshots):
        self.snapshots = [copy.deepcopy(s) for s in snapshots]
        self.read_count = 0

    def __call__(self):
        if not self.snapshots:
            raise RuntimeError("TrackingReader ran out of snapshots")
        self.read_count += 1
        idx = min(self.read_count - 1, len(self.snapshots) - 1)
        return copy.deepcopy(self.snapshots[idx])


class TestFx2F1J1WaitDoneAdversarial(unittest.TestCase):
    """Adversarial suite for production wait_done."""

    # --- Healthy Controls ---

    def test_healthy_control_single_root_ax_document_structure(self):
        """Healthy control: verify single-root AX document construction and order."""
        doc = make_single_root_ax_doc(body_text="Done text", assistant_copy=True, streaming=False)
        roles_names = [(n["role"]["value"], n["name"]["value"]) for n in doc]

        self.assertIn(("RootWebArea", "ChatGPT"), roles_names)
        self.assertIn(("button", "Copiar mensaje"), roles_names)
        self.assertIn(("heading", "ChatGPT dijo:"), roles_names)

        # Confirm user copy precedes anchor and assistant copy follows anchor in document tree
        user_copy_idx = next(i for i, n in enumerate(doc) if n["nodeId"] == "user1-copy")
        anchor_idx = next(i for i, n in enumerate(doc) if n["nodeId"] == "anchor")
        asst_copy_idx = next(i for i, n in enumerate(doc) if n["nodeId"] == "assistant1-copy")

        self.assertLess(user_copy_idx, anchor_idx)
        self.assertLess(anchor_idx, asst_copy_idx)

    def test_healthy_control_flat_record_permutation_preserves_root_and_child_ids(self):
        """Healthy control: permuting flat node list preserves root, nodeId and childIds."""
        doc = make_single_root_ax_doc("Done text")
        permuted = copy.deepcopy(doc)
        random.seed(42)
        random.shuffle(permuted)

        # Nodes by nodeId must be identical
        by_id_doc = {n["nodeId"]: n for n in doc}
        by_id_perm = {n["nodeId"]: n for n in permuted}

        self.assertEqual(set(by_id_doc.keys()), set(by_id_perm.keys()))
        for nid in by_id_doc:
            self.assertEqual(by_id_doc[nid]["childIds"], by_id_perm[nid]["childIds"])
            self.assertEqual(by_id_doc[nid]["role"]["value"], by_id_perm[nid]["role"]["value"])
            self.assertEqual(by_id_doc[nid]["name"]["value"], by_id_perm[nid]["name"]["value"])

    def test_healthy_control_fake_clock_and_tracking_reader(self):
        """Healthy control: verify FakeClock and TrackingReader mechanics."""
        clock = FakeClock(100)
        self.assertEqual(clock.clock(), 100)
        clock.sleep(50)
        self.assertEqual(clock.clock(), 150)
        self.assertEqual(clock.sleep_calls, [50])

        reader = TrackingReader([make_single_root_ax_doc("A"), make_single_root_ax_doc("B")])
        self.assertEqual(reader.read_count, 0)
        s1 = reader()
        s2 = reader()
        s3 = reader()
        self.assertEqual(reader.read_count, 3)
        self.assertEqual(s1[7]["name"]["value"], "A")
        self.assertEqual(s2[7]["name"]["value"], "B")
        self.assertEqual(s3[7]["name"]["value"], "B")

    # --- Production Waiter Contracts (failing due to AUSENTE module) ---

    def test_production_wait_done_success_sequence_exact_polling_and_quiet_confirmation(self):
        """Production wait_done confirms at max_polls=4 after 1 progress poll and 3 quiet polls."""
        from wender.perceive.wait import wait_done

        # Poll 1: visible progress poll
        p1 = make_single_root_ax_doc("Generating text...", assistant_copy=False, streaming=True)
        # Polls 2, 3, 4: identical completed snapshot
        completed = make_single_root_ax_doc("Complete answer text", assistant_copy=True, streaming=False)

        reader = TrackingReader([p1, completed, completed, completed])
        timer = FakeClock(0)
        rules = {
            "absent_roles_names": [{"role": "button", "name": "Detener"}],
            "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
            "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
            "quiet_snapshots": 3,
        }

        res = wait_done(
            reader,
            rules,
            clock=timer.clock,
            sleep=timer.sleep,
            max_polls=4,
            poll_interval_ms=100,
        )

        self.assertEqual(res, {"done": True, "polls": 4, "completion_status": "confirmed_by_adapter"})
        self.assertEqual(reader.read_count, 4)
        self.assertEqual(timer.sleep_calls, [100, 100, 100])

    def test_production_wait_done_paired_timeout_sequence_omitting_assistant_copy(self):
        """Paired timeout sequence: same quiet run/budget as success, but omits assistant copy."""
        from wender.perceive.wait import wait_done

        # Poll 1: progress poll
        p1 = make_single_root_ax_doc("Generating text...", assistant_copy=False, streaming=True)
        # Polls 2, 3, 4: quiet snapshots that OMIT assistant copy button (user copy still present)
        completed_no_copy = make_single_root_ax_doc("Complete answer text", assistant_copy=False, streaming=False)

        reader = TrackingReader([p1, completed_no_copy, completed_no_copy, completed_no_copy])
        timer = FakeClock(0)
        rules = {
            "absent_roles_names": [{"role": "button", "name": "Detener"}],
            "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
            "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
            "quiet_snapshots": 3,
        }

        res = wait_done(
            reader,
            rules,
            clock=timer.clock,
            sleep=timer.sleep,
            max_polls=4,
            poll_interval_ms=100,
        )

        self.assertEqual(res, {"done": False, "polls": 4, "completion_status": "timeout"})
        self.assertEqual(reader.read_count, 4)
        self.assertEqual(timer.sleep_calls, [100, 100, 100])

    def test_production_wait_done_flat_record_permutation_preserves_confirmation(self):
        """Permuting flat raw node list order does not alter document order tree or confirmation."""
        from wender.perceive.wait import wait_done

        p1 = make_single_root_ax_doc("Chunk 1", assistant_copy=False, streaming=True)
        completed = make_single_root_ax_doc("Final text", assistant_copy=True, streaming=False)

        # Reverse flat record lists
        p1_perm = list(reversed(p1))
        completed_perm = list(reversed(completed))

        reader = TrackingReader([p1_perm, completed_perm, completed_perm])
        timer = FakeClock(0)
        rules = {
            "absent_roles_names": [{"role": "button", "name": "Detener"}],
            "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
            "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
            "quiet_snapshots": 2,
        }

        res = wait_done(
            reader,
            rules,
            clock=timer.clock,
            sleep=timer.sleep,
            max_polls=3,
            poll_interval_ms=100,
        )

        self.assertEqual(res, {"done": True, "polls": 3, "completion_status": "confirmed_by_adapter"})
        self.assertEqual(reader.read_count, 3)

    def test_production_wait_done_rejects_malformed_selector_before_reader_or_clock(self):
        """Reject malformed selectors or bad budget rules before reader acquisition or clock calls."""
        from wender.perceive.wait import wait_done

        reader = TrackingReader([make_single_root_ax_doc("A")])
        timer = FakeClock(0)

        # Malformed selector (missing 'name')
        bad_rules = {
            "absent_roles_names": [{"role": "button"}],
            "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
            "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
            "quiet_snapshots": 2,
        }

        with self.assertRaises(ValueError):
            wait_done(
                reader,
                bad_rules,
                clock=timer.clock,
                sleep=timer.sleep,
                max_polls=3,
                poll_interval_ms=100,
            )

        # Verify neither reader nor clock/sleep was called
        self.assertEqual(reader.read_count, 0)
        self.assertEqual(timer.sleep_calls, [])

    def test_production_wait_done_rejects_stale_copy_mutant_before_anchor(self):
        """Stale user copy before anchor cannot satisfy assistant copy condition."""
        from wender.perceive.wait import wait_done

        # Snapshot has user1-copy ("Copiar mensaje") but assistant1 has no copy button
        stale_copy_doc = make_single_root_ax_doc("Answer", assistant_copy=False, streaming=False)

        reader = TrackingReader([stale_copy_doc, stale_copy_doc, stale_copy_doc])
        timer = FakeClock(0)
        rules = {
            "absent_roles_names": [{"role": "button", "name": "Detener"}],
            "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
            "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
            "quiet_snapshots": 2,
        }

        res = wait_done(
            reader,
            rules,
            clock=timer.clock,
            sleep=timer.sleep,
            max_polls=3,
            poll_interval_ms=100,
        )

        self.assertEqual(res, {"done": False, "polls": 3, "completion_status": "timeout"})

    def test_production_wait_done_rejects_missing_quiet_reset_mutant(self):
        """Quiet run must reset when snapshots change, preventing premature confirmation."""
        from wender.perceive.wait import wait_done

        # Every snapshot changes answer text
        s1 = make_single_root_ax_doc("Chunk 1", assistant_copy=True, streaming=False)
        s2 = make_single_root_ax_doc("Chunk 1 and 2", assistant_copy=True, streaming=False)
        s3 = make_single_root_ax_doc("Chunk 1 and 2 and 3", assistant_copy=True, streaming=False)

        reader = TrackingReader([s1, s2, s3])
        timer = FakeClock(0)
        rules = {
            "absent_roles_names": [{"role": "button", "name": "Detener"}],
            "after_last_anchor": {"role": "heading", "name": "ChatGPT dijo:"},
            "present_roles_names": [{"role": "button", "name": "Copiar mensaje"}],
            "quiet_snapshots": 2,
        }

        res = wait_done(
            reader,
            rules,
            clock=timer.clock,
            sleep=timer.sleep,
            max_polls=3,
            poll_interval_ms=100,
        )

        self.assertEqual(res, {"done": False, "polls": 3, "completion_status": "timeout"})


if __name__ == "__main__":
    unittest.main()
