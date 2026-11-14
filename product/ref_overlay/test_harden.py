#!/usr/bin/env python3
"""Hardening tests for compose.py (w186 cell, lane sw-overlay-projection).

  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G \
      -p MemorySwapMax=0 python3 -m unittest -v test_harden

Every test here failed against the F2 reference (commit 41ab955) and each
group carries a negative control: a seam that puts the old behaviour back and
must make the assertion go red.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compose  # noqa: E402
from test_compose import (START_MS, TOL_COLOUR, dist, read_ppm, rgb,  # noqa: E402
                          run)

FONT = next((p for p in compose.FONT_CANDIDATES if os.path.exists(p)), None)
BLUE = rgb(compose.AGENT_COLOURS["modeler-agent"])


def cur(agent, phase, t, x=100, y=100, **kw):
    r = {"v": 1, "rec": "cursor", "agent_id": agent, "phase": phase,
         "x": x, "y": y, "event_id": "%s-1" % (agent,),
         "ts_ms": START_MS + int(t * 1000)}
    r.update(kw)
    return r


def plan(c, e, dur=20.0, **kw):
    return compose.build_plan(c, e, START_MS, dur, 1920, 1080, **kw)


def small_video(path, dur=4, extra=()):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "color=c=gray:size=640x360:rate=30:duration=%d"
         % dur, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
         "-pix_fmt", "yuv420p"] + list(extra) + [path])


def frame_n(video, n, tmpdir):
    """Frame by index (independent of container start_time)."""
    out = os.path.join(tmpdir, "n_%s_%d.ppm" % (os.path.basename(video), n))
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", video, "-vf", "select=eq(n\\,%d)" % n, "-frames:v", "1",
         "-c:v", "ppm", "-f", "image2", out])
    w, h, buf = read_ppm(out)
    return lambda x, y: tuple(buf[(y * w + x) * 3:(y * w + x) * 3 + 3])


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class HostileLogs(unittest.TestCase):
    """json.loads accepts NaN/Infinity and any JSON type in any field."""

    def test_non_finite_and_wrong_types_are_skipped_not_fatal(self):
        nan, inf = float("nan"), float("inf")
        bad_cursors = [
            cur("modeler-agent", "act", 1, x=nan),
            cur("modeler-agent", "act", 1, y=inf),
            cur("modeler-agent", "act", 1, ts_ms=inf),
            cur("modeler-agent", "act", 1, ts_ms=nan),
            cur("modeler-agent", "act", 1, ts_ms=10 ** 400),
            cur("modeler-agent", "act", 1, x=10 ** 400),
            cur(["modeler-agent"], "act", 1),
            cur({"a": 1}, "act", 1),
            cur("modeler-agent", ["act"], 1),
            cur("modeler-agent", {"p": 1}, 1),
        ]
        events = [
            {"v": 1, "rec": "enqueue", "event_id": ["x"], "agent_id": "look-agent"},
            {"v": 1, "rec": "enqueue", "event_id": {"x": 1}, "agent_id": ["y"]},
            {"v": 1, "rec": "turn", "agent_id": ["look-agent"], "ts_ms": START_MS},
            {"v": 1, "rec": "turn", "agent_id": "look-agent", "frame": inf},
            {"v": 1, "rec": "turn", "agent_id": "look-agent", "frame": nan},
            {"v": 1, "rec": "close", "event_id": ["x"], "verdict": "VERIFIED",
             "ts_ms": START_MS},
            {"v": 1, "rec": "close", "event_id": {"x": 1}, "agent_id": {"k": 1},
             "verdict": "VERIFIED", "ts_ms": START_MS},
            {"v": 1, "rec": "control", "op": ["stop"], "ts_ms": nan},
        ]
        good = cur("modeler-agent", "act", 2, x=300, y=300)
        p = plan(bad_cursors + [good], events, frame_ts={1: START_MS})
        cursors = [i for i in p["items"] if i["type"] == "cursor"]
        self.assertEqual([(i["x"], i["y"]) for i in cursors], [(300, 300)])
        self.assertEqual([i for i in p["items"] if i["type"] != "cursor"], [])
        self.assertGreaterEqual(p["stats"]["skipped_invalid"], 9)
        compose.build_filter(p, FONT or "/nonexistent.ttf")  # must not raise

    def test_round_trip_through_json_text(self):
        # the same hostile values as they really arrive: as JSONL text
        d = tempfile.mkdtemp(prefix="vp-f2-h-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "cursor.jsonl")
        with open(path, "w") as fh:
            fh.write('{"rec":"cursor","agent_id":"modeler-agent","phase":"act",'
                     '"x":NaN,"y":Infinity,"ts_ms":%d}\n' % START_MS)
            fh.write('{"rec":"cursor","agent_id":"modeler-agent","phase":"act",'
                     '"x":1e999,"y":5,"ts_ms":-Infinity}\n')
        recs, bad = compose.read_jsonl(path)
        self.assertEqual((len(recs), bad), (2, 0))
        p = plan(recs, [])
        self.assertEqual(p["items"], [])

    def test_agent_flood_cannot_break_the_strip(self):
        # 300 agent ids from the log used to shrink slots to negative widths
        c = [cur("bot-%03d" % i, "act", 1 + i * 0.01) for i in range(300)]
        p = plan(c, [])
        self.assertLessEqual(len(p["ring"]), compose.MAX_RING)
        self.assertEqual(p["ring"][:4], compose.RING_DEFAULT)
        for a in p["ring"]:
            self.assertGreater(p["geo"]["slots"][a]["w"], 40)
            self.assertGreater(p["geo"]["slots"][a]["badge"][2], 0)
        self.assertEqual(p["stats"]["agents_dropped"], 300 - (compose.MAX_RING - 4))

    def test_duplicate_ring_names_collapse(self):
        p = plan([], [], ring=["a", "b", "a", "", "b"])
        self.assertEqual(p["ring"], ["a", "b"])


class UnicodeLabels(unittest.TestCase):
    def setUp(self):
        if FONT is None or not shutil.which("fc-query"):
            self.skipTest("needs DejaVu and fc-query")
        compose.set_font(FONT)
        self.addCleanup(compose.set_font, None)

    def test_letters_the_font_can_draw_are_kept(self):
        self.assertEqual(compose.short_name("modélisateur-agent"), "modélisateur")
        self.assertEqual(compose.short_name("камера-agent"), "камера")
        # NFD input (e + combining acute) is composed, not turned into "e_"
        self.assertEqual(compose.short_name("modél-agent"), "modél")

    def test_letters_without_a_glyph_become_underscores(self):
        # DejaVu Sans Bold has no CJK: tofu boxes would be unreadable noise
        self.assertEqual(compose.short_name("模型-agent"), "__")

    def test_dangerous_characters_never_pass(self):
        hostile = "a'b:c,d;e[f]g\\h%i{j}k=l\nm‮n\ud800o\x00p"
        out = compose.safe_text(hostile, 80)
        self.assertEqual(out, "a_b_c_d_e_f_g_h_i_j_k_l_m_n_o_p")
        out.encode("utf-8")  # a lone surrogate would raise here

    def test_filter_script_with_unicode_agent_runs_in_ffmpeg(self):
        d = tempfile.mkdtemp(prefix="vp-f2-h-")
        self.addCleanup(shutil.rmtree, d, True)
        v, o = os.path.join(d, "in.mp4"), os.path.join(d, "out.mp4")
        small_video(v, 2)
        with open(os.path.join(d, "cursor.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cur("modélisateur-agent", "act", 0.5, 200, 100),
                                ensure_ascii=False) + "\n")
            fh.write(json.dumps(cur("x'%{pts}\\:é", "act", 0.5, 300, 100)) + "\n")
        fs = os.path.join(d, "f.ffscript")
        _, summary = compose.compose(v, o, os.path.join(d, "cursor.jsonl"),
                                     os.path.join(d, "none.jsonl"), START_MS,
                                     keep_filter=fs)
        self.assertEqual(summary["cursor_drawn"], 2)
        with open(fs, encoding="utf-8") as fh:
            script = fh.read()
        self.assertIn("text='modélisateur'", script)
        self.assertNotIn("%{", script)

    def test_negative_control_without_charset_falls_back_to_ascii(self):
        compose.set_font(None)
        self.assertEqual(compose.short_name("modélisateur-agent"), "mod_lisateur")


class Windows(unittest.TestCase):
    """enable windows are half-open: [a, b)."""

    def test_filter_uses_half_open_windows(self):
        p = plan([cur("modeler-agent", "act", 1.0), cur("modeler-agent", "act", 2.0)], [])
        script = compose.build_filter(p, FONT or "/x.ttf")
        self.assertNotIn("between(", script)
        self.assertIn("enable='gte(t,1.000)*lt(t,2.000)'", script)

    def _two_acts(self, half_open):
        d = tempfile.mkdtemp(prefix="vp-f2-h-")
        self.addCleanup(shutil.rmtree, d, True)
        v, o = os.path.join(d, "in.mp4"), os.path.join(d, "out.mp4")
        small_video(v, 4)
        with open(os.path.join(d, "cursor.jsonl"), "w") as fh:
            fh.write(json.dumps(cur("modeler-agent", "act", 1.0, 100, 100)) + "\n")
            fh.write(json.dumps(cur("modeler-agent", "act", 2.0, 400, 100)) + "\n")
        old = compose.HALF_OPEN
        compose.HALF_OPEN = half_open
        try:
            compose.compose(v, o, os.path.join(d, "cursor.jsonl"),
                            os.path.join(d, "none.jsonl"), START_MS)
        finally:
            compose.HALF_OPEN = old
        return frame_n(o, 59, d), frame_n(o, 60, d)

    def test_one_cursor_per_agent_on_the_boundary_frame(self):
        before, at = self._two_acts(True)
        self.assertLess(dist(before(100, 100), BLUE), TOL_COLOUR)
        self.assertGreater(dist(before(400, 100), BLUE), TOL_COLOUR)
        # frame 60 is t == 2.000 exactly: only the new position may be blue
        self.assertLess(dist(at(400, 100), BLUE), TOL_COLOUR)
        self.assertGreater(dist(at(100, 100), BLUE), TOL_COLOUR,
                           "two cursors of one agent on the same frame")

    def test_negative_control_closed_windows_show_two_cursors(self):
        _, at = self._two_acts(False)
        self.assertLess(dist(at(100, 100), BLUE), TOL_COLOUR)
        self.assertLess(dist(at(400, 100), BLUE), TOL_COLOUR)


class StartTime(unittest.TestCase):
    """A container whose first frame is not at pts 0 (cut or remuxed take).

    Hypothesis tested and refuted: "the filter's t includes start_time, so
    the overlay must add it". The ffmpeg CLI rebases t to 0; this pins it."""

    def _run(self, add_start):
        d = tempfile.mkdtemp(prefix="vp-f2-h-")
        self.addCleanup(shutil.rmtree, d, True)
        v0, v = os.path.join(d, "in.mp4"), os.path.join(d, "shift.mkv")
        o = os.path.join(d, "out.mkv")
        small_video(v0, 4)
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", v0, "-c", "copy", "-output_ts_offset", "3", v])
        info = compose.probe_info(v)
        self.assertAlmostEqual(info["start"], 3.0, delta=0.05)
        with open(os.path.join(d, "cursor.jsonl"), "w") as fh:
            fh.write(json.dumps(cur("modeler-agent", "act", 1.0, 100, 100)) + "\n")
        old = compose.ADD_START_TIME
        compose.ADD_START_TIME = add_start
        try:
            compose.compose(v, o, os.path.join(d, "cursor.jsonl"),
                            os.path.join(d, "none.jsonl"), START_MS)
        finally:
            compose.ADD_START_TIME = old
        return frame_n(o, 20, d), frame_n(o, 45, d)

    def test_overlay_follows_the_first_frame_of_a_shifted_container(self):
        # start_time is 3 s, the record is 1 s after the first frame: the
        # cursor must be on frame 45 (1.5 s), not 3 s later.
        early, mid = self._run(False)
        self.assertGreater(dist(early(100, 100), BLUE), TOL_COLOUR)
        self.assertLess(dist(mid(100, 100), BLUE), TOL_COLOUR)

    def test_negative_control_adding_start_time_misses(self):
        _, mid = self._run(True)
        self.assertGreater(dist(mid(100, 100), BLUE), TOL_COLOUR)


