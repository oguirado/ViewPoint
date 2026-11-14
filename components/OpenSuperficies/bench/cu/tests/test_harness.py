import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("cu_harness", os.path.join(BENCH, "harness.py"))
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)
PREREG = os.path.join(BENCH, "prereg_w1.json")
FIX = os.path.join(BENCH, "fixtures", "act_steps.jsonl")


def load():
    with open(PREREG) as f:
        return json.load(f)


def copy_bench(tmp):
    dst = os.path.join(tmp, "cu")
    shutil.copytree(BENCH, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    return dst


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class Prereg(unittest.TestCase):
    def test_prereg_schema_closed(self):
        m = load()
        self.assertEqual(H.validate(m, BENCH), [])
        bad = dict(m, extra_key=1)
        self.assertNotEqual(H.validate(bad, BENCH), [])
        fl = json.loads(json.dumps(m)); fl["wilson_z_milli"] = 1.96
        self.assertNotEqual(H.validate(fl, BENCH), [])
        nt = json.loads(json.dumps(m)); nt["tasks"][0]["oracle_x"] = 1
        self.assertNotEqual(H.validate(nt, BENCH), [])
        self.assertEqual([t["id"] for t in m["tasks"]],
                         ["WF1", "WF2", "DA1", "DA2", "BU1", "BU2", "GU1", "GU2"])

    def test_seeds_cal_test_disjoint(self):
        m = load()
        self.assertEqual(m["seeds"]["cal"], [101, 102, 103])
        self.assertEqual(m["seeds"]["test"], [201, 202, 203, 204, 205])
        self.assertFalse(set(m["seeds"]["cal"]) & set(m["seeds"]["test"]))
        ov = json.loads(json.dumps(m)); ov["seeds"]["test"][0] = 101
        errs = H.validate(ov, BENCH)
        self.assertTrue(any("overlap" in e for e in errs), errs)

    def test_prereg_sha_stable_canonical(self):
        m = load()
        a = H.canonical_sha256(m)
        self.assertEqual(len(a), 64)
        rev = dict(reversed(list(m.items())))
        self.assertEqual(a, H.canonical_sha256(rev))
        m2 = json.loads(json.dumps(m)); m2["seeds"]["test"].append(206)
        self.assertNotEqual(a, H.canonical_sha256(m2))

    def test_task_files_pinned_and_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = copy_bench(tmp)
            p = os.path.join(d, "prereg_w1.json")
            self.assertEqual(H.validate(json.load(open(p)), d), [])
            with open(os.path.join(d, "tasks", "WF1.json"), "a") as f:
                f.write(" ")
            errs = H.validate(json.load(open(p)), d)
            self.assertTrue(any("task_sha256" in e for e in errs), errs)
            self.assertEqual(H.main(["--dry-run", p]), 5)

    def test_dry_run_prints_sha(self):
        r = subprocess.run([sys.executable, "-B", os.path.join(BENCH, "harness.py"), "--dry-run", PREREG],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(H.canonical_sha256(load()), r.stdout)


class RunGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sha = H.canonical_sha256(load())
        self.ok = os.path.join(self.tmp, "resource_ok")
        with open(self.ok, "w") as f:
            f.write("OK\n")

    def test_run_refused_without_resource_ok(self):
        rc = H.main(["--run", PREREG, "--split", "cal", "--expect-sha", self.sha])
        self.assertEqual(rc, 5)
        rc = H.main(["--run", PREREG, "--split", "cal", "--expect-sha", self.sha,
                     "--resource-ok", os.path.join(self.tmp, "missing")])
        self.assertEqual(rc, 5)
        rc = H.main(["--run", PREREG, "--split", "cal", "--expect-sha", self.sha,
                     "--resource-ok", self.ok])
        self.assertEqual(rc, 0)

    def test_run_refused_on_sha_drift(self):
        rc = H.main(["--run", PREREG, "--split", "cal", "--expect-sha", "0" * 64,
                     "--resource-ok", self.ok])
        self.assertEqual(rc, 5)
        rc = H.main(["--run", PREREG, "--split", "cal", "--resource-ok", self.ok])
        self.assertEqual(rc, 5)

    def test_run_requires_split(self):
        rc = H.main(["--run", PREREG, "--expect-sha", self.sha, "--resource-ok", self.ok])
        self.assertEqual(rc, 2)

    def test_split_test_refused_on_dirty_tree(self):
        orig = getattr(H, "tree_dirty", None)
        self.addCleanup(setattr, H, "tree_dirty", orig)
        self.addCleanup(setattr, H, "untracked", H.untracked)
        H.untracked = lambda paths: []  # git-free: tracked inputs are TrackedInputs' job
        args = ["--run", PREREG, "--split", "test", "--expect-sha", self.sha, "--resource-ok", self.ok]
        H.tree_dirty = lambda: True
        self.assertEqual(H.main(args), 5)
        H.tree_dirty = lambda: False
        self.assertEqual(H.main(args), 0)


class Hardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_validate_refuses_duplicate_task_ids(self):
        m = load()
        m["tasks"].append(dict(m["tasks"][0]))
        errs = H.validate(m, BENCH)
        self.assertTrue(any("duplicate id" in e for e in errs), errs)

    def test_validate_refuses_task_id_mismatch(self):
        m = load()
        m["tasks"][0]["id"] = "ZZ9"
        errs = H.validate(m, BENCH)
        self.assertTrue(any("task id mismatch" in e for e in errs), errs)

    def test_task_file_symlink_escape_refused(self):
        d = copy_bench(self.tmp)
        out = os.path.join(self.tmp, "outside.json")
        shutil.copy(os.path.join(d, "tasks", "WF1.json"), out)
        os.remove(os.path.join(d, "tasks", "WF1.json"))
        os.symlink(out, os.path.join(d, "tasks", "WF1.json"))
        errs = H.validate(json.load(open(os.path.join(d, "prereg_w1.json"))), d)
        self.assertTrue(any("escapes tasks dir" in e for e in errs), errs)
        self.assertEqual(H.main(["--dry-run", os.path.join(d, "prereg_w1.json")]), 5)

    def test_task_file_outside_tasks_dir_refused(self):
        d = copy_bench(self.tmp)
        shutil.copy(os.path.join(d, "tasks", "WF1.json"), os.path.join(d, "WF1.json"))
        m = json.load(open(os.path.join(d, "prereg_w1.json")))
        m["tasks"][0]["task_file"] = "WF1.json"
        errs = H.validate(m, d)
        self.assertTrue(any("escapes tasks dir" in e for e in errs), errs)

    def test_schema_error_with_tamper_is_rc2(self):
        d = copy_bench(self.tmp)
        p = os.path.join(d, "prereg_w1.json")
        m = json.load(open(p))
        m["extra_key"] = 1
        write(p, m)
        self.assertEqual(H.main(["--dry-run", p]), 2)
        with open(os.path.join(d, "tasks", "WF1.json"), "a") as f:
            f.write(" ")
        self.assertEqual(H.main(["--dry-run", p]), 2)  # schema + tamper: still schema
        m.pop("extra_key")
        write(p, m)
        self.assertEqual(H.main(["--dry-run", p]), 5)  # pure tamper

    def test_tree_dirty_bound_to_harness_repo(self):
        # harness copied into a git repo whose root is not the harness' own repo root: unverifiable -> dirty
        repo = os.path.join(self.tmp, "r")
        sub = os.path.join(repo, "x", "y", "cu")
        os.makedirs(sub)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", repo], check=True, env=env, timeout=60)
        shutil.copy(os.path.join(BENCH, "harness.py"), sub)
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, env=env, timeout=60)
        subprocess.run(["git", "-C", repo, "commit", "-qm", "c"], check=True, env=env, timeout=60)
        code = ("import importlib.util as u;s=u.spec_from_file_location('h',%r);m=u.module_from_spec(s);"
                "s.loader.exec_module(m);print(m.tree_dirty())" % os.path.join(sub, "harness.py"))
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tmp, capture_output=True, text=True,
                           timeout=60, env=env)
        self.assertEqual(r.stdout.strip(), "True", r.stderr)

    def test_tree_dirty_independent_of_cwd(self):
        seen = []
        real = H.subprocess.run

        def spy(cmd, **kw):
            seen.append((cmd, kw.get("cwd")))
            return real(cmd, **kw)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        H.subprocess.run = spy
        self.addCleanup(setattr, H.subprocess, "run", real)
        H.tree_dirty()
        self.assertTrue(seen)
        for cmd, cwd in seen:
            self.assertIn(H.REPO, cmd)

    def test_split_test_refused_for_prereg_outside_repo(self):
        d = copy_bench(self.tmp)
        p = os.path.join(d, "prereg_w1.json")
        ok = os.path.join(self.tmp, "ok")
        open(ok, "w").write("OK\n")
        H.tree_dirty, orig = (lambda: False), H.tree_dirty
        self.addCleanup(setattr, H, "tree_dirty", orig)
        self.addCleanup(setattr, H, "untracked", H.untracked)
        H.untracked = lambda paths: []  # the repo-containment check alone must refuse
        sha = H.canonical_sha256(json.load(open(p)))
        self.assertEqual(H.main(["--run", p, "--split", "test", "--expect-sha", sha, "--resource-ok", ok]), 5)


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo] + list(args),
                          check=True, capture_output=True, timeout=60)


