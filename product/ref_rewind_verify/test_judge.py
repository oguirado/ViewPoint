"""R3 tests: one block per oracle, each with a wrong-edit fixture, plus the wrong-edit scenario through the
real F1 broker + bridge core (R0 fakes) and the must-FAIL controls. Track P (claude-fable-5-2).

    python3 -m unittest -v test_judge          (from this directory)
    RW_JUDGE_PATH=<mutated judge.py> ...       (used by negctl.py: every guard removed must turn this red)
"""
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRODUCT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_PRODUCT, "ref_rewind"))
sys.path.insert(0, os.path.join(_PRODUCT, "ref_broker"))
import broker as B  # noqa: E402
import fakes  # noqa: E402
import schema as RW  # noqa: E402
import test_schema as TS  # noqa: E402  (R0 reference transaction run_txn / genesis)

_spec = importlib.util.spec_from_file_location("judge", os.environ.get("RW_JUDGE_PATH") or os.path.join(_HERE, "judge.py"))
J = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(J)

BASE_D = {"col:CHAR_mesh": "aa", "col:PROPS": "bb", "mat:Wood": "m1", "render": "r1", "ui:view3d": "cc"}
IN_FP = [160, 240, 80, 40]          # inside F1 FOOTPRINT (0, 80, 1580, 900)
OUT_FP = [1704, 304, 64, 64]        # outliner side (8 px cell aligned)
STATUS_BAR = [0, 1056, 1920, 24]    # below the footprint, beyond the slack
GOOD, WRONG_SPILL, WRONG_IN_SET = TS.GOOD, TS.WRONG, "scene['col:PROPS'].append('table10x')"


def d(**changes):
    out = dict(BASE_D)
    for k, v in changes.items():
        out[k.replace("__", ":")] = v
    return out


def intent(lane="script", write_set=("col:PROPS",), **kw):
    it = {"event_id": "props-agent-1", "agent_id": "props-agent", "lane": lane, "write_set": list(write_set), "expect": "text"}
    it.update(kw)
    return it


def att(it, verdict="STATE_VERIFIED", k=1):
    return {"attempt": "%s.a%d" % (it["event_id"], k), "intent": it, "script": "x", "verdict": verdict, "extra": {}}


def differ_of(regions):
    return lambda a, b: [dict(r) if isinstance(r, dict) else list(r) for r in regions]


def names(j):
    return [o["name"] for o in j["oracles"]]


def orc(j, name):
    return [o for o in j["oracles"] if o["name"] == name][0]


def probe(result, digest, ok=True):
    calls = []

    def run(src):
        calls.append(src)
        return {"ok": ok, "result": result, "digest": digest}
    run.calls = calls
    return run


EXPECT = {"probe": "print(...)", "equals": {"table_top_le_body_bottom": True}}


