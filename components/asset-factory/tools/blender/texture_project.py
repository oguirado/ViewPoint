"""Single-reference projection into an immutable, measured base-colour bundle.

Host code never imports bpy. Run as ``python -B -m tools.blender.texture_project``.
This is a texture tool, not asset admission: mirrored surfaces, de-lighting,
normal/ORM, geometry admission and independent appearance remain CANNOT_SEE.
The rights file is a caller declaration, bound to both input byte digests; it
does not infer legal rights from a filename. No weights or network calls.
"""
from asset_factory import proc as af_proc
import argparse
import functools
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

from asset_factory import core
from providers.birefnet import resources
from .bake_support import Refusal, load_record, read_bytes, safe_glb, sha, upstream

SCHEMA = "af.texture-project/v1"
WORKER = Path(__file__).with_name("texture_project_worker.py")
SUPERVISOR = Path(__file__).with_name("texture_project_process.py")
COMPONENT = Path(__file__).resolve().parents[2]
MEMORY_SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
                "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0",
                "taskset", "-c", "0-15", "nice", "-n", "15"]
FILES = ("textured.glb", "base_color.png", "provenance.png", "worker.json",
         "front_projected.png", "front_baked.png")
MAX_OUTPUT_BYTES = 128 * 2 ** 20  # DESIGN_CHOICE: split UV vertices expand raw GLBs.


