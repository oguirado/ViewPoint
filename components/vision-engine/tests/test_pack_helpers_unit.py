"""Unit tests for private judge-pack item helpers in visioncore/pack.py."""
import unittest
import zlib
from visioncore import ids, pack as P
from visioncore.errors import VqaError


class TestChunk(unittest.TestCase):
    def test_chunk_exact_format_and_crc32(self):
        tag = b"IHDR"
        data = b"\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00"
        chunk = P._chunk(tag, data)

        length = int.from_bytes(chunk[:4], "big")
        self.assertEqual(length, len(data))
        self.assertEqual(chunk[4:8], tag)
        self.assertEqual(chunk[8:8 + length], data)

        crc_expected = zlib.crc32(tag + data) & 0xFFFFFFFF
        crc_actual = int.from_bytes(chunk[8 + length:], "big")
        self.assertEqual(crc_actual, crc_expected)

        # Exact expected chunk bytes
        expected_bytes = (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + crc_expected.to_bytes(4, "big")
        )
        self.assertEqual(chunk, expected_bytes)

    def test_chunk_negative_invalid_type(self):
        with self.assertRaises(TypeError):
            P._chunk("IHDR", b"data")


class TestLabels(unittest.TestCase):
    def test_labels_weight_and_label_resolution(self):
        # 1. Highest flag weight: FLASH (9)
        tr_flash = {"flags": ["FLASH", "DRIFT"], "cls_mask": 0}
        self.assertEqual(P._labels(tr_flash), (9, "FLASH"))

        # 2. CYCLE flag converted to FLICKER (8)
        tr_cycle = {"flags": ["CYCLE"], "cls_mask": 0}
        self.assertEqual(P._labels(tr_cycle), (8, "FLICKER"))

        # 3. GLOBAL flag alters MOVED weight to 2; ties broken alphabetically ("GLOBAL" < "MOVED")
        tr_moved_global = {"flags": ["GLOBAL"], "cls_mask": 1 << 3}
        self.assertEqual(P._labels(tr_moved_global), (2, "GLOBAL"))

        tr_moved = {"flags": [], "cls_mask": 1 << 3}
        self.assertEqual(P._labels(tr_moved), (4, "MOVED"))

        # 4. Tie breaking: when weights are equal, tie-break by label string alphabetically
        # Bit 4 (FROZEN: w=8, name="FROZEN"), Bit 5 (FLICKER: w=8, name="FLICKER")
        tr_tie = {"flags": [], "cls_mask": (1 << 4) | (1 << 5)}
        w, lab = P._labels(tr_tie)
        self.assertEqual((w, lab), (8, "FLICKER"))

        # 5. Fallback when no cands
        tr_empty = {"flags": [], "cls_mask": 0}
        self.assertEqual(P._labels(tr_empty), (1, "CHANGED"))

    def test_labels_negative_invalid_cls_mask(self):
        with self.assertRaises((ValueError, TypeError)):
            P._labels({"cls_mask": "invalid"})


class TestTrackItems(unittest.TestCase):
    def test_track_items_deterministic_order_and_content(self):
        tr1 = {"k": ids.track_id(0, 5), "first": 0, "cls_mask": 4, "flags": ["APPEAR"]}
        tr2 = {"k": ids.track_id(2, 10), "first": 2, "cls_mask": 8, "flags": []}
        g = {"g": 0, "tracks": [tr1, tr2]}

        items = P._track_items(g)
        self.assertEqual(len(items), 2)

        expected_item0 = {
            "id": tr1["k"],
            "kind": "track",
            "w": 7,
            "lab": "APPEAR",
            "first": 0,
            "mm": 5,
            "tr": tr1,
            "g": 0,
        }
        expected_item1 = {
            "id": tr2["k"],
            "kind": "track",
            "w": 4,
            "lab": "MOVED",
            "first": 2,
            "mm": 10,
            "tr": tr2,
            "g": 0,
        }
        self.assertEqual(items[0], expected_item0)
        self.assertEqual(items[1], expected_item1)

    def test_track_items_negative_missing_track_id(self):
        g = {"g": 0, "tracks": [{"first": 0}]}
        with self.assertRaises(KeyError):
            P._track_items(g)


