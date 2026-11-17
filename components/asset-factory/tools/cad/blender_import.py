"""Native Blender STL/GLB import measurements; no renders or visual approval.

Run inside Blender with --background --factory-startup --python-exit-code 2.
No operators are used to create assets; only the supplied artifact is imported.
"""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import sys


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    import bpy
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    source = args.input.resolve(strict=True)
    before = digest(source)
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    suffix = source.suffix.lower()
    if suffix == '.stl':
        # Native operator and flags measured on local Blender 4.0.2.
        result = bpy.ops.import_mesh.stl(filepath=str(source), global_scale=0.001,
                                        use_scene_unit=False, use_facet_normal=False,
                                        axis_forward='Y', axis_up='Z')
        convention = 'STL_mm_to_Blender_m_Z_up'
    elif suffix == '.glb':
        result = bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
        convention = 'glTF_m_Y_up_to_Blender_m_Z_up'
    else:
        raise ValueError('unsupported artifact')
    if result != {'FINISHED'}:
        raise ValueError('import incomplete')
    objects = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    vertices = [obj.matrix_world @ vertex.co for obj in objects for vertex in obj.data.vertices]
    polygons = sum(len(obj.data.polygons) for obj in objects)
    if not vertices or not polygons or digest(source) != before:
        raise ValueError('empty import or changed input')
    bbox = [[round(min(v[i] for v in vertices)*10**6) for i in range(3)],
            [round(max(v[i] for v in vertices)*10**6) for i in range(3)]]
    payload = {'schema': 'af.cad-blender-import/v1', 'rc': 0, 'status': 'OK',
               'source_sha256': before, 'convention': convention,
               'blender_version': bpy.app.version_string,
               'objects': len(objects), 'vertices': len(vertices), 'polygons': polygons,
               'bbox_um': bbox,
               'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
               'appearance_verdict': 'CANNOT_SEE'}
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n'
    # Only a fresh caller-owned scratch measurement may be created.
    with args.out.open('x', encoding='utf-8') as stream:
        stream.write(raw)


if __name__ == '__main__':
    main()
