"""Adapter boundary failures, checked with no optional runtimes installed."""
import contextlib
import io
import json
import math
import os
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blender_visual_qa.drive import containment
from blender_visual_qa.drive.png import encode
from vqa3d.adapters.blender import BlenderAdapter, validate_action
from vqa3d.adapters import blender_side
from vqa3d.adapters.mujoco_sim import MuJoCoSimulationAdapter
from vqa3d.__main__ import main
from vqa3d.check import verify_step

ROOT = Path(__file__).resolve().parents[1]


def simulation_runtime(*, render_error=None):
    model = SimpleNamespace(nu=1)
    data = SimpleNamespace(time=0.0, qpos=[0.0], qvel=[0.0], ctrl=[0.0], ncon=0, contact=[])
    def renderer(*args, **kwargs):
        raise RuntimeError(render_error or "no_renderer_in_unit_test")
    def step(model, data):
        data.time += 0.002
        data.qpos[0] += 0.01
        data.qvel[0] = 1.0
    return SimpleNamespace(__version__="recorded-unit-runtime", MjModel=SimpleNamespace(
        from_xml_string=lambda text: model, from_xml_path=lambda path: model),
        MjData=lambda model: data, mj_forward=lambda model, data: None, mj_step=step,
        Renderer=renderer)


