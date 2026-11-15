"""E8 lane V: episodes live in gen/<n>/episodes/<run_id>.jsonl without the spool transport field; GOP bytes once."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from visioncore import canon
from vmem import gen as G, ingest
from vmem.blobs import iter_blobs
from vmem.store import Store
from tests.test_vmem_fixes import episode, spool_lines

SCRATCH = Path(__file__).resolve().parents[1] / "build" / "room_V"
GOP = bytes(range(256)) * 8


class EpisodeStorage(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"

    def ingest(self, n=1, verdict="PASS"):
        ep = episode(n, gop_blob_sha256=canon.sha256_bytes(GOP), gop_blob_hex=GOP.hex())
        sp = Path(self.tmp.name) / ("s%d.jsonl" % n)
        sp.write_text("".join(canon.dumps(r) for r in spool_lines(n, 0x10, verdict, ep=ep)))
        ingest.ingest(self.root, str(sp))
        return Store(self.root)

    def test_no_hex_duplicate_and_gop_stored_once(self):
        st = self.ingest()
        blobs = [Path(self.root, "blobs", "sha256", s[:2], s + "." + e).read_bytes() for s, e in iter_blobs(self.root)]
        self.assertFalse([b for b in blobs if GOP.hex().encode() in b], "a blob still carries gop_blob_hex")
        self.assertEqual(sum(1 for b in blobs if b == GOP), 1)
        self.assertEqual(st.current(), 1)

    def test_episode_jsonl_in_generation(self):
        st = self.ingest()
        p = Path(G.gen_dir(self.root, 1)) / "episodes" / "r_000001_00000001.jsonl"
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("gop_blob_hex", lines[0])
        self.assertEqual(lines[0]["run_id"], "r_000001_00000001")
        self.assertEqual(lines[0]["gop_blob_sha256"], canon.sha256_bytes(GOP))

    def test_episodes_carried_to_next_generation(self):
        self.ingest(1)
        self.ingest(2)
        d = Path(G.gen_dir(self.root, 2)) / "episodes"
        self.assertEqual(sorted(os.listdir(d)), ["r_000001_00000001.jsonl", "r_000002_00000002.jsonl"])


if __name__ == "__main__":
    unittest.main()
