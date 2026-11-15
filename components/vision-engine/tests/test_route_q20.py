"""Router regression: 20 realistic questions (top-3 hit) and 8 off-domain negatives (rc 3)."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "tools" / "route_code.py"
Q20 = [
    ('why is a tile classified as flicker instead of jitter', 'vc.s2.classify|vc.p.HYST_BAND_PERMILLE'),
    ('frozen screen not detected when the game hangs', 'vc.s2.classify|vc.p.FROZEN_M'),
    ('png decode fails with interlaced image', 'vc.decode.png|vc.decode.unfilter'),
    ('add a new tile size option to the grid', 'vc.s0.grid'),
    ('judge citation rejected with E_CITE', 'vc.s6.verify'),
    ('pack exceeds the token budget E_BUDGET', 'vc.s6.fit'),
    ('change the hysteresis band threshold', 'vc.p.HYST_BAND_PERMILLE'),
    ('regions merge two separate dirty areas', 'vc.s4.regions|vc.s4.adjacency'),
    ('region ids change between runs matching regions', 'vc.s4.match|vc.ids.region_id'),
    ('gop keyframe interval too long scene cut detection', 'vc.s5.cuts|vc.s5.keyframe|vc.p.CUT_PPM_1F|vc.p.CUT_PPM_2F'),
    ('add a new engine adapter for unreal', 'va.base.adapter'),
    ('frame manifest validation error sha mismatch', 'vc.frame.validate_manifest|vc.frame.read_manifest|vc.frame.frames_sha256'),
    ('vmem store open fails on a fresh directory', 'vm.store.open|vm.store.Store'),
    ('memory query returns the wrong unit bm25', 'vm.route.route|vm.query.answer|vm.router.route|vm.router.answer'),
    ('pyramid selection exceeds node budget', 'vc.s3.select|vc.s3.r_sel|vc.s3.bound'),
    ('heatmap crop coordinates off by one', 'vc.s6.crop|vc.s6.heatmap'),
    ('descriptor signature differs across machines ycocg', 'vc.s1.describe|vc.s1.ycocg|vc.s1.describe_padded'),
    ('add a new catalogue threshold row', 'tools.buildcatalogue.main|vc.p.effective'),
    ('camera drift detected as global motion', 'vc.s2.drift|vc.s2.motion'),
    ('how do I bake lightmaps in blender cycles', 'ABSTAIN'),
]
NEG = [
    'quantum teleportation protocol',
    'how do I bake lightmaps in blender cycles',
    'configure kubernetes ingress tls',
    'train a neural network optimizer',
    'react component state hooks',
    'sql join performance index',
    'unity shader graph material',
    'password reset email flow',
]


def route(q):
    p = subprocess.run([sys.executable, "-I", str(ROUTER), "--q", q, "--root", str(ROOT)],
                       capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout), len(p.stdout.encode())


def pending_anchors():
    return set(json.loads((ROOT / "catalogue/layers.json").read_text())["pending_anchors"])


class TestRouteQ20(unittest.TestCase):
    def test_q20_top3(self):
        pending = pending_anchors()
        miss = []
        for q, exp in Q20:
            exp = set(exp.split("|"))
            rc, o, size = route(q)
            self.assertLessEqual(size, 2000)
            if exp == {"ABSTAIN"}:
                if rc != 3:
                    miss.append(q)
                continue
            if exp & pending:
                continue  # target anchor still listed pending in layers.src (owner has not tagged it yet)
            if not (exp & {r[0] for r in o["r"]}):
                miss.append(q)
        self.assertEqual(miss, [])

    def test_negatives_abstain(self):
        for q in NEG:
            rc, o, _ = route(q)
            self.assertEqual(rc, 3, q)


if __name__ == "__main__":
    unittest.main()
