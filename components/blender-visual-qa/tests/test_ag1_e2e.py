"""AG1 L5: `asset` command, receipt chain, limits/threshold registry and the raw-crate e2e chain."""
import contextlib
import csv
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

from blender_visual_qa import cli as top_cli
from blender_visual_qa.assets import cli as acli
from blender_visual_qa.assets import limits

ROOT = Path(__file__).resolve().parent.parent
HEX64 = set("0123456789abcdef")


def run_cli(fn, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(argv)
    lines = [s for s in buf.getvalue().splitlines() if s.strip()]
    return rc, json.loads(lines[-1]) if lines else None


def fake_stage(name, rc, receipt):
    mod = types.ModuleType("blender_visual_qa.assets." + name)
    mod.main = lambda argv=None: (print(json.dumps(receipt)), rc)[1]
    return mod


class TestAssetCli(unittest.TestCase):
    def setUp(self):
        self.saved = {k: v for k, v in sys.modules.items() if k.startswith("blender_visual_qa.assets.fake_")}
        self.saved_stages = dict(acli.STAGES)

    def tearDown(self):
        acli.STAGES.clear()
        acli.STAGES.update(self.saved_stages)
        for k in [k for k in sys.modules if k.startswith("blender_visual_qa.assets.fake_")]:
            del sys.modules[k]

    def install(self, stage, rc, receipt):
        name = "fake_" + stage.replace("-", "_")
        sys.modules["blender_visual_qa.assets." + name] = fake_stage(name, rc, receipt)
        acli.STAGES[stage] = name

    def test_usage_rc2(self):
        for argv in ([], ["nope"]):
            rc, rec = run_cli(acli.main, argv)
            self.assertEqual((rc, rec["code"]), (limits.RC_USAGE, "E_USAGE"))

    def test_missing_stage_rc3_not_crash(self):
        acli.STAGES["mesh-gate"] = "absent_stage_module"
        rc, rec = run_cli(acli.main, ["mesh-gate"])
        self.assertEqual(rc, limits.RC_INCOMPLETE)
        self.assertEqual(rec["stage_receipt"]["reason"], "stage_not_built")

    def test_domain_rc1_maps_to_5_and_unknown_rc_fails(self):
        for raw in (1, 7, 99):
            self.install("inspect", raw, {"status": "FAIL"})
            rc, rec = run_cli(acli.main, ["inspect"])
            self.assertEqual((rc, rec["rc"]), (limits.RC_FAIL, limits.RC_FAIL), raw)

    def test_chain_binds_prev_sha_and_detects_tamper(self):
        self.install("inspect", 0, {"input_sha256": "a" * 64, "status": "PASS"})
        self.install("normalize", 0, {"status": "PASS"})
        with tempfile.TemporaryDirectory() as d:
            chain = os.path.join(d, "chain.jsonl")
            glb = os.path.join(d, "x.glb")
            Path(glb).write_bytes(b"abc")
            r1 = run_cli(acli.main, ["inspect", "--chain", chain])[1]
            r2 = run_cli(acli.main, ["normalize", "--in", glb, "--chain", chain])[1]
            self.assertIsNone(r1["prev_receipt_sha256"])
            self.assertEqual(r2["prev_receipt_sha256"], r1["receipt_sha256"])
            self.assertEqual(r2["input_sha256"], hashlib.sha256(b"abc").hexdigest())
            self.assertEqual(r1["input_sha256"], "a" * 64)
            for r in (r1, r2):
                body = {k: v for k, v in r.items() if k != "receipt_sha256"}
                self.assertEqual(hashlib.sha256(acli.canon(body).encode()).hexdigest(), r["receipt_sha256"])
                self.assertEqual(set(r), {"schema", "stage", "rc", "input_sha256", "prev_receipt_sha256", "receipt_sha256", "stage_receipt"})
            # mutant: a tampered stage receipt no longer matches its sha
            body = {k: v for k, v in r1.items() if k != "receipt_sha256"}
            body["stage_receipt"] = {"status": "FORGED"}
            self.assertNotEqual(hashlib.sha256(acli.canon(body).encode()).hexdigest(), r1["receipt_sha256"])
            self.assertEqual(len(Path(chain).read_text().splitlines()), 2)

    def test_envelope_matches_schema_file(self):
        sch = json.loads((ROOT / "blender_visual_qa/schemas/asset-receipt.v1.schema.json").read_text())
        self.assertEqual(sch["properties"]["schema"]["const"], acli.SCHEMA)
        self.assertEqual(set(sch["required"]), {"schema", "stage", "rc", "input_sha256", "prev_receipt_sha256", "receipt_sha256", "stage_receipt"})
        self.assertEqual(set(sch["properties"]["stage"]["enum"]), set(acli.STAGES))
        self.install("turntable", 3, {"status": "PREVIEW"})
        rc, rec = run_cli(acli.main, ["turntable"])
        self.assertEqual(rc, limits.RC_INCOMPLETE)
        self.assertIn(rec["rc"], sch["properties"]["rc"]["enum"])
        try:
            import jsonschema
        except ImportError:
            return
        jsonschema.Draft202012Validator(sch).validate(rec)

    def test_top_level_asset_dispatch(self):
        self.install("inspect", 0, {"status": "PASS"})
        rc, rec = run_cli(top_cli.main, ["asset", "inspect"])
        self.assertEqual((rc, rec["stage"], rec["schema"]), (limits.RC_OK, "inspect", "bvqa.asset-receipt/v1"))

    def test_top_level_rejects_unknown_command(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = top_cli.main(["assets"])
        self.assertEqual(rc, limits.RC_USAGE)


class TestRegistry(unittest.TestCase):
    def rows(self):
        with (ROOT / "catalogue/thresholds.tsv").open() as f:
            return {r["id"]: r for r in csv.DictReader(f, delimiter="\t")}

    def test_seed_limits_registered_with_owner_and_status(self):
        want = {"ASSET_PIVOT_TOL_UM": 10, "ASSET_TURN_FRAMES": 72, "ASSET_TURN_STEP_MDEG": 5000, "ASSET_TURN_RES": 512, "ASSET_TURN_SPP": 64}
        rows = self.rows()
        for name, value in want.items():
            self.assertEqual(getattr(limits, name), value)
            self.assertEqual(int(rows[name]["value"]), value)
            self.assertEqual((rows[name]["owner"], rows[name]["status"]), ("BASSETS", "DESIGN_CHOICE"))
            self.assertIn("uncalibrated", rows[name]["source"])

    def test_every_limits_constant_has_a_row(self):
        rows = self.rows()
        for name in dir(limits):
            if name.startswith("ASSET_"):
                self.assertIn(name, rows)

    def test_route_subject_and_anchor(self):
        routes = (ROOT / "catalogue/routes.tsv").read_text().splitlines()
        self.assertIn("assets\tbv.assets.cli.main\tR2", routes)
        self.assertEqual(json.loads((ROOT / "catalogue/subjects.json").read_text())["assets"], ["bv.assets.cli.main"])
        symbols = [json.loads(s) for s in (ROOT / "catalogue/symbols.jsonl").read_text().splitlines()]
        hit = [s for s in symbols if s["anchor"] == "bv.assets.cli.main"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["path"], "blender_visual_qa/assets/cli.py")


# ---- programmatic raw crate (no binaries): offset pivot, shared mesh instance, 1.7x scale ----
FACES = (  # (corners CCW seen from outside, outward normal, uv cell)
    (((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)), (0, 0, 1), (0, 0)),
    (((1, 0, 0), (0, 0, 0), (0, 1, 0), (1, 1, 0)), (0, 0, -1), (1, 0)),
    (((1, 0, 1), (1, 0, 0), (1, 1, 0), (1, 1, 1)), (1, 0, 0), (2, 0)),
    (((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)), (-1, 0, 0), (0, 1)),
    (((0, 1, 1), (1, 1, 1), (1, 1, 0), (0, 1, 0)), (0, 1, 0), (1, 1)),
    (((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)), (0, -1, 0), (2, 1)),
)
QUAD_UV = ((0, 0), (1, 0), (1, 1), (0, 1))


def _pad(b):
    return b + b"\0" * (-len(b) % 4)


def build_crate(non_manifold=False):
    pos, nrm, uv, idx = [], [], [], []
    for corners, normal, (cu, cv) in FACES:
        base = len(pos)
        for c, (u, v) in zip(corners, QUAD_UV):
            pos.append(c)
            nrm.append(normal)
            uv.append(((cu * 3 + 0.2 + 2.6 * u) / 9.0, (cv * 3 + 0.2 + 2.6 * v) / 6.0))
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    if non_manifold:  # a third face on the cube edge (0,0,0)-(1,0,0): edge valence 3
        base = len(pos)
        pos += [(0, 0, 0), (1, 0, 0), (0.5, -1, 0)]
        nrm += [(0, 0, 1)] * 3
        uv += [(0.8, 0.9), (0.9, 0.9), (0.85, 0.95)]
        idx += [base, base + 1, base + 2]
    parts = [b"".join(struct.pack(fmt, *row) for row in rows) for fmt, rows in (("<3f", pos), ("<3f", nrm), ("<2f", uv))]
    parts.append(_pad(b"".join(struct.pack("<H", i) for i in idx)))
    offsets, blob = [], b""
    for part in parts:
        offsets.append(len(blob))
        blob += part
    doc = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "crate", "scale": [1.7, 1.7, 1.7], "translation": [0, 0.3, 0], "children": [1, 2]},
                  {"name": "a", "mesh": 0, "translation": [2, 0, 0]},
                  {"name": "b", "mesh": 0, "translation": [-2, 0, 0]}],
        "materials": [{"name": "m"}],
        "meshes": [{"name": "box", "primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0}]}],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [{"buffer": 0, "byteOffset": o, "byteLength": len(part)} for o, part in zip(offsets, parts)],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
                       "min": [min(p[i] for p in pos) for i in range(3)], "max": [max(p[i] for p in pos) for i in range(3)]},
                      {"bufferView": 1, "componentType": 5126, "count": len(nrm), "type": "VEC3"},
                      {"bufferView": 2, "componentType": 5126, "count": len(uv), "type": "VEC2"},
                      {"bufferView": 3, "componentType": 5123, "count": len(idx), "type": "SCALAR"}],
    }
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(blob), b"BIN\0") + blob)


