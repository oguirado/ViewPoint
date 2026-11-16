"""Host-only protocol fault injection; no bpy or installed Blender required."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from blender_visual_qa.drive.png import encode
from vqa3d.adapters import blender_live as live
from vqa3d.adapters import blender_live_payload as payload
from vqa3d.check import verify_step
from vqa3d.fakes import ReplayAdapter
from vqa3d.route_intent import route_intent

MOVE = {"op": "set_location", "object": "Cube", "value_um": [1000000, 0, 0]}
FRAME = {"kind": "frame_change"}


class FakeBridge:
    """Atomic protocol server using recorded response shapes, no script exec."""
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "inbox").mkdir()
        (self.root / "outbox").mkdir()
        self.stop = threading.Event()
        self.requests = []
        self.errors = []
        self.frame = 10
        self.deps = 2
        self.advance = True
        self.background = False
        self.camera = "Camera"
        self.has_view3d = True
        self.mismatch = False
        self.partial = False
        self.silent = False
        self.malformed = False
        self.capture_stale = False
        self.state = {"objects": {"Cube": {"location_um": [0, 0, 0]}},
                      "frame_current": 1, "active_object": "Cube", "mode": "OBJECT"}
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def publish(self, path, result, status="OK"):
        temp = path.with_suffix(".out.tmp")
        temp.write_text(status + "\n" + live.MARKER + json.dumps(result) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def serve(self):
        while not self.stop.is_set():
            for script in sorted((self.root / "inbox").glob("*.py")):
                try:
                    tree = ast.parse(script.read_text())
                    assignment = next(n for n in tree.body if isinstance(n, ast.Assign))
                    request = json.loads(ast.literal_eval(assignment.value))
                    script.unlink()
                    self.requests.append(request)
                    if self.silent:
                        continue
                    mode = request["mode"]
                    result = {"request_id": request["request_id"], "ok": True,
                              "runtime_version": "4.0.2-recorded-shape", "background": self.background,
                              "has_view3d": self.has_view3d, "camera": self.camera, "camera_source": "scene" if self.camera else "missing"}
                    if mode == "ping" and self.advance:
                        self.frame += 1
                    if mode == "act":
                        result["before_frame_seq"] = self.frame
                        self.frame += 10  # Post-action value, distinct from pre-action.
                        self.deps += 1
                        action = request["action"]
                        if action["op"] == "set_location":
                            self.state["objects"]["Cube"]["location_um"] = action["value_um"]
                        elif action["op"] == "run_script":
                            result["ok"] = False
                            result["error"] = "RuntimeError:agent script failed"
                    result.update(state=self.state, frame_seq=self.frame, depsgraph_seq=self.deps)
                    if mode == "capture":
                        if self.capture_stale and request.get("after_seq") is not None:
                            result["frame_seq"] = request["after_seq"]
                        pixel = 255 if self.state["objects"]["Cube"]["location_um"][0] else 0
                        (self.root / (request["request_id"] + ".png")).write_bytes(encode(2, 1, bytes([pixel, 0, 0] * 2)))
                    path = self.root / "outbox" / (request["request_id"] + ".out")
                    status = "OK" if result["ok"] else "ERROR"
                    if self.mismatch:
                        foreign = dict(result, request_id="foreign")
                        self.publish(path, foreign, status)
                        self.stop.wait(0.06)
                    if self.partial:
                        path.write_text("OK\n" + live.MARKER + '{"request_id":')
                        self.stop.wait(0.03)
                    if self.malformed:
                        result["frame_seq"] = True
                    self.publish(path, result, status)
                except FileNotFoundError as exc:
                    if exc.filename != str(script):
                        self.errors.append(repr(exc))  # Only host cancellation of this request is expected.
                except Exception as exc:
                    self.errors.append(repr(exc))
            self.stop.wait(0.002)

    def close(self):
        self.stop.set()
        self.thread.join(2)


class LiveProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = FakeBridge(self.tmp.name)
        self.adapter = live.BlenderLiveAdapter(self.tmp.name, timeout=0.3, frame_timeout=0.12, poll_interval=0.005)

    def tearDown(self):
        self.bridge.close()
        self.adapter.close()
        self.tmp.cleanup()
        self.assertFalse(self.bridge.errors)

    def test_probe_ok_and_state(self):
        probe = self.adapter.probe()
        self.assertTrue(probe["available"])
        self.assertFalse(probe["background"])
        self.assertEqual(probe["camera"], "Camera")
        self.assertEqual(probe["runtime_version"], "4.0.2-recorded-shape")
        self.assertGreater(probe["frame_seq"], 0)
        self.assertEqual(self.adapter.scene_state(), self.bridge.state)

    def test_probe_background_refused(self):
        self.bridge.background = True
        self.assertFalse(self.adapter.probe()["available"])

    def test_probe_missing_camera_refused(self):
        self.bridge.camera = None
        result = self.adapter.probe()
        self.assertFalse(result["available"])
        self.assertIn("scene_camera_missing", result["reason"])

    def test_probe_missing_dir_and_silent_bridge(self):
        missing = live.BlenderLiveAdapter(Path(self.tmp.name) / "absent", timeout=0.03)
        self.assertFalse(missing.probe()["available"])
        self.bridge.silent = True
        self.assertFalse(self.adapter.probe()["available"])

    def test_stale_frame_cannot_see(self):
        self.bridge.advance = False
        started = time.monotonic()
        receipt = verify_step(self.adapter, MOVE, FRAME)
        self.assertEqual(receipt["final_verdict"], "CANNOT_SEE")
        self.assertFalse(receipt["after"]["capture_available"])
        self.assertGreaterEqual(time.monotonic() - started, self.adapter.frame_timeout)
        self.assertFalse(any(r["mode"] == "capture" and r.get("after_seq") is not None for r in self.bridge.requests))
        obs = self.adapter.capture("again")
        self.assertIn("stale_frame_timeout", obs.notes)

    def test_frame_advance_passes_and_records_counters(self):
        receipt = verify_step(self.adapter, MOVE, FRAME)
        self.assertEqual(receipt["final_verdict"], "PASS")
        capture = [r for r in self.bridge.requests if r["mode"] == "capture"][-1]
        self.assertEqual(capture["after_seq"], 20)
        metadata = json.loads(self.adapter.capture("extra").notes[0])
        self.assertGreater(metadata["frame_seq"], capture["after_seq"])
        self.assertEqual(metadata["camera_source"], "scene")
        self.assertEqual(metadata["depsgraph_seq"], 3)

    def test_capture_response_still_must_be_fresh(self):
        self.bridge.capture_stale = True
        receipt = verify_step(self.adapter, MOVE, FRAME)
        self.assertEqual(receipt["final_verdict"], "CANNOT_SEE")

    def test_script_ERROR_is_ERROR_with_sha_receipt(self):
        path = Path(self.tmp.name) / "agent.py"
        source = b"raise RuntimeError('agent script failed')\n"
        path.write_bytes(source)
        receipt = verify_step(self.adapter, {"op": "run_script", "path": str(path)}, FRAME)
        self.assertEqual(receipt["final_verdict"], "ERROR")
        self.assertIn(hashlib.sha256(source).hexdigest(), receipt["error"])
        self.assertIn('"status_line":"ERROR"', receipt["error"])
        sent = [r for r in self.bridge.requests if r["mode"] == "act"][-1]
        self.assertEqual(sent["script_sha256"], hashlib.sha256(source).hexdigest())

    def test_request_id_mismatch_ignored(self):
        self.bridge.mismatch = True
        started = time.monotonic()
        result = self.adapter.probe()
        self.assertTrue(result["available"])
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertNotEqual(self.adapter.process_logs[-1]["response"]["request_id"], "foreign")

    def test_partial_and_foreign_files_ignored(self):
        foreign = self.bridge.root / "outbox" / "foreign.out"
        foreign.write_text("ERROR\nforeign\n")
        self.bridge.partial = True
        self.assertTrue(self.adapter.probe()["available"])
        self.adapter.close()
        self.assertEqual(foreign.read_text(), "ERROR\nforeign\n")

    def test_floats_refused_before_sending(self):
        bad = dict(MOVE, value_um=[1.5, 0, 0])
        with self.assertRaises(ValueError):
            self.adapter.act(bad)
        self.assertEqual(self.bridge.requests, [])
        with self.assertRaises(ValueError):
            self.adapter.act(dict(MOVE, value_um=[True, 0, 0]))

    def test_malformed_counters_are_unavailable(self):
        self.bridge.malformed = True
        self.assertFalse(self.adapter.probe()["available"])

    def test_unique_request_ids(self):
        for _ in range(3):
            self.assertTrue(self.adapter.probe()["available"])
        ids = [r["request_id"] for r in self.bridge.requests]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(list((self.bridge.root / "inbox").iterdir()))


class LiveMutantTests(unittest.TestCase):
    def test_six_fault_mutants_are_caught(self):
        """Compile mutated host copies in memory; run the discriminating witness."""
        source = Path(live.__file__).read_text()
        mutants = [
            ("skip_draw_wait", "if self._pending_draw is not None:", "if False:", "test_stale_frame_cannot_see"),
            ("accept_equal_draw", 'result["frame_seq"] > self._pending_draw', 'result["frame_seq"] >= self._pending_draw', "test_stale_frame_cannot_see"),
            ("ignore_request_id", 'result.get("request_id") == request_id', "True", "test_request_id_mismatch_ignored"),
            ("skip_input_validation", "        validate_action(action)\n        with self._lock:", "        pass\n        with self._lock:", "test_floats_refused_before_sending"),
            ("accept_background", 'if result.get("background") is not False:', "if False:", "test_probe_background_refused"),
            ("accept_missing_camera", 'if not result.get("camera"):', "if False:", "test_probe_missing_camera_refused"),
        ]
        for name, old, new, witness in mutants:
            with self.subTest(mutant=name):
                self.assertIn(old, source)
                module = types.ModuleType("vqa3d.adapters._live_mutant")
                module.__package__ = "vqa3d.adapters"
                module.__file__ = live.__file__
                exec(compile(source.replace(old, new, 1), live.__file__, "exec"), module.__dict__)
                suite = unittest.TestSuite([LiveProtocolTests(witness)])
                stream = io.StringIO()
                with mock.patch.object(live, "BlenderLiveAdapter", module.BlenderLiveAdapter):
                    result = unittest.TextTestRunner(stream=stream).run(suite)
                self.assertFalse(result.wasSuccessful(), name + " survived\n" + stream.getvalue())


class LiveReplayTests(unittest.TestCase):
    def test_recorded_live_trace_replay(self):
        path = Path(__file__).resolve().parents[1] / "vqa3d/traces/blender_live_move.json"
        self.assertTrue(path.is_file(), "live acceptance must record the CI fixture")
        adapter = ReplayAdapter(path)
        for step, expected in zip(adapter.trace["scenario"], adapter.trace["receipts"]):
            receipt = verify_step(adapter, step["action"], step["obligation"])
            self.assertEqual(receipt, expected)
            self.assertEqual(receipt["final_verdict"], "PASS")
        self.assertEqual(adapter._cursor, len(adapter.trace["events"]))
        adapter.close()

    def test_recorded_stale_trace_cannot_pass(self):
        path = Path(__file__).resolve().parents[1] / "vqa3d/traces/blender_live_stale.json"
        adapter = ReplayAdapter(path)
        for step, expected in zip(adapter.trace["scenario"], adapter.trace["receipts"]):
            receipt = verify_step(adapter, step["action"], step["obligation"])
            self.assertEqual(receipt, expected)
            self.assertEqual(receipt["final_verdict"], "CANNOT_SEE")
        captures = [e["observation"] for e in adapter.trace["events"] if e["kind"] == "capture"]
        self.assertIn("stale_frame_timeout", captures[-1]["notes"])
        self.assertFalse(captures[-1]["capture_available"])
        adapter.close()


class LivePayloadTests(unittest.TestCase):
    def test_live_keyword_routing_requires_blender_terms(self):
        with mock.patch("vqa3d.route_intent.code_coordinates", return_value={"rows": ["coordinate"]}):
            for keyword in ("live", "gui", "viewport"):
                self.assertEqual(route_intent("Blender " + keyword)["adapter"], "blender_live")
                self.assertIsNone(route_intent(keyword)["adapter"])
            self.assertEqual(route_intent("move Blender object")["adapter"], "blender")
            self.assertIsNone(route_intent("Blender live mujoco")["adapter"])

    def test_handler_installation_idempotent_and_pause_hook(self):
        callbacks = []
        bpy = types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={},
            handlers=types.SimpleNamespace(depsgraph_update_post=[])),
            types=types.SimpleNamespace(SpaceView3D=types.SimpleNamespace(
                draw_handler_add=lambda function, *args: callbacks.append(function))))
        first = payload.counters(bpy)
        self.assertIs(first, payload.counters(bpy))
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(len(bpy.app.handlers.depsgraph_update_post), 1)
        callbacks[0]()
        bpy.app.handlers.depsgraph_update_post[0](None, None)
        self.assertEqual((first["frame_seq"], first["depsgraph_seq"]), (1, 1))
        first["pause_draw"] = True
        callbacks[0]()
        self.assertEqual(first["frame_seq"], 1)

    def test_render_restores_settings_even_on_error(self):
        settings = types.SimpleNamespace(file_format="JPEG", color_mode="RGBA", color_depth="16")
        render = types.SimpleNamespace(engine="CYCLES", resolution_x=1920, resolution_y=1080,
            resolution_percentage=80, filepath="original.png", use_file_extension=True, image_settings=settings,
            film_transparent=True)
        original = dict(vars(render))
        image_original = dict(vars(settings))
        def failing_render(**kwargs):
            self.assertEqual(render.resolution_x, 96)
            self.assertEqual(render.resolution_y, 72)
            self.assertEqual(render.engine, "BLENDER_WORKBENCH")
            raise RuntimeError("injected render failure")
        bpy = types.SimpleNamespace(context=types.SimpleNamespace(preferences=types.SimpleNamespace(addons={}),
            scene=types.SimpleNamespace(render=render, display=types.SimpleNamespace(shading=types.SimpleNamespace(
                light="STUDIO", color_type="MATERIAL", show_shadows=False, show_cavity=False)))),
            ops=types.SimpleNamespace(render=types.SimpleNamespace(render=failing_render)))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "injected render failure"):
                payload.render(bpy, {"live_dir": root, "request_id": "owned", "width": 96, "height": 72})
        self.assertEqual(vars(render), original)
        self.assertEqual(vars(settings), image_original)


if __name__ == "__main__":
    unittest.main()
