"""F3 film-animation oracles: stdlib only, no bpy. Every new check has a witness and a source mutant.

Real-sequence fixtures under tests/golden/vqa3d_film/ are integer samples recorded live
from take 1827 and from the reconstructed take-0448 NLA defect; they are replayed, never
re-measured, here.
"""
import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from vqa3d import profile as p
from vqa3d.adapters import blender_side
from tests.test_vqa3d_profiles import Scene, selected

GOLDEN = Path(__file__).with_name("golden") / "vqa3d_film"
FOOT = "/objects/foot.L/world_bbox_min_um/2"
HEAD = "/objects/Rig.a/bones/foot.L/head_world_um"
TAIL = "/objects/Rig.a/bones/foot.L/tail_world_um"
QUAT = "/objects/Rig.a/bones/hand.R/quat_local_ppm"
PROP = "/objects/prop~1x~0/world_location_um"


def mutated(module, old, new, symbol):
    source = inspect.getsource(module)
    if old not in source:
        raise AssertionError("mutant_anchor_absent:" + old)
    namespace = dict(vars(module))
    exec(compile(source.replace(old, new, 1), module.__file__, "exec"), namespace)
    return namespace[symbol]


class Shot(Scene):
    """Scene whose sampled leaves can vary per frame: timeline[path][frame]."""

    def __init__(self, frames=(1, 3), **kwargs):
        super().__init__(**kwargs)
        self.state.update(frame_start=frames[0], frame_end=frames[1])
        self.timeline = {}

    def sample(self, frames, paths):
        self.samples.append((frames, paths))
        return {str(f): {path: copy.deepcopy(self.timeline.get(path, {}).get(f, p.lookup(self.state, path)))
                         for path in paths} for f in frames}


class Replay(Shot):
    """Replays a recorded real sequence: {"frames": [a, b], "state": {...}, "timeline": {path: [values]}}."""

    def __init__(self, name):
        data = json.loads((GOLDEN / name).read_text())
        super().__init__(tuple(data["frames"]))
        self.state = {**data["state"], "frame_start": data["frames"][0], "frame_end": data["frames"][1],
                      "frame_current": data["frames"][0]}
        first = data["frames"][0]
        self.timeline = {path: {first + i: v for i, v in enumerate(values)} for path, values in data["timeline"].items()}

    def sample(self, frames, paths):
        self.samples.append((frames, paths))
        return {str(f): {path: self.timeline[path][f] for path in paths if path in self.timeline} for f in frames}


def one(profile_id, check, adapter, **kwargs):
    receipts, result, _ = p.run(selected(p.load(profile_id), check), adapter, **kwargs)
    return receipts[1:], result


def reason(receipt):
    return receipt["verdicts"][0]["reason"]


