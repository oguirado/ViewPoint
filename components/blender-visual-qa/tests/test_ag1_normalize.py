"""AG1 L2: asset normalize (world TRS bake, scale, bottom-centre pivot). Fixture GLBs are built in-test."""
import json
import os
import struct
import tempfile
import unittest

from blender_visual_qa.assets import glb_core, normalize

CUBE = [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
        (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)]
CUBE_IDX = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5]


def pad4(b, fill=b"\0"):
    return b + fill * (-len(b) % 4)


def build_glb(nodes, mesh_names=("crate",), skinned=False):
    """nodes: list of dicts of glTF node keys (mesh index refs into mesh_names)."""
    pos = b"".join(struct.pack("<3f", *v) for v in CUBE)
    idx = b"".join(struct.pack("<H", i) for i in CUBE_IDX)
    blob = pad4(pos) + pad4(idx)
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
             {"buffer": 0, "byteOffset": len(pad4(pos)), "byteLength": len(idx)}]
    acc = [{"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3",
            "min": [-0.5] * 3, "max": [0.5] * 3},
           {"bufferView": 1, "componentType": 5123, "count": len(CUBE_IDX), "type": "SCALAR"}]
    attrs = {"POSITION": 0}
    doc = {"asset": {"version": "2.0"}, "scene": 0}
    if skinned:
        j = pad4(b"".join(struct.pack("<4B", 0, 0, 0, 0) for _ in CUBE))
        w = b"".join(struct.pack("<4f", 1.0, 0, 0, 0) for _ in CUBE)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(j)})
        blob += j
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(w)})
        blob += w
        acc.append({"bufferView": 2, "componentType": 5121, "count": 8, "type": "VEC4"})
        acc.append({"bufferView": 3, "componentType": 5126, "count": 8, "type": "VEC4"})
        attrs.update({"JOINTS_0": 2, "WEIGHTS_0": 3})
    doc["meshes"] = [{"name": n, "primitives": [{"attributes": attrs, "indices": 1, "mode": 4}]} for n in mesh_names]
    doc["accessors"], doc["bufferViews"] = acc, views
    doc["buffers"] = [{"byteLength": len(blob)}]
    doc["nodes"] = list(nodes)
    roots = [i for i in range(len(nodes))]
    if skinned:
        doc["nodes"].append({"name": "joint"})
        doc["skins"] = [{"joints": [len(nodes)]}]
        roots.append(len(nodes))
    doc["scenes"] = [{"nodes": roots}]
    js = pad4(json.dumps(doc, separators=(",", ":")).encode(), b" ")
    body = struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(blob), 0x004E4942) + blob
    return struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


def crate(extra=None):
    """Raw crate: one mesh instanced twice, offset pivot, 1.7x scale."""
    a = {"name": "a", "mesh": 0, "translation": [0, 5, 0], "scale": [1.7, 1.7, 1.7]}
    b = {"name": "b", "mesh": 0, "translation": [3, 5, 0], "scale": [1.7, 1.7, 1.7]}
    nodes = [a, b]
    if extra:
        nodes = extra
    return build_glb(nodes)


def pivot_of(scene):
    return normalize.measure(scene)


