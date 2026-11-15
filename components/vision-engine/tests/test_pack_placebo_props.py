import random
import unittest
import numpy as np

from visioncore import ids, layout, pack
from visioncore.desc import TILE_DTYPE
from visioncore.errors import VqaError


class TestPackPlaceboProps(unittest.TestCase):

    def test_shift_tile_properties(self):
        """Fixed-seed property tests for _shift_tile."""
        for seed in range(10):
            rng = random.Random(seed)
            gw = rng.randint(1, 9)
            gh = rng.randint(1, 9)
            sx = rng.randint(0, 20)
            sy = rng.randint(0, 20)

            # Test all tiles in grid
            for tx in range(gw):
                for ty in range(gh):
                    m = layout.morton(tx, ty)

                    # Roundtrip property: shift (sx, sy) then (gw - sx%gw, gh - sy%gh)
                    m_shifted = pack._shift_tile(m, sx, sy, gw, gh)
                    inv_sx = (gw - (sx % gw)) % gw
                    inv_sy = (gh - (sy % gh)) % gh
                    m_roundtrip = pack._shift_tile(m_shifted, inv_sx, inv_sy, gw, gh)
                    self.assertEqual(m_roundtrip, m)

                    # Zero shift property
                    m_zero = pack._shift_tile(m, 0, 0, gw, gh)
                    self.assertEqual(m_zero, m)

    def test_shift_tile_negative(self):
        """Negative test cases for _shift_tile."""
        # Non-integer morton parameter causing TypeError in unmorton
        with self.assertRaises(TypeError):
            pack._shift_tile("invalid", 0, 0, 5, 5)

        # Invalid morton range (< 0)
        with self.assertRaises(VqaError) as cm:
            pack._shift_tile(-1, 0, 0, 5, 5)
        self.assertEqual(cm.exception.code, "E_VC_ID_RANGE")

    def test_shift_id_properties(self):
        """Fixed-seed property tests for _shift_id."""
        for seed in range(10):
            rng = random.Random(seed)
            gw = rng.randint(1, 9)
            gh = rng.randint(1, 9)
            sx = rng.randint(0, 20)
            sy = rng.randint(0, 20)

            frame = rng.randint(0, 100)
            tx = rng.randint(0, gw - 1)
            ty = rng.randint(0, gh - 1)
            m = layout.morton(tx, ty)

            rid = ids.region_id(frame, m)
            kid = ids.track_id(frame, m)

            for item_id in (rid, kid):
                # Roundtrip identity property
                inv_sx = (gw - (sx % gw)) % gw
                inv_sy = (gh - (sy % gh)) % gh
                s1 = pack._shift_id(item_id, sx, sy, gw, gh)
                s2 = pack._shift_id(s1, inv_sx, inv_sy, gw, gh)
                self.assertEqual(s2, item_id)

                # Zero shift property
                s0 = pack._shift_id(item_id, 0, 0, gw, gh)
                self.assertEqual(s0, item_id)

                # Preserves frame number and kind part
                kind_orig, d_orig = ids.parse_any(item_id)
                kind_shift, d_shift = ids.parse_any(s1)
                self.assertEqual(kind_orig, kind_shift)
                self.assertEqual(d_orig["frame"], d_shift["frame"])
                self.assertEqual(item_id[0], s1[0])

    def test_shift_id_negative(self):
        """Negative test cases for _shift_id."""
        # Syntax error on invalid string
        with self.assertRaises(VqaError) as cm:
            pack._shift_id("invalid_id", 0, 0, 5, 5)
        self.assertEqual(cm.exception.code, "E_VC_ID_SYNTAX")

        # Unsupported ID kind (e.g. global ID 'f0:all' has no morton coordinate)
        with self.assertRaises(KeyError):
            pack._shift_id("f0:all", 0, 0, 5, 5)

    def test_shift_box_properties(self):
        """Fixed-seed property tests for _shift_box."""
        s = 16  # tile pixel size
        for seed in range(10):
            rng = random.Random(seed)
            gw = rng.randint(1, 9)
            gh = rng.randint(1, 9)
            sx = rng.randint(0, 20)
            sy = rng.randint(0, 20)

            # Tile box (t=True)
            tx0 = rng.randint(0, gw - 1)
            ty0 = rng.randint(0, gh - 1)
            tw = rng.randint(1, 5)
            th = rng.randint(1, 5)
            tile_box = [tx0, ty0, tx0 + tw, ty0 + th]

            s_tile = pack._shift_box(tile_box, sx, sy, gw, gh, s, t=True)
            w_orig_t = tile_box[2] - tile_box[0]
            h_orig_t = tile_box[3] - tile_box[1]
            w_shift_t = s_tile[2] - s_tile[0]
            h_shift_t = s_tile[3] - s_tile[1]
            self.assertEqual(w_orig_t, w_shift_t)
            self.assertEqual(h_orig_t, h_shift_t)

            # Zero shift property for tile box
            s_tile_zero = pack._shift_box(tile_box, 0, 0, gw, gh, s, t=True)
            self.assertEqual(s_tile_zero, tile_box)

            # Pixel box (t=False)
            px0 = rng.randint(0, gw * s - 1)
            py0 = rng.randint(0, gh * s - 1)
            pw = rng.randint(1, 50)
            ph = rng.randint(1, 50)
            pixel_box = [px0, py0, px0 + pw, py0 + ph]

            s_pixel = pack._shift_box(pixel_box, sx, sy, gw, gh, s, t=False)
            w_orig_p = pixel_box[2] - pixel_box[0]
            h_orig_p = pixel_box[3] - pixel_box[1]
            w_shift_p = s_pixel[2] - s_pixel[0]
            h_shift_p = s_pixel[3] - s_pixel[1]
            self.assertEqual(w_orig_p, w_shift_p)
            self.assertEqual(h_orig_p, h_shift_p)

            # Zero shift property for pixel box
            s_pixel_zero = pack._shift_box(pixel_box, 0, 0, gw, gh, s, t=False)
            self.assertEqual(s_pixel_zero, pixel_box)

    def test_shift_box_negative(self):
        """Negative test cases for _shift_box."""
        # Malformed box list (< 4 elements)
        with self.assertRaises(IndexError):
            pack._shift_box([0, 0], 0, 0, 5, 5, 16, True)

        # Zero tile stride 's' causing DivisionByZero when t=False
        with self.assertRaises(ZeroDivisionError):
            pack._shift_box([0, 0, 10, 10], 0, 0, 5, 5, s=0, t=False)

    def test_placebo_properties(self):
        """Fixed-seed property tests for _placebo."""
        s = 16
        for seed in range(10):
            rng = random.Random(seed)
            gw = rng.randint(1, 9)
            gh = rng.randint(1, 9)
            sx = rng.randint(0, 20)
            sy = rng.randint(0, 20)

            frame = rng.randint(0, 10)
            tx = rng.randint(0, gw - 1)
            ty = rng.randint(0, gh - 1)
            m = layout.morton(tx, ty)

            rid = ids.region_id(frame, m)
            kid = ids.track_id(frame, m)

            region_dict = {
                rid: {
                    "id": rid,
                    "bbox_t": [tx, ty, tx + 1, ty + 1],
                    "bbox_px": [tx * s, ty * s, (tx + 1) * s, (ty + 1) * s],
                }
            }
            gops = [
                {
                    "gop": 0,
                    "tracks": [
                        {"k": kid, "r_rep": rid}
                    ],
                }
            ]

            new_gops, new_regions, back = pack._placebo(gops, region_dict, sx, sy, gw, gh, s)

            # Shifted gops keep their count and track count
            self.assertEqual(len(new_gops), len(gops))
            self.assertEqual(len(new_gops[0]["tracks"]), len(gops[0]["tracks"]))

            # Back map inverts the shifted region ids exactly
            for nid, orig_id in back.items():
                self.assertEqual(orig_id, rid)
                self.assertEqual(new_regions[nid]["id"], nid)
                # Verify that applying back map on shifted ID yields original ID
                self.assertIn(nid, new_regions)
                self.assertEqual(back[nid], rid)

            # Zero shift property for _placebo
            gops0, regions0, back0 = pack._placebo(gops, region_dict, 0, 0, gw, gh, s)
            self.assertEqual(gops0, gops)
            self.assertEqual(regions0, region_dict)
            self.assertEqual(back0, {rid: rid})

    def test_placebo_negative(self):
        """Negative test cases for _placebo."""
        # Non-iterable gops parameter
        with self.assertRaises(TypeError):
            pack._placebo(None, {}, 0, 0, 5, 5, 16)

        # Invalid region dict structure (e.g., string instead of dict/mapping)
        with self.assertRaises(AttributeError):
            pack._placebo([], "invalid_regions", 0, 0, 5, 5, 16)

    def test_roll_tiles_properties(self):
        """Fixed-seed property tests for _roll_tiles."""
        for seed in range(10):
            rng = random.Random(seed)
            gw = rng.randint(1, 9)
            gh = rng.randint(1, 9)
            sx = rng.randint(0, 20)
            sy = rng.randint(0, 20)

            n_tiles = gw * gh
            arr = np.zeros(n_tiles, dtype=TILE_DTYPE)
            for idx in range(n_tiles):
                tx = idx % gw
                ty = idx // gw
                arr["morton"][idx] = layout.morton(tx, ty)

            shift_dict = {"sx": sx, "sy": sy}
            inv_shift_dict = {"sx": (gw - (sx % gw)) % gw, "sy": (gh - (sy % gh)) % gh}

            rolled = pack._roll_tiles(arr, shift_dict, gw, gh)
            unrolled = pack._roll_tiles(rolled, inv_shift_dict, gw, gh)

            # Roundtrip identity property
            np.testing.assert_array_equal(unrolled["morton"], arr["morton"])

            # Zero shift property
            zero_rolled = pack._roll_tiles(arr, {"sx": 0, "sy": 0}, gw, gh)
            np.testing.assert_array_equal(zero_rolled["morton"], arr["morton"])

    def test_roll_tiles_negative(self):
        """Negative test cases for _roll_tiles."""
        # Array without 'morton' field
        arr_invalid = np.zeros(5, dtype=[("val", "i4")])
        with self.assertRaises(ValueError):
            pack._roll_tiles(arr_invalid, {"sx": 0, "sy": 0}, 5, 5)

        # Shift missing required key 'sx'
        arr = np.zeros(1, dtype=TILE_DTYPE)
        with self.assertRaises(KeyError):
            pack._roll_tiles(arr, {"sy": 0}, 5, 5)

    def test_regions_map_properties(self):
        """Property tests for _regions_map."""
        # Returns dict unchanged when input is dict
        d = {"r0:0000": {"id": "r0:0000"}}
        self.assertIs(pack._regions_map(d), d)

        # Converts list of dicts to dict indexed by 'id'
        lst = [{"id": "r0:0000", "data": 1}, {"id": "r0:0001", "data": 2}]
        mapped = pack._regions_map(lst)
        self.assertEqual(mapped, {"r0:0000": lst[0], "r0:0001": lst[1]})

        # None / empty input returns empty dict
        self.assertEqual(pack._regions_map(None), {})
        self.assertEqual(pack._regions_map([]), {})

    def test_regions_map_negative(self):
        """Negative test cases for _regions_map."""
        # Non-iterable non-dict input (e.g., integer)
        with self.assertRaises(TypeError):
            pack._regions_map(12345)

        # List elements missing 'id' key
        with self.assertRaises(KeyError):
            pack._regions_map([{"no_id": 1}])


if __name__ == "__main__":
    unittest.main()