class TestRawCrateChain(unittest.TestCase):
    """inspect (rc 5, pivot) -> normalize -> inspect rc 0 -> mesh-gate rc 0 [-> turntable], sha-chained."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.chain = str(self.d / "chain.jsonl")
        self.raw = self.d / "raw.glb"
        self.raw.write_bytes(build_crate())

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, *argv):
        return run_cli(acli.main, list(argv) + ["--chain", self.chain])

    def test_chain_raw_to_normalized(self):
        norm = str(self.d / "norm.glb")
        rc, r1 = self.step("inspect", "--in", str(self.raw), "--class", "prop")
        self.assertEqual(rc, limits.RC_FAIL)
        failed = r1["stage_receipt"]["failed"]
        self.assertIn("pivot_y", failed)
        self.assertIn("transforms_frozen", failed)
        rc, r2 = self.step("normalize", "--in", str(self.raw), "--out", norm)
        self.assertEqual(rc, limits.RC_OK)
        self.assertEqual(r2["stage_receipt"]["output_sha256"], hashlib.sha256(Path(norm).read_bytes()).hexdigest())
        rc, r3 = self.step("inspect", "--in", norm, "--class", "prop")
        self.assertEqual((rc, r3["stage_receipt"]["failed"]), (limits.RC_OK, []))
        rc, r4 = self.step("mesh-gate", "--mesh", norm)
        self.assertEqual(rc, limits.RC_OK)
        recs = [r1, r2, r3, r4]
        self.assertEqual(r3["input_sha256"], r2["stage_receipt"]["output_sha256"])
        self.assertEqual(r4["input_sha256"], r2["stage_receipt"]["output_sha256"])
        self.assertIsNone(r1["prev_receipt_sha256"])
        for prev, cur in zip(recs, recs[1:]):
            self.assertEqual(cur["prev_receipt_sha256"], prev["receipt_sha256"])
        # replaying the same chain file twice must not reuse the same link
        self.assertEqual(len(set(r["receipt_sha256"] for r in recs)), len(recs))

    def test_non_manifold_raw_stops_at_mesh_gate_without_render_dir(self):
        bad = self.d / "bad.glb"
        bad.write_bytes(build_crate(non_manifold=True))
        rc, rec = self.step("mesh-gate", "--mesh", str(bad))
        self.assertEqual(rc, limits.RC_FAIL)
        self.assertIn("edge_multiplicity", rec["stage_receipt"]["failed"])
        self.assertFalse((self.d / "render").exists())
        self.assertEqual([p.name for p in self.d.iterdir() if p.is_dir()], [])

    def test_mutant_unnormalized_crate_does_not_pass_inspect(self):
        # mutant: skipping normalize leaves the raw crate, which inspect must reject
        rc, rec = self.step("inspect", "--in", str(self.raw), "--class", "prop")
        self.assertEqual(rc, limits.RC_FAIL)


@unittest.skipUnless(os.environ.get("BVQA_BLENDER") == "1" and os.environ.get("BVQA_SLOW") == "1", "needs BVQA_BLENDER=1 and BVQA_SLOW=1")
class TestTurntableLeg(unittest.TestCase):
    def test_full_chain_with_turntable_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            chain = str(d / "chain.jsonl")
            raw = d / "raw.glb"
            raw.write_bytes(build_crate())
            norm = str(d / "norm.glb")
            steps = [(("inspect", "--in", str(raw), "--class", "prop"), limits.RC_FAIL),
                     (("normalize", "--in", str(raw), "--out", norm), limits.RC_OK),
                     (("inspect", "--in", norm, "--class", "prop"), limits.RC_OK),
                     (("mesh-gate", "--mesh", norm), limits.RC_OK),
                     (("turntable", "--glb", norm, "--out", str(d / "tt"), "--with-truth", "--vision", "shadow"), limits.RC_OK)]
            prev = None
            for argv, want in steps:
                rc, rec = run_cli(acli.main, list(argv) + ["--chain", chain])
                self.assertEqual(rc, want, (argv[0], rec["stage_receipt"]))
                self.assertEqual(rec["prev_receipt_sha256"], prev)
                prev = rec["receipt_sha256"]
            self.assertEqual(len(list((d / "tt" / "render").glob("*.png"))), limits.ASSET_TURN_FRAMES)


if __name__ == "__main__":
    unittest.main()
