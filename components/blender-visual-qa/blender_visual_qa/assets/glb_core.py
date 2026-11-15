"""Stdlib GLB core for the asset layer (AG1 L1): integer scene model, parse, write, sha256.

Frozen interface (AG1, consumed by normalize / mesh_gate / turntable):
  parse_glb(data: bytes) -> Scene ; write_glb(scene) -> bytes ; sha256(data: bytes) -> str ; GlbError(code).
Units: positions in micrometres (1 glTF unit = 1 m), uvs / normals / matrices in q16 (value * 65536, floor+0.5 like
bpy_side.q). `world_q16` is the node world matrix, row-major 4x4 with column vectors (index 4*row+col); translation
entries are q16 metres. `positions_um` are LOCAL (mesh space); `world_positions_um(prim)` applies `world_q16` in
integer arithmetic. Every node of the default scene is walked (AUDIT F1: no meshes[0].primitives[0] shortcut).
Error codes: E_IO (container bytes: magic, length, chunks, buffer overrun) | E_SCHEMA (glTF document content).
"""
# @module {"id":"bv.assets.glb_core","layer":2,"owner":"BASSETS"}
from dataclasses import dataclass, field
import hashlib
import json
import math
import struct

GLB_MAGIC = b"glTF"
GLB_VERSION = 2
GLB_HEADER_BYTES = 12
CHUNK_HEADER_BYTES = 8
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942
MODE_TRIANGLES = 4
MAT_N = 16
Q16 = 65536
UM_PER_M = 1000000
CODES = ("E_IO", "E_SCHEMA")

_CT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_NORM = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}
IDENTITY_Q16 = tuple(Q16 if r == c else 0 for r in range(4) for c in range(4))


class GlbError(Exception):
    """Unreadable or invalid GLB. `code` is E_IO or E_SCHEMA; callers map it to rc 2."""

    def __init__(self, code, detail=""):
        if code not in CODES:
            raise ValueError("unknown GlbError code " + repr(code))
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail


@dataclass
class Prim:
    name: str
    node_path: str
    world_q16: tuple
    positions_um: list
    indices: list
    uvs_q16: list = None
    normals_q16: list = None
    material: str = None
    skinned: bool = False
    mesh_id: int = -1  # glTF mesh index (shared instances share it); -1 when built in memory


@dataclass
class Scene:
    prims: list = field(default_factory=list)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _quant(x, scale, what):
    x = float(x)
    if not math.isfinite(x):
        raise GlbError("E_SCHEMA", "non-finite " + what)
    return int(math.floor(x * scale + 0.5))


