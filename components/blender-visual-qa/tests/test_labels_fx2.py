"""BFX9d lane L: Argon advocate ADV_W9 labels items (bt.argon.01, bt.argon.03, ADV-W9-04 frame_meta coverage)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import labels as L  # noqa: E402
from tests.test_labels import frame, tile  # noqa: E402


def lab_of(tl, ob=1):
    tl = np.asarray(tl, np.uint8)
    return {"tl": tl.copy(), "flags": np.zeros(tl.size, np.uint8), "ob_major": np.full(tl.size, ob, np.uint16)}


class FrozenOverrideTests(unittest.TestCase):
    def test_frozen_override_only_static_tiles(self):
        """bt.argon.01: the frozen override touches only STATIC tiles of the named object; RIGID/OCCLUDE keep their tl."""
        st, rg, oc = L.TL_ID["STATIC"], L.TL_ID["RIGID"], L.TL_ID["OCCLUDE"]
        lab = lab_of([st, rg, st, oc])
        jr = {"temporal": [{"name": "fz", "kind": "frozen", "frames": [3, 9]}]}
        out = L.apply_temporal(lab, 4, jr, {"fz": 1})
        fz = L.TL_ID["FROZEN_T"]
        self.assertEqual(out["tl"].tolist(), [fz, rg, fz, oc])

    def test_frozen_override_outside_span_or_other_object_untouched(self):
        st = L.TL_ID["STATIC"]
        jr = {"temporal": [{"name": "fz", "kind": "frozen", "frames": [3, 9]}]}
        self.assertEqual(L.apply_temporal(lab_of([st, st]), 2, jr, {"fz": 1})["tl"].tolist(), [st, st])
        self.assertEqual(L.apply_temporal(lab_of([st, st], ob=2), 4, jr, {"fz": 1})["tl"].tolist(), [st, st])

    def test_flicker_override_ignores_static_condition(self):
        sw = L.TL_ID["SHADE_WEAK"]
        jr = {"temporal": [{"name": "fl", "kind": "flicker", "frames": [2, 9]}]}
        out = L.apply_temporal(lab_of([L.TL_ID["STATIC"], sw]), 4, jr, {"fl": 1})
        self.assertEqual(out["tl"].tolist(), [L.TL_ID["FLICKER_T"]] * 2)


class ShadeWeakBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.g = L.vgrid.Grid(96, 64)
        self.a = frame((16, 16, 64, 48))

    def _tl(self, r):
        lab = L.label_frame(self.a, self.a, self.g, anim_ob={1}, resid=np.full(self.g.N, r, np.int64),
                            t_enter=64, t_exit=48)
        return int(tile(self.g, lab, 2, 1)["tl"])

    def test_shade_weak_at_half_t_exit(self):
        """bt.argon.03: r == t_exit // 2 (24 at t_exit 48) is SHADE_WEAK (<=), r = 25 is SHADE_BAND."""
        self.assertEqual(self._tl(24), L.TL_ID["SHADE_WEAK"])
        self.assertEqual(self._tl(25), L.TL_ID["SHADE_BAND"])

    def test_shade_strong_at_twice_t_enter(self):
        self.assertEqual(self._tl(128), L.TL_ID["SHADE_STRONG"])
        self.assertEqual(self._tl(127), L.TL_ID["SHADE_BAND"])


class CoverageTests(unittest.TestCase):
    def test_temporal_target_tiles_count_as_coverage(self):
        """ADV-W9-04(2): in B4 the changing content is relabelled FLICKER_T/DRIFT_T; it must still count as coverage,
        otherwise every B4 frame lands in the <50k stratum. FROZEN_T (static by definition) is not coverage."""
        tl = np.zeros(100, np.uint8)
        tl[:10] = L.TL_ID["FLICKER_T"]
        tl[10:15] = L.TL_ID["DRIFT_T"]
        tl[15:20] = L.TL_ID["FROZEN_T"]
        tl[20:25] = L.TL_ID["RIGID"]
        self.assertEqual(L.frame_meta(3, {"tl": tl, "flags": np.zeros(100, np.uint8)})["cov_ppm"], 200000)

    def test_b4_shaped_sequence_has_coverage_after_temporal_override(self):
        g = L.vgrid.Grid(96, 64)
        a = frame((16, 16, 64, 48))
        jr = {"temporal": [{"name": "fl", "kind": "flicker", "frames": [1, 4]}],
              "animated": [{"frame": 1, "kind": "material", "name": "fl"}]}
        labs = L.label_sequence([a, a], g, journal=jr, name_to_index={"fl": 1}, f0=0,
                                resid=np.full((2, g.N), 300, np.int64))
        self.assertTrue((labs[1]["tl"] == L.TL_ID["FLICKER_T"]).any())
        self.assertGreater(L.frame_meta(1, labs[1])["cov_ppm"], 0)


if __name__ == "__main__":
    unittest.main()
