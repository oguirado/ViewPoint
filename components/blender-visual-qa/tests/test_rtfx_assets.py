"""RTFX lane R: fresh asset-intent queries (NOT the frozen router bench split) route to the right bv.assets.* nodes,
and the new asset nodes do not steal off-domain queries."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rtfx_route_code", ROOT / "tools" / "route_code.py")
route_code = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(route_code)
CAT = str(ROOT / "catalogue" / "router")

CASES = [
    ("check a glb file for pivot offset and scale problems", "bv.assets.inspect_gate."),
    ("bake node transforms and put the pivot on the ground", "bv.assets.normalize."),
    ("is the mesh manifold and are the uv islands overlapping", "bv.assets.mesh_gate."),
    ("render a turntable of the asset with truth passes", "bv.assets.turntable."),
    ("run an asset stage from the command line", "bv.assets.cli.main"),
]


class AssetIntents(unittest.TestCase):
    def test_five_asset_queries_hit_asset_nodes_in_top3(self):
        for q, prefix in CASES:
            res = route_code.route(q, 5, CAT)
            self.assertFalse(res["abstain"]["flag"], q)
            top3 = [r[0] for r in res["r"]][:3]
            self.assertTrue(any(a.startswith(prefix) for a in top3), (q, top3))

    def test_off_domain_queries_still_abstain(self):
        for q in ("quantum teleportation protocol", "best pizza recipe in naples", "wire protocol for a chat server"):
            self.assertTrue(route_code.route(q, 5, CAT)["abstain"]["flag"], q)


if __name__ == "__main__":
    unittest.main()