class Resolution(unittest.TestCase):
    def objects(self):
        return Scene().state["objects"]

    def test_mesh_foot_carries_its_parent_bone_and_roles_override_names(self):
        objects = self.objects()
        feet = p.resolve_feet(objects, "(?i)(foot|toe)")
        self.assertEqual([(f["label"], f["source"], f["bones"]) for f in feet], [("foot.L", "mesh", [["Rig.a", "foot.L"]])])
        objects["foot.L"]["role"] = "anatomy"          # declared not-a-foot: the bone is no longer covered
        self.assertEqual([(f["label"], f["source"]) for f in p.resolve_feet(objects, "(?i)(foot|toe)")],
                         [("Rig.a:foot.L", "bone")])
        objects["Slipper"] = {"type": "MESH", "role": "foot", "world_bbox_min_um": [0, 0, 0]}
        self.assertIn(("Slipper", "mesh", []), [(f["label"], f["source"], f["bones"]) for f in p.resolve_feet(objects, "(?i)(foot|toe)")])

    def test_unified_mesh_uses_skin_groups_and_uncovered_bones_stay_visible(self):
        objects = {"Body": {"type": "MESH", "skin_armature": "Rig", "vertex_groups": {
                       "foot.L": {"world_bbox_min_um": [0, 0, 10], "world_bbox_max_um": [1, 1, 1], "verts": 4},
                       "head": {"world_bbox_min_um": [0, 0, 9], "world_bbox_max_um": [1, 1, 1], "verts": 4}}},
                   "Rig": {"type": "ARMATURE", "bones": {"foot.L": {"parent": "shin.L"}, "foot.R": {"parent": "shin.R"}, "head": {},
                                                         "toe.L": {"parent": "foot.L"}, "toe.R": {"parent": "foot.R"}}}}
        feet = p.resolve_feet(objects, "(?i)(foot|toe)")
        # Left: skinned heel geometry; its toe bone has no owned vertices and only adds a slide segment.
        # Right: nothing but joints, so the foot stays visible as a bone-level limb.
        self.assertEqual([(f["label"], f["source"], f["bones"]) for f in feet],
                         [("Body:foot.L", "skin", [["Rig", "foot.L"], ["Rig", "toe.L"]]),
                          ("Rig:foot.R", "bone", [["Rig", "foot.R"], ["Rig", "toe.R"]])])
        self.assertEqual(feet[0]["z"], ["/objects/Body/vertex_groups/foot.L/world_bbox_min_um/2"])
        self.assertEqual(len(feet[1]["z"]), 4)
        objects["Body"]["vertex_groups"]["toe.L"] = {"world_bbox_min_um": [0, 0, 1], "world_bbox_max_um": [1, 1, 1], "verts": 2}
        merged = p.resolve_feet(objects, "(?i)(foot|toe)")[0]
        self.assertEqual((merged["label"], len(merged["z"])), ("Body:foot.L", 2))   # heel and toe are one foot
        # Mutant: stopping at the first rung would silently drop the uncovered right foot.
        broken = mutated(p, "if re.search(foot_regex, bone) and (name, bone) not in covered:", "if False:", "resolve_feet")
        self.assertEqual(len(broken(objects, "(?i)(foot|toe)")), 1)

    def test_floor_is_single_declared_or_named_and_never_guessed(self):
        objects = self.objects()
        self.assertEqual(p.resolve_floor(objects, "(?i)(floor|ground)"), "Floor")
        objects["Floor_lamp"] = {"type": "MESH"}
        with self.assertRaisesRegex(p.CannotSee, "floor_ambiguous:Floor,Floor_lamp"):
            p.resolve_floor(objects, "(?i)(floor|ground)")
        objects["Floor"]["role"] = "floor"
        self.assertEqual(p.resolve_floor(objects, "(?i)(floor|ground)"), "Floor")
        del objects["Floor"], objects["Floor_lamp"]
        with self.assertRaisesRegex(p.CannotSee, "floor_unresolved"):
            p.resolve_floor(objects, "(?i)(floor|ground)")

    def test_prop_rule_name_collection_and_declared_role(self):
        regex = p.load("film-animation")["defaults"]["attachment"]["prop_regex"]
        self.assertTrue(p.is_prop("PROP_mallet", {}, regex))
        self.assertTrue(p.is_prop("mallet", {"collections": ["CHAR", "Props"]}, regex))
        self.assertTrue(p.is_prop("mallet", {"role": "prop"}, regex))
        for name in ("CHAR_head", "MAN_baton_proxy", "property_box", "CHAR_shoe_L"):
            self.assertFalse(p.is_prop(name, {"collections": ["CHAR"]}, regex), name)
        self.assertFalse(p.is_prop("PROP_wig", {"role": "anatomy"}, regex))


