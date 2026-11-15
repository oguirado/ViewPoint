"""Repository conventions: module headers, unique anchors, llms.txt size, HyperBook admission,
STORAGE LAW (no image files tracked), and the plain-node JS tests."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tools import anchors as anc
from tools import reseal

ROOT = Path(__file__).resolve().parents[1]
HB = ROOT / "hyperbooks"
SECTION_RE = re.compile(r"<!-- HB:BEGIN hb.section.v2 -->\s*```json\s*(\{.*?\})\s*```", re.S)


class TestConventions(unittest.TestCase):
    def test_llms_and_agents_size(self):
        self.assertLessEqual(len((ROOT / "llms.txt").read_bytes()), 600)
        self.assertLessEqual(len((ROOT / "AGENTS.md").read_bytes()), 6000)

    def test_module_headers(self):
        _, modules = anc.scan()
        for f, m in modules.items():
            self.assertIsNotNone(m, "missing @module header: %s" % f)
            for k in ("id", "layer", "owner", "deps", "book"):
                self.assertIn(k, m, f)

    def test_anchors_unique(self):
        a, _ = anc.scan()
        ids = [x["id"] for x in a]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreater(len(ids), 20)

    def test_no_image_files(self):
        bad = [p for p in ROOT.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".npy")
               and ".git" not in p.parts]
        self.assertEqual(bad, [])


class TestHyperbooks(unittest.TestCase):
    def setUp(self):
        self.fmt = json.loads((HB / "COMPANION_FORMAT.json").read_text())
        self.anchor_ids = {x["id"] for x in anc.scan()[0]}

    def test_books_admitted(self):
        for b in self.fmt["expected_books"]:
            for f in self.fmt["required_per_book"]:
                self.assertTrue((HB / b / f).is_file(), "%s/%s" % (b, f))
            text = (HB / b / "book.md").read_text()
            secs = [json.loads(s) for s in SECTION_RE.findall(text)]
            self.assertTrue(secs, b)
            for s in secs:
                for a in s["code_anchors"]:
                    self.assertIn(a, self.anchor_ids, "%s: unresolved anchor %s" % (b, a))
            for line in (HB / b / "mathlinks.jsonl").read_text().splitlines():
                row = json.loads(line)
                self.assertIn(row["status"], self.fmt["mathlink_status"])
                if row["status"] == "CITED":
                    self.assertEqual(hashlib.sha256(row["quote"].encode()).hexdigest(), row["quote_sha256"])
                    if os.path.exists(row["target_path"]):
                        n = int(row["claimed_location"].split()[-1])
                        got = Path(row["target_path"]).read_text(encoding="utf-8").splitlines()[n - 1]
                        self.assertEqual(got, row["quote"])

    def test_manifests_fresh(self):
        for b in self.fmt["expected_books"]:
            want = reseal.dump(reseal.manifest_for(HB / b))
            self.assertEqual((HB / b / "manifest.json").read_text(), want, "stale manifest: run tools/reseal.py")


@unittest.skipUnless(shutil.which("node"), "node not on PATH: JS in-page tests skipped")
class TestJs(unittest.TestCase):
    def test_node_tests_pass(self):
        p = subprocess.run(["node", "--test"] + sorted(str(x) for x in (ROOT / "js" / "tests").glob("*.test.js")),
                           capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
