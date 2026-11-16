"""Review regression witnesses, including executable mutants for all blockers."""
import copy
import inspect
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from blender_visual_qa.drive.png import encode
from vqa3d import metrics, temporal
from vqa3d.adapters import blender, blender_side as side, blender_live as live
from vqa3d.adapters import blender_live_payload as payload, blender_live_bridge as bridge
from vqa3d.canon import sha256_canonical
from vqa3d.check import evaluate, verify_step, validate_obligation
from vqa3d.contract import Observation
from vqa3d.fakes import ReplayAdapter, seal_trace
import test_vqa3d_round2 as round2
Fake = round2.Fake
from test_vqa3d_live import FakeBridge, MOVE


def mutant(module, old, new, name):
    source = inspect.getsource(module)
    if source.count(old) != 1:
        raise AssertionError("mutant_anchor_not_unique:" + old)
    namespace = dict(module.__dict__)
    exec(compile(source.replace(old, new), module.__file__, "exec"), namespace)
    return namespace[name]


class BlockerTests(unittest.TestCase):
    def test_r2a01_sub_ppm_pixel_fails_and_floor_mutant_is_caught(self):
        width = height = 1001
        rgb = bytearray(width*height*3)
        pos = (500*width+500)*3
        rgb[pos:pos+3] = b"\xff\xff\xff"
        image = encode(width, height, bytes(rgb))
        obs = Observation.create("fake", "after", {}, image)
        obligation = {"kind": "frame_noise_below", "max_outlier_ppm": 0}
        self.assertEqual(evaluate(obs, obs, obligation)[0], "FAIL")
        broken = mutant(metrics,
            "(outliers * 1000000 + width * height - 1) // (width * height)",
            "outliers * 1000000 // (width * height)", "noise_ppm")
        with patch("vqa3d.check.noise_ppm", broken):
            with self.assertRaises(AssertionError):
                self.assertEqual(evaluate(obs, obs, obligation)[0], "FAIL")

    def test_r2a02_backend_specific_and_global_boolean_mutant(self):
        probe = {"cycles_devices": {"OPTIX": ["G"], "CUDA": ["G"]},
                 "gpu_render_ok": True, "gpu_render_ok_by_type": {"OPTIX": True, "CUDA": False}}
        obligation = {"kind": "capability", "device_type": "CUDA"}
        self.assertEqual(temporal.capability_check(probe, obligation)[0], "CANNOT_SEE")
        self.assertEqual(temporal.capability_check(probe, dict(obligation, device_type="OPTIX"))[0], "PASS")
        broken = mutant(temporal, 'probe.get("gpu_render_ok_by_type", {}).get(wanted) is True',
                        'probe.get("gpu_render_ok") is True', "capability_check")
        with self.assertRaises(AssertionError):
            self.assertEqual(broken(probe, obligation)[0], "CANNOT_SEE")
        adapter = blender.BlenderAdapter()
        def invoke(mode, **fields):
            if mode == "render_caps": return {"render_capabilities": {"cycles_devices": probe["cycles_devices"]}}
            if fields["device_type"] == "CUDA": raise RuntimeError("CUDA_broken")
            return {"gpu_render_ok": True}
        with patch.object(adapter, "_prepare"), patch.object(adapter, "capture", return_value=NS(capture_available=True)), patch.object(adapter, "_invoke", side_effect=invoke):
            self.assertEqual(adapter.probe(render=True)["gpu_render_ok_by_type"], {"OPTIX": True, "CUDA": False})

    def test_r2a03_side_missing_path_fail_and_raise_mutant(self):
        scene = NS(frame_current=7, render=NS(), cycles=NS(), view_settings=NS())
        scene.frame_set = lambda frame: setattr(scene, "frame_current", frame)
        bpy = NS(context=NS(scene=scene, preferences=NS(addons={}), view_layer=NS(update=lambda: None)))
        adapter = Fake()
        adapter.sample = lambda frames, paths: side.sample_scene(bpy, frames, paths)
        obligation = {"kind": "range_over_frames", "frames": [1, 2, 1], "path": "/missing", "min": 0}
        with patch.object(side, "scene_state", return_value={}):
            result = verify_step(adapter, {}, obligation)
        self.assertEqual(result["final_verdict"], "FAIL")
        self.assertEqual(result["verdicts"][-1]["reason"], "sample_state_key_missing")
        self.assertEqual(scene.frame_current, 7)
        broken = mutant(side, "# Missing entries remain absent; the host emits sample_state_key_missing.\n                    pass",
                        "raise RuntimeError('subprocess_crashed')", "sample_scene")
        adapter.sample = lambda frames, paths: broken(bpy, frames, paths)
        with patch.object(side, "scene_state", return_value={}):
            # The mutant namespace retained the function before patching.
            broken.__globals__["scene_state"] = lambda *args: {}
            with self.assertRaises(AssertionError):
                self.assertEqual(verify_step(adapter, {}, obligation)["final_verdict"], "FAIL")
        self.assertEqual(scene.frame_current, 7)