class ExpectOracle(unittest.TestCase):
    """Oracle 1. Wrong-edit fixture: the table is 10x too large, the probe answers False."""

    def run_j(self, run_probe, after=None, it=None):
        it = it or intent(expect=EXPECT)
        after = after or d(col__PROPS="b2")
        return J.judge(att(it), {"point_id": "p1", "digest": BASE_D, "frame_png": None},
                       {"digest": after, "frame_png": None, "job_ms": 12}, run_probe=run_probe)

    def test_right_edit_is_picked_and_probe_source_is_the_intents(self):
        after = d(col__PROPS="b2")
        rp = probe({"table_top_le_body_bottom": True}, after)
        j = self.run_j(rp, after)
        self.assertTrue(j["pick"])
        self.assertEqual(names(j), ["expect", "delta", "cost"])
        self.assertEqual(rp.calls, [EXPECT["probe"]])

    def test_wrong_edit_fails_the_probe(self):
        after = d(col__PROPS="b2")
        j = self.run_j(probe({"table_top_le_body_bottom": False}, after), after)
        self.assertFalse(j["pick"])
        self.assertFalse(orc(j, "expect")["ok"])
        self.assertTrue(orc(j, "expect")["detail"].startswith(J.EXPECT_MISMATCH))
        self.assertTrue(orc(j, "delta")["ok"])           # only the probe caught it
        self.assertEqual(J.reject_reason(j), "expect:EXPECT_MISMATCH")

    def test_probe_that_mutates_the_scene_is_rejected(self):
        after = d(col__PROPS="b2")
        j = self.run_j(probe({"table_top_le_body_bottom": True}, d(col__PROPS="b3")), after)   # right answer, dirty digest
        self.assertFalse(j["pick"])
        self.assertEqual(orc(j, "expect"), RW.oracle("expect", False, RW.PROBE_NOT_READONLY))

    def test_probe_without_digest_is_not_proven_readonly(self):
        j = self.run_j(probe({"table_top_le_body_bottom": True}, None))
        self.assertEqual(orc(j, "expect"), RW.oracle("expect", False, RW.PROBE_NOT_READONLY))

    def test_probe_job_error_is_rejected(self):
        after = d(col__PROPS="b2")
        j = self.run_j(probe({"table_top_le_body_bottom": True}, after, ok=False), after)
        self.assertFalse(j["pick"])
        self.assertEqual(orc(j, "expect"), RW.oracle("expect", False, RW.PROBE_FAILED))

    def test_probe_runner_that_raises_is_rejected_not_a_crash(self):
        def boom(src):
            raise RuntimeError("bridge gone")
        j = self.run_j(boom)
        self.assertFalse(j["pick"])
        self.assertTrue(orc(j, "expect")["detail"].startswith(RW.PROBE_FAILED + " RuntimeError"))

    def test_declared_expectation_without_runner_fails_closed(self):
        j = self.run_j(None)
        self.assertFalse(j["pick"])
        self.assertEqual(orc(j, "expect"), RW.oracle("expect", False, RW.PROBE_FAILED + " no probe runner"))

    def test_text_expect_omits_the_oracle(self):
        rp = probe(True, BASE_D)
        j = self.run_j(rp, it=intent())
        self.assertEqual(names(j), ["delta", "cost"])
        self.assertEqual(rp.calls, [])


class DeltaOracle(unittest.TestCase):
    """Oracle 2. Wrong-edit fixture: the attempt also moves CHAR_body (col:CHAR_mesh, not in write_set)."""

    def run_j(self, after, it=None, verdict="STATE_VERIFIED", base=BASE_D):
        return J.judge(att(it or intent(), verdict), {"point_id": "p1", "digest": base, "frame_png": None},
                       {"digest": after, "frame_png": None, "job_ms": 5})

    def test_delta_inside_write_set(self):
        j = self.run_j(d(col__PROPS="b2"))
        self.assertTrue(j["pick"])
        self.assertEqual(orc(j, "delta")["detail"], 'delta=["col:PROPS"]')

    def test_wrong_edit_spills_into_another_collection(self):
        j = self.run_j(d(col__PROPS="b2", col__CHAR_mesh="a2"))
        self.assertFalse(j["pick"])
        self.assertIn('outside=["col:CHAR_mesh"]', orc(j, "delta")["detail"])
        self.assertEqual(J.reject_reason(j), "delta:DELTA_OUTSIDE")

    def test_new_and_removed_keys_count(self):
        after = d(col__PROPS="b2")
        after["col:NEW"] = "n1"
        self.assertFalse(self.run_j(after)["pick"])
        after = d(col__PROPS="b2")
        del after["col:CHAR_mesh"]
        self.assertFalse(self.run_j(after)["pick"])

    def test_material_needs_touches(self):
        after = d(col__PROPS="b2", mat__Wood="m2")
        self.assertFalse(self.run_j(after)["pick"])
        self.assertTrue(self.run_j(after, it=intent(touches=["mat:Wood"]))["pick"])

    def test_gui_intent_implicitly_touches_the_view_only(self):
        gui = intent(lane="gui", write_set=())

        def gui_j(after):   # a gui turn is judged with frames: without them its witness fails (R3-1)
            return J.judge(att(gui, "VERIFIED"), {"point_id": "p1", "digest": BASE_D, "frame_png": "base.png"},
                           {"digest": after, "frame_png": "after.png", "job_ms": 5}, differ=differ_of([IN_FP]))

        self.assertTrue(gui_j(d(ui__view3d="c2"))["pick"])
        self.assertFalse(self.run_j(d(ui__view3d="c2"), it=intent(write_set=()))["pick"])     # script lane: undeclared
        j = gui_j(d(ui__view3d="c2", col__PROPS="b2"))
        self.assertFalse(j["pick"])
        self.assertEqual(J.reject_reason(j), "delta:DELTA_OUTSIDE")

    def test_no_effect_is_rejected(self):
        j = self.run_j(dict(BASE_D))
        self.assertFalse(j["pick"])
        self.assertEqual(orc(j, "delta")["detail"], J.NO_EFFECT)

    def test_f1_verdict_must_be_committable(self):
        for v in ("JOB_ERROR", "VERIFIED_WITH_SPILL", "OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN", None):
            j = self.run_j(d(col__PROPS="b2"), verdict=v)
            self.assertFalse(j["pick"], v)
            self.assertTrue(orc(j, "delta")["detail"].startswith(J.VERDICT_NOT_COMMITTABLE), v)

    def test_missing_or_malformed_digest_fails_closed(self):
        for after in (None, {}, {"objects": "zz"}):
            j = self.run_j(after)
            self.assertFalse(j["pick"])
            self.assertEqual(orc(j, "delta")["detail"], J.NO_DIGEST)
        self.assertEqual(orc(self.run_j(d(col__PROPS="b2"), base=None), "delta")["detail"], J.NO_DIGEST)


