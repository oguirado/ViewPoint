#!/usr/bin/env python3
"""ViewPoint capsule reference projection (Track P, Claude-written).

Tails the broker's RT/events.jsonl and projects turn-level records to
  * an OpenSwitchboard room, through the osb CLI, and
  * a TaskLight room, through the context-wall MCP server (stdio JSON-RPC).

The broker's JSONL is the order of record; both rooms are projections. This
process never writes events.jsonl. Stdlib only.
"""
import argparse
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

# External checkouts (not vendored): OpenSwitchboard CLI and the TaskLight/context-wall repo.
OSB = os.environ.get("VP_OSB_CLI", "switchboard-skill/bin/osb")
CW_REPO = os.environ.get("VP_CW_REPO", "FireBase_BuildHybridAI/repo")
MCP_CMD = ["bash", "-c", "cd %s && PYTHONPATH=%s exec python3 -m bridge.room.mcp" % (CW_REPO, CW_REPO)]

VERIFIED = {"VERIFIED", "VERIFIED_WITH_SPILL", "STATE_VERIFIED"}
# The event returns to the ring tail: its step is still open.
REQUEUED = {"STALE_FRAME", "TURN_TIMEOUT"}
# The refusal is about a submission, not about the queued event with that id:
# it must never change the state of an existing step.
SUBMISSION = {"QUEUE_FULL", "DUPLICATE_EVENT"}
KNOWN_VERDICTS = VERIFIED | REQUEUED | SUBMISSION | {
    "NO_VISIBLE_CHANGE", "INPUT_UNCERTAIN", "JOB_ERROR", "OUTCOME_UNCERTAIN", "FRAME_EVICTED",
    "NO_LEASE", "LEASE_EXPIRED", "ACTUATOR_BUSY", "FOOTPRINT_MISSING", "CLAIM_REFUSED", "PASS"}

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CLAIM = re.compile(r"^col:[A-Za-z0-9_.-]{1,60}$")
MAX_STEPS, STEP_TEXT, ACCEPT, OUTCOME, TITLE = 12, 80, 160, 240, 120
DONE_STATES = {"done", "blocked"}
MAX_ANOMALIES = 200
MAX_LINE_BYTES = 1 << 20  # a broker record is a few hundred bytes; larger lines are not parsed
# TaskLight tools whose call is a full snapshot: a later acknowledged call of the
# same tool for the same agent supersedes an unacked older one (never replay it).
MCP_SNAPSHOT = {"post_plan", "post_status"}
OSB_ATTEMPTS = 3
OSB_BACKOFF_S = 0.2
# A transport failure (timeout, dead pipe) leaves delivery unknown. These
# tools set state (same call twice = same card), so one retry on a fresh
# server is safe. post_status appends a heartbeat line: a retry could show it
# twice, and the next status supersedes a lost one, so it is never retried.
MCP_RETRY_SAFE = {"post_plan", "post_step", "close_task"}


class EventsFileChanged(Exception):
    """events.jsonl is not the file this projection state was built from."""


