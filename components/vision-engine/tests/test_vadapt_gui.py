import ast
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np

from visioncore import canon, decode, desc, frame as vframe
from visioncore import change as C
from visioncore.errors import VqaError
from visioncore.grid import Grid
from vadapt.gui import GuiAdapter, from_screens
from vadapt.synth.pngenc import encode_png


def shot(k, W=64, H=48):
    rgb = np.full((H, W, 3), 10 * k, np.uint8)
    rgb[:, :, 1] = np.arange(W, dtype=np.uint8)
    return rgb


def make_run(root, n=4, W=64, H=48, steps=None, a11y=None, idle=0, app=None, filt=0):
    root = Path(root)
    (root / "screens").mkdir(parents=True)
    for k in range(n):
        (root / "screens" / ("s%d.png" % k)).write_bytes(encode_png(shot(k, W, H), filt=filt))
    if steps is not None:
        (root / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in steps))
    if a11y is not None:
        (root / "a11y.jsonl").write_text("".join(json.dumps(r) + "\n" for r in a11y))
    if idle:
        (root / "idle").mkdir()
        for k in range(idle):
            (root / "idle" / ("i%02d.png" % k)).write_bytes(encode_png(np.full((H, W, 3), 7, np.uint8)))
    if app is not None:
        (root / "app.json").write_text(json.dumps(app))
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="vgui_"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.a = GuiAdapter()


class Frames(Base):
    def test_natural_sort_and_manifest(self):
        run = make_run(self.d / "r", n=12)
        src = self.a.frames(run)
        self.assertEqual(len(src), 12)
        m = src.meta
        vframe.validate_manifest(m)
        self.assertEqual([f["file"] for f in m["frames"]][:3], ["screens/s0.png", "screens/s1.png", "screens/s2.png"])
        self.assertEqual(m["frames"][10]["file"], "screens/s10.png")  # natural, not lexicographic
        self.assertEqual(m["provenance"]["adapter_id"], "gui")
        self.assertEqual(m["storage"], "png_dir")
        for f, row in zip(src, m["frames"]):
            self.assertEqual(f.sha256, row["sha256"])
            self.assertEqual(row["file_sha256"], hashlib.sha256((run / row["file"]).read_bytes()).hexdigest())
        self.assertTrue(np.array_equal(list(src)[3].rgb, shot(3)))

    def test_steps_map_render_index_and_irregular_time(self):
        steps = [{"file": "s%d.png" % k, "step": 5 * k, "t_us": k * k * 1000} for k in range(4)]
        run = make_run(self.d / "r", steps=steps)
        fr = list(self.a.frames(run))
        self.assertEqual([f.render_index for f in fr], [0, 5, 10, 15])
        self.assertEqual([f.t_us for f in fr], [0, 1000, 4000, 9000])

    def test_default_time_from_step(self):
        run = make_run(self.d / "r", n=3)
        self.assertEqual([f.t_us for f in self.a.frames(run)], [0, 1000000, 2000000])

    def test_decreasing_time_rejected(self):
        steps = [{"file": "s0.png", "t_us": 10}, {"file": "s1.png", "t_us": 5}]
        run = make_run(self.d / "r", n=2, steps=steps)
        with self.assertRaises(VqaError) as c:
            self.a.frames(run)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

    def test_steps_path_escape_rejected(self):
        run = make_run(self.d / "r", n=1, steps=[{"file": "../x.png"}])
        with self.assertRaises(VqaError) as c:
            self.a.frames(run)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

    def test_mixed_sizes_rejected(self):
        run = make_run(self.d / "r", n=2)
        (run / "screens" / "s1.png").write_bytes(encode_png(shot(1, 32, 48)))
        with self.assertRaises(VqaError) as c:
            self.a.frames(run)
        self.assertEqual(c.exception.code, "E_VC_FRAME_SIZE")

    def test_empty_and_missing(self):
        (self.d / "e" / "screens").mkdir(parents=True)
        for p in (self.d / "e", self.d / "nope"):
            with self.assertRaises(VqaError) as c:
                self.a.frames(p)
            self.assertEqual(c.exception.code, "E_IO")

    def test_garbage_png(self):
        run = make_run(self.d / "r", n=1)
        (run / "screens" / "s0.png").write_bytes(b"not a png")
        with self.assertRaises(VqaError) as c:
            self.a.frames(run)
        self.assertEqual(c.exception.code, "E_VC_PNG")

    def test_png_parity_all_filters_and_alpha(self):
        rgb = shot(3, 40, 33)
        alpha = np.arange(40 * 33, dtype=np.uint8).reshape(33, 40)
        for filt in (0, 1, 2, 3, 4, -1):
            for al in (None, alpha):
                data = encode_png(rgb, filt=filt, alpha=al)
                self.assertTrue(np.array_equal(decode.png(data), rgb))
                self.assertTrue(np.array_equal(decode.png(data), decode.png(bytes(data))))

    def test_interlaced_rejected(self):
        data = bytearray(encode_png(shot(1)))
        data[28] = 1  # IHDR interlace byte
        with self.assertRaises(VqaError) as c:
            decode.png(bytes(data))
        self.assertEqual(c.exception.code, "E_VC_PNG")

    def test_deterministic(self):
        run = make_run(self.d / "r", n=4, filt=-1)
        a = canon.dumps(self.a.frames(run).meta)
        b = canon.dumps(GuiAdapter().frames(run).meta)
        self.assertEqual(a, b)


