"""Lane E0 guards for eval/hb_router/heldout: sealed query set, receipt stamps, runbook reproduction (byte-identical)."""
import hashlib, json, os, re, shutil, subprocess, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HO = os.path.join(ROOT, "eval", "hb_router", "heldout")
QUERIES = os.path.join(HO, "queries_all_test.jsonl")
SEAL = "cd9bc2dd880c38ddcba1a9d92012fe411fa408e6228fb35b5eee939b8950d80d"
RUNBOOK = os.path.join(HO, "RUNBOOK.md")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def receipt(name):
    with open(os.path.join(HO, name), encoding="utf-8") as f:
        return json.load(f)


def script_text():
    with open(RUNBOOK, encoding="utf-8") as f:
        m = re.search(r"<!-- SCRIPT -->\n```bash\n(.*?)```", f.read(), re.S)
    return m.group(1) if m else None


class Seal(unittest.TestCase):
    def test_query_sha_matches_sealed_file(self):
        with open(os.path.join(HO, "queries_all_test.sha256")) as f:
            want = f.read().split()[0]
        self.assertEqual(want, SEAL)
        self.assertEqual(sha(QUERIES), want)

    def test_counts(self):
        with open(QUERIES, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertEqual((len(rows), sum(r["class"] == "FIND" for r in rows), sum(r["class"] == "ABSENT" for r in rows)), (120, 70, 50))
        self.assertEqual({r["split"] for r in rows}, {"TEST"})


class Receipt(unittest.TestCase):
    def test_before_carries_both_shas(self):
        d = receipt("run_before.json")
        self.assertEqual(d["queries_sha256"], SEAL)
        self.assertEqual(d["n_rows"], 120)
        self.assertEqual(d["systems"], ["v2", "B1"])
        self.assertFalse(d["thin_dev"])
        self.assertRegex(d["book_md_sha256"], r"^[0-9a-f]{64}$")
        books = d["corpus_books_sha256"]
        self.assertEqual(len(books), 10)
        self.assertEqual(sorted(books), sorted(set(books)))
        self.assertIn("hyperbooks/HB-VC-s1-desc", books)
        self.assertIn("hyperbooks/HB-VN-s0s1", books)
        self.assertEqual(d["corpus_rev"], "7199c81")

    def test_before_book_shas_match_git_rev(self):
        d = receipt("run_before.json")
        for book, want in d["corpus_books_sha256"].items():
            r = subprocess.run(["git", "show", "%s:%s/book.md" % (d["corpus_rev"], book)], cwd=ROOT, capture_output=True)
            self.assertEqual(r.returncode, 0, book)
            self.assertEqual(hashlib.sha256(r.stdout).hexdigest(), want, book)

    def test_after_receipt_if_present_uses_same_seal(self):
        if os.path.isfile(os.path.join(HO, "run_after.json")):
            self.assertEqual(receipt("run_after.json")["queries_sha256"], SEAL)

    def test_after_receipt_stamps_and_current_book_shas(self):
        d = receipt("run_after.json")
        before = receipt("run_before.json")
        self.assertEqual(d["queries_sha256"], SEAL)
        self.assertEqual((d["n_rows"], d["systems"], d["thin_dev"]), (120, ["v2", "B1"], False))
        self.assertEqual(d["corpus_rev"], "WORKTREE")
        self.assertEqual(d["src_dirs"], before["src_dirs"])
        self.assertRegex(d["book_md_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(d["book_md_sha256"], before["book_md_sha256"])
        self.assertEqual(sorted(d["corpus_books_sha256"]), sorted(before["corpus_books_sha256"]))
        # The receipt pins the corpus as measured: compare against the commit that recorded run_after.json,
        # so later factual book fixes (e.g. HB-VN-s0s1 Cargo.lock, eac6d05) do not rewrite measurement history.
        rec = subprocess.run(["git", "log", "-1", "--format=%H", "--", "eval/hb_router/heldout/run_after.json"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        self.assertRegex(rec, r"^[0-9a-f]{40}$")
        for book, want in d["corpus_books_sha256"].items():
            r = subprocess.run(["git", "show", "%s:%s/book.md" % (rec, book)], cwd=ROOT, capture_output=True)
            self.assertEqual(r.returncode, 0, book)
            self.assertEqual(hashlib.sha256(r.stdout).hexdigest(), want, book)

    def test_after_receipt_worktree_bytes_equal_recording_commit(self):
        """The receipt read above must be the committed one: an uncommitted edit would be checked against older books."""
        rec = subprocess.run(["git", "log", "-1", "--format=%H", "--", "eval/hb_router/heldout/run_after.json"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        self.assertRegex(rec, r"^[0-9a-f]{40}$")
        r = subprocess.run(["git", "show", "%s:eval/hb_router/heldout/run_after.json" % rec], cwd=ROOT, capture_output=True)
        self.assertEqual(r.returncode, 0)
        with open(os.path.join(HO, "run_after.json"), "rb") as f:
            self.assertEqual(f.read(), r.stdout)

    def test_after_migrated_books_differ_from_before(self):
        b, a = receipt("run_before.json")["corpus_books_sha256"], receipt("run_after.json")["corpus_books_sha256"]
        same = sorted(k for k in a if a[k] == b[k])
        self.assertEqual(same, ["hyperbooks/HB-VC-s1-desc", "hyperbooks/HB-VN-s0s1"])

    def test_result_md_claims_match_receipts(self):
        with open(os.path.join(HO, "RESULT.md"), encoding="utf-8") as f:
            r = f.read()
        d = receipt("run_after.json")
        self.assertIn(SEAL, r)
        self.assertIn(d["book_md_sha256"], r)
        self.assertIn(receipt("run_before.json")["book_md_sha256"], r)
        self.assertIn("held-out TEST, blind author", r)
        self.assertIn(d["metrics"]["v2"]["FIND"]["raw"]["recall_at_5"], r)
        self.assertIn("No improvement is claimed", r)  # p = 0.161 >= 0.05


class Runbook(unittest.TestCase):
    def test_script_is_embedded_and_fails_closed(self):
        s = script_text()
        self.assertIsNotNone(s)
        self.assertIn("E_SEAL", s)
        self.assertIn("taskset -c 5-9", s)
        self.assertNotIn("HB-VC-s1-desc", s)

    def test_runbook_reproduces_run_before_byte_identically(self):
        s = script_text()
        self.assertIsNotNone(s)
        scr = os.path.join(ROOT, "build", "test_heldout_%d" % os.getpid())
        os.makedirs(scr)
        self.addCleanup(shutil.rmtree, scr, True)
        sh = os.path.join(scr, "runbook.sh")
        with open(sh, "w", encoding="utf-8") as f:
            f.write(s)
        out = os.path.join(scr, "out.json")
        r = subprocess.run(["bash", sh, receipt("run_before.json")["corpus_rev"], out, os.path.join(scr, "w")], cwd=ROOT, capture_output=True, text=True,
                           timeout=900)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        with open(out, "rb") as a, open(os.path.join(HO, "run_before.json"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_seal_mismatch_exits_3(self):
        s = script_text()
        scr = os.path.join(ROOT, "build", "test_heldout_neg_%d" % os.getpid())
        os.makedirs(scr)
        self.addCleanup(shutil.rmtree, scr, True)
        # mutant: the snapshot's .sha256 is replaced right after extraction, so the guard must trip before any work
        mutant = s.replace('cd "$SCR/snap"\n', 'cd "$SCR/snap"\necho 0 > eval/hb_router/heldout/queries_all_test.sha256\n', 1)
        self.assertNotEqual(mutant, s)
        sh = os.path.join(scr, "m.sh")
        with open(sh, "w", encoding="utf-8") as f:
            f.write(mutant)
        r = subprocess.run(["bash", sh, "7199c81", os.path.join(scr, "o.json"), os.path.join(scr, "w")], cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 3)
        self.assertIn("E_SEAL", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(scr, "o.json")))


if __name__ == "__main__":
    unittest.main()
