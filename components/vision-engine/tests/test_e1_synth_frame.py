"""E1 C3: RCORE-10 frame manifest render_index (I14)."""
import copy
import unittest

from vadapt.synth import synthetic as S
from visioncore import frame
from visioncore.errors import VqaError


def manifest():
    fr, _, _ = S.render_scenario("clean_static", 1, 256, 160, 16)
    return S.build_manifest(fr, "clean_static", 1)


class TestRenderIndex(unittest.TestCase):
    def test_valid_manifest(self):
        frame.validate_manifest(manifest())

    def test_all_absent_is_synth_exemption(self):
        m = manifest()
        for f in m["frames"]:
            del f["render_index"]
        frame.validate_manifest(m)

    def test_sparse_render_index_legal(self):
        m = manifest()
        for k, f in enumerate(m["frames"]):
            f["render_index"] = 5 * k
        frame.validate_manifest(m)

    def test_partial_render_index_rejected(self):
        m = manifest()
        del m["frames"][3]["render_index"]
        with self.assertRaises(VqaError) as c:
            frame.validate_manifest(m)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

    def test_bad_render_index_rejected(self):
        for bad in (-1, 1 << 32, "3", True):
            m = copy.deepcopy(manifest())
            m["frames"][2]["render_index"] = bad
            with self.assertRaises(VqaError, msg=repr(bad)):
                frame.validate_manifest(m)


if __name__ == "__main__":
    unittest.main()
