"""Matched CPU Cycles visual ablation, isolated from fast structural completion.

compare_visuals(source, candidate, *, roles, timeout_s) returns af.glb-capture/v1.
Only exact decoded RGB equality passes: this is an identity/partition oracle,
not a calibrated perceptual similarity threshold or an appearance score.
Captures and Blender scratch remain in RAM (/dev/shm). No bpy import on host.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


def run_owned(argv, *, timeout_s, cwd, stdout_path=None):
    """Memory-capped process group; inspect exit without reaping before group kill."""
    from tools.blender.helpers_contracts import GlbError, integer
    import threading
    integer(timeout_s, 1)
    command = ['systemd-run', '--user', '--scope', '-q', '-p', 'MemoryHigh=6G',
               '-p', 'MemoryMax=8G', '-p', 'MemorySwapMax=0', '--', *argv]
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(cwd), 'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '1'}
    for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
        if key in os.environ: env[key] = os.environ[key]
    output = open(stdout_path, 'wb') if stdout_path else subprocess.DEVNULL
    proc = None
    tail = bytearray()
    def drain(stream):
        while True:
            block = stream.read(4096)
            if not block: break
            tail.extend(block)
            del tail[:-8192]
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.PIPE, start_new_session=True)
        reader = threading.Thread(target=drain, args=(proc.stderr,), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_s
        while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if time.monotonic() >= deadline: raise GlbError('deadline_exceeded', 6)
            time.sleep(0.01)
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                raise GlbError('release_unproven', 3) from exc
            reader.join(timeout=1)
            proc.stderr.close()
        if stdout_path: output.close()
    if proc.returncode != 0:
        error = GlbError('renderer_failed', 6 if proc.returncode == -signal.SIGKILL else 2)
        error.diagnostics = tail.decode('utf-8', errors='replace')
        raise error
    return 0


def _capture_record(rc, *reasons, source_sha256=None, candidate_sha256=None, captures=None,
                    receipt_sha256=None, renderer=None, diagnostics=None):
    from tools.blender.helpers_contracts import record, VERDICT, digest, read_bytes, validate_record, sha, GlbError
    from tools.blender.glb_schemas import CAPTURE_SCHEMA
    def known_hash(value):
        if value is None: return None
        try: return sha(value)
        except GlbError: return None
    source_sha256, candidate_sha256 = known_hash(source_sha256), known_hash(candidate_sha256)
    value = record(rc, *reasons, schema='af.glb-capture/v1', verdict=VERDICT[rc],
                   source_sha256=source_sha256, candidate_sha256=candidate_sha256,
                   subject_sha256=candidate_sha256, receipt_sha256=receipt_sha256,
                   tool_sha256=digest(read_bytes(Path(__file__))), renderer=renderer, captures=captures,
                   diagnostics=diagnostics)
    validate_record(value, CAPTURE_SCHEMA)
    return value


def evaluate_captures(captures, *, source_sha256, candidate_sha256):
    from tools.blender.helpers_contracts import GlbError, canonical, closed, integer, sha, validate_record
    from tools.blender.glb_schemas import CAPTURE_FRAMES
    def result(rc, *reasons):
        return _capture_record(rc, *reasons, source_sha256=source_sha256,
                               candidate_sha256=candidate_sha256, captures=captures if rc != 2 else None)
    try:
        sha(source_sha256); sha(candidate_sha256)
        if captures is None: return result(3, 'capture_missing')
        # Check coverage before the schema's minItems so omissions remain rc 3.
        if not isinstance(captures, dict): raise GlbError('capture_invalid')
        views = captures.get('views')
        if not isinstance(views, list) or len(views) != CAPTURE_PROFILE['views']:
            return _capture_record(3, 'coverage_missing', source_sha256=source_sha256,
                                   candidate_sha256=candidate_sha256)
        validate_record(captures, CAPTURE_FRAMES)
        canonical(captures)
        if captures['source_sha256'] != source_sha256 or captures['candidate_sha256'] != candidate_sha256:
            return result(4, 'capture_stale')
        if captures['profile'] != CAPTURE_PROFILE: raise GlbError('capture_profile')
        for key in ('resolution', 'samples', 'seed', 'views'): integer(captures['profile'][key])
        for index, view in enumerate(views):
            if type(view['view']) is not int or view['view'] != index or integer(view['pixel_count'], 1) != 64*64:
                raise GlbError('capture_coverage')
            for key in ('raw_visual_rgb_sha256', 'candidate_rgb_sha256', 'diagnostic_rgb_sha256', 'background_rgb_sha256'):
                sha(view[key])
        if any(v['raw_visual_rgb_sha256'] != v['candidate_rgb_sha256'] for v in views):
            return result(5, 'visual_regression')
        if any(v['raw_visual_rgb_sha256'] == v['background_rgb_sha256'] for v in views):
            return result(3, 'asset_not_observed')
        if captures['helpers_declared'] and not any(v['diagnostic_rgb_sha256'] != v['raw_visual_rgb_sha256'] for v in views):
            return result(3, 'helpers_not_observed')
        return result(0)
    except (GlbError, ValueError, TypeError, KeyError, ArithmeticError):
        return _capture_record(2, 'capture_invalid', source_sha256=source_sha256, candidate_sha256=candidate_sha256)


CAPTURE_PROFILE = {'engine': 'CYCLES', 'device': 'CPU', 'resolution': 64, 'samples': 8,
                   'seed': 0, 'views': 6, 'comparison': 'exact-decoded-rgb',
                   'color': 'Standard'}  # DESIGN_CHOICE deterministic identity oracle.


def compare_visuals(source, candidate, *, roles, timeout_s, expected_source_sha256=None,
                    expected_candidate_sha256=None, expected_receipt_sha256=None, receipt_path=None):
    from tools.blender.glb_io import decode_glb, encode_glb, role_table, verify_partition, verify_bundle
    from tools.blender.helpers_contracts import (GlbError, digest, integer, read_bytes, canonical,
                                                core, parse_json, sha)
    import shutil
    source_hash = candidate_hash = bundle_hash = renderer = diagnostics = None
    def result(rc, *reasons):
        return _capture_record(rc, *reasons, source_sha256=source_hash, candidate_sha256=candidate_hash,
                               receipt_sha256=bundle_hash, renderer=renderer, diagnostics=diagnostics)
    try:
        integer(timeout_s, 1)
        deadline = time.monotonic() + timeout_s
        source_data, candidate_data = read_bytes(source), read_bytes(candidate)
        source_hash, candidate_hash = digest(source_data), digest(candidate_data)
        if ((expected_source_sha256 is not None and sha(expected_source_sha256) != source_hash) or
            (expected_candidate_sha256 is not None and sha(expected_candidate_sha256) != candidate_hash)):
            return result(4, 'capture_input_stale')
        doc, binary = decode_glb(source_data)
        decode_glb(candidate_data)
        table = role_table(doc, roles)
        partition = verify_partition(doc, binary, candidate_data, table, visual=True)
        if partition['rc']: return result(5, *partition['reason_codes'])
        bundle = Path(candidate).parent/'receipt.json'
        if bundle.exists():
            checked = verify_bundle(bundle.parent)
            if checked['rc']: return result(checked['rc'], *checked['reason_codes'])
            bundle_hash = digest(read_bytes(bundle))
        if expected_receipt_sha256 is not None and sha(expected_receipt_sha256) != bundle_hash:
            return result(4, 'capture_receipt_stale')
        if receipt_path is not None and Path(receipt_path).parent.resolve() != Path(candidate).parent.resolve():
            raise GlbError('capture_output_outside_bundle')
        if receipt_path is not None and Path(receipt_path).resolve() in {Path(source).resolve(), Path(candidate).resolve(), bundle.resolve()}:
            raise GlbError('capture_output_conflict')
        for index, node in enumerate(doc['nodes']):
            extras = node.setdefault('extras', {})
            if not isinstance(extras, dict) or '_af_node_index' in extras: raise GlbError('capture_marker_conflict')
            extras['_af_node_index'] = index
        executable = shutil.which('blender', path='/usr/bin:/bin')
        if executable is None: return result(3, 'capture_dependency_missing')
        renderer_hash = core.sha256_file(Path(executable))
        with tempfile.TemporaryDirectory(prefix='af-glb-capture-', dir='/dev/shm') as temp:
            root = Path(temp)
            version_path = root/'renderer-version.txt'
            remaining = lambda: max(1, int(deadline - time.monotonic()))
            run_owned([executable, '--version'], timeout_s=remaining(), cwd=root, stdout_path=version_path)
            renderer = {'path': executable, 'sha256': renderer_hash,
                        'version': read_bytes(version_path).decode('utf-8').splitlines()[0]}
            (root/'raw.glb').write_bytes(encode_glb(doc, binary))
            (root/'candidate.glb').write_bytes(candidate_data)
            plan = {'roles': {str(k): v for k, v in table.items()}, 'source_sha256': source_hash,
                    'candidate_sha256': candidate_hash, 'profile': CAPTURE_PROFILE}
            (root/'plan.json').write_bytes(canonical(plan))
            run_owned([executable, '-b', '--factory-startup', '-noaudio', '--threads', '2',
                       '--python-exit-code', '2', '--python', str(Path(__file__).resolve()), '--', str(root)],
                      timeout_s=remaining(), cwd=root)
            result_path = root/'capture.json'
            if not result_path.is_file(): return result(3, 'capture_missing')
            captures = parse_json(read_bytes(result_path))
            outcome = evaluate_captures(captures, source_sha256=source_hash, candidate_sha256=candidate_hash)
            if (read_bytes(source) != source_data or read_bytes(candidate) != candidate_data or
                core.sha256_file(Path(executable)) != renderer_hash or
                (bundle_hash is not None and digest(read_bytes(bundle)) != bundle_hash)):
                return result(4, 'capture_input_stale')
            if time.monotonic() >= deadline: return result(6, 'deadline_exceeded')
            outcome.update(receipt_sha256=bundle_hash, renderer=renderer)
            if receipt_path is not None and outcome['rc'] == 0:
                forbidden = {'visual.glb', 'helpers.glb', 'receipt.json', 'separation.json'}
                if Path(receipt_path).name in forbidden or Path(receipt_path).is_symlink():
                    raise GlbError('capture_output_conflict')
                core.atomic_write_json(Path(receipt_path), outcome)
            return outcome
    except GlbError as exc:
        diagnostics = getattr(exc, 'diagnostics', None)
        return result(exc.rc, exc.code)
    except FileNotFoundError:
        return result(3, 'capture_dependency_missing')
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
        return result(2, 'capture_io_or_schema')


def _blender_main(root):
    # This branch alone runs inside Blender. All captures stay in RAM scratch.
    import bpy
    from mathutils import Vector
    plan = json.loads((root / 'plan.json').read_text())
    profile = plan['profile']

    def load(name, hide_helpers):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(root / name), import_pack_images=True)
        scene = bpy.context.scene
        all_meshes = [o for o in scene.objects if o.type == 'MESH']
        if not all_meshes:
            raise RuntimeError('no imported mesh')
        if hide_helpers:
            for ob in all_meshes:
                if '_af_node_index' not in ob:
                    raise RuntimeError('node identity absent from importer')
                ob.hide_render = plan['roles'][str(ob['_af_node_index'])] != 'visual'
        meshes = [o for o in all_meshes if not o.hide_render]
        if not meshes:
            raise RuntimeError('no visible mesh')
        return scene, meshes

    scene, meshes = load('raw.glb', True)
    corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    low = Vector(tuple(min(p[i] for p in corners) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in corners) for i in range(3)))
    center, extent = (low + high) / 2, (high-low).length
    if extent <= 0:
        raise RuntimeError('zero scale')
    directions = [(1, -2, 1), (-2, -1, 1), (1, 1, 2), (1, -2, -1), (-2, -1, -1), (1, 1, -2)]
    hashes = {}
    for label, filename, hide in [('raw_visual', 'raw.glb', True),
                                   ('candidate', 'candidate.glb', False),
                                   ('diagnostic', 'raw.glb', False),
                                   ('background', 'raw.glb', False)]:
        for view, direction in enumerate(directions):
            scene, _ = load(filename, hide)
            if label == 'background':
                for ob in scene.objects:
                    if ob.type == 'MESH': ob.hide_render = True
            scene.render.engine = 'CYCLES'
            scene.cycles.device = 'CPU'
            scene.cycles.samples = profile['samples']
            scene.cycles.seed = profile['seed']
            scene.cycles.use_denoising = False
            scene.render.threads_mode = 'FIXED'
            scene.render.threads = 2
            scene.render.resolution_x = scene.render.resolution_y = profile['resolution']
            scene.render.resolution_percentage = 100
            scene.view_settings.view_transform = 'Standard'
            scene.view_settings.look = 'Medium High Contrast'
            scene.world = bpy.data.worlds.new('capture-world')
            scene.world.use_nodes = True
            scene.world.node_tree.nodes['Background'].inputs[0].default_value = (0.2, 0.2, 0.2, 1)
            camera_data = bpy.data.cameras.new('capture-camera')
            camera = bpy.data.objects.new('capture-camera', camera_data)
            scene.collection.objects.link(camera)
            camera.location = center + Vector(direction).normalized() * extent * 2
            camera.rotation_euler = (center-camera.location).to_track_quat('-Z', 'Y').to_euler()
            camera_data.type = 'ORTHO'
            camera_data.ortho_scale = extent * 1.3
            camera_data.clip_start = max(extent * 0.001, 1e-6)
            camera_data.clip_end = extent * 10
            scene.camera = camera
            light_data = bpy.data.lights.new('capture-light', 'AREA')
            light = bpy.data.objects.new('capture-light', light_data)
            scene.collection.objects.link(light)
            light.location = center + Vector((1, -2, 3)).normalized() * extent * 2
            light.rotation_euler = (center-light.location).to_track_quat('-Z', 'Y').to_euler()
            light_data.energy = 500 * extent * extent
            light_data.size = extent * 2
            frame = root / 'frame.png'
            scene.render.image_settings.file_format = 'PNG'
            scene.render.image_settings.color_mode = 'RGB'
            scene.render.image_settings.color_depth = '8'
            scene.render.filepath = str(frame)
            bpy.ops.render.render(write_still=True)
            image = bpy.data.images.load(str(frame), check_existing=False)
            image.colorspace_settings.name = 'Non-Color'
            # Decode PNG using Blender, hash explicitly quantized RGB8 pixels.
            pixels = list(image.pixels)
            rgb = bytes(max(0, min(255, round(v*255))) for i, v in enumerate(pixels) if i % 4 != 3)
            if len(rgb) != profile['resolution']**2 * 3:
                raise RuntimeError('capture size mismatch')
            hashes[(label, view)] = hashlib.sha256(rgb).hexdigest()
            bpy.data.images.remove(image)
            frame.unlink()
    captures = {'schema': 'af.glb-capture-frames/v1',
                'helpers_declared': any(r != 'visual' for r in plan['roles'].values()), 'source_sha256': plan['source_sha256'],
                'candidate_sha256': plan['candidate_sha256'], 'profile': profile,
                'views': [{'view': i, 'raw_visual_rgb_sha256': hashes['raw_visual', i],
                           'candidate_rgb_sha256': hashes['candidate', i],
                           'diagnostic_rgb_sha256': hashes['diagnostic', i],
                           'background_rgb_sha256': hashes['background', i],
                           'pixel_count': profile['resolution']**2} for i in range(profile['views'])]}
    (root / 'capture.json').write_text(json.dumps(captures, sort_keys=True, separators=(',', ':')) + '\n')


if __name__ == '__main__':
    import sys
    _blender_main(Path(sys.argv[sys.argv.index('--') + 1]))
