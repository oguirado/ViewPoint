import json
from pathlib import Path
import struct
import unittest

from asset_factory.domains import FRAME
from test_d18_uv_gates import Store
from tools.blender import uv, uv_quality
from tools.blender.bake_support import upstream
from tools.input.evidence import bytes_digest


class BlenderTangentTests(unittest.TestCase):
    def check_export(self, shading):
        path = Path(__file__).with_name(f"test_d18_uv_sphere_{shading}.glb")
        raw = path.read_bytes()
        pins = {
            "flat": "sha256:94aac7c0dec81f056d5bd0e809ce6583785e7b46e97692e82f31a5b913d7de01",
            "smooth": "sha256:457f90714fc984794f1cf0ac8eefe44937153d75ae1dca00cdbe266aace2df6d",
        }
        self.assertEqual(bytes_digest(raw), pins[shading])
        layout = {"schema": "af.uv-layout/v1", "mesh_digest": bytes_digest(raw), "frame": FRAME,
                  "size": [64, 64], "max_faces": 224, "max_work": 1000000, "max_mesh_bytes": 100000}
        size, faces, _ = uv_quality.load_triangles(raw, layout)
        self.assertEqual(len(faces), 224)
        sampling = {"filter": "bilinear", "wrap": "clamp-to-edge", "mip": "linear-box", "level": 0}
        covered = uv_quality.texel_faces(size, faces, lambda count: None)
        mips = [bytes([60, 90, 20, 255]) * (size[0] * size[1])]
        layout.update(charts=[1] * len(faces), orientation=[-1] * len(faces), color_space="linear",
                      gutters=[[i, min(covered)] for i in range(size[0] * size[1]) if i not in covered],
                      sampler=sampling, stretch=[20, 1], density=[30, 1000000],
                      density_factor=[20, 1], min_packing_ppm=1)
        store = Store()

        def check(candidate):
            pin = store.put("uv-layout", {**layout, "mesh_digest": bytes_digest(candidate)})
            receipt = uv.check_uv_quality(candidate, witness_digest=pin, read_checked=store.read,
                                          sampler=sampling, mips=mips)
            return receipt["source_rc"], receipt["reason_codes"]

        self.assertEqual(check(raw), (0, []))
        doc, data = upstream("glb_core")._split(raw)
        attrs = doc["meshes"][0]["primitives"][0]["attributes"]
        json_length = struct.unpack_from("<I", raw, 12)[0]

        def attribute_start(name):
            accessor = doc["accessors"][attrs[name]]
            view = doc["bufferViews"][accessor["bufferView"]]
            return 28 + json_length + view.get("byteOffset", 0) + accessor.get("byteOffset", 0)

        first_w = attribute_start("TANGENT") + 12
        flipped = bytearray(raw)
        self.assertEqual(struct.unpack_from("<f", raw, first_w)[0], 1.0)
        struct.pack_into("<f", flipped, first_w, -1.0)
        self.assertEqual(check(bytes(flipped)), (5, ["uv_mirrored_tangent_invalid"]))

        zero_normal = bytearray(raw)
        struct.pack_into("<fff", zero_normal, attribute_start("NORMAL"), 0, 0, 0)
        self.assertEqual(check(bytes(zero_normal)), (5, ["uv_mirrored_tangent_invalid"]))

        doc["accessors"][attrs["NORMAL"]]["componentType"] = 5125
        encoded = json.dumps(doc, separators=(",", ":")).encode()
        encoded += b" " * (-len(encoded) % 4)
        malformed = (struct.pack("<4sIIII", b"glTF", 2, 28 + len(encoded) + len(data), len(encoded), 0x4E4F534A)
                     + encoded + struct.pack("<II", len(data), 0x004E4942) + data)
        self.assertEqual(check(malformed), (2, ["uv_tangent_normal_accessor_invalid"]))

    def test_flat_blender_export(self):
        self.check_export("flat")

    def test_smooth_blender_export(self):
        self.check_export("smooth")
