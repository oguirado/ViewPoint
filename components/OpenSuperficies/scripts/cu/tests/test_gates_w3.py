"""W3 INT tests: mutant coverage of the E/P/W/C lanes, gates bundle, KL/ER status vs code, prereg v2 sha in docs.
Run: python3 -B -m unittest discover -s scripts/cu/tests (from the repo root)."""
import copy
import json
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CU = os.path.join(REPO, "scripts", "cu")
PREREG_V2_SHA = "b2f585b89aec55ffded950614a180c66e42b6ab0b2a2725a4c9f1f6333cc2a12"
PREREG_V1_SHA = "505aac99904e99e15dbba13242dd8c2731bf803259bb39d80deb684bd934cd58"


def read(*p):
    with open(os.path.join(REPO, *p), encoding="utf-8") as fh:
        return fh.read()


def rows(name):
    return {l.split("|")[1].strip(): l for l in read("docs", name).splitlines() if l.startswith("| KL-")}


class GatesW3(unittest.TestCase):
    def test_mutants_cover_w3_lanes(self):
        m = json.loads(read("scripts", "cu", "mutants.json"))["mutants"]
        ids = [x["id"] for x in m]
        self.assertGreaterEqual(len(ids), 175)
        self.assertEqual(len(ids), len(set(ids)))
        for want in ("os.e.release.E1", "os.e.carrier.E2", "os.e.fail.E3", "os.p.span.P1", "os.p.gappy.P2", "os.p.mode.P3",
                     "os.p.umask.P4", "os.p.bounds.P5", "os.w.order.W1", "os.w.strip.W2", "os.w.period.W3", "os.w.v1sha.W4",
                     "os.c.cdp.C1", "os.c.url.C2", "os.c.umask.C3"):
            self.assertIn(want, ids)
        by = {x["id"]: x for x in m}
        for x in m:
            self.assertTrue(x["precheck"] and x["oracle"] and x["find"] != x["replace"], x["id"])
        # the re-anchored W2 mutants point at the current W3 code, not at the deleted chmod/Keep text
        self.assertIn("0o770", by["os.live.chunkperm.D8"]["find"])
        self.assertNotIn("Chmod", by["os.live.chunkperm.D8"]["find"])
        self.assertIn("want", by["os.live.prereg.D11"]["find"])

    def test_gates_runs_w3_suites(self):
        txt = read("scripts", "cu", "gates.sh")
        for want in ("vision.tests.test_chunk_analyze", "docker/cu/tests/test_launch_chromium_cu.sh", "test_chromium_profile.py",
                     "docker/cu/tests/test_entrypoint_umask.sh", "bench/wl/labels_v2.py validate bench/wl/prereg_wl_v2.json",
                     "./src/cubook/capture/...", "bench/wl/tests"):
            self.assertIn(want, txt)
        plan = read("scripts", "cu", "run_suite.sh")
        self.assertIn("docker/cu/tests", plan)

    def test_kl20_status_matches_code(self):
        kl = rows("CU_KNOWN_LIMITATIONS.md")
        self.assertIn("CLOSED", kl["KL-20"])
        # what the row claims is in the code: the Analyzer fills AnalyzeUsPerF and the driver wires --analyze
        self.assertIn("row.Grid, row.ClassCounts, row.AnalyzeUsPerF", read("src", "cubook", "capture", "analyzer.go"))
        self.assertIn("--analyze", read("cmd", "srf-cu-wlcap", "main.go"))
        for k in ("KL-21", "KL-22", "KL-23", "KL-24"):
            self.assertIn(k, kl)
        self.assertIn("MIT-SHM", kl["KL-22"])
        self.assertIn("sandbox", kl["KL-21"])

    def test_er_status(self):
        txt = read("docs", "ENGINE_REQUESTS.md")
        self.assertRegex(txt, r"## ER-012 [^\n]*\nstatus: OPEN")
        self.assertRegex(txt, r"## ER-014 [^\n]*\nstatus: ACCEPTED[^\n]*vision-v2 track B")
        self.assertIn("CYCLE", txt)

    def test_prereg_v2_sha_in_docs(self):
        doc = read("docs", "CU_W3.md")
        self.assertIn(PREREG_V2_SHA, doc)
        self.assertIn(PREREG_V1_SHA, doc)
        self.assertIn("S-LIVE v3", doc)
        sys.path.insert(0, os.path.join(REPO, "bench", "wl"))
        import labels_v2 as L
        self.assertEqual(L.prereg_sha256(L.load_manifest(os.path.join(REPO, "bench", "wl", "prereg_wl_v2.json"))), PREREG_V2_SHA)

    def test_parent_prereg_enforced(self):
        """os.w.v1sha.W4 oracle: a v2 manifest whose parent_prereg is not the frozen v1 sha is refused."""
        sys.path.insert(0, os.path.join(REPO, "bench", "wl"))
        import labels_v2 as L
        base = os.path.join(REPO, "bench", "wl")
        m = L.load_manifest(os.path.join(base, "prereg_wl_v2.json"))
        self.assertEqual(m["parent_prereg"], PREREG_V1_SHA)  # positive control
        bad = copy.deepcopy(m)
        bad["parent_prereg"] = "0" * 64
        with self.assertRaises(L.WLError):
            L.check_manifest(bad, base)

    def test_chunk_modes_in_docs_match_code(self):
        src = read("src", "cubook", "capture", "livedriver.go")
        self.assertIn("os.Mkdir(d, 0o770)", src)
        self.assertIsNone(re.search(r"0o(777|755|666|644)", src))


if __name__ == "__main__":
    unittest.main()