class SimulationBoundaryTests(unittest.TestCase):
    def test_missing_runtime_probe_is_unavailable_not_stub(self):
        adapter = MuJoCoSimulationAdapter()
        with patch('vqa3d.adapters.mujoco_sim.importlib.import_module', side_effect=ModuleNotFoundError("mujoco")):
            result = adapter.probe()
        self.assertEqual(result["status"], "implemented")
        self.assertFalse(result["available"])
        self.assertFalse(result["capabilities"]["capture_available"])
        adapter.close()

    def test_renderer_failure_preserves_real_state_and_cannot_pass_pixels(self):
        with patch('vqa3d.adapters.mujoco_sim.importlib.import_module', return_value=simulation_runtime()), \
                patch.dict(os.environ, {"MUJOCO_GL": "egl"}):
            adapter = MuJoCoSimulationAdapter()
            observation = adapter.capture("before")
            self.assertFalse(observation.capture_available)
            self.assertIsNone(observation.frame_png)
            self.assertEqual(observation.state["qpos"], [0])
            result = verify_step(adapter, {"op": "step", "n": 1}, {"kind": "frame_change"})
            self.assertEqual(result["final_verdict"], "CANNOT_SEE")
            result = verify_step(adapter, {"op": "step", "n": 1},
                                 {"kind": "state_change", "keys": ["qpos"], "allow_changes": ["time_us", "qvel"]})
            self.assertEqual(result["final_verdict"], "PASS")
            adapter.close()

    def test_missing_capture_cannot_clear_known_no_change_violation(self):
        with patch('vqa3d.adapters.mujoco_sim.importlib.import_module', return_value=simulation_runtime()), \
                patch.dict(os.environ, {"MUJOCO_GL": "egl"}):
            adapter = MuJoCoSimulationAdapter()
            receipt = verify_step(adapter, {"op": "step", "n": 1}, {"kind": "no_change"})
            self.assertEqual(receipt["final_verdict"], "FAIL")
            adapter.close()

    def test_simulation_rejects_boolean_indices_steps_and_float_control(self):
        with patch('vqa3d.adapters.mujoco_sim.importlib.import_module', return_value=simulation_runtime()), \
                patch.dict(os.environ, {"MUJOCO_GL": "egl"}):
            adapter = MuJoCoSimulationAdapter()
            before = adapter.scene_state()
            for action in ({"op": "step", "n": True}, {"op": "step", "n": 0},
                           {"op": "step", "n": 10001}, {"op": "set_ctrl", "index": True, "value_milli": 1},
                           {"op": "set_ctrl", "index": 1, "value_milli": 1},
                           {"op": "set_ctrl", "index": 0, "value_milli": 0.1}):
                with self.subTest(action=action), self.assertRaises(ValueError):
                    adapter.act(action)
            self.assertEqual(adapter.scene_state(), before)
            adapter.close()
            with self.assertRaises(RuntimeError):
                adapter.scene_state()

    def test_other_gl_backend_is_refused_before_import(self):
        adapter = MuJoCoSimulationAdapter()
        with patch.dict(os.environ, {"MUJOCO_GL": "glfw"}), \
                patch('vqa3d.adapters.mujoco_sim.importlib.import_module') as imported:
            with self.assertRaises(RuntimeError):
                adapter.scene_state()
            result = adapter.probe()
            self.assertFalse(result['available'])
            self.assertIn('unsupported_MUJOCO_GL:glfw', result['reason'])
            self.assertEqual(os.environ['MUJOCO_GL'], 'glfw')
            imported.assert_not_called()
        adapter.close()

    def test_probe_catches_import_and_model_errors(self):
        for failure in (ModuleNotFoundError('missing'), RuntimeError('import failed')):
            with self.subTest(failure=str(failure)), patch.dict(os.environ, {'MUJOCO_GL': 'egl'}), \
                    patch('vqa3d.adapters.mujoco_sim.importlib.import_module', side_effect=failure):
                adapter = MuJoCoSimulationAdapter()
                result = adapter.probe()
                self.assertFalse(result['available'])
                self.assertFalse(any(result['capabilities'].values()))
                self.assertIn(str(failure), result['reason'])
                adapter.close()
        runtime = simulation_runtime()
        def invalid_model(text):
            raise ValueError('invalid MJCF')
        runtime.MjModel.from_xml_string = invalid_model
        with patch.dict(os.environ, {'MUJOCO_GL': 'egl'}), \
                patch('vqa3d.adapters.mujoco_sim.importlib.import_module', return_value=runtime):
            adapter = MuJoCoSimulationAdapter(mjcf='<invalid/>')
            result = adapter.probe()
            self.assertFalse(result['available'])
            self.assertFalse(any(result['capabilities'].values()))
            self.assertIn('invalid MJCF', result['reason'])
            self.assertIsNone(adapter.model)
            adapter.close()

    def test_probe_render_failure_is_sticky_but_state_remains_usable(self):
        runtime = simulation_runtime(render_error='render failed')
        with patch.dict(os.environ, {'MUJOCO_GL': 'egl'}), \
                patch('vqa3d.adapters.mujoco_sim.importlib.import_module', return_value=runtime), \
                patch.object(runtime, 'Renderer', wraps=runtime.Renderer) as renderer:
            adapter = MuJoCoSimulationAdapter()
            result = adapter.probe()
            self.assertFalse(result['available'])
            self.assertEqual(result['backend'], 'egl')
            self.assertEqual(result['capabilities'], {'act': True, 'scene_state': True, 'capture_available': False})
            self.assertIn('render failed', result['reason'])
            first, second = adapter.capture('first'), adapter.capture('second')
            self.assertEqual(first.notes, second.notes)
            self.assertEqual(renderer.call_count, 1)
            adapter.close()
            self.assertFalse(adapter.probe()['available'])

    def test_backend_default_precedes_import_and_detects_preimported_backend(self):
        with patch.dict(os.environ, {}, clear=True):
            def imported(name):
                self.assertEqual(os.environ['MUJOCO_GL'], 'egl')
                return simulation_runtime()
            with patch('vqa3d.adapters.mujoco_sim.importlib.import_module', side_effect=imported):
                adapter = MuJoCoSimulationAdapter()
                self.assertEqual(adapter.scene_state()['time_us'], 0)
                self.assertEqual(adapter.backend, 'egl')
                adapter.close()
        context = type('GLContext', (), {'__module__': 'mujoco.glfw'})
        with patch.dict(os.environ, {'MUJOCO_GL': 'egl'}), \
                patch.dict(sys.modules, {'mujoco': SimpleNamespace(GLContext=context)}):
            adapter = MuJoCoSimulationAdapter()
            result = adapter.probe()
            self.assertFalse(result['available'])
            self.assertEqual(result['backend'], 'glfw')
            self.assertEqual(os.environ['MUJOCO_GL'], 'egl')
            adapter.close()


