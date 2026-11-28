# SPDX-License-Identifier: AGPL-3.0-only
"""glb: dependency-free GLB reader and writer with a structural digest.

The digest is computed from the accessor bytes, image bytes and the node, skin
and animation structure, so it is independent of JSON formatting yet changes
with any geometric, skin, texture or animation change in the file.
"""
import hashlib
import json
import struct

import numpy as np

COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
GLB_MAGIC, CHUNK_JSON, CHUNK_BIN = 0x46546C67, 0x4E4F534A, 0x004E4942


class GlbError(ValueError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class Glb:
    def __init__(self, path):
        self.path = str(path)
        with open(self.path, "rb") as stream:
            self.bytes = stream.read()
        self.sha256 = _sha(self.bytes)
        data = self.bytes
        if len(data) < 20:
            raise GlbError("glb_truncated")
        magic, version, length = struct.unpack_from("<III", data, 0)
        if magic != GLB_MAGIC or version != 2 or length != len(data):
            raise GlbError("glb_header")
        offset, self.doc, self.bin = 12, None, b""
        while offset < length:
            chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
            offset += 8
            chunk = data[offset:offset + chunk_length]
            offset += chunk_length
            if chunk_type == CHUNK_JSON:
                self.doc = json.loads(chunk)
            elif chunk_type == CHUNK_BIN:
                self.bin = chunk
        if self.doc is None:
            raise GlbError("glb_json_chunk_missing")

    # ------------------------------------------------------------ decoding
    def view(self, index):
        view = self.doc["bufferViews"][index]
        start = view.get("byteOffset", 0)
        return self.bin[start:start + view["byteLength"]], view.get("byteStride")

    def accessor(self, index):
        acc = self.doc["accessors"][index]
        if "sparse" in acc:
            raise GlbError("sparse_accessor_unsupported")
        code, size = COMPONENT[acc["componentType"]]
        ncomp = NCOMP[acc["type"]]
        if "bufferView" not in acc:
            return np.zeros((acc["count"], ncomp), dtype=np.dtype(code))
        raw, stride = self.view(acc["bufferView"])
        start = acc.get("byteOffset", 0)
        item = np.dtype(code)
        if stride and stride != size * ncomp:
            rows = np.ndarray((acc["count"], ncomp), dtype=item, buffer=raw, offset=start,
                              strides=(stride, item.itemsize))
            return np.array(rows)
        return np.frombuffer(raw, dtype=item, count=acc["count"] * ncomp, offset=start).reshape(acc["count"], ncomp)

    def accessor_bytes(self, index):
        acc = self.doc["accessors"][index]
        if "bufferView" not in acc:
            return b""
        raw, _ = self.view(acc["bufferView"])
        start = acc.get("byteOffset", 0)
        code, size = COMPONENT[acc["componentType"]]
        return bytes(raw[start:start + acc["count"] * NCOMP[acc["type"]] * size])

    # ------------------------------------------------------------ structure
    def parents(self):
        parent = {}
        for i, node in enumerate(self.doc.get("nodes", [])):
            for child in node.get("children", []):
                if child in parent:
                    raise GlbError("node_two_parents")
                parent[child] = i
        return parent

    @staticmethod
    def local_matrix(node):
        if "matrix" in node:
            return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
        out = np.eye(4)
        if "scale" in node:
            out[:3, :3] = np.diag(node["scale"])
        if "rotation" in node:
            x, y, z, w = np.array(node["rotation"], dtype=np.float64)
            n = np.sqrt(x * x + y * y + z * z + w * w)
            x, y, z, w = x / n, y / n, z / n, w / n
            r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            out[:3, :3] = out[:3, :3] @ r
        if "translation" in node:
            out[:3, 3] = node["translation"]
        return out

    def node_globals(self):
        """Global rest matrices per node index (glTF axes)."""
        nodes = self.doc.get("nodes", [])
        parent = self.parents()
        cache = {}

        def glob(i):
            if i in cache:
                return cache[i]
            matrix = self.local_matrix(nodes[i])
            if i in parent:
                matrix = glob(parent[i]) @ matrix
            cache[i] = matrix
            return matrix

        return [glob(i) for i in range(len(nodes))]

    def structure(self):
        doc = self.doc
        nodes = doc.get("nodes", [])
        parent = self.parents()
        skins = []
        for skin in doc.get("skins", []):
            skins.append({"name": skin.get("name"), "joints": skin["joints"], "skeleton": skin.get("skeleton"),
                          "inverse_bind_sha256": _sha(self.accessor_bytes(skin["inverseBindMatrices"]))})
        meshes = []
        for mesh in doc.get("meshes", []):
            primitives = []
            for prim in mesh["primitives"]:
                if prim.get("mode", 4) != 4:
                    raise GlbError("non_triangle_primitive")
                primitives.append({
                    "attributes": {k: _sha(self.accessor_bytes(v)) for k, v in sorted(prim["attributes"].items())},
                    "count": doc["accessors"][prim["attributes"]["POSITION"]]["count"],
                    "indices_sha256": _sha(self.accessor_bytes(prim["indices"])) if "indices" in prim else None,
                    "material": prim.get("material")})
            meshes.append({"name": mesh.get("name"), "primitives": primitives})
        animations = []
        for anim in doc.get("animations", []):
            animations.append({
                "name": anim.get("name"),
                "channels": sorted((c["target"]["node"], c["target"]["path"]) for c in anim["channels"]),
                "samplers": [{"input": _sha(self.accessor_bytes(s["input"])),
                              "output": _sha(self.accessor_bytes(s["output"])),
                              "interpolation": s.get("interpolation", "LINEAR")} for s in anim["samplers"]]})
        return {
            "extensions_used": doc.get("extensionsUsed", []), "extensions_required": doc.get("extensionsRequired", []),
            "nodes": [{"name": n.get("name"), "parent": parent.get(i), "translation": n.get("translation"),
                       "rotation": n.get("rotation"), "scale": n.get("scale"), "matrix": n.get("matrix"),
                       "mesh": n.get("mesh"), "skin": n.get("skin"), "children": n.get("children")}
                      for i, n in enumerate(nodes)],
            "scenes": doc.get("scenes", []), "skins": skins, "meshes": meshes, "animations": animations,
            "materials": [{"name": m.get("name"), "double_sided": bool(m.get("doubleSided", False))}
                          for m in doc.get("materials", [])],
            "accessors_sha256": [_sha(self.accessor_bytes(i)) for i in range(len(doc.get("accessors", [])))],
            "images_sha256": [_sha(self.view(img["bufferView"])[0]) for img in doc.get("images", []) if "bufferView" in img]}

    def structure_digest(self):
        return _sha(json.dumps(self.structure(), sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def bind_error(self):
        """Largest joint-head distance (m) between inverse-bind matrices and rest node matrices."""
        globals_ = self.node_globals()
        worst, joints = 0.0, 0
        for skin in self.doc.get("skins", []):
            ibm = self.accessor(skin["inverseBindMatrices"]).astype(np.float64).reshape(-1, 4, 4).transpose(0, 2, 1)
            for k, joint in enumerate(skin["joints"]):
                bind = np.linalg.inv(ibm[k])
                worst = max(worst, float(np.linalg.norm(bind[:3, 3] - globals_[joint][:3, 3])))
                joints += 1
        return worst, joints

    def skin_arrays(self, mesh_index=0):
        """Concatenated POSITION (glTF axes, float64), triangles, JOINTS_0 and WEIGHTS_0."""
        pos, tri, joints, weights, base = [], [], [], [], 0
        for prim in self.doc["meshes"][mesh_index]["primitives"]:
            attr = prim["attributes"]
            p = self.accessor(attr["POSITION"]).astype(np.float64)
            t = self.accessor(prim["indices"]).astype(np.int64).reshape(-1, 3)
            pos.append(p)
            tri.append(t + base)
            if "JOINTS_0" in attr:
                joints.append(self.accessor(attr["JOINTS_0"]).astype(np.int64))
                weights.append(self.accessor(attr["WEIGHTS_0"]).astype(np.float64))
            base += len(p)
        return (np.vstack(pos), np.vstack(tri),
                np.vstack(joints) if joints else None, np.vstack(weights) if weights else None)


# ------------------------------------------------------------------ writer
class Writer:
    """Minimal GLB builder for synthetic control rigs."""

    def __init__(self):
        self.bin = bytearray()
        self.views, self.accessors, self.doc = [], [], {"asset": {"version": "2.0", "generator": "pose-oracle controls"}}
        self._pad()

    def _pad(self):
        while len(self.bin) % 4:
            self.bin += b"\x00"

    def buffer(self, data):
        self._pad()
        start = len(self.bin)
        self.bin += data
        self.views.append({"buffer": 0, "byteOffset": start, "byteLength": len(data)})
        return len(self.views) - 1

    def accessor(self, array, component, kind, target=None):
        array = np.ascontiguousarray(array, dtype=np.dtype(COMPONENT[component][0]))
        raw = array.tobytes()
        view = self.buffer(raw)
        if target is not None:
            self.views[view]["target"] = target
        acc = {"bufferView": view, "componentType": component, "count": int(array.shape[0]), "type": kind}
        if kind in ("SCALAR", "VEC3") and component in (5126, 5125):
            flat = array.reshape(array.shape[0], -1)
            acc["min"] = [float(x) for x in flat.min(0)]
            acc["max"] = [float(x) for x in flat.max(0)]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def save(self, path):
        doc = dict(self.doc, buffers=[{"byteLength": len(self.bin)}], bufferViews=self.views, accessors=self.accessors)
        js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        js += b" " * (-len(js) % 4)
        bin_ = bytes(self.bin) + b"\x00" * (-len(self.bin) % 4)
        total = 12 + 8 + len(js) + (8 + len(bin_) if bin_ else 0)
        blob = struct.pack("<III", GLB_MAGIC, 2, total) + struct.pack("<II", len(js), CHUNK_JSON) + js
        if bin_:
            blob += struct.pack("<II", len(bin_), CHUNK_BIN) + bin_
        with open(path, "wb") as stream:
            stream.write(blob)
        return _sha(blob)
