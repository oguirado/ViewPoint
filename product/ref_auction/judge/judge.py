"""ViewPoint readiness JUDGE (reference, Track P, written by Claude Fable 5.2).

Scores how prepared an agent is to execute its queued intent on the capsule NOW.
Deterministic, integer-only, stdlib-only, no I/O inside score_intent (the hot path).

    h = History.from_events("RT/events.jsonl")        # or feed(record) incrementally
    r = score_intent(intent, head_frame, h, claims)   # -> dict, r["score"] in 0..1000

Features (each an int in 0..1000, weights sum to 1000):
    freshness  based_on_frame against the head frame
    claim      write_set inside the agent's claim (script) / GUI lease (gui)
    track      the agent's past verdicts, recency-weighted (VERIFIED vs refusals/rollbacks)
    effect     expected visual effect size from the vision engine changed regions
    cost       script (one bridge job) against GUI (one vision act + look per key)
    retry      drops already taken by this very event
A gate (stale frame, claim mismatch, malformed intent, parked event) forces score 0:
the judge never ranks an intent the broker's ADMIT phase would refuse.
The optional LLM advice is a cached, bounded adjustment computed OFFLINE (llm_advice.py).
"""
import hashlib
import json

V = 1
FRAME_RING = 32          # same ring as ref_broker: older bases are FRAME_EVICTED
MAX_DROPS = 3            # ref_broker parks an event at 3 drops
SCREEN = (1920, 1080)
WINDOW = 16              # outcomes remembered per agent
HALF_LIFE = 4            # outcomes; weight halves every 4 newer outcomes of the same agent
LLM_CLAMP = 100          # the LLM can move a score by at most +-100 of 1000

WEIGHTS = {"freshness": 250, "claim": 150, "track": 300, "effect": 100, "cost": 100, "retry": 100}
assert sum(WEIGHTS.values()) == 1000

GOOD = ("VERIFIED", "STATE_VERIFIED")
# bad mass per outcome (a good outcome carries good mass 1)
SOFT = ("VERIFIED_WITH_SPILL", "NO_VISIBLE_CHANGE", "STALE_FRAME", "TURN_TIMEOUT", "FRAME_EVICTED")
HARD = ("JOB_ERROR", "CLAIM_REFUSED", "BAD_INTENT", "NO_LEASE", "LEASE_EXPIRED", "OUTCOME_UNCERTAIN",
        "INPUT_UNCERTAIN", "FOOTPRINT_MISSING", "DUPLICATE_EVENT", "JUDGE_REJECT")
SEV_SOFT, SEV_HARD, SEV_ROLLBACK = 1, 2, 3
# not the agent's doing: PASS (empty queue), QUEUE_FULL / ACTUATOR_BUSY (broker back-pressure)
IGNORED = ("PASS", "QUEUE_FULL", "ACTUATOR_BUSY")

_Q = (1024, 861, 724, 609)  # round(1024 * 2**(-k/4)), k = 0..3


