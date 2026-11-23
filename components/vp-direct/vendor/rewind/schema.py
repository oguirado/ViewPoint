"""R0: frozen contract of the rewindable Blender capsule (shared schema, stdlib only, no bpy, no broker import).

Track P (declared Claude work, claude-fable-5-2). Units R1-R4 import this module and never redefine
what it defines. Normative text: CONTRACT.md next to this file.

    sys.path.insert(0, <product/ref_rewind>); import schema as RW
"""
import hashlib
import json
import os
import re

CONTRACT_VERSION = "rw1"
DIGEST_VERSION = 2
JUDGE_VERSION = "j1"
K_MAX = 3                      # alternatives per intent, script included
RING_SLOTS = 8                 # checkpoint ring size S; stall guard at S-1
CKPT_DIRNAME = "ckpt"          # RT/ckpt/<point_id>.blend + RT/ckpt/manifest.jsonl (R1 owns it)
SCENE_EPOCH_0 = 1              # scene_epoch of a fresh capsule; +1 per restore, never decreases
RESULT_PREFIX = "RW_RESULT "   # last stdout line of every control job: RW_RESULT <one-line json>
RESULT_MAX = 16000             # bridge keeps a 20000-char tail of the job output
RESTORE_CAP_S = 30.0           # restore deadline; after it: rung A' (relaunch on the checkpoint)

EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,79}$")   # same as broker.py:42
POINT_ID_RE = re.compile(r"^p[0-9]{1,6}$")
ATTEMPT_RE = re.compile(r"^(?P<event_id>[A-Za-z0-9][A-Za-z0-9.\-]{0,72})\.a(?P<k>[1-9])$")
INTENT_EVENT_ID_MAX = 73       # so that "<event_id>.a<k>" and "ctl.probe.<...>" stay legal job names
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# digest v2 key namespaces (R1 digest.py produces exactly these; values are hex strings)
DIGEST_PREFIXES = ("col:", "mat:")
DIGEST_SINGLETONS = ("render", "ui:view3d")

# verdict / reason codes added by the rewind units (F1 codes stay as they are)
JUDGE_REJECT = "JUDGE_REJECT"
ALL_REJECTED = "ALL_REJECTED"            # close verdict: every alternative rolled back, state = base
CKPT_BUDGET = "CKPT_BUDGET"              # store refuses a mark instead of overwriting a pinned point
CKPT_CORRUPT = "CKPT_CORRUPT"            # sha256 of the .blend differs from the ckpt record
CKPT_MISSING = "CKPT_MISSING"
RESTORE_UNAUTH = "RESTORE_UNAUTH"        # restore answer not authenticated by the bridge
RESTORE_MISMATCH = "RESTORE_MISMATCH"    # digest after restore != digest of the point
RESTORE_TIMEOUT = "RESTORE_TIMEOUT"
PROBE_NOT_READONLY = "PROBE_NOT_READONLY"
PROBE_FAILED = "PROBE_FAILED"
BAD_INTENT = "BAD_INTENT"
ROLLBACK_VERDICTS = ("JOB_ERROR", "VERIFIED_WITH_SPILL", JUDGE_REJECT, "OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN")
COMMIT_VERDICTS = ("STATE_VERIFIED", "VERIFIED")
ORACLES = ("expect", "delta", "witness", "cost")   # fixed order in rec:judge


class SchemaError(ValueError):
    """A record or intent violates the R0 contract. .code is a short machine code."""

    def __init__(self, code, msg):
        ValueError.__init__(self, "%s: %s" % (code, msg))
        self.code = code


class CkptError(Exception):
    """Raised by a checkpoint store (R1 or fakes). .code in CKPT_BUDGET | CKPT_CORRUPT | CKPT_MISSING."""

    def __init__(self, code, msg=""):
        Exception.__init__(self, "%s: %s" % (code, msg))
        self.code = code


# ---------------------------------------------------------------- ids
def point_id(n):
    return "p%d" % n


