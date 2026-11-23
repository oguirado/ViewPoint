#!/usr/bin/env python3
"""ViewPoint reference turn-queue actor broker (Track P, Claude-written).

    VP_SWARM_NONCE=<nonce> broker.py run --job <vision job> --rt <RT> --turns N
    broker.py control --rt <RT> --op resume|stop|reconcile --by <who> --reason <text>

One total order over script and GUI events. Turns are NON-INTERACTIVE: agents drop
intent files in RT/intents/<agent_id>/<event_id>.json; on an agent's turn the broker
takes its oldest queued intent, admits it from broker state only, dispatches it (bridge
inbox for lane script, `vision act` for lane gui), closes it with a verdict and captures
the next frame. The broker is the only writer of RT/events.jsonl, RT/cursor.jsonl and
RT/frames, and the only caller of vision look/act. Stdlib only.
"""
import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time

AGENTS = ("modeler-agent", "props-agent", "look-agent", "camera-agent")
GUI_AGENTS = ("camera-agent",)
VIEW_KEYS = ("KP_1", "KP_3", "KP_7", "KP_4", "KP_Add", "Home")  # measured OK (02 section 0)
FOOTPRINT = (0, 80, 1580, 900)       # viewport region where GUI effects paint
FOOTPRINT_SLACK = 64                 # vision reports block-aligned regions
HOVER_XY = "800,500"
QUEUE_MAX = 8
MAX_ACTS = 8
FRAME_RING = 16
MAX_DROPS = 3
JOB_CAP_S = 120.0
GUI_HOLD_MS = 60000
VISION = ("python3", "-I", os.path.expanduser("~/Documentos/vis-skill/vision"))
SCRIPT_SLOTS = {"modeler-agent": (1700, 200), "props-agent": (1700, 400),
                "look-agent": (1700, 600), "camera-agent": (1700, 800)}
VIEW_CENTRE = (FOOTPRINT[0] + FOOTPRINT[2] // 2, FOOTPRINT[1] + FOOTPRINT[3] // 2)
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,79}$")  # no "_": it separates the nonce
PARTIAL_GRACE_S = 2.0

# verdicts that end an event (anything else returns it to the tail of its queue)
REQUEUE = ("STALE_FRAME", "TURN_TIMEOUT", "ACTUATOR_BUSY")


def now_ms():
    return int(time.time() * 1000)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def outbox_mac(nonce, event_id, status, script_sha):
    msg = ("%s\n%s\n%s" % (event_id, status, script_sha)).encode()
    return hmac.new(nonce.encode(), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- vision + frame diff
class Vision:
    """Thin wrapper over the vision CLI for one job. look() -> (rc, dict); act(args) -> (rc, dict)."""

    def __init__(self, job, cwd=None, timeout_s=90):
        self.job, self.cwd, self.timeout_s = job, cwd, timeout_s

    def _run(self, verb, args):
        cmd = list(VISION) + [verb, "--job", self.job] + list(args)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            return 124, {"code": "BROKER_TIMEOUT"}
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = {"code": "UNPARSED", "stdout_tail": p.stdout[-300:], "stderr_tail": p.stderr[-300:]}
        return p.returncode, out

    def look(self):
        return self._run("look", [])

    def act(self, args):
        return self._run("act", args)


def _gray(png, w, h):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", png, "-vf", "scale=%d:%d:flags=area,format=gray" % (w, h),
                        "-f", "rawvideo", "-"], capture_output=True, timeout=30)
    if p.returncode != 0 or len(p.stdout) != w * h:
        raise RuntimeError("ffmpeg gray failed: %s" % p.stderr[-200:])
    return p.stdout


