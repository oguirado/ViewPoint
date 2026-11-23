"""Explicit live harness; discovery never launches a GUI.

Run under the C2b systemd scope wrapper with component root on PYTHONPATH:
python3 -B tests/test_vqa3d_live_acceptance.py
Owns and stops only its recorded Blender and Xvfb PIDs, even on failure.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from vqa3d.adapters.blender_live import BlenderLiveAdapter
from vqa3d.canon import canonical_json

COMPONENT = Path(__file__).resolve().parents[1]
LOG_ROOT = Path(os.environ.get("VQA3D_CAMPAIGN_LIVE", str(COMPONENT / "build" / "live")))


def run_live_acceptance():
    stamp = time.strftime("%H%M_%S")
    prefix = LOG_ROOT / ("C2fix_" + stamp)
    log = open(str(prefix) + "_acceptance.log", "w", buffering=1)
    processes, adapters, streams = [], [], []
    root = Path(tempfile.mkdtemp(prefix="run-", dir="/dev/shm/vqa3d-c2fix"))

    def emit(kind, **fields):
        row = dict(kind=kind, **fields)
        print(canonical_json(row), flush=True)
        print(canonical_json(row), file=log, flush=True)

    def spawn(argv, env, label):
        stream = open(str(prefix) + "_" + label + ".log", "w")
        streams.append(stream)
        proc = subprocess.Popen(argv, env=env, cwd=COMPONENT, stdout=stream, stderr=subprocess.STDOUT)
        processes.append((label, proc))
        emit("process_started", label=label, pid=proc.pid, argv=argv)
        return proc

    def cli(scenario, label, expected, record=False):
        argv = [sys.executable, "-B", "-m", "vqa3d", "check", "--adapter", "blender_live",
                "--scenario", str(scenario), "--process-log", str(prefix) + "_" + label + "_exchanges.json"]
        if record:
            argv += ["--record-trace", str(COMPONENT / ("vqa3d/traces/blender_live_" + label + ".json"))]
        env = dict(os.environ, VQA3D_LIVE_DIR=str(root), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(argv, env=env, cwd=COMPONENT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=120)
        output = result.stdout.decode("utf-8", "replace")
        Path(str(prefix) + "_" + label + ".log").write_text(output)
        rows = [json.loads(line) for line in output.splitlines()]
        emit("cli", argv=argv, rc=result.returncode, expected_rc=expected,
             log=str(prefix) + "_" + label + ".log",
             verdicts=[r.get("final_verdict") for r in rows],
             receipts=[r.get("receipt_sha256") for r in rows])
        assert result.returncode == expected, output
        return rows

    try:
        display = next(n for n in range(120, 65536) if not Path("/tmp/.X11-unix/X" + str(n)).exists()
                       and not Path("/tmp/.X" + str(n) + "-lock").exists())
        home = root / "home"
        home.mkdir()
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
               "DISPLAY": ":" + str(display), "VQA3D_LIVE_DIR": str(root),
               "XDG_RUNTIME_DIR": str(runtime), "PYTHONDONTWRITEBYTECODE": "1",
               "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "1", "LIBGL_ALWAYS_SOFTWARE": "1"}
        xvfb = spawn(["/usr/bin/Xvfb", env["DISPLAY"], "-screen", "0", "1024x768x24", "-nolisten", "tcp"], env, "xvfb")
        limit = time.monotonic() + 10
        while not Path("/tmp/.X11-unix/X" + str(display)).exists():
            assert xvfb.poll() is None and time.monotonic() < limit, "Xvfb unavailable"
            time.sleep(0.05)
        scratch = root / "scene.blend"
        source = COMPONENT / "examples/cube3.blend"
        shutil.copyfile(source, scratch)
        emit("fixture", source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
             scratch=str(scratch), display=display, live_dir=str(root))
        gui = spawn(["/usr/bin/blender", "--factory-startup", "-noaudio", str(scratch), "--python",
                     str(COMPONENT / "vqa3d/adapters/blender_live_bridge.py")], env, "blender")
        adapter = BlenderLiveAdapter(root, timeout=30)
        adapters.append(adapter)
        limit = time.monotonic() + 60
        while not (root / "inbox").is_dir():
            assert gui.poll() is None and time.monotonic() < limit, "Blender bridge startup failed"
            time.sleep(0.1)
        probe = adapter.probe()
        emit("probe", result=probe)
        assert probe["available"], probe
        emit("state", state=adapter.scene_state())
        cli(COMPONENT / "vqa3d/scenarios/blender_live_move.json", "move", 0, record=True)
        cli(COMPONENT / "vqa3d/scenarios/blender_live_wrong_expectation.json", "wrong", 1)
        bad = root / "raise.py"
        bad.write_text("raise RuntimeError('C2b injected script failure')\n")
        scenario = root / "script_error.json"
        scenario.write_text(canonical_json([{"action": {"op": "run_script", "path": str(bad)},
                                              "obligation": {"kind": "frame_change"}}]))
        cli(scenario, "script_error", 2)
        pause = root / "pause_draw.py"
        pause.write_text("bpy.app.driver_namespace['_vqa3d_live_counters']['pause_draw'] = True\n")
        action_receipt = adapter.act({"op": "run_script", "path": str(pause)})
        emit("pause_draw", receipt=action_receipt)
        cli(COMPONENT / "vqa3d/scenarios/blender_live_move.json", "stale", 3, record=True)
        resume = root / "resume_draw.py"
        resume.write_text("bpy.app.driver_namespace['_vqa3d_live_counters']['pause_draw'] = False\n")
        emit("resume_draw", receipt=adapter.act({"op": "run_script", "path": str(resume)}))
        samples = []
        for index in range(10):
            adapter.act({"op": "set_location", "object": "Cube.001 Big", "value_um": [(index % 2) * 1000000, 0, 0]})
            started = time.monotonic_ns()
            obs = adapter.capture("latency_" + str(index))
            elapsed = time.monotonic_ns() - started
            assert obs.capture_available, obs.notes
            samples.append(elapsed)
            emit("capture_sample", elapsed_ns=elapsed, obs_id=obs.obs_id, metadata=json.loads(obs.notes[0]))
        emit("latency", n=len(samples), p50_ns=int(statistics.median(samples)), max_ns=max(samples),
             scope="post-action fresh-draw wait plus scene-camera render and RGB decode; host Xvfb llvmpipe")
        viewport = BlenderLiveAdapter(root, timeout=30, viewport=True)
        adapters.append(viewport)
        observation = viewport.capture("viewport_diagnostic")
        emit("viewport", metadata=json.loads(observation.notes[0]), obs_id=observation.obs_id)
        emit("live_acceptance", result="PASS")
    finally:
        for adapter in reversed(adapters):
            adapter.close()
        for label, proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            emit("process_stopped", label=label, pid=proc.pid, rc=proc.returncode,
                 reaped=proc.poll() is not None)
        for stream in streams:
            stream.close()
        shutil.rmtree(root)
        emit("scratch_removed", live_dir=str(root))
        log.close()


if __name__ == "__main__":
    Path("/dev/shm/vqa3d-c2fix").mkdir(exist_ok=True)
    run_live_acceptance()
