"""ER-3: tools/build_wheel.py is reproducible and checks sealed bytes against ve-sealed-1 plus verified equivalence receipts."""
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_wheel  # noqa: E402
import gate_checks  # noqa: E402


class BuildWheel(unittest.TestCase):
    def test_two_builds_identical(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as o1, tempfile.TemporaryDirectory(dir=ROOT / "build") as o2:
            p1, s1 = build_wheel.build("ve-sealed-1-law1", o1)
            p2, s2 = build_wheel.build("ve-sealed-1-law1", o2)
            self.assertEqual(s1, s2)
            self.assertEqual(p1.read_bytes(), p2.read_bytes())
            self.assertRegex(s1, r"^[0-9a-f]{64}$")
            names = zipfile.ZipFile(p1).namelist()
            self.assertIn("vadapt/ramws.py", names)
            self.assertIn("vadapt/blender/from_render_dir.py", names)
            epoch = int(subprocess.run(["git", "-C", str(ROOT), "show", "-s", "--format=%ct", "ve-sealed-1-law1^{commit}"],
                                       capture_output=True, text=True, check=True).stdout)
            y = zipfile.ZipFile(p1).infolist()[0].date_time[0]
            self.assertEqual(y, __import__("time").gmtime(epoch).tm_year)

    def test_cli_prints_sha(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as o:
            r = subprocess.run([sys.executable, "-B", "tools/build_wheel.py", "ve-sealed-1-law1", "--out", o], cwd=ROOT,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertRegex(r.stdout, r"^[0-9a-f]{64}  .+\.whl\n$")

    def test_sealed_dirs_untouched(self):
        # CARD_D01F: use the production rule, including EQUIV_RECEIPT proof
        # binding and both tracked and untracked sealed-tree rejection.
        self.assertTrue((ROOT / ".git").exists(), "sealed gate requires a checkout")
        self.assertEqual(gate_checks.sealed_untouched(ROOT), [])

    def test_sealed_change_without_receipt_rejected(self):
        receipt = ROOT / "eval/results/rt0/EQUIV_RECEIPT.json"
        exists = Path.exists
        with mock.patch.object(Path, "exists", lambda p: False if p == receipt else exists(p)):
            errors = gate_checks.sealed_untouched(ROOT)
        self.assertTrue(any("visioncore/parallel.py differs" in msg for _, msg in errors), errors)

    def test_sealed_receipt_hash_mismatch_rejected(self):
        subject = ROOT / "visioncore/parallel.py"
        read_bytes = Path.read_bytes
        with mock.patch.object(Path, "read_bytes", lambda p: read_bytes(p) + b"\n" if p == subject else read_bytes(p)):
            errors = gate_checks.sealed_untouched(ROOT)
        self.assertTrue(any("new_sha256 mismatch" in msg for _, msg in errors), errors)
        self.assertTrue(any("visioncore/parallel.py differs" in msg for _, msg in errors), errors)

    def test_sealed_unlisted_change_rejected(self):
        git = gate_checks._git

        def with_unlisted(root, *args):
            result = git(root, *args)
            if args[:2] == ("diff", "--name-only"):
                result.stdout += "visioncore/ids.py\n"
            return result

        with mock.patch.object(gate_checks, "_git", with_unlisted):
            errors = gate_checks.sealed_untouched(ROOT)
        self.assertEqual(errors, [("E_GATE_SEALED", "visioncore/ids.py differs from ve-sealed-1")])



if __name__ == "__main__":
    unittest.main()
