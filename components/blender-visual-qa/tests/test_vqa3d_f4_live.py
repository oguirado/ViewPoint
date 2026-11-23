"""Explicit real-Blender exercise of the footprint floor; unittest discovery starts no processes.

Run: python3 -B tests/test_vqa3d_f4_live.py   (prints one JSON line per case, rc 0 when all hold)
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

COMPONENT = Path(__file__).resolve().parents[1]
INSIDE = r'''
import bpy, json, sys
sys.path.insert(0, COMPONENT)
from vqa3d.adapters import blender_side as side
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

def slab(name, x0, x1, y0, y1, z, parent=None):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)], [], [(0, 1, 2, 3)])
    obj = bpy.data.objects.new(name, mesh); scene.collection.objects.link(obj)
    return obj

def rig(name):
    data = bpy.data.armatures.new(name); obj = bpy.data.objects.new(name, data); scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj; bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new("foot.L"); bone.head = (0.05, 0.0, 0.08); bone.tail = (0.05, 0.2, 0.01)
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj

slab("SubBase", -2, 2, -2, 2, -0.005)
slab("Paver_A", -2, 2, -2, 0.116, 0.0)          # 8 mm joint at y = 0.116 .. 0.124, under the foot centre
slab("Paver_B", -2, 2, 0.124, 2, 0.0)
slab("Lobby", 3, 5, 3, 5, 0.3)
rig_a, rig_b = rig("RigA"), rig("RigB")
bpy.ops.mesh.primitive_cube_add(size=1); shoe = bpy.context.object; shoe.name = "Shoe"
shoe.scale = (0.1, 0.24, 0.06); shoe.location = (0.05, 0.12, 0.032)
bpy.ops.object.transform_apply(location=True, scale=True)
shoe.parent, shoe.parent_type, shoe.parent_bone = rig_a, "BONE", "foot.L"
bpy.context.view_layer.update()
from mathutils import Matrix
bone = rig_a.pose.bones["foot.L"]                 # keep the applied world-space vertices where they are
shoe.matrix_parent_inverse = (rig_a.matrix_world @ bone.matrix @ Matrix.Translation((0, bone.length, 0))).inverted()
coat = slab("B_Coat", -0.2, 0.3, -0.1, 0.4, 0.03)   # another character's cloth lying under the foot
coat.modifiers.new("Armature", "ARMATURE").object = rig_b
foot = {"label": "Shoe", "z": ["/objects/Shoe/world_bbox_min_um/2"], "bones": [["RigA", "foot.L"]]}
cases = {}
def contact(tag, declaration=None):
    bpy.context.view_layer.update()
    state = side.scene_state(bpy, None, [foot], declaration)
    cases[tag] = {"floor": state["contacts"]["Shoe"], "sole_z_um": state["objects"]["Shoe"]["world_bbox_min_um"][2]}
contact("pavers_with_joint_and_other_character")
contact("declared_plane", {"z_um": -2000})
contact("declared_object_subbase", {"object": "SubBase"})
rig_a.location = (4, 4, 0.3); contact("raised_lobby")
rig_a.location = (20, 20, 0); contact("off_every_surface")
print("VQA3D_F4_CONTACT=" + json.dumps(cases, sort_keys=True))
'''
EXPECTED = {"pavers_with_joint_and_other_character": ("Paver_A", 0, "ray_cast"), "declared_plane": ("declared_z", -2000, "declared"),
            "declared_object_subbase": ("SubBase", -5000, "ray_cast"), "raised_lobby": ("Lobby", 300000, "ray_cast"),
            "off_every_surface": ("", None, "unresolved")}


def run():
    with tempfile.TemporaryDirectory(prefix="vqa3d-f4-contact-") as tmp:
        script = Path(tmp) / "inside.py"
        script.write_text("COMPONENT = %r\n" % str(COMPONENT) + INSIDE)
        done = subprocess.run(["nice", "-n", "10", "/usr/bin/blender", "-b", "--factory-startup", "-noaudio", "--python-exit-code", "9",
                               "--python", str(script)], capture_output=True, text=True, timeout=120,
                              env={"PATH": "/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8"})
    rows = [line.split("=", 1)[1] for line in done.stdout.splitlines() if line.startswith("VQA3D_F4_CONTACT=")]
    if done.returncode != 0 or len(rows) != 1:
        print(json.dumps({"status": "ERROR", "rc": done.returncode, "tail": (done.stdout + done.stderr)[-1500:]}))
        return 2
    cases, ok = json.loads(rows[0]), True
    for tag, (name, z, method) in EXPECTED.items():
        floor = cases[tag]["floor"]
        held = (floor.get("object"), floor.get("z_um"), floor.get("method")) == (name, z, method)
        held = held and cases[tag]["sole_z_um"] == (302000 if tag == "raised_lobby" else 2000)
        ok = ok and held
        print(json.dumps({"case": tag, "status": "PASS" if held else "FAIL", "expected": [name, z, method], **cases[tag]}, sort_keys=True), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