class TrackedInputs(unittest.TestCase):
    """ADW2-05: --split test needs a tracked, unmodified prereg and task files (hermetic repo, H.REPO patched)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.cu = os.path.join(self.repo, "bench", "cu")
        shutil.copytree(BENCH, self.cu, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("build/\n*.tmp\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True, timeout=60)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c")
        self.addCleanup(setattr, H, "REPO", H.REPO)
        H.REPO = self.repo
        self.ok = os.path.join(self.tmp, "ok")
        with open(self.ok, "w") as f:
            f.write("OK\n")

    def run_test_split(self, p):
        sha = H.canonical_sha256(json.load(open(p)))
        return H.main(["--run", p, "--split", "test", "--expect-sha", sha, "--resource-ok", self.ok])

    def test_split_test_authorizes_tracked_prereg(self):
        self.assertEqual(self.run_test_split(os.path.join(self.cu, "prereg_w1.json")), 0)

    def test_split_test_refuses_ignored_prereg_copy(self):
        # advocate input: bench/cu/{prereg_w1.json,tasks} copied into git-ignored build/e/
        e = os.path.join(self.repo, "build", "e")
        os.makedirs(e)
        shutil.copy(os.path.join(self.cu, "prereg_w1.json"), e)
        shutil.copytree(os.path.join(self.cu, "tasks"), os.path.join(e, "tasks"))
        self.assertFalse(H.tree_dirty())
        self.assertEqual(self.run_test_split(os.path.join(e, "prereg_w1.json")), 5)
        self.assertEqual(self.run_test_split(os.path.join(self.cu, "prereg_w1.json")), 0)

    def test_split_test_refuses_tmp_ignored_prereg(self):
        p = os.path.join(self.cu, "prereg_w1.json.tmp")
        shutil.copy(os.path.join(self.cu, "prereg_w1.json"), p)
        self.assertFalse(H.tree_dirty())
        self.assertEqual(self.run_test_split(p), 5)

    def test_split_test_refuses_tracked_prereg_with_ignored_task_file(self):
        p = os.path.join(self.cu, "prereg_w1.json")
        shutil.copy(os.path.join(self.cu, "tasks", "WF1.json"), os.path.join(self.cu, "tasks", "WF1.json.tmp"))
        m = json.load(open(p))
        m["tasks"][0]["task_file"] = "tasks/WF1.json.tmp"
        write(p, m)
        git(self.repo, "commit", "-qam", "prereg points at an ignored task file")
        self.assertFalse(H.tree_dirty())
        self.assertEqual(H.validate(m, self.cu), [])
        self.assertEqual(self.run_test_split(p), 5)

    def test_untracked_lists_every_non_tracked_clean_file(self):
        tracked = os.path.join(self.cu, "tasks", "WF1.json")
        self.assertEqual(H.untracked([tracked, os.path.join(self.cu, "prereg_w1.json")]), [])
        tasks = os.path.join(self.cu, "tasks")
        self.assertEqual(H.untracked([tasks]), [tasks])  # a clean tracked dir is not a file
        ign = os.path.join(self.cu, "tasks", "X.tmp")
        open(ign, "w").write("{}")
        new = os.path.join(self.cu, "tasks", "NEW.json")
        open(new, "w").write("{}")
        out = os.path.join(self.tmp, "outside.json")
        open(out, "w").write("{}")
        self.assertEqual(H.untracked([tracked, ign, new, out]), [ign, new, out])
        with open(tracked, "a") as f:
            f.write(" ")
        self.assertEqual(H.untracked([tracked]), [tracked])


class Metrics(unittest.TestCase):
    def steps(self):
        return [json.loads(l) for l in open(FIX) if l.strip()]

    def test_metrics_from_fixture_steps(self):
        out = H.metrics_from_steps(self.steps())
        t = out["WF1"]["A1"]
        self.assertEqual(t["episodes"], 5)
        self.assertEqual(t["success_permille"], 600)
        self.assertEqual(t["actions_median"], 4)
        self.assertEqual(t["actions_p90"], 6)
        self.assertEqual(t["cost_tok_mean"], 1200)
        self.assertEqual(t["cost_usd_e4_mean"], 36)
        self.assertEqual(t["wrong_per_1000"], 100)
        self.assertEqual(t["abstain_permille"], 200)
        self.assertEqual(t["stop_hist"], {"ABSTAIN": 1, "BUDGET_STEPS": 1, "EFFECT_OK": 3})
        self.assertEqual(out["WF1"]["A2"]["success_permille"], 200)
        for v in t.values():
            self.assertNotIsInstance(v, float)

    def test_wilson_lb_no_claim_below_5_seeds(self):
        self.assertIsNone(H.wilson_lb_permille(4, 4))
        self.assertIsNone(H.wilson_lb_permille(0, 3))
        self.assertEqual(H.wilson_lb_permille(5, 5), 565)
        self.assertEqual(H.wilson_lb_permille(0, 5), 0)
        self.assertEqual(H.wilson_lb_permille(3, 5), 230)


class Cli(unittest.TestCase):
    def test_harness_no_rc1(self):
        h = os.path.join(BENCH, "harness.py")
        cases = [[], ["--bogus"], ["--dry-run"], ["--dry-run", "/nonexistent/x.json"],
                 ["--run", PREREG], ["--run", PREREG, "--split", "nope"],
                 ["--dry-run", PREREG, "--run", PREREG]]
        for a in cases:
            r = subprocess.run([sys.executable, "-B", h] + a, capture_output=True, text=True, timeout=60)
            self.assertIn(r.returncode, (0, 2, 3, 4, 5, 6, 7), (a, r.returncode, r.stderr))
            self.assertNotEqual(r.returncode, 1, a)
        r = subprocess.run([sys.executable, "-B", h, "--dry-run", "/nonexistent/x.json"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, "-B", h, "--run", PREREG], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
