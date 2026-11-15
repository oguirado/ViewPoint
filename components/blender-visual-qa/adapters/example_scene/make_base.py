"""Build the clean base scene for the bm.* mutants INSIDE Blender (no .blend is committed, g_blend_binary).

blender -b --factory-startup -noaudio --python adapters/example_scene/make_base.py -- --out <dir>/base.blend
Writes <dir>/base.blend and <dir>/tex.png. Objects: Subject, Static, TwoShell, Textured, Cam, Sun.
"""
import os
import sys

import bpy
import bmesh


def tag(ob, text):
    ob["bvqa_tags"] = text


def material(name, rgba, image=None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes["Principled BSDF"]
    if image is None:
        bsdf.inputs["Base Color"].default_value = rgba
    else:
        # Unlit (emission-only) textured surface: a missing image renders as exact (255,0,255) under Standard (bp.magenta.attrib).
        tex = m.node_tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        bsdf.inputs["Base Color"].default_value = (0, 0, 0, 1)
        bsdf.inputs["Emission Strength"].default_value = 1.0
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
        m.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
    return m


def cube(name, loc, size=1.0):
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    ob = bpy.context.object
    ob.name = name
    return ob


def main():
    out = os.path.abspath(sys.argv[sys.argv.index("--") + 1:][1])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Scene"
    scene.frame_start, scene.frame_end = 1, 4
    scene.render.resolution_x, scene.render.resolution_y = 160, 90
    subject = cube("Subject", (0, 0, 0))
    tag(subject, "closed|subject|animated")
    subject.data.materials.append(material("SubjectMat", (0.12, 0.45, 0.8, 1)))
    # 2 cuts per edge so the +6 subsurf levels of bm.subdiv.09 exceed the per-object tri budget
    mod = subject.modifiers.new("pre", "SUBSURF")
    mod.levels = 2
    bpy.ops.object.modifier_apply(modifier="pre")
    for f, x in ((1, 0.0), (4, 0.3)):
        subject.location.x = x
        subject.keyframe_insert("location", frame=f)
    static = cube("Static", (2.5, 0, 0), 0.8)
    tag(static, "closed|static")
    static.data.materials.append(material("StaticMat", (0.8, 0.3, 0.1, 1)))
    a = cube("TwoShell", (-2.5, 0, 0), 1.0)
    b = cube("tmp", (-2.5, 0, 1.5), 0.5)
    bpy.ops.object.select_all(action="DESELECT")
    a.select_set(True); b.select_set(True)
    bpy.context.view_layer.objects.active = a
    bpy.ops.object.join()
    tag(a, "closed")
    a.data.materials.append(material("ShellMat", (0.3, 0.8, 0.3, 1)))
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4, size=1.5, location=(0, 0, -1.2))
    plane = bpy.context.object
    plane.name = "Textured"
    tag(plane, "open")
    img = bpy.data.images.new("tex", 8, 8)
    img.filepath_raw = os.path.join(os.path.dirname(out), "tex.png")
    img.file_format = "PNG"
    img.save()
    plane.data.materials.append(material("TexMat", (1, 1, 1, 1), img))
    bpy.ops.object.camera_add(location=(0, -9, 1.5), rotation=(1.45, 0, 0))
    cam = bpy.context.object
    cam.name = "Cam"
    cam.data.clip_end = 100.0  # bm.scale_1000.08 must clip all 8 corners (bo.cam.clipping FAILs only on complete clipping)
    scene.camera = cam
    bpy.ops.object.light_add(type="SUN", location=(3, -3, 6))
    sun = bpy.context.object
    sun.name = "Sun"
    sun.data.energy = 3.0
    scene.world = bpy.data.worlds.new("World")
    scene.world.use_nodes = True
    bpy.ops.wm.save_as_mainfile(filepath=out, compress=False)


if __name__ == "__main__":
    main()
