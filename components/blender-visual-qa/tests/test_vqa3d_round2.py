"""Round-two witnesses and executable in-memory source mutants; no bpy required."""
import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import tempfile
import unittest
from unittest.mock import patch

from blender_visual_qa.drive.png import encode
from vqa3d import metrics, temporal
from vqa3d.check import evaluate, validate_obligation, verify_step
from vqa3d.contract import Observation
from vqa3d.fakes import RecordingAdapter, ReplayAdapter
from vqa3d.adapters import blender_side
from vqa3d.adapters.blender import BlenderAdapter, validate_action


def png(values, width=3, height=3):
    return encode(width, height, bytes(c for value in values for c in (value, value, value)))


class Fake:
    id, version, status = "fake", "1", "implemented"
    capabilities = {"act": True, "scene_state": True, "capture_available": True}

    def __init__(self, state=None, image=None, camera_source="none", camera_name=""):
        self.state = copy.deepcopy(state if state is not None else {"x": 0, "frame_current": 1})
        self.image = image
        self.camera_source, self.camera_name = camera_source, camera_name
        self.actions = []
        self.timeline = {1: {"x": 0}, 2: {"x": 1}, 3: {"x": 9}}
        self.probed = {"engines": ["CYCLES"], "cycles_devices": {"CUDA": ["test GPU"]},
                       "gpu_render_ok": True, "gpu_render_ok_by_type": {"CUDA": True}, "denoisers": ["OPENIMAGEDENOISE"],
                       "view_transforms": ["AgX"], "looks": {"AgX": ["AgX - Medium High Contrast"]}}

    def capture(self, tag):
        return Observation.create(self.id, tag, self.state, self.image,
                                  camera_source=self.camera_source, camera_name=self.camera_name)

    def act(self, action):
        self.actions.append(action)
        if action.get("op") == "set_frame":
            self.state["frame_current"] = action["frame"]
        self.state.update(action.get("state", {}))
        if "image" in action:
            self.image = action["image"]
        return {"ok": True}

    def scene_state(self):
        return self.state

    def sample(self, frames, paths):
        return {frame: {path: self.timeline[frame][path] for path in paths} for frame in frames}

    def probe(self, render=False):
        return self.probed

    def close(self):
        pass


def mutated(module, old, new, symbol):
    source = inspect.getsource(module)
    if old not in source:
        raise AssertionError("mutant_anchor_absent:" + old)
    namespace = dict(vars(module))
    exec(compile(source.replace(old, new, 1), module.__file__, "exec"), namespace)
    return namespace[symbol]