def attempt_id(event_id, k):
    a = "%s.a%d" % (event_id, k)
    if not ATTEMPT_RE.match(a):
        raise SchemaError("BAD_ATTEMPT", "%r is not <event_id>.a<1..9> (event_id <= %d chars)" % (a, INTENT_EVENT_ID_MAX))
    return a


def split_attempt(attempt):
    m = ATTEMPT_RE.match(attempt if isinstance(attempt, str) else "")
    if not m:
        raise SchemaError("BAD_ATTEMPT", "%r is not <event_id>.a<k>" % (attempt,))
    return m.group("event_id"), int(m.group("k"))


def control_job_id(kind, *parts):
    """Bridge job name of a broker-owned control job. kind in mark|restore|digest|probe.
    mark:    ctl.mark.<point_id>            restore: ctl.restore.<point_id>.e<scene_epoch_after>
    digest:  ctl.digest.<n>                 probe:   ctl.probe.<n>   (n = any broker counter)
    Agents cannot use these names: F1 ingests intents only from RT/intents/<agent>/ and R2 refuses
    any agent event_id starting with "ctl."."""
    if kind not in ("mark", "restore", "digest", "probe"):
        raise SchemaError("BAD_JOB", "unknown control job kind %r" % (kind,))
    jid = ".".join(["ctl", kind] + [str(p) for p in parts])
    if not EVENT_ID_RE.match(jid):
        raise SchemaError("BAD_JOB", "%r is not a legal bridge job name" % jid)
    return jid


def is_control_id(event_id):
    return isinstance(event_id, str) and event_id.startswith("ctl.")


# ---------------------------------------------------------------- digest v2 helpers
def check_digest(d):
    if not isinstance(d, dict) or not d:
        raise SchemaError("BAD_DIGEST", "digest must be a non-empty object")
    for k, v in d.items():
        if not (isinstance(k, str) and (k.startswith(DIGEST_PREFIXES) or k in DIGEST_SINGLETONS)):
            raise SchemaError("BAD_DIGEST", "key %r is outside the digest v2 namespaces" % (k,))
        if not isinstance(v, str) or not v:
            raise SchemaError("BAD_DIGEST", "value of %r must be a non-empty hex string" % (k,))
    return d


def digest_delta(a, b):
    """Sorted keys whose value differs (added, removed or changed)."""
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def digest_match(a, b):
    return bool(a) and bool(b) and not digest_delta(a, b)


def digest_id(d):
    """One hex id for a whole digest (dedup key, log line)."""
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def delta_outside(delta, intent):
    """Keys of `delta` the intent did not declare. col:* must be in write_set; every other key
    (mat:*, render, ui:view3d) must be in the optional `touches` list. gui intents implicitly
    touch ui:view3d."""
    allowed = set(intent.get("write_set") or []) | set(intent.get("touches") or [])
    if intent.get("lane") == "gui":
        allowed.add("ui:view3d")
    return sorted(k for k in delta if k not in allowed)


# ---------------------------------------------------------------- control-job result line
def result_line(obj):
    """What a control script prints as its LAST stdout line."""
    s = RESULT_PREFIX + json.dumps(obj, sort_keys=True, separators=(",", ":"))
    if len(s) > RESULT_MAX:
        raise SchemaError("RESULT_TOO_LONG", "%d chars > %d" % (len(s), RESULT_MAX))
    return s


def parse_result(out_text):
    """JSON object of the last RW_RESULT line in a job's stdout (or whole outbox body), else None."""
    for line in reversed((out_text or "").split("\n")):
        if line.startswith(RESULT_PREFIX):
            try:
                obj = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dispatch_control(broker, job_id, script):
    """Run a broker-owned control job through the F1 bridge. Uses Broker._dispatch_script (the F1
    hook, broker.py:450) so the answer is HMAC-checked exactly like an agent job.
    Returns {"ok": bool, "code": str, "result": dict|None, "job_ms": int|None, "changed": list|None}.
    ok is True only when the bridge answer is authenticated and its status is OK."""
    if not is_control_id(job_id) or not EVENT_ID_RE.match(job_id):
        raise SchemaError("BAD_JOB", "%r is not a control job id" % (job_id,))
    ev = {"event_id": job_id, "script": script, "write_set": [],
          "script_sha256": hashlib.sha256(script.encode()).hexdigest()}
    verdict, extra = broker._dispatch_script(ev)
    out = {"ok": False, "code": verdict, "result": None, "job_ms": extra.get("job_ms"),
           "changed": extra.get("changed_write_set"), "reason": extra.get("reason")}
    if verdict in ("STATE_VERIFIED", "VERIFIED_WITH_SPILL"):   # authenticated + status OK
        try:
            with open(os.path.join(broker.d_outbox, job_id + ".out"), encoding="utf-8", errors="replace") as f:
                body = f.read()
        except OSError:
            body = extra.get("out_tail") or ""
        out["result"] = parse_result(body)
        out["ok"] = True
        out["code"] = "OK"
    return out