class FeetFloor(unittest.TestCase):
    def test_clearance_is_relative_to_the_floor_surface(self):
        adapter = Shot()
        adapter.state["objects"]["Floor"]["world_bbox_max_um"][2] = 2000
        adapter.timeline[FOOT] = {1: 5000, 2: 2263, 3: 5000}
        receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertEqual(reason(receipts[0]), "feet_clear:foot.L:frame=2:clearance_um=263:floor=Floor:source=mesh")
        adapter.timeline[FOOT][3] = 1999
        receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertEqual(reason(receipts[0]), "foot_below_floor:foot.L:frame=3:clearance_um=-1:floor=Floor:source=mesh")
        # Mutant: world z instead of floor-relative clearance passes a foot under a raised floor.
        broken = mutated(p, ' - integer(row[obligation["floor_path"]])', "", "_feet_floor")
        with patch.object(p, "_feet_floor", broken):
            self.assertEqual(one("film-animation", "feet-floor", adapter)[1]["final_verdict"], "PASS")

    def test_missing_floor_and_missing_sample_are_unobservable(self):
        adapter = Shot()
        adapter.state["objects"].pop("Floor")
        receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual((result["final_verdict"], receipts[0]["error"]), ("CANNOT_SEE", "floor_unresolved:declare_vqa3d_role_floor_or_name"))
        adapter = Shot()
        with patch.object(adapter, "sample", return_value={"1": {}, "2": {}, "3": {}}):
            receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual((result["final_verdict"], receipts[0]["error"]), ("CANNOT_SEE", "state_path_absent:" + FOOT))

    def test_bone_only_feet_prove_penetration_but_never_clearance(self):
        adapter = Shot()
        adapter.state["objects"].pop("foot.L")
        receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual(result["final_verdict"], "CANNOT_SEE")
        self.assertIn("clearance_um=10000", receipts[0]["error"])
        adapter.timeline[TAIL + "/2"] = {2: -30000}
        receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertEqual(reason(receipts[0]), "foot_below_floor:Rig.a:foot.L:frame=2:clearance_um=-30000:floor=Floor:source=bone")
        # Mutant: letting joints certify sole clearance is a false PASS.
        adapter.timeline.clear()
        broken = mutated(p, 'if obligation["source"] == "bone":\n        # Joint positions', 'if False:\n        # Joint positions', "_feet_floor")
        with patch.object(p, "_feet_floor", broken):
            self.assertEqual(one("film-animation", "feet-floor", adapter)[1]["final_verdict"], "PASS")

    def test_real_take_1827_feet(self):
        expected = {"take1827_motion.json": ("FAIL", "foot_below_floor:MAN_foot_L:frame=98:clearance_um=-36098"),
                    "take1827_motion_v2.json": ("PASS", "feet_clear:MAN_foot_L:frame=1:clearance_um=4500"),
                    "take1827_final_scene_v2.json": ("PASS", "feet_clear:CHAR_shoe_L:frame=1:clearance_um=3000")}
        for name, (verdict, start) in expected.items():
            with self.subTest(scene=name):
                receipts, _ = one("film-animation", "feet-floor", Replay(name))
                self.assertEqual(receipts[0]["final_verdict"], verdict)
                self.assertTrue(reason(receipts[0]).startswith(start), reason(receipts[0]))
        right = one("film-animation", "feet-floor", Replay("take1827_final_scene_v2.json"))[0][1]
        self.assertTrue(reason(right).startswith("feet_clear:CHAR_shoe_R:frame=175:clearance_um=263"), reason(right))


class FootSlide(unittest.TestCase):
    def planted(self, frames=(1, 6)):
        adapter = Shot(frames)
        adapter.state["objects"]["foot.L"]["world_bbox_min_um"][2] = 3000
        return adapter

    def test_planted_foot_holds_drags_pivots_and_lifts(self):
        adapter = self.planted()
        receipts, result = one("film-animation", "foot-slide", adapter)
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertEqual(reason(receipts[0]), "planted_feet_hold:foot.L:frames=1-6:drift_um=0:max_um=40000:planted_runs=1:plant_max_um=6000")
        # Dragged: the whole foot translates 12 mm per frame while resting on the floor.
        adapter.timeline[HEAD] = {f: [12000*f, 0, 80000] for f in range(1, 7)}
        adapter.timeline[TAIL] = {f: [12000*f, -150000, 10000] for f in range(1, 7)}
        receipts, result = one("film-animation", "foot-slide", adapter)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertEqual(reason(receipts[0]), "foot_slide:foot.L:frames=1-6:drift_um=60000:max_um=40000:planted_runs=1:plant_max_um=6000")
        # The same travel with the foot lifted for it is a step, not a slide.
        adapter.timeline[FOOT] = {f: (3000 if f in (1, 6) else 50000) for f in range(1, 7)}
        receipts, result = one("film-animation", "foot-slide", adapter)
        self.assertEqual((result["final_verdict"], receipts[0]["error"].split(":")[0]), ("CANNOT_SEE", "foot_never_planted"))
        # Pivot about the toe: the ankle swings 100 mm, the toe stays, nothing slides.
        adapter = self.planted()
        adapter.timeline[HEAD] = {f: [20000*f, 0, 80000] for f in range(1, 7)}
        receipts, result = one("film-animation", "foot-slide", adapter)
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertIn("drift_um=0", reason(receipts[0]))
        # Mutant: tracking the ankle only would call that pivot a 100 mm slide.
        broken = mutated(p, "for k in range(SLIDE_CANDIDATES + 1):", "for k in range(1):", "_foot_slide")
        with patch.object(p, "_foot_slide", broken):
            self.assertEqual(one("film-animation", "foot-slide", adapter)[1]["final_verdict"], "FAIL")

    def test_contact_band_bone_and_sampling_preconditions(self):
        adapter = self.planted()
        adapter.timeline[HEAD] = {f: [12000*f, 0, 80000] for f in range(1, 7)}
        adapter.timeline[TAIL] = {f: [12000*f, -150000, 10000] for f in range(1, 7)}
        # Mutant: a plant band of zero never sees the contact and cannot fail the drag.
        broken = mutated(p, 'planted = value <= obligation["plant_max_um"]', "planted = value <= 0", "_foot_slide")
        self.assertEqual(one("film-animation", "foot-slide", adapter)[1]["final_verdict"], "FAIL")
        with patch.object(p, "_foot_slide", broken):
            self.assertNotEqual(one("film-animation", "foot-slide", adapter)[1]["final_verdict"], "FAIL")
        receipts, result = one("film-animation", "foot-slide", adapter, frames=[1, 5, 2])
        self.assertEqual((result["final_verdict"], receipts[0]["error"]), ("CANNOT_SEE", "foot_slide_requires_step_one"))
        adapter.state["objects"]["foot.L"].update(parent_type="OBJECT", parent_bone="")
        adapter.state["objects"]["Rig.a"]["bones"].pop("foot.L")
        receipts, result = one("film-animation", "foot-slide", adapter)
        self.assertEqual(receipts[0]["error"], "foot_bone_unresolved:foot.L")
        self.assertEqual(result["final_verdict"], "CANNOT_SEE")

    def test_real_take_1827_slide(self):
        got = {}
        for name in ("take1827_motion.json", "take1827_motion_v2.json", "take1827_final_scene_v2.json"):
            receipts, _ = one("film-animation", "foot-slide", Replay(name))
            got[name] = [(r["check_id"].split(":")[1], r["final_verdict"], reason(r)) for r in receipts]
        self.assertEqual([row[1] for row in got["take1827_motion.json"]], ["FAIL", "FAIL"])
        self.assertEqual([row[1] for row in got["take1827_motion_v2.json"]], ["PASS", "PASS"])
        self.assertTrue(all("drift_um=0:" in row[2] for row in got["take1827_motion_v2.json"]))
        # The delivered shot: the left shoe holds, the right shoe is dragged just above the floor.
        left, right = got["take1827_final_scene_v2.json"]
        self.assertEqual((left[0], left[1], right[0], right[1]), ("CHAR_shoe_L", "PASS", "CHAR_shoe_R", "FAIL"))
        self.assertTrue(right[2].startswith("foot_slide:CHAR_shoe_R:frames=9-18:drift_um=267797:"), right[2])


