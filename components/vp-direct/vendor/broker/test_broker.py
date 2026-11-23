"""Tests for the reference broker + bridge. No Blender, no vision: a fake vision and the real
bridge core (bridge.poll_once) on a thread with a dict standing in for the Blender scene.

    python3 -m unittest -v test_broker        (from product/ref_broker/)

Negative controls (02 section 9 + extras) use `red_without`: the same scenario is re-run with one
broker check removed and must then FAIL its assertions, i.e. the test goes red without the check.
"""
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge  # noqa: E402
import broker as B  # noqa: E402

NONCE = "n0ncetest"
CLAIMS = {"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS"],
          "look-agent": ["col:LOOK"], "camera-agent": ["col:CAM"]}
VIEW_REGION = {"x": 0, "y": 96, "w": 1568, "h": 864, "score": 1.0}


def _read(p, mode="r"):
    with open(p, mode) as f:
        return f.read()


def _json(p):
    return json.loads(_read(p))


class FakeVision:
    def __init__(self, d):
        self.d, self.state, self.n = d, 0, 0
        self.acts, self.looks = [], 0
        self.key_rc, self.hover_rc = {}, 0
        self.regions = [VIEW_REGION]

    def look(self):
        self.looks += 1
        self.n += 1
        p = os.path.join(self.d, "shot_%d.png" % self.n)
        with open(p, "wb") as f:
            f.write(b"FAKEPNG state=%d" % self.state)
        return 0, {"frame": p, "width": 1920, "height": 1080}

    def act(self, args):
        self.acts.append(list(args))
        if args[0] == "--xy":
            return self.hover_rc, {}
        rc = self.key_rc.get(args[1], 0)
        if rc == 0:
            self.state += 1
            return 0, {"changed_regions": list(self.regions)}
        return rc, {"changed_regions": []}