# ---------------------------------------------------------------- record builders (validated)
def _need(cond, code, msg):
    if not cond:
        raise SchemaError(code, msg)


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def ckpt_rec(point_id, tick, scene_epoch, blend_sha256, nbytes, digest, frame, dedup_of=None):
    r = {"rec": "ckpt", "point_id": point_id, "tick": tick, "scene_epoch": scene_epoch,
         "blend_sha256": blend_sha256, "bytes": nbytes, "digest": digest, "frame": frame,
         "digest_version": DIGEST_VERSION}
    if dedup_of is not None:
        r["dedup_of"] = dedup_of
    return validate_record(r)


def txn_rec(phase, event_id, attempt, base_point, verdict=None, reason=None, point=None, tick=None):
    r = {"rec": "txn", "phase": phase, "event_id": event_id, "attempt": attempt, "base_point": base_point,
         "verdict": verdict, "reason": reason}
    if point is not None:
        r["point"] = point
    if tick is not None:
        r["tick"] = tick
    return validate_record(r)


def judge_rec(attempt, pick, oracles, judge_version=JUDGE_VERSION):
    return validate_record({"rec": "judge", "attempt": attempt, "pick": pick, "oracles": oracles,
                            "judge_version": judge_version})


def rewind_rec(to, scene_epoch_after, digest_match, by="broker", reason=None, attempt=None):
    r = {"rec": "control", "op": "rewind", "to": to, "by": by, "scene_epoch_after": scene_epoch_after,
         "digest_match": digest_match}
    if reason is not None:
        r["reason"] = reason
    if attempt is not None:
        r["attempt"] = attempt
    return validate_record(r)


def oracle(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:400]}