# --------------------------------------------------------------------------- container
def _split(data):
    if not isinstance(data, (bytes, bytearray)):
        raise GlbError("E_IO", "data is not bytes")
    if len(data) < GLB_HEADER_BYTES or bytes(data[:4]) != GLB_MAGIC:
        raise GlbError("E_IO", "bad magic or truncated header")
    version, length = struct.unpack("<II", data[4:GLB_HEADER_BYTES])
    if version != GLB_VERSION:
        raise GlbError("E_IO", "unsupported glTF version %d" % version)
    if length != len(data):
        raise GlbError("E_IO", "length field %d != size %d" % (length, len(data)))
    off, doc, binbuf = GLB_HEADER_BYTES, None, b""
    while off + CHUNK_HEADER_BYTES <= len(data):
        clen, ctype = struct.unpack("<II", data[off:off + CHUNK_HEADER_BYTES])
        chunk = bytes(data[off + CHUNK_HEADER_BYTES:off + CHUNK_HEADER_BYTES + clen])
        if len(chunk) != clen:
            raise GlbError("E_IO", "chunk overruns file")
        off += CHUNK_HEADER_BYTES + clen
        if ctype == CHUNK_JSON and doc is None:
            try:
                doc = json.loads(chunk.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise GlbError("E_SCHEMA", "JSON chunk invalid: %s" % exc)
        elif ctype == CHUNK_BIN and not binbuf:
            binbuf = chunk
    if off != len(data):
        raise GlbError("E_IO", "trailing bytes after last chunk")
    if not isinstance(doc, dict):
        raise GlbError("E_SCHEMA", "no JSON object chunk")
    return doc, binbuf


def _accessor(doc, binbuf, idx):
    try:
        acc = doc["accessors"][idx]
        if "sparse" in acc:
            raise GlbError("E_SCHEMA", "sparse accessor %d unsupported" % idx)
        fmt, size = _CT[acc["componentType"]]
        nc = _NC[acc["type"]]
        count = int(acc["count"])
        if "bufferView" not in acc:
            return [0.0 if nc == 1 else (0.0,) * nc for _ in range(count)]
        bv = doc["bufferViews"][acc["bufferView"]]
        base = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
        stride = int(bv.get("byteStride") or size * nc)
        norm = bool(acc.get("normalized", False)) and acc["componentType"] in _NORM
        div = _NORM.get(acc["componentType"], 1.0)
        st = struct.Struct("<" + fmt * nc)
        out = []
        for i in range(count):
            o = base + i * stride
            if o + size * nc > len(binbuf):
                raise GlbError("E_IO", "accessor %d overruns the binary buffer" % idx)
            vals = st.unpack_from(binbuf, o)
            if norm:
                vals = tuple(max(v / div, -1.0) for v in vals)
            out.append(vals[0] if nc == 1 else vals)
        return out
    except GlbError:
        raise
    except (KeyError, IndexError, struct.error, TypeError, ValueError) as exc:
        raise GlbError("E_SCHEMA", "corrupt accessor %r: %r" % (idx, exc))


# --------------------------------------------------------------------------- float matrices (parse only)
def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _local(node):
    if "matrix" in node:
        m = node["matrix"]
        if not isinstance(m, list) or len(m) != MAT_N:
            raise GlbError("E_SCHEMA", "node matrix is not 16 numbers")
        return [[float(m[c * 4 + r]) for c in range(4)] for r in range(4)]  # glTF is column-major
    t = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[r[0][0] * s[0], r[0][1] * s[1], r[0][2] * s[2], t[0]],
            [r[1][0] * s[0], r[1][1] * s[1], r[1][2] * s[2], t[1]],
            [r[2][0] * s[0], r[2][1] * s[1], r[2][2] * s[2], t[2]],
            [0.0, 0.0, 0.0, 1.0]]


def _walk(doc):
    """(node_index, world float matrix, path) for every node reachable from the default scene, depth-first."""
    nodes = doc.get("nodes", [])
    scenes = doc.get("scenes", [])
    if scenes:
        try:
            roots = list(scenes[doc.get("scene", 0)].get("nodes", []))
        except (IndexError, TypeError, AttributeError):
            raise GlbError("E_SCHEMA", "default scene index invalid")
    else:
        children = {c for n in nodes for c in n.get("children", [])}
        roots = [i for i in range(len(nodes)) if i not in children]
    ident = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]
    out, seen = [], set()
    stack = [(r, ident, "") for r in reversed(roots)]
    while stack:
        i, parent, path = stack.pop()
        if not isinstance(i, int) or i in seen or not (0 <= i < len(nodes)):
            if isinstance(i, int) and i in seen:
                continue
            raise GlbError("E_SCHEMA", "node reference %r invalid" % (i,))
        seen.add(i)
        node = nodes[i]
        wm = _mul(parent, _local(node))
        p = path + "/" + str(node.get("name", "node%d" % i))
        out.append((i, wm, p))
        for c in reversed(node.get("children", [])):
            stack.append((c, wm, p))
    return out


