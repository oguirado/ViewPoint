"""FX-GC2: gc_blobs deletes generations below the rollback window (SPEC_VMEM §7, DECISION_L2b)."""
import os
import signal
import subprocess
import sys
import unittest

from vmem import gen as G
from vmem.store import Store
from visioncore.errors import VqaError
from tests.test_e8_gc_release import Base, REPO


def tree_bytes(root):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _s, fs in os.walk(str(root)) for f in fs)


CHILD = r'''
import os, signal, sys
from vmem import gen as G
from vmem.store import Store
root = sys.argv[1]
real = G.delete_gen
def dele(r, n):
    real(r, n)
    os.kill(os.getpid(), signal.SIGKILL)
G.delete_gen = dele
st = Store(root)
with st.lock():
    st.gc_blobs()
'''


class GenPrune(Base):
    def _scenario(self):
        for r in (1, 2, 3):
            self.ingest(r)
        self.compact(k_clean=1)
        self.idle_gens(G.VM_GEN_KEEP)

    def test_gens_outside_window_deleted(self):
        self._scenario()
        st = Store(self.root)
        cur = st.current()
        self.assertEqual(G.list_gens(self.root)[0], 1)
        before = tree_bytes(self.root)
        with st.lock():
            st.gc_blobs()
            self.assertEqual(G.list_gens(self.root), list(range(cur - G.VM_GEN_KEEP + 1, cur + 1)))
            low = cur - G.VM_GEN_KEEP + 1
            st.rollback(low)
            with self.assertRaises(VqaError) as cm:
                st.rollback(low - 1)
            self.assertEqual(cm.exception.rc, 3)
            self.assertEqual(st.verify_blobs(), [])
        self.assertLess(tree_bytes(self.root), before)

    def test_second_gc_deletes_nothing(self):
        self._scenario()
        st = Store(self.root)
        with st.lock():
            st.gc_blobs()
            gens = G.list_gens(self.root)
            self.assertEqual(st.gc_blobs(), [])
            self.assertEqual(G.list_gens(self.root), gens)

    def test_sigkill_mid_prune_resumable(self):
        self._scenario()
        res = subprocess.run([sys.executable, "-c", CHILD, str(self.root)], cwd=REPO, capture_output=True, timeout=30,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO)))
        self.assertEqual(res.returncode, -signal.SIGKILL, res.stderr.decode())
        st = Store(self.root)
        with st.lock():
            st.recover()
            cur = st.current()
            self.assertEqual(st.verify_blobs(), [])
            st.gc_blobs()
            self.assertEqual(G.list_gens(self.root), list(range(cur - G.VM_GEN_KEEP + 1, cur + 1)))
            self.assertEqual(st.current(), cur)


if __name__ == "__main__":
    unittest.main()
