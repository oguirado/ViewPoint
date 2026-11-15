"""ADC2-W6A-04: Mutant hardening for g_disk_size.py --no-pin handling and proposed mutants verification."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
G = os.path.join(REPO, "scripts", "cu", "g_disk_size.py")
PROPOSED_MUTANTS = os.path.join(REPO, "scripts", "cu", "mutants_proposed", "os-g-hard-gds-nopin.json")
EXPECTED_ORACLE = "python3 -B -m unittest scripts.cu.tests.test_g_disk_size_argv scripts.cu.tests.test_g_disk_size_nopin"
CAP = 100 * 1024  # 100 KB cap


def run_gds(*args, env=None):
    res = subprocess.run(
        [sys.executable, "-B", G] + list(args),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if res.returncode == 1:
        raise AssertionError("g_disk_size.py returned exit code 1 (must be 0 or 2..7)")
    return res.returncode


class ArgvAndCapTests(unittest.TestCase):
    def test_no_pin_positions(self):
        with tempfile.TemporaryDirectory() as td:
            # --no-pin first
            rc_first = run_gds("--no-pin", "--out", td)
            self.assertEqual(rc_first, 0)

            # --no-pin last
            rc_last = run_gds("--out", td, "--no-pin")
            self.assertEqual(rc_last, 0)

            # --no-pin between --out and its value: argv becomes ['--out', td] after stripping --no-pin, which is valid usage (rc 0)
            rc_between = run_gds("--out", "--no-pin", td)
            self.assertEqual(rc_between, 0)

            # repeated twice
            rc_twice = run_gds("--no-pin", "--out", td, "--no-pin")
            self.assertEqual(rc_twice, 0)

            # given as --no-pin=1 with --no-pin present: stays an unknown option, so argv after stripping --no-pin contains ['--out', td, '--no-pin=1'] (rc 2 usage)
            rc_eq = run_gds("--out", td, "--no-pin=1", "--no-pin")
            self.assertEqual(rc_eq, 2)

    def test_out_dir_boundaries(self):
        # empty out dir with --no-pin
        with tempfile.TemporaryDirectory() as td:
            rc = run_gds("--out", td, "--no-pin")
            self.assertEqual(rc, 0)

        # episode dir of exactly 100 KB cap
        with tempfile.TemporaryDirectory() as td:
            ep = os.path.join(td, "ep1")
            os.mkdir(ep)
            with open(os.path.join(ep, "f.bin"), "wb") as f:
                f.write(b"x" * CAP)
            rc = run_gds("--out", td, "--no-pin")
            self.assertEqual(rc, 0)

        # episode dir of one byte over 100 KB cap
        with tempfile.TemporaryDirectory() as td:
            ep = os.path.join(td, "ep2")
            os.mkdir(ep)
            with open(os.path.join(ep, "f.bin"), "wb") as f:
                f.write(b"x" * (CAP + 1))
            rc = run_gds("--out", td, "--no-pin")
            self.assertEqual(rc, 5)

        # top-level file over the cap
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "top_big.bin"), "wb") as f:
                f.write(b"x" * (CAP + 1))
            rc = run_gds("--out", td, "--no-pin")
            self.assertEqual(rc, 5)

    def test_no_pin_skips_vision_import(self):
        with tempfile.TemporaryDirectory() as pypath:
            vis_dir = os.path.join(pypath, "vision")
            os.mkdir(vis_dir)
            with open(os.path.join(vis_dir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write('raise RuntimeError("vision package imported!")\n')

            env = dict(os.environ, PYTHONPATH=pypath + os.pathsep + os.environ.get("PYTHONPATH", ""))
            with tempfile.TemporaryDirectory() as td:
                rc = run_gds("--out", td, "--no-pin", env=env)
                self.assertEqual(rc, 0)


class ProposedMutantsStructureTests(unittest.TestCase):
    def test_mutants_proposed_json_structure(self):
        self.assertTrue(os.path.isfile(PROPOSED_MUTANTS), "Proposed mutants JSON file missing")
        with open(PROPOSED_MUTANTS, encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("schema"), "os.cu.mutants/v1")
        self.assertIn("mutants", data)
        self.assertIsInstance(data["mutants"], list)
        self.assertGreater(len(data["mutants"]), 0, "Mutants list must not be empty")

        expected_keys = {"id", "file", "find", "replace", "precheck", "oracle"}

        for m in data["mutants"]:
            self.assertIsInstance(m, dict)
            self.assertEqual(set(m.keys()), expected_keys, f"Mutant entry {m.get('id')} keys mismatch")
            self.assertTrue(m["id"].startswith("prop.gds-nopin."), f"Mutant id {m['id']} must start with prop.gds-nopin.")

            target_path = os.path.join(REPO, m["file"])
            self.assertTrue(os.path.isfile(target_path), f"File {m['file']} in mutant {m['id']} does not exist")

            with open(target_path, encoding="utf-8") as tf:
                content = tf.read()

            count = content.count(m["find"])
            self.assertEqual(
                count,
                1,
                f"Mutant {m['id']} find string must occur exactly once in {m['file']}, found {count}",
            )
            self.assertEqual(
                m["oracle"],
                EXPECTED_ORACLE,
                f"Mutant {m['id']} oracle must match task gate command",
            )


if __name__ == "__main__":
    unittest.main()
