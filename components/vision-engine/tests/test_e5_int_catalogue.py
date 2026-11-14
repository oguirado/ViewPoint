"""INT-E catalogue and legacy media integration regressions."""
import csv
import unittest
from pathlib import Path

from tests.test_pack import fixture, batch
from visioncore import pack, params
from tools import build_catalogue

ROOT = Path(__file__).resolve().parents[1]


class IntegrationCatalogue(unittest.TestCase):
    def test_token_thresholds_have_registered_values_and_anchors(self):
        with (ROOT / "catalogue/thresholds.tsv").open() as f:
            rows = {r[0]: r for r in csv.reader(f, delimiter="\t") if r and not r[0].startswith("#")}
        for name, value in (("PACK_CALL_TOK", 15500), ("PACK_RUN_TOK", 62000), ("PACK_RETRY_TOK", 93000)):
            with self.subTest(name=name):
                self.assertIn(name, rows)
                self.assertEqual(int(rows[name][1]), value)
                self.assertEqual(rows[name][2:4], ["tokens", "vc.p." + name])
                self.assertEqual(params.PARAMS[name], value)
        self.assertEqual(set(rows), set(params.PARAMS))

    def test_merged_catalogue_matches_all_current_anchors(self):
        expected, _ = build_catalogue.build(str(ROOT))
        for name, text in expected.items():
            with self.subTest(name=name):
                self.assertEqual((ROOT / "catalogue" / name).read_text(), text)

    def test_dense_track_fixture_preserves_spatial_and_class_diversity(self):
        gops, regions = fixture(18)
        selected, _, mandatory = pack.select_frames(gops)
        self.assertLessEqual(mandatory, 12)
        self.assertEqual(len(selected), 12)
        self.assertEqual(len(regions), 54)
        for g in gops:
            self.assertEqual(len({t["k"] for t in g["tracks"]}), 18)
            self.assertEqual({t["cls_mask"] for t in g["tracks"]}, {4, 8, 16, 32, 64})
        result = pack.pack(batch(gops, regions))
        self.assertLessEqual(result.pack["est_tok"], 15500)
        self.assertEqual(result.pack["gops"], ["g0", "g1", "g2"])