class CoreReviewTests(unittest.TestCase):
    def test_capability_success_has_no_capture_or_action(self):
        adapter = Fake()
        adapter.capture = lambda tag: self.fail("capability_capture")
        self.assertEqual(verify_step(adapter, {}, {"kind": "capability", "engine": "CYCLES"})["final_verdict"], "PASS")
        self.assertEqual(adapter.actions, [])

    def test_render_replay_requires_recorded_probe(self):
        trace = seal_trace({"schema": "vqa3d.trace/v1", "adapter": {"id": "fake", "version": "1", "status": "implemented"},
                            "events": [], "provenance": {}})
        with self.assertRaisesRegex(ValueError, "replay_exhausted"):
            ReplayAdapter(trace).probe(render=True)

    def test_adapter_probe_render_signatures(self):
        from vqa3d.registry import ADAPTERS
        for adapter in ADAPTERS.values():
            self.assertIn("render", inspect.signature(adapter.probe).parameters)

    def test_state_change_missing_key(self):
        obs = Observation.create("fake", "after", {})
        verdict, reason = evaluate(obs, obs, {"kind": "state_change", "keys": ["missing"]})
        self.assertEqual(verdict, "FAIL")
        self.assertIn("required_state_key_missing:missing", reason)

    def test_capture_configuration_is_sealed_in_step_receipt(self):
        adapter = Fake()
        adapter.capture = lambda tag: Observation.create("fake", tag, {}, notes=(
            '{"capture_engine":"eevee","capture_samples":4}',))
        receipt = verify_step(adapter, {}, {"kind": "state_equals", "expected": {"missing": 0}})
        self.assertEqual(receipt["capture_settings"]["after"], {"capture_engine": "eevee", "capture_samples": 4})
        digest = receipt.pop("receipt_sha256")
        self.assertEqual(sha256_canonical(receipt), digest)
        receipt["capture_settings"]["after"]["capture_engine"] = "cycles"
        self.assertNotEqual(sha256_canonical(receipt), digest)

    def test_obligation_and_constructor_overrides(self):
        self.assertEqual(len(temporal.frames_from_spec([1, 2001, 1], 2001)), 2001)
        with self.assertRaises(ValueError): temporal.frames_from_spec([1, 2001, 1])
        validate_obligation({"kind": "range_over_frames", "frames": [1, 2001, 1], "path": "x", "min": 0, "max_sample_frames": 2001})
        with self.assertRaises(ValueError): temporal.validate_sample_request([1], [])
        with self.assertRaises(ValueError): validate_obligation({"kind": "frame_noise_below", "max_outlier_ppm": 0, "luma_weights": [0, 0, 0]})
        image = encode(3, 3, bytes([255, 0, 0]*4+[0, 255, 0]+[255, 0, 0]*4))
        obs = Observation.create("fake", "after", {}, image)
        obligation = {"kind": "frame_noise_below", "max_outlier_ppm": 0, "luma_weights": [0, 0, 1]}
        self.assertEqual(evaluate(obs, obs, obligation)[0], "PASS")
        for cls in (blender.BlenderAdapter, live.BlenderLiveAdapter):
            adapter = cls(width=1920, height=1080, max_width=1920, max_height=1080,
                          capture_engine="cycles", capture_samples=48, max_sample_frames=4000,
                          probe_width=16, probe_height=16, probe_samples=2, probe_timeout=300,
                          **({"timeout": 300, "timeout_ceiling": 300} if cls is live.BlenderLiveAdapter else {"timeout": 300}))
            self.assertEqual((adapter.capture_engine, adapter.capture_samples), ("cycles", 48))
            with self.assertRaises(ValueError): cls(capture_samples=0)


