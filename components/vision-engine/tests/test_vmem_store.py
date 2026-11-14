import os
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from visioncore import canon
from visioncore.errors import VqaError
from vmem import blobs, gen as G, receipts
from vmem.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def code_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except VqaError as e:
        return e
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        self.s = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, tag, blobs_=()):
        with self.s.lock():
            return self.s.commit({"units/active.jsonl": tag.encode()}, blobs_, {"fg_count": 0}, strict=False)


class TestBlobs(Base):
    def test_put_get_dedup_immutable(self):
        sha = blobs.put_blob(self.root, b"abc", "bin")
        self.assertEqual(sha, canon.sha256_bytes(b"abc"))
        self.assertEqual(blobs.put_blob(self.root, b"abc", "bin"), sha)
        self.assertEqual(blobs.get_blob(self.root, sha), b"abc")
        self.assertTrue(blobs.is_immutable(self.root, sha))
        self.assertEqual(blobs.iter_blobs(self.root), [(sha, "bin")])
        self.assertTrue(blobs.verify_blob(self.root, sha))

    def test_corruption_detected(self):
        sha = blobs.put_blob(self.root, b"abc", "png")
        p = blobs.blob_path(self.root, sha, "png")
        os.chmod(p, 0o644)
        with open(p, "wb") as f:
            f.write(b"abd")
        self.assertFalse(blobs.verify_blob(self.root, sha, "png"))
        self.assertEqual(self.s.verify_blobs(), [sha])

    def test_bad_refs(self):
        self.assertEqual(code_of(blobs.blob_path, self.root, "xyz", "bin").code, "E_USAGE")
        self.assertEqual(code_of(blobs.blob_path, self.root, "a" * 64, "exe").code, "E_USAGE")
        self.assertEqual(code_of(blobs.get_blob, self.root, "a" * 64).code, "E_ID")