def validate_record(r):
    """Return r if it is a well-formed rewind record (ckpt | txn | judge | control rewind), else raise
    SchemaError. Records of other types are F1's business and raise NOT_REWIND."""
    _need(isinstance(r, dict), "BAD_REC", "record must be an object")
    kind = r.get("rec")
    if kind == "ckpt":
        _need(isinstance(r.get("point_id"), str) and POINT_ID_RE.match(r["point_id"]), "BAD_POINT", "point_id must match p<digits>")
        _need(_is_int(r.get("tick")) and r["tick"] >= 0, "BAD_REC", "ckpt.tick must be an int >= 0")
        _need(_is_int(r.get("scene_epoch")) and r["scene_epoch"] >= SCENE_EPOCH_0, "BAD_EPOCH", "ckpt.scene_epoch must be an int >= %d" % SCENE_EPOCH_0)
        _need(isinstance(r.get("blend_sha256"), str) and HEX64_RE.match(r["blend_sha256"]), "BAD_SHA", "ckpt.blend_sha256 must be 64 lowercase hex")
        _need(_is_int(r.get("bytes")) and r["bytes"] > 0, "BAD_REC", "ckpt.bytes must be an int > 0")
        check_digest(r.get("digest"))
        _need(_is_int(r.get("frame")) and r["frame"] >= 0, "BAD_REC", "ckpt.frame must be an int >= 0")
        if "dedup_of" in r:
            _need(isinstance(r["dedup_of"], str) and POINT_ID_RE.match(r["dedup_of"]), "BAD_POINT", "ckpt.dedup_of must be a point id")
    elif kind == "txn":
        phase = r.get("phase")
        _need(phase in ("begin", "commit", "rollback"), "BAD_PHASE", "txn.phase must be begin|commit|rollback")
        eid, k = split_attempt(r.get("attempt"))
        _need(r.get("event_id") == eid, "BAD_ATTEMPT", "txn.attempt %r does not belong to event %r" % (r.get("attempt"), r.get("event_id")))
        _need(k <= K_MAX, "BAD_ATTEMPT", "attempt index %d > K_MAX %d" % (k, K_MAX))
        _need(not is_control_id(eid), "BAD_ATTEMPT", "control jobs are not transactions")
        _need(isinstance(r.get("base_point"), str) and POINT_ID_RE.match(r["base_point"]), "BAD_POINT", "txn.base_point must be a point id")
        v = r.get("verdict")
        if phase == "begin":
            _need(v is None, "BAD_VERDICT", "txn begin carries verdict null")
        elif phase == "commit":
            _need(v in COMMIT_VERDICTS, "BAD_VERDICT", "txn commit verdict must be one of %s" % (COMMIT_VERDICTS,))
            _need(isinstance(r.get("point"), str) and POINT_ID_RE.match(r["point"]), "BAD_POINT", "txn commit must name the point marked on the committed state")
            _need(_is_int(r.get("tick")) and r["tick"] >= 1, "BAD_REC", "txn commit must carry the committed tick")
        else:
            _need(v in ROLLBACK_VERDICTS, "BAD_VERDICT", "txn rollback verdict must be one of %s" % (ROLLBACK_VERDICTS,))
        _need(r.get("reason") is None or isinstance(r["reason"], str), "BAD_REC", "txn.reason must be a string or null")
    elif kind == "judge":
        split_attempt(r.get("attempt"))
        _need(isinstance(r.get("pick"), bool), "BAD_REC", "judge.pick must be a bool")
        _need(isinstance(r.get("judge_version"), str) and r["judge_version"], "BAD_REC", "judge.judge_version required")
        os_ = r.get("oracles")
        _need(isinstance(os_, list) and os_, "BAD_ORACLES", "judge.oracles must be a non-empty list")
        names = []
        for o in os_:
            _need(isinstance(o, dict) and o.get("name") in ORACLES and isinstance(o.get("ok"), bool)
                  and isinstance(o.get("detail"), str), "BAD_ORACLES", "oracle must be {name in %s, ok bool, detail str}" % (ORACLES,))
            names.append(o["name"])
        _need(names == [n for n in ORACLES if n in names], "BAD_ORACLES", "oracles out of the fixed order %s or repeated" % (ORACLES,))
        # DEAD-ORACLE GUARD: a pick with a failed (or no deciding) oracle is not a judgment
        deciding = [o for o in os_ if o["name"] != "cost"]
        if r["pick"]:
            _need(deciding and all(o["ok"] for o in deciding), "BAD_PICK", "pick=true needs >=1 deciding oracle and all of them ok")
        else:
            _need(any(not o["ok"] for o in deciding), "BAD_PICK", "pick=false needs a failed deciding oracle")
    elif kind == "control" and r.get("op") == "rewind":
        _need(isinstance(r.get("to"), str) and POINT_ID_RE.match(r["to"]), "BAD_POINT", "rewind.to must be a point id")
        _need(r.get("by") in ("broker", "director"), "NO_LEASE", "rewind is a broker/director control; by=%r refused" % (r.get("by"),))
        _need(_is_int(r.get("scene_epoch_after")) and r["scene_epoch_after"] > SCENE_EPOCH_0, "BAD_EPOCH", "rewind.scene_epoch_after must be > %d" % SCENE_EPOCH_0)
        _need(isinstance(r.get("digest_match"), bool), "BAD_REC", "rewind.digest_match must be a bool")
    else:
        raise SchemaError("NOT_REWIND", "not a rewind record: rec=%r op=%r" % (kind, r.get("op")))
    return r


def is_rewind_record(r):
    return isinstance(r, dict) and (r.get("rec") in ("ckpt", "txn", "judge")
                                    or (r.get("rec") == "control" and r.get("op") == "rewind"))


