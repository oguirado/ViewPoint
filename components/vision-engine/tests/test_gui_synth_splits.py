"""P1 W2 splits: seed ranges, split_leak rc 5, purpose=tune refusal (M4), cross-split sha collision (M5)."""
import unittest


def _refused(fn, *a, **kw):
    from eval.gui_synth import splits
    with unittest.TestCase().assertRaises(splits.SplitLeak) as cm:
        fn(*a, **kw)
    return cm.exception


class TestSplits(unittest.TestCase):
    def test_ranges_ok(self):
        from eval.gui_synth import splits
        splits.check("DEV", [0, 63], "tune")

    def test_k_outside_range_split_leak_rc5(self):
        for split, ks in (("DEV", [1_000_000]), ("DEV", [64]), ("GATE", [0]), ("GATE", [1_000_032]), ("TEST", [1_000_000])):
            e = _refused(__import__("eval.gui_synth.splits", fromlist=["check"]).check, split, ks, "evaluate")
            self.assertEqual(e.code, "split_leak", (split, ks))
            self.assertEqual(e.rc, 5)

    def test_tune_on_gate_and_test_refused(self):
        from eval.gui_synth import splits
        for split, k in (("GATE", 1_000_000), ("TEST", 2_000_000)):
            e = _refused(splits.check, split, [k], "tune")
            self.assertEqual((e.code, e.rc), ("split_leak", 5))
            self.assertIn("tun", str(e).lower())  # refused for the purpose itself, not for an unrelated seal problem
        splits.check("DEV", [0], "tune")

    def test_sha_collision_across_splits_refused(self):
        from eval.gui_synth import splits
        tables = {"DEV": ["aa", "bb"], "GATE": ["cc", "bb"]}
        e = _refused(splits.check, "DEV", [0], "evaluate", sha_tables=tables)
        self.assertEqual(e.code, "split_leak")
        splits.check("DEV", [0], "evaluate", sha_tables={"DEV": ["aa"], "GATE": ["cc"]})

    def test_cli_rc5_leaked_seed(self):
        import subprocess, sys
        p = subprocess.run([sys.executable, "-m", "eval.gui_synth.run", "--split", "DEV", "--ks", "1000000"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
