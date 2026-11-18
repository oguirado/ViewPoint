"""Time unittest cases inside an isolated suite process."""

import json
import os
from pathlib import Path
import resource
import sys
import time
import unittest


def cpu_seconds():
    own = resource.getrusage(resource.RUSAGE_SELF)
    waited = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + waited.ru_utime + waited.ru_stime


class TimedResult(unittest.TextTestResult):
    timing = True

    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.rows = []
        self.active = None

    def startTest(self, test):
        super().startTest(test)
        self.active = {
            "test": test.id(),
            "duration_s": time.perf_counter() if self.timing else None,
            "cpu_s": cpu_seconds() if self.timing else None,
            "result": None,
        }

    def stopTest(self, test):
        if self.active is None:
            raise RuntimeError("missing startTest")
        if self.timing:
            self.active["duration_s"] = time.perf_counter() - self.active["duration_s"]
            self.active["cpu_s"] = cpu_seconds() - self.active["cpu_s"]
        if self.active["result"] is None:
            raise RuntimeError(f"missing result for {test.id()}")
        self.rows.append(self.active)
        self.active = None
        super().stopTest(test)

    def mark(self, test, result):
        if self.active is not None:
            old = self.active["result"]
            if old != "error" and (old != "fail" or result == "error"):
                self.active["result"] = result
        else:
            # setUp/tearDownClass and module fixtures can fail before any case runs.
            self.rows.append({"test": test.id(), "duration_s": None,
                              "cpu_s": None, "result": result})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.mark(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.mark(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self.mark(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.mark(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.mark(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.mark(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.mark(test, "fail" if issubclass(err[0], test.failureException) else "error")


def main():
    source, suite, output_name, mode = sys.argv[1:]
    root = Path(source)
    seen = set()
    TimedResult.timing = mode != "trace"
    if mode == "trace":
        def trace(event, args):
            if event == "open":
                path = args[0]
                if not isinstance(path, (str, bytes)) or args[2] & os.O_ACCMODE != os.O_RDONLY:
                    return
            elif event == "exec":
                path = args[0].co_filename
            else:
                return
            path = os.path.abspath(os.fsdecode(path))
            prefix = source + "/"
            if path.startswith(prefix):
                seen.add(path[len(prefix):])

        sys.addaudithook(trace)

    tests = root / "components/vp-direct" / suite / "tests"
    loader = unittest.TestLoader()
    cases = loader.discover(str(tests), pattern="test_*.py")
    expected = cases.countTestCases()
    result = unittest.TextTestRunner(verbosity=1, resultclass=TimedResult).run(cases)
    with open(output_name, "x", encoding="utf-8") as output:
        json.dump({"cases": result.rows, "discovered": expected, "ran": result.testsRun,
                   "inputs": sorted(seen)}, output)
    return int(not result.wasSuccessful() or not expected or result.testsRun != expected)


if __name__ == "__main__":
    sys.exit(main())
