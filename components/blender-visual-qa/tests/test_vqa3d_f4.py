"""Round-4 review regressions: footprint floor, decisive contact verdicts, contact-aware sampling, run declarations."""
import contextlib
import io
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from vqa3d import profile as p
from vqa3d.__main__ import main
from vqa3d.adapters import blender_side as side
from tests.test_vqa3d_film import Shot, one, reason, mutated, HEAD, TAIL
from tests.test_vqa3d_profiles import selected

SOLE = "/objects/foot.L/world_bbox_min_um/2"
CONTACT = "/contacts/foot.L"
STREET = {"object": "Street", "z_um": 0, "method": "ray_cast"}
NOTHING = {"object": "", "method": "unresolved"}


def contact_step(adapter, check):
    policy = selected(p.load("film-animation"), check)
    state = adapter.scene_state()
    state["contact_sampling"] = 1
    return p.expand(policy, state)[0]["obligation"]


def shot(frames=(1, 6)):
    adapter = Shot(frames)
    adapter.state["contacts"] = {"foot.L": dict(STREET)}
    return adapter


class Session(Shot):
    """A fake with the one-process profile session interface, so sampling policy runs."""
    profile_session, _tmp, timeout, max_sample_frames = True, None, 1, 2000

    def __init__(self, frames, keyframes=()):
        super().__init__(frames)
        self.state["contacts"] = {"foot.L": dict(STREET)}
        self.state["keyframes"] = list(keyframes)
        self.contact_feet, self.floor_declaration, self.sample_cache = [], None, None


class DecisiveContact(unittest.TestCase):
    def test_measured_penetration_fails_although_other_frames_have_no_floor(self):
        adapter = shot()
        adapter.timeline[CONTACT] = {5: dict(NOTHING), 6: dict(NOTHING)}
        adapter.timeline[SOLE] = {3: -5000}
        obligation = contact_step(adapter, "feet-floor")
        verdict, detail = p._feet_floor(adapter, obligation)
        self.assertEqual((verdict, detail), (
            "FAIL", "foot_below_floor:foot.L:frame=3:clearance_um=-5000:floor=Street@0:source=mesh:floor_unresolved_frames=2"))
        # Mutant (the reviewed behaviour): one unresolved frame hides the penetration.
        broken = mutated(p, "if all(value is None for value in clearance):", "if any(value is None for value in clearance):", "_feet_floor")
        with self.assertRaisesRegex(Exception, "^floor_unresolved:foot.L"):  # the mutant module's own CannotSee
            broken(adapter, contact_step(adapter, "feet-floor"))

    def test_clear_foot_cannot_pass_while_some_frames_have_no_floor(self):
        adapter = shot()
        adapter.timeline[CONTACT] = {4: dict(NOTHING)}
        with self.assertRaisesRegex(p.CannotSee, r"^floor_unresolved:foot\.L:frames=1_of_6:first=4:resolved_min_clearance_um=2500:frame=1$"):
            p._feet_floor(adapter, contact_step(adapter, "feet-floor"))
        adapter.timeline[CONTACT] = {f: dict(NOTHING) for f in range(1, 7)}
        with self.assertRaisesRegex(p.CannotSee, r"^floor_unresolved:foot\.L:frames=6_of_6$"):
            p._feet_floor(adapter, contact_step(adapter, "feet-floor"))
        # Mutant: skipping the refusal turns a partly unobserved foot into a PASS.
        adapter.timeline[CONTACT] = {4: dict(NOTHING)}
        broken = mutated(p, "    if missing:\n        raise _unresolved(obligation, frames, clearance)\n    if obligation[\"source\"] == \"bone\":",
                         "    if obligation[\"source\"] == \"bone\":", "_feet_floor")
        self.assertEqual(broken(adapter, contact_step(adapter, "feet-floor"))[0], "PASS")

    def test_drag_is_decisive_and_a_frame_without_floor_is_never_a_plant(self):
        adapter = shot()
        adapter.timeline[CONTACT] = {6: dict(NOTHING)}
        adapter.timeline[HEAD] = {f: [50000 * f, 0, 80000] for f in range(1, 7)}
        adapter.timeline[TAIL] = {f: [50000 * f, -150000, 10000] for f in range(1, 7)}
        verdict, detail = p._foot_slide(adapter, contact_step(adapter, "foot-slide"))
        self.assertEqual(verdict, "FAIL")
        self.assertIn("frames=1-5:drift_um=200000", detail)
        self.assertTrue(detail.endswith(":floor_unresolved_frames=1"))
        still = shot()
        still.timeline[CONTACT] = {6: dict(NOTHING)}
        with self.assertRaisesRegex(p.CannotSee, "floor_unresolved:foot.L:frames=1_of_6:first=6"):
            p._foot_slide(still, contact_step(still, "foot-slide"))