def decay(age):
    """Fixed-point weight 1024 * 2**(-age/HALF_LIFE); integers only."""
    return _Q[age % HALF_LIFE] >> (age // HALF_LIFE)


def union_area(regions, screen=SCREEN):
    """Exact area of the union of [x,y,w,h] boxes clipped to the screen (coordinate compression)."""
    W, H = screen
    boxes = []
    for r in list(regions or [])[:64]:
        try:
            x, y, w, h = (int(v) for v in r)
        except (TypeError, ValueError):
            continue
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 > x0 and y1 > y0:
            boxes.append((x0, y0, x1, y1))
    xs = sorted({b[0] for b in boxes} | {b[2] for b in boxes})
    area = 0
    for i in range(len(xs) - 1):
        spans = sorted((b[1], b[3]) for b in boxes if b[0] <= xs[i] and b[2] >= xs[i + 1])
        covered, end = 0, -1
        for y0, y1 in spans:
            if y1 > end:
                covered += y1 - max(y0, end)
                end = y1
        area += covered * (xs[i + 1] - xs[i])
    return area


def area_permille(regions, screen=SCREEN):
    return union_area(regions, screen) * 1000 // (screen[0] * screen[1])


def intent_sig(intent):
    """Stable digest of the fields that define what will run; LLM advice is bound to it."""
    keep = {k: intent.get(k) for k in ("event_id", "agent_id", "lane", "kind", "based_on_frame",
                                       "write_set", "script", "keys", "expect")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class History:
    """Per-agent memory rebuilt from events.jsonl. feed() is O(1) amortised per record."""

    def __init__(self, screen=SCREEN):
        self.screen = screen
        self.outcomes = {}   # agent -> list of [event_id, good(0|1), severity], oldest first
        self.effects = {}    # (agent, lane) -> list of area permille of verified effects
        self.drops = {}      # event_id -> int
        self.owner = {}      # event_id -> agent_id
        self.lane = {}       # event_id -> lane
        self.head_frame = 0
        self.records = 0

    @classmethod
    def from_events(cls, source, screen=SCREEN):
        h = cls(screen)
        if isinstance(source, str):
            try:
                with open(source) as f:
                    lines = f.readlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    h.feed(json.loads(line))
                except ValueError:
                    continue  # a torn last line must not blind the judge
        else:
            for r in source:
                h.feed(r)
        return h

    def _agent_of(self, r):
        a = r.get("agent_id")
        eid = r.get("event_id")
        if a is None and isinstance(eid, str):
            a = self.owner.get(eid)
        return a if isinstance(a, str) else None

    def _push(self, agent, eid, good, sev):
        lst = self.outcomes.setdefault(agent, [])
        lst.append([eid, good, sev])
        del lst[:-WINDOW]

    def feed(self, r):
        if not isinstance(r, dict):
            return
        self.records += 1
        rec, eid = r.get("rec"), r.get("event_id")
        if rec == "enqueue" and isinstance(eid, str) and isinstance(r.get("agent_id"), str):
            self.owner[eid] = r["agent_id"]
            self.lane[eid] = r.get("lane")
        elif rec == "turn":
            if isinstance(r.get("head_frame"), int):
                self.head_frame = max(self.head_frame, r["head_frame"])
        elif rec == "close":
            if isinstance(r.get("verify_frame_ref"), int):
                self.head_frame = max(self.head_frame, r["verify_frame_ref"])
            v, agent = r.get("verdict"), self._agent_of(r)
            if isinstance(eid, str) and isinstance(r.get("drops"), int):
                self.drops[eid] = r["drops"]
            if agent is not None and isinstance(eid, str):
                self.owner.setdefault(eid, agent)  # txn records carry no agent_id: remember who closed it
            if agent is None or v in IGNORED or not isinstance(v, str):
                return
            if v in GOOD:
                self._push(agent, eid, 1, 0)
            else:
                # unknown verdict codes count as hard: fail closed on what we cannot classify
                self._push(agent, eid, 0, SEV_SOFT if v in SOFT else SEV_HARD)
            if v in GOOD or v == "VERIFIED_WITH_SPILL":
                lane = r.get("lane") or self.lane.get(eid)
                e = self.effects.setdefault((agent, lane), [])
                e.append(area_permille(r.get("own_regions"), self.screen))
                del e[:-WINDOW]
        elif rec == "txn" and r.get("phase") == "rollback":
            agent = self._agent_of(r)
            if agent is None:
                return
            lst = self.outcomes.setdefault(agent, [])
            if lst and lst[-1][0] == eid and eid is not None:
                lst[-1][1], lst[-1][2] = 0, SEV_ROLLBACK  # the close of this attempt, undone: count it once
            else:
                self._push(agent, eid, 0, SEV_ROLLBACK)

    # ---- features over the memory
    def track(self, agent):
        """Recency-weighted Beta mean: 1000*(P+G)/(2P+G+B), P = one pseudo-outcome each way."""
        lst = self.outcomes.get(agent, [])
        g = b = 0
        for age, (_, good, sev) in enumerate(reversed(lst)):
            w = decay(age)
            g += w * good
            b += w * sev
        p = 1024
        return 1000 * (p + g) // (2 * p + g + b)

    def expected_effect(self, agent, lane):
        """Recency-weighted mean area (permille of screen) of this agent's verified effects, or None."""
        e = self.effects.get((agent, lane))
        if not e:
            return None
        num = den = 0
        for age, a in enumerate(reversed(e)):
            w = decay(age)
            num += w * a
            den += w
        return num // den if den else None


def f_effect(p):
    """Tent over the expected changed area p (permille). None = no evidence -> neutral 500.
    0 -> 200 (vision cannot verify an invisible act: NO_VISIBLE_CHANGE risk);
    5..250 -> 1000 (visible and local); 1000 -> 400 (repaints everything: spill, stales every peer)."""
    if p is None:
        return 500
    if p < 5:
        return 200 + 160 * p
    if p <= 250:
        return 1000
    return 1000 - (min(p, 1000) - 250) * 600 // 750


def f_cost(intent):
    if intent["lane"] == "script":
        return 1000 - min(len(intent.get("script") or "") // 4096, 3) * 100  # one bridge job, no vision act
    return max(100, 700 - 150 * (len(intent.get("keys") or []) - 1))          # one act + one look per key


def f_freshness(intent, head, identical_pixels):
    """Returns (value, gate). GUI must cite the head (or byte-identical pixels); scripts decay with lag."""
    b = intent["based_on_frame"]
    lag = head - b
    if lag < 0 or lag > FRAME_RING:
        return 0, "FRAME_EVICTED"
    if intent["lane"] == "gui":
        if lag == 0:
            return 1000, None
        return (900, None) if identical_pixels else (0, "STALE_FRAME")
    return 1000 - lag * 600 // FRAME_RING, None


def _malformed(it):
    if not isinstance(it, dict) or it.get("rec", "enqueue") != "enqueue":
        return "not an enqueue record"
    for k in ("event_id", "agent_id"):
        if not isinstance(it.get(k), str) or not it[k]:
            return "missing %s" % k
    if it.get("lane") not in ("script", "gui"):
        return "lane must be script|gui"
    if type(it.get("based_on_frame")) is not int:
        return "based_on_frame must be an integer"
    if it["lane"] == "script" and not (isinstance(it.get("script"), str) and it["script"].strip()):
        return "script lane without script"
    if it["lane"] == "gui" and not (isinstance(it.get("keys"), list) and it["keys"]):
        return "gui lane without keys"
    return None


def _gated(gate, reason, feats=None):
    return {"v": V, "score": 0, "gate": gate, "reason": reason, "features": feats or {},
            "weights": dict(WEIGHTS), "llm_adj": 0}


def score_intent(intent, head_frame, history, claims, gui_agents=None, identical_pixels=False, advice=None):
    """Readiness of one queued intent. Pure: same arguments -> same integers.

    claims            {"agent": ["col:X", ...]} (RT/claims.json)
    gui_agents        agents holding the GUI lease, or None when the caller does not restrict it
    identical_pixels  caller's proof that frames [based_on_frame, head] are byte-identical
    advice            cached LLM advice {"<event_id>": {"adj": int, "intent_sig": hex}} or None
    """
    bad = _malformed(intent)
    if bad:
        return _gated("BAD_INTENT", bad)
    agent, lane, eid = intent["agent_id"], intent["lane"], intent["event_id"]
    feats = {}
    # claim
    if lane == "script":
        ws = intent.get("write_set") or []
        own = set((claims or {}).get(agent, []))
        if not ws or not set(ws) <= own:
            return _gated("CLAIM_MISMATCH", "write_set %s outside claim %s" % (sorted(ws), sorted(own)))
    elif gui_agents is not None and agent not in gui_agents:
        return _gated("NO_LEASE", "agent holds no GUI lease")
    feats["claim"] = 1000
    # freshness
    feats["freshness"], gate = f_freshness(intent, head_frame, identical_pixels)
    if gate:
        return _gated(gate, "based_on_frame %d against head %d" % (intent["based_on_frame"], head_frame), feats)
    # retry
    drops = history.drops.get(eid, 0)
    if drops >= MAX_DROPS:
        return _gated("PARKED", "event dropped %d times" % drops, feats)
    feats["retry"] = (1000, 600, 300)[drops]
    feats["track"] = history.track(agent)
    p = history.expected_effect(agent, lane)
    tr = intent.get("target_region")
    if p is None and tr:
        p = area_permille([tr], history.screen)
    feats["effect"] = f_effect(p)
    feats["cost"] = f_cost(intent)
    base = sum(WEIGHTS[k] * feats[k] for k in WEIGHTS) // 1000
    adj = 0
    a = (advice or {}).get(eid)
    if isinstance(a, dict) and a.get("intent_sig") == intent_sig(intent) and type(a.get("adj")) is int:
        adj = max(-LLM_CLAMP, min(LLM_CLAMP, a["adj"]))
    return {"v": V, "score": max(0, min(1000, base + adj)), "gate": None, "reason": None, "features": feats,
            "weights": dict(WEIGHTS), "base": base, "llm_adj": adj, "effect_permille": p}


def score_queue(intents, head_frame, history, claims, **kw):
    """Score every queued intent; returns {event_id: result}. Ordering is the auction's business."""
    return {it.get("event_id") if isinstance(it, dict) else None: score_intent(it, head_frame, history, claims, **kw)
            for it in intents}


class Judge:
    """Stateful wrapper for the scheduler plug-in: one History, fed record by record by the broker.

        j = Judge(claims); j.observe(record) for every record appended to events.jsonl
        j.scorer(intent, head_frame) -> int          (au-auction's scorer signature)
        j.score(intent, view) -> full breakdown      (view["head_frame"], optional view["advice"])
    """

    def __init__(self, claims, gui_agents=None, history=None, screen=SCREEN):
        self.claims, self.gui_agents = claims, gui_agents
        self.history = history or History(screen)
        self.advice = None  # set to the parsed RT/judge_advice.json between turns, never read here

    @classmethod
    def from_rt(cls, rt, gui_agents=None):
        """Cold start (not the hot path): rebuild from RT/events.jsonl and RT/claims.json."""
        import os
        try:
            with open(os.path.join(rt, "claims.json")) as f:
                claims = json.load(f)
        except (OSError, ValueError):
            claims = {}
        return cls(claims, gui_agents, History.from_events(os.path.join(rt, "events.jsonl")))

    def observe(self, record):
        self.history.feed(record)

    def score(self, intent, view=None, **kw):
        view = view or {}
        head = view.get("head_frame", self.history.head_frame)
        kw.setdefault("advice", view.get("advice", self.advice))
        return score_intent(intent, head, self.history, self.claims, gui_agents=self.gui_agents, **kw)

    def scorer(self, intent, head_frame=None):
        v = {} if head_frame is None else {"head_frame": head_frame}
        return self.score(intent, v)["score"]
