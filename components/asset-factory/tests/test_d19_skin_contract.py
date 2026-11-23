"""Exact arithmetic and the protected Blender fixture at the skin boundary."""
from collections import defaultdict
import copy
from fractions import Fraction
import json
import os
import struct
from pathlib import Path
import unittest

from asset_factory import core, math_acceptance
from providers.unirig import gate
from providers.unirig.common import ROOT, load, validate
from tools import rig_math
from test_w3_rig_boundary import pack_glb, rig_fixture


def changed_glb(data, mutate):
    glb = gate.GLB(data, 10**30, 10000)
    binary = bytearray(glb.binary)
    mutate(glb.doc, binary)
    return pack_glb(glb.doc, binary)


def accessor_rows(doc, index):
    accessor = doc["accessors"][index]
    view = doc["bufferViews"][accessor["bufferView"]]
    component = {5121: "B", 5123: "H", 5126: "f"}[accessor["componentType"]]
    width = {"VEC3": 3, "VEC4": 4, "MAT4": 16}[accessor["type"]]
    fmt = "<" + component * width
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = view.get("byteStride", struct.calcsize(fmt))
    return fmt, range(start, start + stride * accessor["count"], stride)


def modify_accessor(doc, binary, index, edit):
    fmt, offsets = accessor_rows(doc, index)
    for i, offset in enumerate(offsets):
        row = struct.unpack_from(fmt, binary, offset)
        struct.pack_into(fmt, binary, offset, *edit(i, row))


def primitive_attrs(doc):
    return doc["meshes"][0]["primitives"][0]["attributes"]