def diff_regions(png_a, png_b, size=(1920, 1080), cell=8, thresh=2, max_regions=32):
    """Changed regions [[x,y,w,h],...] between two PNGs (ffmpeg downscale + connected components)."""
    gw, gh = size[0] // cell, size[1] // cell
    a, b = _gray(png_a, gw, gh), _gray(png_b, gw, gh)
    hot = set(i for i in range(gw * gh) if abs(a[i] - b[i]) > thresh)
    regions = []
    while hot:
        seed = hot.pop()
        stack = [seed]
        x0 = x1 = seed % gw
        y0 = y1 = seed // gw
        while stack:
            i = stack.pop()
            x, y = i % gw, i // gw
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    j = ny * gw + nx
                    if 0 <= nx < gw and 0 <= ny < gh and j in hot:
                        hot.discard(j)
                        stack.append(j)
        regions.append([x0 * cell, y0 * cell, (x1 - x0 + 1) * cell, (y1 - y0 + 1) * cell])
    regions.sort(key=lambda r: (r[1], r[0]))
    if len(regions) > max_regions:
        x0 = min(r[0] for r in regions); y0 = min(r[1] for r in regions)
        x1 = max(r[0] + r[2] for r in regions); y1 = max(r[1] + r[3] for r in regions)
        regions = [[x0, y0, x1 - x0, y1 - y0]]
    return regions


def _inside(r, box, slack=0):
    return (r[0] >= box[0] - slack and r[1] >= box[1] - slack and
            r[0] + r[2] <= box[0] + box[2] + slack and r[1] + r[3] <= box[1] + box[3] + slack)


# ---------------------------------------------------------------- broker
class BrokerError(Exception):
    pass


