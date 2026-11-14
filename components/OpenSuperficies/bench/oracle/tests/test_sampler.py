import json
import os
import stat
import tempfile
import unittest

from bench.oracle import sampler as S

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = json.load(open(os.path.join(HERE, "fixtures", "oracle_snapshot_basic.json")))
AX = json.load(open(os.path.join(HERE, "fixtures", "oracle_ax_focus.json")))
URL = "file:///opt/cu/scenes/v3/wl1s_small.html"
ALLOWED = {"Target.setAutoAttach", "Target.getTargets", "Accessibility.enable", "Accessibility.getFullAXTree", "DOMSnapshot.enable",
           "DOMSnapshot.captureSnapshot", "PerformanceTimeline.enable", "Page.enable", "Page.getFrameTree",
           "Page.setLifecycleEventsEnabled", "Page.navigate"}


class FakeProxy:
    """In-memory proxy: records the requests, answers canned CDP frames."""

    def __init__(self, shifts=()):
        self.log, self.out, self.rd = [], b"", S.FrameReader()
        self.shifts = list(shifts)

    def write(self, b):
        for m in self.rd.feed(b):
            self.log.append(m)
            self._answer(m)
        return len(b)

    def _put(self, o):
        self.out += S.frame_encode(o)

    def _answer(self, m):
        meth, i = m["method"], m["id"]
        res = {}
        if meth == "DOMSnapshot.captureSnapshot":
            res = SNAP
        elif meth == "Accessibility.getFullAXTree":
            res = AX
        self._put({"id": i, "result": res, **({"sessionId": m["sessionId"]} if "sessionId" in m else {})})
        if meth == "Target.setAutoAttach":
            self._put({"method": "Target.attachedToTarget", "params": {"sessionId": "S1", "waitingForDebugger": False,
                       "targetInfo": {"targetId": "T1", "type": "page", "url": URL}}})
        if meth == "DOMSnapshot.captureSnapshot":
            shifts, self.shifts = self.shifts, []
            for s in shifts:
                self._put({"method": "PerformanceTimeline.timelineEventAdded", "sessionId": "S1", "params": {"event": {
                    "type": "layout-shift", "layoutShiftDetails": {"value": 0.1, "hadRecentInput": s[2],
                    "sources": [{"previousRect": s[0], "currentRect": s[1], "nodeId": 5}]}}}})

    def read(self, n=65536):
        b, self.out = self.out[:n], self.out[n:]
        return b


def mk(proxy, t=[0]):
    return S.Sampler(proxy, lambda: 5_000_000_000 + 33_333_000, 5_000_000_000 - 1_000_000_000 + 1_000_000_000, URL)


class Framing(unittest.TestCase):
    def test_split_at_every_offset(self):
        whole = S.frame_encode({"id": 1, "method": "Page.enable"}) + S.frame_encode({"id": 2, "method": "Page.getFrameTree"})
        want = S.FrameReader().feed(whole)
        self.assertEqual(len(want), 2)
        for k in range(len(whole) + 1):
            r = S.FrameReader()
            got = r.feed(whole[:k]) + r.feed(whole[k:])
            self.assertEqual(got, want, k)

    def test_oversize_refused(self):
        r = S.FrameReader(limit=16)
        with self.assertRaises(ValueError):
            r.feed(b"x" * 17)
        r = S.FrameReader(limit=16)
        self.assertEqual(r.feed(b'{"a":1}\0'), [{"a": 1}])

    def test_encode_nul_terminated(self):
        self.assertTrue(S.frame_encode({"a": 1}).endswith(b"\0"))
        self.assertEqual(S.frame_encode({"a": 1}).count(b"\0"), 1)


class Start(unittest.TestCase):
    def _w(self, td, body=None, name="START"):
        p = os.path.join(td, name)
        with open(p, "w") as f:
            f.write(body if body is not None else json.dumps({"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": 7000000000, "w": 1920, "h": 1080}))
        return p

    def test_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self._w(td)
            self.assertEqual(S.read_start(td)["t0_ns"], 7000000000)

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                S.read_start(td)  # missing
            p = self._w(td, name="real")
            os.symlink(p, os.path.join(td, "START"))
            with self.assertRaises(ValueError):
                S.read_start(td)  # symlink
            os.unlink(os.path.join(td, "START"))
            os.link(p, os.path.join(td, "START"))
            with self.assertRaises(ValueError):
                S.read_start(td)  # nlink 2
        for body in ("{", json.dumps({"schema": "x", "clock": "CLOCK_MONOTONIC", "t0_ns": 1}),
                     json.dumps({"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_REALTIME", "t0_ns": 1}),
                     json.dumps({"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": 1.5})):
            with tempfile.TemporaryDirectory() as td:
                self._w(td, body)
                with self.assertRaises(ValueError):
                    S.read_start(td)

    def test_fifo_refused(self):
        with tempfile.TemporaryDirectory() as td:
            os.mkfifo(os.path.join(td, "START"))
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_t_us(self):
        self.assertEqual(S.t_us_of(7_001_319_999, 7_000_000_000), 1319)
        self.assertEqual(S.t_us_of(7_000_000_000, 7_000_000_000), 0)


