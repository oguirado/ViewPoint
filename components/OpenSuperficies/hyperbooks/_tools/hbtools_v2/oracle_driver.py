#!/usr/bin/env python3
"""Nonce driver run INSIDE a copy of the subject: runs the named unittest modules, counts assertion calls, optional line tracing.

oracle_driver.py --tests-dir D --modules a,b --nonce N [--trace FILE:LINE --spec JSON --hits OUT.json]
stdout: `HB_ORACLE_CHECKS <n>` and, only if every test passed and at least one ran, `HB_ORACLE_DONE <nonce>`. Exit 0 iff those hold.
"""
import argparse, io, json, os, sys, unittest

sys.dont_write_bytecode = True
CHECKS = [0]


def count_assertions():
    for name in dir(unittest.TestCase):
        if name.startswith("assert") and callable(getattr(unittest.TestCase, name)) and not name.startswith("assert_"):
            orig = getattr(unittest.TestCase, name)

            def wrap(self, *a, __orig=orig, **k):
                CHECKS[0] += 1
                return __orig(self, *a, **k)
            wrap.__name__ = name
            setattr(unittest.TestCase, name, wrap)


class Tracer:
    def __init__(self, path, line, spec):
        self.path, self.line, self.spec = os.path.realpath(path), line, spec
        self.hits, self.vals = 0, []

    def glob(self, frame, event, arg):
        if os.path.realpath(frame.f_code.co_filename) == self.path:
            return self.loc
        return None

    def loc(self, frame, event, arg):
        if event == "line" and frame.f_lineno == self.line:
            self.hits += 1
            s = self.spec
            try:
                if s and s["kind"] == "eq":
                    self.vals.append((eval(s["lhs"], frame.f_globals, frame.f_locals), eval(s["rhs"], frame.f_globals, frame.f_locals)))
                elif s and s["kind"] == "positive":
                    self.vals.append(eval(s["expr"], frame.f_globals, frame.f_locals))
                elif s and s["kind"] == "collide":
                    self.vals.append((eval(s["group"], frame.f_globals, frame.f_locals), eval(s["diff"], frame.f_globals, frame.f_locals)))
            except Exception:
                pass
        return self.loc

    def result(self):
        s, v = self.spec, self.vals
        tie = None
        if s:
            if s["kind"] == "eq":
                tie = any(a == b for a, b in v)
            elif s["kind"] == "positive":
                tie = any(x > 0 for x in v)
            elif s["kind"] == "collide":
                seen = {}
                tie = False
                for g, d in v:
                    seen.setdefault(g, set()).add(d)
                    if len(seen[g]) > 1:
                        tie = True
        return {"hits": self.hits, "tie": tie}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests-dir", required=True)
    ap.add_argument("--modules", required=True)
    ap.add_argument("--nonce", required=True)
    ap.add_argument("--trace")
    ap.add_argument("--spec")
    ap.add_argument("--hits")
    a = ap.parse_args()
    count_assertions()
    sys.path.insert(0, a.tests_dir)
    tr = None
    if a.trace:
        f, ln = a.trace.rsplit(":", 1)
        tr = Tracer(f, int(ln), json.loads(a.spec) if a.spec else None)
        sys.settrace(tr.glob)
    suite = unittest.TestSuite()
    for m in a.modules.split(","):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(m))
    buf = io.StringIO()
    res = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)
    sys.settrace(None)
    if tr and a.hits:
        json.dump(tr.result(), open(a.hits, "w"))
    print("HB_ORACLE_CHECKS %d" % CHECKS[0])
    ok = res.wasSuccessful() and res.testsRun > 0
    if not ok:
        sys.stderr.write(buf.getvalue()[-1500:])
    else:
        print("HB_ORACLE_DONE " + a.nonce)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
