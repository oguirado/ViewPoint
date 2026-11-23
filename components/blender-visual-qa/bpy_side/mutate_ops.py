"""Planted-fault operations for the bm.* mutants (BSPEC_BORACLES section 4), applied to a scratch blend in memory.

OPS[operation](scene, selector, params) edits bpy data in place and returns None; apply_mutant.py saves a copy.
selector: {"object": name} | {"tag": tag} | {} (scene-wide). A selector that matches nothing is E_SCHEMA.
witness(scene) is the canonical q16 projection used by the fault receipt (equal before/after = mutant_noop).
"""
import hashlib
import json
import math
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_common import DriveError


def q16(x):
    return int(math.floor(float(x) * 65536 + 0.5))


def tags_of(ob):
    return set(t for t in str(ob.get("bvqa_tags", "")).split("|") if t)


def select(scene, selector, want_type=None):
    import bpy
    if "object" in selector:
        ob = bpy.data.objects.get(selector["object"])
        found = [ob] if ob is not None else []
    elif "tag" in selector:
        found = sorted((o for o in scene.objects if selector["tag"] in tags_of(o)), key=lambda o: o.name)
    else:
        found = []
    if want_type is not None:
        found = [o for o in found if o.type == want_type]
    if not found:
        raise DriveError("E_SCHEMA", "selector:" + json.dumps(selector, sort_keys=True))
    return found


def param(params, key, default=None):
    if key in params:
        return params[key]
    if default is None:
        raise DriveError("E_SCHEMA", "parameter:" + key)
    return default


def picks(index, permille):
    """Deterministic Bresenham-style share: permille 500 picks even indices, 100 picks index % 10 == 0."""
    return (index * permille) % 1000 < permille


def edit_bmesh(ob, fn):
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    try:
        fn(bm)
        bm.to_mesh(ob.data)
    finally:
        bm.free()
    ob.data.update()


def op_flip_normals(scene, selector, params):
    import bmesh
    pm = param(params, "permille", 1000)
    for ob in select(scene, selector, "MESH"):
        def fn(bm):
            faces = [f for f in bm.faces if picks(f.index, pm)]
            bmesh.ops.reverse_faces(bm, faces=faces)
        edit_bmesh(ob, fn)


def op_flip_shell(scene, selector, params):
    import bmesh
    if param(params, "shell", "smallest") != "smallest":
        raise DriveError("E_SCHEMA", "parameter:shell")
    for ob in select(scene, selector, "MESH"):
        def fn(bm):
            seen, shells = set(), []
            for f in bm.faces:
                if f.index in seen:
                    continue
                stack, group = [f], []
                seen.add(f.index)
                while stack:
                    cur = stack.pop()
                    group.append(cur)
                    for e in cur.edges:
                        for nb in e.link_faces:
                            if nb.index not in seen:
                                seen.add(nb.index)
                                stack.append(nb)
                shells.append(group)
            if len(shells) < 2:
                raise DriveError("E_SCHEMA", "single_shell")
            small = min(shells, key=lambda g: (len(g), min(x.index for x in g)))
            bmesh.ops.reverse_faces(bm, faces=small)
        edit_bmesh(ob, fn)


def op_delete_faces(scene, selector, params):
    import bmesh
    count = param(params, "count", 1)
    cam = scene.camera
    for ob in select(scene, selector, "MESH"):
        def fn(bm):
            cp = cam.matrix_world.translation
            mw = ob.matrix_world
            def score(f):
                c = mw @ f.calc_center_median()
                n = (mw.to_3x3() @ f.normal).normalized()
                return n.dot((cp - c).normalized())
            order = sorted(bm.faces, key=lambda f: (-q16(score(f)), f.index))
            bmesh.ops.delete(bm, geom=order[:count], context="FACES_ONLY")
        edit_bmesh(ob, fn)


def op_unlink_material(scene, selector, params):
    for ob in select(scene, selector):
        ob.material_slots[0].material = None


def tree_image_nodes(tree, seen=None):
    seen = set() if seen is None else seen
    if tree is None or tree.name in seen:
        return
    seen.add(tree.name)
    for n in tree.nodes:
        if n.bl_idname in ("ShaderNodeTexImage", "ShaderNodeTexEnvironment"):
            yield n
        if n.bl_idname == "ShaderNodeGroup":
            yield from tree_image_nodes(n.node_tree, seen)


