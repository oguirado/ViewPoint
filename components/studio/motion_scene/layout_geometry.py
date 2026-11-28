# SPDX-License-Identifier: AGPL-3.0-only
"""Metre-scale layout geometry and camera projection for the clay stage."""
import math

import numpy as np

FPS, LAST_FRAME = 24, 288
SENSOR_MM = 36.0

# ---- part classes used by every mask and metric -------------------------------------------
BG, SET, SCREEN, BODY, ARM, HAND, CAP = 1, 2, 3, 4, 5, 6, 7
PART_NAME = {BG: "bg", SET: "set", SCREEN: "screen", BODY: "body", ARM: "arm", HAND: "hand", CAP: "cap"}

# ---- SET geometry --------------------------------------------------------
CAP_REST = np.array([0.22, 0.100, 1.05])      # cap face centre at rest (this package)
HAND_CONTACT = np.array([0.22, 0.100, 0.955])  # glove end-face centre at contact (95 mm below the cap centre)
CAP_AXIS = np.array([0.0, 1.0, 0.0])          # inward travel axis (world +Y)
CAP_TRAVEL = 0.038                            # qualified stroke [CITED]

# ---- cap mechanism (independently authored; local frame +Z = CAP_AXIS, origin = CAP_REST) ----
CAP_R, CAP_LEN, CAP_CHAMFER = 0.170, 0.110, 0.012
COLLAR_BORE, COLLAR_OUT, LIP_Z, COLLAR_BACK = 0.178, 0.210, 0.050, 0.202
GLYPH = ((0.100, 0.120), (0.000, 0.075), (0.100, 0.030))                 # PLAY triangle, upper-left of the face as seen from CAM_M (local x = -world x)
BRACKET = ((0.26, 0.44), (0.302, 0.364), (0.95, 1.15))                     # plate joining housing back to the TV

def unit(vector):
    vector = np.asarray(vector, dtype=float)
    return vector / np.linalg.norm(vector)


