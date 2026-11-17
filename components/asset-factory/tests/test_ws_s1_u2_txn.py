"""Synthetic byte-buffer controls; the separate Blender gate exercises real RNA."""

import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import uuid

from asset_factory import core
from tools.sculpt.txn import Checkpoint, MeshState, Transaction


class BufferTarget:
    def __init__(self, *, residue=False):
        self.identity = "fixture:" + uuid.uuid4().hex
        self.state = MeshState(struct.pack("<6f", -0.0, 0, 0, 1, 2, 3), b"topology")
        self.residue = residue
        self.discarded = 0

    def read(self):
        return self.state

    def snapshot(self):
        return Checkpoint(self.state, self.identity)

    def restore(self, checkpoint):
        self.state = checkpoint.state
        if self.residue:
            displace(self, 0)

    def discard(self, checkpoint):
        self.discarded += 1


def displace(target, deadline):
    values = list(struct.unpack("<6f", target.state.vertices))
    values[3] += 0.125
    target.state = MeshState(struct.pack("<6f", *values), target.state.topology)


def measure(target, deadline):
    return {"rc": 0, "metrics": {"score": int(struct.unpack("<6f", target.state.vertices)[3] * 8)},
            "reason_codes": [], "state_sha256": target.read().evidence()["state_sha256"]}


def accept(before, after):
    return {"rc": 0, "keep": True, "reason_codes": []}


def reject(before, after):
    return {"rc": 5, "keep": False, "reason_codes": ["guard_regressed"]}


