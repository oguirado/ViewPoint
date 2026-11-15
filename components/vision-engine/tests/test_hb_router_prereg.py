"""Lane E guards for eval/hb_router: sealed query set, row contract, blind text, resolvable gold, receipt/RESULT agreement."""
import ast, glob, hashlib, json, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HR = os.path.join(ROOT, "eval", "hb_router")
QUERIES = os.path.join(HR, "queries_s1.jsonl")
KEYS = {"qid", "split", "class", "text", "intent", "gold", "gold_tests", "receipt", "verdict", "source_event", "notes"}


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def rows():
    with open(QUERIES, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def desc_symbols():
    tree = ast.parse(open(os.path.join(ROOT, "visioncore", "desc.py"), encoding="utf-8").read())
    return {"visioncore/desc.py::" + n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def built_symbols():
    out = set()
    for p in glob.glob(os.path.join(ROOT, "build", "room_E", "**", "symbols.jsonl"), recursive=True):
        for l in open(p, encoding="utf-8"):
            if l.strip():
                out.add(json.loads(l)["id"])
    return out


class QuerySet(unittest.TestCase):
    def test_a_seal_and_receipts(self):
        want = open(os.path.join(HR, "queries_s1.sha256")).read().split()[0]
        self.assertEqual(sha(QUERIES), want)
        found = 0
        for name in ("run_before.json", "run_after.json"):
            p = os.path.join(HR, name)
            if os.path.isfile(p):
                found += 1
                self.assertEqual(json.load(open(p))["queries_sha256"], want, name)
        self.assertTrue(os.path.isfile(os.path.join(HR, "run_before.json")), "run_before.json missing (fail-closed)")

    def test_b_counts_and_keys(self):
        rs = rows()
        self.assertEqual(len({r["qid"] for r in rs}), len(rs))
        for r in rs:
            self.assertEqual(set(r), KEYS, r["qid"])
            self.assertEqual(r["split"], "DEV")
            self.assertIn(r["class"], ("FIND", "ABSENT", "CLAIM"))
        c = lambda k: sum(1 for r in rs if r["class"] == k)
        self.assertGreaterEqual(c("FIND"), 20)
        self.assertGreaterEqual(c("ABSENT"), 20)
        self.assertGreaterEqual(c("CLAIM"), 3)
        for r in rs:
            if r["class"] == "FIND":
                self.assertEqual((r["intent"], len(r["gold"]) >= 1), ("find_code", True), r["qid"])
            elif r["class"] == "ABSENT":
                self.assertEqual((r["intent"], r["gold"]), ("find_code", []), r["qid"])
            else:
                self.assertEqual(r["intent"], "check_claim")
                self.assertRegex(r["receipt"], r"^[^:]+\.json:[\w.]+$")
                self.assertIn(r["verdict"], ("supported", "contradicted"))

    def test_c_no_symbol_names_in_text(self):
        names = {s.split("::")[-1] for s in desc_symbols() | built_symbols()}
        names |= {n.lstrip("_") for n in names}
        names = {n.lower() for n in names if len(n) >= 4}
        for r in rows():
            t = r["text"].lower()
            for n in names:
                self.assertNotIn(n, t, "%s leaks symbol name %s" % (r["qid"], n))

    def test_d_gold_resolves(self):
        syms = desc_symbols()
        built = built_symbols()
        for r in rows():
            for g in r["gold"]:
                self.assertIn(g, syms, r["qid"])
                if built:
                    self.assertIn(g, built, r["qid"])

    def test_e_result_matches_run_after(self):
        p = os.path.join(HR, "run_after.json")
        if not os.path.isfile(p):
            self.skipTest("E-post pending (wave 3): run_after.json not yet produced")
        doc = json.load(open(p))
        text = open(os.path.join(HR, "RESULT.md"), encoding="utf-8").read()
        for system, m in doc["metrics"].items():
            if m.get("FIND"):
                for k, v in m["FIND"]["raw"].items():
                    self.assertIn(v, text, "%s %s" % (system, k))
            if m.get("ABSENT"):
                for k in ("precision", "recall", "f1"):
                    if m["ABSENT"][k] is not None:  # B1 never abstains: precision is null
                        self.assertIn(m["ABSENT"][k], text, "%s %s" % (system, k))


if __name__ == "__main__":
    unittest.main()