def op_missing_texture(scene, selector, params):
    hit = 0
    for ob in select(scene, selector):
        for slot in ob.material_slots:
            if slot.material is None or not slot.material.use_nodes:
                continue
            for node in tree_image_nodes(slot.material.node_tree):
                img = node.image
                if img is None:
                    continue
                if img.packed_file:
                    img.unpack(method="REMOVE")
                img.source = "FILE"
                img.filepath = "//__missing__/x.png"
                hit += 1
    if hit == 0:
        raise DriveError("E_SCHEMA", "no_image_node")


def op_uv_scale(scene, selector, params):
    factor, pm = param(params, "factor", 8), param(params, "permille", 500)
    for ob in select(scene, selector, "MESH"):
        me = ob.data
        if not me.uv_layers:
            raise DriveError("E_SCHEMA", "no_uv")
        data = me.uv_layers[0].data
        for p in me.polygons:
            if not picks(p.index, pm):
                continue
            idx = range(p.loop_start, p.loop_start + p.loop_total)
            cu = sum(data[i].uv[0] for i in idx) / p.loop_total
            cv = sum(data[i].uv[1] for i in idx) / p.loop_total
            for i in idx:
                u, v = data[i].uv
                data[i].uv = (cu + (u - cu) * factor, cv + (v - cv) * factor)
        me.update()


def op_uv_delete(scene, selector, params):
    for ob in select(scene, selector, "MESH"):
        me = ob.data
        while len(me.uv_layers):
            me.uv_layers.remove(me.uv_layers[0])


def op_scale_object(scene, selector, params):
    factor = param(params, "permille", 1000) / 1000
    axis = param(params, "axis", "xyz")
    for ob in select(scene, selector):
        s = list(ob.scale)
        for k, name in enumerate("xyz"):
            if name in axis:
                s[k] *= factor
        ob.scale = s


def op_add_subdiv(scene, selector, params):
    levels = param(params, "levels", 6)
    for ob in select(scene, selector, "MESH"):
        m = ob.modifiers.new("bvqa_subsurf", "SUBSURF")
        m.levels = levels
        m.render_levels = levels


def op_camera_offset(scene, selector, params):
    d = math.radians(param(params, "yaw_mdeg", 90000) / 1000)
    for ob in select(scene, selector, "CAMERA"):
        e = ob.rotation_euler.copy()
        e.z += d
        ob.rotation_euler = e
        ad = ob.animation_data
        if ad and ad.action:
            for fc in ad.action.fcurves:
                if fc.data_path == "rotation_euler" and fc.array_index == 2:
                    for kp in fc.keyframe_points:
                        kp.co.y += d
                        kp.handle_left.y += d
                        kp.handle_right.y += d


def op_camera_clip(scene, selector, params):
    for ob in select(scene, selector, "CAMERA"):
        ob.data.clip_end = param(params, "clip_end_mm", 100) / 1000


def tree_nodes(tree, seen=None):
    seen = set() if seen is None else seen
    if tree is None or tree.name in seen:
        return
    seen.add(tree.name)
    for n in tree.nodes:
        yield n
        if n.bl_idname == "ShaderNodeGroup":
            yield from tree_nodes(n.node_tree, seen)


def op_kill_lights(scene, selector, params):
    import bpy
    for ob in bpy.data.objects:
        if ob.type == "LIGHT":
            ob.data.energy = 0
    for w in bpy.data.worlds:
        w.color = (0, 0, 0)
        if w.use_nodes:
            for n in tree_nodes(w.node_tree):
                if n.bl_idname == "ShaderNodeBackground":
                    n.inputs["Strength"].default_value = 0
    for m in bpy.data.materials:
        if m.use_nodes:
            for n in tree_nodes(m.node_tree):
                if n.bl_idname == "ShaderNodeEmission":
                    n.inputs["Strength"].default_value = 0