def clip(s, n):
    """One printable line of at most n characters.

    Records are untrusted: a NUL makes subprocess raise, a lone surrogate
    cannot be encoded for argv or JSON-RPC, bidi/control characters would
    rewrite how a room line reads. All of them become spaces or "?".
    """
    s = str(s).encode("utf-8", "replace").decode("utf-8")
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def to_int(v, default=0):
    """int for counters read from a record; anything else is the default."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        return int(v)
    except (ValueError, OverflowError):  # NaN, Infinity
        return default


def rt_root():
    return os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref")


# ---------------------------------------------------------------- sinks

class Ledger:
    """Append-only JSONL of every outbound call and its response (the evidence)."""

    def __init__(self, path=None):
        self.path = path
        self.rows = []
        # Idempotency keys of calls a sink already acknowledged, read back
        # from disk: a restarted projector redoes a record but never repeats
        # a call the ledger proves was delivered (redo is a no-op).
        self.acked = set()
        # Durable outbox: keyed calls that failed and were never acknowledged
        # later, in ledger order. A restart retries them (retry_outbox).
        self.pending = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except (ValueError, RecursionError):
                        continue  # torn last line of a killed run
                    if isinstance(row, dict):
                        self._note(row)

    def _note(self, row):
        ok = row.get("rc") == 0 if row.get("sink") == "osb" else row.get("ok") is True
        if not isinstance(row.get("key"), str):
            return
        k = (row.get("sink"), row.get("room"), row["key"])
        if ok:
            self.acked.add(k)
            self.pending.pop(k, None)
            if row.get("sink") == "tasklight" and row.get("tool") in MCP_SNAPSHOT:
                for k2 in [k2 for k2, r in self.pending.items()
                           if r.get("sink") == "tasklight" and r.get("room") == row.get("room")
                           and r.get("tool") == row.get("tool") and r.get("agent") == row.get("agent")]:
                    del self.pending[k2]
        elif k not in self.acked:
            self.pending[k] = row

    def is_acked(self, sink, room, key):
        return key is not None and (sink, room, key) in self.acked

    def add(self, row):
        self.rows.append(row)
        self._note(row)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")


class OsbSink:
    """Posts acts with the osb CLI. A refute without a ref is refused here.

    Delivery: up to OSB_ATTEMPTS tries. Before every retry the room is read
    back: if the act is already there (the CLI timed out or failed after the
    daemon took it) it counts as delivered and is not posted again.
    """

    def __init__(self, room, ledger, osb=OSB, python=("python3", "-I"), timeout=30,
                 attempts=OSB_ATTEMPTS, backoff=OSB_BACKOFF_S, verify_before_retry=True):
        if not SLUG.match(room):
            raise ValueError("bad room %r" % room)
        self.room, self.ledger, self.timeout = room, ledger, timeout
        self.argv0 = list(python) + [osb]
        self.attempts, self.backoff, self.verify = max(1, attempts), backoff, verify_before_retry
        self.errors = self.replayed = self.retries = 0
        # Room clock below which nothing can be an act of this process: the
        # room head read before the first post, then every acknowledged or
        # deduplicated act. None = not read yet; False = could not be read.
        self.lamport = None

    def _run(self, args):
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
        try:
            p = subprocess.run(self.argv0 + args, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=self.timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except (subprocess.TimeoutExpired, OSError, ValueError) as ex:
            return -1, "", repr(ex)

    def _landed(self, agent, act, text):
        """True when the room holds this act above the baseline, i.e. posted
        by this process after its last acknowledged act. History of earlier
        runs (same agent, act and text) never counts as delivered. Without a
        baseline nothing is assumed delivered: a duplicate beats a lost act."""
        if self.lamport is None or self.lamport is False:
            return False
        for ev in self._read(self.lamport) or []:
            if ev.get("agent") == agent and ev.get("act") == act and ev.get("text") == text:
                self.lamport = max(self.lamport, to_int(ev.get("lamport"), 0))
                return True
        return False

    def _read(self, since):
        rc, out, _ = self._run(["read", "--room", self.room, "--json", "--since", str(since)])
        if rc != 0:
            return None
        evs = []
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(ev, dict):
                evs.append(ev)
        return evs

    def _baseline(self):
        evs = self._read(0)
        self.lamport = False if evs is None else max([to_int(e.get("lamport"), 0) for e in evs] + [0])

    def post(self, agent, act, text, ref=None, key=None):
        if act == "refute" and not ref:
            raise ValueError("refute needs a ref")
        if self.ledger.is_acked("osb", self.room, key):
            self.replayed += 1
            return True
        text = clip(text, 1000)
        args = ["post", "--room", self.room, "--agent", agent, "--act", act, "--text=" + text]
        if ref:
            args.append("--ref=" + clip(ref, 200))
        rc, out, err, tries, dedup = -1, "", "", 0, False
        if self.verify and self.lamport is None:
            self._baseline()
        while tries < self.attempts:
            if tries:
                self.retries += 1
                if self.verify and self._landed(agent, act, text):
                    rc, dedup = 0, True
                    break
                time.sleep(self.backoff * tries)
            tries += 1
            rc, out, err = self._run(args)
            if rc == 0:
                break
        if rc == 0 and not dedup:
            try:
                self.lamport = max(self.lamport or 0, int(json.loads(out).get("lamport") or 0))
            except (ValueError, TypeError, AttributeError):
                pass
        if rc != 0:
            self.errors += 1
        row = {"sink": "osb", "room": self.room, "agent": agent, "act": act, "ref": ref,
               "argv": args, "rc": rc, "stdout": out[:2000], "stderr": err[:500],
               "attempts": tries}
        if key is not None:
            row["key"] = key
        if dedup:
            row["dedup"] = "found in the room before retrying"
        self.ledger.add(row)
        return rc == 0


class McpError(Exception):
    transport = False  # True: the pipe/server failed, delivery is unknown


class McpTransportError(McpError):
    transport = True


class McpStdioClient:
    """Minimal MCP stdio client: newline-delimited JSON-RPC 2.0.

    One reader thread per server process feeds a queue. After any transport
    error the process is stopped: a late answer from a timed-out request can
    then never be taken for the answer to the next one, and start() gives a
    clean server.
    """

    def __init__(self, cmd=None, timeout=20):
        self.cmd, self.timeout = list(cmd or MCP_CMD), timeout
        self.proc, self._id, self.tools, self.server = None, 0, {}, None
        self._q, self.starts = None, 0

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        self.stop()
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as ex:
            raise McpTransportError("cannot start the server: %r" % ex)
        self.starts += 1
        q = self._q = queue.Queue()
        out = self.proc.stdout

        def reader():
            try:
                for line in out:
                    q.put(line)
            except (OSError, ValueError):
                pass
            q.put(None)  # EOF

        threading.Thread(target=reader, daemon=True).start()
        init = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                           "clientInfo": {"name": "vp-ref-projection", "version": "1"}})
        self.server = init
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self.request("tools/list", {})
        tools = listed.get("tools", []) if isinstance(listed, dict) else []
        self.tools = {t["name"]: t.get("inputSchema", {}) for t in tools
                      if isinstance(t, dict) and isinstance(t.get("name"), str)}
        return init, listed

    def _send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as ex:
            self.stop()
            raise McpTransportError("server pipe closed: %r" % ex)

    def request(self, method, params):
        if self.proc is None:
            raise McpTransportError("server not running")
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout  # one budget for the whole answer
        while True:
            try:
                line = self._q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.stop()
                raise McpTransportError("timeout waiting for the server")
            if line is None:
                self.stop()
                raise McpTransportError("server closed stdout")
            try:
                msg = json.loads(line)
            except ValueError:
                self.stop()
                raise McpTransportError("non-JSON line from the server")
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise McpError("rpc error %s" % json.dumps(msg["error"]))
                res = msg.get("result", {})
                return res if isinstance(res, dict) else {"result": res}

    def call(self, name, arguments):
        """Returns (ok, result). ok is False when the tool reports isError."""
        schema = self.tools.get(name)
        if schema is None:
            raise McpError("server does not list tool %r" % name)
        missing = [k for k in schema.get("required", []) if k not in arguments]
        unknown = [k for k in arguments if k not in schema.get("properties", arguments)]
        if missing or unknown:
            raise McpError("%s: missing %r unknown %r" % (name, missing, unknown))
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        return (not res.get("isError", False)), res

    def stop(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            p.terminate()  # this PID only
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        # stdout is left to the reader thread: it ends at EOF, and closing the
        # pipe under a blocked reader is undefined.


class TaskLightSink:
    def __init__(self, room, ledger, client, retry_safe=MCP_RETRY_SAFE):
        self.room, self.ledger, self.client = room, ledger, client
        self.retry_safe = set(retry_safe)
        self.errors = self.replayed = self.retries = 0

    def call(self, tool, agent, key=None, **args):
        if self.ledger.is_acked("tasklight", self.room, key):
            self.replayed += 1
            return True
        arguments = dict(room=self.room, agent=agent, **args)
        ok, res, tries = False, {}, 0
        while tries < 2:
            tries += 1
            try:
                if not self.client.alive():
                    self.client.start()  # first use, or the server died
                ok, res = self.client.call(tool, arguments)
                break
            except McpError as ex:
                ok, res = False, {"client_error": str(ex), "transport": ex.transport}
                if not ex.transport or tool not in self.retry_safe:
                    break
                self.retries += 1
        if not ok:
            self.errors += 1
        row = {"sink": "tasklight", "room": self.room, "agent": agent, "tool": tool,
               "arguments": arguments, "ok": ok, "response": res, "attempts": tries}
        if key is not None:
            row["key"] = key
        self.ledger.add(row)
        return ok


def retry_outbox(ledger, osb, tasklight):
    """Retries every keyed call the ledger shows failed and never acknowledged
    (a failed osb post, a not-ok TaskLight call). Returns how many were
    delivered now; what still fails stays in ledger.pending for the next start."""
    done = 0
    for (sink, room, key), row in list(ledger.pending.items()):
        try:
            if sink == "osb" and room == osb.room:
                text = next((a[len("--text="):] for a in row.get("argv") or []
                             if isinstance(a, str) and a.startswith("--text=")), None)
                if text is None or not isinstance(row.get("agent"), str) or not isinstance(row.get("act"), str):
                    continue
                ok = osb.post(row["agent"], row["act"], text, ref=row.get("ref"), key=key)
            elif sink == "tasklight" and room == tasklight.room:
                args = dict(row.get("arguments") or {})
                args.pop("room", None)
                agent = args.pop("agent", row.get("agent"))
                ok = tasklight.call(row.get("tool"), agent, key=key, **args)
            else:
                continue
        except (ValueError, TypeError, AttributeError):
            continue  # a hand-edited ledger row: leave it pending, visible in the summary
        done += bool(ok)
    return done


def clean_claims(obj):
    """claims.json -> {agent: [str, ...]}; anything else is dropped."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and isinstance(v, list):
                out[k] = [clip(x, 64) for x in v if isinstance(x, str) and CLAIM.match(x)][:16]
    return out


