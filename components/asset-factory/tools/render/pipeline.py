"""External Cycles/OIDN render stage, with immutable directory publication.

python -m tools.render.pipeline --scene S --output NEW_DIR --oidn BINARY
    --blender BINARY --frames START END --references F F --device CPU
All external processes have owned groups and deadlines. No binary is vendored.
"""
from asset_factory import proc as af_proc
import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time

from asset_factory import core

HERE = Path(__file__).resolve().parent
VERSION = "2.5.1"


class Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason
        super().__init__(reason)


def reap_group(child):
    try:
        child.stop()
    except af_proc.SignalRefusal as exc:
        raise Refusal(3, "process_release_unproven") from exc

def run(argv, log, timeout_s, *, env=None):
    start = time.monotonic_ns()
    with Path(log).open("wb") as stream:
        child = af_proc.popen([str(a) for a in argv], stdout=stream, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True)
        try:
            rc = af_proc.wait_exit(child, timeout=timeout_s)
        finally:
            reap_group(child)
    if rc != 0:
        raise Refusal(2, "external_process_failed:" + str(rc))
    return (time.monotonic_ns() - start) // 1000000


def oidn_environment(binary):
    # Explicit allowlist: no operator tokens or ambient Python overrides.
    lib = Path(binary).resolve().parent.parent / "lib"
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LD_LIBRARY_PATH": str(lib),
            "OMP_NUM_THREADS": "16", "OPENBLAS_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""}


def probe_oidn(binary, scratch, *, expected_version=VERSION):
    path = Path(binary)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Refusal(3, "oidn_absent")
    scratch = Path(scratch)
    probe = scratch / "probe.pfm"
    probe.write_bytes(b"PF\n16 16\n-1.0\n" + struct.pack("<768f", *([0.5] * 768)))
    log = scratch / "oidn_probe.log"
    try:
        run([path, "-d", "cpu", "--hdr", probe, "-o", scratch / "probe_out.pfm", "-v", "2"],
            log, 30, env=oidn_environment(path))
    except (OSError, Refusal):
        raise Refusal(3, "oidn_probe_failed") from None
    text = log.read_text(errors="replace")
    versions = re.findall(r"Open Image Denoise (\d+\.\d+\.\d+)", text)
    if versions != [expected_version] or "Device    : CPU" not in text:
        raise Refusal(3, "oidn_version_or_device_mismatch")
    if not (scratch / "probe_out.pfm").is_file():
        raise Refusal(3, "oidn_probe_output_absent")
    return {"name": "OIDN", "version": expected_version, "device": "CPU"}


def descriptor(path, role, relative_to=None):
    path = Path(path)
    return {"role": role, "path": str(path.relative_to(relative_to) if relative_to else path.resolve()),
            "sha256": core.sha256_file(path)}