class BoneRotation(unittest.TestCase):
    @staticmethod
    def about_x(mdeg):
        import math
        half = math.radians(mdeg / 1000) / 2
        return [round(math.cos(half) * 1000000), round(math.sin(half) * 1000000), 0, 0]

    def run_angles(self, angles, **kwargs):
        adapter = Shot((1, len(angles)))
        adapter.timeline[QUAT] = {i+1: self.about_x(a) for i, a in enumerate(angles)}
        receipts, _ = one("film-animation", "bone-continuity", adapter, **kwargs)
        return next(r for r in receipts if r["check_id"].endswith("hand.R"))

    def test_quaternion_angle_convention(self):
        self.assertEqual(p.quat_angle_mdeg([1000000, 0, 0, 0], [707107, 707107, 0, 0]), 90000)
        self.assertEqual(p.quat_angle_mdeg([1000000, 0, 0, 0], [-1000000, 0, 0, 0]), 0)        # q and -q
        self.assertEqual(p.quat_angle_mdeg(self.about_x(179000), self.about_x(-179000)), 2000)  # shortest arc
        self.assertEqual(p.quat_angle_mdeg(self.about_x(10000), self.about_x(10100)), 100)      # small angles resolve
        self.assertEqual(p.quat_angle_mdeg([707107, 0, 707107, 0], [707107, 0, -707107, 0]), 180000)

    def test_fast_monotone_motion_passes_and_one_frame_pop_fails(self):
        swing = self.run_angles([0, 20000, 60000, 100000, 120000])           # 40 deg per frame, no return
        self.assertEqual(swing["final_verdict"], "PASS")
        self.assertEqual(reason(swing), "bone_rotation_continuous:max_delta_mdeg=40000:frame=3:max_detour_mdeg=0:detour_frame=2")
        pop = self.run_angles([0, 2000, 47000, 6000, 8000])                   # 45 deg out and back in one frame
        self.assertEqual(pop["final_verdict"], "FAIL")
        self.assertIn("bone_rotation_pop:" + QUAT + ":frame=3:detour_mdeg=82000", reason(pop))
        step = self.run_angles([0, 100000, 200000])
        self.assertIn("bone_rotation_step:" + QUAT + ":frame=2:delta_mdeg=100000", reason(step))
        # Mutant: without the detour term the pop hides under the per-frame ceiling.
        broken = mutated(p, 'if detour > obligation["max_detour_mdeg"]:', "if False:", "_bone_rotation")
        with patch.object(p, "_bone_rotation", broken):
            self.assertEqual(self.run_angles([0, 2000, 47000, 6000, 8000])["final_verdict"], "PASS")

    def test_unobservable_rotation_is_typed(self):
        adapter = Shot()
        adapter.state["objects"]["Rig.a"]["bones"]["hand.R"].pop("quat_local_ppm")
        receipts, result = one("film-animation", "bone-continuity", adapter)
        self.assertEqual(result["final_verdict"], "CANNOT_SEE")
        self.assertIn("state_path_absent:" + QUAT, [r["error"] for r in receipts])
        adapter = Shot()
        adapter.timeline[QUAT] = {2: [500000, 0, 0, 0]}
        receipts, _ = one("film-animation", "bone-continuity", adapter)
        self.assertIn("state_path_invalid:non_unit_quaternion:" + QUAT, [r["error"] for r in receipts])
        self.assertEqual(self.run_angles([0, 1000, 2000], frames=[1, 3, 2])["error"], "continuity_requires_step_one")
        self.assertEqual(self.run_angles([0], frames=[1, 1, 1])["error"], "continuity_needs_adjacent_frames")

    def test_real_take_0448_nla_pop_reconstruction_and_clean_takes(self):
        receipts, result = one("film-animation", "bone-continuity", Replay("take0448_nla_pop.json"))
        failed = {r["check_id"].split(":")[2]: reason(r) for r in receipts if r["final_verdict"] == "FAIL"}
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertTrue({"forearm.R", "upper_arm.R", "upper_arm.L", "forearm.L"} <= set(failed), sorted(failed))
        self.assertIn(":frame=31:detour_mdeg=212000:", failed["forearm.R"])
        for name in ("take0448_nla_fixed.json", "take1827_final_scene_v2.json"):
            receipts, result = one("film-animation", "bone-continuity", Replay(name))
            self.assertEqual((name, result["final_verdict"]), (name, "PASS"))
            self.assertTrue(len(receipts) >= 20)