class Broker:
    def __init__(self, rt, job, nonce, vision=None, agents=AGENTS, differ=diff_regions,
                 job_cap_s=JOB_CAP_S, poll_s=0.1, log=None):
        if not nonce:
            raise BrokerError("nonce required (env VP_SWARM_NONCE)")
        self.rt, self.job, self.nonce = rt, job, nonce
        self.vision = vision or Vision(job, cwd=rt)
        self.agents = tuple(agents)
        self.differ = differ
        self.job_cap_s, self.poll_s = job_cap_s, poll_s
        self.log = log or (lambda m: None)
        self.p_events = os.path.join(rt, "events.jsonl")
        self.p_cursor = os.path.join(rt, "cursor.jsonl")
        self.d_inbox, self.d_outbox = os.path.join(rt, "inbox"), os.path.join(rt, "outbox")
        self.d_frames, self.d_intents = os.path.join(rt, "frames"), os.path.join(rt, "intents")
        for d in (self.d_inbox, self.d_outbox, self.d_frames, os.path.join(self.d_inbox, "retracted"),
                  os.path.join(self.d_outbox, "late")) + tuple(os.path.join(self.d_intents, a) for a in self.agents):
            os.makedirs(d, exist_ok=True)
        # single writer: one broker per RT
        self._lock = open(os.path.join(rt, "broker.lock"), "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise BrokerError("another broker holds %s/broker.lock" % rt)
        self.claims = self._load_claims()
        self.turn = 0            # number of turns opened so far
        self.turn_epoch = 0      # +1 per turn, +1 extra per timeout
        self.tick = 0            # +1 per closed dispatched event
        self.frame = -1          # current frame number
        self.frame_png = None
        self.mode = "IDLE"       # IDLE | RECONCILE | FROZEN
        self.queues = {a: [] for a in self.agents}
        self.status = {}         # event_id -> "queued" | "closed"
        self.drops = {}          # event_id -> int
        self.seen = set()        # (agent_id, intent_sig) already ingested
        self.full_reported = set()
        self.expired = {}        # event_id -> turn_epoch whose lease expired without evidence
        self.timeouts_row = {a: 0 for a in self.agents}
        self.parked = set()
        self.seq = 0
        self._recover()
        if self.frame < 0:
            self._capture(None, None)

    # ---- persistence
    def _load_claims(self):
        p = os.path.join(self.rt, "claims.json")
        try:
            with open(p) as f:
                c = json.load(f)
        except FileNotFoundError:
            return {}
        return {a: [str(x) for x in v] for a, v in c.items() if isinstance(v, list)}

    def _append(self, rec):
        rec = dict(rec)
        rec.setdefault("v", 1)
        self.seq += 1
        rec["seq"] = self.seq
        rec.setdefault("ts_ms", now_ms())
        line = json.dumps(rec, sort_keys=True) + "\n"
        with open(self.p_events, "a") as f:
            f.write(line)
            f.flush()
        if rec.get("rec") == "cursor":
            with open(self.p_cursor, "a") as f:
                f.write(line)
        return rec

    def _cursor(self, ev, phase):
        x, y = SCRIPT_SLOTS.get(ev["agent_id"], (1700, 1000)) if ev["lane"] == "script" else VIEW_CENTRE
        self._append({"rec": "cursor", "agent_id": ev["agent_id"], "event_id": ev["event_id"],
                      "phase": phase, "x": x, "y": y, "frame": self.frame})

    def _recover(self):
        """Rebuild state from events.jsonl (the order of record) and RT/frames after a restart."""
        if os.path.exists(self.p_events):
            with open(self.p_events) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    self.seq = max(self.seq, r.get("seq", 0))
                    kind = r.get("rec")
                    if kind == "enqueue":
                        a, eid = r.get("agent_id"), r.get("event_id")
                        if a in self.queues:
                            # a resubmit (re-observe after STALE_FRAME) replaces the queued event
                            self.queues[a] = [e for e in self.queues[a] if e["event_id"] != eid] + [r]
                            self.status[eid] = "queued"
                            self.seen.add((a, r.get("intent_sig")))
                    elif kind == "turn":
                        self.turn = max(self.turn, r.get("turn", 0))
                        self.turn_epoch = max(self.turn_epoch, r.get("turn_epoch", 0))
                    elif kind == "close":
                        self.tick = max(self.tick, r.get("tick", 0))
                        self.turn_epoch = max(self.turn_epoch, r.get("turn_epoch_after", 0))
                        eid, a = r.get("event_id"), r.get("agent_id")
                        if r.get("ingest"):
                            if r.get("verdict") != "QUEUE_FULL":
                                self.seen.add((a, r.get("intent_sig")))
                            continue
                        if eid is None or a not in self.queues or r.get("verdict") == "LEASE_EXPIRED":
                            continue
                        q = self.queues[a]
                        ev = next((e for e in q if e["event_id"] == eid), None)
                        if ev is None:
                            continue
                        q.remove(ev)
                        self.drops[eid] = r.get("drops", 0)
                        if r.get("requeued"):
                            q.append(ev)
                        else:
                            self.status[eid] = "closed"
                    elif kind == "control":
                        op = r.get("op")
                        if op == "reconcile":
                            self.mode = "RECONCILE"
                        elif op == "stop":
                            self.mode = "FROZEN"
                        elif op == "resume":
                            self.mode = "IDLE"
                        elif op == "park" and r.get("agent_id"):
                            self.parked.add(r["agent_id"])
        nums = [int(m.group(1)) for m in (re.match(r"^frame_(\d+)\.json$", n) for n in os.listdir(self.d_frames)) if m]
        if nums:
            self.frame = max(nums)
            self.frame_png = os.path.join(self.d_frames, "frame_%d.png" % self.frame)

    # ---- frames
    def _capture(self, by_event, regions):
        """vision look -> RT/frames/frame_<N>.{png,json}. regions=None means: diff against the previous frame."""
        rc, out = self.vision.look()
        if rc != 0 or not out.get("frame") or not os.path.exists(out["frame"]):
            raise BrokerError("vision look failed rc=%s code=%s" % (rc, out.get("code")))
        n = self.frame + 1
        png = os.path.join(self.d_frames, "frame_%d.png" % n)
        shutil.copyfile(out["frame"], png + ".tmp")
        os.replace(png + ".tmp", png)
        with open(png, "rb") as f:
            sha = sha256_bytes(f.read())
        exact = True
        if regions is None:
            regions = []
            if self.frame_png and os.path.exists(self.frame_png):
                with open(self.frame_png, "rb") as f:
                    prev_sha = sha256_bytes(f.read())
                if prev_sha != sha:
                    try:
                        regions = self.differ(self.frame_png, png)
                    except Exception as e:  # differ unavailable: declare the whole screen, flagged
                        self.log("differ failed: %s" % e)
                        regions, exact = [[0, 0, out.get("width", 1920), out.get("height", 1080)]], False
        rec = {"v": 1, "frame": n, "png": png, "sha256": sha, "ts_ms": now_ms(),
               "changed_regions": regions, "by_event": by_event}
        if not exact:
            rec["regions_exact"] = False
        tmp = os.path.join(self.d_frames, "frame_%d.json.tmp" % n)
        with open(tmp, "w") as f:
            json.dump(rec, f, sort_keys=True)
        os.replace(tmp, os.path.join(self.d_frames, "frame_%d.json" % n))
        self.frame, self.frame_png = n, png
        return rec

    # ---- INTENT phase: ingest intent files
    def _validate(self, agent, it):
        """Return (verdict, reason) for a malformed/spoofed intent, or None."""
        if not isinstance(it, dict) or it.get("v") != 1 or it.get("rec") != "enqueue":
            return "BAD_INTENT", "not a v1 enqueue record"
        eid = it.get("event_id")
        if not isinstance(eid, str) or not EVENT_ID_RE.match(eid):
            return "BAD_INTENT", "event_id must match %s" % EVENT_ID_RE.pattern
        if it.get("agent_id") != agent:
            return "NO_LEASE", "agent_id %r written under intents/%s" % (it.get("agent_id"), agent)
        if it.get("lane") not in ("script", "gui"):
            return "BAD_INTENT", "lane must be script or gui"
        if not isinstance(it.get("based_on_frame"), int) or isinstance(it.get("based_on_frame"), bool):
            return "BAD_INTENT", "based_on_frame must be an integer"
        ws = it.get("write_set", [])
        if not isinstance(ws, list) or not all(isinstance(x, str) for x in ws):
            return "BAD_INTENT", "write_set must be a list of strings"
        if it["lane"] == "script" and not isinstance(it.get("script"), str):
            return "BAD_INTENT", "lane script needs script source"
        if it["lane"] == "gui":
            k = it.get("keys")
            if not isinstance(k, list) or not k or not all(isinstance(x, str) for x in k):
                return "BAD_INTENT", "lane gui needs a non-empty keys list"
        return None

    def _refuse_ingest(self, agent, eid, verdict, reason, sig):
        self._append({"rec": "close", "tick": self.tick, "turn": self.turn, "event_id": eid, "agent_id": agent,
                      "verdict": verdict, "reason": reason, "ingest": True, "intent_sig": sig,
                      "drops": self.drops.get(eid, 0)})

    def ingest(self):
        for agent in self.agents:
            d = os.path.join(self.d_intents, agent)
            entries = []
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if name.endswith(".json") and os.path.isfile(p):
                    entries.append((os.stat(p).st_mtime_ns, name, p))
            for mtime_ns, name, p in sorted(entries):
                try:
                    with open(p, "rb") as f:
                        raw = f.read()
                except OSError:
                    continue
                sig = sha256_bytes(raw)
                if (agent, sig) in self.seen:
                    continue
                stem = name[:-5]
                try:
                    it = json.loads(raw.decode("utf-8"))
                except ValueError:
                    if time.time() - mtime_ns / 1e9 < PARTIAL_GRACE_S:
                        continue  # maybe still being written
                    self.seen.add((agent, sig))
                    self._refuse_ingest(agent, stem if EVENT_ID_RE.match(stem) else None, "BAD_INTENT", "unparseable JSON", sig)
                    continue
                bad = self._validate(agent, it)
                if bad:
                    self.seen.add((agent, sig))
                    eid = it.get("event_id") if isinstance(it, dict) and isinstance(it.get("event_id"), str) else None
                    self._refuse_ingest(agent, eid, bad[0], bad[1], sig)
                    continue
                eid = it["event_id"]
                q = self.queues[agent]
                resubmit = False
                if eid in self.status:  # IDEMPOTENCY: an event_id is applied at most once
                    old = next((e for e in q if e["event_id"] == eid), None)
                    rebase = (old is not None and old["lane"] == "gui" and it["lane"] == "gui"
                              and self.drops.get(eid, 0) > 0 and it["based_on_frame"] > old["based_on_frame"])
                    if not rebase:
                        self.seen.add((agent, sig))
                        self._refuse_ingest(agent, eid, "DUPLICATE_EVENT", "event_id already %s" % self.status[eid], sig)
                        continue
                    resubmit = True  # re-observe after STALE_FRAME: same event, newer based_on_frame
                elif len(q) >= QUEUE_MAX:  # bounded FIFO: refuse, never overwrite
                    if (agent, sig) not in self.full_reported:
                        self.full_reported.add((agent, sig))
                        self._refuse_ingest(agent, eid, "QUEUE_FULL", "agent FIFO holds %d events" % len(q), sig)
                    continue
                self.seen.add((agent, sig))
                rec = dict(it)
                rec.update({"rec": "enqueue", "v": 1, "intent_sig": sig, "ts_ms": now_ms(),
                            "action_class": "state_job" if it["lane"] == "script" else "view_change",
                            "kind": it.get("kind") or ("job" if it["lane"] == "script" else "key"),
                            "max_acts": 1 if it["lane"] == "script" else MAX_ACTS})
                if it["lane"] == "script":
                    rec["script_sha256"] = sha256_bytes(it["script"].encode())
                if resubmit:
                    rec["resubmit"] = True
                rec = self._append(rec)
                if resubmit:
                    q[:] = [e for e in q if e["event_id"] != eid] + [rec]
                else:
                    q.append(rec)
                self.status[eid] = "queued"
                self._cursor(rec, "intent")

    # ---- ADMIT phase: broker state only. Each check returns (verdict, reason) or None.
    def _check_claim(self, ev):
        if ev["lane"] == "script":
            claim = set(self.claims.get(ev["agent_id"], []))
            ws = ev.get("write_set") or []
            if not ws or not set(ws) <= claim:
                return "CLAIM_REFUSED", "write_set %s outside claim %s" % (sorted(ws), sorted(claim))
        else:
            keys = ev["keys"]
            if len(keys) > MAX_ACTS or any(k not in VIEW_KEYS for k in keys):
                return "CLAIM_REFUSED", "gui keys must be at most %d of %s" % (MAX_ACTS, list(VIEW_KEYS))
        return None

    def _check_lease(self, ev):
        if ev["lane"] == "gui" and ev["agent_id"] not in GUI_AGENTS:
            return "NO_LEASE", "the GUI actuator is leased to %s only" % list(GUI_AGENTS)
        return None

    def _check_fresh(self, ev):
        if ev["lane"] != "gui":
            return None  # STALE_FRAME applies to GUI intents only; claims keep script write sets disjoint
        b = ev["based_on_frame"]
        if b > self.frame or b < self.frame - FRAME_RING:
            return "FRAME_EVICTED", "based_on_frame %d outside ring (%d..%d]" % (b, self.frame - FRAME_RING, self.frame)
        if b < self.frame:
            if self._pixels_identical_since(b):
                ev["auto_rebased"] = True  # the screen the agent saw is byte-identical to the head frame
                return None
            return "STALE_FRAME", "based_on_frame %d < head_frame %d" % (b, self.frame)
        return None

    def _pixels_identical_since(self, b):
        """True when every frame in [b, head] has the same PNG sha256 (events closed, no pixel changed)."""
        shas = set()
        for n in range(b, self.frame + 1):
            try:
                with open(os.path.join(self.d_frames, "frame_%d.json" % n)) as f:
                    shas.add(json.load(f)["sha256"])
            except (OSError, ValueError, KeyError):
                return False
        return len(shas) == 1

    def admit(self, ev):
        for chk in (self._check_lease, self._check_claim, self._check_fresh):
            r = chk(ev)
            if r:
                return r
        return None

    # ---- EXECUTE phase
    def _dispatch_script(self, ev):
        """Returns (verdict, extra). Hands the job to the bridge and waits for the outbox."""
        eid = ev["event_id"]
        name = "%s_%s.py" % (self.nonce, eid)
        out_path = os.path.join(self.d_outbox, eid + ".out")
        self.expired.pop(eid, None)
        if os.path.exists(out_path):  # an answer that precedes the dispatch cannot be the bridge's
            os.replace(out_path, os.path.join(self.d_outbox, "late", "%d_%s.out" % (time.time_ns(), eid)))
        tmp = os.path.join(self.d_inbox, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(ev["script"])
        inbox_path = os.path.join(self.d_inbox, name)
        os.replace(tmp, inbox_path)
        deadline = time.monotonic() + self.job_cap_s
        while not os.path.exists(out_path):
            if time.monotonic() >= deadline:
                try:  # retract atomically; the bridge claims jobs by rename, so exactly one side wins
                    os.rename(inbox_path, os.path.join(self.d_inbox, "retracted", "%d_%s.py" % (time.time_ns(), eid)))
                except OSError:
                    return "OUTCOME_UNCERTAIN", {"reason": "bridge took the job but wrote no outbox within %.0f s" % self.job_cap_s}
                return "TURN_TIMEOUT", {"reason": "job not picked up within %.0f s; retracted, never ran" % self.job_cap_s}
            time.sleep(self.poll_s)
        with open(out_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
        status = lines[0].strip()
        meta = {}
        if len(lines) > 1 and lines[1].startswith("META "):
            try:
                meta = json.loads(lines[1][5:])
            except ValueError:
                meta = {}
        extra = {"job_ms": meta.get("ms"), "out_tail": "\n".join(lines[2:])[-400:]}
        want = outbox_mac(self.nonce, eid, status, ev["script_sha256"])
        if status not in ("OK", "ERROR") or not hmac.compare_digest(str(meta.get("mac", "")), want):
            extra["reason"] = "outbox not authenticated by the bridge (status %r)" % status[:20]
            return "OUTCOME_UNCERTAIN", extra
        if status == "ERROR":
            extra["reason"] = "job raised; 'unchanged' is not proven, re-observe"
            extra["changed_write_set"] = meta.get("changed")
            return "JOB_ERROR", extra
        changed = meta.get("changed")
        extra["changed_write_set"] = changed
        if changed is not None:
            spill = sorted(set(changed) - set(ev["write_set"]))
            if spill:  # declared write_set was inside the claim, the measured one is not
                extra["spill_write_set"] = spill
                extra["reason"] = "job changed collections outside its write_set"
                return "VERIFIED_WITH_SPILL", extra
        return "STATE_VERIFIED", extra

    def _dispatch_gui(self, ev):
        """Returns (verdict, extra). One hover, then one vision act per key."""
        fp = "%d,%d,%d,%d" % FOOTPRINT
        rc, _ = self.vision.act(["--xy", HOVER_XY])
        if rc == 6:
            return "ACTUATOR_BUSY", {"reason": "vision lock/budget on hover (rc 6)", "acts_done": 0}
        if rc not in (0, 3):
            return "INPUT_UNCERTAIN", {"reason": "hover rc %s" % rc, "acts_done": 0}
        regions, done = [], 0
        for key in ev["keys"]:
            rc, out = self.vision.act(["--key", key, "--expect-change", fp])
            if rc == 0:
                done += 1
                for r in out.get("changed_regions") or []:
                    regions.append([r["x"], r["y"], r["w"], r["h"]] if isinstance(r, dict) else list(r))
                continue
            extra = {"acts_done": done, "failed_key": key, "regions": regions}
            if rc == 3:
                extra["reason"] = "no visible change after %s (rc 3); remaining keys not sent" % key
                return "NO_VISIBLE_CHANGE", extra
            if rc == 6 and done == 0:
                extra["reason"] = "vision lock/budget (rc 6)"
                return "ACTUATOR_BUSY", extra
            extra["reason"] = "vision act rc %s on %s" % (rc, key)
            return "INPUT_UNCERTAIN", extra
        own = [r for r in regions if _inside(r, FOOTPRINT, FOOTPRINT_SLACK)]
        spill = [r for r in regions if not _inside(r, FOOTPRINT, FOOTPRINT_SLACK)]
        return ("VERIFIED_WITH_SPILL" if spill else "VERIFIED"), {"acts_done": done, "regions": regions,
                                                                  "own_regions": own, "spill_regions": spill}

    # ---- late evidence for expired leases
    def _late_outbox_scan(self):
        for eid, epoch in list(self.expired.items()):
            p = os.path.join(self.d_outbox, eid + ".out")
            if os.path.exists(p):
                with open(p, errors="replace") as f:
                    first = f.readline().strip()[:20]
                os.replace(p, os.path.join(self.d_outbox, "late", "%d_%s.out" % (time.time_ns(), eid)))
                del self.expired[eid]
                self._append({"rec": "close", "tick": self.tick, "turn": self.turn, "event_id": eid,
                              "verdict": "LEASE_EXPIRED", "turn_epoch": epoch, "turn_epoch_now": self.turn_epoch,
                              "reason": "late result (%s) for an expired turn; ignored" % first})

    # ---- one turn
    def step(self):
        """Run one turn. Returns the verdict string (or the queue mode if not IDLE)."""
        if self.mode != "IDLE":
            return self.mode
        self._late_outbox_scan()
        self.ingest()
        agent = self.agents[self.turn % len(self.agents)]
        self.turn += 1
        self.turn_epoch += 1
        q = self.queues[agent]
        ev = None if agent in self.parked or not q else q[0]
        hold = 0 if ev is None else (int(self.job_cap_s * 1000) if ev["lane"] == "script" else GUI_HOLD_MS)
        self._append({"rec": "turn", "turn": self.turn, "turn_epoch": self.turn_epoch, "agent_id": agent,
                      "head_frame": self.frame, "hold_ms": hold})
        if ev is None:
            self._append({"rec": "close", "tick": self.tick, "turn": self.turn, "event_id": None, "agent_id": agent,
                          "verdict": "PASS", "reason": "agent parked" if agent in self.parked else "no queued intent"})
            return "PASS"
        eid = ev["event_id"]
        close = {"rec": "close", "turn": self.turn, "event_id": eid, "agent_id": agent, "lane": ev["lane"],
                 "rebased": False, "dispatch_capture_seq": None, "verify_frame_ref": None,
                 "own_regions": [], "spill_regions": []}
        refusal = self.admit(ev)
        close["rebased"] = bool(ev.get("resubmit") or ev.get("auto_rebased"))
        if refusal:
            verdict, reason = refusal
            q.pop(0)
            requeued = False
            if verdict == "STALE_FRAME":
                self.drops[eid] = self.drops.get(eid, 0) + 1
                if self.drops[eid] < MAX_DROPS:
                    q.append(ev)
                    requeued = True
                else:
                    close["parked"] = True
            if not requeued:
                self.status[eid] = "closed"
            close.update({"tick": self.tick, "verdict": verdict, "reason": reason,
                          "drops": self.drops.get(eid, 0), "requeued": requeued})
            self._append(close)
            self._cursor(ev, "result")
            return verdict
        # admitted: DISPATCH
        dispatch_frame = self.frame
        self._cursor(ev, "act")
        if ev["lane"] == "script":
            verdict, extra = self._dispatch_script(ev)
        else:
            verdict, extra = self._dispatch_gui(ev)
        q.pop(0)
        requeued = False
        if verdict == "TURN_TIMEOUT":
            self.turn_epoch += 1
            self.expired[eid] = self.turn_epoch - 1
            self.drops[eid] = self.drops.get(eid, 0) + 1
            self.timeouts_row[agent] += 1
            if self.drops[eid] < MAX_DROPS:
                q.append(ev)
                requeued = True
        else:
            self.timeouts_row[agent] = 0
            if verdict == "ACTUATOR_BUSY":
                q.append(ev)
                requeued = True
            elif verdict == "OUTCOME_UNCERTAIN":
                self.expired[eid] = self.turn_epoch
        if not requeued:
            self.status[eid] = "closed"
        regions = extra.pop("regions", None)
        self.tick += 1
        frame_rec = self._capture(eid, regions if ev["lane"] == "gui" and regions is not None else None)
        own = extra.pop("own_regions", None)
        close.update({"tick": self.tick, "verdict": verdict, "dispatch_capture_seq": dispatch_frame,
                      "verify_frame_ref": frame_rec["frame"], "drops": self.drops.get(eid, 0), "requeued": requeued,
                      "turn_epoch_after": self.turn_epoch,
                      "own_regions": own if own is not None else frame_rec["changed_regions"]})
        close.update(extra)
        self._append(close)
        self._cursor(ev, "result")
        if verdict in ("OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN"):
            # RECONCILE: nothing is dispatched and nothing is repeated until a control resume
            self.mode = "RECONCILE"
            self._append({"rec": "control", "op": "reconcile", "by": "broker", "event_id": eid,
                          "reason": "%s on %s: classify the effect from frame %d, then resume" % (verdict, eid, self.frame)})
        elif self.timeouts_row[agent] >= 2 and agent not in self.parked:
            self.parked.add(agent)
            self._append({"rec": "control", "op": "park", "by": "broker", "agent_id": agent,
                          "reason": "two TURN_TIMEOUT in a row"})
        return verdict

    def run(self, turns):
        out = []
        for _ in range(turns):
            v = self.step()
            out.append(v)
            self.log("turn %d -> %s" % (self.turn, v))
            if self.mode != "IDLE":
                break
        return out

    def control(self, op, by, reason):
        if op not in ("stop", "resume", "reconcile"):
            raise BrokerError("unsupported control op %r" % op)
        self._append({"rec": "control", "op": op, "by": by, "reason": reason})
        self.mode = {"stop": "FROZEN", "resume": "IDLE", "reconcile": "RECONCILE"}[op]
        if op == "resume":
            self.turn_epoch += 1

    def close(self):
        self._lock.close()


class _NoVision:
    def look(self):
        return 9, {"code": "NO_VISION"}

    act = None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--job", required=True)
    r.add_argument("--rt", default=os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref"))
    r.add_argument("--turns", type=int, required=True)
    r.add_argument("--job-cap-s", type=float, default=JOB_CAP_S)
    c = sub.add_parser("control")
    c.add_argument("--rt", default=os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref"))
    c.add_argument("--op", required=True, choices=("stop", "resume", "reconcile"))
    c.add_argument("--by", required=True)
    c.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    nonce = os.environ.get("VP_SWARM_NONCE", "")
    try:
        if a.cmd == "control":
            if not os.path.exists(os.path.join(a.rt, "events.jsonl")):
                raise BrokerError("no events.jsonl under %s" % a.rt)
            b = Broker(a.rt, "-", nonce or "control-only", vision=_NoVision())
            b.control(a.op, a.by, a.reason)
            print(json.dumps({"ok": True, "mode": b.mode}))
            return 0
        b = Broker(a.rt, a.job, nonce, job_cap_s=a.job_cap_s, log=lambda m: print(m, file=sys.stderr))
        verdicts = b.run(a.turns)
        print(json.dumps({"ok": b.mode == "IDLE", "mode": b.mode, "turns": len(verdicts), "verdicts": verdicts,
                          "turn": b.turn, "tick": b.tick, "frame": b.frame,
                          "queued": {k: len(v) for k, v in b.queues.items()}}))
        return 0 if b.mode == "IDLE" else 3
    except BrokerError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