class Scope(Base):
    def test_defaults(self):
        run = make_run(self.d / "r", n=1)
        self.assertEqual(self.a.scope(run), {"app": "gui", "scene": "session", "res": "64x48", "build_seq": None})

    def test_app_json_slug(self):
        run = make_run(self.d / "r", n=1, app={"app": "Open Superficies", "scene": "Login Screen!", "build_seq": 7})
        self.assertEqual(self.a.scope(run), {"app": "open_superficies", "scene": "login_screen", "res": "64x48",
                                             "build_seq": 7})
        vframe.validate_manifest(self.a.frames(run).meta)

    def test_bad_build_seq(self):
        run = make_run(self.d / "r", n=1, app={"build_seq": -1})
        with self.assertRaises(VqaError):
            self.a.scope(run)


class Hints(Base):
    def rows(self, run):
        return list(self.a.hints(run))

    def test_none_without_a11y(self):
        self.assertIsNone(self.a.hints(make_run(self.d / "r", n=2)))

    def test_hints_rows_clip_and_order(self):
        a11y = [{"step": 1, "name": "OK Button", "bbox": [60, 40, 20, 20], "role": "button"},
                {"step": 0, "name": "Title", "bbox": [-4, 0, 20, 8], "z": 3},
                {"step": 0, "name": "Title", "bbox": [0, 20, 8, 8]},
                {"step": 0, "name": "gone", "bbox": [100, 100, 5, 5]}]
        run = make_run(self.d / "r", n=2, a11y=a11y)
        r = self.rows(run)
        self.assertEqual([(h["i"], h["rid"], h["bbox_px"], h["z"]) for h in r],
                         [(0, "h:a11y:title", [0, 0, 16, 8], 3), (0, "h:a11y:title_2", [0, 20, 8, 8], None),
                          (1, "h:a11y:ok_button", [60, 40, 4, 8], None)])
        for h in r:
            self.assertEqual((h["schema"], h["role"]), ("vqa.region-hint/v1", "a11y"))

    def test_step_maps_via_render_index(self):
        steps = [{"file": "s%d.png" % k, "step": 10 + k} for k in range(2)]
        run = make_run(self.d / "r", n=2, steps=steps, a11y=[{"step": 11, "name": "x", "bbox": [0, 0, 4, 4]}])
        self.assertEqual(self.rows(run)[0]["i"], 1)

    def test_bad_rows(self):
        bad = [{"step": 9, "name": "x", "bbox": [0, 0, 4, 4]},      # no such frame
               {"step": 0, "name": "x", "bbox": [0, 0, 4]},         # arity
               {"step": 0, "name": "x", "bbox": [0, 0, 0, 4]},      # empty
               {"step": 0, "name": "x", "bbox": [0, 0, 4.5, 4]},    # float
               {"name": "x", "bbox": [0, 0, 4, 4]}]                 # no step
        for row in bad:
            run = make_run(self.d / ("r%d" % len(row) + str(bad.index(row))), n=1, a11y=[row])
            with self.assertRaises(VqaError, msg=row) as c:
                self.rows(run)
            self.assertEqual(c.exception.code, "E_VC_FRAME_SCHEMA")

    def test_rid_grammar(self):
        run = make_run(self.d / "r", n=1, a11y=[{"step": 0, "name": "  Ünï / Çode!! " + "z" * 80, "bbox": [0, 0, 4, 4]},
                                                {"step": 0, "name": "", "bbox": [4, 0, 4, 4]}])
        import re
        for h in self.rows(run):
            self.assertRegex(h["rid"], r"^h:a11y:[a-z0-9_]{1,40}$")


