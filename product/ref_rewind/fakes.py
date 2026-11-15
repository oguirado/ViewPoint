"""R0 fakes: a capsule without Blender or vision, for the tests of R1-R4.

Real parts: the F1 broker (product/ref_broker/broker.py) and the F1 bridge core (bridge.poll_once) on a
thread. Fake parts: the scene (a dict keyed by digest v2 keys), the vision (frame bytes are a function
of the scene, so "frame equals base frame" is decidable), the .blend (JSON of the scene), the
checkpoint store and the judge. The fake ops have the R1 signatures (mark_script, restore_script,
digest_script) and print the same RW_RESULT lines, so R2/R4 swap in R1's ops without other changes.

    sys.path.insert(0, <product/ref_rewind>); import fakes, schema as RW
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "ref_broker"))
import bridge  # noqa: E402  (F1, imported, never edited)
import broker as B  # noqa: E402
import schema as RW  # noqa: E402

NONCE = "n0ncerw"
CLAIMS = {"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS"],
          "look-agent": ["col:LOOK"], "camera-agent": ["col:CAM"]}
VIEW_REGION = {"x": 0, "y": 96, "w": 1568, "h": 864, "score": 1.0}
IN_FOOTPRINT = [0, 96, 16, 16]


def scene_digest(scene):
    """Fake digest v2: one hex per key of the scene dict."""
    return {k: hashlib.sha256(json.dumps(scene[k], sort_keys=True).encode()).hexdigest()[:16] for k in scene}


# ---------------------------------------------------------------- fake ops (R1 ops.py signatures)
def mark_script(point_path):
    return ("import json, os\n"
            "_p = %r\n"
            "os.makedirs(os.path.dirname(_p), exist_ok=True)\n"
            "with open(_p + '.tmp', 'w') as f: json.dump(scene, f, sort_keys=True)\n"
            "os.replace(_p + '.tmp', _p)\n"
            "print(rw_result({'op': 'mark', 'path': _p, 'bytes': os.path.getsize(_p), 'digest': digest()}))\n"
            % point_path)


def restore_script(point_path, load_ui=True):
    return ("import json\n"
            "with open(%r) as f: _s = json.load(f)\n"
            "_ui = scene.get('ui:view3d')\n"
            "scene.clear(); scene.update(_s)\n"
            "if not %r and _ui is not None: scene['ui:view3d'] = _ui\n"
            "print(rw_result({'op': 'restore', 'path': %r, 'load_ui': %r, 'digest': digest()}))\n"
            % (point_path, bool(load_ui), point_path, bool(load_ui)))


def noop_restore_script(point_path, load_ui=True):
    """Mutant for negative controls: answers like a restore but leaves the scene as it is."""
    return "print(rw_result({'op': 'restore', 'path': %r, 'load_ui': %r, 'digest': digest()}))\n" % (point_path, bool(load_ui))


def digest_script():
    return "print(rw_result({'op': 'digest', 'digest': digest()}))\n"


class FakeOps:
    """Same attribute names as the R1 ops module."""
    mark_script = staticmethod(mark_script)
    restore_script = staticmethod(restore_script)
    digest_script = staticmethod(digest_script)


# ---------------------------------------------------------------- fake vision
class FakeVision:
    """look() writes a 'PNG' whose bytes depend only on the scene (and on `noise`, to model
    non-scene pixels such as a status bar)."""

    def __init__(self, d, scene):
        self.d, self.scene, self.n = d, scene, 0
        self.acts, self.looks, self.noise = [], 0, 0
        self.key_rc, self.hover_rc = {}, 0
        self.regions = [VIEW_REGION]

    def look(self):
        self.looks += 1
        self.n += 1
        p = os.path.join(self.d, "shot_%d.png" % self.n)
        with open(p, "wb") as f:
            f.write(("FAKEPNG %s noise=%d" % (json.dumps(self.scene, sort_keys=True), self.noise)).encode())
        return 0, {"frame": p, "width": 1920, "height": 1080}

    def act(self, args):
        self.acts.append(list(args))
        if args[0] == "--xy":
            return self.hover_rc, {}
        rc = self.key_rc.get(args[1], 0)
        if rc == 0:   # a view key changes the 3D view, which is inside digest v2
            self.scene.setdefault("ui:view3d", []).append(args[1])
            return 0, {"changed_regions": list(self.regions)}
        return rc, {"changed_regions": []}


def fake_differ(png_a, png_b):
    """Stand-in for broker.diff_regions: [] iff the two fake frames have equal bytes."""
    with open(png_a, "rb") as fa, open(png_b, "rb") as fb:
        return [] if fa.read() == fb.read() else [list(IN_FOOTPRINT)]


# ---------------------------------------------------------------- fake checkpoint store (R1 store.py interface)
class FakeStore:
    """Interface R2/R4 code against (R1's Store implements the same methods, plus ring/budget/dedup):
      path_for(point_id) -> str                    where the mark job must write the .blend
      admit(point_id, tick, scene_epoch, digest, frame, pinned=()) -> rec:ckpt dict (not yet appended)
            raises RW.CkptError(CKPT_MISSING | CKPT_BUDGET)
      verify(point_id) -> path                     sha256 re-checked; raises CkptError(CKPT_CORRUPT | CKPT_MISSING)
      get(point_id) -> rec:ckpt dict | None
      points() -> [point_id, ...] oldest first
    The fake keeps every point (no ring)."""

    def __init__(self, rt, slots=RW.RING_SLOTS, budget_bytes=None, check_sha=True):
        self.dir = os.path.join(rt, RW.CKPT_DIRNAME)
        os.makedirs(self.dir, exist_ok=True)
        self.slots, self.budget_bytes, self.check_sha = slots, budget_bytes, check_sha
        self._recs = {}

    def path_for(self, point_id):
        if not RW.POINT_ID_RE.match(point_id):
            raise RW.SchemaError("BAD_POINT", point_id)
        return os.path.join(self.dir, point_id + ".blend")

    def admit(self, point_id, tick, scene_epoch, digest, frame, pinned=()):
        p = self.path_for(point_id)
        if not os.path.isfile(p):
            raise RW.CkptError(RW.CKPT_MISSING, p)
        n = os.path.getsize(p)
        if self.budget_bytes is not None and n + sum(r["bytes"] for r in self._recs.values()) > self.budget_bytes:
            raise RW.CkptError(RW.CKPT_BUDGET, "%d bytes over budget" % n)
        rec = RW.ckpt_rec(point_id, tick, scene_epoch, RW.sha256_file(p), n, digest, frame)
        self._recs[point_id] = rec
        return dict(rec)

    def verify(self, point_id):
        rec = self._recs.get(point_id)
        p = self.path_for(point_id)
        if rec is None or not os.path.isfile(p):
            raise RW.CkptError(RW.CKPT_MISSING, point_id)
        if self.check_sha and RW.sha256_file(p) != rec["blend_sha256"]:   # SHA CHECK
            raise RW.CkptError(RW.CKPT_CORRUPT, point_id)
        return p

    def get(self, point_id):
        r = self._recs.get(point_id)
        return dict(r) if r else None

    def points(self):
        return list(self._recs)


# ---------------------------------------------------------------- fake judge (R3 judge signature)
def fake_judge(attempt, base, after, run_probe=None, differ=fake_differ):
    """judge(attempt, base, after, run_probe) -> rec:judge.
      attempt = {"attempt": "<event_id>.a<k>", "intent": <enqueue record>, "script": str,
                 "verdict": <F1 verdict>, "extra": <F1 extra>}
      base    = {"point_id", "digest", "frame_png"}
      after   = {"digest", "frame_png", "job_ms"}
      run_probe(script) -> {"ok": bool, "result": <json>, "digest": {...}}   (bridge round trip)
    Oracles in the fixed order RW.ORACLES; an oracle that does not apply is omitted."""
    it = attempt["intent"]
    oracles = []
    ex = it.get("expect")
    if isinstance(ex, dict) and run_probe is not None:
        pr = run_probe(ex["probe"])
        if not pr.get("ok"):
            oracles.append(RW.oracle("expect", False, RW.PROBE_FAILED))
        elif not RW.digest_match(pr.get("digest"), after["digest"]):
            oracles.append(RW.oracle("expect", False, RW.PROBE_NOT_READONLY))
        else:
            oracles.append(RW.oracle("expect", pr.get("result") == ex["equals"], "got %s" % json.dumps(pr.get("result"))))
    delta = RW.digest_delta(base["digest"], after["digest"])
    out = RW.delta_outside(delta, it)
    oracles.append(RW.oracle("delta", bool(delta) and not out, "delta=%s outside=%s" % (delta, out)))
    if base.get("frame_png") and after.get("frame_png"):
        oracles.append(RW.oracle("witness", bool(differ(base["frame_png"], after["frame_png"])), "frame changed"))
    oracles.append(RW.oracle("cost", True, "job_ms=%s" % after.get("job_ms")))
    pick = all(o["ok"] for o in oracles if o["name"] != "cost")
    return RW.judge_rec(attempt["attempt"], pick, oracles, judge_version="fake-" + RW.JUDGE_VERSION)


def accept_all_judge(attempt, base, after, run_probe=None, differ=fake_differ):
    """Mutant for negative controls (dead oracle)."""
    return RW.judge_rec(attempt["attempt"], True, [RW.oracle("delta", True, "mutant: accept all")], judge_version="mutant")


# ---------------------------------------------------------------- the fake capsule
class Capsule:
    """Temp RT + real F1 broker (or a subclass) + real bridge core on a thread + fake scene/vision."""

    def __init__(self, cls=B.Broker, with_bridge=True, cap=5.0, nonce=NONCE, rt=None, broker_kw=None):
        self.rt = rt or tempfile.mkdtemp(prefix="vprw_test_")
        self._own_rt = rt is None
        self.shots = os.path.join(self.rt, "_shots")
        os.makedirs(self.shots, exist_ok=True)
        with open(os.path.join(self.rt, "claims.json"), "w") as f:
            json.dump(CLAIMS, f)
        self.scene = {c[0]: [] for c in CLAIMS.values()}
        self.scene["ui:view3d"] = ["persp"]
        self.vision = FakeVision(self.shots, self.scene)
        self.ran = []
        self._stop = threading.Event()
        self.thread = None
        self.cls, self.cap, self.nonce = cls, cap, nonce
        self.broker_kw = dict(broker_kw or {})
        self.broker = self.new_broker()
        if with_bridge:
            self.start_bridge()

    def new_broker(self):
        return self.cls(self.rt, "fakejob", self.nonce, vision=self.vision, job_cap_s=self.cap, poll_s=0.01,
                        differ=fake_differ, **self.broker_kw)

    def restart_broker(self):
        self.broker.close()
        self.broker = self.new_broker()
        return self.broker

    def digest(self):
        return scene_digest(self.scene)

    def start_bridge(self, nonce=None):
        ns = {"scene": self.scene, "time": time, "digest": self.digest, "rw_result": RW.result_line}
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                self.ran += bridge.poll_once(self.rt, nonce or self.nonce, ns, digest=self.digest, log=lambda m: None)
                time.sleep(0.01)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def stop_bridge(self):
        self._stop.set()
        if self.thread:
            self.thread.join(2)
            self.thread = None

    def put(self, agent, eid, lane="script", script="scene['col:CHAR_mesh'].append(1)",
            write_set=("col:CHAR_mesh",), keys=None, based=None, dir_agent=None, **extra):
        it = {"v": 1, "rec": "enqueue", "event_id": eid, "agent_id": agent, "lane": lane,
              "kind": "job" if lane == "script" else "key",
              "based_on_frame": self.broker.frame if based is None else based,
              "write_set": list(write_set), "expect": "test"}
        if lane == "script":
            it["script"] = script
        else:
            it["keys"] = list(keys or ["KP_1"])
        it.update(extra)
        d = os.path.join(self.rt, "intents", dir_agent or agent)
        with open(os.path.join(d, eid + ".tmp"), "w") as f:
            json.dump(it, f)
        os.replace(os.path.join(d, eid + ".tmp"), os.path.join(d, eid + ".json"))
        time.sleep(0.002)   # distinct mtimes -> deterministic FIFO order
        return it

    def control(self, kind, script, *parts):
        """Dispatch one control job through the real bridge; returns RW.dispatch_control's dict."""
        return RW.dispatch_control(self.broker, RW.control_job_id(kind, *parts), script)

    def events(self, rec=None, **match):
        out = []
        for r in RW.read_events(os.path.join(self.rt, "events.jsonl")):
            if rec and r.get("rec") != rec:
                continue
            if all(r.get(k) == v for k, v in match.items()):
                out.append(r)
        return out

    def verdicts(self, eid):
        return [r["verdict"] for r in self.events("close", event_id=eid)]

    def close(self):
        self.stop_bridge()
        self.broker.close()
        if self._own_rt:
            shutil.rmtree(self.rt, ignore_errors=True)
