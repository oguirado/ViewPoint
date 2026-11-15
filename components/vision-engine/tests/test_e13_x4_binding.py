"""JC3-2: the LIVE held-out path (summarize_manifest / check_manifest_binding) binds every row to the frozen evaluation
config (manifest x4 config + fitted params_sha256). A missing or mismatched config_sha256 / partition_sha256 on ANY row
-> rc 5 E_GATE split_leak. Uses the committed X4.jsonl rows (no driver run) and a tiny private manifest (seeds 5000+)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval import x4_fp as X4
from tests.test_e10_split_manifest import tiny_manifest
from visioncore.errors import VqaError

RESULTS = Path(__file__).resolve().parent.parent / "eval" / "results" / "X4.jsonl"


def committed_rows():
    return [json.loads(s) for s in RESULTS.read_text().splitlines()]


class LiveConfigBinding(unittest.TestCase):
    def assertLeak(self, fn, why):
        with self.assertRaises(VqaError) as cm:
            fn()
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_GATE", 5))
        self.assertEqual((cm.exception.detail["reason"], cm.exception.detail["why"]), ("split_leak", why))

    def test_committed_rows_nominal_pass(self):
        s = X4.summarize_manifest(committed_rows(), X4.load_manifest())
        self.assertEqual(s["verdict"], "PASS")
        self.assertEqual(s["config_sha256"], X4.manifest_config_sha(X4.load_manifest()))

    def test_wrong_or_missing_config_on_any_row_fails(self):
        m = X4.load_manifest()
        rows = committed_rows()
        for i in (0, 1, len(rows) - 1):
            for how in ("wrong", "missing", "empty"):
                r = copy.deepcopy(rows)
                if how == "wrong":
                    r[i]["config_sha256"] = "0" * 64
                elif how == "missing":
                    del r[i]["config_sha256"]
                else:
                    r[i]["config_sha256"] = ""
                with self.subTest(row=i, how=how):
                    self.assertLeak(lambda: X4.summarize_manifest(r, m),
                                    "config_sha_mismatch" if how == "wrong" else "unbound_row")

    def test_uniformly_wrong_config_fails(self):
        """All rows agree with each other but not with the frozen receipt (the old rows[0] echo would pass)."""
        r = [dict(x, config_sha256="1" * 64) for x in committed_rows()]
        self.assertLeak(lambda: X4.summarize_manifest(r, X4.load_manifest()), "config_sha_mismatch")

    def test_params_drift_fails(self):
        """Rows computed under another fitted params table no longer match the frozen config."""
        rows, m = committed_rows(), X4.load_manifest()
        with mock.patch.object(X4.P, "params_sha256", return_value="f" * 64):
            self.assertLeak(lambda: X4.summarize_manifest(rows, m), "config_sha_mismatch")

    def test_wrong_or_missing_partition_fails(self):
        m = X4.load_manifest()
        for v in ("0" * 64, None):
            r = copy.deepcopy(committed_rows())
            if v is None:
                del r[2]["partition_sha256"]
            else:
                r[2]["partition_sha256"] = v
            with self.subTest(v=v):
                self.assertLeak(lambda: X4.summarize_manifest(r, m), "partition_sha_mismatch")

    def test_live_run_rows_tiny_manifest(self):
        """End-to-end live path on a tiny private manifest: own rows pass binding; tampered config refused."""
        with tempfile.TemporaryDirectory() as d:
            m = tiny_manifest(d)
            rows, summ = X4.run_rows(30, None, manifest=m)
            self.assertIsNotNone(summ)
            self.assertEqual({r["config_sha256"] for r in rows}, {X4.manifest_config_sha(m)})
            self.assertEqual(summ["config_sha256"], X4.manifest_config_sha(m))
            bad = [dict(rows[0], config_sha256=X4.config_sha())] + rows[1:]   # SEAL-config sha, not this manifest's
            self.assertLeak(lambda: X4.summarize_manifest(bad, m), "config_sha_mismatch")

    def test_legacy_stats_records_expected_config(self):
        rows = committed_rows()
        s = X4._stats([dict(r, config_sha256="2" * 64) for r in rows], 30, "3" * 64)
        self.assertEqual(s["config_sha256"], "3" * 64)


if __name__ == "__main__":
    unittest.main()
