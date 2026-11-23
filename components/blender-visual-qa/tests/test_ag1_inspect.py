"""AG1 L1: glb_core frozen interface + inspect_gate (24 integer checks, matrix sha, named mutants)."""
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from blender_visual_qa.assets import glb_core as g
from blender_visual_qa.assets import inspect_gate as ig

S = 1000000
CELL = 16384
INSET = 1024
FACES = (((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)),
         ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)),
         ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)))
NORMALS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def cube_parts(origin=(-S // 2, 0, -S // 2), size=S, cell0=0, base=0):
    pos, idx, uvs, nrm = [], [], [], []
    for f, quad in enumerate(FACES):
        cell = cell0 + f
        cu, cv = (cell % 4) * CELL + INSET, (cell // 4) * CELL + INSET
        k = CELL - 2 * INSET
        n = len(pos) + base
        for c, uv in zip(quad, ((0, 0), (k, 0), (k, k), (0, k))):
            pos.append(tuple(origin[i] + c[i] * size for i in range(3)))
            uvs.append((cu + uv[0], cv + uv[1]))
            nrm.append(tuple(x * g.Q16 for x in NORMALS[f]))
        idx += [n, n + 1, n + 2, n, n + 2, n + 3]
    return pos, idx, uvs, nrm


def cube(name="crate", world=g.IDENTITY_Q16, **kw):
    pos, idx, uvs, nrm = cube_parts(**kw)
    return g.Prim(name, "/" + name, tuple(world), pos, idx, uvs, nrm, "wood")


def glb(*prims, **kw):
    return g.write_glb(g.Scene(list(prims)), **kw)


def translated(tx=0, ty=0, tz=0):
    w = list(g.IDENTITY_Q16)
    w[3], w[7], w[11] = tx, ty, tz
    return tuple(w)


def repack(data, edit):
    doc, binbuf = g._split(data)
    edit(doc)
    j = json.dumps(doc).encode()
    j += b" " * ((4 - len(j) % 4) % 4)
    body = struct.pack("<II", len(j), g.CHUNK_JSON) + j + struct.pack("<II", len(binbuf), g.CHUNK_BIN) + binbuf
    return g.GLB_MAGIC + struct.pack("<II", 2, 12 + len(body)) + body


class TestGlbCore(unittest.TestCase):
    def test_roundtrip_exact_and_deterministic(self):
        data = glb(cube())
        s = g.parse_glb(data)
        self.assertEqual(len(s.prims), 1)
        p = s.prims[0]
        ref = cube()
        self.assertEqual((p.positions_um, p.indices, p.uvs_q16, p.normals_q16, p.material, p.world_q16),
                         (ref.positions_um, ref.indices, ref.uvs_q16, ref.normals_q16, "wood", g.IDENTITY_Q16))
        self.assertEqual(g.write_glb(s), data)
        self.assertEqual(g.sha256(data), g.sha256(glb(cube())))
        self.assertRegex(g.sha256(data), r"^[0-9a-f]{64}$")

    def test_shared_instance_and_world_matrix(self):
        a, b = cube("a"), cube("b", world=translated(ty=3 * g.Q16))
        b.name = "b"
        s = g.parse_glb(glb(a, b, share_identical=True))
        self.assertEqual(s.prims[0].mesh_id, s.prims[1].mesh_id)
        self.assertEqual(s.prims[1].world_q16[7], 3 * g.Q16)
        self.assertEqual(min(p[1] for p in g.world_positions_um(s.prims[1])), 3 * S)

    def test_children_compose_in_world_space(self):
        def edit(doc):
            doc["nodes"][0]["children"] = [1]
            doc["nodes"][0]["translation"] = [2.0, 0.0, 0.0]
            doc["scenes"][0]["nodes"] = [0]
        s = g.parse_glb(repack(glb(cube("p"), cube("c", world=translated(ty=g.Q16))), edit))
        child = [p for p in s.prims if p.name == "c"][0]
        self.assertEqual(child.node_path, "/p/c")
        self.assertEqual((child.world_q16[3], child.world_q16[7]), (2 * g.Q16, g.Q16))

    def test_errors_are_coded(self):
        good = glb(cube())
        cases = {"E_IO": [b"nope" + good[4:], good[:-4], b""],
                 "E_SCHEMA": [repack(good, lambda d: d["meshes"][0]["primitives"][0]["attributes"].pop("POSITION")),
                              repack(good, lambda d: d["accessors"].__setitem__(
                                  d["meshes"][0]["primitives"][0]["indices"],
                                  dict(d["accessors"][d["meshes"][0]["primitives"][0]["indices"]], count=5))),
                              repack(good, lambda d: d["meshes"][0]["primitives"][0].__setitem__("mode", 1))]}
        for code, blobs in cases.items():
            for blob in blobs:
                with self.assertRaises(g.GlbError) as cm:
                    g.parse_glb(blob)
                self.assertEqual(cm.exception.code, code)
        nan = cube()
        nan.positions_um = [(math.nan, 0, 0)] + nan.positions_um[1:]
        with self.assertRaises(g.GlbError) as cm:
            g.parse_glb(glb(nan))
        self.assertEqual(cm.exception.code, "E_SCHEMA")
        with self.assertRaises(g.GlbError) as cm:
            g.read_glb("/nonexistent/x.glb")
        self.assertEqual(cm.exception.code, "E_IO")


class TestInspectGate(unittest.TestCase):
    def run_gate(self, data, cls="prop", **kw):
        return ig.run(data, cls, **kw)

    def test_baseline_cube_passes_24_checks(self):
        rc, r = self.run_gate(glb(cube()))
        self.assertEqual((rc, r["failed"], r["verdict"]), (0, [], "PASS"))
        self.assertEqual([c["id"] for c in r["checks"]], list(ig.CHECK_IDS))
        self.assertEqual(len(r["checks"]), 24)
        self.assertEqual(r["metrics"]["tris"], 12)
        self.assertEqual(r["matrix_sha256"], json.loads(ig.DEFAULT_MATRIX.read_text())["signed"]["body_sha256"])

    def test_mutant_flip_one_triangle_twin_winding(self):
        m = cube()
        m.indices[0], m.indices[1] = m.indices[1], m.indices[0]
        rc, r = self.run_gate(glb(m))
        self.assertEqual((rc, r["failed"]), (5, ["twin_winding"]))

    def test_mutant_pinched_bowtie_vertex(self):
        pa, ia, ua, na = cube_parts(origin=(-S, 0, -S))
        pb, ib, ub, nb = cube_parts(origin=(0, S, 0), cell0=6, base=len(pa))
        bow = g.Prim("bowtie", "/bowtie", g.IDENTITY_Q16, pa + pb, ia + ib, ua + ub, na + nb, "wood")
        rc, r = self.run_gate(glb(bow))
        self.assertEqual((rc, r["failed"]), (5, ["pinched_vertices"]))

    def test_mutant_negative_determinant_node(self):
        w = list(g.IDENTITY_Q16)
        w[0] = -g.Q16
        rc, r = self.run_gate(glb(cube(world=w)))
        self.assertEqual(rc, 5)
        self.assertIn("reflection", r["failed"])
        self.assertIn("orientation_outward", r["failed"])

    def test_mutant_matrix_sha_drift_rc4(self):
        m = json.loads(ig.DEFAULT_MATRIX.read_text())
        m["classes"]["prop"]["tris_max"] = 13
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps(m))
            rc, r = self.run_gate(glb(cube()), matrix_path=p)
            self.assertEqual((rc, r["verdict"]), (4, "STALE"))
            m["signed"]["body_sha256"] = ig.body_sha256(m)  # re-signed: accepted again
            p.write_text(json.dumps(m))
            self.assertEqual(self.run_gate(glb(cube()), matrix_path=p)[0], 0)
            del m["signed"]
            p.write_text(json.dumps(m))
            self.assertEqual(self.run_gate(glb(cube()), matrix_path=p)[0], 3)
            m["classes"]["prop"]["bogus"] = 1
            m["signed"] = {"by": "t", "body_sha256": ig.body_sha256(m)}
            p.write_text(json.dumps(m))
            self.assertEqual(self.run_gate(glb(cube()), matrix_path=p)[0], 2)
        self.assertEqual(self.run_gate(glb(cube()), expect_matrix_sha="0" * 64)[0], 4)

    def test_mutant_raw_crate_offset_pivot_and_unfrozen(self):
        rc, r = self.run_gate(glb(cube(world=translated(ty=g.Q16 // 2, tx=g.Q16))))
        self.assertEqual(rc, 5)
        self.assertEqual(sorted(r["failed"]), ["pivot_xz", "pivot_y", "transforms_frozen"])

    def test_mutant_world_walk_shortcut(self):
        # AUDIT F1: the defect sits in the SECOND node; a local-space / first-prim shortcut misses it.
        bad = cube("b", world=translated(ty=0))
        bad.indices[0], bad.indices[1] = bad.indices[1], bad.indices[0]
        bad.positions_um = [(x + 3 * S, y, z) for x, y, z in bad.positions_um]
        a = cube("a", origin=(-S // 2 - 3 * S, 0, -S // 2), cell0=6)
        rc, r = self.run_gate(glb(a, bad))
        self.assertEqual(rc, 5)
        self.assertIn("twin_winding", r["failed"])
        self.assertNotIn("twin_winding", self.run_gate(glb(a))[1]["failed"])  # a first-node-only walk misses it
        self.assertEqual(self.run_gate(glb(a, bad))[1]["metrics"]["nodes_with_mesh"], 2)
        raw = glb(cube(world=translated(ty=g.Q16 // 2)))
        with mock.patch.object(g, "world_positions_um", lambda p: list(p.positions_um)):
            self.assertNotIn("pivot_y", self.run_gate(raw)[1]["failed"])  # mutant survives only without world walk
        self.assertIn("pivot_y", self.run_gate(raw)[1]["failed"])

    def test_mutant_uv_overlap(self):
        m = cube()
        m.uvs_q16 = [m.uvs_q16[i % 4] for i in range(len(m.uvs_q16))]  # every face in the same cell
        rc, r = self.run_gate(glb(m))
        self.assertEqual((rc, r["failed"]), (5, ["uv_overlap"]))

    def test_instances_share_uv_layout_but_distinct_layouts_overlap(self):
        a = cube("a", origin=(-S - S // 2, 0, -S // 2))
        b = cube("b", origin=(S // 2, 0, -S // 2))
        self.assertEqual(self.run_gate(glb(a, b))[0], 0)  # baked clones: identical layout pooled once
        b.uvs_q16 = [(u, v + 2 * INSET) for u, v in b.uvs_q16]  # distinct layout, same material, overlapping
        rc, r = self.run_gate(glb(a, b))
        self.assertEqual((rc, r["failed"]), (5, ["uv_overlap"]))

    def test_rc_incomplete_and_usage(self):
        empty = g.Prim("e", "/e", g.IDENTITY_Q16, [(0, 0, 0)], [], None, None, None)
        self.assertEqual(self.run_gate(glb(empty))[0], 3)
        self.assertEqual(self.run_gate(b"garbage")[0], 2)
        self.assertEqual(self.run_gate(glb(cube()), cls="vehicle")[0], 2)
        self.assertEqual(self.run_gate(glb(cube()), cls="kit_piece")[1]["class"], "kit_tile")

    def test_receipt_chain_cli_and_determinism(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.glb").write_bytes(glb(cube()))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc1 = ig.main(["--in", str(d / "a.glb"), "--class", "prop", "--out", str(d / "r1.json")])
                rc2 = ig.main(["--in", str(d / "a.glb"), "--class", "prop", "--out", str(d / "r2.json"),
                               "--prev-receipt", str(d / "r1.json")])
                rc3 = ig.main(["--in", str(d / "a.glb"), "--class", "prop", "--out", str(d / "r3.json")])
            self.assertEqual((rc1, rc2, rc3), (0, 0, 0))
            r1, r2 = (d / "r1.json").read_bytes(), json.loads((d / "r2.json").read_text())
            self.assertEqual(r1, (d / "r3.json").read_bytes())
            self.assertEqual(r2["prev_receipt_sha256"], g.sha256(r1))
            self.assertEqual(r2["input_sha256"], g.sha256((d / "a.glb").read_bytes()))
            self.assertEqual(r2["schema"], "bvqa.asset-inspect/v1")
            self.assertEqual(r1.decode(), ig.canon(json.loads(r1)))


if __name__ == "__main__":
    unittest.main()