class WitnessOracle(unittest.TestCase):
    """Oracle 3. Wrong-edit fixtures: a digest change the viewport does not show; a gui turn that
    paints outside the footprint; a restore that leaves pixels of the wrong edit."""

    def run_j(self, regions, it=None, after=None, verdict="STATE_VERIFIED", **kw):
        it = it or intent()
        return J.judge(att(it, verdict), {"point_id": "p1", "digest": BASE_D, "frame_png": "base.png"},
                       {"digest": after or d(col__PROPS="b2"), "frame_png": "after.png", "job_ms": 5},
                       differ=differ_of(regions) if not callable(regions) else regions, **kw)

    def test_change_inside_footprint(self):
        j = self.run_j([IN_FP])
        self.assertTrue(j["pick"])
        self.assertEqual(names(j), ["delta", "witness", "cost"])
        self.assertEqual(orc(j, "witness")["detail"], "in=1 out=0 first=[160,240,80,40]")

    def test_differ_gets_base_then_after_frame(self):
        seen = []
        self.run_j(lambda a, b: seen.append((a, b)) or [IN_FP])
        self.assertEqual(seen, [("base.png", "after.png")])

    def test_digest_changed_but_nothing_visible_is_rejected(self):
        for regions in ([], [STATUS_BAR]):
            j = self.run_j(regions)
            self.assertFalse(j["pick"])
            self.assertTrue(orc(j, "witness")["detail"].startswith(J.WITNESS_NO_CHANGE))
        self.assertEqual(J.reject_reason(self.run_j([])), "witness:WITNESS_NO_CHANGE")

    def test_gui_change_outside_footprint_is_a_spill(self):
        gui = intent(lane="gui", write_set=())
        j = self.run_j([IN_FP, OUT_FP], it=gui, after=d(ui__view3d="c2"), verdict="VERIFIED")
        self.assertFalse(j["pick"])
        self.assertTrue(orc(j, "witness")["detail"].startswith(J.WITNESS_SPILL))
        self.assertTrue(self.run_j([IN_FP], it=gui, after=d(ui__view3d="c2"), verdict="VERIFIED")["pick"])

    def test_script_lane_tolerates_outliner_repaint_unless_strict(self):
        j = self.run_j([IN_FP, OUT_FP])
        self.assertTrue(j["pick"])
        self.assertEqual(orc(j, "witness")["detail"], "in=1 out=1 first=[160,240,80,40]")
        self.assertFalse(self.run_j([IN_FP, OUT_FP], strict_outside=True)["pick"])

    def test_region_within_slack_of_the_border_is_not_spill(self):
        gui = intent(lane="gui", write_set=())
        edge = [1568, 96, 64, 64]      # crosses x=1580 by 52 px < slack 64
        self.assertTrue(self.run_j([edge], it=gui, after=d(ui__view3d="c2"), verdict="VERIFIED")["pick"])
        far = [1568, 96, 160, 64]
        self.assertFalse(self.run_j([far], it=gui, after=d(ui__view3d="c2"), verdict="VERIFIED")["pick"])

    def test_render_only_delta_need_not_show(self):
        j = self.run_j([], it=intent(write_set=(), touches=["render"]), after=d(render="r2"))
        self.assertTrue(j["pick"])

    def test_differ_error_fails_closed(self):
        def boom(a, b):
            raise RuntimeError("ffmpeg gray failed")
        j = self.run_j(boom)
        self.assertFalse(j["pick"])
        self.assertTrue(orc(j, "witness")["detail"].startswith(J.WITNESS_ERROR))

    def test_dict_regions_from_vision_are_accepted(self):
        self.assertTrue(self.run_j([{"x": 0, "y": 96, "w": 1568, "h": 864, "score": 1.0}])["pick"])

    def test_no_frames_omits_the_oracle(self):
        j = J.judge(att(intent()), {"point_id": "p1", "digest": BASE_D, "frame_png": None},
                    {"digest": d(col__PROPS="b2"), "frame_png": "after.png", "job_ms": 5}, differ=differ_of([]))
        self.assertEqual(names(j), ["delta", "cost"])

    def test_gui_without_a_frame_fails_the_witness(self):
        """xr-core R3-1: R2 passes frame_png=None when the base frame file is gone."""
        gui = intent(lane="gui", write_set=())
        for base_png, after_png in ((None, "after.png"), ("base.png", None), (None, None)):
            j = J.judge(att(gui, "VERIFIED"), {"point_id": "p1", "digest": BASE_D, "frame_png": base_png},
                        {"digest": d(ui__view3d="c2"), "frame_png": after_png, "job_ms": 5}, differ=differ_of([IN_FP]))
            self.assertFalse(j["pick"], (base_png, after_png))
            self.assertTrue(orc(j, "witness")["detail"].startswith(J.WITNESS_ERROR))
            self.assertEqual(J.reject_reason(j).split(":")[0], "witness")

    def test_non_finite_job_ms_does_not_raise(self):
        for ms in (float("inf"), float("-inf"), float("nan")):
            j = J.judge(att(intent()), {"point_id": "p1", "digest": BASE_D, "frame_png": "a"},
                        {"digest": d(col__PROPS="b2"), "frame_png": "b", "job_ms": ms}, differ=differ_of([IN_FP]))
            self.assertFalse(orc(j, "cost")["ok"], ms)
            self.assertEqual(orc(j, "cost")["detail"], "job_ms=unknown")

    # ---- after a rollback
    def test_rollback_witness_equal_frames(self):
        self.assertEqual(J.rollback_witness("a", "b", differ=differ_of([])), RW.oracle("witness", True, "restored in=0 out=0"))

    def test_rollback_witness_wrong_edit_still_on_screen(self):
        o = J.rollback_witness("a", "b", differ=differ_of([IN_FP]))
        self.assertFalse(o["ok"])
        self.assertTrue(o["detail"].startswith(J.WITNESS_NOT_RESTORED))
        self.assertFalse(J.rollback_witness("a", "b", differ=differ_of([[1500, 880, 200, 200]]))["ok"])   # straddles the border

    def test_rollback_witness_ignores_pixels_outside_footprint(self):
        self.assertEqual(J.rollback_witness("a", "b", differ=differ_of([STATUS_BAR, OUT_FP])),
                         RW.oracle("witness", True, "restored in=0 out=2"))

    def test_rollback_witness_fails_closed(self):
        def boom(a, b):
            raise RuntimeError("no frame")
        self.assertFalse(J.rollback_witness("a", "b", differ=boom)["ok"])
        self.assertFalse(J.rollback_witness(None, "b", differ=differ_of([]))["ok"])


