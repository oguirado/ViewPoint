"""CPU Blender fixture author. Run with Blender's --python entry point."""
from collections import Counter
from fractions import Fraction
from itertools import permutations, product
from math import ceil, dist, pi
import json
from pathlib import Path
import sys


UM = 125000
PPM = 1000000
def rotation(axis, cosine, sine):
    if axis == "x":
        return ((PPM, 0, 0), (0, cosine, -sine), (0, sine, cosine))
    return ((cosine, -sine, 0), (sine, cosine, 0), (0, 0, PPM))


def tet_faces(tet):
    a, b, c, d = tet
    return ((b, c, d), (a, d, c), (a, b, d), (a, c, b))


def author():
    points, point_ids, tets = [], {}, []
    origins = {"torso": (0, 20, 0), "neck": (0, 24, 0), "face": (0, 28, 0)}
    parents = {"torso": None, "neck": "torso", "face": "neck"}
    tails = {"face": (0, 31, 0)}
    joints_by_part = {"torso": {"torso"}, "neck": {"torso", "neck", "face"},
                      "face": {"neck", "face"}}
    poses = {name: {} for name in ("elbows", "elbow_flexion", "hands", "knees", "face_attachment")}
    poses["face_attachment"]["face"] = rotation("z", 960000, 280000)

    for side, sign in (("left", 1), ("right", -1)):
        def joint(role):
            return side + "_" + role

        shoulder, elbow, hand = (joint(name) for name in ("shoulder", "elbow", "hand"))
        hip, knee, foot = (joint(name) for name in ("hip", "knee", "foot"))
        origins.update({shoulder: (sign*7, 22, 0), elbow: (sign*9, 22, 0),
                        hand: (sign*17, 22, 0), hip: (sign*4, 14, 0),
                        knee: (sign*4, 7, 0), foot: (sign*4, 2, 0)})
        parents.update({shoulder: "torso", elbow: shoulder, hand: elbow,
                        hip: "torso", knee: hip, foot: knee})
        tails.update({hand: (sign*19, 22, 0), foot: (sign*4, 1, 0)})
        joints_by_part[joint("arm")] = {"torso", shoulder, elbow, hand}
        joints_by_part[joint("leg")] = {"torso", hip, knee, foot}
        poses["elbows"][elbow] = rotation("x", 800000, sign*600000)
        poses["elbow_flexion"][shoulder] = rotation("x", 960000, sign*280000)
        poses["elbow_flexion"][elbow] = rotation("z", 600000, sign*800000)
        poses["hands"][hand] = rotation("x", 800000, sign*600000)
        poses["knees"][knee] = rotation("x", 800000, 600000)

    # A common lattice shares each full face across rectangular cells. Using
    # the same oriented six-tet split on every cell makes the interface
    # triangulation conform across neck, shoulder, wrist and hip seams.
    xs = [-20, -17, -13, -9, -6, -3, 0, 3, 6, 9, 13, 17, 20]
    ys = [0, 4, 8, 12, 16, 20, 24, 27, 32]
    for (x0, x1), (y0, y1) in product(zip(xs, xs[1:]), zip(ys, ys[1:])):
        side = "left" if x0 >= 0 else "right"
        near, far = sorted(map(abs, (x0, x1)))
        if far <= 6 and 12 <= y0 < 24:
            part = "torso"
        elif far <= 3 and y0 >= 24:
            part = "neck" if y0 == 24 else "face"
        elif near >= 6 and y0 == 20:
            part = side + "_arm"
        elif near == 3 and y0 < 12:
            part = side + "_leg"
        else:
            continue
        corners = {}
        for xyz in product(range(2), repeat=3):
            point = tuple(values[i] for i, values in zip(xyz, ((x0, x1), (y0, y1), (-2, 2))))
            if point not in point_ids:
                point_ids[point] = len(points)
                points.append(point)
            corners[xyz] = point_ids[point]
        for order in permutations(range(3)):
            key = [0, 0, 0]
            tet = [corners[tuple(key)]]
            for axis in order:
                key[axis] = 1
                tet.append(corners[tuple(key)])
            if sum(a > b for i, a in enumerate(order) for b in order[i+1:]) % 2:
                tet[-2], tet[-1] = tet[-1], tet[-2]
            tets.append({"part": part, "vertices": tet})

    def weight_row(p):
        x, y, _ = p
        sign = "left_" if x > 0 else "right_"
        if abs(x) > 6:
            return {9: {"torso": 0.5, sign+"shoulder": 0.5},
                    13: {sign+"shoulder": 0.5, sign+"elbow": 0.5},
                    17: {sign+"elbow": 0.5, sign+"hand": 0.5},
                    20: {sign+"hand": 1}}[abs(x)]
        if y < 12:
            return {8: {sign+"hip": 1}, 4: {sign+"knee": 0.5, sign+"foot": 0.5},
                    0: {sign+"foot": 1}}[y]
        if y > 24:
            return {"torso": 0.5, "neck": 0.25, "face": 0.25} if y == 27 else {"face": 1}
        return {"torso": 1}

    weights = [weight_row(p) for p in points]
    allowed = [set() for _ in points]
    for tet in tets:
        for i in tet["vertices"]:
            allowed[i].update(joints_by_part[tet["part"]])
    faces = [face for t in tets for face in tet_faces(t["vertices"])]
    counts = Counter(tuple(sorted(face)) for face in faces)
    boundary = [face for face in faces if counts[tuple(sorted(face))] == 1]

    def transform(p, name, rotations):
        # Authored active joints have parallel rest axes. Apply the point's
        # local rotation about its own bind head, followed by its ancestors.
        while name is not None:
            if name in rotations:
                centre = origins[name]
                relative = [a-b for a, b in zip(p, centre)]
                p = tuple(c + Fraction(sum(a*b for a, b in zip(row, relative)), PPM)
                          for c, row in zip(centre, rotations[name]))
            name = parents[name]
        return p

    pose_records = []
    for pose_id, rotations in poses.items():
        deltas = [{"joint": name, "matrix_ppm": [
            matrix[r][c] if r < 3 and c < 3 else PPM if r == c == 3 else 0
            for c in range(4) for r in range(4)]} for name, matrix in sorted(rotations.items())]
        expected = []
        for p, row in zip(points, weights):
            samples = [(Fraction(w), transform(p, name, rotations)) for name, w in row.items()]
            expected.append([round(UM * sum(w*q[a] for w, q in samples)) for a in range(3)])
        pose_records.append({"id": pose_id, "joint_deltas": deltas, "expected_um": expected})

    def physical(table):
        return {n: [a*UM for a in p] for n, p in table.items()}

    fixture = {"schema": "af.skin-anatomy-fixture/v2",
               "frame": "gltf_Y_up_right_handed",
               "vertices_um": [[a*UM for a in p] for p in points],
               "allowed_joints": [sorted(row) for row in allowed], "tetrahedra": tets,
               "joint_origins": physical(origins), "joint_parents": parents,
               "leaf_tails_um": physical(tails),
               "symmetric_pairs": [["left_"+n, "right_"+n] for n in
                                   ("shoulder", "elbow", "hand", "hip", "knee", "foot")],
               "poses": pose_records}
    return fixture, boundary, weights