def op_dup_coplanar(scene, selector, params):
    import bpy
    import bmesh
    off = param(params, "offset_um", 1) / 1000000
    for ob in select(scene, selector, "MESH"):
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        bm.faces.ensure_lookup_table()
        f = bm.faces[0]
        sc = sum(abs(s) for s in ob.matrix_world.to_scale()) / 3
        shift = f.normal * (off / sc)
        out = bmesh.new()
        vs = [out.verts.new(v.co + shift) for v in f.verts]
        out.faces.new(vs)
        me = bpy.data.meshes.new(ob.name + "_zf")
        out.to_mesh(me)
        out.free()
        bm.free()
        dup = bpy.data.objects.new(ob.name + "_zf", me)
        for c in ob.users_collection:
            c.objects.link(dup)
        dup.matrix_world = ob.matrix_world
        dup["bvqa_tags"] = "open"
        mat = bpy.data.materials.new(ob.name + "_zf_mat")
        mat.diffuse_color = (0.1, 0.9, 0.1, 1)
        me.materials.append(mat)


def op_freeze_anim(scene, selector, params):
    hit = 0
    for ob in select(scene, selector):
        if ob.animation_data and ob.animation_data.action:
            ob.animation_data.action = None
            hit += 1
    if hit == 0:
        raise DriveError("E_SCHEMA", "not_animated")


def set_interp(action, mode, prefix=""):
    for fc in action.fcurves:
        if fc.data_path.startswith(prefix):
            for kp in fc.keyframe_points:
                kp.interpolation = mode


def op_drift(scene, selector, params):
    step = param(params, "um_per_frame", 500) / 1000000
    a, b = scene.frame_start, scene.frame_end
    for ob in select(scene, selector):
        x0 = ob.location.x
        ob.location.x = x0
        ob.keyframe_insert("location", index=0, frame=a)
        ob.location.x = x0 + step * (b - a)
        ob.keyframe_insert("location", index=0, frame=b)
        ob.location.x = x0
        set_interp(ob.animation_data.action, "LINEAR", "location")


def op_flicker(scene, selector, params):
    period = param(params, "period", 2)
    if period < 2:
        raise DriveError("E_SCHEMA", "parameter:period")
    a, b = scene.frame_start, scene.frame_end
    for ob in select(scene, selector, "MESH"):
        mat = next((s.material for s in ob.material_slots if s.material and s.material.use_nodes), None)
        node = mat.node_tree.nodes.get("Principled BSDF") if mat else None
        if node is None:
            raise DriveError("E_SCHEMA", "no_principled")
        node.inputs["Emission Color"].default_value = (1, 1, 1, 1)
        sock = node.inputs["Emission Strength"]
        for f in range(a, b + 1):
            sock.default_value = 10 if (f - a) % period == 0 else 0
            sock.keyframe_insert("default_value", frame=f)
        set_interp(mat.node_tree.animation_data.action, "CONSTANT")


def op_step_hold(scene, selector, params):
    """Journal case (O-B6): emission A->B->B->C CONSTANT keys; a step then hold then step is NOT a flicker."""
    levels = [param(params, k, d) for k, d in (("a", 0), ("b", 5), ("c", 10))]
    seq = [levels[0], levels[1], levels[1], levels[2]]
    a = scene.frame_start
    for ob in select(scene, selector, "MESH"):
        mat = next((s.material for s in ob.material_slots if s.material and s.material.use_nodes), None)
        node = mat.node_tree.nodes.get("Principled BSDF") if mat else None
        if node is None:
            raise DriveError("E_SCHEMA", "no_principled")
        node.inputs["Emission Color"].default_value = (1, 1, 1, 1)
        sock = node.inputs["Emission Strength"]
        for i, v in enumerate(seq):
            sock.default_value = v
            sock.keyframe_insert("default_value", frame=a + i)
        set_interp(mat.node_tree.animation_data.action, "CONSTANT")


def op_hide_render(scene, selector, params):
    for ob in select(scene, selector):
        ob.hide_render = True


def op_bowtie(scene, selector, params):
    from mathutils import Vector
    for ob in select(scene, selector, "MESH"):
        def fn(bm):
            v0 = bm.verts[0]
            n = v0.normal if v0.normal.length else Vector((0, 0, 1))
            t = n.cross(Vector((0, 0, 1)))
            if t.length == 0:
                t = n.cross(Vector((1, 0, 0)))
            t.normalize()
            p1 = bm.verts.new(v0.co + n * 0.2)
            p2 = bm.verts.new(v0.co + n * 0.2 + t * 0.2)
            bm.faces.new((v0, p1, p2))
        edit_bmesh(ob, fn)


