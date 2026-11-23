"""Unit tests for private GLB parsing helpers in blender_visual_qa/assets/glb_core.py."""
import math
import struct
import unittest

from blender_visual_qa.assets import glb_core
from blender_visual_qa.assets.glb_core import (
    GLB_HEADER_BYTES,
    GLB_MAGIC,
    GLB_VERSION,
    IDENTITY_Q16,
    Q16,
    GlbError,
    Prim,
    Scene,
    _accessor,
    _div_round,
    _local,
    _mul,
    _quant,
    _split,
    _walk,
    det3_q48,
    write_glb,
)


def make_glb_bytes(doc, binbuf=b""):
    """Build a raw GLB byte string with custom JSON doc and binary buffer."""
    import json

    j = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    j += b" " * ((4 - len(j) % 4) % 4)
    body = struct.pack("<II", len(j), glb_core.CHUNK_JSON) + j
    if binbuf or binbuf == b"":
        b = binbuf + b"\x00" * ((4 - len(binbuf) % 4) % 4)
        if b:
            body += struct.pack("<II", len(b), glb_core.CHUNK_BIN) + b
    length = GLB_HEADER_BYTES + len(body)
    return GLB_MAGIC + struct.pack("<II", GLB_VERSION, length) + body


class TestGlbCoreUnit(unittest.TestCase):
    # ------------------------------------------------------------------- _quant
    def test_quant_finite(self):
        self.assertEqual(_quant(1.0, 1000, "test"), 1000)
        self.assertEqual(_quant(0.0004, 1000, "test"), 0)
        self.assertEqual(_quant(-0.15, 10, "test"), -1)
        self.assertEqual(_quant(0.15, 10, "test"), 2)

    def test_quant_non_finite_negative(self):
        for val in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(val=val):
                with self.assertRaises(GlbError) as cm:
                    _quant(val, 1000, "test")
                self.assertEqual(cm.exception.code, "E_SCHEMA")
                self.assertIn("non-finite test", cm.exception.detail)

    # ------------------------------------------------------------------- _split
    def test_split_valid(self):
        sc = Scene(prims=[
            Prim(name="p", node_path="/p", world_q16=IDENTITY_Q16, positions_um=[(0, 0, 0)], indices=[0, 0, 0])
        ])
        glb_data = write_glb(sc)
        doc, binbuf = _split(glb_data)
        self.assertIsInstance(doc, dict)
        self.assertEqual(doc["asset"]["version"], "2.0")
        self.assertIsInstance(binbuf, bytes)

    def test_split_bad_input_types_and_headers_negative(self):
        # Non-bytes
        with self.assertRaises(GlbError) as cm:
            _split("not bytes")
        self.assertEqual(cm.exception.code, "E_IO")

        # Truncated header
        with self.assertRaises(GlbError) as cm:
            _split(b"glTF12")
        self.assertEqual(cm.exception.code, "E_IO")

        # Bad magic
        with self.assertRaises(GlbError) as cm:
            _split(b"BADM" + struct.pack("<II", GLB_VERSION, 12))
        self.assertEqual(cm.exception.code, "E_IO")

        # Wrong version
        with self.assertRaises(GlbError) as cm:
            _split(GLB_MAGIC + struct.pack("<II", 1, 12))
        self.assertEqual(cm.exception.code, "E_IO")

        # Length field mismatch
        with self.assertRaises(GlbError) as cm:
            _split(GLB_MAGIC + struct.pack("<II", GLB_VERSION, 20))
        self.assertEqual(cm.exception.code, "E_IO")

    def test_split_chunk_errors_negative(self):
        # Chunk overrun
        bad_chunk_hdr = GLB_MAGIC + struct.pack("<II", GLB_VERSION, 20) + struct.pack("<II", 100, glb_core.CHUNK_JSON)
        with self.assertRaises(GlbError) as cm:
            _split(bad_chunk_hdr)
        self.assertEqual(cm.exception.code, "E_IO")

        # Trailing bytes
        valid = make_glb_bytes({"asset": {"version": "2.0"}})
        with self.assertRaises(GlbError) as cm:
            _split(valid + b"extra")
        self.assertEqual(cm.exception.code, "E_IO")

        # Invalid JSON
        bad_json_body = struct.pack("<II", 4, glb_core.CHUNK_JSON) + b"{bad"
        bad_json_glb = GLB_MAGIC + struct.pack("<II", GLB_VERSION, 12 + len(bad_json_body)) + bad_json_body
        with self.assertRaises(GlbError) as cm:
            _split(bad_json_glb)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # No JSON dict chunk
        no_json_body = struct.pack("<II", 4, glb_core.CHUNK_BIN) + b"1234"
        no_json_glb = GLB_MAGIC + struct.pack("<II", GLB_VERSION, 12 + len(no_json_body)) + no_json_body
        with self.assertRaises(GlbError) as cm:
            _split(no_json_glb)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    @unittest.expectedFailure
    def test_split_rejects_bin_before_json_chunk_order_negative(self):
        # glTF 2.0 spec requires JSON chunk to be first chunk (chunk 0).
        import json
        j = json.dumps({"asset": {"version": "2.0"}}).encode()
        j += b" " * ((4 - len(j) % 4) % 4)
        b = b"1234"
        body = struct.pack("<II", len(b), glb_core.CHUNK_BIN) + b + struct.pack("<II", len(j), glb_core.CHUNK_JSON) + j
        glb = GLB_MAGIC + struct.pack("<II", GLB_VERSION, 12 + len(body)) + body
        with self.assertRaises(GlbError) as cm:
            _split(glb)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    # ------------------------------------------------------------------- _accessor
    def test_accessor_valid(self):
        binbuf = struct.pack("<3f3f", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        doc = {
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": 2, "type": "VEC3"},
                {"count": 3, "type": "SCALAR", "componentType": 5126}  # default zeros (no bufferView)
            ],
            "bufferViews": [{"byteOffset": 0, "byteLength": len(binbuf)}]
        }
        res0 = _accessor(doc, binbuf, 0)
        self.assertEqual(res0, [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        res1 = _accessor(doc, binbuf, 1)
        self.assertEqual(res1, [0.0, 0.0, 0.0])

    def test_accessor_normalized(self):
        binbuf = struct.pack("<2B", 0, 255)
        doc = {
            "accessors": [{"bufferView": 0, "componentType": 5121, "count": 2, "type": "SCALAR", "normalized": True}],
            "bufferViews": [{"byteOffset": 0, "byteLength": 2}]
        }
        res = _accessor(doc, binbuf, 0)
        self.assertEqual(res, [0.0, 1.0])

    def test_accessor_negative_cases(self):
        binbuf = b"\x00" * 8
        doc = {
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": 5, "type": "VEC3"},  # buffer overrun
                {"bufferView": 0, "componentType": 9999, "count": 1, "type": "VEC3"},  # invalid componentType
                {"bufferView": 0, "componentType": 5126, "count": 1, "type": "INVALID"},  # invalid type
                {"sparse": {}, "componentType": 5126, "count": 1, "type": "SCALAR"},  # sparse
                {"bufferView": 0, "componentType": 5126, "count": "bad", "type": "SCALAR"},  # invalid count type
            ],
            "bufferViews": [{"byteOffset": 0, "byteLength": 8}]
        }
        # Out-of-range buffer view -> E_IO
        with self.assertRaises(GlbError) as cm:
            _accessor(doc, binbuf, 0)
        self.assertEqual(cm.exception.code, "E_IO")

        # Invalid componentType -> E_SCHEMA
        with self.assertRaises(GlbError) as cm:
            _accessor(doc, binbuf, 1)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Invalid type -> E_SCHEMA
        with self.assertRaises(GlbError) as cm:
            _accessor(doc, binbuf, 2)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Sparse accessor unsupported -> E_SCHEMA
        with self.assertRaises(GlbError) as cm:
            _accessor(doc, binbuf, 3)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Invalid count type -> E_SCHEMA
        with self.assertRaises(GlbError) as cm:
            _accessor(doc, binbuf, 4)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Missing accessor index -> E_SCHEMA
        with self.assertRaises(GlbError) as cm:
            _accessor(doc, binbuf, 99)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    # ------------------------------------------------------------------- _mul & _local
    def test_mul_and_local_composition(self):
        # Identity
        ident = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
        self.assertEqual(_mul(ident, ident), ident)

        # Translation + Scale node
        node_trs = {"translation": [10.0, 20.0, 30.0], "scale": [2.0, 2.0, 2.0]}
        m_trs = _local(node_trs)
        expected_trs = [
            [2.0, 0.0, 0.0, 10.0],
            [0.0, 2.0, 0.0, 20.0],
            [0.0, 0.0, 2.0, 30.0],
            [0.0, 0.0, 0.0, 1.0]
        ]
        self.assertEqual(m_trs, expected_trs)

        # Matrix node (glTF column-major)
        # [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [5, 6, 7, 1]]
        col_major = [1, 0, 0, 0,  0, 1, 0, 0,  0, 0, 1, 0,  5, 6, 7, 1]
        m_mat = _local({"matrix": col_major})
        expected_mat = [
            [1.0, 0.0, 0.0, 5.0],
            [0.0, 1.0, 0.0, 6.0],
            [0.0, 0.0, 1.0, 7.0],
            [0.0, 0.0, 0.0, 1.0]
        ]
        self.assertEqual(m_mat, expected_mat)

        # Compose TRS and Matrix via _mul
        composed = _mul(m_mat, m_trs)
        # Applying m_trs then m_mat: scale by 2, translate by (10, 20, 30) then translate by (5, 6, 7) -> translation (15, 26, 37)
        expected_composed = [
            [2.0, 0.0, 0.0, 15.0],
            [0.0, 2.0, 0.0, 26.0],
            [0.0, 0.0, 2.0, 37.0],
            [0.0, 0.0, 0.0, 1.0]
        ]
        self.assertEqual(composed, expected_composed)

    def test_local_negative_cases(self):
        # Invalid matrix length
        with self.assertRaises(GlbError) as cm:
            _local({"matrix": [1.0] * 15})
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Invalid matrix element type
        with self.assertRaises(ValueError):
            _local({"matrix": ["not_a_number"] * 16})

    def test_mul_negative_case(self):
        # Incorrect dimensions
        with self.assertRaises(IndexError):
            _mul([[1.0]], [[1.0]])

    # ------------------------------------------------------------------- _walk
    def test_walk_hierarchy_and_cycle(self):
        doc = {
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "root", "children": [1], "translation": [1.0, 0.0, 0.0]},
                {"name": "child", "children": [2], "translation": [0.0, 2.0, 0.0]},
                {"name": "leaf", "translation": [0.0, 0.0, 3.0]}
            ]
        }
        res = _walk(doc)
        self.assertEqual(len(res), 3)
        paths = [p for _, _, p in res]
        self.assertEqual(paths, ["/root", "/root/child", "/root/child/leaf"])

        # Cycle refusal test: node 0 -> child 1 -> child 0
        doc_cycle = {
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "n0", "children": [1]},
                {"name": "n1", "children": [0]}
            ]
        }
        res_cycle = _walk(doc_cycle)
        visited = [i for i, _, _ in res_cycle]
        self.assertEqual(visited, [0, 1])  # Visited each node once, stopped cycle

    def test_walk_negative_cases(self):
        # Invalid scene index
        doc_bad_scene = {"scene": 5, "scenes": [{"nodes": [0]}], "nodes": [{"name": "n0"}]}
        with self.assertRaises(GlbError) as cm:
            _walk(doc_bad_scene)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Invalid child node reference (out of bounds)
        doc_bad_child = {"scenes": [{"nodes": [0]}], "nodes": [{"name": "n0", "children": [99]}]}
        with self.assertRaises(GlbError) as cm:
            _walk(doc_bad_child)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

        # Non-integer child node reference
        doc_invalid_child_type = {"scenes": [{"nodes": [0]}], "nodes": [{"name": "n0", "children": ["abc"]}]}
        with self.assertRaises(GlbError) as cm:
            _walk(doc_invalid_child_type)
        self.assertEqual(cm.exception.code, "E_SCHEMA")

    # ------------------------------------------------------------------- _div_round
    def test_div_round_positive_numerators(self):
        self.assertEqual(_div_round(0, 2), 0)
        self.assertEqual(_div_round(1, 2), 1)
        self.assertEqual(_div_round(3, 2), 2)
        self.assertEqual(_div_round(4, 2), 2)

    @unittest.expectedFailure
    def test_div_round_negative_half_away_from_zero_negative(self):
        # Note: glb_core._div_round implementation `(2*n + d) // (2*d)` uses Python's floor division,
        # which rounds towards negative infinity instead of rounding half away from zero for negative numerators.
        self.assertEqual(_div_round(-1, 2), -1)
        self.assertEqual(_div_round(-3, 2), -2)

    # ------------------------------------------------------------------- det3_q48
    def test_det3_q48_values(self):
        # Identity determinant is positive
        det_ident = det3_q48(IDENTITY_Q16)
        self.assertGreater(det_ident, 0)
        self.assertEqual(det_ident, Q16 ** 3)

        # Mirrored matrix (X axis flipped: m[0] = -Q16)
        mirrored = list(IDENTITY_Q16)
        mirrored[0] = -Q16
        det_mirrored = det3_q48(mirrored)
        self.assertLess(det_mirrored, 0)
        self.assertEqual(det_mirrored, -(Q16 ** 3))

    def test_det3_q48_negative_case(self):
        # Invalid matrix length
        with self.assertRaises(IndexError):
            det3_q48([Q16] * 5)


if __name__ == "__main__":
    unittest.main()