def export(output):
    import bpy
    from mathutils import Matrix

    fixture, boundary, weights = author()

    def blender_xyz(um):
        x, y, z = (v / PPM for v in um)
        return (x, -z, y)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm_data = bpy.data.armatures.new("Fixture skeleton")
    arm = bpy.data.objects.new("Fixture skeleton", arm_data)
    bpy.context.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    parents = fixture["joint_parents"]
    for name, pos in fixture["joint_origins"].items():
        bone = arm_data.edit_bones.new(name)
        bone.head = blender_xyz(pos)
        # Uniform rest axes on all animated bones. A nontrivial leaf bind
        # rotation gives the affine-only inverse-bind transpose a witness.
        x, y, z = pos
        bone.tail = blender_xyz((x, y+UM, z))
        if name == "left_foot":
            bone.roll = pi / 3
        if parents[name] is not None:
            bone.parent = arm_data.edit_bones[parents[name]]
    bpy.ops.object.mode_set(mode="OBJECT")
    data = bpy.data.meshes.new("Humanoid surface")
    data.from_pydata([blender_xyz(p) for p in fixture["vertices_um"]], [], boundary)
    data.update()
    obj = bpy.data.objects.new("Humanoid", data)
    bpy.context.collection.objects.link(obj)
    obj.parent = arm
    modifier = obj.modifiers.new("Fixture LBS", "ARMATURE")
    modifier.object = arm
    for name in fixture["joint_origins"]:
        group = obj.vertex_groups.new(name=name)
        for index, row in enumerate(weights):
            if name in row:
                group.add([index], row[name], "REPLACE")
    for polygon in data.polygons:
        polygon.use_smooth = True
    bpy.ops.export_scene.gltf(filepath=str(output / "humanoid.glb"), export_format="GLB",
                              export_animations=False, export_yup=True, export_draco_mesh_compression_enable=False)
    # Independently exercise Blender's CPU armature evaluation at every
    # authored pose, including the mixed-weight sections near the joints.
    errors = {}
    for pose in fixture["poses"]:
        for bone in arm.pose.bones:
            bone.matrix_basis = Matrix.Identity(4)
        bpy.context.view_layer.update()
        for entry in pose["joint_deltas"]:
            values = entry["matrix_ppm"]
            delta = Matrix(tuple(tuple(values[c*4+r]/PPM for c in range(4)) for r in range(4)))
            bone = arm.pose.bones[entry["joint"]]
            bone.matrix_basis = delta
        bpy.context.view_layer.update()
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        errors[pose["id"]] = max(
            ceil(dist(tuple(v*PPM for v in (p.co.x, p.co.z, -p.co.y)), wanted))
            for p, wanted in zip(mesh.vertices, pose["expected_um"], strict=True))
        evaluated.to_mesh_clear()
    (output / "blender-pose-errors.json").write_text(json.dumps(errors, sort_keys=True) + "\n")
    (output / "anatomy.json").write_text(
        json.dumps(fixture, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    export(Path(sys.argv[sys.argv.index("--") + 1]))
