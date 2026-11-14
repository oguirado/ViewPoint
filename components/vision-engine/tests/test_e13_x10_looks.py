"""FX7 lane O: X10 uses the real S7 look producer (JC3-1), FG/FAIL-episode 100 KB budget (ADV-S1-3), floor wording (JC3-3),
KNOWN_LIMITATIONS KL-4 current status (JC3-4). Each test fails on BASE_E ba96ac9."""
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from eval import x10_compaction as x10
from visioncore import canon, pipeline
from visioncore.errors import VqaError
from vmem import ingest as I
from vmem.store import Store

ROOT = Path(__file__).resolve().parent.parent
SPEC_VMEM = Path(os.environ.get("VE_SPEC_VMEM", "spec/SPEC_VMEM.md"))
_VR = {}


def vr720():
    if "v" not in _VR:
        _VR["v"] = x10._analyze_synth("clean_static", 1, 1280, 720, 20)
    return _VR["v"]


def write_spool(tmp, lines):
    sp = os.path.join(tmp, "spool.jsonl")
    with open(sp, "w") as f:
        f.write("".join(canon.dumps(l) for l in lines))
    return sp


class TestLookProducer(unittest.TestCase):
    def test_macroblock_looks_cover_every_tile_once(self):
        vr = vr720()
        lines = x10.build_spool(vr, hints=vr.x10_hints)
        tile = [l["body"] for l in lines if l["type"] == "look" and l["body"]["role"] == "tile"]
        self.assertEqual(len(tile), 60)  # 80x45 grid: 10 x 6 macroblocks (SPEC_VMEM §3.4, JC3-1: not 57)
        self.assertEqual(sum(b["look"]["n"] for b in tile), vr.grid.N)
        self.assertEqual(len({b["m0"] >> 6 for b in tile}), 60)
        self.assertTrue(all(b["m0"] & 63 == 0 for b in tile))
        self.assertTrue(x10.look_coverage(lines, vr.grid.N))
        self.assertFalse([l for l in lines if l["type"] == "region" and l["body"]["look"]["n"] == 1])

    def test_hint_looks_emitted(self):
        vr = vr720()
        hint = [l["body"] for l in x10.build_spool(vr, hints=vr.x10_hints)
                if l["type"] == "look" and l["body"]["role"] != "tile"]
        self.assertEqual([(b["role"], b["m0"], b["look"]["n"]) for b in hint], [("ui", 0, 64), ("ui", 64, 64)])

    def test_coverage_gap_refused(self):
        vr = vr720()
        real = x10.anchor_looks
        with mock.patch.object(x10, "anchor_looks", lambda v, h=(): real(v, h)[1:]):
            with self.assertRaises(VqaError) as c:
                x10.build_spool(vr, hints=vr.x10_hints)
        self.assertEqual(c.exception.detail["reason"], "look_coverage")

    def test_ingest_hint_and_macroblock_sharing_m0_keep_own_tk(self):
        vr = vr720()
        with tempfile.TemporaryDirectory() as tmp:
            root = x10._ingest_run(vr, tmp)
            active, _ = I._load(Store(root))
        gold = [u for u in active if u["kind"] == "baseline" and u["look"]]
        self.assertEqual(sum(1 for u in gold if ":tile:" in u["tk"]), 60)
        self.assertEqual(sum(u["look"]["n"] for u in gold if ":tile:" in u["tk"]), vr.grid.N)
        self.assertEqual(sum(1 for u in gold if ":ui:" in u["tk"]), 2)

    def test_measure_reports_full_workload(self):
        m = x10.measure_clean_l2("clean_static", 1280, 720, 20)
        self.assertEqual((m["info"]["looks_tile"], m["info"]["looks_hint"]), (60, 2))
        self.assertGreaterEqual(m["info"]["n_look"], 3600)