class CostOracle(unittest.TestCase):
    """Oracle 4: recorded, never deciding, tie-break between accepted attempts."""

    def rec(self, k, ms, ok=True):
        after = d(col__PROPS="b2") if ok else d(col__PROPS="b2", col__CHAR_mesh="a2")
        return J.judge(att(intent(), k=k), {"point_id": "p1", "digest": BASE_D, "frame_png": None},
                       {"digest": after, "frame_png": None, "job_ms": ms})

    def test_cost_is_recorded_last_and_never_decides(self):
        j = self.rec(1, 37)
        self.assertEqual(j["oracles"][-1], RW.oracle("cost", True, "job_ms=37"))
        self.assertEqual(J.cost_ms(j), 37)
        j = self.rec(1, None)
        self.assertTrue(j["pick"])                      # unknown cost does not reject
        self.assertEqual(j["oracles"][-1], RW.oracle("cost", False, "job_ms=unknown"))
        self.assertIsNone(J.cost_ms(j))
        self.assertFalse(self.rec(1, 1, ok=False)["pick"])   # a cheap wrong edit stays rejected

    def test_best_prefers_cheaper_accepted_attempt(self):
        a1, a2, a3 = self.rec(1, 90), self.rec(2, 40), self.rec(3, 5, ok=False)
        self.assertEqual(J.best([a1, a2, a3])["attempt"], "props-agent-1.a2")
        self.assertEqual(J.best([self.rec(1, 40), self.rec(2, 40)])["attempt"], "props-agent-1.a1")   # tie: order
        self.assertEqual(J.best([self.rec(1, None), self.rec(2, 400)])["attempt"], "props-agent-1.a2")

    def test_best_discards_all_when_none_accepted(self):
        self.assertIsNone(J.best([self.rec(1, 1, ok=False), self.rec(2, 2, ok=False)]))
        self.assertIsNone(J.best([]))