def op_collapse_face(scene, selector, params):
    count = param(params, "count", 1)
    for ob in select(scene, selector, "MESH"):
        def fn(bm):
            for f in bm.faces[:count]:
                f.verts[0].co = f.verts[1].co.copy()
        edit_bmesh(ob, fn)


OPS = {"flip_normals": op_flip_normals, "flip_shell": op_flip_shell, "delete_faces": op_delete_faces,
       "unlink_material": op_unlink_material, "missing_texture": op_missing_texture, "uv_scale": op_uv_scale,
       "uv_delete": op_uv_delete, "scale_object": op_scale_object, "add_subdiv": op_add_subdiv,
       "camera_offset": op_camera_offset, "camera_clip": op_camera_clip, "kill_lights": op_kill_lights,
       "dup_coplanar": op_dup_coplanar, "freeze_anim": op_freeze_anim, "drift": op_drift, "flicker": op_flicker, "step_hold": op_step_hold,
       "hide_render": op_hide_render, "bowtie": op_bowtie, "collapse_face": op_collapse_face}


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def anim_witness(ad):
    if not ad or not ad.action:
        return ""
    return sha([[fc.data_path, fc.array_index, [[q16(k.co.x), q16(k.co.y), k.interpolation] for k in fc.keyframe_points]]
                for fc in sorted(ad.action.fcurves, key=lambda c: (c.data_path, c.array_index))])


# @anchor:bpy.mutate_ops.witness
def witness(scene):
    """Canonical q16 projection of the datablocks the ops can touch (whole-scene superset, deterministic)."""
    import bpy
    w = {"objects": {}, "meshes": {}, "materials": {}, "cameras": {}, "lights": {}, "worlds": {}, "images": {}}
    for ob in bpy.data.objects:
        w["objects"][ob.name] = {
            "hide": int(ob.hide_render), "tags": str(ob.get("bvqa_tags", "")),
            "loc": [q16(v) for v in ob.location], "rot": [q16(v) for v in ob.rotation_euler], "scale": [q16(v) for v in ob.scale],
            "mods": [[m.type, int(getattr(m, "levels", 0)), int(getattr(m, "render_levels", 0))] for m in ob.modifiers],
            "slots": [s.material.name if s.material else "" for s in ob.material_slots], "anim": anim_witness(ob.animation_data)}
    for me in bpy.data.meshes:
        uv = [[q16(c) for l in layer.data for c in l.uv] for layer in me.uv_layers]
        w["meshes"][me.name] = sha([[q16(c) for v in me.vertices for c in v.co], [list(p.vertices) for p in me.polygons],
                                    [p.material_index for p in me.polygons], [la.name for la in me.uv_layers], uv])
    for m in bpy.data.materials:
        d = {"diffuse": [q16(c) for c in m.diffuse_color], "nodes": 0, "anim": ""}
        if m.use_nodes and m.node_tree:
            vals = []
            for n in tree_nodes(m.node_tree):
                for s in n.inputs:
                    dv = getattr(s, "default_value", None)
                    if isinstance(dv, float):
                        vals.append([n.name, s.identifier, q16(dv)])
                    elif dv is not None and hasattr(dv, "__len__") and not isinstance(dv, str):
                        vals.append([n.name, s.identifier, [q16(c) for c in dv]])
            d["nodes"] = sha(vals)
            d["anim"] = anim_witness(m.node_tree.animation_data)
        w["materials"][m.name] = d
    for c in bpy.data.cameras:
        w["cameras"][c.name] = [q16(c.clip_start), q16(c.clip_end), q16(c.lens)]
    for li in bpy.data.lights:
        w["lights"][li.name] = [li.type, q16(li.energy)]
    for wd in bpy.data.worlds:
        vals = [q16(c) for c in wd.color]
        if wd.use_nodes and wd.node_tree:
            vals += [q16(n.inputs["Strength"].default_value) for n in tree_nodes(wd.node_tree) if n.bl_idname == "ShaderNodeBackground"]
        w["worlds"][wd.name] = vals
    for im in bpy.data.images:
        w["images"][im.name] = [im.source, im.filepath, int(bool(im.packed_file))]
    return w
