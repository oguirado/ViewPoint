"""Real Blender exporter qualification; no render, network, or persistent scene."""
import ast
import json
import os
from pathlib import Path
import signal
import subprocess
import unittest
from unittest import mock

from wender.perceive.blender_bpy import capture_scene, _quantize
from wender.state.blender import BlenderStateError


# [TARGET, synthetic test policy] Not a production STATE budget or calibrated optimum.
LIMITS = {"max_objects": 100, "max_vertices": 100000, "max_edges": 300000,
          "max_faces": 100000, "max_bytes": 1000000, "timeout_ms": 30000}


class TestBlenderExporterBoundary(unittest.TestCase):
    def test_expired_deadline_is_refused(self):
        from types import SimpleNamespace
        scene = SimpleNamespace(frame_subframe=0, objects=[], name="Empty", frame_current=1)
        graph = SimpleNamespace(object_instances=[], objects=[],
                                scene=SimpleNamespace(original=scene),
                                view_layer=SimpleNamespace(objects=SimpleNamespace(active=None)))
        with mock.patch("wender.perceive.blender_bpy.time.monotonic_ns", side_effect=[0, 1000001]):
            with self.assertRaises(BlenderStateError) as caught:
                capture_scene(scene, graph, blender_version="4.0.2", micrometers_per_unit=1000000,
                              limits={**LIMITS, "timeout_ms": 1})
        self.assertEqual(caught.exception.rc, 6)

    def test_explicit_units_and_budgets_required_before_scene_read(self):
        for limits, units in (({}, 1000000), (LIMITS, True), (LIMITS, 0),
                              ({**LIMITS, "timeout_ms": 0.5}, 1000000)):
            with self.subTest(limits=limits, units=units), self.assertRaises(BlenderStateError) as caught:
                capture_scene(None, None, blender_version="4.0.2",
                              micrometers_per_unit=units, limits=limits)
            self.assertEqual(caught.exception.rc, 2)

    def test_rounding_ties_even_and_nonfinite_refusal(self):
        self.assertEqual([_quantize(v, 1) for v in (-2.5, -1.5, -0.5, 0.5, 1.5, 2.5)],
                         [-2, -2, 0, 0, 2, 2])
        for value in (float("nan"), float("inf"), -float("inf"), True):
            with self.assertRaises(BlenderStateError):
                _quantize(value, 1000000)

    def test_quantization_bound_against_exact_binary_ratio(self):
        from fractions import Fraction
        # [TARGET, fixture] Dyadic coordinates, including signs and tie cases.
        for numerator in range(-256, 257):
            value = numerator / 128
            error = abs(Fraction(value) * 1000000 - _quantize(value, 1000000))
            self.assertLessEqual(error, Fraction(1, 2))

    def test_host_modules_do_not_import_blender_or_write_files(self):
        import wender.perceive.blender as host
        import wender.perceive.blender_bpy as exporter
        import wender.state.blender as state
        for module in (host, exporter, state):
            tree = ast.parse(Path(module.__file__).read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    self.assertFalse(set(names) & {"bpy", "bmesh", "mathutils", "gpu"})
            self.assertNotIn("visioncore", Path(module.__file__).read_text())


class TestRealBlender(unittest.TestCase):
    def test_real_scene_is_stable_read_only_and_negative_controls_refuse(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
import copy, hashlib, json, sys
import bpy
sys.path.insert(0, sys.argv[-1])
from wender.perceive.blender import validate_blender_state
from wender.perceive.blender_bpy import capture_scene
from wender.state import schema
from wender.state.blender import BlenderStateError
limits = {"max_objects":100,"max_vertices":100000,"max_edges":300000,
          "max_faces":100000,"max_bytes":1000000,"timeout_ms":30000}
def export(**changes):
    bpy.context.view_layer.update()
    return capture_scene(bpy.context.scene, bpy.context.evaluated_depsgraph_get(),
                         blender_version=bpy.app.version_string,
                         micrometers_per_unit=1000000, limits={**limits, **changes})
def refused(call, rc):
    try:
        call()
    except BlenderStateError as exc:
        assert exc.rc == rc, (str(exc), exc.rc, rc)
        return exc.rc
    raise AssertionError("negative control accepted")
before = [(o.name_full, o.type, tuple(tuple(r) for r in o.matrix_world)) for o in bpy.context.scene.objects]
first, second = export(), export()
assert schema.dumps(first) == schema.dumps(second)
validate_blender_state(first, max_bytes=limits["max_bytes"])
assert schema.validate_state(first["state"]) == []
assert first["state"]["source"]["kind"] == "blender"
cube_entry = next(o for o in first["blender"]["scene"]["objects"] if o["name"] == "Cube")
assert cube_entry["mesh"]["bounds_um"] == [-1000000]*3 + [1000000]*3
assert cube_entry["basis_ppm"] == [1000000,0,0,0,1000000,0,0,0,1000000]
cube = next(o for o in first["blender"]["scene"]["objects"] if o["name"] == "Cube")
assert cube["mesh"]["vertices"] == 8 and cube["mesh"]["faces"] == 6
assert cube["mesh"]["boundary_edges"] == cube["mesh"]["nonmanifold_edges"] == 0
assert cube["mesh"]["bounds_um"] == [-1000000]*3 + [1000000]*3
after = [(o.name_full, o.type, tuple(tuple(r) for r in o.matrix_world)) for o in bpy.context.scene.objects]
assert before == after
codes = [refused(lambda:export(max_vertices=1), 6), refused(lambda:export(max_bytes=1), 6)]
bad = copy.deepcopy(first)
bad["blender"]["scene"]["objects"][0]["basis_ppm"][0] = 1.0
codes.append(refused(lambda:validate_blender_state(bad, max_bytes=limits["max_bytes"]), 2))
# Exact native identity retains changes below the summary's integer grid.
obj = bpy.data.objects["Cube"]
old = obj.data.vertices[0].co.x
obj.data.vertices[0].co.x = old + 0.0000001
obj.data.update()
changed = export()
assert schema.dumps(first) != schema.dumps(changed)
obj.data.vertices[0].co.x = old
obj.data.update()
assert schema.dumps(first) == schema.dumps(export())
# A modifier must affect evaluated geometry without modifying the source mesh.
modifier = obj.modifiers.new("Measured subdivision", "SUBSURF")
modifier.levels = 1
subdivided = export()
subcube = next(o for o in subdivided["blender"]["scene"]["objects"] if o["name"] == "Cube")
assert subcube["mesh"]["vertices"] > 8 and len(obj.data.vertices) == 8
obj.modifiers.remove(modifier)
# Open surface and wire edges are reported rather than hidden or passed.
mesh = bpy.data.meshes.new("Open mesh")
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),(2,2,0),(3,2,0)],[(3,4)],[(0,1,2)])
opened = bpy.data.objects.new("Open", mesh)
bpy.context.scene.collection.objects.link(opened)
open_entry = next(o for o in export()["blender"]["scene"]["objects"] if o["name"] == "Open")
assert open_entry["mesh"]["boundary_edges"] == 3
assert open_entry["mesh"]["nonmanifold_edges"] == 4
bpy.data.objects.remove(opened, do_unlink=True)
# Edit mode is an incomplete observation, not a fabricated object-mode result.
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
codes.append(refused(export, 3))
bpy.ops.object.mode_set(mode="OBJECT")
bpy.context.scene.frame_subframe = 0.5
codes.append(refused(export, 3))
bpy.context.scene.frame_subframe = 0
prototype = bpy.data.collections.new("Prototype")
prototype.objects.link(obj.copy())
instance = bpy.data.objects.new("Instance", None)
instance.instance_type = "COLLECTION"
instance.instance_collection = prototype
bpy.context.scene.collection.objects.link(instance)
assert refused(export, 3) == 3
bpy.data.objects.remove(instance, do_unlink=True)
obj.hide_viewport = True
assert refused(export, 3) == 3
obj.hide_viewport = False
other_scene = bpy.data.scenes.new("Other")
assert refused(lambda: capture_scene(other_scene, bpy.context.evaluated_depsgraph_get(),
    blender_version=bpy.app.version_string, micrometers_per_unit=1000000, limits=limits), 4) == 4
print("S3U2_RESULT=" + json.dumps({"rc":0,"same_bytes":True,"readonly":True,
      "bytes":len(schema.dumps(first).encode()),"sha256":hashlib.sha256(schema.dumps(first).encode()).hexdigest(),
      "negative_rcs":codes,"blender_version":bpy.app.version_string}, sort_keys=True))
'''
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "2"}
        proc = subprocess.Popen(["/usr/bin/blender", "--background", "--factory-startup", "--threads", "2",
                                 "--python-exit-code", "2", "--python-expr", script, "--", str(root)],
                                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=90)
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        self.assertEqual(proc.returncode, 0, output[-6000:])
        rows = [line.removeprefix("S3U2_RESULT=") for line in output.splitlines() if line.startswith("S3U2_RESULT=")]
        self.assertEqual(len(rows), 1, output[-6000:])
        record = json.loads(rows[0])
        self.assertEqual(record["negative_rcs"], [6, 6, 2, 3, 3])
        print("S3U2_RUNTIME=" + json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