# ---------------------------------------------------------------- intent extension
def validate_intent_ext(it):
    """(verdict, reason) if the optional rewind fields of an F1 intent are malformed, else None.
    F1 validation (broker._validate) still applies first; old intents (no new field) pass."""
    eid = it.get("event_id")
    if is_control_id(eid):
        return "NO_LEASE", "event ids starting with 'ctl.' are reserved for broker control jobs"
    if isinstance(eid, str) and len(eid) > INTENT_EVENT_ID_MAX:
        return BAD_INTENT, "event_id longer than %d chars leaves no room for the attempt suffix" % INTENT_EVENT_ID_MAX
    if it.get("op") is not None or it.get("rec") == "control":
        return "NO_LEASE", "agents cannot submit control operations (rewind is broker-only)"
    alts = it.get("alternatives")
    if alts is not None:
        if it.get("lane") != "script":
            return BAD_INTENT, "alternatives are only defined for lane script"
        if not isinstance(alts, list) or not all(isinstance(s, str) and s for s in alts):
            return BAD_INTENT, "alternatives must be a list of non-empty script sources"
        if 1 + len(alts) > K_MAX:
            return BAD_INTENT, "script + alternatives > K_MAX=%d" % K_MAX
    ex = it.get("expect")
    if isinstance(ex, dict):
        if not isinstance(ex.get("probe"), str) or not ex["probe"] or "equals" not in ex:
            return BAD_INTENT, "expect object needs probe (bpy source) and equals (json)"
    elif ex is not None and not isinstance(ex, str):
        return BAD_INTENT, "expect must be text (F1) or {probe, equals}"
    t = it.get("touches")
    if t is not None:
        if not isinstance(t, list) or not all(isinstance(k, str) and (k.startswith("mat:") or k in DIGEST_SINGLETONS) for k in t):
            return BAD_INTENT, "touches must list mat:<name>, render or ui:view3d keys"
    return None


def attempt_scripts(it):
    """[(attempt_id, script), ...] in the order they are tried (serial, each from the same base)."""
    scripts = [it["script"]] + list(it.get("alternatives") or [])
    return [(attempt_id(it["event_id"], k + 1), s) for k, s in enumerate(scripts)]


