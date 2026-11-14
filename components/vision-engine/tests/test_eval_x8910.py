"""Lane E3 eval drivers X8/X9/X10 + run_all: positive, negative, mutant (a broken subject must flip the verdict)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval import run_all, x8_citations as x8, x9_vmem as x9, x10_compaction as x10


def STORAGE_OK():
    """FX4: x10.run measures a real 200-frame ingest by default; unit tests inject an in-limit measurement so only the
    compaction/FG logic decides the verdict (the storage gate itself is covered in test_e7_x10_storage)."""
    return [{"scenario": sc, "before_bytes": 1000, "after_bytes": 100, "info": {}} for sc in x10.CLEAN_SCENARIOS]


def check_rows(tc, rows, x):
    tc.assertTrue(rows)
    for r in rows:
        tc.assertEqual((r["schema"], r["x"]), ("vqa.eval-row/v1", x))
        json.dumps(r)
        tc.assertFalse([v for v in r.values() if isinstance(v, float)], r)  # integers only


class TestX8(unittest.TestCase):
    def test_pass_small(self):
        rows, s = x8.run(None, 7)
        self.assertEqual((s["verdict"], s["valid_accepted"], s["corrupt_rejected"], s["corrupt_total"]), ("PASS", 7, 70, 70))
        self.assertTrue(all(v == 7 for v in s["per_corruption_rejected"].values()))
        check_rows(self, rows, "X8")

    def test_ten_corruptions_distinct(self):
        self.assertEqual(len(set(x8.CORRUPTIONS)), 10)

    def test_mutant_identity_corruption_killed(self):
        with mock.patch.object(x8, "corrupt", lambda kind, res, grid: res):
            _, s = x8.run(None, 7)
        self.assertEqual((s["verdict"], s["corrupt_rejected"]), ("FAIL", 0))

    def test_mutant_wrong_valid_arm_killed(self):
        real = x8.build_item

        def bad(name, seed, tmp):
            v, pk, res, how = real(name, seed, tmp)
            return v, pk, dict(res, pointers=["f9999:t0000"]), how
        with mock.patch.object(x8, "build_item", bad):
            _, s = x8.run(None, 7)
        self.assertEqual((s["verdict"], s["valid_accepted"]), ("FAIL", 0))

    def test_bound_and_out_files(self):
        self.assertEqual(x8.clopper_pearson_lb_all(250), 988088)
        with tempfile.TemporaryDirectory() as d:
            x8.run(d, 7)
            self.assertTrue((Path(d) / "X8.jsonl").is_file() and (Path(d) / "X8.summary.json").is_file())


class TestX9(unittest.TestCase):
    def test_pass(self):
        rows, s = x9.run(None)
        self.assertEqual(s["verdict"], "PASS")
        self.assertEqual((s["answerable"], s["unrecorded_abstained"], s["recall_at_5_ppm"]), (20, 5, 1000000))
        self.assertLessEqual(s["max_answer_tok"], 2520)
        self.assertEqual(s["verify_failed_gates"], [])
        check_rows(self, rows, "X9")

    def test_negative_answerable_query_in_unrecorded_set_killed(self):
        with mock.patch.object(x9, "UNRECORDED", x9.UNRECORDED[:4] + (("route", {"q": "ammo counter"}),)):
            _, s = x9.run(None)
        self.assertEqual((s["verdict"], s["unrecorded_abstained"]), ("FAIL", 4))

    def test_mutant_wrong_expected_subject_killed(self):
        bad = (("ammo counter", "score_ticker"),) + x9.ROUTE_Q[1:]
        with mock.patch.object(x9, "ROUTE_Q", bad):
            _, s = x9.run(None)
        self.assertLess(s["top1"], 12)  # recall@5 saturates on 4 subjects, top1 is the discriminating metric

    def test_units_valid_and_fg(self):
        us, blobs, ex = x9.build_units(range(1, 6))
        self.assertEqual(len({u["id"] for u in us}), len(us))
        self.assertEqual(sorted(ex["last_good"]), sorted(x[0] for x in x9.SUBJECTS))
        self.assertTrue(any(u["layer"] == "FG" for u in us) and any(u["layer"] == "BG" for u in us))


class TestX10(unittest.TestCase):
    def test_pass(self):
        rows, s = x10.run(None, measure=STORAGE_OK, advisory=None)
        self.assertEqual((s["verdict"], s["cycles_ok"], s["fg_sha_stable"], s["fg_cap_control_rejected"]), ("PASS", 3, True, True))
        self.assertLess(s["l2_bytes_per_run_final"], 500 * 1024)
        check_rows(self, rows, "X10")

    def test_mutant_compaction_dropping_fg_killed(self):
        real = x10.C.compact

        def drop_fg(units_, *a, **k):
            out = real(units_, *a, **k)
            out["active"] = [u for u in out["active"] if u["pin"] is None]
            out["fg_ids_sha256"] = x10.C.fg_ids_sha256(out["active"] + out["archive"])
            return out
        with mock.patch.object(x10.C, "compact", drop_fg):
            _, s = x10.run(None, measure=STORAGE_OK, advisory=None)
        self.assertEqual(s["verdict"], "FAIL")
        self.assertFalse(s["fg_sha_stable"])

    def test_mutant_no_cap_control_killed(self):
        real = x10.C.compact
        with mock.patch.object(x10.C, "compact", lambda u, sizes=None, **k: real(u, sizes, **{**k, "table": None})):
            _, s = x10.run(None, measure=STORAGE_OK, advisory=None)
        self.assertEqual((s["verdict"], s["fg_cap_control_rejected"]), ("FAIL", False))


class TestRunAll(unittest.TestCase):
    def test_only_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            real = x10.run   # run_all calls run(out); keep the real 720p ingest measurement out of this unit test
            with mock.patch.object(x10, "run", lambda out: real(out, measure=STORAGE_OK, advisory=None)):
                sums, ok = run_all.run(d, {"X9", "X10"})
            self.assertTrue(ok)
            self.assertEqual([s["x"] for s in sums], ["X9", "X10"])
            text = (Path(d) / "report.md").read_text()
            self.assertIn("| X9 | G-E8 | PASS |", text)
            self.assertIn("| X6 | - | AUSENTE |", text)
            self.assertEqual(text, run_all.report(sums)[0])  # deterministic, no clock

    def test_missing_driver_is_ausente_not_pass(self):
        s = run_all.run_one("X3", "eval.no_such_driver", Path("."))
        self.assertEqual(s["verdict"], "AUSENTE")
        text, ok = run_all.report([s])
        self.assertTrue(ok)  # absent is declared, never counted as PASS
        self.assertNotIn("| PASS |", text.split("## Details")[0])

    def test_failing_and_crashing_driver_flip_overall(self):
        with tempfile.TemporaryDirectory() as d:
            boom = mock.Mock()
            boom.run.side_effect = RuntimeError("x")
            with mock.patch.object(run_all.importlib, "import_module", return_value=boom):
                s = run_all.run_one("X8", "eval.x8_citations", Path(d))
            self.assertEqual(s["verdict"], "ERROR")
            self.assertFalse(run_all.report([s])[1])
            self.assertFalse(run_all.report([{"x": "X9", "verdict": "FAIL"}])[1])

    def test_main_rc(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(run_all, "DRIVERS", (("X9", "eval.x9_vmem"),)):
            self.assertEqual(run_all.main(["--out", d]), 0)


if __name__ == "__main__":
    unittest.main()
