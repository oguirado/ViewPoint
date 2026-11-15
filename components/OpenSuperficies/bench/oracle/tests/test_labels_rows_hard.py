import json
import os
import subprocess
import sys
import tempfile
import unittest

from bench.oracle import labels_oracle as LO

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSED_MUTANTS_PATH = os.path.join(HERE, "..", "..", "..", "scripts", "cu", "mutants_proposed", "os-g-hard-labels-rows.json")


def make_sample(seq, t, nodes=None, **k):
    d = {
        "seq": seq,
        "t_us": t,
        "snap": "aa",
        "nodes": nodes,
        "sx": 0,
        "sy": 0,
        "focus": None,
        "shifts": [],
        "frac": 0,
        "canv": [],
    }
    d.update(k)
    return d


class TestMutantProposalsStructure(unittest.TestCase):
    def test_proposed_mutants_schema_and_rules(self):
        self.assertTrue(os.path.isfile(PROPOSED_MUTANTS_PATH), f"Missing file {PROPOSED_MUTANTS_PATH}")
        with open(PROPOSED_MUTANTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("schema"), "os.cu.mutants/v1")
        mutants = data.get("mutants")
        self.assertIsInstance(mutants, list)
        self.assertGreater(len(mutants), 0)

        expected_keys = {"id", "file", "find", "replace", "precheck", "oracle"}
        expected_oracle = "python3 -B -m unittest bench.oracle.tests.test_labels_rows_hard bench.oracle.tests.test_labels_oracle"

        root_dir = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

        for m in mutants:
            self.assertEqual(set(m.keys()), expected_keys)
            self.assertTrue(m["id"].startswith("prop.labels-rows."), f"Invalid id prefix: {m['id']}")
            self.assertEqual(m["oracle"], expected_oracle)

            target_file_abs = os.path.join(root_dir, m["file"])
            self.assertTrue(os.path.isfile(target_file_abs), f"Target file does not exist: {target_file_abs}")
            with open(target_file_abs, "r", encoding="utf-8") as tf:
                content = tf.read()

            find_count = content.count(m["find"])
            self.assertEqual(find_count, 1, f"Expected 'find' string {m['find']!r} to occur exactly once in {m['file']}, found {find_count}")


class TestNodeRowsHardening(unittest.TestCase):
    def test_node_row_length_6_and_7(self):
        row6 = [9, 10, 10, 100, 100, 1]
        row7 = [9, 10, 10, 100, 100, 1, "sha"]
        s_bad = [make_sample(1, 1000000, nodes=[row6]), make_sample(2, 1016666, nodes=[row6])]
        s_good = [make_sample(1, 1000000, nodes=[row7]), make_sample(2, 1016666, nodes=[row7])]

        with self.assertRaises(ValueError):
            LO.derive(s_bad, [], 1920, 1080, "run1")

        out = LO.derive(s_good, [], 1920, 1080, "run1")
        self.assertIsInstance(out, list)

    def test_node_row_geometry_types(self):
        bad_geoms = [
            True,        # bool
            10.5,        # float
            "10",        # numeric string
            None,        # None
        ]
        for bad_g in bad_geoms:
            row_bad = [9, bad_g, 10, 100, 100, 1, "sha"]
            s_bad = [make_sample(1, 1000000, nodes=[row_bad]), make_sample(2, 1016666, nodes=[row_bad])]
            with self.assertRaises(ValueError):
                LO.derive(s_bad, [], 1920, 1080, "run1")

    def test_node_list_empty_and_not_a_list(self):
        # Empty node list is allowed
        s_empty = [make_sample(1, 1000000, nodes=[]), make_sample(2, 1016666, nodes=[])]
        out = LO.derive(s_empty, [], 1920, 1080, "run1")
        self.assertIsInstance(out, list)

        # Nodes not a list
        not_lists = ["not a list", 123, {"a": 1}, True]
        for bad_nodes in not_lists:
            s_bad = [make_sample(1, 1000000, nodes=bad_nodes), make_sample(2, 1016666, nodes=bad_nodes)]
            with self.assertRaises(ValueError):
                LO.derive(s_bad, [], 1920, 1080, "run1")