def parse_glb(data):
    doc, binbuf = _split(data)
    meshes, nodes, mats = doc.get("meshes", []), doc.get("nodes", []), doc.get("materials", [])
    prims = []
    for ni, wm, path in _walk(doc):
        node = nodes[ni]
        if "mesh" not in node:
            continue
        try:
            mesh = meshes[node["mesh"]]
        except (IndexError, TypeError):
            raise GlbError("E_SCHEMA", "node %d mesh reference invalid" % ni)
        world = tuple(_quant(wm[r][c], Q16, "matrix") for r in range(4) for c in range(4))
        for pi, prim in enumerate(mesh.get("primitives", [])):
            where = "node %d primitive %d" % (ni, pi)
            if prim.get("mode", MODE_TRIANGLES) != MODE_TRIANGLES:
                raise GlbError("E_SCHEMA", where + ": not TRIANGLES")
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                raise GlbError("E_SCHEMA", where + ": no POSITION")
            pos = [tuple(_quant(c, UM_PER_M, "position") for c in p) for p in _accessor(doc, binbuf, attrs["POSITION"])]
            if any(len(p) != 3 for p in pos):
                raise GlbError("E_SCHEMA", where + ": POSITION is not VEC3")
            if "indices" in prim:
                idx = [int(i) for i in _accessor(doc, binbuf, prim["indices"])]
            else:
                idx = list(range(len(pos)))
            if len(idx) % 3 != 0:
                raise GlbError("E_SCHEMA", where + ": index count not a multiple of 3")
            if idx and (max(idx) >= len(pos) or min(idx) < 0):
                raise GlbError("E_SCHEMA", where + ": index out of range")
            uvs = nrm = None
            if "TEXCOORD_0" in attrs:
                uvs = [tuple(_quant(c, Q16, "uv") for c in t) for t in _accessor(doc, binbuf, attrs["TEXCOORD_0"])]
            if "NORMAL" in attrs:
                nrm = [tuple(_quant(c, Q16, "normal") for c in t) for t in _accessor(doc, binbuf, attrs["NORMAL"])]
            for what, arr in (("TEXCOORD_0", uvs), ("NORMAL", nrm)):
                if arr is not None and len(arr) != len(pos):
                    raise GlbError("E_SCHEMA", where + ": %s count != POSITION count" % what)
            mi = prim.get("material")
            material = None
            if mi is not None:
                try:
                    material = str(mats[mi].get("name", "material%d" % mi))
                except (IndexError, TypeError, AttributeError):
                    raise GlbError("E_SCHEMA", where + ": material reference invalid")
            name = str(node.get("name", "node%d" % ni))
            prims.append(Prim(name=name if pi == 0 else "%s.%d" % (name, pi), node_path=path, world_q16=world,
                              positions_um=pos, indices=idx, uvs_q16=uvs, normals_q16=nrm, material=material,
                              skinned=("skin" in node or "JOINTS_0" in attrs), mesh_id=int(node["mesh"])))
    return Scene(prims=prims)


def read_glb(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise GlbError("E_IO", "cannot read %s: %s" % (path, exc))
    return parse_glb(data)


# --------------------------------------------------------------------------- integer helpers
def _div_round(n, d):
    return (2 * n + d) // (2 * d)


def world_positions_um(prim):
    """Local positions mapped through world_q16 in exact integer arithmetic (rounded to 1 um)."""
    m = prim.world_q16
    out = []
    for p in prim.positions_um:
        out.append(tuple(_div_round(m[4 * r] * p[0] + m[4 * r + 1] * p[1] + m[4 * r + 2] * p[2]
                                    + m[4 * r + 3] * UM_PER_M, Q16) for r in range(3)))
    return out


def det3_q48(world_q16):
    """Determinant of the upper 3x3 of a q16 matrix, exact (scaled by 2**48). Sign < 0 means a reflection."""
    m = world_q16
    return (m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8])
            + m[2] * (m[4] * m[9] - m[5] * m[8]))


# --------------------------------------------------------------------------- writer
def _pad(b, fill):
    return b + fill * ((4 - len(b) % 4) % 4)


