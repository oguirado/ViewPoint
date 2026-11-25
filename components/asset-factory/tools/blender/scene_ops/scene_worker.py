# SPDX-License-Identifier: AGPL-3.0-only
"""Blender-side reader and transaction worker. The host owns publication."""

import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Quaternion

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scene_contract import CONSTRAINTS, Refusal, canonical, require  # noqa: E402


def reference(block):
    return block.name if block is not None else None


def transform(location, rotation, scale):
    return {"location": list(location), "rotation_wxyz": list(rotation), "scale": list(scale)}


def constraint_state(item):
    state = {"name": item.name, "type": item.type, "influence": item.influence,
             "mute": item.mute, "owner_space": item.owner_space,
             "target_space": item.target_space}
    for key in ("target", "pole_target", "space_object"):
        if hasattr(item, key):
            state[key] = reference(getattr(item, key))
    for key in ("subtarget", "pole_subtarget", "space_subtarget",
                "use_x", "use_y", "use_z", "invert_x", "invert_y", "invert_z",
                "use_offset", "mix_mode"):
        if hasattr(item, key):
            state[key] = getattr(item, key)
    if hasattr(item, "targets"):
        state["targets"] = [{"target": reference(target.target), "subtarget": target.subtarget,
                             "weight": target.weight} for target in item.targets]
    return state


def authored_transform(obj):
    rotation = obj.rotation_euler.to_quaternion()
    if obj.rotation_mode == "QUATERNION":
        rotation = obj.rotation_quaternion
    elif obj.rotation_mode == "AXIS_ANGLE":
        angle, *axis = obj.rotation_axis_angle
        rotation = Quaternion(axis, angle)
    return transform(obj.location, rotation.normalized(), obj.scale)


def data_state(obj):
    data = obj.data
    if obj.type == "MESH":
        return {"vertices": len(data.vertices), "edges": len(data.edges),
                "polygons": len(data.polygons)}
    if obj.type == "ARMATURE":
        return {"bones": {bone.name: reference(bone.parent) for bone in data.bones}}
    if obj.type == "CAMERA":
        return {"type": data.type, "lens": data.lens, "ortho_scale": data.ortho_scale,
                "clip_start": data.clip_start, "clip_end": data.clip_end}
    if obj.type == "LIGHT":
        return {"type": data.type, "energy": data.energy, "color": list(data.color)}
    return {}


def object_state(obj, evaluated):
    animation = obj.animation_data
    action = animation.action if animation else None
    state = {
        "type": obj.type, "data": reference(obj.data), "data_summary": data_state(obj),
        "factory_id": obj.get("af_id") if isinstance(obj.get("af_id"), str) else None,
        "parent": reference(obj.parent), "parent_type": obj.parent_type,
        "parent_bone": obj.parent_bone,
        "parent_inverse": [list(row) for row in obj.matrix_parent_inverse],
        "collections": sorted(c.name for c in obj.users_collection),
        "local_transform": authored_transform(obj),
        "constraints": [constraint_state(item) for item in obj.constraints],
        "materials": [{"index": index, "link": slot.link, "material": reference(slot.material)}
                      for index, slot in enumerate(obj.material_slots)],
        "animation": {"action": reference(action), "nla_tracks": len(animation.nla_tracks)
                      if animation else 0, "fcurves": len(action.fcurves)
                      if action is not None and hasattr(action, "fcurves") else None},
        "physics": {"rigid_body": obj.rigid_body.type if obj.rigid_body else None},
    }
    if evaluated is not None:
        state["world_transform"] = transform(*evaluated.matrix_world.decompose())
    return state


def inspect(revision):
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    objects = {}
    for obj in bpy.data.objects:
        evaluated = obj.evaluated_get(depsgraph) if obj.name in bpy.context.view_layer.objects else None
        objects[obj.name] = object_state(obj, evaluated)
    return {
        "schema": "vp3d.scene_snapshot/v1", "revision_sha256": revision,
        "active_scene": scene.name,
        "scenes": {item.name: {"objects": sorted(item.objects.keys()),
                               "camera": reference(item.camera), "frame": item.frame_current,
                               "frame_range": [item.frame_start, item.frame_end]}
                   for item in bpy.data.scenes},
        "materials": {item.name: {"diffuse_color": list(item.diffuse_color),
                                  "use_nodes": item.use_nodes} for item in bpy.data.materials},
        "objects": objects,
    }