class Attachment(unittest.TestCase):
    def test_only_declared_props_are_attachments(self):
        adapter = Shot()
        adapter.state["objects"]["CHAR_head"] = {"type": "MESH", "parent_type": "BONE", "parent": "Rig.a", "parent_bone": "hand.R",
                                                 "world_location_um": [0, 0, 1750000], "collections": ["CHAR"], "tris": 1,
                                                 "world_bbox_min_um": [0, 0, 0], "world_bbox_max_um": [1000, 1000, 1000]}
        receipts, result = one("film-animation", "attachments", adapter)
        self.assertEqual([r["check_id"] for r in receipts], ["attachments:prop/x~"])   # anatomy at 1.75 m is not judged
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertEqual(reason(receipts[0]), "attachment_held:max_tail_distance_um=50000:frame=1:max_um=75000:drift_um=0:max_drift_um=1000:frames=3")
        adapter.state["objects"].pop("prop/x~")
        receipts, result = one("film-animation", "attachments", adapter)
        self.assertEqual((result["final_verdict"], receipts[0]["error"]),
                         ("CANNOT_SEE", "selector_empty:attachments:undeclared_bone_parented_meshes=2"))
        adapter.state["objects"]["CHAR_head"]["role"] = "in_grip"
        self.assertEqual(one("film-animation", "attachments", adapter)[1]["final_verdict"], "FAIL")
        # Mutant: structural selection judges anatomy origins as props.
        adapter.state["objects"]["CHAR_head"].pop("role")
        broken = mutated(p, "matches = [(name, row) for name, row in matches if is_prop(name, row, regex)]", "pass", "expand")
        with patch.object(p, "expand", broken):
            wrong = one("film-animation", "attachments", adapter)[0]
            self.assertEqual([r["check_id"] for r in wrong], ["attachments:CHAR_head", "attachments:foot.L"])
            # hand.R is a grip bone, so the mis-selected head is judged as a held prop.
            self.assertTrue(wrong[0]["error"].startswith("attachment_distance_exceeded:"))

    def test_distance_and_slip(self):
        adapter = Shot()
        adapter.timeline[PROP] = {2: [0, 0, 76000]}
        receipts, result = one("film-animation", "attachments", adapter)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertTrue(reason(receipts[0]).startswith("attachment_distance_exceeded:max_tail_distance_um=76000:frame=2"))
        adapter.timeline[PROP] = {2: [0, 0, 60000]}                       # inside the reach, but sliding in the hand
        receipts, result = one("film-animation", "attachments", adapter)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertTrue(reason(receipts[0]).startswith("attachment_slip:max_tail_distance_um=60000:frame=2:max_um=75000:drift_um=10000"))
        broken = mutated(p, 'if drift > obligation["max_drift_um"]:', "if False:", "_attachment")
        with patch.object(p, "_attachment", broken):
            self.assertEqual(one("film-animation", "attachments", adapter)[1]["final_verdict"], "PASS")

    def test_real_take_1827_mallet(self):
        receipts, result = one("film-animation", "attachments", Replay("take1827_final_scene_v2.json"))
        self.assertEqual([r["check_id"] for r in receipts], ["attachments:PROP_mallet"])
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertIn("max_tail_distance_um=54529:", reason(receipts[0]))
        receipts, result = one("film-animation", "attachments", Replay("take1827_motion_v2.json"))
        self.assertEqual((result["final_verdict"], receipts[0]["error"]),
                         ("CANNOT_SEE", "selector_empty:attachments:undeclared_bone_parented_meshes=23"))