class TestGen(Base):
    def test_commit_flip_snapshot(self):
        self.assertIsNone(self.s.current())
        r1 = self.commit("one", [(b"png1", "png")])
        r2 = self.commit("two")
        self.assertEqual((r1["gen"], r2["gen"], self.s.current()), (1, 2, 2))
        snap = self.s.snapshot()
        self.assertEqual(snap.read("units/active.jsonl"), b"two")
        self.assertEqual(snap.manifest["parent_gen"], 1)
        self.assertEqual(snap.manifest["schema"], "vmem.manifest/v1")
        self.assertEqual(snap.manifest["files"]["units/active.jsonl"], canon.sha256_bytes(b"two"))
        self.assertTrue(snap.validate())
        self.assertTrue(os.path.islink(os.path.join(self.root, "CURRENT")))
        self.assertTrue(self.s.journal.verify_chain())

    def test_snapshot_stale_after_flip(self):
        self.commit("one")
        snap = self.s.snapshot()
        self.commit("two")
        self.assertEqual(code_of(snap.validate).rc, 4)

    def test_lock_held_rc4(self):
        with self.s.lock():
            e = code_of(Store(self.root).lock().__enter__)
            self.assertEqual((e.code, e.rc), ("E_STALE", 4))
        self.commit("after")  # released

    def test_commit_needs_lock(self):
        self.assertEqual(code_of(self.s.commit, {"a": b"x"}).code, "E_USAGE")

    def test_gen_exhausted_rc5(self):
        self.commit("one")
        os.makedirs(G.gen_dir(self.root, 999999))
        with open(os.path.join(G.gen_dir(self.root, 999999), "manifest.json"), "wb") as f:
            f.write(canon.dumps({"gen": 999999}).encode())
        os.symlink("gen/999999", os.path.join(self.root, "CURRENT.new"))
        os.replace(os.path.join(self.root, "CURRENT.new"), os.path.join(self.root, "CURRENT"))
        self.assertEqual(code_of(G.gen_name, 1000000).code, "E_GATE")
        e = code_of(self.commit, "x")
        self.assertEqual((e.code, e.rc, e.detail["reason"]), ("E_GATE", 5, "gen_exhausted"))
        self.assertEqual(self.s.current(), 999999)

    def test_current_without_manifest_rc4(self):
        self.commit("one")
        os.unlink(os.path.join(G.gen_dir(self.root, 1), "manifest.json"))
        e = code_of(self.s.snapshot)
        self.assertEqual((e.code, e.rc), ("E_STALE", 4))

    def test_bad_relpath_leaves_current(self):
        self.commit("one")
        with self.s.lock():
            e = code_of(self.s.commit, {"../x": b"1"})
        self.assertEqual(e.code, "E_USAGE")
        self.assertEqual(self.s.current(), 1)
        self.assertEqual(G.list_gens(self.root), [1])

    def test_float_in_extra_refused(self):
        self.commit("one")
        with self.s.lock():
            e = code_of(self.s.commit, {"a": b"1"}, (), {"x": 1.5})
        self.assertEqual(e.code, "E_FLOAT")
        self.assertEqual(self.s.current(), 1)

    def test_rollback_window(self):
        for i in range(6):
            self.commit("g%d" % i)
        with self.s.lock():
            self.assertEqual(self.s.rollback(3), 3)
            self.assertEqual(self.s.snapshot().read("units/active.jsonl"), b"g2")
        self.assertEqual(self.s.current(), 3)
        with self.s.lock():
            e = code_of(self.s.rollback, 1)  # CURRENT=3, keep=4 -> window [0,3]: gen 1 inside
        self.assertIsNone(e)
        self.assertEqual(self.s.current(), 1)

    def test_rollback_outside_window_rc3(self):
        for i in range(6):
            self.commit("g%d" % i)
        with self.s.lock():
            e = code_of(self.s.rollback, 2)  # window [3,6]
        self.assertEqual((e.code, e.rc), ("E_ID", 3))
        self.assertEqual(self.s.current(), 6)

    def test_gc_blobs(self):
        r = self.commit("one", [(b"keep", "bin"), (b"drop", "bin")])
        keep, drop = r["blobs"]
        with self.s.lock():
            gone = self.s.gc_blobs({keep})
        self.assertEqual(gone, [drop])
        self.assertEqual([s for s, _ in blobs.iter_blobs(self.root)], [keep])

    def test_gc_1000_blobs(self):
        self.commit("one", [(b"junk%d" % i, "bin") for i in range(1000)])
        with self.s.lock():
            gone = self.s.gc_blobs({})
        self.assertEqual(len(gone), 1000)
        self.assertEqual(blobs.iter_blobs(self.root), [])
        rec = [r for r in self.s.journal.records() if r["type"] == "gc"][0]
        self.assertEqual(rec["body"]["n"], 1000)
        self.assertLessEqual(len(canon.dumps(rec)), receipts.VM_JREC_MAX)
        self.assertTrue(self.s.journal.verify_chain())

    def test_gc_journals_before_unlink(self):
        self.commit("one", [(b"junk", "bin")])
        with mock.patch("vmem.store.delete_blob", side_effect=OSError("crash")):
            with self.s.lock():
                with self.assertRaises(OSError):
                    self.s.gc_blobs({})
        self.assertEqual([r["type"] for r in self.s.journal.records()].count("gc"), 1)

    def test_deterministic_manifest(self):
        a = self.commit("one", [(b"x", "bin")])
        root2 = os.path.join(self.tmp.name, "store2")
        s2 = Store(root2)
        with s2.lock():
            b = s2.commit({"units/active.jsonl": b"one"}, [(b"x", "bin")], {"fg_count": 0}, strict=False)
        self.assertEqual(a["manifest_sha256"], b["manifest_sha256"])