class Record(unittest.TestCase):
    def test_record_is_schema_valid_in_fixed_order(self):
        after = d(col__PROPS="b2")
        it = intent(expect=EXPECT)
        j = J.judge(att(it), {"point_id": "p1", "digest": BASE_D, "frame_png": "a"},
                    {"digest": after, "frame_png": "b", "job_ms": 3},
                    run_probe=probe(EXPECT["equals"], after), differ=differ_of([IN_FP]))
        self.assertEqual(names(j), list(RW.ORACLES))
        self.assertIs(RW.validate_record(j), j)
        self.assertEqual((j["rec"], j["attempt"], j["pick"], j["judge_version"]), ("judge", "props-agent-1.a1", True, RW.JUDGE_VERSION))
        self.assertIsNone(J.reject_reason(j))

    def test_all_oracles_fail_together_and_are_all_reported(self):
        it = intent(expect=EXPECT)
        after = d(col__PROPS="b2", col__CHAR_mesh="a2")
        j = J.judge(att(it), {"point_id": "p1", "digest": BASE_D, "frame_png": "a"},
                    {"digest": after, "frame_png": "b", "job_ms": 3},
                    run_probe=probe({"table_top_le_body_bottom": False}, after), differ=differ_of([]))
        self.assertEqual([(o["name"], o["ok"]) for o in j["oracles"]],
                         [("expect", False), ("delta", False), ("witness", False), ("cost", True)])
        RW.validate_record(j)

    def test_make_judge_has_the_r0_signature(self):
        jf = J.make_judge(differ=differ_of([IN_FP]))
        j = jf(att(intent()), {"point_id": "p1", "digest": BASE_D, "frame_png": "a"},
               {"digest": d(col__PROPS="b2"), "frame_png": "b", "job_ms": 3}, run_probe=None)
        self.assertTrue(j["pick"])
        self.assertEqual(names(j), ["delta", "witness", "cost"])