class Capture(unittest.TestCase):
    def test_pixel_frames_are_bounded_evenly_and_keep_both_ends(self):
        self.assertEqual(p.pixel_frames([1, 345, 1], 12), [1, 32, 64, 95, 126, 157, 189, 220, 251, 282, 314, 345])
        self.assertEqual(p.pixel_frames([1, 3, 1], 12), [1, 2, 3])
        self.assertEqual(p.pixel_frames([5, 9, 1], 1), [5])
        self.assertEqual(p.pixel_frames([1, 345, 1]), list(range(1, 346)))

    def test_run_renders_only_declared_sample_frames_and_says_so(self):
        adapter = Shot((1, 40))
        with patch.object(adapter, "capture", wraps=adapter.capture) as captures:
            receipts, result = one("film-animation", "nonblank", adapter)
        self.assertEqual(result["final_verdict"], "PASS")
        self.assertEqual(captures.call_count, 12)
        self.assertTrue(reason(receipts[0]).startswith("frames_sampled=12_of_40;frame=1:"))
        self.assertEqual(adapter.state["frame_current"], 1)
        # Mutant: dropping the coverage note would present 12 frames as the whole shot.
        broken = mutated(p, "if len(wanted) < total:", "if False:", "run")
        with patch.object(p, "run", broken):
            self.assertFalse(reason(one("film-animation", "nonblank", Shot((1, 40)))[0][0]).startswith("frames_sampled"))

    def test_fallback_camera_pixels_never_qualify_a_shot(self):
        from blender_visual_qa.drive.png import encode
        from vqa3d.contract import Observation

        class Fallback(Shot):
            def capture(self, tag):
                return Observation.create(self.id, tag, self.scene_state(), encode(2, 2, bytes(range(12))),
                                          camera_source="fallback", camera_name="VQA3D_Camera")
        profile = selected(p.load("film-animation"), "nonblank", "noise", "scene-camera")
        receipts, result, _ = p.run(profile, Fallback())
        by_id = {r["check_id"]: r for r in receipts}
        self.assertEqual(by_id["nonblank:scene"]["error"], "capture_camera_not_scene:fallback")
        self.assertEqual(by_id["noise:scene"]["final_verdict"], "CANNOT_SEE")
        self.assertEqual(result["final_verdict"], "CANNOT_SEE")
        # A profile that does not ask for the shot camera (game assets) may use the fallback view.
        self.assertEqual(one("game-realtime", "nonblank", Fallback())[1]["final_verdict"], "PASS")
        broken = mutated(p, 'if shot_camera and obs.camera_source != "scene":', "if False:", "run")
        self.assertEqual(broken(profile, Fallback())[1]["final_verdict"], "ADVISORY")

    def test_missing_gpu_and_denoiser_stay_visible_without_failing_the_shot(self):
        profile = selected(p.load("film-animation"), "scene-camera", "gpu-render", "denoiser")
        receipts, result, _ = p.run(profile, Scene())      # probe: CPU only; this fake build lists a denoiser
        by_id = {r["check_id"]: r for r in receipts}
        self.assertEqual(by_id["gpu-render:scene"]["error"], "capability_missing:device_type:CUDA")
        self.assertEqual(by_id["denoiser:scene"]["final_verdict"], "PASS")
        self.assertEqual(result["final_verdict"], "ADVISORY")
        adapter = Scene()
        gpu = dict(adapter.probe(), cycles_devices={"CPU": ["CPU"], "CUDA": ["GB10"]}, gpu_render_ok_by_type={"CUDA": False}, denoisers=[])
        with patch.object(adapter, "probe", return_value=gpu):
            receipts, result, _ = p.run(profile, adapter)
        self.assertEqual([r["final_verdict"] for r in receipts[2:]], ["CANNOT_SEE", "CANNOT_SEE"])  # enumerated is not rendered
        gpu["gpu_render_ok_by_type"]["CUDA"] = True
        with patch.object(adapter, "probe", return_value=gpu):
            self.assertEqual(p.run(profile, adapter)[0][2]["final_verdict"], "PASS")