class TransactionTests(unittest.TestCase):
    def run_txn(self, target=None, *, apply=displace, observe=measure,
                decide=accept, commit=False, engine=None):
        engine = engine or Transaction(target or BufferTarget())
        receipt = engine.run("candidate", apply, observe, decide, commit=commit,
                             timeout_s=10, cleanup_timeout_s=2)
        schema = json.loads((Path(__file__).resolve().parents[1] /
                             "tools/sculpt/txn/stroke.schema.json").read_text())
        core.validate(receipt, schema)
        self.assertNotEqual(receipt["rc"], 1)
        return receipt

    def test_100_strokes_restore_exact_bytes_and_signed_zero(self):
        target = BufferTarget()
        original = target.read()
        engine = Transaction(target)
        for index in range(100):
            with self.subTest(stroke=index):
                receipt = self.run_txn(target, engine=engine)
                self.assertEqual(receipt["rc"], 0)
                self.assertTrue(receipt["restored"])
                self.assertNotEqual(receipt["before"]["vertex_sha256"],
                                    receipt["proposed"]["vertex_sha256"])
                self.assertEqual(receipt["before"], receipt["final"])
                self.assertEqual(target.read(), original)
        self.assertEqual(target.discarded, 100)
        self.assertEqual(target.state.vertices[:4], struct.pack("<f", -0.0))

    def test_residue_goes_red_and_fences_other_handle(self):
        target = BufferTarget(residue=True)
        engine = Transaction(target)
        receipt = self.run_txn(target, engine=engine)
        self.assertEqual(receipt["rc"], 5)
        self.assertEqual(receipt["cleanup_rc"], 5)
        self.assertIn("rollback_residue", receipt["reason_codes"])
        self.assertFalse(receipt["restored"])
        self.assertTrue(receipt["fenced"])
        self.assertIsNotNone(engine.pending_checkpoint)
        self.assertEqual(target.discarded, 0)
        before = target.read()
        second = self.run_txn(target, engine=Transaction(target))
        self.assertEqual(second["rc"], 3)
        self.assertEqual(second["action"], "REFUSE")
        self.assertEqual(target.read(), before)

    def test_approved_commit_and_next_reject_restore_latest_commit(self):
        target = BufferTarget()
        original = target.read()
        committed = self.run_txn(target, commit=True)
        self.assertEqual(committed["rc"], 0)
        self.assertEqual(committed["action"], "COMMIT")
        self.assertIsNone(committed["restored"])
        self.assertNotEqual(target.read(), original)
        approved = target.read()
        reverted = self.run_txn(target, decide=reject, commit=True)
        self.assertEqual(reverted["rc"], 5)
        self.assertTrue(reverted["restored"])
        self.assertEqual(target.read(), approved)

    def test_no_op_never_accepted(self):
        receipt = self.run_txn(apply=lambda t, d: None, commit=True)
        self.assertEqual(receipt["rc"], 5)
        self.assertIn("no_op_stroke", receipt["reason_codes"])

    def test_exception_after_partial_apply_restores(self):
        def broken(target, deadline):
            displace(target, deadline)
            raise RuntimeError("planted")
        receipt = self.run_txn(apply=broken, commit=True)
        self.assertEqual(receipt["rc"], 2)
        self.assertTrue(receipt["restored"])

    def test_interrupt_after_partial_apply_restores(self):
        def interrupted(target, deadline):
            displace(target, deadline)
            raise KeyboardInterrupt()
        receipt = self.run_txn(apply=interrupted, commit=True)
        self.assertEqual(receipt["rc"], 3)
        self.assertTrue(receipt["restored"])

    def test_deadline_after_partial_apply_restores_under_fresh_cleanup_deadline(self):
        clock = [0]
        def late(target, deadline):
            displace(target, deadline)
            clock[0] = 11_000_000_000
        with patch("tools.sculpt.txn.api.time.monotonic_ns", side_effect=lambda: clock[0]):
            receipt = self.run_txn(apply=late, commit=True)
        self.assertEqual(receipt["rc"], 6)
        self.assertTrue(receipt["restored"])
        self.assertEqual(receipt["cleanup_rc"], 0)

    def test_topology_mutation_rejected_and_restored(self):
        def topology(target, deadline):
            target.state = MeshState(target.state.vertices, b"changed")
        receipt = self.run_txn(apply=topology, commit=True)
        self.assertEqual(receipt["rc"], 5)
        self.assertTrue(receipt["restored"])

    def test_measurement_mutation_rejected_and_restored(self):
        def mutating(target, deadline):
            result = measure(target, deadline)
            displace(target, deadline)
            return result
        receipt = self.run_txn(observe=mutating, commit=True)
        self.assertEqual(receipt["rc"], 4)
        self.assertIn("measurement_mutated_state", receipt["reason_codes"])
        self.assertTrue(receipt["restored"])

    def test_missing_measurement_cannot_see(self):
        receipt = self.run_txn(observe=lambda t, d: {
            **measure(t, d), "metrics": {}})
        self.assertEqual(receipt["rc"], 3)
        self.assertTrue(receipt["restored"])

    def test_invalid_metric_or_rc_never_commits(self):
        for value in [True, 1.25, None]:
            with self.subTest(value=value):
                receipt = self.run_txn(observe=lambda t, d: {
                    **measure(t, d), "metrics": {"score": value}}, commit=True)
                self.assertEqual(receipt["rc"], 2)
                self.assertTrue(receipt["restored"])
        receipt = self.run_txn(decide=lambda b, a: {"rc": 1, "keep": False, "reason_codes": ["foreign"]})
        self.assertEqual(receipt["rc"], 2)

    def test_nonboolean_keep_refused(self):
        receipt = self.run_txn(decide=lambda b, a: {"rc": 0, "keep": 1, "reason_codes": []})
        self.assertEqual(receipt["rc"], 2)
        self.assertTrue(receipt["restored"])

    def test_replayed_measurement_rejected(self):
        target = BufferTarget()
        stale = measure(target, 0)
        receipt = self.run_txn(target, observe=lambda t, d: stale, commit=True)
        self.assertEqual(receipt["rc"], 4)
        self.assertIn("measurement_stale", receipt["reason_codes"])
        self.assertTrue(receipt["restored"])

    def test_judge_cannot_rewrite_measurement_receipt(self):
        def rewrite(before, after):
            before["score"] = 123456
            return accept(before, after)
        receipt = self.run_txn(decide=rewrite)
        self.assertEqual(receipt["rc"], 0)
        self.assertEqual(receipt["measurements"]["before"]["metrics"]["score"], 8)

    def test_discard_failure_fences_surface(self):
        target = BufferTarget()
        def broken(checkpoint):
            raise OSError("planted")
        target.discard = broken
        receipt = self.run_txn(target)
        self.assertEqual(receipt["rc"], 3)
        self.assertTrue(receipt["restored"])
        self.assertTrue(receipt["fenced"])

    def test_restore_error_fences_surface(self):
        target = BufferTarget()
        def broken(checkpoint):
            raise OSError("planted")
        target.restore = broken
        receipt = self.run_txn(target)
        self.assertEqual(receipt["rc"], 3)
        self.assertTrue(receipt["fenced"])
        self.assertIn("rollback_unproven", receipt["reason_codes"])

    def test_snapshot_corruption_fails_closed(self):
        target = BufferTarget()
        before = target.read()
        target.snapshot = lambda: Checkpoint(MeshState(target.state.vertices, b"corrupt"), None)
        receipt = self.run_txn(target)
        self.assertEqual(receipt["rc"], 4)
        self.assertEqual(receipt["action"], "REFUSE")
        self.assertTrue(receipt["fenced"])
        self.assertEqual(target.read(), before)

    def test_snapshot_failure_after_mutation_fences(self):
        target = BufferTarget()
        def broken():
            displace(target, 0)
            raise OSError("planted")
        target.snapshot = broken
        receipt = self.run_txn(target)
        self.assertEqual(receipt["rc"], 2)
        self.assertEqual(receipt["cleanup_rc"], 3)
        self.assertTrue(receipt["fenced"])

    def test_commit_drift_rolls_back(self):
        target = BufferTarget()
        read = target.read
        armed = [False]
        def read_with_drift():
            result = read()
            if armed[0]:
                armed[0] = False
                displace(target, 0)
            return result
        def arm_drift(before, after):
            armed[0] = True
            return accept(before, after)
        target.read = read_with_drift
        receipt = self.run_txn(target, decide=arm_drift, commit=True)
        self.assertEqual(receipt["rc"], 4)
        self.assertEqual(receipt["action"], "REVERT")
        self.assertTrue(receipt["restored"])

    def test_actor_reentrancy_refused_before_second_mutation(self):
        target = BufferTarget()
        nested = []
        def reenter(t, deadline):
            nested.append(self.run_txn(t))
            displace(t, deadline)
        receipt = self.run_txn(target, apply=reenter)
        self.assertEqual(receipt["rc"], 0)
        self.assertEqual(nested[0]["rc"], 5)
        self.assertEqual(nested[0]["reason_codes"], ["actor_busy"])

    def test_invalid_geometry_rejected(self):
        for data in [b"", b"bad", struct.pack("<3f", float("nan"), 0, 0)]:
            with self.subTest(data=data), self.assertRaises(core.ContractError):
                MeshState(data, b"topology")


if __name__ == "__main__":
    unittest.main()
