"""Original humanoid fixture in integral micrometres, front along negative Y."""
from itertools import combinations, product

from asset_factory.domains import PPM


class AssemblyError(ValueError):
    def __init__(self, reason, rc=5):
        super().__init__(reason)
        self.rc = rc


PLACEMENTS = {
    "arm-left": (250_000, 0, 1_350_000),
    "arm-right": (-250_000, 0, 1_350_000),
    "head": (0, 0, 1_530_000),
    "leg-left": (125_000, 0, 900_000),
    "leg-right": (-125_000, 0, 900_000),
    "torso": (0, 0, 900_000),
}

# Each row gives the torso's outward normal and the full rectangular mating
# patch in the two remaining axes, in increasing axis order.
MATES = {
    "arm-left": (0, 1, (110_000, 100_000)),
    "arm-right": (0, -1, (110_000, 100_000)),
    "head": (2, 1, (140_000, 140_000)),
    "leg-left": (2, -1, (160_000, 150_000)),
    "leg-right": (2, -1, (160_000, 150_000)),
}

ROTATIONS = {
    (0, 1): [0, 707_107, 0, 707_107],
    (0, -1): [0, -707_107, 0, 707_107],
    (2, 1): [0, 0, 0, PPM],
    (2, -1): [PPM, 0, 0, 0],
}
IDENTITY = ROTATIONS[2, 1]
DIMENSIONS_UM = (1_440_000, 310_000, 1_800_000)
VIEWS = (
    ("front", (0, -1)), ("front34_left", (1, -1)), ("left", (1, 0)),
    ("back", (0, 1)), ("right", (-1, 0)), ("front34_right", (-1, -1)),
)


def _boxes(**regions):
    return {name: [[1000 * n for n in bounds[:3]], [1000 * n for n in bounds[3:]]]
            for name, bounds in regions.items()}


def parts():
    torso = _boxes(body=(-250, -105, 0, 250, 105, 630))
    head = _boxes(neck=(-70, -70, 0, 70, 70, 55),
                  skull=(-115, -100, 55, 115, 105, 270),
                  nose=(-25, -135, 145, 25, -100, 185))
    arm = _boxes(shoulder=(0, -55, -50, 135, 55, 50),
                 upper_arm=(75, -55, -180, 205, 55, -50),
                 forearm=(145, -48, -305, 335, 48, -180),
                 wrist=(288, -40, -365, 394, 40, -305),
                 hand=(362, -44, -437, 470, 44, -365))
    leg = _boxes(thigh=(-80, -75, -445, 80, 75, 0),
                 shin=(-69, -68, -815, 69, 68, -445),
                 foot=(-75, -182, -900, 75, 128, -815))

    def mirror(boxes):
        return {name: [[-hi[0], *lo[1:]], [-lo[0], *hi[1:]]]
                for name, (lo, hi) in boxes.items()}

    return {"arm-left": arm, "arm-right": mirror(arm), "head": head,
            "leg-left": leg, "leg-right": mirror(leg), "torso": torso}


def transform(position, rotation=IDENTITY):
    return {"translation_um": list(position), "rotation_xyzw_ppm": list(rotation),
            "uniform_scale_ppm": PPM}


def dimensions(boxes):
    return tuple(max(hi[axis] for _, hi in boxes) - min(lo[axis] for lo, _ in boxes)
                 for axis in range(3))


def _covers_patch(boxes, point, axis, sign, size):
    tangents = [index for index in range(3) if index != axis]
    face = 1 if sign > 0 else 0
    for lo, hi in boxes:
        plane = (lo, hi)[face][axis]
        if plane == point[axis] and all(
            2 * lo[i] <= 2 * point[i] - width and 2 * hi[i] >= 2 * point[i] + width
            for i, width in zip(tangents, size)
        ):
            return True
    return False


def check_geometry(shapes, positions):
    expected = parts()
    if shapes.keys() != PLACEMENTS.keys() or positions.keys() != PLACEMENTS.keys():
        raise AssemblyError("incomplete_part_set", 3)
    for name, boxes in shapes.items():
        if name.endswith(("-left", "-right")):
            role, side = name.split("-")
            counterpart = role + ("-right" if side == "left" else "-left")
            if boxes != expected[name] and boxes == expected[counterpart]:
                raise AssemblyError("mirrored_part")
        if name.startswith("arm-") and boxes.get("hand") != expected[name]["hand"]:
            raise AssemblyError("missing_hand")
        if boxes != expected[name]:
            raise AssemblyError("unsupported_fixture_geometry", 3)
        if (name.endswith("-left") and positions[name][0] <= 0
                or name.endswith("-right") and positions[name][0] >= 0):
            raise AssemblyError("wrong_side")
    world = {name: [tuple(tuple(x + t for x, t in zip(point, positions[name]))
                          for point in box) for box in boxes.values()]
             for name, boxes in shapes.items()}
    maximum = 0
    for first, second in combinations(world.values(), 2):
        for (lo, hi), (other_lo, other_hi) in product(first, second):
            overlap = min(min(b, d) - max(a, c) for a, b, c, d in zip(lo, hi, other_lo, other_hi))
            maximum = max(maximum, overlap)
    if maximum:
        raise AssemblyError("interpenetration")
    gaps = {}
    for name, (axis, sign, size) in MATES.items():
        anchor = tuple(positions["torso"][i] + PLACEMENTS[name][i] - PLACEMENTS["torso"][i]
                       for i in range(3))
        point = positions[name]
        if not (_covers_patch(world["torso"], anchor, axis, sign, size)
                and _covers_patch(world[name], point, axis, -sign, size)):
            raise AssemblyError("socket_face_coverage")
        gaps[name] = sign * (point[axis] - anchor[axis])
        if gaps[name] != 0 or point != anchor:
            raise AssemblyError("socket_gap")
    if positions["torso"] != PLACEMENTS["torso"]:
        raise AssemblyError("root_pose")
    measured = dimensions([box for boxes in world.values() for box in boxes])
    if measured != DIMENSIONS_UM or min(lo[2] for boxes in world.values() for lo, _ in boxes) != 0:
        raise AssemblyError("physical_scale")
    return {"dimensions_um": measured, "seam_gap_um": gaps, "max_gap_um": max(gaps.values()),
            "max_interpenetration_um": maximum}


def boundary(boxes):
    """Return consistently oriented integer triangles on the exact box union."""
    axes = [sorted({point[i] for box in boxes.values() for point in box}) for i in range(3)]
    cells = set()
    for low, high in boxes.values():
        cells.update(product(*(range(a.index(lo), a.index(hi))
                               for a, lo, hi in zip(axes, low, high))))
    vertices, faces = {}, []
    for cell in sorted(cells):
        for axis, sign in product(range(3), (-1, 1)):
            adjacent = list(cell)
            adjacent[axis] += sign
            if tuple(adjacent) in cells:
                continue
            b, c = (axis + 1) % 3, (axis + 2) % 3
            indices = []
            for u, v in ((0, 0), (1, 0), (1, 1), (0, 1)):
                index = list(cell)
                index[axis] += int(sign > 0)
                index[b] += u
                index[c] += v
                point = tuple(a[i] for a, i in zip(axes, index))
                indices.append(vertices.setdefault(point, len(vertices)))
            if sign < 0:
                indices.reverse()
            x, y, z, w = indices
            faces.extend(((x, y, z), (x, z, w)))
    return list(vertices), faces
