"""ADC2-W6A-04: g_disk_size.py --no-pin is honoured anywhere in argv (the import guard already scans all of argv; main() must agree)."""
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
G = os.path.join(REPO, "scripts", "cu", "g_disk_size.py")


def run(*a):
    return subprocess.run([sys.executable, "-B", G] + list(a), capture_output=True, text=True, timeout=60).returncode


class NoPin(unittest.TestCase):
    def test_no_pin_any_position(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(run("--no-pin", "--out", td), 0)
            self.assertEqual(run("--out", "--no-pin", td), 0)
            self.assertEqual(run("--out", td, "--no-pin"), 0)
            self.assertEqual(run("--out", td, "--no-pin", "--no-pin"), 0)

    def test_usage_still_rc2(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(run("--no-pin"), 2)
            self.assertEqual(run("--no-pin", "--out"), 2)
            self.assertEqual(run("--no-pin", "--out", td, "extra"), 2)
            self.assertEqual(run("--no-pin", "--out", os.path.join(td, "absent")), 2)

    def test_breach_still_rc5(self):
        with tempfile.TemporaryDirectory() as td:
            os.mkdir(os.path.join(td, "ep"))
            with open(os.path.join(td, "ep", "big.bin"), "wb") as f:
                f.write(b"\0" * (100 * 1024 + 1))
            self.assertEqual(run("--no-pin", "--out", td), 5)


if __name__ == "__main__":
    unittest.main()
