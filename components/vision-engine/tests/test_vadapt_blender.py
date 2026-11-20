import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from vadapt.blender import BlenderAdapter, adapt
from visioncore import canon, decode, frame as vframe
from visioncore.errors import VqaError


def _img(seed, W=64, H=48):
    return ((np.arange(W * H * 3, dtype=np.uint32).reshape(H, W, 3) * (seed + 3)) & 255).astype(np.uint8)


def _mk(d, nums=(1, 2, 3), prefix="frame_", **kw):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    for n in nums:
        (d / ("%s%04d.png" % (prefix, n))).write_bytes(decode.encode_png(_img(n)))
    return d


class BlenderAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_frames_order_and_render_index(self):
        d = _mk(self.root / "r", nums=(10, 2, 3))
        src = BlenderAdapter().frames(d)
        self.assertEqual(len(src), 3)
        self.assertEqual([f.render_index for f in src], [2, 3, 10])
        self.assertEqual([f.i for f in src], [0, 1, 2])
        vframe.validate_manifest(src.meta)
        self.assertEqual(src.meta["provenance"]["adapter_id"], "blender")
        self.assertEqual(src.meta["frames"][0]["sha256"], list(src)[0].sha256)

    def test_scene_json_scope_and_fps(self):
        d = _mk(self.root / "r")
        (d / "scene.json").write_text('{"app":"My App!","scene":"Shot 01","build_seq":7,"fps_num":30,"fps_den":1}')
        m = BlenderAdapter().frames(d).meta
        self.assertEqual(m["scope"], {"app": "my_app", "scene": "shot_01", "res": "64x48", "build_seq": 7})
        self.assertEqual([f["t_us"] for f in m["frames"]], [0, 33333, 66666])

    def test_hints(self):
        d = _mk(self.root / "r")
        (d / "bounds.jsonl").write_text(
            '{"frame":2,"name":"cube","role":"object","bbox_px":[1,2,10,10],"z":3}\n'
            '{"frame":1,"name":"axes","role":"gizmo","bbox_px":[0,0,5,5]}\n')
        rows = list(BlenderAdapter().hints(d))
        self.assertEqual([r["i"] for r in rows], [0, 1])
        self.assertEqual(rows[0]["rid"], "h:gizmo:axes")
        self.assertIsNone(rows[0]["z"])
        self.assertIsNone(BlenderAdapter().hints(_mk(self.root / "n")))

    def test_bad_hints(self):
        for line in ('{"frame":9,"name":"a","bbox_px":[0,0,1,1]}', '{"frame":1,"name":"A b","bbox_px":[0,0,1,1]}',
                     '{"frame":1,"name":"a","role":"zzz","bbox_px":[0,0,1,1]}', '{"frame":1,"name":"a","bbox_px":[0,0,0,1]}',
                     'not json'):
            d = _mk(self.root / ("b%d" % abs(hash(line))))
            (d / "bounds.jsonl").write_text(line + "\n")
            with self.assertRaises(VqaError) as c:
                BlenderAdapter().hints(d)
            self.assertEqual(c.exception.rc, 2, line)

    def test_still_probe(self):
        d = _mk(self.root / "r")
        self.assertIsNone(BlenderAdapter().still_probe(d))
        _mk(d / "still", nums=(1, 2))
        self.assertEqual(len(BlenderAdapter().still_probe(d)), 2)

    def test_exr_and_empty_refused(self):
        d = _mk(self.root / "r")
        (d / "frame_0009.exr").write_bytes(b"x")
        with self.assertRaises(VqaError) as c:
            BlenderAdapter().frames(d)
        self.assertEqual(c.exception.code, "E_VC_PNG")
        (self.root / "e").mkdir()
        with self.assertRaises(VqaError) as c:
            BlenderAdapter().frames(self.root / "e")
        self.assertEqual(c.exception.rc, 2)

    def test_size_mismatch_and_duplicates(self):
        d = _mk(self.root / "r")
        (d / "frame_0004.png").write_bytes(decode.encode_png(_img(4, 32, 32)))
        with self.assertRaises(VqaError) as c:
            BlenderAdapter().frames(d)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")
        d2 = _mk(self.root / "r2")
        (d2 / "other_0001.png").write_bytes(decode.encode_png(_img(1)))
        with self.assertRaises(VqaError):
            BlenderAdapter().frames(d2)

    def test_missing_dir(self):
        with self.assertRaises(VqaError) as c:
            BlenderAdapter().frames(self.root / "nope")
        self.assertEqual(c.exception.code, "E_IO")

    def test_adapt_writes_and_roundtrips(self):
        d = _mk(self.root / "r")
        (d / "bounds.jsonl").write_text('{"frame":1,"name":"cube","bbox_px":[0,0,4,4]}\n')
        out = self.root / "o"
        man = adapt(d, out)
        m2 = vframe.read_manifest(out / "frames.manifest.json")
        self.assertEqual(man, m2)
        self.assertEqual(m2["hints"], "region_hints.jsonl")
        for it in m2["frames"]:
            rgb = decode.png(out / it["file"])
            self.assertEqual(vframe.Frame(it["i"], it["t_us"], it["render_index"], rgb).sha256, it["sha256"])
        self.assertTrue(canon.is_canonical((out / "region_hints.jsonl").read_text()))
        # deterministic
        out2 = self.root / "o2"
        adapt(d, out2)
        self.assertEqual((out / "frames.manifest.json").read_bytes(), (out2 / "frames.manifest.json").read_bytes())

    def test_adapt_hashes_and_writes_the_bytes_it_decodes(self):
        d = _mk(self.root / "r", nums=(1,))
        src = d / "frame_0001.png"
        captured = decode.encode_png(_img(2))
        read_bytes = Path.read_bytes

        def read_frame(path):
            return captured if path == src else read_bytes(path)

        out = self.root / "o"
        with patch.object(Path, "read_bytes", read_frame), \
                patch.object(decode, "png", wraps=decode.png) as png:
            man = adapt(d, out)
        png.assert_called_once_with(captured)
        item = man["frames"][0]
        self.assertEqual((out / item["file"]).read_bytes(), captured)
        self.assertEqual(item["file_sha256"], canon.sha256_bytes(captured))
        self.assertEqual(item["sha256"], vframe.Frame(0, 0, 1, _img(2)).sha256)

    def test_frame_read_failures_report_io_error(self):
        d = _mk(self.root / "r")
        out = self.root / "o"
        for read in (lambda: BlenderAdapter().frames(d), lambda: adapt(d, out)):
            with self.subTest(read=read), \
                    patch.object(Path, "read_bytes", side_effect=PermissionError("reading a frame")):
                with self.assertRaises(VqaError) as ctx:
                    read()
                self.assertEqual(ctx.exception.code, "E_IO")
        self.assertFalse(out.exists())

    def test_adapt_refusal_leaves_out_untouched(self):
        d = _mk(self.root / "r")
        (d / "bounds.jsonl").write_text('{"frame":99,"name":"c","bbox_px":[0,0,1,1]}\n')
        out = self.root / "o"
        with self.assertRaises(VqaError):
            adapt(d, out)
        self.assertFalse(out.exists())

    def test_hint_frame_must_be_an_integer(self):
        d = _mk(self.root / "r")
        for invalid, expected in (("true", "E_VC_FRAME_SCHEMA"), ("1.0", "E_FLOAT"), ("[]", "E_VC_FRAME_SCHEMA")):
            with self.subTest(frame=invalid):
                (d / "bounds.jsonl").write_text(
                    '{"frame":%s,"name":"cube","bbox_px":[0,0,4,4]}\n' % invalid)
                with self.assertRaises(VqaError) as ctx:
                    BlenderAdapter().hints(d)
                self.assertEqual(ctx.exception.code, expected)

    def test_adapt_write_error_preserves_existing_output(self):
        d = _mk(self.root / "r")
        out = self.root / "o"
        adapt(d, out)
        (out / "notes.txt").write_text("keep")
        before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        newer = _mk(self.root / "newer", nums=(4, 5, 6, 7))
        replace = Path.replace

        def fail_manifest(src, dst):
            if dst == out / "frames.manifest.json":
                raise PermissionError("publishing manifest")
            return replace(src, dst)

        with patch.object(Path, "replace", fail_manifest):
            with self.assertRaises(VqaError) as ctx:
                adapt(newer, out)
        self.assertEqual(ctx.exception.code, "E_IO")
        self.assertEqual({p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}, before)

    def test_adapt_write_error_leaves_new_output_absent(self):
        d = _mk(self.root / "r")
        out = self.root / "o"
        with patch.object(vframe, "write_manifest", side_effect=PermissionError("writing manifest")):
            with self.assertRaises(VqaError) as ctx:
                adapt(d, out)
        self.assertEqual(ctx.exception.code, "E_IO")
        self.assertFalse(out.exists())

    def test_no_bpy_import(self):
        import ast
        import vadapt.blender.from_render_dir as m
        tree = ast.parse(Path(m.__file__).read_text())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertNotIn("bpy", mods)


if __name__ == "__main__":
    unittest.main()
