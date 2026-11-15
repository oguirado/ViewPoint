#!/usr/bin/env python3
"""Tests for compose.py. Run from this directory:

  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G \
      -p MemorySwapMax=0 python3 -m unittest -v test_compose

Pixel tests build a synthetic 20 s 1920x1080 testsrc video, compose it with a
4-agent log and sample frames as PPM (stdlib parser).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compose  # noqa: E402

START_MS = 1_800_000_000_000
TOL_COLOUR = 48     # agent colour after two yuv420 x264 generations
TOL_UNCHANGED = 40  # per-channel drift allowed on untouched pixels
MEAN_UNCHANGED = 6.0

AGENTS = compose.RING_DEFAULT
# agent -> (intent t, act t, x, y, verdict)
SCRIPT = {
    "modeler-agent": (1.0, 3.0, 400, 300, "VERIFIED"),
    "props-agent": (5.0, 7.0, 1500, 300, "CLAIM_REFUSED"),
    "look-agent": (9.0, 11.0, 1500, 700, "JOB_ERROR"),
    "camera-agent": (13.0, 15.0, 200, 700, "VERIFIED"),
}
OUTSIDE_XY = (200, 500)  # where the out-of-window records point


def rgb(hexs):
    return tuple(int(hexs[i:i + 2], 16) for i in (0, 2, 4))


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("%s\n%s" % (" ".join(cmd), r.stderr[-1500:]))
    return r


def make_video(path, w, h, dur):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "testsrc=size=%dx%d:rate=30:duration=%d"
         % (w, h, dur), "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "16", "-pix_fmt", "yuv420p", path])


def read_ppm(path):
    with open(path, "rb") as fh:
        data = fh.read()
    toks, pos = [], 0
    while len(toks) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        toks.append(data[pos:end])
        pos = end
    assert toks[0] == b"P6" and toks[3] == b"255", toks
    return int(toks[1]), int(toks[2]), data[pos + 1:]


class Frame:
    def __init__(self, video, t, tmpdir):
        out = os.path.join(tmpdir, "f_%s_%07.3f.ppm"
                           % (os.path.basename(video), t))
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-y", "-ss", "%.3f" % t, "-i", video, "-frames:v", "1",
             "-c:v", "ppm", "-f", "image2", out])
        self.w, self.h, self.buf = read_ppm(out)

    def px(self, x, y):
        i = (y * self.w + x) * 3
        return tuple(self.buf[i:i + 3])


def dist(a, b):
    return max(abs(a[i] - b[i]) for i in range(3))


def write_logs(d, out_of_window=True, extra_cursor=()):
    cur, ev = [], []
    for n, a in enumerate(AGENTS):
        ti, ta, x, y, verdict = SCRIPT[a]
        eid = "%s-1" % a
        ev.append({"v": 1, "rec": "enqueue", "event_id": eid, "agent_id": a,
                   "lane": "script", "kind": "job", "based_on_frame": n,
                   "write_set": ["col:X"], "script": "pass", "expect": "x",
                   "ts_ms": START_MS + int(ti * 1000)})
        cur.append({"v": 1, "rec": "cursor", "agent_id": a, "event_id": eid,
                    "phase": "intent", "x": x, "y": y, "frame": n,
                    "ts_ms": START_MS + int(ti * 1000)})
        ev.append({"v": 1, "rec": "turn", "turn": n + 1, "turn_epoch": n + 1,
                   "agent_id": a, "head_frame": n, "hold_ms": 60000,
                   "ts_ms": START_MS + int((ta - 0.5) * 1000)})
        cur.append({"v": 1, "rec": "cursor", "agent_id": a, "event_id": eid,
                    "phase": "act", "x": x, "y": y, "frame": n,
                    "ts_ms": START_MS + int(ta * 1000)})
        ev.append({"v": 1, "rec": "close", "tick": n + 1, "turn": n + 1,
                   "event_id": eid, "verdict": verdict,
                   "ts_ms": START_MS + int((ta + 1.0) * 1000)})
    if out_of_window:
        ox, oy = OUTSIDE_XY
        for ts in (START_MS - 1000, START_MS + 25000):
            cur.append({"v": 1, "rec": "cursor", "agent_id": "camera-agent",
                        "event_id": "camera-agent-9", "phase": "act",
                        "x": ox, "y": oy, "frame": 99, "ts_ms": ts})
            ev.append({"v": 1, "rec": "close", "tick": 99, "turn": 99,
                       "event_id": "look-agent-9", "verdict": "VERIFIED",
                       "ts_ms": ts})
    cur.extend(extra_cursor)
    cpath, epath = os.path.join(d, "cursor.jsonl"), os.path.join(d, "events.jsonl")
    with open(cpath, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in cur))
        fh.write("{not json\n")  # a torn line must be skipped, not fatal
    with open(epath, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in ev))
    return cpath, epath


class PixelTests(unittest.TestCase):
    """20 s 1920x1080, 4 agents, one composite shared by all tests."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ref_overlay_test_")
        cls.src = os.path.join(cls.tmp, "src.mp4")
        cls.out = os.path.join(cls.tmp, "out.mp4")
        make_video(cls.src, 1920, 1080, 20)
        cpath, epath = write_logs(cls.tmp)
        cls.plan, cls.summary = compose.compose(
            cls.src, cls.out, cpath, epath, START_MS)
        cls._frames = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def frame(self, video, t):
        key = (video, t)
        if key not in self._frames:
            self._frames[key] = Frame(video, t, self.tmp)
        return self._frames[key]

    def test_output_geometry(self):
        w, h, dur = compose.probe(self.out)
        self.assertEqual((w, h), (1920, 1080))
        self.assertAlmostEqual(dur, 20.0, delta=0.1)
        self.assertEqual(self.summary["cursor_drawn"], 8)
        self.assertEqual(self.summary["turn_drawn"], 4)
        self.assertEqual(self.summary["badge_drawn"], 4)
        self.assertEqual(self.summary["bad_json_lines"], 1)

    def test_solid_cursor_colour_at_act_time(self):
        for a in AGENTS:
            ti, ta, x, y, _ = SCRIPT[a]
            want = rgb(compose.AGENT_COLOURS[a])
            got = self.frame(self.out, ta + 0.5).px(x, y)
            self.assertLessEqual(dist(got, want), TOL_COLOUR, (a, got, want))
            # and NOT there before the intent / after the act dwell
            for t in (ti - 0.5, ta + compose.DWELL_S["act"] + 0.5):
                got = self.frame(self.out, t).px(x, y)
                ref = self.frame(self.src, t).px(x, y)
                self.assertLessEqual(dist(got, ref), TOL_UNCHANGED,
                                     (a, t, got, ref))

    def test_ghost_cursor_is_hollow_at_intent_time(self):
        half = self.plan["geo"]["cur"] // 2
        for a in AGENTS:
            ti, _, x, y, _ = SCRIPT[a]
            want = rgb(compose.AGENT_COLOURS[a])
            f = self.frame(self.out, ti + 0.5)
            ref = self.frame(self.src, ti + 0.5)
            edge = f.px(x - half + 1, y)  # on the 3 px border
            self.assertLessEqual(dist(edge, want), TOL_COLOUR + 16, (a, edge))
            # centre of a ghost shows the recording, not the agent colour
            self.assertLessEqual(dist(f.px(x, y), ref.px(x, y)),
                                 TOL_UNCHANGED, a)

    def test_agents_do_not_bleed_into_each_other(self):
        # at modeler's act time, the other three cursor spots are untouched
        t = SCRIPT["modeler-agent"][1] + 0.5
        for a in AGENTS[1:]:
            _, _, x, y, _ = SCRIPT[a]
            self.assertLessEqual(
                dist(self.frame(self.out, t).px(x, y),
                     self.frame(self.src, t).px(x, y)), TOL_UNCHANGED, a)

    def test_queue_head_highlight_follows_turns(self):
        slots = self.plan["geo"]["slots"]
        for a in AGENTS:
            ta = SCRIPT[a][1]
            f = self.frame(self.out, ta + 0.5)  # turn opened at ta-0.5
            for b in AGENTS:
                sl = slots[b]
                # low-left corner area of the slot: no text there
                got = f.px(sl["x"] + 12, sl["y"] + sl["h"] - 10)
                want = rgb(compose.AGENT_COLOURS[b])
                if a == b:
                    self.assertLessEqual(dist(got, want), TOL_COLOUR, (a, got))
                else:
                    self.assertGreater(dist(got, want), TOL_COLOUR, (a, b, got))

    def test_verdict_badges(self):
        slots = self.plan["geo"]["slots"]
        expect = {"modeler-agent": compose.GREEN, "props-agent": compose.AMBER,
                  "look-agent": compose.RED, "camera-agent": compose.GREEN}
        for a in AGENTS:
            ta = SCRIPT[a][1]
            bx, by, bw, bh = slots[a]["badge"]
            p = (bx + 5, by + bh // 2)  # left padding of the badge, no text
            got = self.frame(self.out, ta + 1.5).px(*p)
            self.assertLessEqual(dist(got, rgb(expect[a])), TOL_COLOUR,
                                 (a, got))
            gone = self.frame(self.out, ta + 1.0 + compose.BADGE_S + 0.5).px(*p)
            self.assertGreater(dist(gone, rgb(expect[a])), TOL_COLOUR, a)

    def test_unchanged_outside_overlay_regions(self):
        g = self.plan["geo"]
        for t in (0.5, 3.5, 11.5, 19.0):
            f, ref = self.frame(self.out, t), self.frame(self.src, t)
            # overlay regions live at time t: strip + cursors (+ label box)
            boxes = [(0, g["strip_y"], g["W"], g["strip_h"])]
            for it in self.plan["items"]:
                if it["type"] == "cursor" and it["a"] - 0.1 <= t <= it["b"] + 0.1:
                    boxes.append((it["x"] - 40, it["y"] - 40, 260, 80))
            total, n, worst = 0, 0, 0
            for y in range(8, g["H"], 16):
                for x in range(8, g["W"], 16):
                    if any(bx - 8 <= x < bx + bw + 8 and by - 8 <= y < by + bh + 8
                           for bx, by, bw, bh in boxes):
                        continue
                    d = dist(f.px(x, y), ref.px(x, y))
                    total, n, worst = total + d, n + 1, max(worst, d)
            self.assertGreater(n, 6000)
            self.assertLessEqual(worst, TOL_UNCHANGED, (t, worst))
            self.assertLess(total / n, MEAN_UNCHANGED, (t, total / n))

    def test_event_outside_duration_does_not_draw(self):
        # records at -1 s and +25 s point at OUTSIDE_XY; nothing may appear
        self.assertEqual(self.summary["skipped_out_of_window"], 4)
        x, y = OUTSIDE_XY
        yellow = rgb(compose.AGENT_COLOURS["camera-agent"])
        for t in (0.0, 0.5, 1.5, 19.5):
            got = self.frame(self.out, t).px(x, y)
            self.assertLessEqual(
                dist(got, self.frame(self.src, t).px(x, y)), TOL_UNCHANGED, t)
            self.assertGreater(dist(got, yellow), TOL_COLOUR, t)
        # no badge for look-agent-9 at t=0.5 either
        bx, by, bw, bh = self.plan["geo"]["slots"]["look-agent"]["badge"]
        got = self.frame(self.out, 0.5).px(bx + 5, by + bh // 2)
        self.assertGreater(dist(got, rgb(compose.GREEN)), TOL_COLOUR)


class NegativeControl(unittest.TestCase):
    """Remove the window check -> the out-of-window record DOES draw.

    This proves test_event_outside_duration_does_not_draw is not vacuous:
    with clip_to_window=False the same pixel sample turns agent-coloured.
    """

    def test_window_check_removed_goes_red(self):
        tmp = tempfile.mkdtemp(prefix="ref_overlay_neg_")
        try:
            src = os.path.join(tmp, "src.mp4")
            make_video(src, 640, 360, 4)
            cpath = os.path.join(tmp, "cursor.jsonl")
            with open(cpath, "w") as fh:  # act 1 s BEFORE the recording
                fh.write(json.dumps({
                    "v": 1, "rec": "cursor", "agent_id": "camera-agent",
                    "event_id": "camera-agent-9", "phase": "act",
                    "x": 60, "y": 150, "ts_ms": START_MS - 1000}) + "\n")
            epath = os.path.join(tmp, "events.jsonl")
            open(epath, "w").close()
            yellow = rgb(compose.AGENT_COLOURS["camera-agent"])
            seen = {}
            for clip in (True, False):
                out = os.path.join(tmp, "out_%s.mp4" % clip)
                _, summ = compose.compose(src, out, cpath, epath, START_MS,
                                          clip_to_window=clip)
                seen[clip] = (Frame(out, 0.5, tmp).px(60, 150),
                              summ["cursor_drawn"])
            self.assertGreater(dist(seen[True][0], yellow), TOL_COLOUR)
            self.assertEqual(seen[True][1], 0)
            # check removed: the pixel assertion above would fail
            self.assertLessEqual(dist(seen[False][0], yellow), TOL_COLOUR)
            self.assertEqual(seen[False][1], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PlanTests(unittest.TestCase):
    """No ffmpeg: logic of build_plan / build_filter."""

    def plan(self, cur, ev, dur=20.0, **kw):
        return compose.build_plan(cur, ev, START_MS, dur, 1920, 1080, **kw)

    def c(self, agent, phase, t, x=100, y=100, **kw):
        r = {"v": 1, "rec": "cursor", "agent_id": agent, "phase": phase,
             "x": x, "y": y, "event_id": agent + "-1",
             "ts_ms": START_MS + int(t * 1000)}
        r.update(kw)
        return r

    def test_verdict_classes(self):
        self.assertEqual(compose.verdict_colour("VERIFIED"), compose.GREEN)
        self.assertEqual(compose.verdict_colour("STATE_VERIFIED"), compose.GREEN)
        for v in ("STALE_FRAME", "CLAIM_REFUSED", "QUEUE_FULL", "PASS",
                  "TURN_TIMEOUT", "NO_LEASE", "DUPLICATE_EVENT"):
            self.assertEqual(compose.verdict_colour(v), compose.AMBER, v)
        for v in ("JOB_ERROR", "OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN",
                  "SOMETHING_NEW"):
            self.assertEqual(compose.verdict_colour(v), compose.RED, v)

    def test_intent_replaced_by_act(self):
        p = self.plan([self.c("look-agent", "intent", 2.0),
                       self.c("look-agent", "act", 3.0)], [])
        cur = [i for i in p["items"] if i["type"] == "cursor"]
        self.assertEqual([(i["ghost"], i["a"], i["b"]) for i in cur],
                         [(True, 2.0, 3.0), (False, 3.0, 5.0)])

    def test_untimed_record_falls_back_to_frame_ts_or_is_skipped(self):
        rec = self.c("look-agent", "act", 0)
        del rec["ts_ms"]
        rec["frame"] = 44
        p = self.plan([rec], [])
        self.assertEqual(p["stats"]["skipped_untimed"], 1)
        self.assertEqual(p["stats"]["cursor_drawn"], 0)
        p = self.plan([rec], [], frame_ts={44: START_MS + 4000})
        self.assertEqual(p["stats"]["cursor_drawn"], 1)
        self.assertEqual(p["items"][0]["a"], 4.0)

    def test_invalid_records_are_skipped(self):
        p = self.plan([self.c("look-agent", "bogus", 1),
                       self.c("look-agent", "act", 1, x="12"),
                       {"rec": "cursor"}],
                      [{"rec": "close", "event_id": "nobody-1",
                        "verdict": "VERIFIED", "ts_ms": START_MS + 1000}])
        self.assertEqual(p["items"], [])
        self.assertEqual(p["stats"]["skipped_invalid"], 4)

    def test_unknown_agent_joins_ring_with_fallback_colour(self):
        p = self.plan([self.c("rigger-agent", "act", 1)], [])
        self.assertEqual(p["ring"][-1], "rigger-agent")
        self.assertEqual(p["items"][0]["colour"], compose.EXTRA_COLOURS[0])

    def test_cursor_in_both_logs_is_drawn_once(self):
        r = self.c("look-agent", "act", 1)
        p = self.plan([r], [dict(r)])
        self.assertEqual(p["stats"]["cursor_drawn"], 1)

    def test_hostile_text_cannot_break_the_filter_script(self):
        evil = "x':enable=0,drawbox=c=red;[a]\\%{pts}"
        p = self.plan([], [
            {"rec": "close", "event_id": "look-agent-1", "verdict": evil,
             "ts_ms": START_MS + 1000},
            {"rec": "control", "op": evil, "ts_ms": START_MS + 1000},
            {"rec": "turn", "agent_id": "look-agent", "turn": evil,
             "ts_ms": START_MS + 1000}])
        script = compose.build_filter(p, "/font.ttf")
        for line in script.splitlines():
            if "text='" not in line:
                continue
            text = line.split("text='", 1)[1].split("'", 1)[0]
            self.assertRegex(text, r"^[A-Za-z0-9_ .+#-]*$")
        for ch in ";[]\\%":
            self.assertNotIn(ch, script)

    def test_window_clip_plan_level(self):
        recs = [self.c("look-agent", "act", -1.0),
                self.c("look-agent", "act", 20.0),
                self.c("look-agent", "act", 19.5)]
        p = self.plan(recs, [])
        self.assertEqual(p["stats"]["skipped_out_of_window"], 2)
        self.assertEqual(len(p["items"]), 1)
        self.assertLessEqual(p["items"][0]["b"], 20.0)


if __name__ == "__main__":
    unittest.main()