def rot_z(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def axis_frame(axis, up=(0.0, 0.0, 1.0)):
    """Rotation whose local +Z is `axis`, local +Y as close to `up` as possible."""
    z = unit(axis)
    up = np.asarray(up, dtype=float)
    if abs(float(np.dot(z, unit(up)))) > 0.98:
        up = np.array([0.0, 1.0, 0.0]) if abs(z[1]) < 0.9 else np.array([1.0, 0.0, 0.0])
    y = unit(up - np.dot(up, z) * z)
    x = np.cross(y, z)
    return np.column_stack([x, y, z])


def mat(rot=None, pos=(0.0, 0.0, 0.0)):
    m = np.eye(4)
    if rot is not None:
        m[:3, :3] = rot
    m[:3, 3] = pos
    return m


# ---- triangle soup ----------------------------------------------------------------------------
def _outward(verts, faces):
    """Flip windings so face normals point away from the shape centre (convex shapes only)."""
    tri = verts[faces]
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    centre = verts.mean(axis=0)
    reference = tri.mean(axis=1) - centre
    flip = np.einsum("ij,ij->i", normal, reference) < 0
    faces = faces.copy()
    faces[flip] = faces[flip][:, [0, 2, 1]]
    return faces


def box(size, open_back=False):
    """Axis-aligned box; open_back drops the -Z face (glove at the wrist line)."""
    hx, hy, hz = (s / 2 for s in size)
    v = np.array([[x, y, z] for x in (-hx, hx) for y in (-hy, hy) for z in (-hz, hz)])  # 4*ix + 2*iy + iz
    quads = ((0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5))
    f = []
    for a, b, c, d in quads:
        f += [(a, b, c), (a, c, d)]
    f = np.array(f, dtype=np.int64)
    f = _outward(v, f)
    if open_back:
        tri = v[f]
        normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        f = f[normal[:, 2] > -1e-9]
    return v, f


def revolve(profile, segments=48):
    """Revolve an (r, z) profile about local Z; r == 0 makes a pole. Windings come out outward."""
    verts, rings = [], []
    for r, z in profile:
        if r <= 1e-9:
            rings.append([len(verts)])
            verts.append((0.0, 0.0, z))
        else:
            ring = []
            for i in range(segments):
                a = 2 * math.pi * i / segments
                ring.append(len(verts))
                verts.append((r * math.cos(a), r * math.sin(a), z))
            rings.append(ring)
    faces = []
    for low, high in zip(rings, rings[1:]):
        if len(low) == 1 and len(high) == 1:
            continue
        if len(low) == 1:
            faces += [(low[0], high[(i + 1) % segments], high[i]) for i in range(segments)]
        elif len(high) == 1:
            faces += [(low[i], low[(i + 1) % segments], high[0]) for i in range(segments)]
        else:
            for i in range(segments):
                j = (i + 1) % segments
                faces += [(low[i], low[j], high[j]), (low[i], high[j], high[i])]
    return np.array(verts, dtype=float), np.array(faces, dtype=np.int64)


def prism(points2d, depth):
    """Triangle prism in the local XY plane; front face at z = -depth (outward, toward the viewer), back at z = 0."""
    n = len(points2d)
    v = np.array([(x, y, -depth) for x, y in points2d] + [(x, y, 0.0) for x, y in points2d])
    f = []
    for i in range(1, n - 1):
        f += [(0, i + 1, i), (n, n + i, n + i + 1)]
    for i in range(n):
        j = (i + 1) % n
        f += [(i, j, n + j), (i, n + j, n + i)]
    f = np.array(f, dtype=np.int64)
    return v, _outward(v, f)


class Primitive:
    __slots__ = ("name", "part", "verts", "faces", "matrix", "bevel", "smooth", "role", "in_solo")

    def __init__(self, name, part, mesh, matrix=None, bevel=0.0, smooth=False, role=None, in_solo=True):
        self.name, self.part = name, part
        self.verts, self.faces = mesh
        self.matrix = np.eye(4) if matrix is None else np.asarray(matrix, dtype=float)
        self.bevel, self.smooth, self.role, self.in_solo = bevel, smooth, role, in_solo

    def world(self):
        return (self.matrix[:3, :3] @ self.verts.T).T + self.matrix[:3, 3]


def cap_frame_matrix(travel=0.0, origin=CAP_REST, axis=CAP_AXIS):
    return mat(axis_frame(axis), np.asarray(origin, dtype=float) + travel * unit(axis))


def build_set():
    """Static SET primitives: TV + stand at the qualified dims; pod, collar, room authored here."""
    prims = []

    def add(name, part, centre, size, bevel=0.0):
        prims.append(Primitive(name, part, box(size), mat(None, centre), bevel))

    add("Floor", BG, (0.0, 0.0, -0.072), (16.0, 14.0, 0.144))
    add("Back wall", BG, (0.0, 2.31, 2.504), (15.0, 0.18, 5.0))
    add("Left wall", BG, (-3.42, -2.63, 2.5), (0.18, 10.0, 5.0))
    add("Back skirting", BG, (0.0, 2.194, 0.079), (14.0, 0.044, 0.158), 0.004)
    add("Left skirting", BG, (-3.309, -2.596, 0.079), (0.042, 9.64, 0.158), 0.004)
    add("CRT cabinet", SET, (0.935, 0.694, 1.12), (1.03, 0.66, 0.666), 0.10)
    add("CRT rear housing", SET, (0.935, 1.005, 1.13), (0.78, 0.47, 0.51), 0.12)
    add("CRT front trim", SET, (0.935, 0.365, 1.114), (0.969, 0.125, 0.597), 0.06)
    add("CRT glass gasket", SET, (0.935, 0.317, 1.157), (0.827, 0.057, 0.502), 0.025)
    screen = (np.array([[x, 0.277, z] for x in (0.552, 1.318) for z in (0.935, 1.379)]),
             np.array([[0, 2, 3], [0, 3, 1]], dtype=np.int64))   # normal -Y, toward the audience
    prims.append(Primitive("CRT Screen", SCREEN, screen, role="screen"))
    add("Stand deck", SET, (0.819, 0.602, 0.746), (1.56, 0.87, 0.084), 0.012)
    add("Stand lower shelf", SET, (0.819, 0.611, 0.244), (1.44, 0.71, 0.042), 0.008)
    for i, (x, y) in enumerate(((0.19, 0.2925), (0.19, 0.923), (1.4475, 0.2925), (1.4475, 0.923))):
        add(f"Stand upright {i}", SET, (x, y, 0.365), (0.052, 0.055, 0.696), 0.004)
        add(f"Stand foot {i}", SET, (x, y, 0.021), (0.078, 0.083, 0.042), 0.006)
    add("PLAY bracket", SET, tuple((lo + hi) / 2 for lo, hi in BRACKET), tuple(hi - lo for lo, hi in BRACKET), 0.008)
    housing = revolve([(COLLAR_BORE, LIP_Z), (COLLAR_OUT, LIP_Z), (COLLAR_OUT, COLLAR_BACK),
                       (0.0, COLLAR_BACK), (0.0, COLLAR_BACK - 0.006), (COLLAR_BORE, COLLAR_BACK - 0.006),
                       (COLLAR_BORE, LIP_Z)], 64)
    prims.append(Primitive("PLAY housing", SET, housing, cap_frame_matrix(), 0.0, True, "collar"))
    return prims


def cap_primitives(travel=0.0):
    face = cap_frame_matrix(travel=travel)
    cap = revolve([(0.0, 0.0), (CAP_R - CAP_CHAMFER, 0.0), (CAP_R, CAP_CHAMFER), (CAP_R, CAP_LEN - CAP_CHAMFER),
                   (CAP_R - CAP_CHAMFER, CAP_LEN), (0.0, CAP_LEN)], 64)
    return [Primitive("PLAY cap", CAP, cap, face, 0.0, True, "cap"),
            Primitive("PLAY glyph", CAP, prism(GLYPH, 0.004), face, 0.0, False, "glyph")]




def camera_matrix(target, theta_deg, elev_deg, dist, lens_mm=40.0):
    """Camera on a sphere about `target`; theta from world -Y toward +X, elev above the horizon."""
    t, e = math.radians(theta_deg), math.radians(elev_deg)
    offset = dist * np.array([math.sin(t) * math.cos(e), -math.cos(t) * math.cos(e), math.sin(e)])
    pos = np.asarray(target, dtype=float) + offset
    rot = rot_z(theta_deg) @ np.array([[1, 0, 0], [0, math.cos(math.pi / 2 - e), -math.sin(math.pi / 2 - e)],
                                       [0, math.sin(math.pi / 2 - e), math.cos(math.pi / 2 - e)]])
    world_to_cam = np.eye(4)
    world_to_cam[:3, :3] = rot.T
    world_to_cam[:3, 3] = -rot.T @ pos
    return {"pos": pos, "rot": rot, "euler": (math.pi / 2 - e, 0.0, t), "theta": theta_deg, "elev": elev_deg,
            "dist": dist, "lens": lens_mm, "target": np.asarray(target, dtype=float), "world_to_cam": world_to_cam}