class Round2Checks(unittest.TestCase):
    def result(self, obligation, state=None, **kwargs):
        return verify_step(Fake(state, **kwargs), {}, obligation)

    def test_diagnostics_integer_vectors_and_mutant(self):
        f = Fake({"x": [4, 5, 6]})
        result = verify_step(f, {}, {"kind": "state_change", "keys": ["x"]})
        self.assertEqual(result["error"], ";".join(f"required_state_unchanged:x.{i}:value={v}" for i, v in enumerate([4, 5, 6])))
        result = self.result({"kind": "state_equals", "expected": {"x": 7}}, {"x": 4})
        self.assertEqual(result["error"], "state_not_equal:x:expected=7:actual=4")
        from vqa3d import check
        broken = mutated(check, 'f"state_not_equal:{key}:expected={expected}:actual={actual}"', '"state_not_equal:" + key', "evaluate")
        before = after = Fake({"x": 4}).capture("before")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(before, after, {"kind": "state_equals", "expected": {"x": 7}})[1], result["error"])

    def test_unexpected_and_missing_state_reasons_retain_evidence(self):
        f = Fake({"x": 1, "y": 4})
        receipt = verify_step(f, {"state": {"x": 2, "y": 5}}, {"kind": "state_change", "keys": ["x"]})
        self.assertEqual(receipt["error"], "unexpected_state_change:/y:expected=4:actual=5")
        f = Fake({"x": [1, 2, 3]})
        receipt = verify_step(f, {"state": {"x": [1, 2, 4]}}, {"kind": "no_change"})
        self.assertEqual(receipt["error"], "unexpected_state_change:/x/2:expected=3:actual=4")
        missing = self.result({"kind": "state_equals", "expected": {"missing": 5}})
        self.assertEqual(missing["error"], "expected_state_key_missing:missing:expected=5:actual=AUSENTE")

    def test_tolerances_global_per_path_boundaries_and_mutant(self):
        for extra in ({"tolerance": 2}, {"tolerances": {"x": 2}}):
            ob = {"kind": "state_equals", "expected": {"x": 10}, **extra}
            self.assertEqual(self.result(ob, {"x": 12})["final_verdict"], "PASS")
            self.assertEqual(self.result(ob, {"x": 13})["final_verdict"], "FAIL")
        from vqa3d import check
        broken = mutated(check, "abs(actual - expected) > tolerance", "actual != expected", "evaluate")
        obs = Fake({"x": 12}).capture("before")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(obs, obs, {"kind": "state_equals", "expected": {"x": 10}, "tolerance": 2})[0], "PASS")

    def test_distance_exact_three_dimensions_and_mutants(self):
        ob = {"kind": "within_distance", "a": "a", "b": [0, 0, 0], "max_um": 5}
        self.assertEqual(self.result(ob, {"a": [0, 3, 4]})["final_verdict"], "PASS")
        bad = self.result(ob, {"a": [0, 0, 6]})
        self.assertIn("distance_um=6", bad["error"])
        self.assertEqual(bad["final_verdict"], "FAIL")
        # Floor sqrt is diagnostic only: sqrt(26) floors to 5 but exceeds 5.
        self.assertEqual(self.result(ob, {"a": [1, 0, 5]})["final_verdict"], "FAIL")
        for old, new in (("zip(vector(a), vector(b))", "zip(vector(a)[:2], vector(b)[:2])"),
                         ("squared > maximum**2", "distance > maximum")):
            broken = mutated(temporal, old, new, "distance_check")
            witness = [0, 0, 6] if "zip" in old else [1, 0, 5]
            with self.assertRaises(AssertionError):
                self.assertEqual(broken(witness, [0, 0, 0], 5)[0], "FAIL")

    def test_range_first_frame_vector_and_mutant(self):
        ob = {"kind": "range_over_frames", "frames": [1, 3, 1], "path": "x", "min": 0, "max": 1}
        f = Fake()
        result = verify_step(f, {}, ob)
        self.assertEqual(result["error"], "range_exceeded:x:frame=3:element=0:value=9")
        f.timeline[3]["x"] = 1
        self.assertEqual(verify_step(f, {}, ob)["final_verdict"], "PASS")
        f.timeline[3]["x"] = [1, -1]
        self.assertIn("element=1:value=-1", verify_step(f, {}, ob)["error"])
        broken = mutated(temporal, 'f"range_exceeded:{obligation[\'path\']}:frame={frame}:element={index}:value={value}"',
                         'f"range_exceeded:{obligation[\'path\']}:frame={frame-1}:element={index}:value={value}"', "evaluate_temporal")
        with self.assertRaises(AssertionError):
            self.assertIn("frame=3:", broken(f, f.state, ob)[1])

    def test_max_delta_vector_and_mutant(self):
        f = Fake()
        ob = {"kind": "max_delta_over_frames", "frames": [1, 3, 1], "path": "x", "max_delta": 8}
        self.assertEqual(verify_step(f, {}, ob)["final_verdict"], "PASS")
        ob["max_delta"] = 7
        self.assertIn("frames=2,3:delta=8", verify_step(f, {}, ob)["error"])
        f.timeline = {1: {"x": [0, 0, 0]}, 2: {"x": [0, 0, 8]}, 3: {"x": [0, 0, 8]}}
        self.assertEqual(verify_step(f, {}, ob)["final_verdict"], "FAIL")
        broken = mutated(temporal, "largest_delta > obligation[\"max_delta\"]", "largest_delta < obligation[\"max_delta\"]", "evaluate_temporal")
        with self.assertRaises(AssertionError):
            self.assertIn("frames=1,2:delta=8", broken(f, f.state, ob)[1])

    def test_distance_explicit_nonuniform_frames_and_mutant(self):
        f = Fake()
        f.timeline = {1: {"x": [0, 0, 0]}, 106: {"x": [0, 0, 3]}, 250: {"x": [0, 0, 6]}}
        ob = {"kind": "within_distance", "frames": [1, 106, 250], "a": "x", "b": [0, 0, 0], "max_um": 5}
        self.assertIn("frame=250:distance_um=6", verify_step(f, {}, ob)["error"])
        ob["max_um"] = 6
        self.assertEqual(verify_step(f, {}, ob)["final_verdict"], "PASS")
        broken = mutated(temporal, 'frames = obligation["frames"] if kind == "within_distance" else frames_from_spec(obligation["frames"], maximum)',
                         'frames = frames_from_spec(obligation["frames"], maximum)', "evaluate_temporal")
        ob["max_um"] = 5
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(f, f.state, ob)[0], "FAIL")

    def test_camera_provenance_identity_and_fallback_mutant(self):
        ob = {"kind": "camera_is", "name": "CAM_main"}
        self.assertEqual(self.result(ob, camera_source="scene", camera_name="CAM_main")["final_verdict"], "PASS")
        for source, name in (("fallback", "CAM_main"), ("scene", "other"), ("none", "")):
            self.assertEqual(self.result(ob, camera_source=source, camera_name=name)["final_verdict"], "FAIL")
        a = Fake(camera_source="scene", camera_name="CAM_main").capture("before")
        b = Fake(camera_source="fallback", camera_name="CAM_main").capture("before")
        self.assertNotEqual(a.obs_id, b.obs_id)
        from vqa3d import check
        broken = mutated(check, 'after.camera_source != "scene" or ', '', "evaluate")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(b, b, ob)[0], "FAIL")

    def test_blank_decoded_rgb_threshold_and_mutant(self):
        ob = {"kind": "frame_not_blank"}
        self.assertEqual(self.result(ob, image=png([0]*9))["final_verdict"], "FAIL")
        self.assertEqual(self.result(ob, image=png(list(range(9))))["final_verdict"], "PASS")
        self.assertEqual(self.result(ob)["final_verdict"], "CANNOT_SEE")
        self.assertEqual(self.result({**ob, "max_uniform_ppm": 1000000}, image=png([4]*9))["final_verdict"], "FAIL")
        from vqa3d import check
        with patch.object(check, "uniform_ppm", return_value=0), self.assertRaises(AssertionError):
            self.assertEqual(self.result(ob, image=png([0]*9))["final_verdict"], "FAIL")

    def test_noise_constant_isolated_pin_threshold_and_mutant(self):
        self.assertEqual(metrics.noise_ppm(5, 5, bytes([42]*75)), 0)
        rgb = bytearray(75)
        for i in (6, 18):
            rgb[3*i:3*i+3] = bytes([255]*3)
        self.assertEqual(metrics.noise_ppm(5, 5, rgb), 2*1000000//25)
        image = encode(5, 5, bytes(rgb))
        self.assertEqual(self.result({"kind": "frame_noise_below", "max_outlier_ppm": 79999}, image=image)["final_verdict"], "FAIL")
        self.assertEqual(self.result({"kind": "frame_noise_below", "max_outlier_ppm": 80000}, image=image)["final_verdict"], "PASS")
        self.assertEqual(self.result({"kind": "frame_noise_below", "max_outlier_ppm": 0})["final_verdict"], "CANNOT_SEE")
        self.assertEqual(metrics.noise_ppm(5, 5, rgb, 255), 0)
        broken = mutated(metrics, "neighbours[4]) > threshold", "neighbours[4]) > 0", "noise_ppm")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(5, 5, rgb, 255), 0)

    def test_frame_change_noise_floor_region_and_mutants(self):
        before = Observation.create("fake", "before", {}, png([10]*9))
        after = Observation.create("fake", "after", {}, png([11]*9))
        ob = {"kind": "frame_change", "pixel_tolerance": 1, "min_changed_ppm": 1}
        self.assertEqual(evaluate(before, after, ob)[0], "FAIL")
        ob["pixel_tolerance"] = 0
        self.assertEqual(evaluate(before, after, ob)[0], "PASS")
        after = Observation.create("fake", "after", {}, png([12]+[10]*8))
        ob["min_changed_ppm"] = 111112
        self.assertEqual(evaluate(before, after, ob)[0], "FAIL")
        ob["min_changed_ppm"] = 111111
        self.assertEqual(evaluate(before, after, ob)[0], "PASS")
        ob.update(region=[1, 1, 2, 2], min_changed_ppm=0)
        self.assertEqual(evaluate(before, after, ob)[0], "FAIL")
        broken = mutated(metrics, "> tolerance for c", ">= tolerance for c", "changed_ppm")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(bytes([10]*3), bytes([11]*3), 1), 0)
        from vqa3d import check
        broken = mutated(check, "score > 0 and score >= limit", "score > 0", "evaluate")
        ob.pop("region"); ob["min_changed_ppm"] = 111112
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(before, after, ob)[0], "FAIL")

    def test_capability_each_axis_and_gpu_fault_before_action(self):
        ob = {"kind": "capability", "engine": "CYCLES", "device_type": "CUDA", "denoiser": "OPENIMAGEDENOISE",
              "view_transform": "AgX", "look": "AgX - Medium High Contrast"}
        f = Fake()
        self.assertEqual(verify_step(f, {}, ob)["final_verdict"], "PASS")
        for key in set(ob)-{"kind"}:
            f = Fake()
            receipt = verify_step(f, {}, {**ob, key: "ABSENT"})
            self.assertEqual(receipt["final_verdict"], "CANNOT_SEE")
            self.assertIn("capability_missing:"+key, receipt["error"])
            self.assertEqual(f.actions, [])
        f = Fake(); f.probed["gpu_render_ok"] = False; f.probed["gpu_render_ok_by_type"]["CUDA"] = False
        self.assertEqual(verify_step(f, {}, ob)["final_verdict"], "CANNOT_SEE")
        self.assertEqual(f.actions, [])
        broken = mutated(temporal, 'present = present and probe.get("gpu_render_ok_by_type", {}).get(wanted) is True', 'present = present', "capability_check")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(f.probed, ob)[0], "CANNOT_SEE")

    def test_validation_unknowns_types_ranges_and_sample_budget(self):
        examples = [{"kind": "within_distance", "a": [0, 0, 0], "b": [0, 0, 0], "max_um": 1},
                    {"kind": "range_over_frames", "frames": [1, 2, 1], "path": "x", "min": 0},
                    {"kind": "max_delta_over_frames", "frames": [1, 2, 1], "path": "x", "max_delta": 1},
                    {"kind": "camera_is", "name": "camera"}, {"kind": "frame_not_blank"},
                    {"kind": "frame_noise_below", "max_outlier_ppm": 0}, {"kind": "capability", "engine": "CYCLES"}]
        for ob in examples:
            validate_obligation(ob)
            self.assertEqual(self.result({**ob, "unknown": 1})["final_verdict"], "ERROR")
        for frames in ([1, 2001, 1], [3, 1, 1], [1, 3, 0], [True, 3, 1], [1, 2]):
            with self.assertRaises(ValueError):
                validate_obligation({"kind": "range_over_frames", "frames": frames, "path": "x", "min": 0})
        self.assertEqual(len(temporal.frames_from_spec([1, 2000, 1])), 2000)
        for key in ("tolerance", "pixel_tolerance", "min_changed_ppm", "max_uniform_ppm", "max_outlier_ppm", "threshold"):
            for invalid in (True, -1):
                kind = "state_equals" if key == "tolerance" else "frame_change"
                ob = {"kind": kind, key: invalid}
                if kind == "state_equals": ob["expected"] = {"x": 0}
                self.assertEqual(self.result(ob)["final_verdict"], "ERROR")
        for value in (True, -1, 1.0):
            self.assertEqual(self.result({"kind": "state_equals", "expected": {"x": 0}, "tolerances": {"x": value}})["final_verdict"], "ERROR")
        for ob in ({"kind": "state_equals", "expected": {"x": 0}, "tolerance": 1, "tolerances": {"x": 1}},
                   {"kind": "capability"}, {"kind": "within_distance", "a": [1, 2], "b": [1, 2, 3], "max_um": 2}):
            self.assertEqual(self.result(ob)["final_verdict"], "ERROR")

    def test_record_replay_samples_probe_and_camera(self):
        f = Fake(camera_source="scene", camera_name="CAM")
        rec = RecordingAdapter(f)
        scenario = [{"action": {}, "obligation": {"kind": "range_over_frames", "frames": [1, 3, 1], "path": "x", "min": 0}},
                    {"action": {}, "obligation": {"kind": "capability", "engine": "CYCLES"}}]
        receipts = [verify_step(rec, **step) for step in scenario]
        trace = rec.trace(scenario, receipts, {"recording": "unit_fake"})
        replay = ReplayAdapter(trace)
        self.assertEqual([verify_step(replay, **step) for step in scenario], receipts)
        self.assertEqual(replay._cursor, len(trace["events"]))
        self.assertEqual(receipts[0]["after"]["camera_source"], "scene")

    def test_fallback_sample_restores_frame_and_rejects_action_failure(self):
        f = Fake(); f.sample = None
        self.assertEqual(temporal.sample_adapter(f, [3, 5], ["frame_current"]),
                         {"3": {"frame_current": 3}, "5": {"frame_current": 5}})
        self.assertEqual(f.state["frame_current"], 1)
        for frames in ([], [True], list(range(2001))):
            with self.assertRaises(ValueError): temporal.sample_adapter(f, frames, ["x"])
        f.act = lambda action: {"ok": False}
        with self.assertRaises(ValueError): temporal.sample_adapter(f, [2], ["x"])


