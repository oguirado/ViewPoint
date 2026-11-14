"""E8 lane C: clean compaction retires BG episodes AND releases their GOP blobs after the rollback window (SPEC_VMEM §4/§7)."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from visioncore import canon
from vmem import compact as C, gen as G, ingest, units as U
from vmem.blobs import iter_blobs
from vmem.store import Store
from tests.test_vmem_fixes import episode, spool_lines

REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO / "build" / "room_GC"


def gop(n):
    return bytes([n]) * 600 + bytes(range(256))


def sha(n):
    return canon.sha256_bytes(gop(n))


CHILD = r'''
import os, signal, sys
from unittest.mock import patch
from vmem import ingest, compact as C
from vmem import store as S
from vmem.store import Store
root, mode = sys.argv[1:]
st = Store(root)
with st.lock():
    st.hook = (lambda n: os.kill(os.getpid(), signal.SIGKILL) if n == "gc_journaled" else None) if mode == "journaled" else None
    if mode == "unlink":
        real, cnt = S.delete_blob, [0]
        def dele(*a):
            real(*a)
            cnt[0] += 1
            if cnt[0] == 1:
                os.kill(os.getpid(), signal.SIGKILL)
        S.delete_blob = dele
    st.gc_blobs()
'''


class Base(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"

    def ingest(self, run, verdict="PASS"):
        ep = episode(run, gop_blob_sha256=sha(run), gop_blob_hex=gop(run).hex(),
                     verdicts=[{"verdict": verdict}], fg=verdict == "FAIL")
        sp = Path(self.tmp.name) / ("s%d.jsonl" % run)
        sp.write_text("".join(canon.dumps(r) for r in spool_lines(run, 0x10 + run * 0x1000, verdict, ep=ep)))
        ingest.ingest(self.root, str(sp))

    def blobs(self):
        return {s for s, _e in iter_blobs(self.root)}

    def compact(self, k_clean):
        st = Store(self.root)
        with st.lock():
            active, archive = ingest._load(st)
            sizes = {s: os.stat(ingest.blob_path(self.root, s, e)).st_size for s, e in iter_blobs(self.root)}
            plan = C.compact(active, sizes, k_clean=k_clean, gen=(st.current() or 0) + 1)
            ingest.write_gen(st, plan["active"], archive + plan["archive"])
            return plan

    def idle_gens(self, n):
        st = Store(self.root)
        with st.lock():
            for _ in range(n):
                active, archive = ingest._load(st)
                ingest.write_gen(st, active, archive)


class ReleaseAfterWindow(Base):
    def test_retired_clean_blob_freed_only_after_window(self):
        for r in (1, 2, 3):
            self.ingest(r)
        self.assertTrue({sha(1), sha(2), sha(3)} <= self.blobs())
        plan = self.compact(k_clean=1)
        self.assertEqual(len(plan["retired"]), 2)
        st = Store(self.root)
        with st.lock():
            self.assertEqual(st.gc_blobs(), [])  # parent gens (inside the window) still reference them
        self.assertTrue({sha(1), sha(2), sha(3)} <= self.blobs())
        self.idle_gens(G.VM_GEN_KEEP - 1)  # the compacting gen is now the oldest in the window
        with st.lock():
            dropped = st.gc_blobs()
        self.assertEqual(set(dropped) & {sha(1), sha(2), sha(3)}, {sha(1), sha(2)})
        self.assertIn(sha(3), self.blobs())
        self.assertNotIn(sha(1), self.blobs())

    def test_rollback_inside_window_still_has_blobs(self):
        for r in (1, 2, 3):
            self.ingest(r)
        pre = Store(self.root).current()
        self.compact(k_clean=1)
        self.idle_gens(G.VM_GEN_KEEP - 2)
        st = Store(self.root)
        with st.lock():
            st.gc_blobs()
            st.rollback(pre)  # still inside the window
            self.assertEqual(st.current(), pre)
            self.assertEqual(st.verify_blobs(), [])
            self.assertTrue({sha(1), sha(2), sha(3)} <= self.blobs())
            refs = st.window_refs()
            self.assertTrue({sha(1), sha(2), sha(3)} <= refs)

    def test_fail_and_foreground_blobs_never_freed(self):
        self.ingest(1, "FAIL")
        for r in (2, 3, 4):
            self.ingest(r)
        plan = self.compact(k_clean=1)
        self.assertEqual(len(plan["retired"]), 2)  # runs 2 and 3; FAIL run 1 is dirty and untouched
        self.idle_gens(G.VM_GEN_KEEP + 1)
        st = Store(self.root)
        with st.lock():
            st.gc_blobs()
            self.assertEqual(st.verify_blobs(), [])
        self.assertIn(sha(1), self.blobs())
        self.assertIn(sha(4), self.blobs())
        self.assertNotIn(sha(2), self.blobs())
        self.assertNotIn(sha(3), self.blobs())
        active, archive = ingest._load(Store(self.root))
        for u in active + archive:
            if U.is_fg(u):
                self.assertTrue(all(self.blobs() >= {b} for b in U.blob_refs(u)))

    def test_shared_blob_kept_when_active_unit_references_it(self):
        ep = {"gop_blob_sha256": sha(9)}
        for r in (1, 2, 3):
            e = episode(r, gop_blob_sha256=sha(9), gop_blob_hex=gop(9).hex())
            sp = Path(self.tmp.name) / ("s%d.jsonl" % r)
            sp.write_text("".join(canon.dumps(x) for x in spool_lines(r, 0x10 + r * 0x1000, "PASS", ep=e)))
            ingest.ingest(self.root, str(sp))
        self.compact(k_clean=1)  # run 3's episode still points at the shared blob
        self.idle_gens(G.VM_GEN_KEEP + 1)
        st = Store(self.root)
        with st.lock():
            st.gc_blobs()
        self.assertIn(ep["gop_blob_sha256"], self.blobs())

    def test_gc_idempotent(self):
        for r in (1, 2, 3):
            self.ingest(r)
        self.compact(k_clean=1)
        self.idle_gens(G.VM_GEN_KEEP)
        st = Store(self.root)
        with st.lock():
            first = st.gc_blobs()
            self.assertTrue(first)
            self.assertEqual(st.gc_blobs(), [])


class GcCrash(Base):
    def _prepare(self):
        for r in (1, 2, 3):
            self.ingest(r)
        self.compact(k_clean=1)
        self.idle_gens(G.VM_GEN_KEEP)

    def _crash(self, mode):
        res = subprocess.run([sys.executable, "-c", CHILD, str(self.root), mode], cwd=REPO, capture_output=True,
                             timeout=30, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO)))
        self.assertEqual(res.returncode, -signal.SIGKILL, res.stderr.decode())

    def test_sigkill_during_gc_is_safe_and_resumable(self):
        for mode in ("journaled", "unlink"):
            with self.subTest(mode=mode):
                self.tearDown_store()
                self._prepare()
                self._crash(mode)
                st = Store(self.root)
                with st.lock():
                    st.recover()
                    self.assertEqual(st.verify_blobs(), [])
                    self.assertTrue(st.journal.verify_chain())
                    self.assertTrue(sha(3) in self.blobs())  # live blob never touched
                    st.gc_blobs()  # resume completes the sweep
                    self.assertNotIn(sha(1), self.blobs())
                    self.assertNotIn(sha(2), self.blobs())
                    self.assertIn(sha(3), self.blobs())
                    self.assertEqual(st.verify_blobs(), [])

    def tearDown_store(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
