"""Wiring of the hbtools_v2 copy (hbtools_v2/, standards/schemas/, tests/fixtures/hbv2/) into the engine repo.

(a) schemas, (b) provenance/patch ledger, (c)/(d) independent-implementation check, (e) privacy rules 1,3,4,5,
(f) engine gates stay green (J-1 regression guard: the copy lives at <repo>/hbtools_v2, never under tools/).
Stdlib only.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "hbtools_v2")
SCHEMAS = os.path.join(ROOT, "standards", "schemas")
FIX = os.path.join(ROOT, "tests", "fixtures", "hbv2")
QUERIES = ["double a number", "pipeline entry point", "base class helper", "count down recursion",
           "call the second layer star import"]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def prov():
    with open(os.path.join(TOOLS, "PROVENANCE.json"), encoding="utf-8") as f:
        return json.load(f)


class Schemas(unittest.TestCase):
    def assert_schema_copy(self, row, path, patches):
        self.assertEqual(sha(path), row["copied_sha256"], row["path"])
        if row["copied_sha256"] != row["source_sha256"]:
            # A standards exception binds this exact source and copied version.
            # Merely mentioning a schema or listing a tool patch is insufficient.
            expected = [row["path"], "standards patch", row["source_sha256"], row["copied_sha256"]]
            entries = [[cell.strip() for cell in line.strip().split("|")[1:-1]]
                       for line in patches.splitlines() if line.strip().startswith("|")]
            self.assertIn(expected, entries, row["path"] + " needs an exact standards patch with old/new sha")

    def test_a_thirteen_schemas_parse_and_match_provenance(self):
        names = sorted(n for n in os.listdir(SCHEMAS) if n.endswith(".schema.json"))
        self.assertEqual(len(names), 13)
        rows = {r["path"]: r for r in prov()["files"]}
        with open(os.path.join(TOOLS, "PATCHES.md"), encoding="utf-8") as f:
            patches = f.read()
        for n in names:
            with open(os.path.join(SCHEMAS, n), encoding="utf-8") as f:
                doc = json.load(f)
            self.assertIsInstance(doc, dict, n)
            r = rows.get("standards/schemas/" + n)
            self.assertIsNotNone(r, n)
            self.assert_schema_copy(r, os.path.join(SCHEMAS, n), patches)

    def test_a_unlisted_schema_change_fails_even_with_refreshed_provenance(self):
        # Exercise test_a itself with a valid but unlisted schema modification.
        scratch = os.path.join(ROOT, "build", "room_P")
        os.makedirs(scratch, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            schema_dir = os.path.join(tmp, "schemas")
            shutil.copytree(SCHEMAS, schema_dir)
            name = "hb.answer.v2.schema.json"
            path = os.path.join(schema_dir, name)
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            doc["description"] = "Unlisted standards change"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            provenance = prov()
            for row in provenance["files"]:
                if row["path"] == "standards/schemas/" + name:
                    row["copied_sha256"] = sha(path)
            with mock.patch(__name__ + ".SCHEMAS", schema_dir), mock.patch(__name__ + ".prov", return_value=provenance):
                with self.assertRaisesRegex(AssertionError, "needs an exact standards patch"):
                    self.test_a_thirteen_schemas_parse_and_match_provenance()

    def test_a_patch_must_bind_kind_and_both_hashes(self):
        path = os.path.join(SCHEMAS, "hb.anchor.v2.schema.json")
        row = next(r for r in prov()["files"] if r["path"] == "standards/schemas/hb.anchor.v2.schema.json")
        self.assertNotEqual(row["source_sha256"], row["copied_sha256"])
        cells = [row["path"], "standards patch", row["source_sha256"], row["copied_sha256"]]
        self.assert_schema_copy(row, path, "| " + " | ".join(cells) + " |")
        for index, replacement in ((0, "standards/schemas/other.schema.json"), (1, "tool patch"),
                                   (2, "0" * 64), (3, "0" * 64)):
            bad = list(cells)
            bad[index] = replacement
            with self.subTest(column=index), self.assertRaises(AssertionError):
                self.assert_schema_copy(row, path, "| " + " | ".join(bad) + " |")
        with self.assertRaises(AssertionError):
            self.assert_schema_copy(row, path, "")


class Ledger(unittest.TestCase):
    def test_b_copied_sha_equals_provenance_or_is_patched(self):
        with open(os.path.join(TOOLS, "PATCHES.md"), encoding="utf-8") as f:
            patches = f.read()
        files = prov()["files"]
        self.assertGreater(len(files), 40)
        for r in files:
            p = os.path.join(ROOT, r["path"])
            self.assertTrue(os.path.isfile(p), r["path"])
            self.assertEqual(sha(p), r["copied_sha256"], r["path"])
            if r["copied_sha256"] != r["source_sha256"]:
                self.assertIn("| %s |" % r["path"], patches, r["path"] + " differs from source but is not in PATCHES.md")
        kinds = re.findall(r"^[1-9]\. ", patches, re.M)
        self.assertLessEqual(len(kinds), 4)

    def test_b_excluded_files_are_absent(self):
        ex = prov()["excluded"]
        self.assertTrue(ex)
        for e in ex:
            self.assertFalse(os.path.exists(os.path.join(TOOLS, os.path.basename(e["source"]))), e["source"])
            self.assertTrue(e["reason"])


def _score_ref_run(extra_env):
    code = (
        "import json,os,sys,tempfile\n"
        "sys.dont_write_bytecode=True\n"
        "sys.path.insert(0,%r)\n"
        "import mini_hb, hb_query as Q, hblib2 as L\n"
        "d=tempfile.mkdtemp()\n"
        "mini_hb.make_mini(%r, os.path.join(d,'m'))\n"
        "h=Q.Hyper(os.path.join(d,'m'))\n"
        "out=[]\n"
        "for q in %r:\n"
        "    a=Q.answer(h,q,'FIND'); S=h.ix.scores(L.toks(q),h.find); o=h.ix.order(S)\n"
        "    out.append({'q':q,'order':[h.ix.ids[x] for x in o],'scores':[S[x] for x in o],'answer_bytes':len(L.canon(a)),"
        "'answer_sha256':L.sha256b(L.canon(a)),'rows':[[r[0],r[1],r[2]] for r in a['r']]})\n"
        "print(json.dumps(out))\n"
    ) % (TOOLS, os.path.join(FIX, "graph"), QUERIES)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HBV2_")}
    env.update(extra_env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=300, cwd=ROOT)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-800:])
    return json.loads(r.stdout)


class IndependentImplementation(unittest.TestCase):
    def _ref(self):
        with open(os.path.join(FIX, "score_ref.json"), encoding="utf-8") as f:
            ref = json.load(f)["queries"]
        self.assertEqual(len(ref), 5)
        return ref

    def test_c_hb_query_reproduces_score_ref_env_unset(self):
        self.assertEqual(_score_ref_run({}), self._ref())

    def test_d_same_with_book_sub_explicit(self):
        self.assertEqual(_score_ref_run({"HBV2_BOOK_SUB": "book"}), self._ref())


PUB_ABS = re.compile(r"(?<![\w.:/-])/(?:home|Users|root|tmp|var|mnt|opt|usr|etc|media|srv)(?:/[\w.-]+)+|\b[A-Za-z]:\\\\?[\w.-]+")
PUB_ABS_OK = ("/usr/bin/env",)
PUB_PREFIX = re.compile("scr" + "-[a-z]")
PUB_URL = re.compile(r"https?://[^\s\"'`)>\]]+")
PUB_URL_OK = {"https://json-schema.org/draft/2020-12/schema"}
PUB_CRED = re.compile(r"sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[:=]|password\s*[:=]|"
                      r"secret\s*[:=]|token\s*[:=]\s*\S{12,}", re.I)


def privacy_hits(text, name):
    hits = []
    for m in PUB_ABS.finditer(text):
        if not m.group(0).startswith(PUB_ABS_OK):
            hits.append("abs_path %s:%d" % (name, text[:m.start()].count("\n") + 1))
    for m in PUB_CRED.finditer(text):
        hits.append("cred %s:%d" % (name, text[:m.start()].count("\n") + 1))
    if PUB_PREFIX.search(text):
        hits.append("private_tool_prefix %s" % name)
    for m in PUB_URL.finditer(text):
        if m.group(0) not in PUB_URL_OK:
            hits.append("url %s:%d" % (name, text[:m.start()].count("\n") + 1))
    return hits


class Privacy(unittest.TestCase):
    def test_e_scan_detects_planted_hits(self):
        planted = "x = '/home/someone/f'\n" + "scr" + "-tool\nhttp://example.org/x\npassword: x\n"
        self.assertEqual(len(privacy_hits(planted, "p")), 4)

    def test_e_no_hits_in_hbtools_v2_and_standards(self):
        hits, n = [], 0
        for base in (TOOLS, os.path.join(ROOT, "standards")):
            for dp, dn, fn in os.walk(base):
                dn[:] = sorted(d for d in dn if d != "__pycache__")
                for f in sorted(fn):
                    p = os.path.join(dp, f)
                    try:
                        with open(p, encoding="utf-8") as fh:
                            t = fh.read()
                    except (UnicodeDecodeError, OSError):
                        continue
                    n += 1
                    hits += privacy_hits(t, os.path.relpath(p, ROOT))
        self.assertGreater(n, 50)
        self.assertEqual(hits, [])


class EngineGates(unittest.TestCase):
    def test_f_gates_all_rc0_with_copy_in_place(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "tools", "hbtools_v2")), "J-1: the copy must not live under tools/")
        r = subprocess.run([sys.executable, "tools/gates.py", "--all"], cwd=ROOT, capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
