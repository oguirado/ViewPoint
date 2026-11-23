"""Real Blender rollback gate. Runs inside a bounded Blender scratch process.

blender -b --factory-startup --python tools/sculpt/txn/blender_gate.py --
    [--raw-glb INPUT] [--plant-residue] [--report OUTPUT]

The fixed gate exercises a hundred deterministic vertex perturbations, not an
unqualified sculpt operator. The negative leaves one float32 ULP on one vertex.
Reports publish only on PASS. JSON on stdout always records the gate outcome.
"""

import argparse
import json
from pathlib import Path
import resource
import struct
import sys
import time

from asset_factory import core
from tools.sculpt.txn import Transaction
from tools.sculpt.txn.bpy_target import BlenderMeshTarget


def _one_ulp(obj):
    vertex = obj.data.vertices[0]
    bits = struct.unpack("<I", struct.pack("<f", vertex.co.x))[0]
    # Positive/negative finite values step in different numeric directions, but
    # either adjacent representable bit pattern suffices for the residue oracle.
    bits = bits - 1 if (bits & 0x7fffffff) == 0x7f7fffff else bits + 1
    vertex.co.x = struct.unpack("<f", struct.pack("<I", bits))[0]
    obj.data.update()


def _run(args):
    import bpy
    started = time.monotonic_ns()
    input_hash = core.sha256_file(args.raw_glb) if args.raw_glb else None
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    if args.raw_glb:
        bpy.ops.import_scene.gltf(filepath=str(args.raw_glb.resolve()))
        objects = sorted((o for o in bpy.context.scene.objects if o.type == "MESH"),
                         key=lambda obj: obj.name)
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16)
        objects = [bpy.context.object]
    if not objects:
        return {"schema": "af.sculpt-txn-gate/v1", "rc": 3,
                "status": "INCOMPLETE", "verdict": "CANNOT_SEE",
                "reason_codes": ["mesh_missing"], "checks_evaluated": 0}
    schema = core.load_json(Path(__file__).with_name("stroke.schema.json"), max_bytes=65536)
    records, failures = [], []
    mesh_counts = []
    for object_index, obj in enumerate(objects):
        target = BlenderMeshTarget(obj)
        mesh_count_before = len(bpy.data.meshes)
        baseline = target.read()
        original_restore = target.restore
        if args.plant_residue:
            def faulty_restore(checkpoint):
                original_restore(checkpoint)
                _one_ulp(obj)
            target.restore = faulty_restore
        transaction = Transaction(target)

        def measure(current, deadline):
            state = current.read()
            return {"rc": 0, "metrics": {"vertex_count": len(state.vertices) // 12},
                    "state_sha256": state.evidence()["state_sha256"], "reason_codes": []}

        def decide(before, after):
            return {"rc": 0, "keep": True, "reason_codes": []}

        for stroke in range(100):
            def apply(current, deadline):
                index = stroke % len(current.obj.data.vertices)
                vertex = current.obj.data.vertices[index]
                axis = stroke % 3
                # Fixture-only kernel, deliberately independent of operator RNA.
                original = vertex.co[axis]
                vertex.co[axis] = original + (1 if stroke % 2 else -1) * max(abs(original), 1) / 1024
                current.obj.data.update()
            receipt = transaction.run(f"mesh-{object_index}-stroke-{stroke}", apply,
                                      measure, decide, commit=False,
                                      timeout_s=30, cleanup_timeout_s=30)
            core.validate(receipt, schema)
            records.append(receipt)
            if (receipt["rc"] != 0 or receipt["restored"] is not True
                    or target.read() != baseline):
                failures.append({"object_index": object_index, "stroke": stroke,
                                 "rc": receipt["rc"] or 5,
                                 "reason_codes": receipt["reason_codes"] or ["rollback_invariant"]})
                break
        mesh_counts.append({"object_index": object_index,
                            "vertex_count": len(baseline.vertices) // 12,
                            "mesh_datablocks_before": mesh_count_before,
                            "mesh_datablocks_after": len(bpy.data.meshes)})
        if not failures and len(bpy.data.meshes) != mesh_count_before:
            failures.append({"object_index": object_index, "rc": 5,
                             "reason_codes": ["mesh_datablock_leak"]})
        if failures:
            break
    if args.raw_glb and core.sha256_file(args.raw_glb) != input_hash:
        failures.append({"rc": 4, "reason_codes": ["source_drift"]})
    rc = core.worst_rc(f["rc"] for f in failures) if failures else 0
    return {"schema": "af.sculpt-txn-gate/v1", "rc": rc, "status": core.STATUS[rc],
            "verdict": core.VERDICT[rc], "blender_version": bpy.app.version_string,
            "input_sha256": input_hash, "planted_residue": args.plant_residue,
            "strokes_required_per_mesh": 100, "strokes_evaluated": len(records),
            "checks_evaluated": len(records), "mesh_counts": mesh_counts,
            "failures": failures, "receipts": records,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "wall_ns": time.monotonic_ns() - started}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-glb", type=Path)
    parser.add_argument("--plant-residue", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = _run(args)
        if result["rc"] == 0 and args.report:
            core.atomic_write_json(args.report, result)
        return core.emit(result)
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except Exception:
        return core.emit({"rc": 2, "status": "ERROR", "reason_codes": ["blender_gate_error"]})


if __name__ == "__main__":
    result_rc = main(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    # Blender catches SystemExit in --python; explicit process status is required.
    sys.stdout.flush()
    sys.stderr.flush()
    import os
    os._exit(result_rc)
