"""Probe the private worker's native libraries without starting a display."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import driver


NUMPY_PROBE = """
import json
import numpy

solution = numpy.linalg.solve([[2, 0], [0, 4]], [4, 12])
print(json.dumps({"module": numpy.__file__, "version": numpy.__version__,
                  "solution": solution.tolist()}))
"""
WORKER_PROBE = """
from pathlib import Path
import sys

sys.path.insert(0, "/checkout/components/vp-direct/qa/live_driver")
import driver

if Path("/proc/1/comm").read_text().strip() != "bwrap":
    raise RuntimeError("worker probe requires the private PID namespace")
from wender.state import schema
from wender.learn import cgroup_memory
""" + NUMPY_PROBE


class WorkerImportSmoke(unittest.TestCase):
    def setUp(self):
        driver.require_scope()
        work = self.enterContext(TemporaryDirectory(prefix="vp-qd-imports-"))
        plan = driver.sandbox(Path(work), ["smoke"], None)
        self.worker = plan[:plan.index("--") + 1] + ["/usr/bin/python3", "-IB", "-c", WORKER_PROBE]

    def test_worker_imports_system_numpy_and_wender(self):
        host = subprocess.run(["/usr/bin/python3", "-IB", "-c", NUMPY_PROBE],
                              check=True, capture_output=True, text=True, timeout=15)
        worker = subprocess.run(self.worker, capture_output=True, text=True, timeout=15)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        observed = json.loads(worker.stdout)
        self.assertEqual(observed, json.loads(host.stdout))
        self.assertEqual(observed["solution"], [2.0, 3.0])

    def test_worker_import_fails_without_each_native_alternative(self):
        for soname in ("libblas.so.3", "liblapack.so.3"):
            with self.subTest(soname=soname):
                mounts = [i for i, option in enumerate(self.worker[:self.worker.index("--")])
                          if option == "--ro-bind" and
                          self.worker[i + 2].startswith(f"/etc/alternatives/{soname}-")]
                self.assertEqual(len(mounts), 1, "mutant requires a Debian native library alternative")
                index, = mounts
                mutant = self.worker[:index] + self.worker[index + 3:]
                result = subprocess.run(mutant, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"{soname}: cannot open shared object file", result.stderr)


if __name__ == "__main__":
    unittest.main()
