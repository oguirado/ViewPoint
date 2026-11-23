"""Real bpy target controls, inherited inside the test runner's resource scope."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class BlenderTargetTests(unittest.TestCase):
    def test_real_mesh_controls(self):
        root = Path(__file__).resolve().parents[1]
        child = subprocess.run(
            ["/usr/bin/blender", "-b", "--factory-startup", "--python-exit-code", "2",
             "--python", str(Path(__file__).resolve()), "--", "--bpy-controls"],
            cwd=root, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root),
                           "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            text=True, start_new_session=True)
        self.assertEqual(child.returncode, 0, child.stdout[-4000:])
        records = [json.loads(line) for line in child.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(records[-1]["rc"], 0)
        self.assertEqual(records[-1]["controls_passed"], 8)
        print(json.dumps(records[-1], sort_keys=True))


def _controls():
    import bpy
    from asset_factory import core
    from tools.sculpt.txn import Transaction
    from tools.sculpt.txn.bpy_target import BlenderMeshTarget

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.data.vertices[0].co.x = -0.0
    obj.data.uv_layers.new(name="checkpoint_uv")
    target = BlenderMeshTarget(obj)
    original = target.read()
    mesh_count = len(bpy.data.meshes)

    def measure(current, deadline):
        state = current.read()
        return {"rc": 0, "state_sha256": state.evidence()["state_sha256"],
                "metrics": {"vertices": len(state.vertices) // 12}, "reason_codes": []}

    def accept(before, after):
        return {"rc": 0, "keep": True, "reason_codes": []}

    def run(apply, *, commit=True):
        return Transaction(target).run("control", apply, measure, accept,
                                       commit=commit, timeout_s=10, cleanup_timeout_s=10)

    results = []
    def topology(current, deadline):
        current.obj.data.vertices.add(1)
        current.obj.data.update()
    receipt = run(topology)
    assert receipt["rc"] == 5 and receipt["restored"] is True
    assert target.read() == original
    results.append("topology_rollback")

    def transform(current, deadline):
        current.obj.scale.x *= 2
        bpy.context.view_layer.update()
    receipt = run(transform)
    assert receipt["rc"] == 5 and receipt["restored"] is True
    assert target.read() == original
    results.append("transform_rollback")

    def displace(current, deadline):
        current.obj.data.vertices[1].co.y += 0.125
        current.obj.data.update()
    def broken(current, deadline):
        displace(current, deadline)
        raise RuntimeError("planted")
    receipt = run(broken)
    assert receipt["rc"] == 2 and receipt["restored"] is True
    assert target.read() == original
    results.append("partial_apply_rollback")

    receipt = run(lambda t, d: None)
    assert receipt["rc"] == 5 and receipt["restored"] is True
    results.append("no_op_rejected")

    receipt = run(displace, commit=False)
    assert receipt["rc"] == 0 and receipt["restored"] is True
    assert target.read().vertices[:4] == original.vertices[:4]
    assert obj.data.uv_layers.get("checkpoint_uv") is not None
    results.append("signed_zero_and_customdata_restore")

    receipt = run(displace)
    assert receipt["rc"] == 0 and receipt["action"] == "COMMIT"
    assert target.read() != original and len(bpy.data.meshes) == mesh_count
    results.append("measured_commit_no_mesh_leak")

    shared = obj.copy()
    bpy.context.collection.objects.link(shared)
    try:
        BlenderMeshTarget(obj)
    except core.ContractError as exc:
        assert exc.code == "scratch_mesh_required"
    else:
        raise AssertionError("shared mesh admitted")
    bpy.data.objects.remove(shared, do_unlink=True)
    results.append("shared_mesh_refused")

    obj.shape_key_add(name="Basis")
    try:
        BlenderMeshTarget(obj)
    except core.ContractError as exc:
        assert exc.code == "scratch_mesh_required"
    else:
        raise AssertionError("shape keys admitted")
    results.append("shape_keys_refused")
    return {"rc": 0, "controls_passed": len(results), "controls": results,
            "blender_version": bpy.app.version_string}


if __name__ == "__main__":
    if "--bpy-controls" in sys.argv:
        try:
            record = _controls()
        except BaseException as exc:
            record = {"rc": 5, "reason": type(exc).__name__, "detail": str(exc)}
        print(json.dumps(record, sort_keys=True), flush=True)
        os._exit(record["rc"])
    unittest.main()