# ---------------------------------------------------------------- projector

class Projector:
    """Pure mapping from broker records to sink calls. State is JSON-serialisable."""

    def __init__(self, osb, tasklight, state=None, claims=None):
        self.osb, self.tl = osb, tasklight
        self.claims = clean_claims(claims)
        self.s = state or {"offset": 0, "line": 0, "agents": {}, "event_agent": {},
                           "turn_agent": {}, "anomalies": [], "ignored": {}}
        self.s.setdefault("anomaly_count", len(self.s.get("anomalies", [])))
        self.s.setdefault("head", None)
        self._n = 0

    def key(self):
        """Idempotency key of the next outbound call: <record line>.<ordinal>.
        The mapping is deterministic, so redoing a record after a crash asks
        for the same keys in the same order."""
        self._n += 1
        return "%s.%d" % (self.s["line"], self._n)

    # -- helpers
    def anomaly(self, why, rec=None):
        self.s["anomaly_count"] = self.s.get("anomaly_count", 0) + 1
        if len(self.s["anomalies"]) < MAX_ANOMALIES:  # bounded: state is rewritten per record
            kind = rec.get("rec") if isinstance(rec, dict) else None
            self.s["anomalies"].append({"line": self.s["line"], "why": clip(why, 200),
                                        "rec": kind if isinstance(kind, str) else None})

    def agent(self, aid):
        return self.s["agents"].setdefault(aid, {
            "title": None, "steps": [], "overflow": [], "status": None, "closed": False,
            "plan_ok": False})

    def status(self, aid, word, detail):
        a = self.agent(aid)
        text = clip("%s: %s" % (word, detail), OUTCOME)
        if a["status"] == text or not a["title"]:
            return
        a["status"] = text
        self.tl.call("post_status", aid, key=self.key(), text=text)

    def post_plan(self, aid):
        a = self.agent(aid)
        steps = []
        for st in a["steps"]:
            d = {"text": st["text"]}
            if st.get("accept"):
                d["accept"] = st["accept"]
            steps.append(d)
        a["plan_ok"] = self.tl.call("post_plan", aid, key=self.key(), title=a["title"], steps=steps)
        a["closed"] = False

    def step_index(self, aid, event_id):
        for i, st in enumerate(self.agent(aid)["steps"]):
            if st["event_id"] == event_id:
                return i
        return None

    def set_step(self, aid, i, state, outcome):
        st = self.agent(aid)["steps"][i]
        st["state"] = state
        self.tl.call("post_step", aid, key=self.key(), index=i, state=state, outcome=clip(outcome, OUTCOME))

    def resolve_agent(self, rec):
        aid = rec.get("agent_id")
        eid = rec.get("event_id")
        if not aid and isinstance(eid, str):
            aid = self.s["event_agent"].get(eid)
            if not aid:
                m = re.match(r"^(.+-agent)-\d+$", eid)
                aid = m.group(1) if m else None
        if not aid and rec.get("turn") is not None:
            aid = self.s["turn_agent"].get(str(rec["turn"]))
        return aid if isinstance(aid, str) and SLUG.match(aid) else None

    # -- records
    def feed(self, rec):
        self.s["line"] += 1
        self._n = 0
        if not isinstance(rec, dict) or rec.get("v") != 1:
            return self.anomaly("not a v1 record", rec)
        kind = rec.get("rec")
        handler = {"enqueue": self.on_enqueue, "turn": self.on_turn, "close": self.on_close,
                   "control": self.on_control}.get(kind if isinstance(kind, str) else None)
        if handler is None:  # cursor and unknown records are not turn-level
            kind = clip(kind, 40)
            self.s["ignored"][str(kind)] = self.s["ignored"].get(str(kind), 0) + 1
            return
        handler(rec)

    def on_enqueue(self, rec):
        aid, eid = self.resolve_agent(rec), rec.get("event_id")
        if not aid or not isinstance(eid, str) or not EVENT_ID.match(eid):
            return self.anomaly("enqueue with bad agent_id or event_id", rec)
        if eid in self.s["event_agent"]:
            return self.anomaly("enqueue repeats event_id %s" % eid, rec)
        self.s["event_agent"][eid] = aid
        ws = rec.get("write_set")
        ws = [clip(w, 64) for w in (ws if isinstance(ws, list) else []) if isinstance(w, str)][:16]
        what = "%s %s" % (clip(rec.get("lane", "?"), 16), clip(rec.get("kind", "?"), 16))
        target = ",".join(ws) if ws else "view"
        # The script source and the keys are never copied to a room: only their shape.
        self.osb.post(aid, "claim", "enqueue %s: %s -> %s based_on_frame=%s expect=%s" % (
            eid, what, target, clip(rec.get("based_on_frame"), 24), clip(rec.get("expect", ""), 200)),
            ref=eid, key=self.key())
        a = self.agent(aid)
        if a["title"] is None:
            own = ",".join(self.claims.get(aid) or ws) or "view"
            a["title"] = clip("[ViewPoint capsule] %s on %s" % (aid, own), TITLE)
        step = {"event_id": eid, "state": "pending", "text": clip("%s: %s -> %s" % (eid, what, target), STEP_TEXT)}
        if rec.get("expect"):
            step["accept"] = clip(rec["expect"], ACCEPT)
        if len(a["steps"]) >= MAX_STEPS:
            a["overflow"].append(eid)
            self.status(aid, "waiting", "%s queued beyond the %d-step card" % (eid, MAX_STEPS))
            return
        a["steps"].append(step)
        self.post_plan(aid)
        if a["status"] is None or a["status"].startswith("waiting"):
            self.status(aid, "waiting", "%d queued, last %s" % (
                sum(1 for s in a["steps"] if s["state"] not in DONE_STATES), eid))

    def on_turn(self, rec):
        aid = self.resolve_agent(rec)
        if not aid:
            return self.anomaly("turn without a valid agent_id", rec)
        self.s["turn_agent"][str(rec.get("turn"))] = aid
        self.osb.post(aid, "ack", "turn %s granted: turn_epoch=%s head_frame=%s hold_ms=%s" % (
            clip(rec.get("turn"), 24), clip(rec.get("turn_epoch"), 24), clip(rec.get("head_frame"), 24),
            clip(rec.get("hold_ms"), 24)), key=self.key())
        a = self.agent(aid)
        for i, st in enumerate(a["steps"]):  # per-agent FIFO: the oldest open step is the head
            if st["state"] not in DONE_STATES:
                if st["state"] != "active":
                    self.set_step(aid, i, "active", "turn %s, head_frame %s" % (rec.get("turn"), rec.get("head_frame")))
                break
        self.status(aid, "acting", "turn %s on frame %s" % (rec.get("turn"), rec.get("head_frame")))

    def on_close(self, rec):
        verdict, eid = rec.get("verdict"), rec.get("event_id")
        aid = self.resolve_agent(rec)
        if not isinstance(verdict, str) or not aid:
            return self.anomaly("close without verdict or resolvable agent", rec)
        if verdict not in KNOWN_VERDICTS:
            self.anomaly("unknown verdict %s (projected as a refusal)" % clip(verdict, 40), rec)
            verdict = clip(re.sub(r"[^A-Z_]", "", verdict.upper()) or "UNKNOWN", 40)
        if verdict == "PASS":  # forfeited turn: nothing happened, the tick did not advance
            return self.status(aid, "waiting", "passed turn %s" % rec.get("turn"))
        ref = eid if isinstance(eid, str) and EVENT_ID.match(eid) else "turn:%s" % rec.get("turn")
        facts = "%s %s tick=%s turn=%s frames=%s->%s drops=%s%s" % (
            verdict, ref, rec.get("tick"), rec.get("turn"), rec.get("dispatch_capture_seq"),
            rec.get("verify_frame_ref"), rec.get("drops", 0), " rebased" if rec.get("rebased") else "")
        facts = clip(facts, 400)
        i = self.step_index(aid, eid) if isinstance(eid, str) else None
        if (i is not None and verdict not in SUBMISSION
                and self.agent(aid)["steps"][i]["state"] in DONE_STATES):
            # A terminal step stays terminal: a repeated or late close must not
            # reopen it, re-post it as evidence, or turn a blocked step green.
            return self.anomaly("close %s for already terminal event %s" % (verdict, eid), rec)
        if verdict in VERIFIED:
            spill = rec.get("spill_regions")
            spill = spill if isinstance(spill, list) else []
            self.osb.post(aid, "evidence", "close %s spill_regions=%d" % (facts, len(spill)), ref=ref,
                          key=self.key())
            if i is not None:
                self.set_step(aid, i, "done", facts)
            a = self.agent(aid)
            left = sum(1 for s in a["steps"] if s["state"] not in DONE_STATES)
            self.status(aid, "waiting", "%s closed %s, %d queued" % (ref, verdict, left))
            return
        # Everything else is a refusal or an unverified outcome: never posted as evidence.
        self.osb.post(aid, "refute", "close %s" % facts, ref=ref, key=self.key())
        if verdict in SUBMISSION:
            return self.status(aid, "blocked", "%s for %s (submission refused)" % (verdict, ref))
        if verdict in REQUEUED and to_int(rec.get("drops")) < 3:
            if i is not None:
                self.set_step(aid, i, "pending", "%s, back to the tail" % facts)
            return self.status(aid, "waiting", "%s re-queued after %s" % (ref, verdict))
        if i is not None:
            self.set_step(aid, i, "blocked", facts + (" parked" if verdict in REQUEUED else ""))
        self.status(aid, "blocked", "%s %s" % (ref, verdict))

    def on_control(self, rec):
        op = rec.get("op")
        by = rec.get("by") if isinstance(rec.get("by"), str) and SLUG.match(rec.get("by")) else "broker"
        if op not in ("stop", "resume"):
            op = clip(op, 40)
            self.s["ignored"]["control:%s" % op] = self.s["ignored"].get("control:%s" % op, 0) + 1
            return
        self.osb.post(by, "verdict", "%s: %s" % (op, clip(rec.get("reason", ""), 300)), key=self.key())
        for aid, a in sorted(self.s["agents"].items()):
            if a["closed"] or not a["title"]:
                continue
            if op == "stop":
                self.status(aid, "blocked", "paused, stop by %s" % by)
            else:
                self.status(aid, "waiting", "resumed by %s" % by)

    def finalize(self):
        """End of the run: close every card whose steps are all terminal.
        An agent with open steps is left open on purpose; the reconciler flags it."""
        for aid, a in sorted(self.s["agents"].items()):
            if a["closed"] or not a["title"]:
                continue
            states = [s["state"] for s in a["steps"]]
            if a["overflow"] or any(st not in DONE_STATES for st in states):
                continue
            outcome = "%d done, %d blocked of %d events" % (
                states.count("done"), states.count("blocked"), len(states))
            a["closed"] = self.tl.call("close_task", aid, key="F%s.%s" % (self.s["line"], aid),
                                       outcome=outcome)