class TestJournal(Base):
    def test_journal_checkpoint(self):
        j = receipts.Journal(os.path.join(self.tmp.name, "j"), seg_cap=4096)
        for i in range(50):
            j.append("pin", {"id": "V-BAS-%06d" % i, "pad": "x" * 200})
        nseg = len(j.segments())
        self.assertGreater(nseg, 2)
        b = j.checkpoint(40)
        self.assertEqual(b["seq"], 40)
        self.assertLess(len(j.segments()), nseg)
        self.assertTrue(j.verify_chain())
        r = j.append("pin", {"id": "V-BAS-999999"})
        self.assertEqual(r["seq"], 51)
        self.assertTrue(j.verify_chain())
        with open(j.path(j.segments()[-1]), "ab") as f:
            f.write(b"")
        recs = j.records()
        recs[0]["body"]["id"] = "tamper"
        self.assertNotEqual(recs[0]["body"]["id"], "V-BAS-000000")

    def test_compact_journal_after_gens(self):
        for i in range(8):
            self.commit("g%d" % i)
        with self.s.lock():
            base = self.s.compact_journal()
        self.assertIsNotNone(base)
        self.assertTrue(self.s.journal.verify_chain())

    def test_chain_and_tamper(self):
        j = self.s.journal
        for i in range(3):
            j.append("pin", {"id": "V-FAIL-%d" % i})
        self.assertTrue(j.verify_chain())
        p = j.path(1)
        with open(p, "rb") as f:
            data = f.read().replace(b"V-FAIL-1", b"V-FAIL-9")
        with open(p, "wb") as f:
            f.write(data)
        self.assertFalse(j.verify_chain())

    def test_torn_tail_recovered(self):
        j = self.s.journal
        j.append("pin", {"id": "a"})
        j.append("pin", {"id": "b"})
        with open(j.path(1), "ab") as f:
            f.write(b'{"seq":3,"prev":"')
        self.assertGreater(j.recover_torn(), 0)
        self.assertEqual(len(j.records()), 2)
        self.assertTrue(j.verify_chain())
        j.append("pin", {"id": "c"})
        self.assertTrue(j.verify_chain())

    def test_rotation_subtraction_form(self):
        self.assertFalse(receipts.rotates(10, 5, 15))
        self.assertTrue(receipts.rotates(11, 5, 15))
        # no wrap: huge used never makes a negative sum pass
        self.assertTrue(receipts.rotates(2**64 - 1, 2**64 - 1, 2**20))

    def test_rotates_segments(self):
        j = receipts.Journal(self.tmp.name + "/j", seg_cap=300, rec_max=300)
        for i in range(8):
            j.append("pin", {"id": "x" * 60})
        self.assertGreater(len(j.segments()), 1)
        self.assertTrue(j.verify_chain())
        for n in j.segments():
            self.assertLessEqual(os.path.getsize(j.path(n)), 300)

    def test_record_max_rc6(self):
        j = receipts.Journal(self.tmp.name + "/j2", seg_cap=1000, rec_max=100)
        self.assertEqual(code_of(j.append, "pin", {"id": "y" * 200}).rc, 6)

    def test_bad_type(self):
        self.assertEqual(code_of(self.s.journal.append, "nope").code, "E_USAGE")

    def test_receipt_sha(self):
        self.assertEqual(receipts.receipt_sha256(b"m"), canon.sha256_bytes(b"m"))


CHILD = r"""
import os, signal, sys
from vmem.store import Store
root, point = sys.argv[1], sys.argv[2]
s = Store(root)
def hook(name):
    if name == point:
        os.kill(os.getpid(), signal.SIGKILL)
s.hook = hook
with s.lock():
    s.commit({"units/active.jsonl": b"new"}, [(b"newblob", "bin")], {"fg_count": 0}, strict=False)
"""


class TestCrash(Base):
    def run_child(self, point):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=ROOT)
        p = subprocess.run([sys.executable, "-c", CHILD, self.root, point], cwd=ROOT, env=env, timeout=20)
        return p.returncode

    def test_kill_at_every_point_keeps_old_or_new(self):
        for point in G.POINTS:
            with self.subTest(point=point):
                self.tearDown(); self.setUp()
                self.commit("old", [(b"oldblob", "bin")])
                self.assertEqual(self.run_child(point), -signal.SIGKILL)
                cur = self.s.current()
                self.assertIn(cur, (1, 2))
                snap = self.s.snapshot()
                want = b"old" if cur == 1 else b"new"
                self.assertEqual(snap.read("units/active.jsonl"), want)
                for rel, sha in snap.manifest["files"].items():
                    self.assertEqual(canon.sha256_bytes(snap.read(rel)), sha)
                self.assertEqual(self.s.verify_blobs(), [])
                # flip happens at current_replaced: earlier kills keep gen 1
                if point in ("stage_written", "gen_dir_fsynced", "gen_renamed", "gens_dir_fsynced", "current_tmp_created"):
                    self.assertEqual(cur, 1)
                else:
                    self.assertEqual(cur, 2)
                # recovery under lock heals state and the store accepts a further commit
                r = self.commit("after")
                self.assertEqual(r["gen"], cur + 1)
                self.assertTrue(self.s.journal.verify_chain())
                self.assertEqual(sorted(x for x in os.listdir(os.path.join(self.root, "gen")) if x.endswith(".tmp")), [])
                self.assertFalse(os.path.exists(os.path.join(self.root, "CURRENT.tmp")))

    def test_kill_after_flip_reconciles_journal(self):
        self.commit("old")
        self.run_child("root_fsynced")
        with self.s.lock():
            pass
        flips = [r["body"]["gen"] for r in self.s.journal.records() if r["type"] == "flip"]
        self.assertEqual(flips, [1, 2])

    def test_unacked_gen_dir_removed(self):
        self.commit("old")
        self.run_child("gens_dir_fsynced")  # gen/000002 renamed, CURRENT still 1
        self.assertIn(2, G.list_gens(self.root))
        self.assertEqual(self.s.current(), 1)
        with self.s.lock():
            pass
        self.assertEqual(G.list_gens(self.root), [1])


if __name__ == "__main__":
    unittest.main()
