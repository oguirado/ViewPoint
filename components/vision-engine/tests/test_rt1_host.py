"""RT1-L0: host receipt. Fake PATH, home dir and cpuinfo only; nothing is installed or executed beyond `--version`."""
import json
import os
import tempfile
import unittest

from tools import rt_host

CPUINFO = "processor : 0\nFeatures : fp asimd sve sve2\nCPU part : 0xd87\n\nprocessor : 5\nFeatures : fp asimd sve sve2\nCPU part : 0xd85\n"


class HostReceipt(unittest.TestCase):
    def _empty(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def test_missing_cargo_is_ausente_with_install_line(self):
        home = self._empty()
        r = rt_host.receipt(path_env=self._empty(), home=home, cpuinfo_text=CPUINFO)
        c = r["tools"]["cargo"]
        self.assertEqual(c["PATH"]["path"], "AUSENTE")
        self.assertEqual(c["cargo_bin"]["path"], "AUSENTE")
        self.assertEqual(c["status"], "AUSENTE")
        self.assertIn("export PATH=$HOME/.cargo/bin:$PATH", c["operator_install"])
        self.assertIn("python3 -m pip install --user 'maturin>=1.7,<2'", r["operator_install"])

    def test_path_and_cargo_bin_probed_separately(self):
        home = self._empty()
        b = os.path.join(home, ".cargo", "bin")
        os.makedirs(b)
        exe = os.path.join(b, "cargo")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\necho cargo 9.9.9\n")
        os.chmod(exe, 0o755)
        r = rt_host.receipt(path_env=self._empty(), home=home, cpuinfo_text=CPUINFO)
        c = r["tools"]["cargo"]
        self.assertEqual(c["PATH"]["path"], "AUSENTE")
        self.assertEqual(c["cargo_bin"]["path"], exe)
        self.assertEqual(c["cargo_bin"]["version"], "cargo 9.9.9")
        self.assertEqual(c["status"], "not_on_PATH")
        self.assertEqual(c["operator_install"], rt_host.CARGO_PATH_LINE)

    def test_cpuinfo_without_sve2(self):
        txt = CPUINFO.replace(" sve2", "")
        f = rt_host.cpu_receipt(txt)["features"]
        self.assertEqual(f, {"asimd": True, "sve": True, "sve2": False})
        self.assertTrue(rt_host.cpu_receipt(CPUINFO)["features"]["sve2"])

    def test_sve2_must_hold_on_every_cpu(self):
        txt = CPUINFO.replace("fp asimd sve sve2\nCPU part : 0xd85", "fp asimd sve\nCPU part : 0xd85")
        self.assertFalse(rt_host.cpu_receipt(txt)["features"]["sve2"])

    def test_per_cpu_part_and_python(self):
        r = rt_host.receipt(path_env="", home=self._empty(), cpuinfo_text=CPUINFO)
        self.assertEqual(r["cpu"]["cpu_part"], {"0": "0xd87", "5": "0xd85"})
        self.assertEqual(r["cpu"]["cpus_by_part"]["0xd85"], [5])
        self.assertIn("Py_GIL_DISABLED", r["python"])
        self.assertIn("numpy", r["python"])
        self.assertIn("total_bytes", r["shm"])
        json.dumps(r)

    def test_empty_cpuinfo_reports_false_not_true(self):
        self.assertEqual(rt_host.cpu_receipt("")["features"], {"asimd": False, "sve": False, "sve2": False})


if __name__ == "__main__":
    unittest.main()