def scene_object(scene, key):
    item = scene.objects.get(key)
    require(item is not None, "OBJECT_MISSING", key)
    require(item.library is None and item.override_library is None, "OBJECT_READ_ONLY", key)
    return item


def independent_dependency(obj, dependency):
    pending = [dependency]
    seen = set()
    while pending:
        item = pending.pop()
        require(item != obj, "DEPENDENCY_CYCLE", obj.name)
        if item not in seen:
            seen.add(item)
            if item.parent is not None:
                pending.append(item.parent)
            for constraint in item.constraints:
                for key in ("target", "pole_target", "space_object"):
                    target = getattr(constraint, key, None)
                    if isinstance(target, bpy.types.Object):
                        pending.append(target)
                pending.extend(slot.target for slot in getattr(constraint, "targets", ())
                               if slot.target is not None)


def apply_edit(scene, edit):
    operation = edit["op"]
    obj = scene_object(scene, edit["object"])

    if operation == "object.rename":
        new_name = edit["name"]
        collision = bpy.data.objects.get(new_name)
        require(collision is None or collision == obj, "RENAME_COLLISION", new_name)
        obj.name = new_name
        require(obj.name == new_name, "RENAME_COLLISION", new_name)
    elif operation == "object.set_location":
        obj.location = edit["location"]
    elif operation == "parent.set":
        require(obj.parent_type == "OBJECT", "PARENT_TYPE_NOT_ALLOWED", obj.name)
        parent = scene_object(scene, edit["parent"]) if edit["parent"] is not None else None
        if parent is not None:
            independent_dependency(obj, parent)
        obj.parent = parent
        obj.matrix_parent_inverse = Matrix.Identity(4)
    elif operation == "material.assign":
        index = edit["slot"]
        require(index < len(obj.material_slots), "SLOT_MISSING", str(index))
        material = bpy.data.materials.get(edit["material"])
        require(material is not None, "MATERIAL_MISSING", edit["material"])
        slot = obj.material_slots[index]
        slot.link = "OBJECT"
        slot.material = material
    elif operation == "constraint.add":
        name = edit["name"]
        require(obj.constraints.get(name) is None, "CONSTRAINT_NAME_COLLISION", name)
        target = scene_object(scene, edit["target"])
        independent_dependency(obj, target)
        constraint = obj.constraints.new(edit["type"])
        constraint.name = name
        require(constraint.name == name, "CONSTRAINT_NAME_COLLISION", name)
        constraint.target = target
        constraint.influence = edit["influence"]
    elif operation == "constraint.remove":
        name = edit["name"]
        constraint = obj.constraints.get(name)
        require(constraint is not None, "CONSTRAINT_MISSING", name)
        require(constraint.type in CONSTRAINTS, "CONSTRAINT_NOT_ALLOWED", constraint.type)
        obj.constraints.remove(constraint)


def edit(command):
    scene = bpy.data.scenes.get(command["scene"])
    require(scene is not None, "SCENE_MISSING", command["scene"])
    applied = 0
    for change in command["edits"]:
        try:
            apply_edit(scene, change)
        except Refusal as exc:
            exc.evidence.update(edit_index=applied, operations_applied_to_private_copy=applied)
            raise
        applied += 1
    bpy.context.preferences.filepaths.save_version = 0
    status = bpy.ops.wm.save_as_mainfile(filepath=command["output"], relative_remap=False)
    if status != {"FINISHED"}:
        raise RuntimeError(f"Unexpected Blender save status: {status}")
    return {"operations_applied": applied, "scene": scene.name}


def main():
    request, response = map(Path, sys.argv[sys.argv.index("--") + 1:])
    command = json.loads(request.read_text(encoding="utf-8"))
    try:
        require(not bpy.data.libraries, "LINKED_LIBRARIES_NOT_SUPPORTED")
        if command["operation"] == "af.scene.inspect":
            result = inspect(command["revision_sha256"])
        else:
            # A copy into a new directory cannot carry Blend-relative dependencies.
            relative_assets = sorted(path for path in bpy.utils.blend_paths(packed=False)
                                     if path.startswith("//"))
            require(not relative_assets, "RELATIVE_ASSETS_NOT_SUPPORTED", paths=relative_assets)
            result = edit(command)
        reply = {"ok": True, "result": result}
    except Refusal as exc:
        reply = {"ok": False, "code": exc.code, "detail": exc.detail, "evidence": exc.evidence}
    response.write_bytes(canonical(reply))


if __name__ == "__main__":
    main()