def render(scene, output, *, oidn, blender, start, end, references=(), samples=96,
           reference_samples=1024, resolution_percentage=50, device="CPU", seed=0,
           timeout_s=3600):
    from tools.render import identity
    for value in (start, end, samples, reference_samples, resolution_percentage, seed, timeout_s):
        if type(value) is not int:
            raise Refusal(2, "integer_required")
    if not (0 <= start <= end and end - start < 1000 and samples > 0 and
            reference_samples > samples and 1 <= resolution_percentage <= 100 and
            0 <= seed < 2**31 and 1 <= timeout_s <= 86400 and device in ("CPU", "CUDA")):
        raise Refusal(2, "invalid_render_parameters")
    if any(type(f) is not int or not start <= f <= end for f in references) or len(set(references)) != len(references):
        raise Refusal(2, "invalid_references")
    if device != "CPU":
        raise Refusal(3, "cuda_admission_unavailable")
    source, target = Path(scene).resolve(), Path(output).absolute()
    if not source.is_file() or source.suffix.lower() != ".blend":
        raise Refusal(2, "source_invalid")
    if not Path(blender).is_file() or not os.access(blender, os.X_OK):
        raise Refusal(3, "blender_absent")
    if target.exists() or target.is_symlink():
        raise Refusal(5, "output_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / ("." + target.name + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Refusal(5, "output_held") from None
    os.close(fd)
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix="." + target.name + ".", dir=target.parent))
        denoiser = probe_oidn(oidn, stage)
        inputs = [descriptor(source, "blend_scene"), descriptor(oidn, "oidn_binary"),
                  descriptor(blender, "blender_binary"), descriptor(HERE / "blender_passes.py", "render_script"),
                  descriptor(Path(__file__), "pipeline_script"), descriptor(HERE / "frame.schema.json", "receipt_schema")]
        parameters = {"start": start, "end": end, "samples": samples, "references": list(references),
                      "reference_samples": reference_samples, "resolution_percentage": resolution_percentage,
                      "seed": seed, "device": device}
        request_hash = identity.request_sha256(parameters, inputs)
        tasks = [{"frame": f, "samples": samples, "dir": str(Path("low") / f"{f:06d}")}
                 for f in range(start, end + 1)]
        tasks += [{"frame": f, "samples": reference_samples, "dir": str(Path("reference") / f"{f:06d}")}
                  for f in references]
        job = {"mode": "render", "device": device, "seed": seed,
               "resolution_percentage": resolution_percentage, "request_sha256": request_hash, "tasks": tasks}
        job_path = stage / "render_job.json"
        core.atomic_write_json(job_path, job)
        job_hash = core.sha256_file(job_path)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "OMP_NUM_THREADS": "16",
               "OPENBLAS_NUM_THREADS": "1", "TMPDIR": str(stage), "CUDA_VISIBLE_DEVICES": ""}
        prefix = [blender, "-b", source, "--disable-autoexec", "--python-exit-code", "2",
                  "--python", HERE / "blender_passes.py", "--"]
        run(prefix + [job_path], stage / "render.log", timeout_s, env=env)
        tone_tasks = []
        for task in tasks:
            directory = stage / task["dir"]
            task["oidn_ms"] = run([oidn, "-d", "cpu", "--hdr", directory / "colour.pfm",
                "--alb", directory / "albedo.pfm", "--nrm", directory / "normal.pfm",
                "-o", directory / "denoised.pfm", "-v", "2"], directory / "oidn.log", 120,
                env=oidn_environment(oidn))
            tone_tasks.append({"input": str(directory / "denoised.pfm"),
                               "output": str(directory / "denoised.png"),
                               "meta": str(directory / "tone.json")})
        core.atomic_write_json(stage / "tone_job.json", {"mode": "tone_map", "tasks": tone_tasks})
        run(prefix + [stage / "tone_job.json"], stage / "tone.log", 300, env=env)
        for item in inputs:
            if core.sha256_file(Path(item["path"])) != item["sha256"]:
                raise Refusal(4, "input_changed")
        schema = core.load_json(HERE / "frame.schema.json", max_bytes=32768)
        receipts = []
        for task in tasks:
            directory = stage / task["dir"]
            meta = core.load_json(directory / "render.json", max_bytes=1048576)
            if (meta.get("settings") != identity.settings(parameters, task["frame"], task["samples"])
                    or meta.get("request_sha256") != request_hash or meta.get("job_sha256") != job_hash):
                raise Refusal(4, "render_execution_identity_drift")
            for dep in meta["dependencies"]:
                if core.sha256_file(Path(dep["path"])) != dep["sha256"]:
                    raise Refusal(4, "scene_dependency_changed")
            tone = core.load_json(directory / "tone.json", max_bytes=4096)
            receipt = {"schema": "af.render-frame/v1", "rc": 0, "status": "OK",
                "frame": task["frame"], "samples": task["samples"], "resolution_percentage": resolution_percentage,
                "width": meta["width"], "height": meta["height"], "device": device,
                "seed": seed, "request_sha256": request_hash, "job_sha256": job_hash,
                "denoiser": denoiser, "view": meta["view"], "inputs": inputs + meta["dependencies"],
                "outputs": [descriptor(p, p.name, stage) for p in sorted(directory.iterdir()) if p.is_file()],
                "timings_ms": {"render": meta["render_ms"], "oidn": task["oidn_ms"], "tone_map": tone["tone_map_ms"]},
                "absence_reasons": {}}
            core.validate(receipt, schema)
            core.atomic_write_json(directory / "receipt.json", receipt)
            receipts.append(descriptor(directory / "receipt.json", "render_frame", stage))
        result = {"schema": "af.render-run/v1", "rc": 0, "status": "OK", "frames": receipts,
                  "parameters": parameters, "request_sha256": request_hash,
                  "job": descriptor(job_path, "render_job", stage),
                  "toolchain": inputs, "absence_reasons": {"quality": "render completion is not asset PASS"}}
        core.atomic_write_json(stage / "receipt.json", result)
        from tools.render.evaluate import read_run
        read_run(stage, expected_request_sha256=request_hash)
        if target.exists():
            raise Refusal(5, "output_exists")
        os.rename(stage, target)
        stage = None
        return result
    finally:
        # Keep failed logs under the output parent, without replacing caller bytes.
        if stage is not None:
            failed = stage.with_name(stage.name + ".failed")
            os.rename(stage, failed)
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--oidn", required=True, type=Path)
    parser.add_argument("--blender", required=True, type=Path)
    parser.add_argument("--frames", nargs=2, required=True, type=int)
    parser.add_argument("--references", nargs="*", type=int, default=[])
    parser.add_argument("--samples", type=int, default=96)
    parser.add_argument("--reference-samples", type=int, default=1024)
    parser.add_argument("--resolution-percentage", type=int, default=50)
    parser.add_argument("--device", choices=("CPU", "CUDA"), required=True)
    parser.add_argument("--timeout-s", type=int, default=3600)
    try:
        args = parser.parse_args(argv)
        result = render(args.scene, args.output, oidn=args.oidn, blender=args.blender,
                        start=args.frames[0], end=args.frames[1], references=args.references,
                        samples=args.samples, reference_samples=args.reference_samples,
                        resolution_percentage=args.resolution_percentage, device=args.device, timeout_s=args.timeout_s)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    except Refusal as exc:
        result = {"rc": exc.rc, "status": core.STATUS[exc.rc], "reason_codes": [exc.reason]}
    except subprocess.TimeoutExpired:
        result = {"rc": 6, "status": "BUDGET_EXCEEDED", "reason_codes": ["deadline_exceeded"]}
    except (OSError, ValueError, core.ContractError) as exc:
        result = {"rc": 2, "status": "ERROR", "reason_codes": [type(exc).__name__ + ":" + str(exc)]}
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