def append_vec4(doc, binary, name, component, rows):
    fmt = "<4" + {5123: "H", 5126: "f"}[component]
    raw = b"".join(struct.pack(fmt, *row) for row in rows)
    view = len(doc["bufferViews"])
    doc["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(raw)})
    binary.extend(raw)
    doc["buffers"][0]["byteLength"] = len(binary)
    primitive_attrs(doc)[name] = len(doc["accessors"])
    doc["accessors"].append({"bufferView": view, "componentType": component,
                             "count": len(rows), "type": "VEC4"})


def inverse_affine(matrix):
    a, b, c = (matrix[i:i+3] for i in (0, 4, 8))
    rows = (rig_math.cross(b, c), rig_math.cross(c, a), rig_math.cross(a, b))
    determinant = rig_math.dot(a, rows[0])
    return tuple(Fraction(rows[r][i], determinant) if r < 3 else 0
                 for i in range(3) for r in range(4)) + tuple(
                     -Fraction(rig_math.dot(row, matrix[12:15]), determinant) for row in rows) + (1,)


def compensate_binds(doc, binary):
    glb = gate.GLB(pack_glb(doc, binary), 10**30, 10000, exact=True)
    _, worlds = glb.hierarchy()
    skin = doc["skins"][0]
    mesh = next(i for i, node in enumerate(doc["nodes"]) if "mesh" in node)
    modify_accessor(doc, binary, skin["inverseBindMatrices"], lambda i, _: gate.mul(
        inverse_affine(worlds[skin["joints"][i]]), worlds[mesh]))


class SkinFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (directory := os.getenv("SKIN_FIXTURE_DIR")):
            raise unittest.SkipTest("CPU Blender fixture: set SKIN_FIXTURE_DIR after running the author script")
        root = Path(directory)
        cls.data = (root / "humanoid.glb").read_bytes()
        cls.fixture = json.loads((root / "anatomy.json").read_bytes())
        cls.policy = load(ROOT / "gate-policy.json")

    def check(self, expected, reason=None, data=None, mutate=None, role="skin-fixture",
              trusted=True, bad_pin=False, settings=None):
        fixture = copy.deepcopy(self.fixture)
        if mutate:
            mutate(fixture)
        raw = core.canonical_json(fixture).encode()
        pin = math_acceptance.canonical_digest(fixture)
        if bad_pin:
            pin = "sha256:" + "0" * 64
        policy = copy.deepcopy(self.policy)
        if settings:
            policy["skin_fixture"].update(settings)
        receipt = gate.inspect_skin_bytes(
            self.data if data is None else data, policy=policy, fixture_digest=pin,
            read_checked=lambda name, _: raw if trusted and name == role else None,
        )
        validate(receipt, "skin_gate")
        if directory := os.getenv("SKIN_CONTROLS_DIR"):
            suffix = self._testMethodName
            if reason:
                suffix += "-" + reason
            if role != "skin-fixture":
                suffix += "-wrong-role"
            (Path(directory) / (suffix + ".json")).write_text(core.canonical_json(receipt))
        self.assertEqual(receipt["rc"], expected, receipt)
        if reason:
            self.assertIn(reason, receipt["reason_codes"])
        return receipt

    def test_full_pose_suite(self):
        receipt = self.check(0)
        metrics = receipt["metrics"]
        self.assertEqual(metrics["pose_coverage_lower_ppm"], 1000000)
        self.assertEqual(metrics["deformation_intersections"], 0)
        self.assertGreater(metrics["posed_vertex_count"], metrics["vertices"])
        self.assertGreater(metrics["bone_segment_count"], 0)
        self.assertGreater(metrics["minimum_jacobian_lower_ppm"], 0)
        witness = Path(os.environ["SKIN_FIXTURE_DIR"]) / "blender-pose-errors.json"
        errors = json.loads(witness.read_text())
        self.assertEqual(set(errors), set(receipt["checked_pose_ids"]))
        self.assertLessEqual(max(errors.values()), self.policy["skin_fixture"]["pose_error_um"])
        if log := os.getenv("SKIN_RESULT_FILE"):
            Path(log).write_text(core.canonical_json(receipt))

    def test_skeleton_only(self):
        self.check(5, "skinned_mesh_missing", data=changed_glb(
            self.data, lambda doc, _: [n.pop("mesh", None) for n in doc["nodes"]]))

    def test_invalid_weights(self):
        self.check(5, "weight_sum", data=rig_fixture(weights=[(.25, 0, 0, 0)] * 3))

    def test_p8_blender_weights_are_normalized(self):
        def unnormalize(doc, binary):
            modify_accessor(doc, binary, primitive_attrs(doc)["WEIGHTS_0"],
                            lambda i, row: tuple(w / 2 for w in row) if i == 0 else row)

        self.check(5, "weight_sum", data=changed_glb(self.data, unnormalize))

    def transpose_binds(self, affine_row_preserved):
        def transpose(doc, binary):
            def edit(_, row):
                return tuple(row[r*4+c] if not affine_row_preserved or r < 3 and c < 3
                             else row[c*4+r] for c in range(4) for r in range(4))

            modify_accessor(doc, binary, doc["skins"][0]["inverseBindMatrices"], edit)

        return changed_glb(self.data, transpose)

    def test_p1_transposed_blender_inverse_binds(self):
        self.check(5, "matrix_not_affine", data=self.transpose_binds(False))

    def test_p1b_transposed_blender_bind_rotation(self):
        self.check(5, "bind_pose_mismatch", data=self.transpose_binds(True))

    def test_p4_compensated_nonuniform_armature(self):
        def scale(doc, binary):
            def hierarchy():
                return gate.GLB(pack_glb(doc, binary), 10**30, 10000, exact=True).hierarchy()

            parents, rest = hierarchy()
            root = doc["scenes"][doc.get("scene", 0)]["nodes"][0]
            doc["nodes"][root]["scale"] = [2, 1, 1]
            joints = set(doc["skins"][0]["joints"])
            while joints:
                i = min(i for i in joints if parents[i] not in joints)
                _, scaled = hierarchy()
                translation = gate.point(inverse_affine(scaled[parents[i]]), rest[i][12:15])
                doc["nodes"][i]["translation"] = list(map(float, translation))
                joints.remove(i)
            modify_accessor(doc, binary, primitive_attrs(doc)["POSITION"],
                            lambda _, p: (p[0] / 2, *p[1:]))
            compensate_binds(doc, binary)

        self.check(5, "skin_joint_world_not_rigid", data=changed_glb(self.data, scale))

    def test_p2_influence_cap_counts_all_sets(self):
        def extra(doc, binary):
            attrs = primitive_attrs(doc)
            original = attrs["WEIGHTS_0"]
            modify_accessor(doc, binary, original,
                            lambda i, row: (.125, .125, .125, .125) if i == 0 else row)
            count = doc["accessors"][original]["count"]
            append_vec4(doc, binary, "JOINTS_1", 5123, [(4, 5, 0, 0)] + [(0,)*4]*(count-1))
            append_vec4(doc, binary, "WEIGHTS_1", 5126, [(.25, .25, 0, 0)] + [(0,)*4]*(count-1))
            modify_accessor(doc, binary, attrs["JOINTS_0"],
                            lambda i, row: (0, 1, 2, 3) if i == 0 else row)

        self.check(5, "skin_influence_cap", data=changed_glb(self.data, extra))

    def test_fixture_has_anatomical_parent_chain(self):
        parents = self.fixture["joint_parents"]
        self.assertEqual(parents["face"], "neck")
        self.assertEqual(parents["left_hand"], "left_elbow")
        self.assertEqual(parents["left_elbow"], "left_shoulder")
        self.assertEqual(parents["left_foot"], "left_knee")
        self.assertEqual(parents["left_knee"], "left_hip")

    def test_candidate_authored_or_changed_authority(self):
        self.check(3, "skin_fixture_unavailable", trusted=False)
        self.check(3, "skin_fixture_unavailable", role="candidate")
        self.check(4, "skin_fixture_stale", bad_pin=True)

    def test_previous_world_delta_fixture_requires_reauthoring(self):
        self.check(3, "skin_fixture_local_pose_required",
                   mutate=lambda f: f.update(schema="af.skin-anatomy-fixture/v1"))

    def test_anatomical_hierarchy_remains_bound(self):
        def change_parent(fixture):
            fixture["joint_parents"]["left_elbow"] = "right_elbow"

        self.check(5, "skin_anatomy_joint_hierarchy", mutate=change_parent)

    def test_missing_pose(self):
        self.check(3, "skin_pose_coverage_missing", mutate=lambda f: f["poses"].pop())

    def test_missing_reference_vertex(self):
        self.check(3, "skin_pose_vertices_missing", mutate=lambda f: f["poses"][0]["expected_um"].pop())

    def test_pose_displacement(self):
        def shift(fixture):
            fixture["poses"][0]["expected_um"][0][0] += 10000
        self.check(5, "skin_pose_displacement", mutate=shift)

    def test_exact_placement_keeps_micrometre_pose_limit(self):
        def shift(fixture):
            pose = next(p for p in fixture["poses"] if p["id"] == "elbows")
            pose["expected_um"][0][0] += 3

        receipt = self.check(5, "skin_pose_displacement", mutate=shift)
        self.assertEqual(receipt["metrics"]["joint_origin_error_upper_um"], 0)
        self.assertEqual(receipt["metrics"]["pose_error_upper_um"], 3)

    def test_shared_seams_form_one_closed_surface(self):
        glb = gate.GLB(self.data, 10**30, 10000)
        primitive = glb.doc["meshes"][0]["primitives"][0]
        indices, _ = glb.accessor(primitive["indices"], "SCALAR", (5121, 5123, 5125))
        positions, _ = glb.accessor(primitive["attributes"]["POSITION"], "VEC3", (5126,))
        edges = defaultdict(list)
        for i, (a, b, c) in enumerate(zip(indices[::3], indices[1::3], indices[2::3], strict=True)):
            for src, dst in ((a[0], b[0]), (b[0], c[0]), (c[0], a[0])):
                edges[tuple(sorted((src, dst)))].append(i)
        self.assertEqual({len(faces) for faces in edges.values()}, {2})
        adjacent = defaultdict(set)
        for a, b in edges.values():
            adjacent[a].add(b)
            adjacent[b].add(a)
        pending, seen = [0], set()
        while pending:
            face = pending.pop()
            if face not in seen:
                seen.add(face)
                pending.extend(adjacent[face] - seen)
        self.assertEqual(len(seen), len(indices) // 3)
        self.assertEqual(len({index for index, in indices}), len(positions))
        for seam in ((0, 3, .25), (0, 3.375, .25),
                     (1.625, 2.5, .25), (2.125, 2.5, .25),
                     (.375, 1.5, .25), (.375, 1, .25), (.375, .5, .25)):
            self.assertEqual(positions.count(seam), 1)

    def reweight(self, assignments):
        glb = gate.GLB(self.data, 10**30, 10000)
        positions, _ = glb.accessor(primitive_attrs(glb.doc)["POSITION"], "VEC3", (5126,))
        names = [glb.doc["nodes"][j]["name"] for j in glb.doc["skins"][0]["joints"]]
        replacements = {positions.index(p): names.index(name) for p, name in assignments.items()}

        def edit(doc, binary):
            attrs = primitive_attrs(doc)
            modify_accessor(doc, binary, attrs["JOINTS_0"],
                            lambda i, row: (replacements[i], 0, 0, 0) if i in replacements else row)
            modify_accessor(doc, binary, attrs["WEIGHTS_0"],
                            lambda i, row: (1, 0, 0, 0) if i in replacements else row)

        return changed_glb(self.data, edit)

    def move_heads(self, changes):
        def edit(doc, binary):
            for node in doc["nodes"]:
                if node.get("name") in changes:
                    delta = changes[node["name"]]
                    node["translation"] = [x + y for x, y in zip(node["translation"], delta)]
            compensate_binds(doc, binary)

        return changed_glb(self.data, edit)

    def test_elbow_collapse(self):
        # The bent forearm's lower corner rotates onto the upper corner.
        data = self.reweight({(1.625, 2.5, -.25): "left_elbow",
                              (1.625, 3.0, -.25): "left_shoulder"})
        self.check(5, "skin_tet_inverted_or_collapsed", data=data)

    def test_bone_crosses_air_between_inside_endpoints(self):
        # Move the actual child from the wrist to inside the torso. The
        # elbow-to-child segment leaves the arm before it enters the torso.
        data = self.move_heads({"left_hand": (-1.625, -.75, 0)})
        self.check(5, "skin_bone_outside", data=data)

    def test_candidate_heads_can_differ_within_the_placement_budget(self):
        cases = (
            ("left_elbow", (0, .001, 0)),
            ("left_elbow", (.001, 0, 0)),
            ("left_shoulder", (0, 0, .001)),
            ("left_knee", (0, .001, 0)),
            ("torso", (0, .001, 0)),
        )
        for name, delta in cases:
            with self.subTest(joint=name, offset=delta):
                receipt = self.check(0, data=self.move_heads({name: delta}))
                self.assertGreater(receipt["metrics"]["pose_error_upper_um"],
                                   self.policy["skin_fixture"]["pose_error_um"])

    def test_posed_joint_at_placement_limit(self):
        # Move both elbows equally to isolate placement from bilateral symmetry.
        data = self.move_heads({n: (0, .0625, 0) for n in ("left_elbow", "right_elbow")})
        receipt = self.check(0, data=data)
        self.assertEqual(receipt["metrics"]["joint_origin_error_upper_um"], 62500)
        self.assertGreater(receipt["metrics"]["pose_error_upper_um"], 1)

    def test_contained_candidate_heads_still_obey_the_placement_budget(self):
        data = self.move_heads({n: (0, .0625 + 2**-20, 0) for n in ("left_elbow", "right_elbow")})
        receipt = self.check(5, "skin_joint_origin_mismatch", data=data)
        self.assertEqual(receipt["metrics"]["joint_origin_error_upper_um"], 62501)

    def test_placement_slack_does_not_cross_branches(self):
        def shift(fixture):
            other_branch = fixture["allowed_joints"].index(["torso"])
            pose = next(p for p in fixture["poses"] if p["id"] == "elbows")
            pose["expected_um"][other_branch][0] += 3

        data = self.move_heads({"left_elbow": (0, .001, 0)})
        self.check(5, "skin_pose_displacement", data=data, mutate=shift)

    def test_leaf_tail_is_required_but_cannot_override_child_segment(self):
        self.check(3, "skin_bone_coverage_missing",
                   mutate=lambda f: f["leaf_tails_um"].update(left_elbow=[0, 0, 0]))

    def test_wrong_limb_weights(self):
        def swap(doc, binary):
            skin = doc["skins"][0]
            lookup = [doc["nodes"][j]["name"] for j in skin["joints"]]
            left = lookup.index("left_elbow")
            right = lookup.index("right_elbow")
            for mesh in doc["meshes"]:
                for primitive in mesh["primitives"]:
                    a = doc["accessors"][primitive["attributes"]["JOINTS_0"]]
                    v = doc["bufferViews"][a["bufferView"]]
                    fmt = {5121: "B", 5123: "H"}[a["componentType"]]
                    size = struct.calcsize(fmt)
                    for index in range(a["count"]):
                        pos = (v.get("byteOffset", 0) + a.get("byteOffset", 0)
                               + index * v.get("byteStride", 4 * size))
                        row = struct.unpack_from("<" + fmt * 4, binary, pos)
                        struct.pack_into("<" + fmt * 4, binary, pos,
                                         *(right if j == left else j for j in row))

        self.check(5, "skin_locality_leak", data=changed_glb(self.data, swap))

    def test_bad_inverse_bind(self):
        def singular(doc, binary):
            accessor = doc["accessors"][doc["skins"][0]["inverseBindMatrices"]]
            view = doc["bufferViews"][accessor["bufferView"]]
            start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
            struct.pack_into("<12f", binary, start, *([0] * 12))

        self.check(5, "singular_bind_matrix", data=changed_glb(self.data, singular))

    def test_symmetry_uses_positive_certified_lengths(self):
        def asymmetry(fixture):
            fixture["leaf_tails_um"]["left_hand"][0] += 31250

        self.check(5, "skin_bone_asymmetry", mutate=asymmetry)

    def test_clearance_envelope_fails_for_inside_bone_too_near_surface(self):
        self.check(5, "skin_bone_clearance", settings={"clearance_um": 200000})

    def test_stretch_limit_detects_loss_even_without_a_collapse(self):
        self.check(5, "skin_tet_stretch", settings={"stretch_min_ppm": 1000000})

    def test_volume_limit_detects_per_part_loss(self):
        self.check(5, "skin_deformation_volume", settings={"volume_change_max_ppm": 0})

    def test_packing_limit_checks_pose_displacement(self):
        self.check(5, "skin_pack_displacement", settings={"pack_error_um": 0})

    def test_alternating_neighbour_weights_stay_local_but_fail_gradient(self):
        data = self.reweight({(1.125, 2.5, -.25): "left_elbow"})
        receipt = self.check(5, "skin_edge_gradient", data=data)
        self.assertEqual(receipt["metrics"]["locality_leak_upper_ppm"], 0)

    def test_unpaired_influence_set_is_malformed(self):
        def extra(doc, _):
            attrs = primitive_attrs(doc)
            attrs["JOINTS_1"] = attrs["JOINTS_0"]

        self.check(2, "skin_influence_sets_invalid", data=changed_glb(self.data, extra))

    def test_normalized_influences_split_across_sets(self):
        def split(doc, binary):
            attrs = primitive_attrs(doc)
            index = attrs["WEIGHTS_0"]
            fmt, offsets = accessor_rows(doc, index)
            original = [struct.unpack_from(fmt, binary, offset) for offset in offsets]
            secondary = [(0, row[1], 0, 0) for row in original]
            modify_accessor(doc, binary, index, lambda _, row: (row[0], 0, row[2], row[3]))
            attrs["JOINTS_1"] = attrs["JOINTS_0"]
            append_vec4(doc, binary, "WEIGHTS_1", 5126, secondary)

        self.check(0, data=changed_glb(self.data, split))

    def test_work_budget_is_inconclusive(self):
        self.check(6, "skin_fixture_work_budget", settings={"work_budget": 1})

    def test_pose_translation_is_rejected(self):
        def translate(fixture):
            pose = next(p for p in fixture["poses"] if p["id"] == "hands")
            pose["joint_deltas"][0]["matrix_ppm"][12] = 1

        self.check(5, "skin_pose_not_rigid", mutate=translate)

    def test_anatomical_shoulder_rotation_intersects_the_torso(self):
        def adduct(fixture):
            pose = next(p for p in fixture["poses"] if p["id"] == "elbow_flexion")
            matrices = {entry["joint"]: entry["matrix_ppm"] for entry in pose["joint_deltas"]}
            for joint, cosine, sine in (("left_shoulder", 280000, -960000),
                                        ("left_elbow", 960000, -280000)):
                matrices[joint][:] = [cosine, sine, 0, 0, -sine, cosine, 0, 0,
                                      0, 0, 1000000, 0, 0, 0, 0, 1000000]

        self.check(5, "skin_tet_intersection", mutate=adduct)


class RigMathTests(unittest.TestCase):
    def test_equal_remainders_and_exact_quota(self):
        weights = {"right": Fraction(1, 2), "left": Fraction(1, 2)}
        self.assertEqual(rig_math.pack_weights(weights, 4, 255),
                         ({"left": 128, "right": 127}, 0, Fraction(1, 255)))

    def test_pruned_long_lever_cannot_hide_behind_small_discarded_mass(self):
        weights = {j: Fraction(w, 100) for j, w in (("a", 50), ("b", 25), ("c", 20), ("tip", 5))}
        quotas, removed, quantization = rig_math.pack_weights(weights, 3, 255)
        self.assertEqual(sum(quotas.values()), 255)
        self.assertNotIn("tip", quotas)
        self.assertEqual(removed, Fraction(1, 20))
        bound = rig_math.displacement_squared(
            [(0, 0, 0), (0, 0, 0), (0, 0, 0), (2000000, 0, 0)], removed, quantization)
        self.assertGreaterEqual(bound, 100000**2)
