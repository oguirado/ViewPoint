"""Stage-2 mutant hook (BSPEC_BORACLES section 4): apply one planted fault to the loaded scratch blend.

blender -b --factory-startup -noaudio --python-exit-code 9 <base.blend> --python bpy_side/apply_mutant.py -- \
  --spec spec.json --out mutant.blend --receipt receipt.json [--journal J]
spec = {"id": "bm.x.nn", "fault": {"operation", "selector", "parameters"}}.
Equal witnesses -> rc 5 E_GATE mutant_noop and <out> is not written. Unknown operation/selector -> rc 2 E_SCHEMA.
"""
import hashlib
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_common import DriveError, Parser, canonical, load_json, run, write
import mutate_ops


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def entry():
    import bpy
    p = Parser()
    for k in ("--spec", "--out", "--receipt"):
        p.add_argument(k, required=True)
    p.add_argument("--journal", default="")
    args = p.parse_args(sys.argv[sys.argv.index("--") + 1:])
    spec = load_json(args.spec)
    for k in ("id", "fault"):
        if k not in spec:
            raise DriveError("E_SCHEMA", "spec:" + k)
    fault = spec["fault"]
    for k in ("operation", "selector", "parameters"):
        if k not in fault:
            raise DriveError("E_SCHEMA", "fault:" + k)
    op = mutate_ops.OPS.get(fault["operation"])
    if op is None:
        raise DriveError("E_SCHEMA", "operation:" + str(fault["operation"]))
    scene = bpy.context.scene
    if args.journal:
        scene = bpy.data.scenes.get(load_json(args.journal)["scene"]) or scene
    base_sha = file_sha(bpy.data.filepath)
    before = mutate_ops.witness(scene)
    op(scene, fault["selector"], fault["parameters"])
    after = mutate_ops.witness(scene)
    if before == after:
        raise DriveError("E_GATE", "mutant_noop", rc=5)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmp = args.out + ".tmp.blend"
    bpy.ops.wm.save_as_mainfile(filepath=tmp, copy=True, compress=False)
    os.replace(tmp, args.out)
    write(args.receipt, {
        "schema": "bvqa.fault-receipt/v1", "mutant_id": spec["id"],
        "fault_spec_sha256": hashlib.sha256(canonical(fault).rstrip(b"\n")).hexdigest(),
        "base_blend_sha256": base_sha, "mutant_blend_sha256": file_sha(args.out),
        "blender_version": bpy.app.version_string, "witness_before": before, "witness_after": after})
    return {"mutant_id": spec["id"]}


if __name__ == "__main__":
    run(entry)