# ---------------------------------------------------------------- tail

def read_new_lines(path, offset):
    """Complete (newline-terminated) lines after offset -> ([bytes], new_offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1
    # split on "\n" only: splitlines() also breaks on a bare "\r", which
    # would desynchronise the per-line byte offsets
    return data[:end].split(b"\n")[:-1], offset + end


def check_identity(path, state):
    """The offset and the idempotency keys are only valid for the file they
    were computed on. A truncated, rotated or replaced events.jsonl would be
    read from the middle and its calls skipped as already delivered: refuse."""
    if not state.get("offset"):
        return
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.readline()
    except OSError:
        raise EventsFileChanged("events file is gone: %s" % path)
    if size < state["offset"]:
        raise EventsFileChanged("events file shrank below the saved offset (%d < %d)"
                                % (size, state["offset"]))
    if state.get("head") and hashlib.sha256(head).hexdigest() != state["head"]:
        raise EventsFileChanged("events file starts with a different record than the saved state")


def pump(proj, path, checkpoint=None):
    """Feeds every complete new line. checkpoint() runs after each record with
    the offset already past it, so a crash redoes at most one record."""
    check_identity(path, proj.s)
    lines, _ = read_new_lines(path, proj.s["offset"])
    for raw in lines:
        if proj.s["offset"] == 0 and not proj.s.get("head"):
            proj.s["head"] = hashlib.sha256(raw + b"\n").hexdigest()
        if len(raw) > MAX_LINE_BYTES:
            proj.s["line"] += 1
            proj.anomaly("oversized line (%d bytes), not parsed" % len(raw))
        elif raw.strip():
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError, MemoryError):
                proj.s["line"] += 1
                proj.anomaly("unparseable line")
            else:
                proj.feed(rec)
        proj.s["offset"] += len(raw) + 1
        if checkpoint:
            checkpoint()
    return len(lines)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rt", default=rt_root())
    ap.add_argument("--events", help="default RT/events.jsonl")
    ap.add_argument("--osb-room", default="viewpoint-capsule-test")
    ap.add_argument("--tasklight-room", default="demo")
    ap.add_argument("--out", help="projection dir for state.json and ledger.jsonl (default RT/projection)")
    ap.add_argument("--once", action="store_true", help="read to EOF, finalize, exit")
    ap.add_argument("--idle-exit", type=float, default=0, help="follow mode: finalize after S idle seconds")
    ap.add_argument("--poll", type=float, default=0.25)
    args = ap.parse_args(argv)

    events = args.events or os.path.join(args.rt, "events.jsonl")
    out = args.out or os.path.join(args.rt, "projection")
    os.makedirs(out, exist_ok=True)
    state_path = os.path.join(out, "state.json")
    state = None
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    claims = {}
    try:
        with open(os.path.join(args.rt, "claims.json"), encoding="utf-8") as f:
            claims = clean_claims(json.load(f))
    except (OSError, ValueError, AttributeError, RecursionError):
        pass

    ledger = Ledger(os.path.join(out, "ledger.jsonl"))
    # Test seams only: VP_PROJ_MCP_CMD (JSON argv) and VP_PROJ_OSB (path) swap in doubles.
    mcp_cmd = json.loads(os.environ["VP_PROJ_MCP_CMD"]) if os.environ.get("VP_PROJ_MCP_CMD") else None
    client = McpStdioClient(mcp_cmd)
    try:
        init, listed = client.start()
        ledger.add({"sink": "tasklight", "handshake": {"initialize": init, "tools/list": listed}})
    except McpError as ex:  # the osb projection must not die with TaskLight
        ledger.add({"sink": "tasklight", "handshake_error": str(ex)})
    osb = OsbSink(args.osb_room, ledger, osb=os.environ.get("VP_PROJ_OSB") or OSB)
    tl = TaskLightSink(args.tasklight_room, ledger, client)
    proj = Projector(osb, tl, state=state, claims=claims)

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    retried = 0
    try:
        retried = retry_outbox(ledger, osb, tl)  # calls an earlier run could not deliver
        last = time.monotonic()
        while True:
            if pump(proj, events, checkpoint=lambda: save_state(state_path, proj.s)):
                last = time.monotonic()
            if args.once or stop or (args.idle_exit and time.monotonic() - last > args.idle_exit):
                break
            time.sleep(args.poll)
        proj.finalize()
        save_state(state_path, proj.s)
    except EventsFileChanged as ex:
        print(json.dumps({"error": str(ex), "hint": "use a fresh --out for a new events file"}))
        return 3
    finally:
        client.stop()
    try:
        tail = max(0, os.path.getsize(events) - proj.s["offset"])
    except OSError:
        tail = 0
    summary = {"lines": proj.s["line"], "anomalies": proj.s["anomaly_count"],
               "osb_errors": osb.errors, "tasklight_errors": tl.errors,
               "replayed": osb.replayed + tl.replayed, "retries": osb.retries + tl.retries,
               "partial_tail_bytes": tail,
               "outbox_retried": retried, "unacked": len(ledger.pending),
               "agents": {a: {"steps": len(v["steps"]), "closed": v["closed"]}
                          for a, v in sorted(proj.s["agents"].items())}}
    print(json.dumps(summary, sort_keys=True))
    return 1 if (osb.errors or tl.errors or ledger.pending) else 0


if __name__ == "__main__":
    sys.exit(main())