class Files(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vp-f2-h-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.v = os.path.join(self.d, "in.mp4")
        small_video(self.v, 1)
        self.none = os.path.join(self.d, "none.jsonl")

    def test_output_equal_to_input_is_refused(self):
        before = sha(self.v)
        link = os.path.join(self.d, "alias.mp4")
        os.symlink(self.v, link)
        for out in (self.v, link):
            with self.assertRaises(SystemExit) as cm:
                compose.compose(self.v, out, self.none, self.none, START_MS)
            self.assertIn("same file", str(cm.exception))
        self.assertEqual(sha(self.v), before)

    def test_failed_encode_keeps_the_previous_output(self):
        out = os.path.join(self.d, "out.mp4")
        with open(out, "w") as fh:
            fh.write("KEEP")
        with self.assertRaises(SystemExit):
            compose.compose(self.v, out, self.none, self.none, START_MS,
                            preset="no-such-preset")
        with open(out) as fh:
            self.assertEqual(fh.read(), "KEEP")
        self.assertEqual(sorted(os.listdir(self.d)), ["in.mp4", "out.mp4"])

    def test_bad_start_values_are_clean_errors(self):
        sf = os.path.join(self.d, "s.start_ms")
        for text in ("", "abc", "nan", "inf", "-5"):
            with open(sf, "w") as fh:
                fh.write(text)
            with self.assertRaises(SystemExit) as cm:
                compose.main(["--video", self.v, "--out",
                              os.path.join(self.d, "o.mp4"), "--rt", self.d,
                              "--start-file", sf])
            self.assertIn("start", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.d, "o.mp4")))

    def test_not_a_video_is_a_clean_error(self):
        junk = os.path.join(self.d, "junk.mp4")
        with open(junk, "w") as fh:
            fh.write("not a video")
        with self.assertRaises(SystemExit) as cm:
            compose.compose(junk, os.path.join(self.d, "o.mp4"), self.none,
                            self.none, START_MS)
        self.assertIn("ffprobe", str(cm.exception))

    def test_offset_ms_shifts_every_record(self):
        p0 = plan([cur("modeler-agent", "act", 1.0)], [])
        p1 = compose.build_plan([cur("modeler-agent", "act", 1.0)], [],
                                START_MS + 250, 20.0, 1920, 1080)
        self.assertAlmostEqual(p0["items"][0]["a"] - p1["items"][0]["a"], 0.25)


if __name__ == "__main__":
    unittest.main()