class TestFrameallItems(unittest.TestCase):
    def test_frameall_items_exact_selection_and_ordering(self):
        f0_id = ids.global_id(10)
        f1_id = ids.global_id(12)

        g = {
            "g": 1,
            "f0": 10,
            "cut": True,
            "events": [
                ["FLASH", f0_id],
                ["FLASH", f1_id],
            ],
            "global": [
                [10, True, False],
            ],
        }

        items = P._frameall_items(g)
        self.assertEqual(len(items), 2)
        fids = [it["id"] for it in items]
        self.assertEqual(fids, sorted([f0_id, f1_id]))

        item_map = {it["id"]: it for it in items}
        self.assertEqual(item_map[f0_id]["lab"], "CUT")
        self.assertEqual(item_map[f0_id]["w"], 9)
        self.assertEqual(item_map[f0_id]["first"], 10)
        self.assertEqual(item_map[f0_id]["g"], 1)

        self.assertEqual(item_map[f1_id]["lab"], "FLASH")
        self.assertEqual(item_map[f1_id]["w"], 9)
        self.assertEqual(item_map[f1_id]["first"], 12)
        self.assertEqual(item_map[f1_id]["g"], 1)

    def test_frameall_items_negative_missing_f0(self):
        g = {"g": 0, "cut": True}
        with self.assertRaises(KeyError):
            P._frameall_items(g)


class TestQuote(unittest.TestCase):
    def test_quote_exact_rendering(self):
        # 1. frameall item
        fa_item = {"kind": "frameall", "id": ids.global_id(5)}
        q_fa = P._quote(fa_item, {})
        self.assertEqual(q_fa, "f5:all ALL")

        # 2. track item
        tr = {"k": ids.track_id(3, 12), "first": 3, "last": 5, "cls_mask": 4, "r_rep": ids.region_id(3, 12)}
        tr_item = {"kind": "track", "id": tr["k"], "tr": tr}
        reg = {
            "id": ids.region_id(3, 12),
            "frame": 3,
            "bbox_t": [0, 0, 1, 1],
            "bbox_px": [0, 0, 32, 32],
        }
        regions = {reg["id"]: reg}
        q_tr = P._quote(tr_item, regions)
        self.assertEqual(q_tr, "k3:000c CHANGED")

    def test_quote_negative_invalid_id(self):
        invalid_item = {"kind": "frameall", "id": "invalid_id"}
        with self.assertRaises(VqaError) as cm:
            P._quote(invalid_item, {})
        self.assertEqual(cm.exception.code, "E_VC_ID_SYNTAX")


class TestEncode(unittest.TestCase):
    def test_encode_exact_rendering(self):
        reg = {
            "id": ids.region_id(4, 8),
            "frame": 4,
            "bbox_t": [0, 0, 1, 1],
            "bbox_px": [0, 0, 16, 16],
        }
        regions = {reg["id"]: reg}

        # 1. frameall item level 0 vs level 1
        fa_item = {"kind": "frameall", "id": ids.global_id(4), "lab": "CUT"}
        enc_fa_l0 = P._encode(fa_item, 0, regions)
        self.assertEqual(enc_fa_l0, ["f4:all", "CUT", "f4:all ALL"])

        enc_fa_l1 = P._encode(fa_item, 1, regions)
        self.assertEqual(enc_fa_l1, ["f4:all", "CUT"])

        # 2. track item level 0 vs level 1
        tr = {
            "k": ids.track_id(4, 8),
            "first": 4,
            "last": 6,
            "cls_mask": 4,
            "r_rep": ids.region_id(4, 8),
            "mv_mode": [1, -1],
            "n_tiles_max": 2,
            "period": 3,
            "flags": ["APPEAR"],
        }
        tr_item = {"kind": "track", "id": tr["k"], "lab": "CHANGED", "tr": tr}

        enc_tr_l1 = P._encode(tr_item, 1, regions)
        self.assertEqual(enc_tr_l1, ["k4:0008", 4, 6, "CHANGED", [1, -1]])

        enc_tr_l0 = P._encode(tr_item, 0, regions)
        expected_l0 = [
            "k4:0008",
            "r4:0008",
            "CHANGED",
            [0, 0, 16, 16],
            2,
            [1, -1],
            3,
            ["APPEAR"],
            "k4:0008 CHANGED mv=1,-1 p=3",
        ]
        self.assertEqual(enc_tr_l0, expected_l0)

    def test_encode_negative_missing_region(self):
        tr = {"k": ids.track_id(1, 1), "r_rep": ids.region_id(1, 1), "mv_mode": [0, 0]}
        tr_item = {"kind": "track", "id": tr["k"], "lab": "CHANGED", "tr": tr}
        with self.assertRaises(VqaError) as cm:
            P._encode(tr_item, 0, {})
        self.assertEqual(cm.exception.code, "E_SCHEMA")


