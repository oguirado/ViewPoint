"""Enable Cycles ground-truth passes, render via File Output EXR, decode to .npy.

Run INSIDE Blender:
  blender -b --factory-startup -noaudio <scratch.blend> --python bpy_side/passes.py -- \
      --run <run> --frames <a>:<b> --truth-seed 0 --truth-spp 1 [--res WxH] [--threads N]
      [--scene-json f] [--journal f] [--keep-crypto] [--determinism-check] [--no-uv]
      [--grid grid:WxH:sS] [--exr-codec ZIP|NONE] [--exr-dir d] [--decode inblender|external]
--decode external keeps the EXRs and writes <run>/truth/pending.jsonl for truth/decode_worker.py (FC-CBLD-1).
rc 0 ok; rc 2 + JSON on stderr for E_TRUTH_MB / E_TRUTH_ENGINE / E_TRUTH_QUANT / E_TRUTH_ARGS;
rc 5 E_DETERMINISM.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import decode_exr as dx  # noqa: E402
from decode_exr import TruthError  # noqa: E402

SCHEMA = "bvqa.truth-frame/v1"
PENDING_SCHEMA = "bvqa.truth-pending/v1"
CRYPTO = ["CryptoObject00", "CryptoObject01", "CryptoObject02"]


def parse_args(argv):
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser(prog="passes.py")
    p.add_argument("--run", required=True)
    p.add_argument("--frames", default=None, help="a:b inclusive; default scene range")
    p.add_argument("--truth-seed", type=int, default=0)
    p.add_argument("--truth-spp", type=int, default=1)
    p.add_argument("--res", default=None, help="WxH override")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--scene-json", default=None)
    p.add_argument("--journal", default=None)
    p.add_argument("--keep-crypto", action="store_true")
    p.add_argument("--no-uv", action="store_true")
    p.add_argument("--determinism-check", action="store_true")
    p.add_argument("--grid", default=None, help="visioncore grid ident; must match the render size (default: derived)")
    p.add_argument("--exr-codec", choices=("ZIP", "NONE"), default="ZIP")
    p.add_argument("--exr-dir", default=None, help="EXR scratch dir (/dev/shm ok); default <run>/truth/_exr")
    p.add_argument("--decode", choices=("inblender", "external"), default="inblender")
    try:
        return p.parse_args(argv)
    except SystemExit:
        raise TruthError("E_TRUTH_ARGS", "bad arguments %r" % (argv,))


def file_sha(path):
    return dx.sha256_file(path) if path and os.path.exists(path) else None


def check_scene(scene, args):
    if scene.render.engine != "CYCLES":
        raise TruthError("E_TRUTH_ENGINE", "truth needs CYCLES, scene has %s" % scene.render.engine)
    if scene.render.use_motion_blur:
        raise TruthError("E_TRUTH_MB", "motion blur is on: Vector pass invalid")


def configure(scene, args):
    import bpy
    cy = scene.cycles
    cy.device = "CPU"
    cy.samples = args.truth_spp
    cy.seed = args.truth_seed
    cy.use_animated_seed = False
    cy.use_denoising = False
    cy.use_adaptive_sampling = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = max(1, min(4, args.threads))
    scene.render.use_persistent_data = False
    scene.render.use_motion_blur = False
    scene.render.resolution_percentage = 100
    if args.res:
        w, h = (int(v) for v in args.res.lower().split("x"))
        scene.render.resolution_x, scene.render.resolution_y = w, h
    scene.render.use_compositing = True
    vl = scene.view_layers[0]
    vl.use_pass_z = True
    vl.use_pass_normal = True
    vl.use_pass_vector = True
    vl.use_pass_object_index = True
    vl.use_pass_material_index = True
    vl.use_pass_uv = True
    vl.use_pass_cryptomatte_object = bool(args.keep_crypto)
    return vl


def resolve_grid(scene, args):
    """The grid ident written to frames.jsonl: never null. A given --grid must equal the one for the render size."""
    want = dx.grid_ident(scene.render.resolution_x, scene.render.resolution_y)
    if args.grid is not None and args.grid != want:
        raise TruthError("E_TRUTH_ARGS", "--grid %r does not match render size (%s)" % (args.grid, want))
    return want


def build_compositor(scene, exr_dir, keep_crypto, with_uv, codec="ZIP"):
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()
    rl = tree.nodes.new("CompositorNodeRLayers")
    comp = tree.nodes.new("CompositorNodeComposite")
    tree.links.new(rl.outputs["Image"], comp.inputs["Image"])
    fo = tree.nodes.new("CompositorNodeOutputFile")
    fo.base_path = exr_dir
    fo.format.file_format = "OPEN_EXR"
    fo.format.color_depth = "32"
    fo.format.exr_codec = codec
    fo.format.color_mode = "RGB"
    fo.file_slots.clear()
    names = [n for k, n in dx.PASSES.items() if k != "uv" or with_uv]
    if keep_crypto:
        names += CRYPTO
    for n in names:
        fo.file_slots.new(n + "_")
        tree.links.new(rl.outputs[n], fo.inputs[-1])
    return fo


def valid_indices(scene, materials=None):
    if materials is None:
        import bpy
        materials = bpy.data.materials
    obs = {o.pass_index for o in scene.objects if o.type in dx.RANK_TYPES}  # hidden objects are in scene.objects
    mas = {m.pass_index for m in materials}
    return obs, mas


def render_frames(scene, frames, exr_dir):
    import bpy
    for f in frames:
        scene.frame_set(f)
        bpy.ops.render.render(write_still=False)


def run_pass_set(scene, frames, exr_dir, valid_ob, valid_ma, with_uv):
    """Render + decode in memory (no .npy writes); returns {frame: arrays}."""
    render_frames(scene, frames, exr_dir)
    res = {}
    for f in frames:
        res[f] = dx.decode_arrays(exr_dir, f, valid_ob, valid_ma, with_uv)
        for pname in dx.PASSES.values():
            p = dx.exr_path(exr_dir, pname, f)
            if os.path.exists(p):
                os.remove(p)
    return res


def write_pending(path, records):
    """Merge by frame into pending.jsonl (chunks of <= 8 frames append), atomically."""
    merged = {}
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    d = json.loads(line)
                    merged[d["frame"]] = d
    for r in records:
        merged[r["frame"]] = r
    with open(path + ".tmp", "w") as fh:
        for f in sorted(merged):
            fh.write(json.dumps(merged[f], sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(path + ".tmp", path)


def main(argv):
    import bpy
    args = parse_args(argv)
    scene = bpy.context.scene
    check_scene(scene, args)
    if args.frames:
        a, b = (int(v) for v in args.frames.split(":"))
    else:
        a, b = scene.frame_start, scene.frame_end
    if a > b or b - a + 1 > 8:
        raise TruthError("E_TRUTH_ARGS", "frame range %d:%d invalid or > 8 frames" % (a, b))
    frames = list(range(a, b + 1))
    with_uv = not args.no_uv
    configure(scene, args)
    grid = resolve_grid(scene, args)
    truth_dir = os.path.join(args.run, "truth")
    exr_dir = os.path.abspath(args.exr_dir) if args.exr_dir else os.path.join(truth_dir, "_exr")
    os.makedirs(truth_dir, exist_ok=True)
    os.makedirs(exr_dir, exist_ok=True)
    build_compositor(scene, exr_dir, args.keep_crypto, with_uv, args.exr_codec)
    valid_ob, valid_ma = valid_indices(scene)

    if args.determinism_check:
        r1 = run_pass_set(scene, frames, exr_dir, valid_ob, valid_ma, with_uv)
        r2 = run_pass_set(scene, frames, exr_dir, valid_ob, valid_ma, with_uv)
        same = all(dx.arrays_equal(r1[f], r2[f]) for f in frames)
        shas = {str(f): {k: hashlib.sha256(v.tobytes()).hexdigest() for k, v in r1[f].items()} for f in frames}
        with open(os.path.join(truth_dir, "determinism.json"), "w") as fh:
            json.dump({"identical": same, "frames": frames, "sha256": shas}, fh, sort_keys=True, separators=(",", ":"))
        if not same:
            raise TruthError("E_DETERMINISM", "two renders differ", rc=5)

    render_frames(scene, frames, exr_dir)
    if args.keep_crypto:
        keep = os.path.join(truth_dir, "crypto")
        os.makedirs(keep, exist_ok=True)
        for f in frames:
            for n in CRYPTO:
                src = dx.exr_path(exr_dir, n, f)
                if os.path.exists(src):
                    shutil.move(src, os.path.join(keep, os.path.basename(src)))
    scene_sha = file_sha(args.scene_json)
    journal_sha = file_sha(args.journal)
    external = args.decode == "external"
    new_lines, pending = {}, []
    for f in frames:
        shas = {} if external else dx.decode_frame(exr_dir, f, truth_dir, valid_ob, valid_ma, with_uv)
        new_lines[f] = {
            "schema": SCHEMA, "render_index": f,
            "width": scene.render.resolution_x, "height": scene.render.resolution_y,
            "grid": grid, "blender_version": bpy.app.version_string, "engine": "CYCLES",
            "cycles_device": scene.cycles.device, "threads": scene.render.threads, "exr_codec": args.exr_codec,
            "filter_width": float(scene.cycles.filter_width), "truth_spp": args.truth_spp, "truth_seed": args.truth_seed, "motion_blur": False,
            "files": shas, "scene_sha256": scene_sha, "journal_sha256": journal_sha,
        }
        if external:
            pending.append({"schema": PENDING_SCHEMA, "frame": f, "exr_codec": args.exr_codec, "with_uv": with_uv,
                            "passes": {k: dx.exr_path(exr_dir, n, f) for k, n in dx.PASSES.items() if k != "uv" or with_uv},
                            "valid_ob": sorted(valid_ob), "valid_ma": sorted(valid_ma), "frame_line": new_lines[f]})
    if external:
        write_pending(os.path.join(truth_dir, "pending.jsonl"), pending)
        print("TRUTH_OK frames=%s pending=%d" % (frames, len(pending)))
        return
    if args.exr_dir:  # a shared scratch dir (e.g. /dev/shm): decode_frame removed our files, drop it only if empty
        try:
            os.rmdir(exr_dir)
        except OSError:
            pass
    else:
        shutil.rmtree(exr_dir, ignore_errors=True)
    fpath = os.path.join(truth_dir, "frames.jsonl")
    merged = {}
    if os.path.exists(fpath):
        with open(fpath) as fh:
            for line in fh:
                if line.strip():
                    d = json.loads(line)
                    merged[d["render_index"]] = d
    merged.update(new_lines)
    with open(fpath + ".tmp", "w") as fh:
        for ri in sorted(merged):
            fh.write(json.dumps(merged[ri], sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(fpath + ".tmp", fpath)
    print("TRUTH_OK frames=%s" % frames)


def entry():
    try:
        main(sys.argv)
    except TruthError as e:
        sys.stderr.write(json.dumps({"error": e.name, "rc": e.rc, "msg": e.msg}) + "\n")
        sys.stderr.flush()
        sys.exit(e.rc)


if __name__ == "__main__":
    entry()