class RenderSettings(unittest.TestCase):
    def test_film_enforces_quality_settings_and_records_shot_choices(self):
        adapter = Scene()
        adapter.state["render"].update(look="AgX - Base Contrast", samples=128, motion_blur=0, clamp_indirect_milli=10000)
        receipts, result = one("film-animation", "render-settings", adapter)
        self.assertEqual(result["final_verdict"], "PASS")
        text = reason(receipts[0])
        for part in ("engine=CYCLES", "samples=128", "look=AgX - Base Contrast(recorded)", "motion_blur=0(recorded)", "clamp_indirect_milli=10000(recorded)"):
            self.assertIn(part, text)
        for key, value in (("engine", "BLENDER_EEVEE"), ("samples", 8), ("view_transform", "Standard")):
            broken = Scene()
            broken.state["render"][key] = value
            self.assertEqual(one("film-animation", "render-settings", broken)[1]["final_verdict"], "FAIL", key)
        missing = Scene()
        missing.state["render"].pop("samples")
        self.assertEqual(one("film-animation", "render-settings", missing)[0][0]["error"], "state_path_absent:/render/samples")

    def test_enforce_list_is_validated_and_other_profiles_stay_strict(self):
        profile = p.load("film-animation")
        check = next(c for c in profile["checks"] if c["id"] == "render-settings")
        for bad in (["engine", "engine"], ["shutter"], [], "engine", ["look"]):
            check["params"]["enforce"] = bad
            with self.assertRaises(ValueError):
                p.validate(profile)
        still = Scene()
        still.state["render"] = copy.deepcopy(p.load("hyperreal-still")["render"])
        self.assertEqual(one("hyperreal-still", "render-settings", still)[1]["final_verdict"], "PASS")
        still.state["render"]["look"] = "None"
        self.assertEqual(one("hyperreal-still", "render-settings", still)[1]["final_verdict"], "FAIL")


class Schema(unittest.TestCase):
    def test_film_oracle_templates_are_checked(self):
        def broken(mutate):
            profile = p.load("film-animation")
            mutate({c["id"]: c for c in profile["checks"]}, profile)
            profile["provenance"] = {path: "target" for path in p.numeric_paths(profile)}
            return profile
        cases = {
            "feet select": lambda c, d: c["feet-floor"].update(select={"type": "MESH"}),
            "bad regex": lambda c, d: d["defaults"]["feet_floor"].update(foot_regex="("),
            "missing slide limit": lambda c, d: d["defaults"]["foot_slide"].pop("max_slide_um"),
            "negative slide": lambda c, d: d["defaults"]["foot_slide"].update(max_slide_um=-1),
            "attachment selector": lambda c, d: c["attachments"].update(select={"type": "MESH"}),
            "bone path": lambda c, d: c["bone-continuity"]["params"].update(path="/objects/{object}/quat"),
            "bone selector": lambda c, d: c["bone-continuity"].update(select={"type": "MESH"}),
            "capture frames": lambda c, d: d["capture"].update(max_frames=0),
            "unknown param": lambda c, d: c["foot-slide"]["params"].update(max_um=1)}
        for name, mutate in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                p.validate(broken(mutate))
        p.validate(broken(lambda c, d: d["defaults"]["feet_floor"].update(min_clearance_um=-500)))  # a declared tolerance is allowed

    def test_every_film_threshold_is_provenanced_and_measured_ones_name_a_file(self):
        profile = p.load("film-animation")
        self.assertIsNone(profile["budgets"])
        self.assertEqual(set(p.numeric_paths(profile)), set(profile["provenance"]))
        measured = {k: v for k, v in profile["provenance"].items() if v.startswith("measured:")}
        self.assertEqual(set(measured), {"/capture/samples", "/render/samples", "/defaults/frame_noise_below/production_calibrated_px"})
        self.assertEqual([c["id"] for c in profile["checks"] if c["severity"] == "gate"],
                         ["feet-floor", "foot-slide", "bone-continuity", "attachments", "scene-camera", "nonblank", "render-settings"])