class TestCounts(unittest.TestCase):
    def test_counts_exact_aggregation(self):
        tr1 = {"cls_mask": (1 << 2) | (1 << 3), "flags": ["FLICKER"]}  # C=1, M=1, F=1
        tr2 = {"cls_mask": (1 << 5) | (1 << 6), "flags": ["FROZEN", "DRIFT"]}  # Z=1, D=1
        tr3 = {"cls_mask": (1 << 4), "flags": ["CYCLE"]}  # F=1
        g = {"tracks": [tr1, tr2, tr3]}

        cnts = P._counts(g, frameall_n=2)
        expected = {"C": 1, "M": 1, "F": 2, "Z": 1, "D": 1, "X": 2}
        self.assertEqual(cnts, expected)

    def test_counts_negative_invalid_cls_mask(self):
        g = {"tracks": [{"cls_mask": "invalid"}]}
        with self.assertRaises(ValueError):
            P._counts(g, 0)


class TestKey(unittest.TestCase):
    def test_key_exact_ordering(self):
        item1 = {"id": "k1:0001", "w": 5, "first": 10, "mm": 1}
        item2 = {"id": "k1:0002", "w": 3, "first": 10, "mm": 2}
        acc = {"k1:0002": 5}  # acc boosts item2 effective weight to 3+5=8

        key1 = P._key(item1, acc)
        key2 = P._key(item2, acc)

        self.assertEqual(key1, (-5, 10, 1, "k1:0001"))
        self.assertEqual(key2, (-8, 10, 2, "k1:0002"))
        self.assertLess(key2, key1)

    def test_key_negative_missing_field(self):
        item = {"id": "k1:0001"}
        with self.assertRaises(KeyError):
            P._key(item, {})


class TestSize(unittest.TestCase):
    def test_size_exact_calculation(self):
        d = {"g": 0, "C": 1, "i": [["f0:all", "CUT"]]}
        s = P._size(d)
        expected_s = len(P.canon.dumps(d).encode("utf-8")) - 1
        self.assertEqual(s, expected_s)

    def test_size_negative_non_serializable(self):
        d = {"unsupported": object()}
        with self.assertRaises(TypeError):
            P._size(d)


class TestFrameIds(unittest.TestCase):
    def test_frame_ids_explicit_and_legacy_dense(self):
        # 1. GOP row with explicit frame_ids
        g_explicit = {"f0": 10, "n_frames": 11, "frame_ids": [10, 12, 15, 20]}
        self.assertEqual(P._frame_ids(g_explicit), [10, 12, 15, 20])

        # 2. Older dense GOP row without frame_ids
        g_dense = {"f0": 5, "n_frames": 4}
        self.assertEqual(P._frame_ids(g_dense), [5, 6, 7, 8])

    def test_frame_ids_negative_missing_keys(self):
        g_bad = {"f0": 5}
        with self.assertRaises(KeyError):
            P._frame_ids(g_bad)


if __name__ == "__main__":
    unittest.main()
