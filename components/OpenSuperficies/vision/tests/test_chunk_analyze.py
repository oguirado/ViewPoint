"""vision/chunk_analyze.py: pin first, closed integer row, ramws-only, read-only, static / flicker chunks."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
TOOL = Path(os.environ.get("CA_TOOL_STUB") or REPO / "vision" / "chunk_analyze.py")  # CA_TOOL_STUB: failing-first stub run only
TEST_ROOT = "/dev/shm/vqa-ramws-test"
W, H = 64, 48
CLASSES = ["CHANGED", "DRIFT", "FLICKER", "FROZEN", "JITTER", "MOVED", "UNCHANGED"]


def run(args, env_extra=None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-B", str(TOOL)] + args, capture_output=True, text=True, timeout=120, env=env)
    return p.returncode, p.stdout


def make_chunk(frames, root=TEST_ROOT, name="chunk_0"):
    run_dir = Path(root) / ("e_%d" % os.getpid())
    d = run_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "frames.rgb24").write_bytes(b"".join(f.tobytes() for f in frames))
    return run_dir, d


def blocks(pattern):
    a = np.zeros((H, W, 3), np.uint8)
    b = a.copy()
    b[:16, :16] = 255
    return [b if c == "B" else a for c in pattern]


def tree_state(d):
    return sorted((p.name, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in Path(d).rglob("*") if p.is_file())


def args_for(d, n, grid=16):
    return ["analyze", "--chunk-dir", str(d), "--w", str(W), "--h", str(H), "--frames", str(n), "--grid", str(grid)]


class ChunkAnalyze(unittest.TestCase):
    def setUp(self):
        self.env = {"OS_CU_TEST_ROOT": "1"}
        self.run_dir = None

    def tearDown(self):
        shutil.rmtree(Path(TEST_ROOT) / ("e_%d" % os.getpid()), ignore_errors=True)

    def test_pin_imported_first(self):
        lines = TOOL.read_text(encoding="utf-8").splitlines()
        i_pin = next((i for i, l in enumerate(lines) if l.startswith("from vision import pin")), 10**6)
        i_act = next((i for i, l in enumerate(lines) if l.strip() == "pin.activate()"), 10**6)
        eng = [i for i, l in enumerate(lines) if re.match(r"(import|from) (vadapt|visioncore|vmem)", l)]
        self.assertTrue(eng and i_pin < i_act < min(eng), (i_pin, i_act, eng))
        import ast
        names = {n.id for n in ast.walk(ast.parse(TOOL.read_text())) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(ast.parse(TOOL.read_text())) if isinstance(n, ast.Attribute)}
        self.assertFalse(names & {"GuiAdapter", "adapt", "VisionRun"}, names & {"GuiAdapter", "adapt", "VisionRun"})

    def test_row_integer_closed_schema(self):
        self.run_dir, d = make_chunk(blocks("AAAA"))
        rc, out = run(args_for(d, 4), self.env)
        self.assertEqual(rc, 0, out)
        row = json.loads(out)
        self.assertEqual(sorted(row), ["analyze_us_per_frame", "chunk", "class_counts", "engine_wheel_sha8", "frames", "grid"])
        self.assertEqual(sorted(row["class_counts"]), sorted(CLASSES))
        ints = [row["chunk"], row["frames"], row["grid"], row["analyze_us_per_frame"]] + list(row["class_counts"].values())
        self.assertTrue(all(type(v) is int for v in ints), row)
        self.assertGreater(row["analyze_us_per_frame"], 0)
        self.assertEqual(row["engine_wheel_sha8"], "5e2395d5")
        self.assertEqual(sum(row["class_counts"].values()), 4 * 12)  # 4x3 tiles at s=16, every tile classified once per frame

    def test_grid_must_equal_engine_grid_rc2(self):
        self.run_dir, d = make_chunk(blocks("AA"))
        rc, out = run(args_for(d, 2, grid=32), self.env)
        self.assertEqual((rc, json.loads(out).get("code")), (2, "E_GRID"), out)
        self.assertEqual(run(args_for(d, 2, grid=16), self.env)[0], 0)

    def test_refuses_non_ramws_dir_rc5(self):
        _, ok = make_chunk(blocks("AA"))
        self.assertEqual(run(args_for(ok, 2), self.env)[0], 0)  # positive control
        bad = Path(os.environ.get("TMPDIR", "/tmp")) / ("ca_%d" % os.getpid()) / "run" / "chunk_0"
        bad.mkdir(parents=True)
        try:
            (bad / "frames.rgb24").write_bytes(b"".join(f.tobytes() for f in blocks("AA")))
            rc, out = run(args_for(bad, 2), self.env)
            self.assertEqual((rc, json.loads(out).get("code")), (5, "E_RAM_NOT_TMPFS"), out)
            rc, out = run(args_for(bad, 2))
            self.assertEqual(rc, 5, out)
        finally:
            shutil.rmtree(bad.parent.parent, ignore_errors=True)
        # unmanaged /dev/shm dir without the test hook is refused too
        rc, out = run(args_for(ok, 2))
        self.assertEqual((rc, json.loads(out).get("code")), (5, "E_RAM_NOT_TMPFS"), out)

    def test_refuses_root_outside_ramws_with_held_lock_rc5(self):
        # JW3-02: owner.json + a lock held LOCK_EX would pass the .lock check; only the RAMWS_ROOT containment refuses it
        base = Path(tempfile.mkdtemp(prefix="ca_root_"))
        run_dir = base / "run"
        (run_dir / "chunk_0").mkdir(parents=True)
        (run_dir / "owner.json").write_text("{}")
        (run_dir / "chunk_0" / "frames.rgb24").write_bytes(b"".join(f.tobytes() for f in blocks("AA")))
        lk = os.open(str(run_dir / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            rc, out = run(args_for(run_dir / "chunk_0", 2))
            self.assertEqual((rc, json.loads(out)), (5, {"code": "E_RAM_NOT_TMPFS", "detail": "unmanaged run dir"}), out)
        finally:
            os.close(lk)
            shutil.rmtree(base, ignore_errors=True)

    def test_frames_symlink_escape_refused_rc5(self):
        # ADC-W3-01: frames.rgb24 -> file outside the chunk dir must be refused; positive control: regular file passes
        self.run_dir, d = make_chunk(blocks("AA"))
        self.assertEqual(run(args_for(d, 2), self.env)[0], 0)
        outside = Path(tempfile.mkdtemp(prefix="ca_out_"))
        try:
            (outside / "d.rgb24").write_bytes(b"".join(f.tobytes() for f in blocks("AA")))
            os.unlink(d / "frames.rgb24")
            os.symlink(outside / "d.rgb24", d / "frames.rgb24")
            rc, out = run(args_for(d, 2), self.env)
            self.assertEqual((rc, json.loads(out).get("code")), (5, "E_RAM_NOT_TMPFS"), out)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_frames_in_dir_symlink_and_nonregular_refused_rc5(self):
        self.run_dir, d = make_chunk(blocks("AA"))
        os.rename(d / "frames.rgb24", d / "real.bin")
        os.symlink("real.bin", d / "frames.rgb24")  # symlink that stays inside the chunk dir is still refused
        rc, out = run(args_for(d, 2), self.env)
        self.assertEqual((rc, json.loads(out).get("code")), (5, "E_RAM_NOT_TMPFS"), out)
        os.unlink(d / "frames.rgb24")
        os.mkdir(d / "frames.rgb24")  # not a regular file
        rc, out = run(args_for(d, 2), self.env)
        self.assertEqual((rc, json.loads(out).get("code")), (5, "E_RAM_NOT_TMPFS"), out)

    def test_never_writes_frames(self):
        self.run_dir, d = make_chunk(blocks("ABAB"))
        before = tree_state(self.run_dir)
        rc, out = run(args_for(d, 4), self.env)
        self.assertEqual(rc, 0, out)
        self.assertTrue(json.loads(out).get("frames") == 4 and before)
        self.assertEqual(tree_state(self.run_dir), before)
        os.chmod(d / "frames.rgb24", 0o444)
        os.chmod(d, 0o555)
        try:
            self.assertEqual(run(args_for(d, 4), self.env)[0], 0)  # runs on a read-only chunk
        finally:
            os.chmod(d, 0o755)

    def test_static_chunk_all_unchanged(self):
        self.run_dir, d = make_chunk(blocks("AAAAAA"))
        rc, out = run(args_for(d, 6), self.env)
        self.assertEqual(rc, 0, out)
        c = json.loads(out).get("class_counts", {})
        self.assertEqual(c.get("UNCHANGED"), 6 * 12, c)
        self.assertEqual(sum(v for k, v in c.items() if k != "UNCHANGED"), 0, c)

    def test_flicker_chunk_detects_flicker(self):
        self.run_dir, d = make_chunk(blocks("ABABAB"))  # engine FLICKER is period-2 (prev2); long-period blink is ER-014
        rc, out = run(args_for(d, 6), self.env)
        self.assertEqual(rc, 0, out)
        c = json.loads(out).get("class_counts", {})
        self.assertGreater(c.get("FLICKER", 0), 0, c)

    def test_host_handshake_through_driver_real_engine(self):
        # CF-02: the host driver runs this tool (pinned wheel) on a READY chunk and writes ANALYZED with the row sha
        sys.path.insert(0, str(REPO / "bench" / "wl"))
        import driver as D
        self.run_dir, d = make_chunk(blocks("ABAB"))
        (d / "READY").write_text(json.dumps({"schema": "os.cu.chunk-ready/v1", "chunk": 0, "frames": 4, "w": W, "h": H, "grid": 16}))
        old = os.environ.get("OS_CU_TEST_ROOT")
        os.environ["OS_CU_TEST_ROOT"] = "1"
        try:
            a = D.parser().parse_args(["--out", "/x", "--analyze", "host", "--analyze-script", str(TOOL)])
            line = D._analyze_chunk(a, str(d), 0, W, H, time.monotonic() + 120)
        finally:
            if old is None:
                os.environ.pop("OS_CU_TEST_ROOT", None)
            else:
                os.environ["OS_CU_TEST_ROOT"] = old
        self.assertEqual((d / "ANALYZED").read_text(), line)
        row = json.loads(line)
        self.assertEqual((row["engine_wheel_sha8"], row["frames"], row["chunk"]), ("5e2395d5", 4, 0))
        self.assertGreater(row["class_counts"]["FLICKER"], 0)
        self.assertFalse((d / "ANALYZE_FAILED").exists())
        (d / "ANALYZED").unlink()
        (d / "frames.rgb24").write_bytes(b"\0" * 10)  # short chunk: the engine refuses it, the host marks ANALYZE_FAILED
        self.assertIsNone(D._analyze_chunk(a, str(d), 0, W, H, time.monotonic() + 120))
        self.assertEqual(json.loads((d / "ANALYZE_FAILED").read_text())["rc"], 5)

    def test_no_rc1_on_bad_usage(self):
        for a in ([], ["analyze"], ["analyze", "--bogus", "1"], ["analyze", "--chunk-dir", "/nonexistent/chunk_0", "--w", "x", "--h", "1", "--frames", "1", "--grid", "16"]):
            rc, out = run(a, self.env)
            self.assertIn(rc, (2, 5), (a, rc, out))
        self.assertEqual(run(["analyze", "--chunk-dir", "/nonexistent/chunk_0", "--w", "x", "--h", "1", "--frames", "1", "--grid", "16"])[0], 2)


if __name__ == "__main__":
    unittest.main()