class BlenderSide(unittest.TestCase):
    def test_unit_quaternion_is_sign_canonical_integer_ppm(self):
        quat = lambda w, x, y, z: NS(normalized=lambda: NS(w=w, x=x, y=y, z=z))
        self.assertEqual(blender_side.unit_quat_ppm(quat(-0.6, 0.0, 0.8, 0.0)), [600000, 0, -800000, 0])
        self.assertEqual(blender_side.unit_quat_ppm(quat(0.0, -1.0, 0.0, 0.0)), [0, 1000000, 0, 0])
        self.assertEqual(blender_side.unit_quat_ppm(quat(1.0, 0.0, 0.0, 0.0)), [1000000, 0, 0, 0])
        broken = mutated(blender_side, "return [-v for v in row] if lead < 0 else row", "return row", "unit_quat_ppm")
        self.assertNotEqual(broken(quat(-0.6, 0.0, 0.8, 0.0)), [600000, 0, -800000, 0])

    def test_bone_rows_world_and_parent_relative(self):
        class M:
            def __init__(self, tag): self.tag = tag
            def __matmul__(self, other): return M(self.tag + "*" + other.tag) if isinstance(other, M) else [v + 1 for v in other]
            def inverted_safe(self): return M("inv(" + self.tag + ")")
            def to_quaternion(self):
                table = {"W*root": (1, 0, 0, 0), "root": (0, 1, 0, 0), "W*child": (0, 0, 1, 0), "inv(root)*child": (0, 0, 0, -1)}
                w, x, y, z = table[self.tag]
                return NS(normalized=lambda: NS(w=w, x=x, y=y, z=z))
        root = NS(name="root", head=[0, 0, 0], tail=[0, 0, 1], parent=None, matrix=M("root"))
        child = NS(name="child", head=[0, 0, 1], tail=[0, 0, 2], parent=root, matrix=M("child"))
        rows = blender_side.bone_rows(NS(matrix_world=M("W"), pose=NS(bones=[root, child])))
        self.assertEqual(rows["root"]["quat_world_ppm"], [1000000, 0, 0, 0])
        self.assertEqual(rows["root"]["quat_local_ppm"], [0, 1000000, 0, 0])     # armature space for a root bone
        self.assertEqual(rows["child"]["quat_world_ppm"], [0, 0, 1000000, 0])
        self.assertEqual(rows["child"]["quat_local_ppm"], [0, 0, 0, 1000000])    # parent^-1 @ bone, sign canonical
        self.assertEqual((rows["child"]["parent"], rows["child"]["tail_world_um"]), ("root", [1000000, 1000000, 3000000]))
        broken = mutated(blender_side, "(bone.parent.matrix.inverted_safe() @ bone.matrix) if bone.parent else bone.matrix", "bone.matrix", "bone_rows")
        with self.assertRaises(KeyError):
            broken(NS(matrix_world=M("W"), pose=NS(bones=[root, child])))

    def test_skin_groups_bound_only_vertices_owned_by_the_bone(self):
        obj = NS(vertex_groups=[NS(index=0, name="foot.L"), NS(index=1, name="shin.L")])
        mesh = NS(vertices=[NS(groups=[NS(group=0, weight=0.9)]), NS(groups=[NS(group=0, weight=0.5), NS(group=1, weight=0.5)]),
                            NS(groups=[NS(group=0, weight=0.2), NS(group=1, weight=0.8)]), NS(groups=[])])
        points = [(0.0, 0.0, 0.01), (0.1, 0.0, 0.03), (0.0, 0.0, -0.5), (0.0, 0.0, -9.0)]
        groups = blender_side.skin_groups(obj, None, mesh, points)
        self.assertEqual(groups["foot.L"], {"world_bbox_min_um": [0, 0, 10000], "world_bbox_max_um": [100000, 0, 30000], "verts": 2})
        self.assertEqual(groups["shin.L"]["world_bbox_min_um"], [0, 0, -500000])
        broken = mutated(blender_side, "if name is None or element.weight < weight_floor:", "if name is None:", "skin_groups")
        self.assertEqual(broken(obj, None, mesh, points)["foot.L"]["world_bbox_min_um"][2], -500000)  # a shin vertex leaks in


if __name__ == "__main__":
    unittest.main()