def write_glb(scene, share_identical=False):
    """Deterministic GLB: one root node per prim (matrix = world_q16, omitted when identity), float32 metres.
    Positions survive parse(write(.)) exactly within |x| <= 8.3 m (float32 spacing <= 1 um). Skin data is not
    written (prims are static). share_identical=True makes prims with identical geometry share one mesh."""
    binbuf = bytearray()
    views, accs, meshes, nodes, mats, mat_index, mesh_key = [], [], [], [], [], {}, {}

    def add(values, fmt, comp, typ, target=None, minmax=False):
        nc = _NC[typ]
        flat = [x for v in values for x in (v if nc > 1 else (v,))]
        raw = struct.pack("<%d%s" % (len(flat), fmt), *flat)
        while len(binbuf) % 4:
            binbuf.append(0)
        view = {"buffer": 0, "byteOffset": len(binbuf), "byteLength": len(raw)}
        if target is not None:
            view["target"] = target
        binbuf.extend(raw)
        views.append(view)
        acc = {"bufferView": len(views) - 1, "componentType": comp, "count": len(values), "type": typ}
        if minmax and values:
            acc["min"] = [min(v[i] for v in values) for i in range(nc)]
            acc["max"] = [max(v[i] for v in values) for i in range(nc)]
        accs.append(acc)
        return len(accs) - 1

    for prim in scene.prims:
        if len(prim.world_q16) != MAT_N:
            raise GlbError("E_SCHEMA", "world_q16 of %s is not 16 integers" % prim.name)
        key = (tuple(prim.positions_um), tuple(prim.indices), tuple(prim.uvs_q16 or ()),
               tuple(prim.normals_q16 or ()), prim.material)
        if share_identical and key in mesh_key:
            mesh = mesh_key[key]
        else:
            attrs = {"POSITION": add([tuple(c / UM_PER_M for c in p) for p in prim.positions_um], "f", 5126, "VEC3",
                                     34962, minmax=True)}
            if prim.normals_q16 is not None:
                attrs["NORMAL"] = add([tuple(c / Q16 for c in n) for n in prim.normals_q16], "f", 5126, "VEC3", 34962)
            if prim.uvs_q16 is not None:
                attrs["TEXCOORD_0"] = add([tuple(c / Q16 for c in t) for t in prim.uvs_q16], "f", 5126, "VEC2", 34962)
            p = {"attributes": attrs, "indices": add(list(prim.indices), "I", 5125, "SCALAR", 34963),
                 "mode": MODE_TRIANGLES}
            if prim.material is not None:
                if prim.material not in mat_index:
                    mat_index[prim.material] = len(mats)
                    mats.append({"name": prim.material})
                p["material"] = mat_index[prim.material]
            meshes.append({"name": prim.name, "primitives": [p]})
            mesh = mesh_key[key] = len(meshes) - 1
        node = {"name": prim.name, "mesh": mesh}
        if tuple(prim.world_q16) != IDENTITY_Q16:
            node["matrix"] = [prim.world_q16[4 * r + c] / Q16 for c in range(4) for r in range(4)]
        nodes.append(node)
    doc = {"asset": {"version": "2.0", "generator": "bvqa.assets.glb_core"}, "scene": 0,
           "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes, "accessors": accs,
           "bufferViews": views}
    if mats:
        doc["materials"] = mats
    if binbuf:
        doc["buffers"] = [{"byteLength": len(_pad(bytes(binbuf), b"\x00"))}]
    j = _pad(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8"), b" ")
    body = struct.pack("<II", len(j), CHUNK_JSON) + j
    if binbuf:
        b = _pad(bytes(binbuf), b"\x00")
        body += struct.pack("<II", len(b), CHUNK_BIN) + b
    return GLB_MAGIC + struct.pack("<II", GLB_VERSION, GLB_HEADER_BYTES + len(body)) + body