class BlenderBoundaryTests(unittest.TestCase):
    def test_captive_cuda_is_opt_in_only_for_the_headless_cuda_diagnostic(self):
        with self.assertRaisesRegex(ValueError, "invalid_captive_cuda_profile"):
            BlenderAdapter(captive_cuda=True, profile_session=True)
        with self.assertRaisesRegex(ValueError, "invalid_captive_cuda_profile"):
            BlenderAdapter(captive_cuda=1)

        launches = []
        reported_devices = [containment.CUDA_DEVICE]

        @contextlib.contextmanager
        def owned(argv, scratch, **settings):
            request = json.loads((scratch / "request.json").read_text())
            mode = request["mode"]
            self.assertIn("-b", argv)
            self.assertIs(request["session"], False)
            launches.append((mode, settings.get("captive_cuda"), settings.get("devices")))
            stdout, stderr = scratch / "stdout", scratch / "stderr"
            stdout.touch()
            stderr.touch()
            response = {"ok": True, "runtime_version": "offline fixture"}
            if mode == "gpu_probe":
                response.update(gpu_render_ok=True, gpu_devices=reported_devices)
                (scratch / "gpu-probe.png").write_bytes(encode(8, 8, bytes(8 * 8 * 3)))
            (scratch / "response.json").write_text(json.dumps(response))
            proc = SimpleNamespace(pid=42, stdin=SimpleNamespace(close=lambda: None),
                                   wait=lambda timeout: 0, returncode=0)
            yield proc, stdout, stderr, {"admitted": True}

        for selected in (False, True):
            with self.subTest(selected=selected), containment.workspace() as scratch:
                adapter = BlenderAdapter(captive_cuda=selected)
                adapter.scratch = scratch
                adapter.blend = scratch / "scene.blend"
                with patch.object(adapter, "_prepare"), \
                        patch("vqa3d.adapters.blender.BLENDER", Path(sys.executable).resolve()), \
                        patch("vqa3d.adapters.blender.worker.admitted_process", owned), \
                        patch("vqa3d.adapters.blender.worker.cuda_device_nodes", return_value=containment.CUDA_NODES), \
                        patch("vqa3d.adapters.blender.os.getpgid", return_value=42):
                    adapter._invoke("capture")
                    self.assertTrue(adapter.gpu_probe("CUDA"))
                    self.assertFalse(adapter.gpu_probe("OPTIX"))
                    if selected:
                        reported_devices[:] = ["foreign device"]
                        self.assertFalse(adapter.gpu_probe("CUDA"))
                        self.assertIn("gpu_artifact_or_device_unverified", adapter.gpu_probe_diagnostics["CUDA"]["reason"])
        self.assertEqual(launches, [
            ("capture", None, None), ("gpu_probe", None, containment.CUDA_NODES),
            ("capture", None, None), ("gpu_probe", True, containment.CUDA_NODES),
            ("gpu_probe", True, containment.CUDA_NODES),
        ])

    def test_captive_cuda_missing_devices_refuse_before_publishing_request(self):
        with containment.workspace() as scratch:
            adapter = BlenderAdapter(captive_cuda=True)
            adapter.scratch = scratch
            with patch.object(adapter, "_prepare"), \
                    patch("vqa3d.adapters.blender.worker.cuda_device_nodes", return_value=()), \
                    patch("vqa3d.adapters.blender.worker.admitted_process") as launch:
                self.assertFalse(adapter.gpu_probe("CUDA"))
            self.assertIn("captive_cuda_requires_headless_exact_devices", adapter.gpu_probe_diagnostics["CUDA"]["reason"])
            launch.assert_not_called()
            self.assertFalse((scratch / "request.json").exists())

    def test_scratch_is_a_copy_and_close_removes_only_owned_files(self):
        with containment.workspace() as temp:
            source = temp / 'source.blend'
            source.write_bytes(b'original blend')
            adapter = BlenderAdapter(source, scratch_parent=temp)
            with patch('vqa3d.adapters.blender.BLENDER', source), patch.object(adapter, '_invoke'):
                adapter._prepare()
                self.assertNotEqual(adapter.blend, source)
                adapter.blend.write_bytes(b'changed scratch')
                scratch = adapter.scratch
            adapter.close()
            self.assertFalse(scratch.exists())
            self.assertEqual(source.read_bytes(), b'original blend')
            with self.assertRaises(RuntimeError):
                adapter.capture('after')

    def test_timeout_exits_owned_context_and_retains_recorded_pid(self):
        cleaned = []
        process = SimpleNamespace(pid=12345, stdin=SimpleNamespace(close=lambda: None))
        def wait(timeout):
            raise subprocess.TimeoutExpired(['blender'], timeout)
        process.wait = wait
        @contextlib.contextmanager
        def owned(*args, **kwargs):
            self.assertEqual(kwargs['env']['PATH'], '/usr/bin:/bin')
            self.assertNotIn('SECRET', kwargs['env'])
            try:
                scratch = args[1]
                stdout, stderr = scratch / "stdout", scratch / "stderr"
                stdout.write_text("partial log")
                stderr.write_text("")
                yield process, stdout, stderr, {"admitted": True}
            finally:
                cleaned.append(process.pid)
        with tempfile.TemporaryDirectory(dir=ROOT / 'vqa3d') as temp:
            adapter = BlenderAdapter(scratch_parent=temp, timeout=1)
            adapter.scratch = Path(temp)
            adapter.blend = Path(temp) / 'scene.blend'
            with patch('vqa3d.adapters.blender.worker.admitted_process', owned), \
                    patch('vqa3d.adapters.blender.os.getpgid', return_value=54321):
                with self.assertRaises(TimeoutError):
                    adapter._invoke('capture')
            self.assertEqual(cleaned, [12345])
            self.assertEqual(adapter.process_logs[-1]['pgid'], 54321)
            self.assertTrue(adapter.process_logs[-1]['timeout'])
            self.assertEqual(adapter.process_logs[-1]['output'], 'partial log')
            adapter.close()

    def test_failed_capture_declares_no_pixels_or_state_comparison(self):
        adapter = BlenderAdapter()
        self.assertFalse(adapter.capabilities['capture_available'])
        with patch.object(adapter, '_prepare', side_effect=RuntimeError('unavailable')):
            receipt = verify_step(adapter, {}, {'kind': 'frame_change'})
        self.assertEqual(receipt['final_verdict'], 'ERROR')
        self.assertFalse(adapter.capabilities['capture_available'])
        self.assertFalse(receipt['state_comparison_available'])
        self.assertIs(type(receipt['state_sha256_change']), bool)
        self.assertFalse(receipt['before']['capture_available'])
        self.assertIsNone(receipt['before']['state_sha256'])
        adapter.close()

    def test_real_timeout_reaps_owned_process_group(self):
        with containment.workspace() as temp:
            binary = temp / "fixture-blender"
            binary.write_text("#!/usr/bin/python3\nimport time; time.sleep(60)\n")
            binary.chmod(0o700)
            adapter = BlenderAdapter(timeout=1)
            adapter.scratch = temp
            adapter.blend = temp / "scene.blend"
            adapter.blend.write_bytes(b"fixture")
            with patch('vqa3d.adapters.blender.BLENDER', binary):
                with self.assertRaises(TimeoutError):
                    adapter._invoke('capture')
            record = adapter.process_logs[-1]
            self.assertEqual(record['pgid'], record['pid'])
            self.assertTrue(record['timeout'])
            self.assertTrue(record['containment']['admitted'])
            with self.assertRaises(ProcessLookupError):
                os.getpgid(record['pid'])
            adapter.close()

    def test_probe_missing_binary_source_start_and_render_errors_never_raise(self):
        executable = Path(sys.executable).resolve()
        for missing in ('binary', 'source'):
            adapter = BlenderAdapter(ROOT / 'vqa3d' / 'missing.blend' if missing == 'source' else None)
            binary = ROOT / 'vqa3d' / 'missing-binary' if missing == 'binary' else executable
            with patch('vqa3d.adapters.blender.BLENDER', binary):
                result = adapter.probe()
            self.assertFalse(result['available'])
            self.assertFalse(any(result['capabilities'].values()))
            self.assertIn('unavailable', result['reason'])
            adapter.close()
        for failure in (FileNotFoundError('failed start'), RuntimeError('failed render')):
            adapter = BlenderAdapter()
            with patch('vqa3d.adapters.blender.BLENDER', executable), \
                    patch.object(adapter, '_invoke', side_effect=failure):
                result = adapter.probe()
            self.assertFalse(result['available'])
            self.assertIn(str(failure), result['reason'])
            self.assertFalse(any(result['capabilities'].values()))
            self.assertIsNone(adapter._tmp)
            adapter.close()
            self.assertFalse(adapter.probe()['available'])
        adapter = BlenderAdapter()
        with patch.object(adapter, '_prepare'), patch.object(adapter, 'capture', side_effect=RuntimeError('render failed')):
            self.assertIn('render failed', adapter.probe()['reason'])
        adapter.close()

    def test_quantised_actions_reject_floats_bools_and_old_units_before_prepare(self):
        for op, field in (('set_location', 'value_um'), ('set_rotation', 'value_mdeg'), ('set_scale', 'value_ppm')):
            validate_action({'op': op, 'object': 'Cube', field: [1, 0, -1]})
            for invalid in ([0.5, 0, 0], [True, 0, 0], [0, 0], [0, 0, '1']):
                adapter = BlenderAdapter()
                with patch.object(adapter, '_prepare') as prepared, self.assertRaises(ValueError):
                    adapter.act({'op': op, 'object': 'Cube', field: invalid})
                prepared.assert_not_called()
                adapter.close()
        for value in (True, 0.0, [1], 2):
            with self.assertRaises(ValueError):
                validate_action({'op': 'hide', 'object': 'Cube', 'value': value})
        with self.assertRaises(ValueError):
            validate_action({'op': 'set_location', 'object': 'Cube', 'value': [1, 0, 0]})

    def test_side_converts_quantised_transforms_and_rejects_float_boundary(self):
        obj = SimpleNamespace(hide_render=False)
        bpy = SimpleNamespace(data=SimpleNamespace(objects={'Cube': obj}),
                              context=SimpleNamespace(view_layer=SimpleNamespace(update=lambda: None)))
        for op, field, attr, values, expected in (
                ('set_location', 'value_um', 'location', [500000, 0, -1000000], [.5, 0, -1]),
                ('set_rotation', 'value_mdeg', 'rotation_euler', [180000, 0, 0], [math.pi, 0, 0]),
                ('set_scale', 'value_ppm', 'scale', [1000000, 2000000, 1000000], [1, 2, 1])):
            blender_side.apply(bpy, {'op': op, 'object': 'Cube', field: values})
            self.assertEqual(getattr(obj, attr), expected)
            with self.assertRaises(ValueError):
                blender_side.apply(bpy, {'op': op, 'object': 'Cube', field: [.5, 0, 0]})

    def test_invalid_dimensions_and_boolean_limits_are_refused(self):
        for kwargs in ({'width': 97}, {'height': 73}, {'width': True}, {'timeout': True}, {'timeout': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BlenderAdapter(**kwargs)

    def test_fallback_camera_preserves_existing_camera(self):
        camera = SimpleNamespace(location=(1, 2, 3), rotation_euler=(4, 5, 6),
                                 data=SimpleNamespace(type='PERSP', ortho_scale=17))
        scene = SimpleNamespace(camera=camera)
        before = copy.deepcopy(camera)
        blender_side.ensure_camera(None, scene)
        self.assertIs(scene.camera, camera)
        self.assertEqual(camera, before)
        linked = []
        class IDObject(SimpleNamespace):
            def __setitem__(self, key, value): setattr(self, key, value)
        bpy = SimpleNamespace(data=SimpleNamespace(
            objects=SimpleNamespace(new=lambda name, data: IDObject(data=data)),
            cameras=SimpleNamespace(new=lambda name: SimpleNamespace())))
        scene = SimpleNamespace(camera=None, collection=SimpleNamespace(objects=SimpleNamespace(link=linked.append)))
        blender_side.ensure_camera(bpy, scene)
        self.assertEqual(linked, [scene.camera])
        self.assertEqual(scene.camera.location, (5, -7, 5))
        self.assertEqual(scene.camera.data.type, 'ORTHO')


class ProbeCLITests(unittest.TestCase):
    def test_noncanonical_scenario_refuses_before_any_adapter_and_preserves_trace(self):
        valid = {'action': {'op': 'set_location', 'object': 'Cube', 'value_um': [1000000, 0, 0]},
                 'obligation': {'kind': 'state_change', 'keys': ['/objects/Cube/location_um']}}
        for invalid in (0.5, True):
            bad = copy.deepcopy(valid)
            bad['action']['value_um'][0] = invalid
            with tempfile.TemporaryDirectory(dir=ROOT / 'vqa3d') as temp:
                scenario, trace = Path(temp) / 'scenario.json', Path(temp) / 'trace.json'
                scenario.write_text(json.dumps([valid, bad]))
                trace.write_text('original')
                with patch('vqa3d.__main__.create_adapter') as create, contextlib.redirect_stdout(io.StringIO()):
                    rc = main(['check', '--adapter', 'blender', '--scenario', str(scenario), '--record-trace', str(trace)])
                self.assertEqual(rc, 2)
                create.assert_not_called()
                self.assertEqual(trace.read_text(), 'original')

    def test_all_adapters_report_even_if_probe_constructor_or_cleanup_raises(self):
        def create(name):
            if name == 'blender':
                raise FileNotFoundError('failed constructor')
            if name == 'mujoco':
                def broken():
                    raise RuntimeError('failed import')
                return SimpleNamespace(probe=broken, close=broken)
            from vqa3d.registry import create_adapter
            return create_adapter(name)
        output = io.StringIO()
        with patch('vqa3d.__main__.create_adapter', side_effect=create), contextlib.redirect_stdout(output):
            rc = main(['probe'])
        self.assertEqual(rc, 3)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['id'] for row in rows], ['blender', 'mujoco', 'autocad', 'omniverse', 'isaac_sim', 'blender_live'])
        self.assertTrue(all(not row['available'] for row in rows))


if __name__ == '__main__':
    unittest.main()
