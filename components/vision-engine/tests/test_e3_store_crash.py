"""E-V6: bounded SIGKILL trials through real ingest and post-crash recovery."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from visioncore import canon
from vmem import gates, gen as G, ingest
from vmem.blobs import iter_blobs
from vmem.store import Store
from tests.test_vmem_fixes import spool_lines

REPO = Path(__file__).resolve().parents[1]
CHILD = r'''
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


class IngestCrash(unittest.TestCase):
    def test_ingest_thirty_sigkills_old_or_new_and_recovery(self):
        scratch = REPO / "build" / "room_V1"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            for trial in range(30):
                point = G.POINTS[trial % len(G.POINTS)]
                with self.subTest(trial=trial, point=point):
                    work = Path(temporary) / str(trial)
                    work.mkdir()
                    root = work / "store"
                    def spool(run, desc):
                        path = work / ("run%d.jsonl" % run)
                        path.write_text("".join(canon.dumps(r) for r in spool_lines(run, desc, "FAIL")))
                        return str(path)
                    ingest.ingest(root, spool(1, 0x10))
                    pending = spool(2, 0x9990000)
                    result = subprocess.run(
                        [sys.executable, "-c", CHILD, str(root), pending, point], cwd=REPO,
                        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO)),
                        capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
                    st = Store(root)
                    with st.lock():
                        self.assertIn(st.current(), (1, 2))
                        snap = st.snapshot()
                        for name, expected in snap.manifest["files"].items():
                            self.assertEqual(canon.sha256_bytes(snap.read(name)), expected)
                        self.assertEqual(st.verify_blobs(), [])
                        self.assertTrue(st.journal.verify_chain())
                        self.assertFalse(any(p.name.endswith(".tmp") for p in (root / "gen").iterdir()))
                        self.assertFalse((root / "CURRENT.tmp").exists())
                        for sha, ext in iter_blobs(root):
                            self.assertIn(ext, ("bin", "png", "mp4"))
                    self.assertEqual(gates.failed(gates.verify(root)), [])
                    ingest.ingest(root, pending)  # retries either finish old work or deduplicate the durable flip
                    self.assertEqual(Store(root).current(), 2)
                    ingest.ingest(root, spool(3, 0x99999900))
                    self.assertEqual(Store(root).current(), 3)
                    self.assertEqual(gates.failed(gates.verify(root)), [])


if __name__ == "__main__":
    unittest.main()