class Sampling(unittest.TestCase):
    def test_setup_exact_params_and_allowlist(self):
        p = FakeProxy()
        s = S.Sampler(p, lambda: 6_000_000_000, 5_000_000_000, URL)
        s.setup()
        row = s.sample(1)
        meths = [m["method"] for m in p.log]
        self.assertTrue(set(meths) <= ALLOWED, set(meths) - ALLOWED)
        self.assertFalse([m for m in meths if m.split(".")[0] in ("Input", "Runtime", "Emulation", "Network", "Fetch", "Debugger")])
        sa = [m for m in p.log if m["method"] == "Target.setAutoAttach"][0]
        self.assertEqual(sa["params"], {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        pt = [m for m in p.log if m["method"] == "PerformanceTimeline.enable"][0]
        self.assertEqual(pt["params"], {"eventTypes": ["layout-shift"]})
        cs = [m for m in p.log if m["method"] == "DOMSnapshot.captureSnapshot"][0]
        self.assertEqual(cs["sessionId"], "S1")
        self.assertEqual(cs["params"]["computedStyles"], ["visibility", "opacity", "display"])
        self.assertTrue(all(set(m) <= {"id", "method", "params", "sessionId"} for m in p.log))
        self.assertEqual(row["t_us"], 1_000_000)
        self.assertNotIn("Page.navigate", meths)
        self.assertNotIn("Page.captureScreenshot", meths)

    def test_row_integer_only_and_values(self):
        p = FakeProxy()
        s = S.Sampler(p, lambda: 6_000_000_000, 5_000_000_000, URL)
        s.setup()
        row = s.sample(3)

        def walk(v):
            if isinstance(v, dict):
                [walk(x) for x in v.values()]
            elif isinstance(v, list):
                [walk(x) for x in v]
            else:
                self.assertNotIsInstance(v, float)
        walk(row)
        self.assertEqual((row["seq"], row["sx"], row["sy"], row["frac"], row["focus"]), (3, 0, 64, 0, "7"))
        self.assertEqual(sorted(row), sorted(["seq", "t_us", "snap", "nodes", "sx", "sy", "focus", "shifts", "frac", "canv"]))
        nodes = {n[0]: n for n in row["nodes"]}
        self.assertEqual(nodes[4][1:5], [100, 200, 640, 480])
        self.assertEqual(nodes[4][5], 1)
        self.assertEqual(nodes[6][5], 0)  # hidden/opacity 0/display none
        self.assertEqual(row["canv"], [6])
        self.assertEqual(len(row["snap"]), 16)
        self.assertLess(len(json.dumps(row)), 2048)

    def test_unchanged_snapshot_omits_nodes(self):
        p = FakeProxy()
        s = S.Sampler(p, lambda: 6_000_000_000, 5_000_000_000, URL)
        s.setup()
        r1, r2 = s.sample(1), s.sample(2)
        self.assertIsNotNone(r1["nodes"])
        self.assertIsNone(r2["nodes"])
        self.assertEqual(r1["snap"], r2["snap"])

    def test_layout_shift_collected(self):
        p = FakeProxy(shifts=[({"x": 0, "y": 100, "width": 50, "height": 50}, {"x": 0, "y": 130, "width": 50, "height": 50}, False)])
        s = S.Sampler(p, lambda: 6_000_000_000, 5_000_000_000, URL)
        s.setup()
        row = s.sample(1)
        self.assertEqual(row["shifts"], [{"n": 5, "p": [0, 100, 50, 50], "c": [0, 130, 50, 50], "ri": 0}])
        self.assertEqual(s.sample(2)["shifts"], [])

    def test_fractional_bounds_flag(self):
        snap = json.loads(json.dumps(SNAP))
        snap["documents"][0]["layout"]["bounds"][2] = [100.5, 200, 640, 480]
        row = S.row_from_snapshot(snap, AX, [], 1, 0)
        self.assertEqual(row["frac"], 1)
        self.assertEqual({n[0]: n for n in row["nodes"]}[4][1], 100)

    def test_no_forbidden_literals_in_source(self):
        src = open(os.path.join(os.path.dirname(HERE), "sampler.py")).read()
        for bad in ("Input.", "Runtime.", "captureScreenshot", "startScreencast", "remote-debugging-port", "socket"):
            self.assertNotIn(bad, src.replace("# no Input./Runtime./socket", ""))

class Fx1(unittest.TestCase):
    """FX1 (ADC-W6A-03, ADC-W6A-06)."""

    def _w(self, td, **kv):
        d = {"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": 7000000000}
        d.update(kv)
        with open(os.path.join(td, "START"), "w") as f:
            json.dump(d, f)

    def test_start_t0_must_be_positive(self):
        for t0 in (0, -1):
            with tempfile.TemporaryDirectory() as td:
                self._w(td, t0_ns=t0)
                with self.assertRaises(ValueError):
                    S.read_start(td)

    def test_start_run_nonce_checked(self):
        for bad in ("NOT_HEX", "A" * 32, "a" * 31, "a" * 33, 7, None):
            with tempfile.TemporaryDirectory() as td:
                self._w(td, run_nonce=bad)
                with self.assertRaises(ValueError, msg=repr(bad)):
                    S.read_start(td)
        with tempfile.TemporaryDirectory() as td:
            self._w(td, run_nonce="0123456789abcdef0123456789abcdef")
            self.assertEqual(S.read_start(td)["t0_ns"], 7000000000)

    def test_default_tracks_only_prereg_ids_and_row_is_small(self):
        strs = ["#document", "HTML", "BODY", "DIV", "id", "box", "class", "r", "visibility", "opacity", "display", "visible", "1", "block"]
        n = 2 + 400 + 1
        attrs = [[], []] + [[6, 7]] * 400 + [[4, 5]]
        bounds = [[0, 0, 1920, 16000]] * 2 + [[0, 22 * i - 40, 1920, 22] for i in range(400)] + [[100, 200, 80, 80]]
        snap = {"documents": [{"layout": {"bounds": bounds, "nodeIndex": list(range(n)), "styles": [[11, 12, 13]] * n},
                               "nodes": {"attributes": attrs, "backendNodeId": list(range(1, n + 1)), "nodeName": [0, 1] + [3] * 401,
                                         "nodeType": [9] + [1] * (n - 1), "nodeValue": [-1] * n, "parentIndex": [-1] * n},
                               "scrollOffsetX": 0, "scrollOffsetY": 0}], "strings": strs}
        row = S.row_from_snapshot(snap, {"nodes": []}, [], 1, 0, None, S.DEFAULT_TRACK_IDS)
        self.assertEqual([x[0] for x in row["nodes"]], [n])  # only id=box survives
        self.assertLess(len(json.dumps(row)), 1024)
        full = S.row_from_snapshot(snap, {"nodes": []}, [], 1, 0, None, None)
        self.assertGreater(len(json.dumps(full)), 19000)  # the unrestricted row is the ADC-03 blow-up

    def test_sampler_defaults_to_tracked_ids(self):
        p = FakeProxy()
        s = S.Sampler(p, lambda: 6_000_000_000, 5_000_000_000, URL)
        self.assertEqual(s.track_ids, S.DEFAULT_TRACK_IDS)
        self.assertEqual(set(S.DEFAULT_TRACK_IDS), {"box", "cv", "caret", "f1"})



class Fx2(unittest.TestCase):
    """W6a FX2: ADC2-W6A-05 (START duplicate keys) and RD6A-02 (rows cap headroom)."""

    def test_start_duplicate_keys_refused(self):
        raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":-1,"t0_ns":1000}'
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "START"), "w") as f:
                f.write(raw)
            with self.assertRaises(ValueError):
                S.read_start(td)
            with open(os.path.join(td, "START"), "w") as f:
                f.write(raw.replace('"t0_ns":-1,', ""))
            self.assertEqual(S.read_start(td)["t0_ns"], 1000)

    def test_rows_cap_headroom_400_rows_of_300_bytes(self):
        sink = S.RowSink()
        rows = [{"seq": i, "pad": "x" * (300 - len('{"pad":"","seq":%d}' % i) - 1)} for i in range(400)]
        self.assertTrue(all(S.row_bytes(r) == 300 for r in rows))
        kept = 0
        for r in rows:  # sample_loop: stop at the first row that would cross the cap
            if not sink.add(r):
                break
            kept += 1
        self.assertTrue(sink.truncated)
        self.assertEqual(kept, S.ROWS_CAP // 300)
        self.assertEqual(len(sink.rows), kept)
        with tempfile.TemporaryDirectory() as td:
            n = S.write_rows(os.path.join(td, "rows.jsonl"), sink.rows)  # never raises (was rc 5 with 400 x 300 B = 120,000 B)
        self.assertLessEqual(n, S.ROWS_CAP)
        self.assertEqual(n, sink.size)
        small = S.RowSink()
        self.assertTrue(all(small.add({"seq": i}) for i in range(400)))
        self.assertFalse(small.truncated)


if __name__ == "__main__":
    unittest.main()
