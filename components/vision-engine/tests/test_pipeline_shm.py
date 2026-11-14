"""rgb24_raw shm frame source (CDESIGN_CENG F2)."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from visioncore import frame as fr, pipeline
from visioncore.errors import VqaError


def write_raw_run(root, run_id, n=4, W=48, H=32):
    d = pipeline.shm_run_dir(run_id, root)
    d.mkdir(parents=True)
    rng = np.random.default_rng(1)
    arr = rng.integers(0, 256, (n, H, W, 3), dtype=np.uint8)
    (d / "frames.rgb24").write_bytes(arr.tobytes())
    m = {"schema": "vqa.frame/v1", "width": W, "height": H, "channels": 3, "pixel_format": "rgb24", "fps_num": 30,
         "fps_den": 1, "count": n, "storage": "rgb24_raw", "raw_path": "frames.rgb24", "frame_bytes": W * H * 3,
         "frames": [{"i": i, "t_us": i * 33333, "render_index": i, "file": "frames.rgb24",
                     "sha256": hashlib.sha256(arr[i].tobytes()).hexdigest(), "file_sha256": hashlib.sha256(arr[i].tobytes()).hexdigest()} for i in range(n)],
         "hints": None, "still_probe": None, "scope": {"app": "t", "scene": "t", "res": f"{W}x{H}", "build_seq": None},
         "provenance": {"adapter_id": "synth", "adapter_version": "1", "capture_receipt_sha256": None}}
    fr.write_manifest(d / "frames.manifest.json", m)
    return d, arr


class TestShmSource(unittest.TestCase):
    def test_open_and_analyze_parallel_equals_serial(self):
        root = Path(tempfile.mkdtemp(prefix="shm_"))
        d, arr = write_raw_run(root, "r1")
        src = pipeline.open_shm_run("r1", root)
        self.assertEqual(src.raw_array.shape, arr.shape)
        self.assertFalse(src.raw_array.flags.writeable)
        self.assertEqual([f.rgb.tobytes() for f in src], [a.tobytes() for a in arr])
        a = pipeline.analyze(src, workers=1)
        b = pipeline.analyze(pipeline.open_shm_run("r1", root), workers=2, cpus=[])
        self.assertEqual(a.vision_json(), b.vision_json())
        self.assertEqual(a._blobs()["tiles.bin"], b._blobs()["tiles.bin"])

    def test_bad_run_id_refused(self):
        for bad in ("../x", "A", "", "a/b", "x" * 49):
            with self.assertRaises(VqaError):
                pipeline.shm_run_dir(bad)

    def test_wrong_size_refused(self):
        root = Path(tempfile.mkdtemp(prefix="shm_"))
        d, _ = write_raw_run(root, "r2")
        (d / "frames.rgb24").write_bytes(b"\0" * 10)
        with self.assertRaises(VqaError) as c:
            pipeline.open_shm_run("r2", root)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")


if __name__ == "__main__":
    unittest.main()