class TestNormalize(unittest.TestCase):
    def test_bake_ground_centre_and_clone(self):
        out, rec = normalize.run_bytes(crate())
        self.assertEqual(rec["rc"], 0)
        sc = glb_core.parse_glb(out)
        self.assertEqual(len(sc.prims), 2)
        for p in sc.prims:
            self.assertEqual(tuple(p.world_q16), normalize.IDENTITY_Q16)
        m = pivot_of(sc)
        self.assertLessEqual(abs(m["pivot_y_um"]), normalize.ASSET_PIVOT_TOL_UM)
        self.assertLessEqual(abs(m["centre_x_um"]), normalize.ASSET_PIVOT_TOL_UM)
        # two instances 3 m apart stay 3 m apart; cube 1.7 m tall: dims x = 3+1.7 m
        self.assertAlmostEqual(m["dims_um"][1], 1700000, delta=4)
        self.assertAlmostEqual(m["dims_um"][0], 4700000, delta=4)
        # the two instances are distinct geometry (cloned, not aliased)
        xs = [min(q[0] for q in p.positions_um) for p in sc.prims]
        self.assertNotEqual(xs[0], xs[1])

    def test_target_height(self):
        out, rec = normalize.run_bytes(crate(), target_height_um=1000000)
        self.assertAlmostEqual(rec["dims_um"][1], 1000000, delta=4)
        self.assertAlmostEqual(rec["k_ppm"], 588235, delta=2)

    def test_idempotent(self):
        out, _ = normalize.run_bytes(crate())
        out2, rec2 = normalize.run_bytes(out)
        self.assertEqual(rec2["k_ppm"], normalize.PPM)
        self.assertEqual(glb_core.sha256(out), glb_core.sha256(out2))

    def test_reflection_rc5(self):
        bad = build_glb([{"name": "m", "mesh": 0, "scale": [-1, 1, 1]}])
        with self.assertRaises(normalize.NormalizeError) as cm:
            normalize.run_bytes(bad)
        self.assertEqual(cm.exception.rc, 5)

    def test_skinned_rc3_unless_drop(self):
        sk = build_glb([{"name": "s", "mesh": 0, "skin": 0, "translation": [0, 2, 0]}], skinned=True)
        with self.assertRaises(normalize.NormalizeError) as cm:
            normalize.run_bytes(sk)
        self.assertEqual(cm.exception.rc, 3)
        out, rec = normalize.run_bytes(sk, drop_animations=True)
        self.assertEqual(rec["rc"], 0)
        self.assertFalse(any(p.skinned for p in glb_core.parse_glb(out).prims))

    def test_empty_rc3(self):
        empty = build_glb([{"name": "n"}])
        with self.assertRaises(normalize.NormalizeError) as cm:
            normalize.run_bytes(empty)
        self.assertEqual(cm.exception.rc, 3)

    def test_cli_rc_and_receipt_chain(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "raw.glb"), os.path.join(d, "n.glb")
            with open(src, "wb") as fh:
                fh.write(crate())
            self.assertEqual(normalize.main(["--in", src, "--out", dst, "--prev-receipt-sha256", "ab" * 32]), 0)
            self.assertTrue(os.path.exists(dst))
            self.assertEqual(normalize.main(["--in", os.path.join(d, "missing.glb")]), 2)
            self.assertEqual(normalize.main(["--bogus"]), 2)
            with open(src, "wb") as fh:
                fh.write(b"not a glb")
            self.assertEqual(normalize.main(["--in", src]), 2)

    # ---- mutants: each must be killed (rc != 0 / flipped receipt) ----
    def _run_mutant(self, attr, replacement):
        orig = getattr(normalize, attr)
        setattr(normalize, attr, replacement)
        try:
            return normalize.run_bytes(crate())
        finally:
            setattr(normalize, attr, orig)

    def test_mutant_skip_grounding(self):
        with self.assertRaises(normalize.NormalizeError) as cm:
            self._run_mutant("ground", lambda prims: None)
        self.assertEqual(cm.exception.rc, 5)

    def test_mutant_scale_applied_twice(self):
        orig = normalize.apply_scale
        def twice(prims, k):
            orig(prims, k)
            orig(prims, k)
        _, rec_ok = normalize.run_bytes(crate(), target_height_um=1000000)
        setattr(normalize, "apply_scale", twice)
        try:
            _, rec_bad = normalize.run_bytes(crate(), target_height_um=1000000)
        finally:
            setattr(normalize, "apply_scale", orig)
        self.assertNotEqual(rec_ok["dims_um"], rec_bad["dims_um"])
        self.assertNotAlmostEqual(rec_bad["dims_um"][1], 1000000, delta=1000)

    def test_mutant_instance_not_cloned(self):
        real = normalize.bake_prim
        first = []

        def aliasing(prim):  # second instance reuses the first one's baked vertices
            if not first:
                first.append(real(prim))
            return first[0]
        setattr(normalize, "bake_prim", aliasing)
        try:
            with self.assertRaises(normalize.NormalizeError) as cm:
                normalize.run_bytes(crate())
        finally:
            setattr(normalize, "bake_prim", real)
        self.assertEqual(cm.exception.rc, 5)
        clean, _ = normalize.run_bytes(crate())
        cx = [min(q[0] for q in p.positions_um) for p in glb_core.parse_glb(clean).prims]
        self.assertNotEqual(cx[0], cx[1])


if __name__ == "__main__":
    unittest.main()
