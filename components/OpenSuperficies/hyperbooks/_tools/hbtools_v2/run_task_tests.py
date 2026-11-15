#!/usr/bin/env python3
"""Run tests/test_<Task>_*.py and write the task receipt out/<Task>.json (counts only; measurements live in their own receipts)."""
import io, os, sys, unittest

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    task = argv[0]
    tests_dir = os.path.join(L.HB, "tests")
    sys.path.insert(0, tests_dir)
    suite = unittest.defaultTestLoader.discover(tests_dir, pattern="test_%s_*.py" % task, top_level_dir=tests_dir)
    buf = io.StringIO()
    res = unittest.TextTestRunner(stream=buf, verbosity=1).run(suite)
    bad = len(res.failures) + len(res.errors)
    rec = {"schema": "hb.receipt.v2", "kind": "test", "tool": "run_task_tests.py", "task": task,
           "metrics": {"tests": res.testsRun, "passed": res.testsRun - bad - len(res.skipped), "failed": bad, "skipped": len(res.skipped)},
           "inputs": {}}
    L.write_json(os.path.join(L.HB, "out", "%s.json" % task), rec)
    if bad:
        sys.stderr.write(buf.getvalue()[:3500])
    print(L.dumps(rec["metrics"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
