#!/usr/bin/env python3
"""HB2-01: runtime trace of the frozen suite -> TESTS edges (test -> tool symbol), receipt kind `trace`.

`sys.setprofile` call events for code objects under <subject>/tools/, attributed to the running test through a
TestResult.startTest hook; symbol = co_qualname cut at the first `.<locals>` (nested defs belong to their owner).
Module-level `<module>` events are ignored (import-time execution belongs to no test).
"""
import argparse, io, os, sys, unittest

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402
import hb_extract2 as X  # noqa: E402


class Tracer:
    def __init__(self, subject, qual_to_id):
        self.tools_dir = os.path.abspath(subject) + os.sep
        self.qual_to_id = qual_to_id            # (path relative to the subject, qual) -> id
        self.cur = None
        self.hits = {}                          # test id -> set of tool symbol ids
        self.file_cache = {}

    def prof(self, frame, event, arg):
        if event != "call" or self.cur is None:
            return
        co = frame.f_code
        fn = co.co_filename
        r = self.file_cache.get(fn)
        if r is None:
            r = fn[len(self.tools_dir):] if fn.startswith(self.tools_dir) else ""
            r = r if L.is_src(r) else ""
            self.file_cache[fn] = r
        if not r:
            return
        q = co.co_qualname
        if q == "<module>":
            return
        q = q.split(".<locals>")[0]
        sid = self.qual_to_id.get((r, q))
        if sid is None and q.startswith("<"):
            sid = self.qual_to_id.get((r, X.MOD))
        if sid:
            self.hits.setdefault(self.cur, set()).add(sid)


def run(subject, quiet=True):
    subject = os.path.abspath(subject)
    repo = X.Repo(subject)
    qual_to_id = {(m.path, s["qual"]): s["id"] for m in repo.mods if L.is_src(m.path) for s in m.syms}
    tid = {}
    for m in repo.mods:
        if m.path.startswith("tests/"):
            for s in m.syms:
                if s["kind"] == "test":
                    cls, meth = s["qual"].split(".", 1)
                    tid[(m.stem, meth, cls)] = s["id"]
    tr = Tracer(subject, qual_to_id)
    status = {}

    class Res(unittest.TextTestResult):
        def startTest(self, test):
            super().startTest(test)
            tr.cur = tid.get((type(test).__module__, test._testMethodName, type(test).__name__))
            if tr.cur:
                tr.hits.setdefault(tr.cur, set())
                status[tr.cur] = "pass"

        def stopTest(self, test):
            tr.cur = None
            super().stopTest(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            k = tid.get((type(test).__module__, test._testMethodName, type(test).__name__))
            if k:
                status[k] = "fail"

        addError = addFailure

    tests_dir = os.path.join(subject, "tests")
    sys.path.insert(0, tests_dir)
    sys.path.insert(0, os.path.join(subject, "tools"))
    if L.SRC_DIRS:                                   # HBV2_SRC_DIRS: run only the in-scope test modules
        sys.path.insert(0, subject)
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(m.stem) for m in repo.mods if m.path.startswith("tests/"))
    else:
        suite = unittest.defaultTestLoader.discover(tests_dir, top_level_dir=tests_dir)
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=Res)
    sys.setprofile(tr.prof)
    try:
        res = runner.run(suite)
    finally:
        sys.setprofile(None)
    edges = sorted((t, s) for t, ss in tr.hits.items() for s in ss)
    return dict(edges=edges, ran=res.testsRun, failures=len(res.failures), errors=len(res.errors), status=status,
                coverage={t: len(ss) for t, ss in sorted(tr.hits.items())})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--out", default=os.path.join(L.HB, "out", "trace.json"))
    a = ap.parse_args(argv)
    r = run(a.subject)
    L.clean_pycache(a.subject)
    npass = sum(1 for v in r["status"].values() if v == "pass")
    m = dict(tests=r["ran"], failures=r["failures"], errors=r["errors"], tests_edges=len(r["edges"]),
             tests_with_edges=sum(1 for v in r["coverage"].values() if v), pass_tests=npass)
    doc = X.make_receipt("trace", "hb_trace.py/2.0", m, {"edges_sha256": L.sha256b(L.canon(r["edges"]))})
    doc["edges"] = [list(e) for e in r["edges"]]
    doc["coverage"] = r["coverage"]
    L.write_json(a.out, doc)
    print(L.dumps(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
