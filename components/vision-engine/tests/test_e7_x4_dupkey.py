import unittest

from eval import x4_fp as X
from visioncore.errors import VqaError

TINY = dict(W=256, H=160, n=16)


def tiny_rows(controls, seeds):
    return [X.run_trial(c, s, **TINY) for c in controls for s in seeds]


def tiny_cfg():
    return X._sha(X.config_doc(**TINY))


class X4DuplicateAcrossControls(unittest.TestCase):
    def leak(self, rows, why):
        with self.assertRaises(VqaError) as cm:
            X.check_binding(rows, config=tiny_cfg())
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_GATE", 5))
        self.assertEqual(cm.exception.detail["reason"], "split_leak")
        self.assertEqual(cm.exception.detail["why"], why)

    def test_controls_sharing_seeds_bind_cleanly(self):
        # real run: every control reuses seed numbers 0..29; seeds 1,2 and 4,5 used to render the same capture
        rows = tiny_rows(("clean_static", "gpu_jitter", "shader_pulse"), range(6))
        X.check_binding(rows, config=tiny_cfg())
        self.assertEqual(len({r["capture_sha256"] for r in rows}), len(rows))

    def test_seeds_render_distinct_captures_per_control(self):
        shas = {X.run_trial("clean_static", s, **TINY)["capture_sha256"] for s in range(X.TEST_SEEDS)}
        self.assertEqual(len(shas), X.TEST_SEEDS)

    def test_genuine_duplicate_row_still_refused(self):
        rows = tiny_rows(("clean_static", "iid_noise"), range(2))
        self.leak(rows + [dict(rows[0])], "duplicate_row")

    def test_relabelled_capture_still_refused(self):
        rows = tiny_rows(("clean_static", "iid_noise"), range(2))
        rows[3] = dict(rows[3], capture_sha256=rows[0]["capture_sha256"])
        self.leak(rows, "duplicate_row")


if __name__ == "__main__":
    unittest.main()