class Still(Base):
    def test_none_without_idle(self):
        self.assertIsNone(self.a.still_probe(make_run(self.d / "r", n=1)))

    def test_probe(self):
        run = make_run(self.d / "r", n=1, idle=30)
        s = self.a.still_probe(run)
        self.assertEqual(len(s), 30)
        vframe.validate_manifest(s.meta)
        self.assertEqual(s.meta["scope"]["scene"], "still_probe")
        self.assertEqual(s.meta["frames"][0]["file"], "idle/i00.png")
        self.assertTrue(self.a.frames(run).meta["still_probe"])


class Adapt(Base):
    def test_adapt_writes_and_resolves_files(self):
        run = make_run(self.d / "r", n=3, a11y=[{"step": 0, "name": "a", "bbox": [0, 0, 8, 8]}])
        out = self.d / "out" / "deep"
        m = self.a.adapt(run, out)
        self.assertEqual(m["hints"], "region_hints.jsonl")
        text = (out / "frames.manifest.json").read_text()
        self.assertTrue(canon.is_canonical(text))
        man = vframe.read_manifest(out / "frames.manifest.json")
        self.assertTrue((out / man["frames"][2]["file"]).is_file())
        self.assertEqual(decode.png(out / man["frames"][2]["file"]).tobytes(), shot(2).tobytes())
        self.assertTrue(canon.is_canonical((out / "region_hints.jsonl").read_text()))

    def test_adapt_no_a11y_has_no_hints_file(self):
        run = make_run(self.d / "r", n=1)
        out = self.d / "o"
        self.a.adapt(run, out)
        self.assertFalse((out / "region_hints.jsonl").exists())
        self.assertIsNone(vframe.read_manifest(out / "frames.manifest.json")["hints"])

    def test_refusal_leaves_out_untouched(self):
        run = make_run(self.d / "r", n=1, a11y=[{"step": 9, "name": "x", "bbox": [0, 0, 4, 4]}])
        out = self.d / "o"
        out.mkdir()
        (out / "guard").write_bytes(b"g")
        (out / "frames.manifest.json").write_bytes(b"old")
        with self.assertRaises(VqaError):
            self.a.adapt(run, out)
        self.assertEqual((out / "frames.manifest.json").read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["frames.manifest.json", "guard"])

    def test_cli(self):
        run = make_run(self.d / "r", n=2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = from_screens.main(["--in", str(run), "--out", str(self.d / "o")])
        o = json.loads(buf.getvalue())
        self.assertEqual((rc, o["code"], o["rc"], o["detail"]["count"]), (0, "OK", 0, 2))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = from_screens.main(["--in", str(self.d / "nope"), "--out", str(self.d / "o2")])
        self.assertEqual((rc, json.loads(buf.getvalue())["code"]), (2, "E_IO"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = from_screens.main(["--bogus"])
        self.assertEqual((rc, json.loads(buf.getvalue())["code"]), (2, "E_USAGE"))

    def test_no_engine_imports(self):
        for p in Path(from_screens.__file__).parent.glob("*.py"):
            mods = set()
            for n in ast.walk(ast.parse(p.read_text())):
                if isinstance(n, ast.Import):
                    mods.update(a.name for a in n.names)
                elif isinstance(n, ast.ImportFrom):
                    mods.add(("." * n.level) + (n.module or ""))
                    mods.update(("%s.%s" % (n.module, a.name)) for a in n.names)
            for m in mods:
                self.assertFalse(m.split(".")[0] in ("bpy", "godot", "vmem", "vadapt"), (p.name, m))


def run_vision(frames):
    """S0-S2 over decoded frames -> sha256 of the concatenated per-frame tile bytes + frames_sha256."""
    frames = list(frames)
    H, W = frames[0].shape[:2]
    g = Grid(W, H)
    h = hashlib.sha256()
    words_prev = xh = st = None
    for k, rgb in enumerate(frames):
        padded = g.pad(rgb)
        tiles, eq = desc.describe_padded(padded, g, words_prev, xh)
        if k == 0:
            st = C.ClassState.init(tiles)
            tiles, st, _ = C.classify_first(st, tiles)
        else:
            tiles, st, _ = C.classify_tiles(st, tiles, eq)
        h.update(np.ascontiguousarray(tiles).tobytes())
        words_prev, xh = padded, tiles["xh64"]
    return h.hexdigest(), vframe.frames_sha256(vframe_sha(f) for f in frames)


def vframe_sha(rgb):
    return hashlib.sha256(rgb.tobytes()).hexdigest()


class A4EngineFree(Base):
    """Same frames through different adapters -> identical vision output (SPEC_ARCH §10 test_engine_free)."""

    def test_gui_vs_synth_adapter(self):
        from vadapt.synth import SynthAdapter, render_scenario
        for name in ("flicker", "scroll", "frozen_subregion"):
            fr, _, _ = render_scenario(name, 1, 256, 160, 16)
            run = self.d / name
            (run / "screens").mkdir(parents=True)
            for k, f in enumerate(fr):
                (run / "screens" / ("%03d.png" % k)).write_bytes(encode_png(f, filt=(-1 if name == "scroll" else 0)))
            sy = SynthAdapter()
            sy.write_run(self.d / ("syn_" + name), name, 1, 256, 160, 16)
            gui = [f.rgb for f in self.a.frames(run)]
            syn = [f.rgb for f in sy.frames(self.d / ("syn_" + name))]
            self.assertEqual(run_vision(gui), run_vision(syn), name)

    def test_gui_vs_godot_adapter(self):
        try:
            from vadapt.godot import GodotAdapter
            from tests.test_vadapt_godot import make_capture
        except ImportError as e:
            self.skipTest("godot adapter unavailable: %s" % e)
        cap = make_capture(self.d / "g", n=6)
        godot = [f.rgb for f in GodotAdapter().frames(cap)]
        gui_run = make_run(self.d / "u", n=6, steps=[{"file": "s%d.png" % k, "step": 2 * k} for k in range(6)])
        gui = [f.rgb for f in self.a.frames(gui_run)]
        self.assertEqual(run_vision(godot), run_vision(gui))
        self.assertNotEqual(run_vision(godot)[0], run_vision(gui[:-1] + [shot(9)])[0])  # control: output is sensitive

    def test_hints_do_not_change_pixels_or_manifest_hash(self):
        a = make_run(self.d / "a", n=3)
        b = make_run(self.d / "b", n=3, a11y=[{"step": 1, "name": "x", "bbox": [0, 0, 16, 16]}])
        self.assertEqual(run_vision([f.rgb for f in self.a.frames(a)]), run_vision([f.rgb for f in self.a.frames(b)]))


if __name__ == "__main__":
    unittest.main()