class Env:
    def __init__(self, cls=B.Broker, with_bridge=True, cap=5.0, bridge_nonce=NONCE):
        self.rt = tempfile.mkdtemp(prefix="vpref_test_")
        self.shots = os.path.join(self.rt, "_shots")
        os.makedirs(self.shots)
        with open(os.path.join(self.rt, "claims.json"), "w") as f:
            json.dump(CLAIMS, f)
        self.vision = FakeVision(self.shots)
        self.scene = {c[0]: [] for c in CLAIMS.values()}
        self.ran = []
        self._stop = threading.Event()
        self.thread = None
        self.cls, self.cap = cls, cap
        self.broker = self.new_broker()
        if with_bridge:
            self.start_bridge(bridge_nonce)

    def new_broker(self):
        return self.cls(self.rt, "fakejob", NONCE, vision=self.vision, job_cap_s=self.cap, poll_s=0.01,
                        differ=lambda a, b: [[0, 0, 16, 16]])

    def start_bridge(self, nonce=NONCE):
        ns = {"scene": self.scene, "paint": self._paint, "time": time}

        def digest():
            return {k: hashlib.sha256(repr(v).encode()).hexdigest() for k, v in self.scene.items()}

        def loop():
            while not self._stop.is_set():
                self.ran += bridge.poll_once(self.rt, nonce, ns, digest=digest, log=lambda m: None)
                time.sleep(0.01)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def _paint(self):
        self.vision.state += 1

    def put(self, agent, eid, lane="script", script="scene['col:CHAR_mesh'].append(1); paint()",
            write_set=("col:CHAR_mesh",), keys=None, based=0, dir_agent=None, **extra):
        it = {"v": 1, "rec": "enqueue", "event_id": eid, "agent_id": agent, "lane": lane,
              "kind": "job" if lane == "script" else "key", "based_on_frame": based,
              "write_set": list(write_set), "expect": "test"}
        if lane == "script":
            it["script"] = script
        else:
            it["keys"] = list(keys or ["KP_1", "KP_3"])
        it.update(extra)
        d = os.path.join(self.rt, "intents", dir_agent or agent)
        with open(os.path.join(d, eid + ".tmp"), "w") as f:
            json.dump(it, f)
        os.replace(os.path.join(d, eid + ".tmp"), os.path.join(d, eid + ".json"))
        time.sleep(0.002)  # distinct mtimes -> deterministic FIFO order

    def events(self, rec=None, **match):
        out = []
        with open(os.path.join(self.rt, "events.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                if rec and r["rec"] != rec:
                    continue
                if all(r.get(k) == v for k, v in match.items()):
                    out.append(r)
        return out

    def verdicts(self, eid):
        return [r["verdict"] for r in self.events("close", event_id=eid)]

    def key_acts(self):
        return [a for a in self.vision.acts if a[0] == "--key"]

    def close(self):
        self._stop.set()
        if self.thread:
            self.thread.join(2)
        self.broker.close()
        shutil.rmtree(self.rt, ignore_errors=True)


class Base(unittest.TestCase):
    def env(self, **kw):
        e = Env(**kw)
        self.addCleanup(e.close)
        return e

    def red_without(self, check, scenario):
        """Negative control: with `check` removed from the broker the scenario's assertions must fail."""
        weak = type("Weak", (B.Broker,), {check: lambda self, *a, **k: None})
        with self.assertRaises(AssertionError, msg="scenario stayed green without %s" % check):
            scenario(weak)


class ScriptLane(Base):
    def test_state_verified_records_frame_cursor(self):
        e = self.env()
        e.put("modeler-agent", "modeler-agent-1")
        self.assertEqual(e.broker.run(1), ["STATE_VERIFIED"])
        self.assertEqual(e.scene["col:CHAR_mesh"], [1])
        c = e.events("close", event_id="modeler-agent-1")[0]
        self.assertEqual((c["tick"], c["turn"], c["dispatch_capture_seq"], c["verify_frame_ref"]), (1, 1, 0, 1))
        self.assertEqual(c["changed_write_set"], ["col:CHAR_mesh"])
        fr = _json(os.path.join(e.rt, "frames", "frame_1.json"))
        self.assertEqual((fr["v"], fr["frame"], fr["by_event"]), (1, 1, "modeler-agent-1"))
        self.assertEqual(fr["changed_regions"], [[0, 0, 16, 16]])
        self.assertTrue(os.path.exists(fr["png"]))
        self.assertEqual(fr["sha256"], hashlib.sha256(_read(fr["png"], "rb")).hexdigest())
        f0 = _json(os.path.join(e.rt, "frames", "frame_0.json"))
        self.assertIsNone(f0["by_event"])
        cur = e.events("cursor", event_id="modeler-agent-1")
        self.assertEqual([r["phase"] for r in cur], ["intent", "act", "result"])
        self.assertEqual({(r["x"], r["y"]) for r in cur}, {B.SCRIPT_SLOTS["modeler-agent"]})
        self.assertEqual(len(_read(os.path.join(e.rt, "cursor.jsonl")).splitlines()), 3)
        enq = e.events("enqueue")[0]
        self.assertEqual(enq["script_sha256"], hashlib.sha256(enq["script"].encode()).hexdigest())
        self.assertEqual(os.listdir(os.path.join(e.rt, "inbox", "running")), [])

    def test_job_error(self):
        e = self.env()
        e.put("modeler-agent", "m-err", script="raise ValueError('boom')")
        self.assertEqual(e.broker.run(1), ["JOB_ERROR"])
        self.assertIn("ValueError", e.events("close", event_id="m-err")[0]["out_tail"])
        self.assertEqual(e.broker.frame, 1)  # a frame is captured: 'unchanged' is not proven

    def _claim_scenario(self, cls=B.Broker):
        e = self.env(cls=cls)
        e.put("modeler-agent", "m-steal", script="scene['col:PROPS'].append('x')", write_set=["col:PROPS"])
        e.broker.run(1)
        self.assertEqual(e.verdicts("m-steal"), ["CLAIM_REFUSED"])
        self.assertEqual(e.scene["col:PROPS"], [], "refused job must not run")
        self.assertEqual(e.ran, [])
        self.assertEqual(e.broker.frame, 0)
        self.assertEqual(e.broker.tick, 0)

    def test_claim_refused(self):
        self._claim_scenario()

    def test_negative_control_claim_check(self):
        self.red_without("_check_claim", self._claim_scenario)

    def test_empty_write_set_refused(self):
        e = self.env()
        e.put("modeler-agent", "m-empty", write_set=[])
        self.assertEqual(e.broker.run(1), ["CLAIM_REFUSED"])

    def test_spill_measured_by_bridge_digest(self):
        e = self.env()
        e.put("modeler-agent", "m-spill", script="scene['col:PROPS'].append('x')")  # declares col:CHAR_mesh
        self.assertEqual(e.broker.run(1), ["VERIFIED_WITH_SPILL"])
        self.assertEqual(e.events("close", event_id="m-spill")[0]["spill_write_set"], ["col:PROPS"])

    def test_unauthenticated_outbox_is_uncertain(self):
        e = self.env(with_bridge=False)
        stop = threading.Event()

        def forger():  # an agent that answers for the bridge without knowing the nonce
            while not stop.is_set():
                if any(n.endswith(".py") for n in os.listdir(os.path.join(e.rt, "inbox"))):
                    with open(os.path.join(e.rt, "outbox", "m-forged.out"), "w") as f:
                        f.write("OK\nMETA {\"changed\": []}\n")
                    return
                time.sleep(0.01)

        t = threading.Thread(target=forger, daemon=True)
        t.start()
        self.addCleanup(stop.set)
        e.put("modeler-agent", "m-forged")
        self.assertEqual(e.broker.run(1), ["OUTCOME_UNCERTAIN"])
        self.assertEqual(e.broker.mode, "RECONCILE")


class Queue(Base):
    def test_pass_does_not_advance_tick_or_frame(self):
        e = self.env()
        self.assertEqual(e.broker.run(4), ["PASS"] * 4)
        self.assertEqual((e.broker.tick, e.broker.frame, e.vision.looks), (0, 0, 1))
        self.assertEqual([t["agent_id"] for t in e.events("turn")], list(B.AGENTS))

    def test_round_robin_one_event_per_turn_oldest_first(self):
        e = self.env()
        e.put("look-agent", "l-1", script="scene['col:LOOK'].append('l1')", write_set=["col:LOOK"])
        e.put("modeler-agent", "m-1", script="scene['col:CHAR_mesh'].append('m1')")
        e.put("modeler-agent", "m-2", script="scene['col:CHAR_mesh'].append('m2')")
        self.assertEqual(e.broker.run(1), ["STATE_VERIFIED"])
        # out of turn: look-agent enqueued first but modeler-agent is head; only m-1 has run
        self.assertEqual(e.ran, ["m-1"])
        self.assertEqual(e.scene["col:LOOK"], [])
        self.assertEqual(e.broker.run(4), ["PASS", "STATE_VERIFIED", "PASS", "STATE_VERIFIED"])
        self.assertEqual(e.ran, ["m-1", "l-1", "m-2"])
        self.assertEqual([c["tick"] for c in e.events("close") if c["event_id"]], [1, 2, 3])

    def test_queue_full_refuses_and_never_overwrites(self):
        e = self.env()
        for i in range(10):
            e.put("modeler-agent", "m-%d" % i, script="scene['col:CHAR_mesh'].append(%d)" % i)
        e.broker.ingest()
        self.assertEqual([len(e.verdicts("m-%d" % i)) for i in range(10)], [0] * 8 + [1, 1])
        self.assertEqual(e.verdicts("m-8"), ["QUEUE_FULL"])
        self.assertEqual([q["event_id"] for q in e.broker.queues["modeler-agent"]], ["m-%d" % i for i in range(8)])
        e.broker.ingest()
        self.assertEqual(len(e.events("close", verdict="QUEUE_FULL")), 2, "refusal is logged once per file")
        e.broker.run(1)
        self.assertEqual(e.scene["col:CHAR_mesh"], [0])
        e.broker.ingest()  # one slot free: the oldest refused file is admitted, the other still waits
        self.assertEqual(len(e.broker.queues["modeler-agent"]), 8)
        self.assertEqual(e.broker.queues["modeler-agent"][-1]["event_id"], "m-8")

    def _dup_scenario(self, cls=B.Broker):
        e = self.env(cls=cls)
        e.put("modeler-agent", "m-1", script="scene['col:CHAR_mesh'].append('first')")
        e.broker.run(4)
        e.put("modeler-agent", "m-1", script="scene['col:CHAR_mesh'].append('again')")
        e.broker.run(4)
        self.assertEqual(e.scene["col:CHAR_mesh"], ["first"], "an event_id is applied at most once")
        self.assertEqual(e.verdicts("m-1"), ["STATE_VERIFIED", "DUPLICATE_EVENT"])

    def test_duplicate_event(self):
        self._dup_scenario()

    def test_negative_control_duplicate_check(self):
        class NoDedup(B.Broker):
            def ingest(self):
                self.status.clear()
                return B.Broker.ingest(self)
        with self.assertRaises(AssertionError):
            self._dup_scenario(NoDedup)

    def test_duplicate_while_queued(self):
        e = self.env()
        e.put("look-agent", "l-1", script="scene['col:LOOK'].append('a')", write_set=["col:LOOK"])
        e.broker.ingest()
        e.put("look-agent", "l-1", script="scene['col:LOOK'].append('b')", write_set=["col:LOOK"])
        e.broker.run(4)
        self.assertEqual(e.scene["col:LOOK"], ["a"])
        self.assertIn("DUPLICATE_EVENT", e.verdicts("l-1"))

    def test_bad_intent_and_spoofed_agent(self):
        e = self.env()
        e.put("modeler-agent", "bad_id")                      # "_" not allowed in event_id
        e.put("modeler-agent", "m-lane", lane="script", script=None)
        e.put("camera-agent", "p-spoof", dir_agent="props-agent", lane="gui")
        p = os.path.join(e.rt, "intents", "look-agent", "l-junk.json")
        with open(p, "w") as f:
            f.write("{not json")
        os.utime(p, (time.time() - 10, time.time() - 10))
        self.assertEqual(e.broker.run(4), ["PASS"] * 4)
        self.assertEqual(e.verdicts("m-lane"), ["BAD_INTENT"])
        self.assertEqual(e.verdicts("l-junk"), ["BAD_INTENT"])
        self.assertEqual(e.verdicts("p-spoof"), ["NO_LEASE"])
        self.assertEqual(len(e.events("close", verdict="BAD_INTENT")), 3)
        self.assertEqual(e.vision.acts, [])
        e.broker.run(4)
        self.assertEqual(len(e.events("close", verdict="BAD_INTENT")), 3, "refused once, not per scan")

    def test_single_writer_lock(self):
        e = self.env()
        with self.assertRaises(B.BrokerError):
            e.new_broker()

    def test_restart_recovers_state_from_event_log(self):
        e = self.env()
        e.put("modeler-agent", "m-1")
        e.put("look-agent", "l-1", script="scene['col:LOOK'].append('l')", write_set=["col:LOOK"])
        e.put("look-agent", "l-2", script="scene['col:LOOK'].append('l2')", write_set=["col:LOOK"])
        self.assertEqual(e.broker.run(1), ["STATE_VERIFIED"])
        e.broker.close()
        e.broker = e.new_broker()
        self.assertEqual((e.broker.turn, e.broker.tick, e.broker.frame), (1, 1, 1))
        self.assertEqual([q["event_id"] for q in e.broker.queues["look-agent"]], ["l-1", "l-2"])
        self.assertEqual(e.broker.run(3), ["PASS", "STATE_VERIFIED", "PASS"])
        self.assertEqual(e.events("close", verdict="DUPLICATE_EVENT"), [])
        self.assertEqual(e.scene["col:CHAR_mesh"], [1])
        seqs = [r["seq"] for r in e.events()]
        self.assertEqual(seqs, sorted(set(seqs)))


class GuiLane(Base):
    def test_verified_hover_then_keys(self):
        e = self.env()
        e.put("camera-agent", "c-1", lane="gui", write_set=[])
        self.assertEqual(e.broker.run(4), ["PASS", "PASS", "PASS", "VERIFIED"])
        fp = "0,80,1580,900"
        self.assertEqual(e.vision.acts, [["--xy", "800,500"], ["--key", "KP_1", "--expect-change", fp],
                                         ["--key", "KP_3", "--expect-change", fp]])
        c = e.events("close", event_id="c-1")[0]
        self.assertEqual((c["acts_done"], c["verify_frame_ref"], c["spill_regions"]), (2, 1, []))
        self.assertEqual(c["own_regions"], [[0, 96, 1568, 864]] * 2)
        fr = _json(os.path.join(e.rt, "frames", "frame_1.json"))
        self.assertEqual(fr["changed_regions"], [[0, 96, 1568, 864]] * 2)
        self.assertEqual({(r["x"], r["y"]) for r in e.events("cursor", event_id="c-1")}, {B.VIEW_CENTRE})

    def test_spill_no_change_uncertain_busy(self):
        e = self.env()
        e.vision.regions = [VIEW_REGION, {"x": 1600, "y": 0, "w": 320, "h": 64}]
        e.put("camera-agent", "c-spill", lane="gui", keys=["KP_1"])
        self.assertEqual(e.broker.run(4)[-1], "VERIFIED_WITH_SPILL")
        self.assertEqual(e.events("close", event_id="c-spill")[0]["spill_regions"], [[1600, 0, 320, 64]])

        e.vision.key_rc = {"KP_3": 3}
        e.put("camera-agent", "c-nochange", lane="gui", keys=["KP_1", "KP_3", "KP_7"], based=e.broker.frame)
        n = len(e.key_acts())
        self.assertEqual(e.broker.run(4)[-1], "NO_VISIBLE_CHANGE")
        self.assertEqual([a[1] for a in e.key_acts()[n:]], ["KP_1", "KP_3"], "remaining keys are not sent")

        e.vision.key_rc = {"KP_1": 6}
        e.put("camera-agent", "c-busy", lane="gui", keys=["KP_1"], based=e.broker.frame)
        self.assertEqual(e.broker.run(4)[-1], "ACTUATOR_BUSY")
        self.assertEqual([q["event_id"] for q in e.broker.queues["camera-agent"]], ["c-busy"])
        e.broker.queues["camera-agent"].clear()

        e.vision.key_rc = {"KP_1": 8}
        e.put("camera-agent", "c-unc", lane="gui", keys=["KP_1", "KP_3"], based=e.broker.frame)
        n = len(e.key_acts())
        self.assertEqual(e.broker.run(4)[-1], "INPUT_UNCERTAIN")
        self.assertEqual(len(e.key_acts()) - n, 1, "no automatic repeat, no further keys")
        self.assertEqual(e.broker.mode, "RECONCILE")
        self.assertEqual(e.events("control")[-1]["op"], "reconcile")

    def test_key_allowlist(self):
        e = self.env()
        e.put("camera-agent", "c-quit", lane="gui", keys=["ctrl+q"])
        self.assertEqual(e.broker.run(4)[-1], "CLAIM_REFUSED")
        self.assertEqual(e.vision.acts, [])

    def test_frame_evicted(self):
        e = self.env()
        e.put("camera-agent", "c-future", lane="gui", based=99)
        self.assertEqual(e.broker.run(4)[-1], "FRAME_EVICTED")
        self.assertEqual(e.vision.acts, [])

    # ---- 02 section 9, control 1: out of turn / no lease
    def _no_lease_scenario(self, cls=B.Broker):
        e = self.env(cls=cls)
        e.put("props-agent", "p-gui", lane="gui", write_set=[])   # B submits a GUI act; the lease is camera-agent's
        e.broker.run(2)
        self.assertEqual(e.verdicts("p-gui"), ["NO_LEASE"])
        self.assertEqual(e.vision.acts, [], "no input may be injected")
        self.assertEqual(e.events("cursor", event_id="p-gui", phase="act"), [], "no dispatch record")
        self.assertEqual(e.broker.frame, 0, "next frame unchanged")
        self.assertEqual(e.vision.state, 0)

    def test_nc1_out_of_turn_no_lease(self):
        self._no_lease_scenario()

    def test_nc1_negative_control(self):
        self.red_without("_check_lease", self._no_lease_scenario)

    # ---- 02 section 9, control 2: stale frame
    def _stale_scenario(self, cls=B.Broker):
        e = self.env(cls=cls)
        e.put("camera-agent", "c-1", lane="gui", based=0)          # B drafts against frame 0
        e.put("modeler-agent", "m-1")                              # A's event changes the screen
        self.assertEqual(e.broker.run(4), ["STATE_VERIFIED", "PASS", "PASS", "STALE_FRAME"])
        self.assertEqual(e.vision.acts, [], "no input injected for a stale intent")
        c = e.events("close", event_id="c-1")[0]
        self.assertEqual((c["drops"], c["requeued"], c["verify_frame_ref"]), (1, True, None))
        self.assertEqual([q["event_id"] for q in e.broker.queues["camera-agent"]], ["c-1"], "event at the tail")
        return e

    def test_nc2_stale_frame(self):
        e = self._stale_scenario()
        # re-observe: same event resubmitted citing the head frame is admitted as rebased
        e.put("camera-agent", "c-1", lane="gui", based=e.broker.frame)
        self.assertEqual(e.broker.run(4)[-1], "VERIFIED")
        closes = e.events("close", event_id="c-1")
        self.assertEqual([c["verdict"] for c in closes], ["STALE_FRAME", "VERIFIED"])
        self.assertTrue(closes[-1]["rebased"])
        self.assertEqual(len(e.key_acts()), 2)

    def test_nc2_negative_control(self):
        self.red_without("_check_fresh", self._stale_scenario)

    def test_pixel_identical_frames_do_not_make_an_intent_stale(self):
        e = self.env()
        e.put("modeler-agent", "m-quiet", script="scene['col:CHAR_mesh'].append(1)")  # no paint(): same pixels
        e.put("camera-agent", "c-1", lane="gui", based=0)
        self.assertEqual(e.broker.run(4), ["STATE_VERIFIED", "PASS", "PASS", "VERIFIED"])
        c = e.events("close", event_id="c-1")[0]
        self.assertEqual((c["rebased"], c["dispatch_capture_seq"]), (True, 1))

    def test_three_drops_park_the_event(self):
        e = self.env()
        e.put("modeler-agent", "m-1")
        e.put("camera-agent", "c-1", lane="gui", based=0)
        e.broker.run(12)
        closes = e.events("close", event_id="c-1")
        self.assertEqual([c["drops"] for c in closes], [1, 2, 3])
        self.assertTrue(closes[-1]["parked"])
        self.assertEqual(e.broker.queues["camera-agent"], [])
        self.assertEqual(e.vision.acts, [])


class Timeouts(Base):
    # ---- 02 section 9, control 3: dead head
    def _dead_head_scenario(self, cls=B.Broker):
        e = self.env(cls=cls, with_bridge=False, cap=0.3)          # nobody answers for the head
        e.put("modeler-agent", "m-1")
        e.put("props-agent", "p-1", script="scene['col:PROPS'].append('p')", write_set=["col:PROPS"])
        t0 = time.monotonic()
        self.assertEqual(e.broker.step(), "TURN_TIMEOUT")
        self.assertLess(time.monotonic() - t0, 15)
        c = e.events("close", event_id="m-1")[0]
        self.assertEqual((c["verdict"], c["requeued"], c["drops"]), ("TURN_TIMEOUT", True, 1))
        self.assertEqual([n for n in os.listdir(os.path.join(e.rt, "inbox")) if n.endswith(".py")], [],
                         "the unanswered job is retracted")
        epoch = e.events("turn")[0]["turn_epoch"]
        self.assertGreater(e.broker.turn_epoch, epoch)
        # a late answer carrying the old turn arrives, then the bridge comes up
        mac = B.outbox_mac(NONCE, "m-1", "OK", hashlib.sha256(e.events("enqueue")[0]["script"].encode()).hexdigest())
        with open(os.path.join(e.rt, "outbox", "m-1.out"), "w") as f:
            f.write("OK\nMETA %s\n" % json.dumps({"changed": [], "mac": mac}))
        e.start_bridge()
        self.assertEqual(e.broker.step(), "STATE_VERIFIED")        # the next agent's turn opens
        self.assertEqual(e.events("turn")[1]["agent_id"], "props-agent")
        self.assertEqual(e.verdicts("m-1"), ["TURN_TIMEOUT", "LEASE_EXPIRED"], "late answer must be refused")
        late = e.events("close", event_id="m-1")[1]
        self.assertEqual((late["verdict"], late["turn_epoch"]), ("LEASE_EXPIRED", epoch))
        self.assertEqual(e.scene["col:CHAR_mesh"], [])
        self.assertNotIn("STATE_VERIFIED", e.verdicts("m-1"))
        return e

    def test_nc3_dead_head(self):
        e = self._dead_head_scenario()
        e.broker.run(3)                                             # m-1 gets its next turn with a live bridge
        self.assertEqual(e.verdicts("m-1"), ["TURN_TIMEOUT", "LEASE_EXPIRED", "STATE_VERIFIED"])
        self.assertEqual(e.scene["col:CHAR_mesh"], [1])

    def test_nc3_negative_control(self):
        self.red_without("_late_outbox_scan", self._dead_head_scenario)

    def test_two_timeouts_park_the_agent(self):
        e = self.env(with_bridge=False, cap=0.1)
        e.put("modeler-agent", "m-1")
        e.broker.run(5)
        self.assertEqual(e.verdicts("m-1"), ["TURN_TIMEOUT", "TURN_TIMEOUT"])
        self.assertEqual([(c["op"], c["agent_id"]) for c in e.events("control")], [("park", "modeler-agent")])
        e.start_bridge()
        e.broker.run(4)
        self.assertEqual(e.scene["col:CHAR_mesh"], [], "a parked agent is not dispatched")

    def test_outcome_uncertain_enters_reconcile_until_resume(self):
        e = self.env(cap=0.2)
        e.put("modeler-agent", "m-slow", script="time.sleep(0.8); scene['col:CHAR_mesh'].append('late')")
        e.put("props-agent", "p-1", script="scene['col:PROPS'].append('p')", write_set=["col:PROPS"])
        self.assertEqual(e.broker.run(4), ["OUTCOME_UNCERTAIN"], "the run stops at the uncertain event")
        self.assertEqual(e.broker.mode, "RECONCILE")
        self.assertEqual(e.broker.step(), "RECONCILE")
        self.assertEqual(e.scene["col:PROPS"], [], "nothing is dispatched during RECONCILE")
        time.sleep(1.0)                                             # the job finishes late
        e.broker.control("resume", "director-agent", "effect classified from the frame")
        self.assertEqual(e.broker.step(), "STATE_VERIFIED")
        self.assertEqual(e.verdicts("m-slow"), ["OUTCOME_UNCERTAIN", "LEASE_EXPIRED"])
        self.assertEqual(e.scene["col:CHAR_mesh"], ["late"])       # it did run: uncertain was the honest verdict
        self.assertEqual(e.ran.count("m-slow"), 1, "no automatic repeat")


class Bridge(Base):
    def _nonce_scenario(self, split=None):
        rt = tempfile.mkdtemp(prefix="vpref_bridge_")
        self.addCleanup(shutil.rmtree, rt, True)
        os.makedirs(os.path.join(rt, "inbox"))
        hits = []
        for name in ("evil.py", "wrongnonce_ev-1.py", NONCE + "x_ev-2.py", "_ev-3.py"):
            with open(os.path.join(rt, "inbox", name), "w") as f:
                f.write("hit(%r)" % name)
        with open(os.path.join(rt, "inbox", NONCE + "_ok-1.py"), "w") as f:
            f.write("hit('good'); print('hello')")
        with open(os.path.join(rt, "inbox", NONCE + "_err-1.py"), "w") as f:
            f.write("raise RuntimeError('x')")
        old = bridge._split
        if split:
            bridge._split = split
        try:
            ran = bridge.poll_once(rt, NONCE, {"hit": hits.append}, log=lambda m: None)
        finally:
            bridge._split = old
        self.assertEqual(hits, ["good"], "only jobs carrying the nonce may run")
        self.assertEqual(sorted(ran), ["err-1", "ok-1"])
        self.assertEqual(len(os.listdir(os.path.join(rt, "inbox", "rejected"))), 4)
        self.assertEqual(sorted(os.listdir(os.path.join(rt, "outbox"))), ["err-1.out", "ok-1.out"])
        ok = _read(os.path.join(rt, "outbox", "ok-1.out")).split("\n")
        self.assertEqual(ok[0], "OK")
        self.assertTrue(ok[1].startswith("META "))
        self.assertIn("hello", ok[2])
        self.assertTrue(_read(os.path.join(rt, "outbox", "err-1.out")).startswith("ERROR\n"))

    def test_nonce_gate(self):
        self._nonce_scenario()

    def test_negative_control_nonce_gate(self):
        def no_check(name, nonce):
            return name[:-3].replace("_", "-") or "x"
        with self.assertRaises(AssertionError):
            self._nonce_scenario(split=no_check)

    def test_empty_nonce_rejects_everything(self):
        rt = tempfile.mkdtemp(prefix="vpref_bridge_")
        self.addCleanup(shutil.rmtree, rt, True)
        os.makedirs(os.path.join(rt, "inbox"))
        with open(os.path.join(rt, "inbox", "_ev-1.py"), "w") as f:
            f.write("x=1")
        self.assertEqual(bridge.poll_once(rt, "", {}, log=lambda m: None), [])
        self.assertEqual(os.listdir(os.path.join(rt, "outbox")), [])


def _png(path, w, h, boxes):
    """Write a gray PNG (stdlib) that is black except for white boxes [(x,y,w,h)]."""
    rows = []
    for y in range(h):
        row = bytearray(w)
        for (bx, by, bw, bh) in boxes:
            if by <= y < by + bh:
                row[bx:bx + bw] = b"\xff" * bw
        rows.append(b"\x00" + bytes(row))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class Differ(Base):
    def test_diff_regions_with_real_ffmpeg(self):
        d = tempfile.mkdtemp(prefix="vpref_diff_")
        self.addCleanup(shutil.rmtree, d, True)
        a, b = os.path.join(d, "a.png"), os.path.join(d, "b.png")
        _png(a, 1920, 1080, [])
        _png(b, 1920, 1080, [(160, 240, 80, 40), (1600, 800, 64, 64)])
        self.assertEqual(B.diff_regions(a, a), [])
        self.assertEqual(B.diff_regions(a, b), [[160, 240, 80, 40], [1600, 800, 64, 64]])


if __name__ == "__main__":
    unittest.main()
