"""Private Blender process. Its recipe and mesh paths are staged by the caller."""
from collections import Counter
from hashlib import sha256
import math
import json
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector

# Blender runs this file as a script with its own Python search path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from camera import world_matrix


COLORS = {
    "body": (0.12, 0.24, 0.36, 1), "skin": (0.5, 0.28, 0.16, 1),
    "trousers": (0.06, 0.09, 0.12, 1),
}


def export(path):
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True,
                              export_yup=True, export_extras=True)


def material(role):
    result = bpy.data.materials.new(role)
    result.diffuse_color = COLORS[role]
    result.use_nodes = True
    nodes = result.node_tree.nodes
    nodes.clear()
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.inputs["Base Color"].default_value = COLORS[role]
    shader.inputs["Roughness"].default_value = 1.0
    output = nodes.new("ShaderNodeOutputMaterial")
    result.node_tree.links.new(shader.outputs[0], output.inputs["Surface"])
    return result


def author(root, parts, units):
    for name, data in parts.items():
        bpy.ops.wm.read_factory_settings(use_empty=True)
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata([tuple(n / units for n in v) for v in data["vertices"]],
                         [], data["faces"])
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        obj["instance_id"] = name
        role = "skin" if name == "head" else "trousers" if name.startswith("leg-") else "body"
        obj.data.materials.append(material(role))
        if name.startswith("arm-"):
            obj.data.materials.append(material("skin"))
            low, high = data["hand"]
            for polygon in mesh.polygons:
                coords = [data["vertices"][i] for i in polygon.vertices]
                if all(all(lo <= n <= hi for lo, n, hi in zip(low, v, high)) for v in coords):
                    polygon.material_index = 1
        obj.select_set(True)
        export(root / (name + ".glb"))


def oriented_triangle(a, b, c):
    return min((a, b, c), (b, c, a), (c, a, b))


def verify(objects, parts, units, tolerance_nm, translated=False):
    actual, expected, max_error_nano = Counter(), Counter(), 0
    for name, obj in objects.items():
        data = parts[name]
        shift = data["position"] if translated else [0, 0, 0]
        if translated and "world_vertices" in data:
            verts = data["world_vertices"]
        else:
            verts = [tuple(n + t for n, t in zip(v, shift)) for v in data["vertices"]]
        expected.update(oriented_triangle(*(tuple(verts[index]) for index in tri)) for tri in data["faces"])
        points = []
        for vertex in obj.data.vertices:
            point = tuple(n * units for n in (obj.matrix_world @ vertex.co))
            integer = tuple(round(n) for n in point)
            max_error_nano = max(max_error_nano, *(math.ceil(abs(a - b) * 1000)
                                                   for a, b in zip(point, integer)))
            points.append(integer)
        obj.data.calc_loop_triangles()
        actual.update(oriented_triangle(*(points[i] for i in tri.vertices))
                      for tri in obj.data.loop_triangles)
    if actual != expected or max_error_nano > tolerance_nm:
        reason = "part_reconstruction_mismatch" if any("rotation" in p for p in parts.values()) else "fixture_reconstruction_mismatch"
        raise ValueError(reason)
    return {"triangles": sum(actual.values()), "max_coordinate_residual_nm": max_error_nano}