class TestRectHardening(unittest.TestCase):
    def test_rect_ok_and_validate_label(self):
        base_label = {
            "schema": "vqa.oracle.label/v1",
            "run_id": "r",
            "event_id": 0,
            "f0": 1,
            "f1": 2,
            "tiles": [0],
            "ideal": 2,
            "mv": None,
            "period_ms": None,
            "flags": [],
            "source": "mutation",
            "source_ref": "9",
            "confidence": "exact",
        }

        bad_rects = [
            [-1, 10, 100, 100],      # negative x
            [10, -1, 100, 100],      # negative y
            [10, 10, 0, 100],        # w=0
            [10, 10, 100, 0],        # h=0
            [10, 10, -5, 100],       # negative w
            [10, 10, 100, -5],       # negative h
            [10, 10, True, 100],     # bool
            [10, 10, 10.5, 100],     # float
            [10, 10, "100", 100],    # numeric string
            [10, 10, None, 100],     # None
            [10, 10, 100],           # len 3
            [10, 10, 100, 100, 50],  # len 5
            [],                      # len 0
        ]

        for bad_r in bad_rects:
            self.assertFalse(LO._rect_ok(bad_r), f"_rect_ok accepted bad rect {bad_r}")
            bad_lab = dict(base_label, rects=[bad_r])
            with self.assertRaises(ValueError):
                LO.validate_label(bad_lab)

        good_rect = [10, 10, 100, 100]
        self.assertTrue(LO._rect_ok(good_rect))
        good_lab = dict(base_label, rects=[good_rect])
        LO.validate_label(good_lab)


class TestCaretRectAndCLISubprocess(unittest.TestCase):
    def _run_cli(self, extra_args, samples=None):
        if samples is None:
            samples = [make_sample(1, 1000000, focus="f1"), make_sample(2, 1016666, focus="f1")]
        with tempfile.TemporaryDirectory() as td:
            sp = os.path.join(td, "s.json")
            op = os.path.join(td, "l.jsonl")
            with open(sp, "w", encoding="utf-8") as f:
                json.dump({"samples": samples, "ops": []}, f)
            cmd = [
                sys.executable,
                "-B",
                "-m",
                "bench.oracle.labels_oracle",
                "--samples",
                sp,
                "--w",
                "1920",
                "--h",
                "1080",
                "--run-id",
                "r",
                "--out",
                op,
            ] + extra_args
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode, proc.stdout, proc.stderr

    def test_caret_rect_cli_boundary_cases(self):
        # 3 values -> rc 2
        rc, _, err = self._run_cli(["--caret-rect", "50,42,2"])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)

        # 4 values -> rc 0
        rc, _, _ = self._run_cli(["--caret-rect", "50,42,2,36"])
        self.assertEqual(rc, 0)

        # 5 values -> rc 2
        rc, _, err = self._run_cli(["--caret-rect", "50,42,2,36,10"])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)

        # float value -> rc 2
        rc, _, err = self._run_cli(["--caret-rect", "50,42.5,2,36"])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)

        # empty string / space argument -> rc 2
        rc, _, err = self._run_cli(["--caret-rect", " "])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)

        # empty values in comma list -> rc 2
        rc, _, err = self._run_cli(["--caret-rect", ",,,"])
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)

        # negative w -> rc 0 (handled safely)
        rc, _, err = self._run_cli(["--caret-rect", "50,42,-2,36"])
        self.assertEqual(rc, 0)

    def test_cli_bad_node_rows(self):
        bad_nodes_list = [
            [[9, 10, 10, 100, 100, 1]],            # length 6
            [[9, True, 10, 100, 100, 1, "sha"]],   # bool geometry
            [[9, 10.5, 10, 100, 100, 1, "sha"]],   # float geometry
            [[9, "10", 10, 100, 100, 1, "sha"]],   # numeric string geometry
            [[9, None, 10, 100, 100, 1, "sha"]],   # None geometry
            "not a list",                         # nodes not a list
        ]
        for bad_nodes in bad_nodes_list:
            samples = [make_sample(1, 1000000, nodes=bad_nodes), make_sample(2, 1016666, nodes=bad_nodes)]
            rc, stdout, stderr = self._run_cli([], samples=samples)
            self.assertEqual(rc, 2, f"Expected rc 2 for bad nodes {bad_nodes}, got {rc}")
            self.assertNotIn("Traceback", stderr)
            self.assertIn("E_SCHEMA", stderr)


if __name__ == "__main__":
    unittest.main()
