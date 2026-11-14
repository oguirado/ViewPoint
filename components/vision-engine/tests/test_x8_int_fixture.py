"""X8 cut-heavy captures use legal witness packs without trimming source evidence."""
import tempfile
import unittest

from eval import x8_citations as x8


class X8WitnessPack(unittest.TestCase):
    def test_scroll_keeps_full_evidence_and_rejects_every_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            vision, pack, valid, how = x8.build_item("scroll", 1, tmp)
        self.assertEqual(how, "region")
        self.assertEqual(len(vision["frames"]), x8.syn.DEF_N)
        self.assertEqual(len(pack["gops"]), 1)
        self.assertLessEqual(len(pack["frame_map"]), 12)
        self.assertEqual(x8.cite.verify_citations(vision, {"results": [valid]}, pack)["rc"], 0)
        for corruption in x8.CORRUPTIONS:
            with self.subTest(corruption=corruption):
                bad = x8.corrupt(corruption, valid, vision["grid"])
                self.assertEqual(x8.cite.verify_citations(vision, {"results": [bad]}, pack)["rc"], 3)

    def test_oracle_not_weakened_by_int_e_migration(self):
        # JCODE-2: the INT-E fixture migration keeps all 10 forged-citation negatives over all 25 items.
        self.assertEqual(len(set(x8.CORRUPTIONS)), 10)
        self.assertEqual(len(x8.items()), 25)