class SampledPlants(unittest.TestCase):
    FRAMES = [1, 2, 3, 7, 8, 9]

    def stepping(self, shift):
        adapter = shot((1, 9))
        late = {f: shift for f in (7, 8, 9)}
        adapter.timeline[HEAD] = {f: [x, 0, 80000] for f, x in late.items()}
        adapter.timeline[TAIL] = {f: [x, -150000, 10000] for f, x in late.items()}
        obligation = contact_step(adapter, "foot-slide")
        obligation["sample_frames"] = list(self.FRAMES)
        return adapter, obligation

    def test_a_step_taken_on_unsampled_frames_is_not_a_slide(self):
        adapter, obligation = self.stepping(300000)
        verdict, detail = p._foot_slide(adapter, obligation)
        self.assertEqual((verdict, detail), ("PASS", "planted_feet_hold:foot.L:frames=1-3:drift_um=0:max_um=40000:planted_runs=2:plant_max_um=6000"))
        # Mutant (the reviewed behaviour): samples are joined across the gap and the step reads as a drag.
        broken = mutated(p, "gap_moved = (planted and start is not None and", "gap_moved = (False and start is not None and", "_foot_slide")
        adapter, obligation = self.stepping(300000)
        self.assertEqual(broken(adapter, obligation)[0], "FAIL")

    def test_a_slow_drag_across_resting_gaps_still_accumulates(self):
        adapter = shot((1, 60))
        frames = [f for anchor in range(1, 61, 6) for f in (anchor, anchor + 1, anchor + 2)]
        adapter.timeline[HEAD] = {f: [5000 * ((f - 1) // 6), 0, 80000] for f in frames}
        adapter.timeline[TAIL] = {f: [5000 * ((f - 1) // 6), -150000, 10000] for f in frames}
        obligation = contact_step(adapter, "foot-slide")
        obligation["sample_frames"] = frames
        verdict, detail = p._foot_slide(adapter, obligation)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("drift_um=45000", detail)
        self.assertIn("planted_runs=1", detail)


class Refinement(unittest.TestCase):
    FRAMES = [1, 2, 6, 7, 8, 12, 13, 14]

    def track(self, clearance, z):
        return clearance, [[[[0, 0, value], [0, -150000, value]]] for value in z]

    def test_gaps_next_to_a_moving_foot_in_reach_of_the_floor_are_filled(self):
        rest = [2500] * 8
        self.assertEqual(p.contact_refinement(self.FRAMES, [self.track(rest, [80000] * 8)], 6000, 100), ([], False))
        # Frame 8 moves 4 mm against frame 7: within 4 frames it can cross the floor.
        z = [80000, 80000, 80000, 80000, 76000, 80000, 80000, 80000]
        self.assertEqual(p.contact_refinement(self.FRAMES, [self.track(rest, z)], 6000, 100), ([9, 10, 11], False))
        # A different pose on each side of a gap, no adjacent witness needed.
        z = [80000, 80000, 90000, 90000, 90000, 90000, 90000, 90000]
        self.assertEqual(p.contact_refinement(self.FRAMES, [self.track(rest, z)], 6000, 100)[0], [3, 4, 5])
        # High above the floor and at rest: nothing to add. No floor on either side: nothing to measure.
        self.assertEqual(p.contact_refinement(self.FRAMES, [self.track([400000] * 8, [80000] * 8)], 6000, 100)[0], [])
        self.assertEqual(p.contact_refinement(self.FRAMES, [self.track([None] * 8, z)], 6000, 100)[0], [])
        self.assertEqual(p.contact_refinement(self.FRAMES, [self.track(rest, z)], 6000, 2), ([3, 4], True))

    def falling(self):
        adapter = Session((1, 30))
        adapter.timeline[SOLE] = {8: 500, 10: -5000}
        adapter.timeline[HEAD] = {8: [0, 0, 76000], 9: [0, 0, 74000], 10: [0, 0, 72500], 11: [0, 0, 78000]}
        return adapter

    def test_default_sampling_finds_a_penetration_between_strides_mutant(self):
        adapter = self.falling()
        self.assertNotIn(10, p.sample_frames([1, 30, 1], p.load("film-animation")["sampling"], []))
        receipts, result = one("film-animation", "feet-floor", adapter)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertIn("frame=10:clearance_um=-5000", reason(receipts[0]))
        self.assertEqual(result["timings"]["refined_frames"], 3)
        self.assertEqual(receipts[0]["frame_sampling"]["sample_count"], result["timings"]["sampled_frames"] + 3)
        # Mutant (the reviewed behaviour): stride sampling alone steps over the defect: a false PASS.
        broken = mutated(p, "extra.update(range(frames[index]+1, frames[index+1]))", "pass", "prefetch_profile")
        with patch.object(p, "prefetch_profile", broken):
            self.assertEqual(one("film-animation", "feet-floor", self.falling())[1]["final_verdict"], "PASS")

    def test_an_explicit_frame_range_is_sampled_as_given(self):
        adapter = self.falling()
        receipts, result = one("film-animation", "feet-floor", adapter, frames=[1, 30, 1])
        self.assertEqual((result["final_verdict"], result["timings"]["refined_frames"]), ("FAIL", 0))
        self.assertEqual(receipts[0]["frame_sampling"]["sample_count"], 30)


class RunDeclarations(unittest.TestCase):
    def test_the_floor_of_a_scene_is_declared_for_the_run_not_in_a_profile(self):
        self.assertEqual(sorted(path.stem for path in p.DIRECTORY.glob("film-animation*.json")), ["film-animation"])
        policy = selected(p.load("film-animation"), "feet-floor")
        adapter = Session((1, 3))
        receipts, result, trace = p.run(policy, adapter, floor={"z_um": -2000})
        self.assertEqual(adapter.floor_declaration, {"z_um": -2000})
        self.assertEqual(receipts[1]["floor_declaration"], {"declared_by": "run", "z_um": -2000})
        self.assertEqual(trace["scene_state"]["declared_floor"], {"z_um": -2000})
        self.assertEqual(receipts[1]["profile"], p.identity(p.load("film-animation")) | {"sha256": receipts[1]["profile"]["sha256"]})
        self.assertEqual(receipts[1]["profile"], p.identity(policy))
        plain = Session((1, 3))
        rows, _, _ = p.run(policy, plain)
        self.assertNotIn("floor_declaration", rows[1])
        self.assertIsNone(plain.floor_declaration)
        for bad in ({}, {"z_um": 1.5}, {"object": "a", "z_um": 0}):
            with self.assertRaises(ValueError):
                p.run(policy, Session((1, 3)), floor=bad)
        # Mutant: a run declaration that never reaches the contact sampler.
        broken = mutated(p, '(("run", scene_state.get("declared_floor")), ("check", params.get("floor")),', '(("check", params.get("floor")),', "expand")
        with patch.object(p, "expand", broken):
            unseen = Session((1, 3))
            p.run(policy, unseen, floor={"z_um": -2000})
            self.assertIsNone(unseen.floor_declaration)

    def test_cli_floor_and_attach_are_typed(self):
        for argv, error in ((["--floor", "height=3"], "floor_requires_z_um_object_or_collection"),
                            (["--floor", "z_um=low"], "invalid literal"),
                            (["--attach", "PROP_phone"], "attach_requires_object_mode"),
                            (["--attach", "PROP_phone=glued"], "invalid_attachment_mode")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["profile", "check", "--profile", "film-animation", "--adapter", "blender"] + argv)
            self.assertEqual(rc, 2)
            self.assertIn(error, out.getvalue())

    def mode(self, adapter, **kwargs):
        receipts, result = one("film-animation", "attachments", adapter, **kwargs)
        return receipts[0]["attachment_mode"], receipts[0]["attachment_declared_by"], result["final_verdict"]

    def test_props_are_classed_by_declaration_then_by_the_bone_that_carries_them(self):
        self.assertNotIn("mapping", p.load("film-animation")["defaults"]["attachment"])
        adapter = Shot()
        prop = adapter.state["objects"]["prop/x~"]
        self.assertEqual(self.mode(adapter), ("in_grip", "object_property", "PASS"))
        prop.pop("attach")
        prop.update(bone_relative_um=[400000, 0, 0], world_location_um=[400000, 0, 0])
        # Undeclared, carried by a hand: held, so 40 cm from the grip fails.
        self.assertEqual(self.mode(adapter), ("in_grip", "grip_bone_regex", "FAIL"))
        self.assertEqual(self.mode(adapter, attach={"prop/x~": "rigid_follow"}), ("rigid_follow", "run", "PASS"))
        with self.assertRaises(ValueError):
            one("film-animation", "attachments", adapter, attach={"prop/x~": "glued"})
        # Undeclared, hung from the chest: follows rigidly, distance to the bone tail is not judged.
        bones = adapter.state["objects"]["Rig.a"]["bones"]
        bones["chest"] = dict(bones["hand.R"])
        prop["parent_bone"] = "chest"
        self.assertEqual(self.mode(adapter), ("rigid_follow", "default", "PASS"))
        self.assertEqual(self.mode(adapter, attach={"prop/x~": "in_grip"}), ("in_grip", "run", "FAIL"))
        # Mutant: without the grip-bone rule a prop that left the hand follows "rigidly" and passes.
        prop["parent_bone"] = "hand.R"
        broken = mutated(p, '("grip_bone_regex", "in_grip" if grip_bone and re.search(', '("grip_bone_regex", "in_grip" if False and re.search(', "expand")
        with patch.object(p, "expand", broken):
            self.assertEqual(self.mode(adapter), ("rigid_follow", "default", "PASS"))


class Vec(list):
    @property
    def z(self):
        return self[2]

    @z.setter
    def z(self, value):
        self[2] = value

    def __sub__(self, other):
        return Vec(a - b for a, b in zip(self, other))


class Footprint(unittest.TestCase):
    """contact_floor against a scripted scene.ray_cast: layers are (object, top z in metres, covers(x, y))."""
    STATE = {"objects": {"sole": {"world_bbox_min_um": [0, 0, 2000], "world_bbox_max_um": [100000, 240000, 60000]},
                         "Rig": {"bones": {"foot": {"head_world_um": [50000, 0, 80000]}}}}}
    FOOT = {"label": "sole", "z": ["/objects/sole/world_bbox_min_um/2"], "bones": [["Rig", "foot"]]}

    def cast(self, layers, objects, declaration=None, module=side):
        def ray_cast(graph, origin, direction, distance):
            for obj, top, covers in sorted(layers, key=lambda layer: -layer[1]):
                if top < origin.z and covers(origin[0], origin[1]):
                    return True, Vec([origin[0], origin[1], top]), None, 0, obj, None
            return False, None, None, -1, None, None
        scene = NS(objects=objects, ray_cast=ray_cast)
        bpy = NS(context=NS(scene=scene, evaluated_depsgraph_get=lambda: None))
        with patch.dict("sys.modules", {"mathutils": NS(Vector=Vec)}):
            return module.contact_floor(bpy, self.STATE, self.FOOT, declaration)

    def test_a_joint_between_paving_stones_under_the_foot_centre_is_not_the_floor(self):
        rig = NS(name="Rig", parent=None, modifiers=[], type="ARMATURE")
        pavers = NS(name="Pavement", parent=None, modifiers=[], type="MESH")
        base = NS(name="SubBase", parent=None, modifiers=[], type="MESH")
        joint = lambda x, y: abs(y - 0.12) < 0.004          # 8 mm joint under the bbox centre line
        layers = [(pavers, 0.0, lambda x, y: not joint(x, y)), (base, -0.005, lambda x, y: True)]
        floor = self.cast(layers, [rig, pavers, base])
        self.assertEqual(floor, {"object": "Pavement", "z_um": 0, "method": "ray_cast", "rays": 9})
        # Mutant (the reviewed behaviour): one centre ray reports the sub-base 5 mm below.
        broken = mutated(side, "FOOTPRINT_QUARTERS = (1, 2, 3)", "FOOTPRINT_QUARTERS = (2,)", "contact_floor")
        self.assertEqual(self.cast(layers, [rig, pavers, base], module=NS(contact_floor=broken))["object"], "SubBase")

    def test_another_character_under_the_foot_is_not_a_floor_unless_declared(self):
        rig = NS(name="Rig", parent=None, modifiers=[], type="ARMATURE")
        other = NS(name="OtherRig", parent=None, modifiers=[], type="ARMATURE")
        shoe = NS(name="Other_Shoe", parent=None, modifiers=[NS(type="ARMATURE", object=other)], type="MESH")
        hat = NS(name="Other_Hat", parent=other, modifiers=[], type="MESH")
        street = NS(name="Street", parent=None, modifiers=[], type="MESH")
        layers = [(hat, 0.05, lambda x, y: True), (shoe, 0.03, lambda x, y: True), (street, 0.0, lambda x, y: True)]
        objects = [rig, other, shoe, hat, street]
        self.assertEqual(self.cast(layers, objects)["object"], "Street")
        # A rigged platform counts once its author declares it.
        hat.get = lambda key, default=None: {"vqa3d_role": "floor"}.get(key, default)
        self.assertEqual(self.cast(layers, objects), {"object": "Other_Hat", "z_um": 50000, "method": "ray_cast", "rays": 9})
        del hat.get
        # Mutant (the reviewed behaviour): only the foot's own rig is skipped.
        broken = mutated(side, 'if parent.name in own_rigs or getattr(parent, "type", "") == "ARMATURE":', "if parent.name in own_rigs:", "contact_floor")
        self.assertEqual(self.cast(layers, objects, module=NS(contact_floor=broken))["object"], "Other_Hat")

    def test_no_surface_under_any_ray_stays_unresolved(self):
        rig = NS(name="Rig", parent=None, modifiers=[], type="ARMATURE")
        self.assertEqual(self.cast([], [rig]), {"object": "", "method": "unresolved"})
        self.assertEqual(self.cast([], [rig], {"z_um": -2000}), {"object": "declared_z", "z_um": -2000, "method": "declared"})


if __name__ == "__main__":
    unittest.main()