def import_meshes(path):
    before = set(bpy.context.scene.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    objects = set(bpy.context.scene.objects) - before
    if any(obj.type != "MESH" for obj in objects) or not objects:
        raise ValueError("unexpected_glb_object")
    return objects


def assemble(root, request, units):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1
    parts = request["parts"]
    tolerance = request["max_import_residual_nm"]
    objects, reconstruction = {}, {}
    for name, data in parts.items():
        imported = import_meshes(root / (name + ".glb"))
        if len(imported) != 1:
            raise ValueError("unexpected_part_object_count")
        obj, = imported
        objects[name] = obj
        reconstruction[name] = verify({name: obj}, parts, units, tolerance)
        obj["instance_id"] = name
        position = Vector(tuple(n / units for n in data["position"]))
        if "rotation" in data:
            matrix = Matrix([[n / d for n, d in row] for row in data["rotation"]]).to_4x4()
            matrix.translation = position
            obj.matrix_world = matrix
        else:
            obj.location = position
        bpy.context.view_layer.update()

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects.values():
        obj.select_set(True)
    export(root / "scene.glb")

    reconstructed = import_meshes(root / "scene.glb")
    by_id = {obj.get("instance_id"): obj for obj in reconstructed}
    if len(by_id) != len(reconstructed) or by_id.keys() != parts.keys():
        raise ValueError("assembled_instance_ids")
    master = verify(by_id, parts, units, tolerance, translated=True)
    for obj in reconstructed:
        bpy.data.objects.remove(obj, do_unlink=True)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = request["render"]["samples"]
    scene.cycles.use_denoising = False  # The collector runs the pinned CPU preview filter.
    scene.render.resolution_x = scene.render.resolution_y = request["render"]["size"]
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = True
    scene.render.dither_intensity = 0
    scene.view_settings.view_transform = "Standard"
    scene.world = bpy.data.worlds.new("Even white environment")
    scene.world.use_nodes = True
    world = scene.world.node_tree.nodes.get("Background")
    world.inputs["Strength"].default_value = 1.0
    world.inputs["Color"].default_value = (0.85, 0.85, 0.85, 1)
    render = request["render"]
    center = tuple(n / units for n in render["center_um"])
    focus = bpy.data.objects.new("Camera aim", None)
    focus.location = Vector(center)
    scene.collection.objects.link(focus)
    cameras = {}
    for name, direction in render["views"]:
        camera = bpy.data.cameras.new(name)
        camera.type = "ORTHO"
        camera.ortho_scale = render["span_um"] / units
        camera.dof.focus_object = focus
        obj = bpy.data.objects.new(name, camera)
        scene.collection.objects.link(obj)
        obj.matrix_world = Matrix(world_matrix(direction, center, render["radius_um"] / units))
        cameras[name] = obj
    scene.camera = cameras["front"]
    bpy.context.view_layer.update()
    bpy.ops.wm.save_as_mainfile(filepath=str(root / "scene.blend"))
    observations = {}
    for name, camera in cameras.items():
        scene.camera = camera
        path = root / ("rgba-" + name + ".png")
        scene.render.filepath = str(path)
        bpy.context.view_layer.update()
        before = observe_camera(scene)
        bpy.ops.render.render(write_still=True)
        if before != observe_camera(scene):
            raise ValueError("camera_changed_during_render")
        observations[name] = dict(before, rgba_sha256="sha256:" + sha256(path.read_bytes()).hexdigest())
    flat = render_flat_face(root, scene, cameras["front"], objects)
    return {"reconstructed_parts": reconstruction, "reconstructed_master": master,
            "blender_version": bpy.app.version_string, "render_device": scene.cycles.device,
            "render_engine": scene.render.engine, "samples": scene.cycles.samples,
            "cameras": observations, "flat_face_sha256": flat}


def observe_camera(scene):
    camera = scene.camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
    focus = camera.data.dof.focus_object.evaluated_get(bpy.context.evaluated_depsgraph_get())
    render = scene.render
    return {"type": camera.data.type, "ortho_scale": float(camera.data.ortho_scale).hex(),
            "matrix_world": [[float(value).hex() for value in row] for row in camera.matrix_world],
            "aim": [float(value).hex() for value in focus.matrix_world.translation],
            "shift_xy": [float(value).hex() for value in (camera.data.shift_x, camera.data.shift_y)],
            "pixel_aspect_xy": [float(value).hex() for value in (render.pixel_aspect_x, render.pixel_aspect_y)]}


def render_flat_face(root, scene, front, objects):
    """Render a single isolated flat face under the same rig and light."""
    for obj in objects.values():
        obj.hide_render = True
    scene.camera = front
    rotation = front.matrix_world.to_3x3()
    center = front.data.dof.focus_object.matrix_world.translation
    size = front.data.ortho_scale / 4
    vertices = [center + rotation @ Vector((x*size, y*size, 0))
                for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    mesh = bpy.data.meshes.new("flat lighting witness")
    mesh.from_pydata(vertices, [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.materials.append(material("body"))
    witness = bpy.data.objects.new("flat lighting witness", mesh)
    scene.collection.objects.link(witness)
    path = root / "lighting-flat.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(sys.argv[sys.argv.index("--") + 1])
    request = json.loads((root / "request.json").read_bytes())
    if request["operation"] == "author":
        author(root, request["parts"], request["units"])
    else:
        result = assemble(root, request, request["units"])
        (root / "worker-result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