def output_bytes(path):
    """Bounded read of one owned staging leaf; no symlink or special-file reads."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Refusal(2, "output_regular_file_required")
        raw = stream.read(MAX_OUTPUT_BYTES + 1)
    if len(raw) > MAX_OUTPUT_BYTES:
        raise Refusal(6, "output_byte_budget_exceeded")
    return raw


def tool_snapshot():
    paths = [Path(__file__), WORKER, SUPERVISOR, Path(resources.__file__), Path(af_proc.__file__),
             Path(af_proc.__file__).with_name("proc_pidns.py"),
             COMPONENT / "providers/trellis2/build/process_guard.py",
             Path(__file__).with_name("texture_project_rights.schema.json"),
             Path(__file__).with_name("texture_project.schema.json"),
             Path(__file__).with_name("bake_support.py"), Path(core.__file__),
             COMPONENT / "config" / "territory-groups.v1.json"]
    paths += [Path(upstream(name).__file__) for name in ("glb_core", "bake_pbr", "orm_pack")]
    return [{"name": p.name, "sha256": sha(read_bytes(p))} for p in paths]


def record(rc, *reasons, **fields):
    return dict(schema=SCHEMA, rc=rc, status=core.STATUS[rc], reason_codes=list(reasons),
                asset_verdict="CANNOT_SEE", **fields)


def check_rights(rights, mesh_sha256, reference_sha256):
    schema = load_record(read_bytes(Path(__file__).with_name("texture_project_rights.schema.json")))
    core.validate(rights, schema)
    if len(rights["inputs"]) != 2 or {r["role"] for r in rights["inputs"]} != {"mesh", "reference"}:
        raise Refusal(2, "rights_roles_invalid")
    for item in rights["inputs"]:
        expected = mesh_sha256 if item["role"] == "mesh" else reference_sha256
        if item["sha256"] != expected:
            raise Refusal(4, "rights_subject_drift")
        if not item["provider_id"].strip() or not item["provenance"].strip():
            raise Refusal(3, "rights_evidence_missing")



def geometry_identity(raw):
    """Compare world triangle multisets at the sibling parser's micrometre grid.

    Attribute splits and primitive/material regrouping may change vertex indices.
    No position, triangle multiplicity or winding change is admitted on that grid.
    """
    g = upstream("glb_core")
    scene = g.parse_glb(raw)
    tris = []
    for prim in scene.prims:
        if prim.skinned:
            raise Refusal(3, "skinned_mesh_unsupported")
        pts = g.world_positions_um(prim)
        for i in range(0, len(prim.indices), 3):
            tri = tuple(tuple(pts[j]) for j in prim.indices[i:i + 3])
            tris.append(min(tri, tri[1:] + tri[:1], tri[2:] + tri[:2]))
    if not tris:
        raise Refusal(3, "empty_mesh")
    return sha(json.dumps(sorted(tris), separators=(",", ":")).encode()), len(tris)


def _image(raw):
    # Bound dimensions *before* invoking the sibling PNG decoder/decompressor.
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise Refusal(2, "png_required")
    import struct
    w, h = struct.unpack(">II", raw[16:24])
    if not (0 < w <= 4096 and 0 < h <= 4096):
        raise Refusal(6, "reference_pixel_budget")
    decoder = upstream("orm_pack")
    try:
        return decoder.decode_png(raw)
    except decoder.OrmError as exc:
        raise Refusal(2, "png_invalid") from exc


def measure_bundle(stage, source, resolution, max_front_mae_ppm):
    """Re-read outputs. A worker rc or its own metrics alone never pass this gate."""
    stage = Path(stage)
    raw = output_bytes(stage / "textured.glb")
    doc = safe_glb(raw)
    before, ntri = geometry_identity(source)
    after, output_ntri = geometry_identity(raw)
    if after != before or ntri != output_ntri:
        raise Refusal(5, "geometry_changed")
    png = read_bytes(stage / "base_color.png")
    mask = read_bytes(stage / "provenance.png")
    w, h, ch, pix = _image(png)
    mw, mh, mch, mpix = _image(mask)
    if (w, h, mw, mh) != (resolution,) * 4 or ch != 4 or mch != 4:
        raise Refusal(2, "map_dimensions_or_channels")
    g = upstream("glb_core")
    uv = upstream("bake_pbr").uv_tris(g.parse_glb(raw))
    if not uv:
        raise Refusal(3, "uv_missing")
    if any(len(set(t)) < 3 or ((t[1][0] - t[0][0]) * (t[2][1] - t[0][1]) -
            (t[1][1] - t[0][1]) * (t[2][0] - t[0][0])) == 0 for _, t in uv):
        raise Refusal(5, "uv_degenerate")
    if any(not (0 <= c <= 65536) for _, t in uv for p in t for c in p):
        raise Refusal(5, "uv_out_of_bounds")
    covered = upstream("bake_pbr").coverage(uv, resolution)
    if not covered:
        raise Refusal(3, "zero_covered_texels")
    front = back = mixed = inpainted = missing = 0
    for x, y in covered:
        i = (y * w + x) * 4
        r, green, b, a = mpix[i:i + 4]
        if pix[i + 3] != 255 or a != 255 or r + green + b < 240:
            missing += 1
        elif b >= 247 and r <= 8 and green <= 8:
            inpainted += 1
        elif b > 8:
            missing += 1
        elif r >= 247 and green <= 8:
            front += 1
        elif green >= 247 and r <= 8:
            back += 1
        else:
            mixed += 1
    if missing:
        raise Refusal(5, "unpopulated_surface_texels")
    if front == 0:
        raise Refusal(3, "front_projection_unobserved")
    # Render direct projection and reconstructed atlas from the same explicit
    # orthographic camera. This measures bake fidelity, not reference likeness.
    rw, rh, rc, expected = _image(read_bytes(stage / "front_projected.png"))
    bw, bh, bc, actual = _image(read_bytes(stage / "front_baked.png"))
    if (rw, rh, bw, bh) != (resolution,) * 4 or rc != 4 or bc != 4:
        raise Refusal(2, "front_render_invalid")
    indices = [i for i in range(0, len(expected), 4) if expected[i + 3] > 0]
    if not indices or not any(actual[i + 3] > 0 for i in range(0, len(actual), 4)):
        raise Refusal(3, "front_render_unobserved")
    # Identical geometry/camera and opaque materials must preserve alpha, even
    # at antialiased edges. Missing support cannot shrink the error denominator.
    if expected[3::4] != actual[3::4]:
        raise Refusal(5, "front_render_support_changed")
    weight = sum(expected[i + 3] for i in indices)
    mae = sum(abs(expected[i + c] - actual[i + c]) * expected[i + 3]
              for i in indices for c in range(3))
    mae_ppm = mae * 1000000 // (weight * 3 * 255)
    if mae_ppm > max_front_mae_ppm:
        raise Refusal(5, "front_reconstruction_changed")
    # Authenticate the actual exported material binding and embedded PNG bytes.
    _, binary = g._split(raw)
    checks = 0
    for mesh in doc["meshes"]:
        for prim in mesh["primitives"]:
            mat = doc["materials"][prim["material"]]
            pbr = mat["pbrMetallicRoughness"]
            binding = pbr["baseColorTexture"]
            if binding.get("texCoord", 0) != 0 or binding.get("extensions"):
                raise Refusal(5, "base_color_mapping_changed")
            if (pbr.get("baseColorFactor", [1, 1, 1, 1]) != [1, 1, 1, 1] or
                    pbr.get("metallicFactor", 1) != 0 or mat.get("alphaMode", "OPAQUE") != "OPAQUE"):
                raise Refusal(5, "base_color_material_changed")
            tex = doc["textures"][binding["index"]]
            sampler = doc["samplers"][tex["sampler"]]
            if any(sampler.get(k, 10497) != 33071 for k in ("wrapS", "wrapT")):
                raise Refusal(5, "texture_wrap_changed")
            img = doc["images"][tex["source"]]
            view = doc["bufferViews"][img["bufferView"]]
            off = view.get("byteOffset", 0)
            if img["mimeType"] != "image/png" or binary[off:off + view["byteLength"]] != png:
                raise Refusal(5, "embedded_base_color_drift")
            checks += 1
    if checks == 0:
        raise Refusal(3, "material_binding_missing")
    return dict(triangles=ntri, geometry_sha256=before, uv_triangles=len(uv),
                covered_texels=len(covered), populated_texels=len(covered) - missing,
                populated_coverage_ppm=(len(covered) - missing) * 1000000 // len(covered),
                front_projected_texels=front, mirrored_texels=back, seam_mixed_texels=mixed,
                inpainted_texels=inpainted,
                front_render_pixels=len(indices), front_reconstruction_mae_ppm=mae_ppm,
                material_bindings_checked=checks, resolution=resolution)


def _scope_prefix():
    """Reuse proven enclosing limits; nested user-bus credentials can refuse."""
    return resources.bounded_scope_prefix(MEMORY_SCOPE)


def _retire_group(proc):
    try:
        af_proc.signal_group(proc, signal.SIGKILL)
    except ProcessLookupError:
        pass


def scoped_run(argv, timeout_s, log):
    """One memory-bounded service; systemd owns the whole descendant tree."""
    with Path(log).open("wb") as stream:
        job = af_proc.popen(MEMORY_SCOPE + list(argv), stdout=stream,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, env={"PATH": os.defpath, "HOME": str(Path.home()),
            "LANG": "C.UTF-8", "CUDA_VISIBLE_DEVICES": "", "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "1"})
        try:
            return af_proc.wait_exit(job, timeout=timeout_s)
        finally:
            job.stop()

def bounded(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        timeout = kwargs.get("timeout_s")
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            return record(2, "projection_policy_invalid")
        if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
            return record(3, "deadline_unavailable")
        def expired(signum, frame):
            raise Refusal(6, "terminated" if signum == signal.SIGTERM else "deadline_exceeded")
        previous = {sig: signal.signal(sig, expired) for sig in (signal.SIGTERM, signal.SIGALRM)}
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return function(*args, **kwargs)
        except Refusal as exc:
            return record(exc.rc, exc.reason)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return run


@bounded
def project(mesh, reference, destination, *, rights, front, resolution, margin_px, timeout_s,
            max_front_mae_ppm, runner=None):
    """Publish a complete technical bundle only; source and destination never alias.

    resolution/margin are explicit caller budgets, not quality calibration.
    runner is a test seam; CLI only uses the real scoped CPU worker.
    """
    started, stage, lockfd, lock = time.monotonic(), None, None, None
    try:
        if front not in ("-Y", "+Y") or any(type(v) is not int for v in
                (resolution, margin_px, timeout_s, max_front_mae_ppm)):
            raise Refusal(2, "projection_policy_invalid")
        if not (32 <= resolution <= 1024 and 1 <= margin_px <= 32 and
                margin_px * 8 < resolution and 1 <= timeout_s <= 1800 and 0 <= max_front_mae_ppm <= 1000000):
            raise Refusal(2, "projection_policy_invalid")
        mesh, reference, dest = Path(mesh), Path(reference), Path(os.path.abspath(destination))
        if dest.exists() or dest.is_symlink():
            raise Refusal(2, "destination_exists")
        # No writes through a symlink in the caller's output ancestry.
        if any(p.is_symlink() for p in (dest.parent, *dest.parent.parents)):
            raise Refusal(2, "destination_symlink")
        raw, image = read_bytes(mesh), read_bytes(reference)
        tools_before = tool_snapshot()
        rights_raw = read_bytes(rights)
        declaration = load_record(rights_raw)
        check_rights(declaration, sha(raw), sha(image))
        safe_glb(raw)
        geometry_identity(raw)
        _image(image)
        blender = shutil.which("blender")
        if blender is None:
            raise Refusal(3, "blender_unavailable")
        blender = Path(blender).resolve()
        runtime_sha256 = core.sha256_file(blender)
        dest.parent.mkdir(parents=True, exist_ok=True)
        lock = dest.with_name("." + dest.name + ".texture-project.lock")
        try:
            lockfd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            lock = None
            raise Refusal(5, "destination_busy")
        stage = Path(tempfile.mkdtemp(prefix="." + dest.name + ".", dir=dest.parent))
        (stage / "input.glb").write_bytes(raw)
        (stage / "reference.png").write_bytes(image)
        (stage / "rights.json").write_bytes(rights_raw)
        argv = [str(blender), "-b", "--factory-startup", "-noaudio", "--threads", "4",
                "--python-exit-code", "2", "--python", str(WORKER), "--",
                "--mesh", str(stage / "input.glb"), "--reference", str(stage / "reference.png"),
                "--out", str(stage), "--front=" + front, "--resolution", str(resolution),
                "--margin-px", str(margin_px)]
        try:
            rc = (runner or scoped_run)(argv, timeout_s, stage / "blender.log")
        except subprocess.TimeoutExpired:
            raise Refusal(6, "deadline_exceeded")
        if type(rc) is not int or rc not in core.RC:
            raise Refusal(2, "worker_contract")
        if rc:
            log = stage / "blender.log"
            tail = ""
            if log.is_file():
                with log.open("rb") as f:
                    f.seek(max(0, f.seek(0, os.SEEK_END) - 3000))
                    tail = f.read(3000).decode("utf-8", "replace")
            return record(rc, "worker_refused", log_tail=tail)
        worker = load_record(read_bytes(stage / "worker.json"))
        if worker.get("rc") != 0 or type(worker.get("rc")) is not int:
            raise Refusal(3, "worker_evidence_missing")
        metrics = measure_bundle(stage, raw, resolution, max_front_mae_ppm)
        if read_bytes(mesh) != raw or read_bytes(reference) != image or read_bytes(rights) != rights_raw:
            raise Refusal(4, "input_drift")
        if core.sha256_file(blender) != runtime_sha256:
            raise Refusal(4, "runtime_drift")
        if tool_snapshot() != tools_before:
            raise Refusal(4, "toolchain_drift")
        rec = record(0, scope="projection-bake-only", inputs={"mesh_sha256": sha(raw),
                     "reference_sha256": sha(image), "rights_sha256": sha(rights_raw)},
                     policy={"front_blender_axis": front, "fit": "orthographic-xz-bounds",
                             "backfill": "horizontal-reference-mirror-plus-UV-nearest-inpaint", "margin_px": margin_px,
                             "max_front_mae_ppm": max_front_mae_ppm,
                             "threshold_status": "caller_policy_uncalibrated",
                             "resolution": resolution, "timeout_s": timeout_s},
                     metrics=metrics, worker=worker,
                     unavailable={"back_appearance": "single_reference_unobserved",
                                  "delighted_albedo": "reference_contains_unknown_lighting",
                                  "appearance_score": "independent_reviewer_absent",
                                  "per_texel_visibility": "face_centroid_estimate_only",
                                  "normal_orm": "base_color_only", "asset_admission": "upstream_gate_not_evaluated"},
                     wall_ms=int((time.monotonic() - started) * 1000),
                     runner="scoped-cpu" if runner is None else "injected-test-runner",
                     tool_files=tools_before +
                                [{"name": "runtime-blender", "sha256": runtime_sha256}],
                     files=[{"path": p, "sha256": sha(output_bytes(stage / p))} for p in
                            (*FILES, "input.glb", "reference.png", "rights.json", "blender.log")])
        core.validate(rec, load_record(read_bytes(Path(__file__).with_name("texture_project.schema.json"))))
        core.atomic_write_json(stage / "receipt.json", rec)
        # No writes to a destination on any refusal, including a late collision.
        if dest.exists() or dest.is_symlink():
            raise Refusal(2, "destination_exists")
        for path in stage.iterdir():
            with path.open("rb") as f:
                os.fsync(f.fileno())
        os.replace(stage, dest)
        stage = None
        return rec
    except af_proc.SignalRefusal as exc:
        return record(exc.rc, exc.code)
    except Refusal as exc:
        return record(exc.rc, exc.reason)
    except core.ContractError as exc:
        return record(2, exc.code)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return record(2, "projection_error", detail=str(exc))
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        if lockfd is not None:
            os.close(lockfd)
            lock.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ("mesh", "reference", "out", "rights"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--front", required=True, choices=("-Y", "+Y"), help="Blender world axis; use --front=-Y")
    p.add_argument("--resolution", required=True, type=int)
    p.add_argument("--margin-px", required=True, type=int)
    p.add_argument("--timeout-s", required=True, type=int)
    p.add_argument("--max-front-mae-ppm", required=True, type=int)
    a = p.parse_args(argv)
    return core.emit(project(a.mesh, a.reference, a.out, rights=a.rights, front=a.front,
                            resolution=a.resolution, margin_px=a.margin_px, timeout_s=a.timeout_s,
                            max_front_mae_ppm=a.max_front_mae_ppm))


if __name__ == "__main__":
    raise SystemExit(main())
