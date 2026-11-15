"""P1 W2 storage: frames only in ramws; SIGTERM mid-run removes the workspace; no frame files under the repo (M9)."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKSET = ["taskset", "-c", "5-9"]  # run.py requires CPUs 5-9 in the affinity mask


def repo_frame_files():
    out = []
    for d, ds, fs in os.walk(REPO):
        ds[:] = [x for x in ds if x not in (".git", "__pycache__") and not x.startswith("room_")]  # other lanes' scratch is not ours
        out += [os.path.join(d, f) for f in fs if f.endswith((".rgb24", ".png", ".raw", ".npy"))]
    return out


def _own_frames(root, pid):
    """frames.rgb24 files of workspaces whose owner.json names `pid` (other lanes may run gui_synth concurrently)."""
    out = set()
    for d, _, fs in os.walk(root) if os.path.isdir(root) else ():
        if "frames.rgb24" in fs and "owner.json" in fs:
            try:
                with open(os.path.join(d, "owner.json"), encoding="utf-8") as f:
                    if json.load(f).get("pid") == pid:
                        out.add(os.path.join(d, "frames.rgb24"))
            except (OSError, ValueError):
                pass
    return out


class TestStorage(unittest.TestCase):
    def test_run_scene_leaves_no_frames_on_disk(self):
        before = set(repo_frame_files())
        code = ("from eval.gui_synth import run, scenes\n"
                "run.run_scene('scroll_small', 0, split='DEV', w=320, h=180, n=8, workers=1)\n")
        r = subprocess.run(TASKSET + [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertEqual(set(repo_frame_files()), before)

    def test_sigterm_removes_ramws(self):
        code = ("from eval.gui_synth import run\n"
                "run.main(['--split','DEV','--res','320x180','--backend','python','--out','%s'])\n"
                % os.path.join("build", "room_T", "sigterm.json"))
        os.makedirs(os.path.join(REPO, "build", "room_T"), exist_ok=True)
        root = "/dev/shm/vqa-ramws/gui_synth"
        mine = set()
        p = subprocess.Popen(TASKSET + [sys.executable, "-c", code], cwd=REPO)
        try:
            seen = False
            for _ in range(100):
                mine = _own_frames(root, p.pid)  # frames.rgb24 exists only after open_run installed the signal handlers
                if mine:
                    seen = True
                    break
                time.sleep(0.1)
            self.assertTrue(seen, "run never opened a gui_synth ramws workspace")
            self.assertIsNone(p.poll(), "run finished before SIGTERM; not a mid-run kill")
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
        left = sorted(f for f in mine if os.path.exists(f))
        self.assertEqual(left, [])
        r = subprocess.run([sys.executable, "tools/gates.py", "--gate", "g_disk_frames"], cwd=REPO, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:  # a live foreign engine process (e.g. another lane's blender) trips the gate independently of us
            rows = json.loads(r.stdout)["findings"]["g_disk_frames"]
            self.assertTrue(rows and all("engine process alive" in x[1] for x in rows), r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
