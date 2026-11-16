"""Adversarial review regressions: real bytes and owned-process boundaries."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest import mock

from test_f2u1_mesh import api, contracts, worker, glb, pack, policy, request


class ReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw, self.candidate = self.root / "raw.glb", self.root / "candidate.glb"
        self.raw.write_bytes(glb())
        self.candidate.write_bytes(glb())

    def compare(self, **changes):
        return api.compare_meshes(self.raw, self.candidate, policy=policy(**changes))

    def test_review_a_malformed_glb_subset_is_refused(self):
        self.assertEqual(self.compare()[0], 0)
        raw = self.raw.read_bytes()
        size = struct.unpack_from("<I", raw, 12)[0]
        document = json.loads(raw[20:20 + size])
        binary = raw[28 + size:]
        mutations = {
            "short_view": lambda d: d["bufferViews"][0].update(byteLength=1),
            "buffer_reference": lambda d: d["bufferViews"][0].update(buffer=99),
            "negative_accessor": lambda d: d["meshes"][0]["primitives"][0]["attributes"].update(POSITION=-2),
            "fractional_count": lambda d: d["accessors"][0].update(count=12.9),
            "asset_version": lambda d: d["asset"].update(version="999.0"),
            "nan_extras": lambda d: d.update(extras={"ignored": float("nan")}),
            "boolean_reference": lambda d: d["bufferViews"][0].update(buffer=False),
            "negative_count": lambda d: d["accessors"][0].update(count=-1),
            "buffer_bounds": lambda d: d["buffers"][0].update(byteLength=1),
            "accessor_view_bounds": lambda d: d["accessors"][0].update(byteOffset=4),
            "fractional_mesh": lambda d: d["nodes"][0].update(mesh=0.0),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                doc = copy.deepcopy(document)
                mutate(doc)
                self.candidate.write_bytes(pack(doc, binary))
                rc, rec = self.compare()
                self.assertEqual(rc, 2, rec)

    def _bounded_probe(self, callback, expected):
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        def invoke():
            receiver.close()
            try:
                sender.send(callback())
            finally:
                sender.close()
        child = context.Process(target=invoke)
        started = time.monotonic()
        child.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(3), "API did not return within external probe deadline")
            rc, rec = receiver.recv()
            self.assertEqual(rc, expected, rec)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            if child.is_alive():
                child.kill()
            child.join(timeout=1)
            receiver.close()

    def test_review_b_fifo_input_and_receipt_are_refused_without_blocking(self):
        fifo = self.root / "input.fifo"
        os.mkfifo(fifo)
        self._bounded_probe(lambda: api.compare_meshes(fifo, self.candidate, policy=policy(timeout_s=1)), 2)
        self._bounded_probe(lambda: api.read_receipt(fifo, self.raw, self.candidate,
                                                    policy=policy(timeout_s=1)), 2)

    def test_review_b_blocked_acquisition_and_rechecks_obey_one_deadline(self):
        original = api._read
        calls = 0
        def block(path):
            nonlocal calls
            calls += 1
            time.sleep(10)
            return original(path)
        with mock.patch.object(api, "_read", side_effect=block):
            self._bounded_probe(lambda: self.compare(timeout_s=1), 6)
        # A later input recheck must have the same bounded acquisition path.
        original_spawn = api._spawn
        def block_after_evaluation(*args):
            result = original_spawn(*args)
            api._read = block
            return result
        with mock.patch.object(api, "_spawn", side_effect=block_after_evaluation):
            self._bounded_probe(lambda: self.compare(timeout_s=1), 6)

    def test_review_c_real_child_malformed_envelopes_return_error(self):
        child = self.root / "malformed_worker.py"
        for envelope in ({"receipt": None, "artefact": None}, {"receipt": [], "artefact": None},
                         {"receipt": {"rc": False}, "artefact": None}, [], None):
            with self.subTest(envelope=envelope):
                child.write_text("import sys\nsys.stdin.buffer.read()\nprint(" + repr(json.dumps(envelope)) + ")\n")
                with mock.patch.object(api, "WORKER", child):
                    rc, rec = self.compare()
                self.assertEqual(rc, 2, rec)

    def test_review_d_every_stored_evidence_field_is_bound(self):
        rc, rec = self.compare()
        self.assertEqual(rc, 0, rec)
        path = self.root / "receipt.json"
        for field, value in (("artefact_sha256", "0" * 64), ("prev_sha256", "1" * 64),
                             ("unavailable", {"extra": "altered evidence"})):
            with self.subTest(field=field):
                changed = copy.deepcopy(rec)
                changed[field] = value
                path.write_text(contracts.canonical(changed))
                self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 4)
        path.write_text(contracts.canonical(rec))
        self.assertEqual(api.read_receipt(path, self.raw, self.candidate, policy=policy())[0], 0)

    def test_review_d_success_contract_binds_artefact_identity(self):
        rc, rec = self.compare()
        self.assertEqual(rc, 0, rec)
        contracts.validate_success(rec, "compare")
        rec["artefact_sha256"] = "0" * 64
        with self.assertRaises(contracts.MeshError) as caught:
            contracts.validate_success(rec, "compare")
        self.assertEqual(caught.exception.rc, 2)

    def test_review_e_proven_failure_outranks_later_unavailable_evidence(self):
        req = request("compare", glb(), glb(), policy(require_uv=True))
        with mock.patch.object(worker, "_silhouette", side_effect=contracts.MeshError(3, "silhouette_unavailable")):
            rec = worker.evaluate(req)["receipt"]
        self.assertIn("topology:uv_missing", rec["failed"])
        self.assertEqual(rec["rc"], 5, rec)
        self.assertEqual(rec["status"], "FAIL")
        self.assertEqual(rec["unavailable"]["evaluation"], "silhouette_unavailable")


if __name__ == "__main__":
    unittest.main()