class SideReviewTests(unittest.TestCase):
    def fake(self):
        scene = NS(frame_current=3,
                   render=NS(engine="BLENDER_WORKBENCH", resolution_x=600, resolution_y=400, resolution_percentage=75),
                   cycles=NS(device="CPU", samples=64, use_denoising=True),
                   view_settings=NS(view_transform="AgX", look="None"))
        scene.frame_set = lambda frame: setattr(scene, "frame_current", frame)
        devices = [NS(name="G", type="CUDA", use=False), NS(name="CPU", type="CPU", use=True)]
        prefs = NS(compute_device_type="NONE", devices=devices, refresh_devices=lambda: None)
        bpy = NS(context=NS(scene=scene, preferences=NS(addons={"cycles": NS(preferences=prefs)}),
                            view_layer=NS(update=lambda: None)), ops=NS(render=NS(render=lambda: None)))
        return bpy, scene, prefs

    def snapshot(self, scene, prefs):
        return copy.deepcopy([vars(scene.render), vars(scene.cycles), vars(scene.view_settings),
                              scene.frame_current, prefs.compute_device_type, [d.use for d in prefs.devices]])

    def test_gpu_settings_restored_on_success_and_error(self):
        bpy, scene, prefs = self.fake()
        before = self.snapshot(scene, prefs)
        self.assertTrue(side.gpu_probe(bpy, "CUDA", width=16, samples=2))
        self.assertEqual(self.snapshot(scene, prefs), before)
        bpy.ops.render.render = lambda: (_ for _ in ()).throw(RuntimeError("render_failed"))
        with self.assertRaisesRegex(RuntimeError, "render_failed"): side.gpu_probe(bpy, "CUDA")
        self.assertEqual(self.snapshot(scene, prefs), before)

    def test_capture_engine_samples_and_denoising_are_configurable(self):
        bpy, scene, prefs = self.fake()
        scene.render.image_settings = NS(file_format="PNG", color_mode="RGB", color_depth="8")
        scene.display = NS(shading=NS(light="STUDIO", color_type="MATERIAL", show_shadows=False, show_cavity=False))
        before = copy.deepcopy(vars(scene.cycles))
        request = {"width": 96, "height": 72, "capture_engine": "cycles", "capture_samples": 4}
        with side.preserve_settings(bpy):
            notes = side.configure_capture(bpy, request)
            self.assertEqual(scene.render.engine, "CYCLES")
            self.assertEqual(scene.cycles.samples, 4)
            self.assertFalse(scene.cycles.use_denoising)
            self.assertEqual(json.loads(notes[0])["capture_samples"], 4)
            side.configure_capture(bpy, dict(request, capture_denoising=True))
            self.assertTrue(scene.cycles.use_denoising)
        self.assertEqual(vars(scene.cycles), before)

    def test_caps_restore_on_enum_error(self):
        bpy, scene, prefs = self.fake()
        before = self.snapshot(scene, prefs)
        def enum(owner, key):
            if key == "look": raise RuntimeError("enum_failed")
            return ["CYCLES"] if key == "engine" else ["Standard"]
        with patch.object(side, "enum_ids", side_effect=enum):
            with self.assertRaisesRegex(RuntimeError, "enum_failed"): side.render_capabilities(bpy)
        self.assertEqual(self.snapshot(scene, prefs), before)

    def test_enum_dynamic_parse_and_rna_fallback_restore(self):
        class Dynamic:
            value = "A"
            def __setattr__(self, key, value):
                if value == "__VQA3D_INVALID_ENUM__": raise TypeError("value not found in ('A', 'B')")
                object.__setattr__(self, key, value)
        owner = Dynamic()
        self.assertEqual(side.enum_ids(owner, "value"), ["A", "B"])
        self.assertEqual(owner.value, "A")
        owner = NS(value="A", bl_rna=NS(properties={"value": NS(enum_items=[NS(identifier="A"), NS(identifier="B")])}))
        self.assertEqual(side.enum_ids(owner, "value"), ["A", "B"])
        self.assertEqual(owner.value, "A")

    def test_whole_objects_triangles_images_and_fallback_exclusion(self):
        bpy, modules, _ = round2.SideFields().fake_scene()
        bpy.context.scene.objects[0].get = lambda key, default=None: key == "vqa3d_fallback_camera"
        bpy.data.images = [NS(name="tex.png", type="IMAGE", size=[256, 128])]
        with patch.dict("sys.modules", modules):
            for path in ("objects", "/objects"):
                state = side.scene_state(bpy, [path])
                self.assertEqual(set(state["objects"]), {"RIG"})
                self.assertEqual(state["images"]["tex.png"]["size_px"], [256, 128])
            bpy.context.scene.objects[0].get = lambda key, default=None: default
            state = side.scene_state(bpy)
            self.assertEqual(state["objects"]["Mesh"]["tris"], 2)
            obj = bpy.context.scene.objects[0]
            obj.type, obj.data = "LIGHT", NS(energy=12.5, color=[1, .5, 0], type="AREA")
            self.assertEqual(side.scene_state(bpy)["objects"]["Mesh"]["energy_mw"], 12500)

    def test_sample_empty_paths_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_sample_paths"):
            side.sample_scene(None, [1], [])

    def test_dotted_object_names_require_unambiguous_pointer(self):
        state = {"objects": {"Cube.001 Big": {"location_um": [1, 2, 3]}}}
        self.assertEqual(side.lookup(state, "/objects/Cube.001 Big/location_um"), [1, 2, 3])
        with self.assertRaisesRegex(ValueError, "blender_object_paths_require_json_pointer"):
            side.lookup(state, "objects.Cube.001 Big.location_um")

    def test_missing_camera_provenance_stays_unknown(self):
        scene = NS(camera=NS(name="CAM"), get=lambda key, default: default)
        bpy = NS(context=NS(scene=scene), app=NS(version_string="test"))
        with tempfile.TemporaryDirectory() as root:
            request, response = Path(root)/"request.json", Path(root)/"response.json"
            request.write_text('{"mode":"state"}')
            with patch.dict("sys.modules", {"bpy": bpy}), patch("sys.argv", ["side", "--", str(request), str(response)]), patch.object(side, "scene_state", return_value={}):
                side.main()
            self.assertEqual(json.loads(response.read_text())["camera_source"], "unknown")
        observation = Observation.create("fake", "after", {}, camera_source="unknown", camera_name="CAM")
        self.assertEqual(evaluate(observation, observation, {"kind": "camera_is", "name": "CAM"})[0], "FAIL")

    def test_bbox_clips_near_and_far_with_crossing_edges(self):
        class Vec(list):
            x = property(lambda self: self[0])
            y = property(lambda self: self[1])
            z = property(lambda self: self[2])
            def lerp(self, other, t): return Vec([a+(b-a)*t for a,b in zip(self, other)])
        class Inverse:
            def __matmul__(self, value): return Vec([value[0], value[1], -value[2]])
        camera = NS(matrix_world=NS(inverted=lambda: Inverse()), data=NS(clip_start=.1, clip_end=2))
        points = [Vec([x,y,z]) for x in (-1,1) for y in (-1,1) for z in (-1,3)]
        clipped = side.clip_bbox(points, camera)
        self.assertTrue(clipped)
        self.assertTrue(all(.1-1e-10 <= point.z <= 2+1e-10 for point in clipped))
        projected = [NS(x=.5+point.x/(2*point.z), y=.5+point.y/(2*point.z)) for point in clipped]
        self.assertGreater(side.projected_fraction(projected), 0)

    def test_typed_action_reasons(self):
        with self.assertRaisesRegex(ValueError, "unsupported_action_op:unknown"):
            blender.validate_action({"op": "unknown"})
        with self.assertRaisesRegex(ValueError, "object_not_found:Lost"):
            side.apply(NS(data=NS(objects={})), dict(MOVE, object="Lost"))

    def test_bridge_private_directories(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"VQA3D_LIVE_DIR": temp}):
            bpy = NS(app=NS(driver_namespace={}, timers=NS(register=lambda *a, **kw: None)))
            old = os.umask(0o000)
            try: bridge.install(bpy)
            finally: os.umask(old)
            for path in (Path(temp), Path(temp)/"inbox", Path(temp)/"outbox"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)


class LiveReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = FakeBridge(self.temp.name)
        self.adapter = live.BlenderLiveAdapter(self.temp.name, timeout=.25, frame_timeout=.12, poll_interval=.003)

    def tearDown(self):
        self.bridge.close()
        self.adapter.close()
        self.temp.cleanup()

    def test_float_matched_response_and_silent_after_action_are_errors(self):
        self.bridge.state["float"] = 1.5
        result = verify_step(self.adapter, MOVE, {"kind": "frame_change"})
        self.assertEqual(result["final_verdict"], "ERROR")
        self.assertIn("float", result["error"])
        del self.bridge.state["float"]
        original = self.adapter.act
        def act(action):
            result = original(action)
            self.bridge.silent = True
            return result
        self.adapter.act = act
        result = verify_step(self.adapter, MOVE, {"kind": "frame_change"})
        self.assertEqual(result["final_verdict"], "ERROR")
        self.assertIn("live_bridge_timeout", result["error"])

    def test_receipt_seals_action_and_counters(self):
        receipt = self.adapter.act(MOVE)
        digest = receipt.pop("receipt_sha256")
        self.assertEqual(sha256_canonical(receipt), digest)
        for field in ("frame_seq", "depsgraph_seq", "before_frame_seq"):
            changed = copy.deepcopy(receipt); changed[field] += 1
            self.assertNotEqual(sha256_canonical(changed), digest)
        receipt["action"]["value_um"][0] += 1
        self.assertNotEqual(sha256_canonical(receipt), digest)
        self.assertEqual(MOVE["value_um"][0], 1000000)

    def test_probe_requires_view3d(self):
        self.bridge.has_view3d = False
        probe = self.adapter.probe()
        self.assertFalse(probe["available"])
        self.assertIn("view3d_context_missing", probe["reason"])

    def test_capture_deadline_and_viewport_cleanup(self):
        self.adapter.act(MOVE)
        original = self.adapter._exchange
        deadlines = []
        def exchange(mode, **fields):
            if mode == "capture": deadlines.append(fields.get("deadline"))
            return original(mode, **fields)
        self.adapter._exchange = exchange
        self.adapter.capture("after")
        self.assertIsInstance(deadlines[0], float)
        self.adapter.viewport = True
        def capture(mode, **fields):
            result, status = original(mode, **fields)
            if mode == "capture":
                (Path(self.temp.name)/(result["request_id"]+".viewport.png")).write_bytes(b"viewport")
            return result, status
        self.adapter._exchange = capture
        self.adapter.capture("viewport")
        self.assertEqual(list(Path(self.temp.name).glob("*.png")), [])

    def test_capture_stall_respects_shared_frame_deadline(self):
        self.adapter.timeout = 1
        self.adapter.frame_timeout = .1
        self.adapter.act(MOVE)
        original = self.adapter._exchange
        def exchange(mode, **fields):
            if mode == "capture": self.bridge.silent = True
            return original(mode, **fields)
        self.adapter._exchange = exchange
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "live_bridge_timeout:capture"):
            self.adapter.capture("after")
        self.assertLess(time.monotonic()-started, .5)

    def test_payload_renders_before_state_and_counters(self):
        events = []
        scene = NS(camera=NS(name="Camera", get=lambda k,d:d), get=lambda k,d:d)
        class Scene(NS):
            def __setitem__(self, key, value): setattr(self, key, value)
        scene = Scene(**vars(scene))
        bpy = NS(context=NS(scene=scene, window_manager=NS(windows=[NS(screen=NS(areas=[NS(type="VIEW_3D")]))])),
                 app=NS(version_string="fake", background=False))
        seq = {"frame_seq": 2, "depsgraph_seq": 3}
        def render(*args): events.append("render"); seq["depsgraph_seq"] = 4
        def state(*args): events.append("state"); return {"after_render": int(events[0] == "render")}
        with patch.object(payload, "counters", return_value=seq), patch.object(payload, "side_module"), patch.object(payload, "redraw"), patch.object(payload, "scene_state", side_effect=state), patch.object(payload, "render", side_effect=render):
            result = payload.dispatch(bpy, {"mode": "capture", "request_id": "test"})
        self.assertEqual(events, ["render", "state"])
        self.assertEqual(result["depsgraph_seq"], 4)


if __name__ == "__main__":
    unittest.main()