class TestFailEpisodeBudget(unittest.TestCase):
    def _spool(self, tmp, nbytes):
        vr = x10._analyze_synth("clean_static", 1, 256, 160, 16)
        lines = x10.build_spool(vr, hints=vr.x10_hints)
        blob = bytes(np.random.default_rng(7).integers(0, 256, nbytes, dtype=np.uint8))
        for l in lines:
            if l["type"] == "episode":
                l["body"].update(verdicts=[{"verdict": "FAIL"}], gop_blob_hex=blob.hex(),
                                 gop_blob_sha256=canon.sha256_bytes(blob))
        return write_spool(tmp, lines)

    def test_over_100kb_rc6_store_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            sp, root = self._spool(tmp, I.FG_EPISODE_MAX_B + 1), os.path.join(tmp, "store")
            with self.assertRaises(VqaError) as c:
                I.ingest(root, sp)
            self.assertEqual((c.exception.code, c.exception.rc, c.exception.detail["reason"]),
                             ("E_BUDGET", 6, "fg_episode_budget"))
            self.assertIsNone(Store(root).current())
            self.assertTrue(os.path.exists(sp))

    def test_at_limit_accepted(self):
        self.assertEqual(I.FG_EPISODE_MAX_B, 102400)
        with tempfile.TemporaryDirectory() as tmp:
            sp, root = self._spool(tmp, I.FG_EPISODE_MAX_B), os.path.join(tmp, "store")
            self.assertEqual(I.ingest(root, sp)["gen"], 1)


class TestFloorWording(unittest.TestCase):
    def test_counterexample_capture_beats_claimed_floor(self):
        """JC3-3 regression (judge build/room_JC3/review_floor.py): 3,600 distinct xh64, one sig64 -> GOP < 16*N_distinct."""
        from vadapt.synth import synthetic as S
        rng = np.random.default_rng(31)
        pat = np.array([[100, 100, 101, 101], [100, 101, 100, 101], [100, 101, 101, 100],
                        [101, 100, 100, 101], [101, 100, 101, 100], [101, 101, 100, 100]], dtype=np.uint8)
        y = pat[rng.integers(0, 6, (360, 640))].reshape(360, 640, 2, 2).transpose(0, 2, 1, 3).reshape(720, 1280)
        frames = [np.repeat(y[..., None], 3, axis=2) for _ in range(16)]
        run = pipeline.analyze(S.SynthSource(frames, S.build_manifest(frames, "clean_static", 31)))
        t = run.tiles[0]
        distinct = len(set(int(v) for v in t["xh64"]))
        self.assertEqual((distinct, len(set(int(v) for v in t["sig64"]))), (3600, 1))
        self.assertLess(len(run.gop_files["gops.bin"]), 16 * distinct)

    @unittest.skipUnless(SPEC_VMEM.exists(), "campaign SPEC_VMEM absent")
    def test_spec_floor_not_proved(self):
        row = [l for l in SPEC_VMEM.read_text().splitlines() if "16·N_distinct" in l]
        self.assertTrue(row)
        self.assertFalse([l for l in row if "| PROVED" in l])
        self.assertTrue(all("ASSUMED" in l for l in row))
        self.assertNotIn("≤ 57 tile looks", SPEC_VMEM.read_text())


class TestKnownLimitationsKL4(unittest.TestCase):
    def test_kl4_historical_and_current_status_consistent(self):
        text = (ROOT / "KNOWN_LIMITATIONS.md").read_text()
        kl4 = [l for l in text.splitlines() if l.startswith("| KL-4 ")][0]
        self.assertIn("HISTORICAL", kl4)
        cur = [l for l in text.splitlines() if l.startswith("KL-4 status")]
        self.assertTrue(cur)
        got = re.search(r"clean_static ([0-9,]+) B before / ([0-9,]+) B after", cur[0])
        self.assertIsNotNone(got)
        nums = [int(x.replace(",", "")) for x in got.groups()]
        info = {"resolution": "1280x720", "frames": 200, "n_look": 3728, "n_lu": 62, "n_look_before": 3728,
                "n_lu_before": 62, "window_ok": True}
        m = {"scenario": "clean_static", "before_bytes": nums[0], "after_bytes": nums[1], "info": info}
        self.assertIn("RESOLVED by AMD-L2-3", cur[0])  # operator option 1; status text agrees with the gate verdict
        self.assertTrue(x10.clean_storage_verdict([m], ("clean_static",)))
        self.assertFalse(x10.clean_storage_verdict([dict(m, after_bytes=598017)], ("clean_static",)))


if __name__ == "__main__":
    unittest.main()
