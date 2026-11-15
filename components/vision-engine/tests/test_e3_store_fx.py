"""FX-E r1 W4: RW4-1 stale read-only blob tmp, RW4-2 gc window, RW4-3 fail-closed window, RW4-4 blob/gc kill points."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from visioncore import canon
from visioncore.errors import VqaError
from vmem import blobs, gates, gen as G, ingest
from vmem.store import Store, BLOB_POINTS
from tests.test_vmem_fixes import spool_lines

REPO = Path(__file__).resolve().parents[1]


def unit_line(i, sha):
    return canon.dumps({"id": "V-EVT-%06d" % i, "anchors": [{"crop": sha}], "look": None})


class StaleTmp(unittest.TestCase):
    def test_stale_readonly_blob_tmp_retry(self):  # RW4-1
        with tempfile.TemporaryDirectory() as t:
            data = b"payload"
            sha = canon.sha256_bytes(data)
            path = blobs.blob_path(t, sha, "bin")
            os.makedirs(os.path.dirname(path))
            with open(path + ".tmp", "wb") as f:
                f.write(b"torn")
            os.chmod(path + ".tmp", 0o444)
            self.assertEqual(blobs.put_blob(t, data), sha)
            self.assertTrue(blobs.verify_blob(t, sha))
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_recover_removes_blob_tmps(self):  # RW4-1
        with tempfile.TemporaryDirectory() as t:
            sha = canon.sha256_bytes(b"x")
            path = blobs.blob_path(t, sha, "bin")
            os.makedirs(os.path.dirname(path))
            with open(path + ".tmp", "wb") as f:
                f.write(b"torn")
            os.chmod(path + ".tmp", 0o444)
            with Store(t).lock():
                pass
            self.assertFalse(os.path.exists(path + ".tmp"))


class GcWindow(unittest.TestCase):
    def test_gc_keeps_rollback_window_blobs(self):  # RW4-2 (kills n >= cur mutant)
        with tempfile.TemporaryDirectory() as t:
            st = Store(t)
            shas = []
            with st.lock():
                for k in range(1, 7):
                    sha = canon.sha256_bytes(b"blob-%d" % k)
                    shas.append(sha)
                    st.commit({"units/active.jsonl": unit_line(k, sha).encode()},
                              blobs=[(b"blob-%d" % k, "bin")], strict=False)
                self.assertEqual(st.current(), 6)
                deleted = set(st.gc_blobs())
                keep = shas[6 - G.VM_GEN_KEEP:]
                self.assertEqual(deleted, set(shas[:6 - G.VM_GEN_KEEP]))
                have = {s for s, _e in blobs.iter_blobs(t)}
                for sha in keep:
                    self.assertIn(sha, have)
                st.rollback(6 - G.VM_GEN_KEEP + 1)
                self.assertEqual(st.verify_blobs(), [])
                for u in st.gen_units(st.current()):
                    from vmem import units as U
                    self.assertLessEqual(U.blob_refs(u), have)

    def test_window_refs_fail_closed(self):  # RW4-3
        with tempfile.TemporaryDirectory() as t:
            st = Store(t)
            with st.lock():
                st.commit({"units/active.jsonl": canon.dumps({"id": "V-EVT-000001", "look": None}).encode()},
                          strict=False)
                with self.assertRaises(VqaError) as cm:
                    st.window_refs()
                self.assertEqual(cm.exception.code, "E_GATE")
                self.assertEqual(cm.exception.detail["reason"], "g_window")
                with self.assertRaises(VqaError):
                    st.gc_blobs()


CHILD_GC = r'''
import os, signal, sys
from vmem.store import Store
root, point = sys.argv[1:]
st = Store(root)
def kill(name):
    if name == point:
        os.kill(os.getpid(), signal.SIGKILL)
st.hook = kill
with st.lock():
    st.gc_blobs()
'''
CHILD_INGEST = r'''
import os, signal, sys
from unittest.mock import patch
from vmem import ingest
from vmem.store import Store
root, spool, point = sys.argv[1:]
st = Store(root)
def kill(name):
    if name == point:
        os.kill(os.getpid(), signal.SIGKILL)
st.hook = kill
with patch.object(ingest, "Store", return_value=st):
    ingest.ingest(root, spool)
'''


def run_child(code, *args):
    return subprocess.run([sys.executable, "-c", code, *args], cwd=REPO, capture_output=True, timeout=20,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO)))


class BlobGcKill(unittest.TestCase):
    def test_hook_points_declared(self):  # RW4-4
        self.assertEqual(BLOB_POINTS, ("blob_tmp_written", "blob_chmod", "gc_journaled"))

    def test_blob_path_kills_then_retry(self):  # RW4-4 (+RW4-1 end to end)
        scratch = REPO / "build" / "room_F4"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as t:
            for point in ("blob_tmp_written", "blob_chmod"):
                with self.subTest(point=point):
                    work = Path(t) / point
                    work.mkdir()
                    root = work / "store"

                    def spool(run, desc):
                        p = work / ("run%d.jsonl" % run)
                        p.write_text("".join(canon.dumps(r) for r in spool_lines(run, desc, "FAIL")))
                        return str(p)
                    ingest.ingest(root, spool(1, 0x10))
                    pending = spool(2, 0x9990000)
                    res = run_child(CHILD_INGEST, str(root), pending, point)
                    self.assertEqual(res.returncode, -signal.SIGKILL, res.stderr.decode())
                    leftover = [n for _d, _s, fs in os.walk(root / "blobs") for n in fs if n.endswith(".tmp")]
                    self.assertTrue(leftover, "kill point must leave a blob tmp")
                    with Store(root).lock() as st:
                        self.assertEqual(st.current(), 1)
                        self.assertEqual(st.verify_blobs(), [])
                    self.assertFalse([n for _d, _s, fs in os.walk(root / "blobs") for n in fs if n.endswith(".tmp")])
                    ingest.ingest(root, pending)
                    self.assertEqual(Store(root).current(), 2)
                    self.assertEqual(gates.failed(gates.verify(root)), [])

    def test_blob_tmp_retry_without_recover(self):  # RW4-1: retry alone (put_blob) heals
        with tempfile.TemporaryDirectory(dir=(REPO / "build").as_posix() if (REPO / "build").is_dir() else None) as t:
            work = Path(t)
            sha = canon.sha256_bytes(b"retry")
            path = blobs.blob_path(work / "s", sha, "bin")
            os.makedirs(os.path.dirname(path))
            Path(path + ".tmp").write_bytes(b"x")
            os.chmod(path + ".tmp", 0o444)
            blobs.put_blob(work / "s", b"retry")
            self.assertTrue(blobs.verify_blob(work / "s", sha))

    def test_gc_journaled_kill_then_resume(self):  # RW4-4
        scratch = REPO / "build" / "room_F4"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as t:
            root = Path(t) / "store"
            st = Store(root)
            with st.lock():
                sha0 = canon.sha256_bytes(b"keep")
                st.commit({"units/active.jsonl": unit_line(1, sha0).encode()}, blobs=[(b"keep", "bin")], strict=False)
                orphan = blobs.put_blob(root, b"orphan")
            res = run_child(CHILD_GC, str(root), "gc_journaled")
            self.assertEqual(res.returncode, -signal.SIGKILL, res.stderr.decode())
            self.assertIn((orphan, "bin"), blobs.iter_blobs(root))  # journaled, not yet unlinked
            with Store(root).lock() as st2:
                self.assertTrue(st2.journal.verify_chain())
                self.assertEqual(st2.gc_blobs(), [orphan])
                self.assertEqual(st2.verify_blobs(), [])
                self.assertEqual(st2.gc_blobs(), [])
                self.assertTrue(st2.journal.verify_chain())


if __name__ == "__main__":
    unittest.main()
