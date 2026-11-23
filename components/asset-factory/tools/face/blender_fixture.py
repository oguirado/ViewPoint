"""CPU scene construction and readback for the bounded FACE head family."""
from fractions import Fraction
from pathlib import Path
import struct

from tools.blender.bake_support import upstream

from asset_factory import domains
from tools.input.evidence import bytes_digest, require
from tools.mesh.certificate import sub, cross
from tools.blender.uv_quality import raster_size


def make_scene(template, vertices):
    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_animated_seed = False
    scene.render.resolution_x = scene.render.resolution_y = 96
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.view_settings.view_transform = "Raw"
    scene.render.dither_intensity = 0
    scene.world = bpy.data.worlds.new("Black")
    scene.world.color = (0, 0, 0)

    faces = template["faces"]
    require(len(template["parts"]) == len(vertices) and len(template["materials"]) == len(faces),
            2, "fixture_mesh_coverage")
    require(0 < len(faces) <= 65536, 6, "fixture_face_budget")
    for tri in faces:
        require(len(tri) == 3 and all(type(i) is int and 0 <= i < len(vertices) for i in tri),
                2, "invalid_face_triangle")
        a, b, c = (vertices[i] for i in tri)
        require(any(cross(sub(b,a), sub(c,a))), 5, "degenerate_face_triangle")
    mesh = bpy.data.meshes.new("FixtureHead")
    mesh.from_pydata([[v / domains.UM_PER_METER for v in p] for p in vertices], [], faces)
    mesh.update()
    rig = bpy.data.objects.new("FixtureHead", mesh)
    scene.collection.objects.link(rig)
    # Constant emission gives a deterministic linear-light diagnostic raster.
    for name, rgb in (("skin", (.55,.29,.18)), ("lip", (.35,.025,.035)),
                      ("iris", (.025,.045,.065)), ("nose", (.65,.32,.19))):
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        tree = material.node_tree
        emit = tree.nodes.new("ShaderNodeEmission")
        emit.inputs["Color"].default_value = (*rgb, 1)
        tree.links.new(emit.outputs[0], tree.nodes.get("Material Output").inputs["Surface"])
        mesh.materials.append(material)
    for poly, index in zip(mesh.polygons, template["materials"], strict=True):
        require(type(index) is int and 0 <= index < len(mesh.materials), 2, "invalid_face_material")
        poly.material_index = index
    return scene, rig


def install_camera(scene, name, record):
    import bpy
    from mathutils import Matrix, Vector
    rows, _, (fx, fy, cx, cy) = domains._camera(record)
    rotation = Matrix([[float(v) for v in row] for row in rows])
    w, h = scene.render.resolution_x, scene.render.resolution_y
    require(fx == fy and cx == Fraction(w, 2) and cy == Fraction(h, 2),
            2, "unsupported_fixture_intrinsics")
    cam = bpy.data.cameras.new(name)
    cam.type = "PERSP"
    cam.sensor_fit = "HORIZONTAL"
    cam.lens = float(fx * Fraction(36,w))
    cam.sensor_width = 36
    cam.clip_start = .001
    obj = bpy.data.objects.new(name, cam)
    scene.collection.objects.link(obj)
    centre = domains.camera_center(record)
    obj.matrix_world = (rotation.transposed() @ Matrix.Diagonal(Vector((1,-1,-1)))).to_4x4()
    obj.location = [float(x/domains.UM_PER_METER) for x in centre]
    bpy.context.view_layer.update()
    return obj


def render(scene, cam, filename):
    import bpy
    require(scene.render.engine == "CYCLES" and scene.cycles.device == "CPU", 5, "cpu_render_required")
    scene.camera = cam
    scene.render.filepath = str(Path(filename).resolve())
    bpy.ops.render.render(write_still=True)


def decoded_rgb(raw, pin, size):
    require(raw is not None, 3, "render_missing")
    require(bytes_digest(raw) == pin, 4, "render_digest_drift")
    raster_size(size)
    require(len(raw) >= 33 and len(raw) <= 1_048_576 and raw[:8] == b"\x89PNG\r\n\x1a\n"
            and list(struct.unpack(">II",raw[16:24])) == size,
            2, "fixture_render_format")
    width, height, channels, data = upstream("orm_pack").decode_png(raw)
    require(channels == 3 and [width,height] == size, 2, "fixture_linear_rgb8_required")
    return data