# ---------------------------------------------------------------- replay (shared fold of events.jsonl)
class RewindState:
    """State every unit derives the same way from events.jsonl. Fold with apply(); never mutated otherwise."""

    def __init__(self):
        self.scene_epoch = SCENE_EPOCH_0
        self.confirmed_tick = 0
        self.points = {}          # point_id -> ckpt record (insertion = log order)
        self.last_good = None     # point id of the newest committed (or genesis) state
        self.open_txn = None      # the txn begin record without commit/rollback, or None
        self.attempts = {}        # attempt -> "begin" | "commit" | "rollback"
        self.judged = {}          # attempt -> pick
        self.rewinds = []         # control rewind records
        self.pending_restore = None   # last rewind since the open txn began: (to, digest_match)
        self.violations = []      # [(seq, code, msg)]
        self.frozen = False

    def _bad(self, r, code, msg):
        self.violations.append((r.get("seq"), code, msg))

    def apply(self, r):
        if not is_rewind_record(r):
            if isinstance(r, dict) and r.get("rec") == "control":
                if r.get("op") == "stop":
                    self.frozen = True
                elif r.get("op") == "resume":
                    self.frozen = False
            return self
        try:
            validate_record(r)
        except SchemaError as e:
            self._bad(r, e.code, str(e))
            return self
        kind = r["rec"]
        if kind == "ckpt":
            if r["point_id"] in self.points:
                self._bad(r, "POINT_REUSED", "point %s marked twice" % r["point_id"])
            if r["scene_epoch"] != self.scene_epoch:
                self._bad(r, "EPOCH_SKEW", "ckpt at scene_epoch %d, log is at %d" % (r["scene_epoch"], self.scene_epoch))
            self.points[r["point_id"]] = r
            if self.last_good is None:      # genesis point
                self.last_good = r["point_id"]
        elif kind == "judge":
            self.judged[r["attempt"]] = r["pick"]
        elif kind == "txn":
            a, phase = r["attempt"], r["phase"]
            if phase == "begin":
                if self.open_txn is not None:
                    self._bad(r, "TXN_OVERLAP", "begin %s while %s is open (one Blender, serial attempts)" % (a, self.open_txn["attempt"]))
                if a in self.attempts:
                    self._bad(r, "ATTEMPT_REUSED", "attempt id %s used twice (never repeat in place)" % a)
                if r["base_point"] not in self.points:
                    self._bad(r, "UNKNOWN_POINT", "base_point %s has no ckpt record" % r["base_point"])
                elif r["base_point"] != self.last_good:
                    self._bad(r, "BASE_NOT_LAST_GOOD", "base_point %s != last_good %s" % (r["base_point"], self.last_good))
                self.open_txn, self.pending_restore = r, None
                self.attempts[a] = "begin"
            else:
                if self.open_txn is None or self.open_txn["attempt"] != a:
                    self._bad(r, "TXN_NOT_OPEN", "%s %s without its begin" % (phase, a))
                elif self.open_txn["base_point"] != r["base_point"]:
                    self._bad(r, "BASE_CHANGED", "%s %s cites another base than its begin" % (phase, a))
                if phase == "commit":
                    if self.judged.get(a) is not True:
                        self._bad(r, "COMMIT_UNJUDGED", "commit %s without a judge record with pick=true" % a)
                    if r["point"] not in self.points:
                        self._bad(r, "UNKNOWN_POINT", "commit point %s has no ckpt record" % r["point"])
                    if r["tick"] <= self.confirmed_tick:
                        self._bad(r, "TICK_BACKWARDS", "commit tick %d <= confirmed_tick %d" % (r["tick"], self.confirmed_tick))
                    self.confirmed_tick = max(self.confirmed_tick, r["tick"])
                    self.last_good = r["point"]
                else:
                    # ROLLBACK GUARD: a rollback is only a rollback if the base was restored and matched
                    if self.pending_restore != (r["base_point"], True):
                        self._bad(r, "ROLLBACK_WITHOUT_RESTORE", "rollback %s without a preceding rewind to %s with digest_match=true" % (a, r["base_point"]))
                    if r["verdict"] == JUDGE_REJECT and self.judged.get(a) is not False:
                        self._bad(r, "ROLLBACK_UNJUDGED", "JUDGE_REJECT rollback %s without a judge record with pick=false" % a)
                self.attempts[a] = phase
                self.open_txn, self.pending_restore = None, None
        else:  # control rewind
            if r["to"] not in self.points:
                self._bad(r, "UNKNOWN_POINT", "rewind to %s which has no ckpt record" % r["to"])
            # EPOCH GUARD: every restore bumps scene_epoch by exactly one, also a failed one
            if r["scene_epoch_after"] != self.scene_epoch + 1:
                self._bad(r, "EPOCH_NOT_BUMPED", "rewind scene_epoch_after %d, expected %d" % (r["scene_epoch_after"], self.scene_epoch + 1))
            self.scene_epoch = max(self.scene_epoch, r["scene_epoch_after"])
            self.rewinds.append(r)
            self.pending_restore = (r["to"], r["digest_match"])
            if not r["digest_match"]:
                self.frozen = True   # only a director rewind to last_good (then resume) leaves this state
            elif r["by"] == "director" and r["to"] == self.last_good and self.open_txn is None:
                pass
        return self

    def summary(self):
        return {"scene_epoch": self.scene_epoch, "confirmed_tick": self.confirmed_tick,
                "last_good": self.last_good, "points": list(self.points),
                "open_txn": self.open_txn["attempt"] if self.open_txn else None,
                "attempts": dict(self.attempts), "rewinds": len(self.rewinds),
                "frozen": self.frozen, "violations": len(self.violations)}


def replay(records):
    st = RewindState()
    for r in records:
        st.apply(r)
    return st


def read_events(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def exportable(tick, state):
    """Commit frontier: external effects (export, room/TaskLight projection) only for ticks <= confirmed_tick."""
    return _is_int(tick) and tick <= state.confirmed_tick and state.open_txn is None