class SideFields(unittest.TestCase):
    def fake_scene(self):
        class Vec(list):
            x = property(lambda s: s[0]); y = property(lambda s: s[1]); z = property(lambda s: s[2])
            def lerp(self, other, t): return Vec([a+(b-a)*t for a,b in zip(self, other)])
        class Matrix:
            def inverted(self): return NS(__unused=True)  # Camera uses its own inverse below.
            def __matmul__(self, value):
                if hasattr(value, "to_quaternion"): return value  # F3: pose-bone matrices carry orientation only here
                return Vec([value[0]+2, value[1]+3, value[2]+4])
            def decompose(self): return ([2, 3, 4], NS(to_euler=lambda: [1.5707963267948966, 0, 0]), [2, 2, 2])
        mesh = NS(vertices=[NS(co=[0, 0, -1]), NS(co=[1, 1, 1])], calc_loop_triangles=lambda: None, loop_triangles=[0, 1])
        clear = []
        evaluated = NS(matrix_world=Matrix(), to_mesh=lambda: mesh, to_mesh_clear=lambda: clear.append(True),
                       pose=NS(bones=[NS(name="hand.R", head=[0, 0, 0], tail=[0, 0, 1], parent=None,
                                         matrix=NS(to_quaternion=lambda: NS(normalized=lambda: NS(w=-0.6, x=0.0, y=0.8, z=0.0))))]))
        def obj(name, kind):
            return NS(name=name, type=kind, get=lambda k,d=None:d, location=[0, 0, 0], rotation_euler=[0, 0, 0], scale=[1, 1, 1],
                      hide_render=False, parent=NS(name="P"), parent_type="BONE", parent_bone="hand.R",
                      users_collection=[NS(name="PROP"), NS(name="A")], modifiers=[], vertex_groups=[],
                      evaluated_get=lambda deps: evaluated)
        class Inverse:
            def __matmul__(self, value): return Vec([value[0], value[1], -value[2]])
        camera = NS(matrix_world=NS(inverted=lambda: Inverse()), data=NS(clip_start=0.01, clip_end=100))
        scene = NS(objects=[obj("Mesh", "MESH"), obj("RIG", "ARMATURE")], frame_current=106, camera=camera)
        bpy = NS(context=NS(scene=scene, view_layer=NS(update=lambda: None), evaluated_depsgraph_get=lambda: object()), data=NS(images=[]))
        modules = {"mathutils": NS(Vector=Vec), "bpy_extras": NS(),
                   "bpy_extras.object_utils": NS(world_to_camera_view=lambda scene, cam, p: NS(x=p[0]/10, y=p[1]/10, z=p[2]))}
        return bpy, modules, clear

    def test_evaluated_world_parent_bones_bbox_frame_fields_and_faults(self):
        bpy, modules, clear = self.fake_scene()
        with patch.dict(sys.modules, modules): state = blender_side.scene_state(bpy)
        def witness(s):
            self.assertEqual(s["frame_current"], 106)
            row = s["objects"]["Mesh"]
            self.assertEqual(row["parent"], "P")
            self.assertEqual(row["parent_type"], "BONE")
            self.assertEqual(row["parent_bone"], "hand.R")
            self.assertEqual(row["location_um"], [0, 0, 0])
            self.assertEqual(row["world_location_um"], [2000000, 3000000, 4000000])
            self.assertEqual(row["world_rotation_mdeg"], [90000, 0, 0])
            self.assertEqual(row["world_scale_ppm"], [2000000]*3)
            self.assertEqual(row["world_bbox_min_um"], [2000000, 3000000, 3000000])
            self.assertEqual(row["world_bbox_max_um"], [3000000, 4000000, 5000000])
            self.assertEqual(row["view_ppm"], 1000000)
            self.assertEqual(row["view_depth_um"], 4000000)
            bone = s["objects"]["RIG"]["bones"]["hand.R"]
            self.assertEqual(bone["head_world_um"], [2000000, 3000000, 4000000])
            self.assertEqual(bone["tail_world_um"], [2000000, 3000000, 5000000])
            # F3 fields: author declarations and sign-canonical pose-bone quaternions.
            self.assertEqual((row["role"], row["collections"]), ("", ["A", "PROP"]))
            self.assertEqual(bone["quat_world_ppm"], [600000, 0, -800000, 0])
            self.assertEqual(bone["quat_local_ppm"], [600000, 0, -800000, 0])
            self.assertEqual(bone["parent"], "")
            self.assertNotIn("vertex_groups", row)
        witness(state)
        self.assertEqual(clear, [True])
        for old, new in (("evaluated.matrix_world @ bone.head", "bone.head"),
                         ("evaluated.matrix_world @ bone.tail", "bone.tail"),
                         ("min(v[i] for v in points)", "max(v[i] for v in points)"),
                         ('"world_location_um": [quantise(v, 1000000) for v in location]',
                          '"world_location_um": [quantise(v, 1000000) for v in obj.location]'),
                         ('"world_rotation_mdeg": [quantise(math.degrees(v), 1000) for v in rotation.to_euler()]',
                          '"world_rotation_mdeg": [0, 0, 0]'),
                         ('"world_scale_ppm": [quantise(v, 1000000) for v in scale]',
                          '"world_scale_ppm": [quantise(v, 1000000) for v in obj.scale]'),
                         ('sorted(collection.name for collection in obj.users_collection)',
                          '[collection.name for collection in obj.users_collection]'),
                         ('row["role"] = role if type(role) is str else ""', 'row["role"] = "prop"')):
            broken = mutated(blender_side, old, new, "scene_state")
            with patch.dict(sys.modules, modules), self.assertRaises(AssertionError):
                witness(broken(bpy))
        # Every emitted new field has an explicit injected bad-value rejection.
        targets = [(state, "frame_current")]
        targets += [(state["objects"]["Mesh"], k) for k in ("parent", "parent_type", "parent_bone", "world_location_um",
                    "world_rotation_mdeg", "world_scale_ppm", "world_bbox_min_um", "world_bbox_max_um", "view_ppm", "view_depth_um")]
        targets += [(state["objects"]["Mesh"], k) for k in ("role", "collections")]
        targets += [(state["objects"]["RIG"]["bones"]["hand.R"], k) for k in ("head_world_um", "tail_world_um", "quat_world_ppm", "quat_local_ppm", "parent")]
        for row, key in targets:
            saved = row[key]; row[key] = "injected_wrong_field"
            with self.subTest(field=key), self.assertRaises(AssertionError): witness(state)
            row[key] = saved
        bpy.context.scene.camera = None
        with patch.dict(sys.modules, modules): state = blender_side.scene_state(bpy)
        self.assertNotIn("view_ppm", state["objects"]["Mesh"])

    def test_behind_camera_excluded_and_source_mutant(self):
        bpy, modules, _ = self.fake_scene()
        bpy.context.scene.camera.data.clip_start = 10
        with patch.dict(sys.modules, modules):
            self.assertEqual(blender_side.scene_state(bpy)["objects"]["Mesh"]["view_ppm"], 0)
            broken = mutated(blender_side, "clipped = clip_bbox(bbox, scene.camera)", "clipped = bbox", "scene_state")
            with self.assertRaises(AssertionError):
                self.assertEqual(broken(bpy)["objects"]["Mesh"]["view_ppm"], 0)

    def test_capture_finishes_render_before_extracting_evaluated_mesh(self):
        events = []
        scene = NS(camera=NS(name="CAM"), get=lambda k, d: "scene", render=NS(image_settings=NS()),
                   display=NS(shading=NS()))
        def render(): events.append("render")
        def save_render(path, scene):
            events.append("pixels")
            Path(path).write_bytes(png([0]*9))
        bpy = NS(context=NS(scene=scene), app=NS(version_string="unit"), ops=NS(render=NS(render=render)),
                 data=NS(images={"Render Result": NS(save_render=save_render)}))
        def state(_bpy):
            self.assertEqual(events, ["render", "pixels"])
            events.append("state")
            return {"frame_current": 1}
        root = Path(__file__).resolve().parents[1] / "vqa3d"
        with tempfile.TemporaryDirectory(dir=root) as temp:
            request, response = Path(temp)/"req.json", Path(temp)/"out.json"
            request.write_text(json.dumps({"mode": "capture", "width": 3, "height": 3}))
            with patch.dict(sys.modules, {"bpy": bpy}), patch.object(sys, "argv", ["side", "--", str(request), str(response)]), patch.object(blender_side, "scene_state", side_effect=state):
                blender_side.main()
            self.assertEqual(events, ["render", "pixels", "state"])
            self.assertEqual(json.loads(response.read_text())["state"], {"frame_current": 1})

    def test_projection_clipping_behind_and_mutant(self):
        points = [NS(x=-1, y=0), NS(x=1, y=1)]
        self.assertEqual(blender_side.projected_fraction(points), 500000)
        self.assertEqual(blender_side.projected_fraction([]), 0)
        self.assertEqual(blender_side.projected_fraction([NS(x=2, y=0), NS(x=3, y=1)]), 0)
        broken = mutated(blender_side, "clipped / area", "area / area", "projected_fraction")
        with self.assertRaises(AssertionError): self.assertEqual(broken(points), 500000)

    def test_set_frame_integer_action_and_mutant(self):
        frames = []
        bpy = NS(context=NS(scene=NS(frame_set=frames.append), view_layer=NS(update=lambda: None)))
        validate_action({"op": "set_frame", "frame": 106})
        blender_side.apply(bpy, {"op": "set_frame", "frame": 106})
        self.assertEqual(frames, [106])
        for frame in (True, 1.1, "1"):
            with self.assertRaises(ValueError): validate_action({"op": "set_frame", "frame": frame})
            with self.assertRaises(ValueError): blender_side.apply(bpy, {"op": "set_frame", "frame": frame})
        broken = mutated(blender_side, 'frame_set(action["frame"])', 'frame_set(action["frame"]+1)', "apply")
        frames.clear(); broken(bpy, {"op": "set_frame", "frame": 106})
        with self.assertRaises(AssertionError): self.assertEqual(frames, [106])

    def test_blender_sampling_uses_single_invoke_and_limits(self):
        a = BlenderAdapter()
        with patch.object(a, "_prepare"), patch.object(a, "_invoke", return_value={"samples": {"1": {"x": 3}}}) as call:
            self.assertEqual(a.sample([1], ["x"]), {"1": {"x": 3}})
            call.assert_called_once_with("sample", frames=[1], paths=["x"])
            call.reset_mock()
            with self.assertRaises(ValueError): a.sample(list(range(2001)), ["x"])
            call.assert_not_called()

    def test_render_probe_reports_metadata_gpu_errors_and_never_raises(self):
        a = BlenderAdapter()
        caps = {"cycles_devices": {"CUDA": ["GB10"]}, "denoisers": [], "view_transforms": ["AgX"],
                "looks": {"AgX": ["AgX - Medium High Contrast"]}, "engines": ["CYCLES"]}
        def invoke(mode, **kwargs):
            if mode == "render_caps": return {"render_capabilities": caps}
            a.process_logs.append({"output": "CUDA sm_121 kernel failure"})
            raise RuntimeError("gpu failed")
        with patch.object(a, "_prepare"), patch.object(a, "capture", return_value=NS(capture_available=True)), patch.object(a, "_invoke", side_effect=invoke):
            result = a.probe(render=True)
        self.assertEqual(result["cycles_devices"], caps["cycles_devices"])
        self.assertEqual(result["looks"], caps["looks"])
        self.assertFalse(result["gpu_render_ok"])
        self.assertIn("sm_121", result["gpu_render_stderr_tail"])
        with patch.object(a, "_prepare", side_effect=RuntimeError("unavailable")):
            self.assertFalse(a.probe(render=True)["gpu_render_ok"])

        from vqa3d.adapters import blender
        broken = mutated(blender, 'report["gpu_render_ok"] = any(report["gpu_render_ok_by_type"].values())',
                         'report["gpu_render_ok"] = True', "BlenderAdapter")()
        with patch.object(broken, "_prepare"), patch.object(broken, "capture", return_value=NS(capture_available=True)), patch.object(broken, "_invoke", side_effect=invoke):
            with self.assertRaises(AssertionError):
                self.assertFalse(broken.probe(render=True)["gpu_render_ok"])

    def test_render_caps_enum_devices_transforms_looks_and_absent_cycles(self):
        prefs = NS(devices=[NS(type="CPU", name="host"), NS(type="CUDA", name="GB10")],
                   refresh_devices=lambda: None)
        view = NS(view_transform="AgX")
        scene = NS(render=NS(), view_settings=view, cycles=NS())
        bpy = NS(context=NS(scene=scene, preferences=NS(addons={"cycles": NS(preferences=prefs)})))
        def enum(owner, key):
            return {"engine": ["CYCLES"], "compute_device_type": ["NONE", "CUDA"],
                    "denoiser": ["OPENIMAGEDENOISE"], "view_transform": ["AgX", "Standard"],
                    "look": [view.view_transform+" Look"]}[key]
        with patch.object(blender_side, "enum_ids", side_effect=enum):
            caps = blender_side.render_capabilities(bpy)
            self.assertEqual(caps, {"engines": ["CYCLES"], "cycles_devices": {"CPU": ["host"], "CUDA": ["GB10"]},
                                   "denoisers": ["OPENIMAGEDENOISE"], "view_transforms": ["AgX", "Standard"],
                                   "looks": {"AgX": ["AgX Look"], "Standard": ["Standard Look"]}})
            self.assertEqual(view.view_transform, "AgX")
            bpy.context.preferences.addons = {}
            missing = blender_side.render_capabilities(bpy)
            self.assertEqual(missing["cycles_devices"], {})
            self.assertEqual(missing["denoisers"], [])

    def test_gpu_probe_sets_eight_pixels_one_sample_gpu_only(self):
        devices = [NS(type="CPU", use=True), NS(type="CUDA", use=False)]
        prefs = NS(devices=devices, compute_device_type="NONE", refresh_devices=lambda: None)
        scene = NS(render=NS(resolution_x=640, resolution_y=480, resolution_percentage=80, engine="BLENDER_WORKBENCH"),
                   cycles=NS(samples=64, device="CPU", use_denoising=True))
        renders = []
        def render():
            renders.append(True)
            self.assertEqual((scene.render.resolution_x, scene.render.resolution_y), (8, 8))
            self.assertEqual(scene.cycles.samples, 1)
            self.assertEqual(scene.cycles.device, "GPU")
            self.assertEqual([d.use for d in devices], [False, True])
        bpy = NS(context=NS(scene=scene, preferences=NS(addons={"cycles": NS(preferences=prefs)})),
                 ops=NS(render=NS(render=render)))
        self.assertTrue(blender_side.gpu_probe(bpy, "CUDA"))
        self.assertEqual((scene.render.resolution_x, scene.render.resolution_y), (640, 480))
        self.assertEqual(scene.cycles.samples, 64)
        self.assertEqual(scene.cycles.device, "CPU")
        self.assertEqual([d.use for d in devices], [True, False])
        self.assertEqual(renders, [True])
        with self.assertRaises(RuntimeError): blender_side.gpu_probe(bpy, "OPTIX")


class Round2Replay(unittest.TestCase):
    def test_retained_round2_traces(self):
        root = Path(__file__).resolve().parents[1] / "vqa3d/traces/round2"
        traces = sorted(root.glob("*.json"))
        self.assertTrue(traces, "round2_traces_required")
        for path in traces:
            trace = json.loads(path.read_text())
            adapter = ReplayAdapter(trace)
            expected_recording = "synthetic" if path.name.startswith("s") else "live"
            self.assertEqual(trace["provenance"]["recording"], expected_recording)
            self.assertEqual([verify_step(adapter, **step) for step in trace["scenario"]], trace["receipts"], path.name)
            self.assertEqual(adapter._cursor, len(trace["events"]))