class Scenario(unittest.TestCase):
    """Wrong-edit scenario through the real F1 broker + bridge core (R0 fakes, R0 run_txn)."""

    def cap(self):
        c = fakes.Capsule()
        self.addCleanup(c.close)
        return c

    def run_scenario(self, judge, scripts):
        c = self.cap()
        store, st = TS.genesis(c)
        out = TS.run_txn(c, store, st, "props-agent-1", scripts, judge=judge)
        return c, st, out

    def final_check(self, c):
        """The demo's final probe: only accepted work is in the scene."""
        self.assertEqual(c.scene["col:PROPS"], ["table"])
        self.assertEqual(c.scene["col:CHAR_mesh"], [])

    def test_spill_edit_rolled_back_then_right_edit_committed(self):
        c, st, out = self.run_scenario(J.make_judge(differ=fakes.fake_differ), [WRONG_SPILL, GOOD])
        self.assertEqual(out, "COMMIT")
        self.final_check(c)
        js = c.events("judge")
        self.assertEqual([(j["attempt"], j["pick"]) for j in js], [("props-agent-1.a1", False), ("props-agent-1.a2", True)])
        self.assertEqual([(o["name"], o["ok"]) for o in js[0]["oracles"]],
                         [("expect", False), ("delta", False), ("witness", True), ("cost", True)])
        self.assertEqual(names(js[1]), list(RW.ORACLES))
        self.assertEqual(RW.replay(RW.read_events(os.path.join(c.rt, "events.jsonl"))).violations, [])
        self.assertEqual(st.scene_epoch, RW.SCENE_EPOCH_0 + 1)

    def test_in_write_set_wrong_edit_is_caught_by_the_probe_alone(self):
        c, st, out = self.run_scenario(J.make_judge(differ=fakes.fake_differ), [WRONG_IN_SET, GOOD])
        self.assertEqual(out, "COMMIT")
        self.final_check(c)
        j1 = c.events("judge")[0]
        self.assertEqual([(o["name"], o["ok"]) for o in j1["oracles"]],
                         [("expect", False), ("delta", True), ("witness", True), ("cost", True)])

    def test_all_alternatives_wrong_leaves_the_base(self):
        c, st, out = self.run_scenario(J.make_judge(differ=fakes.fake_differ), [WRONG_SPILL, WRONG_IN_SET])
        self.assertEqual(out, RW.ALL_REJECTED)
        self.assertEqual(c.scene["col:PROPS"], [])
        self.assertIsNone(J.best(c.events("judge")))

    def test_must_fail_always_accept_judge_breaks_the_final_assertion(self):
        """Dead-oracle control (rw-build F7): with an accept-all judge the same scenario must end wrong."""
        c, st, out = self.run_scenario(fakes.accept_all_judge, [WRONG_IN_SET, GOOD])
        self.assertEqual(out, "COMMIT")                 # the wrong a1 was committed: F1 alone cannot see it
        with self.assertRaises(AssertionError):
            self.final_check(c)

    def test_probe_that_mutates_through_the_real_bridge(self):
        c = self.cap()
        store, st = TS.genesis(c)
        c.control("digest", GOOD, 1)               # apply the edit as a broker job
        after = c.digest()
        it = intent(expect={"probe": "scene['col:PROPS'].append('x'); " + TS.PROBE, "equals": ["table", "x"]})

        def run_probe(src):
            r = c.control("probe", src, 1)
            return {"ok": r["ok"], "result": r["result"]["value"], "digest": r["result"]["digest"]}

        j = J.judge(att(it), {"point_id": "p0", "digest": store.get("p0")["digest"], "frame_png": None},
                    {"digest": after, "frame_png": None, "job_ms": 1}, run_probe=run_probe)
        self.assertFalse(j["pick"])
        self.assertEqual(j["oracles"][0], RW.oracle("expect", False, RW.PROBE_NOT_READONLY))
        self.assertEqual(c.scene["col:PROPS"], ["table", "x"])     # the probe did answer "equals": only the digest tells


def _png(path, w, h, boxes):
    rows = []
    for y in range(h):
        row = bytearray(w)
        for (bx, by, bw, bh) in boxes:
            if by <= y < by + bh:
                row[bx:bx + bw] = b"\xff" * bw
        rows.append(b"\x00" + bytes(row))

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class RealDiffer(unittest.TestCase):
    """The witness with the default differ (F1 diff_regions, real ffmpeg) on real PNG files."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="vprw_r3_png_")
        cls.base, cls.table, cls.status, cls.both = [os.path.join(cls.dir, n) for n in ("base.png", "table.png", "status.png", "both.png")]
        _png(cls.base, 1920, 1080, [])
        _png(cls.table, 1920, 1080, [tuple(IN_FP)])
        _png(cls.status, 1920, 1080, [(8, 1060, 400, 16)])
        _png(cls.both, 1920, 1080, [tuple(IN_FP), tuple(OUT_FP)])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def run_j(self, after_png, it=None, after=None, verdict="STATE_VERIFIED"):
        return J.judge(att(it or intent(), verdict), {"point_id": "p1", "digest": BASE_D, "frame_png": self.base},
                       {"digest": after or d(col__PROPS="b2"), "frame_png": after_png, "job_ms": 5})

    def test_forward_witness(self):
        self.assertEqual(orc(self.run_j(self.table), "witness"), RW.oracle("witness", True, "in=1 out=0 first=[160,240,80,40]"))
        self.assertFalse(self.run_j(self.base)["pick"])
        self.assertFalse(self.run_j(self.status)["pick"])
        gui = intent(lane="gui", write_set=())
        self.assertFalse(self.run_j(self.both, it=gui, after=d(ui__view3d="c2"), verdict="VERIFIED")["pick"])

    def test_rollback_witness(self):
        self.assertTrue(J.rollback_witness(self.base, self.base)["ok"])
        self.assertTrue(J.rollback_witness(self.base, self.status)["ok"])
        self.assertFalse(J.rollback_witness(self.base, self.table)["ok"])
        self.assertFalse(J.rollback_witness(self.base, os.path.join(self.dir, "missing.png"))["ok"])

    def test_default_differ_is_f1(self):
        self.assertEqual(B.diff_regions(self.base, self.both), [IN_FP, OUT_FP])


if __name__ == "__main__":
    unittest.main()
