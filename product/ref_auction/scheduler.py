"""ViewPoint reference turn-order auction (Track P, Claude-written; plug-in, stdlib only).

Replaces "agent = ring[turn % N]" behind a scheduler interface (room viewpoint-auction, au-market D1-D3):

    pick   = scheduler.pick(view)            # proposal only; the broker's admit() stays the authority
    recs   = scheduler.settle(pick, close)   # after the broker wrote its close record

The lot is ONE turn (one head event, fixed hold). Everything is integer arithmetic, no wall clock,
no I/O: the same view sequence always yields the same records, and every "auction" record can be
re-decided from its own fields (verify_log).

Rule `vickrey_aging_v1` (per turn):
  1. eligible = head intent of every unparked agent with a queued intent.
  2. bid_eff  = clamp(bid, 0, min(balance, bid_cap)); an oversized bid is clamped, never refused.
  3. bonus    = readiness * r_max // 1000 (readiness 0..1000 comes from the judge; it is not a bid).
     score    = bid_eff + alpha * wait + bonus.
  4. aging guard: if some eligible agent has wait >= w_max, the longest waiter wins at price 0.
  5. readiness tier: an intent with readiness 0 (stale frame, claim mismatch) cannot win against a
     ready one whatever it bids; it is served by the guard, or when nobody is ready.
  6. otherwise max score wins; ties go to ring order starting after the last served agent.
  7. price = clamp(runner_up_score - alpha*wait_w - bonus_w, 0, bid_eff_w), charged at pick,
     refunded on a broker-fault close (ACTUATOR_BUSY). Budgets are play money held here.
`wait` = picks an agent sat through while eligible; it resets on ANY pick of that agent.
Bound (tested): an eligible agent is picked within w_max + N - 1 picks.
"""

POLICY = "vickrey_aging_v1"
BROKER_FAULT = ("ACTUATOR_BUSY",)
UNCERTAIN = ("OUTCOME_UNCERTAIN", "INPUT_UNCERTAIN")


def _int(x, default=0):
    """Agent-supplied numbers are untrusted: only plain ints count (bool, float, str, None -> default)."""
    return x if isinstance(x, int) and not isinstance(x, bool) else default


def default_readiness(intent, head_frame):
    """Deterministic stand-in for the judge: a GUI intent drafted on another frame is not ready."""
    if not isinstance(intent, dict):
        return 0
    if intent.get("lane") == "gui" and _int(intent.get("based_on_frame"), -1) != head_frame:
        return 0
    return 1000


class Params(object):
    FIELDS = ("alpha", "w_max", "income", "balance_cap", "bid_cap", "r_max", "balance_init")

    def __init__(self, n, alpha=1, w_max=None, income=1, balance_cap=None, bid_cap=None, r_max=None,
                 balance_init=None, aging_guard=True, clamp_bids=True, use_readiness=True):
        if n < 1:
            raise ValueError("need at least one agent")
        self.n = n
        self.alpha = alpha
        self.w_max = 2 * n if w_max is None else w_max
        self.income = income
        self.balance_cap = 4 * n if balance_cap is None else balance_cap
        self.bid_cap = 2 * n if bid_cap is None else bid_cap
        self.r_max = 2 * n if r_max is None else r_max
        self.balance_init = 2 * n if balance_init is None else balance_init
        # The three switches below exist ONLY for negative controls; production keeps them True.
        self.aging_guard, self.clamp_bids, self.use_readiness = aging_guard, clamp_bids, use_readiness
        for f in self.FIELDS:
            v = getattr(self, f)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError("%s must be a non-negative int" % f)

    def public(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        for f in ("aging_guard", "clamp_bids", "use_readiness"):
            if not getattr(self, f):
                d[f] = False  # a disabled safety is visible in every record
        return d

    @classmethod
    def from_public(cls, n, d):
        return cls(n, **d)

    @property
    def bound(self):
        """Max picks between an agent becoming eligible and being picked (inclusive)."""
        return self.w_max + self.n - 1


def ring_rank(ring, last_served):
    """Agent -> position in the ring walk that starts just after the last served agent."""
    n = len(ring)
    start = (ring.index(last_served) + 1) % n if last_served in ring else 0
    return {a: (i - start) % n for i, a in enumerate(ring)}


def decide(entries, ring, last_served, p):
    """Pure decision. entries: [{agent_id, event_id, bid, wait, readiness, balance}] (raw values).
    Returns (rows, winner_row|None, reason, price, runner_up_agent|None); rows carry the derived
    bid_eff, bonus, score and are in ring order."""
    rank = ring_rank(ring, last_served)
    rows = []
    for e in entries:
        bal = max(_int(e.get("balance")), 0)
        raw = max(_int(e.get("bid")), 0)
        eff = min(raw, bal, p.bid_cap) if p.clamp_bids else raw
        rd = min(max(_int(e.get("readiness")), 0), 1000) if p.use_readiness else 1000
        wait = max(_int(e.get("wait")), 0)
        bonus = rd * p.r_max // 1000 if p.use_readiness else 0
        ready = rd > 0
        rows.append({"agent_id": e["agent_id"], "event_id": e["event_id"], "bid": raw, "bid_eff": eff,
                     "wait": wait, "readiness": rd, "bonus": bonus, "balance": bal,
                     "score": eff + p.alpha * wait + bonus if ready else 0})
    rows.sort(key=lambda r: ring.index(r["agent_id"]))
    if not rows:
        return rows, None, "none", 0, None
    oldest = min(rows, key=lambda r: (-r["wait"], rank[r["agent_id"]]))
    if p.aging_guard and oldest["wait"] >= p.w_max:
        return rows, oldest, "aging_guard", 0, None
    if len(rows) == 1:
        return rows, rows[0], "sole", 0, None
    pool = [r for r in rows if r["readiness"] > 0]
    if not pool:
        return rows, oldest, "all_unready", 0, None
    order = sorted(pool, key=lambda r: (-r["score"], rank[r["agent_id"]]))
    w = order[0]
    if len(order) == 1:
        return rows, w, "auction", 0, None
    ru = order[1]
    price = min(max(ru["score"] - p.alpha * w["wait"] - w["bonus"], 0), w["bid_eff"])
    return rows, w, "auction", price, ru["agent_id"]


class RoundRobinScheduler(object):
    """The frozen H1 rule behind the same interface (baseline for A/B): ring[turn % N], no records."""
    policy = "round_robin"

    def __init__(self, ring):
        self.ring = tuple(ring)

    def pick(self, view):
        agent = self.ring[view["turn"] % len(self.ring)]
        eid = next((e["event_id"] for e in view.get("eligible", ()) if e["agent_id"] == agent), None)
        return {"agent_id": agent, "event_id": eid, "reason": "round_robin", "price": 0, "records": []}

    def settle(self, pick, close_rec):
        return []


class AuctionScheduler(object):
    policy = POLICY

    def __init__(self, ring, params=None, scorer=default_readiness, **kw):
        self.ring = tuple(ring)
        if len(set(self.ring)) != len(self.ring):
            raise ValueError("duplicate agent in ring")
        self.p = params or Params(len(self.ring), **kw)
        self.scorer = scorer
        self.balance = {a: min(self.p.balance_init, self.p.balance_cap) for a in self.ring}
        self.wait = {a: 0 for a in self.ring}
        self.last_served = None
        self.picks = 0
        self._open = None  # the pick awaiting settle (escrow)

    # ---- view helpers
    def _readiness(self, view, e):
        s = (view.get("scores") or {}).get(e["agent_id"], {})
        if isinstance(s, dict) and e["event_id"] in s:
            return _int(s[e["event_id"]])
        try:
            v = self.scorer(e.get("intent"), view.get("head_frame"))
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        except Exception:
            pass
        self._errs.append(e["agent_id"])  # a broken judge never promotes an intent, and it shows
        return 0

    def pick(self, view):
        turn, tick = view.get("turn"), view.get("tick")
        base = {"v": 1, "rec": "auction", "turn": turn, "tick": tick, "policy": POLICY,
                "params": self.p.public(), "head_frame": view.get("head_frame")}
        if view.get("mode", "IDLE") != "IDLE":  # RECONCILE / FROZEN / HI control: no auction
            base.update({"eligible": [], "winner": None, "event_id": None, "reason": "hold", "price": 0,
                         "runner_up": None, "last_served": self.last_served})
            return {"agent_id": None, "event_id": None, "reason": "hold", "price": 0, "records": [base]}
        seen, entries, self._errs = set(), [], []
        bids = view.get("bids") or {}
        for e in view.get("eligible", ()):
            a = e.get("agent_id")
            if a not in self.balance or a in seen:
                continue  # unknown agent or a second intent of the same agent: only the FIFO head competes
            seen.add(a)
            b = bids.get(a, {})
            entries.append({"agent_id": a, "event_id": e.get("event_id"),
                            "bid": b.get(e.get("event_id"), 0) if isinstance(b, dict) else 0,
                            "wait": self.wait[a], "readiness": self._readiness(view, e),
                            "balance": self.balance[a]})
        prev = self.last_served
        rows, w, reason, price, ru = decide(entries, self.ring, prev, self.p)
        for a in self.ring:  # wait counts picks sat through while eligible
            self.wait[a] = self.wait[a] + 1 if (a in seen and w is not None and a != w["agent_id"]) else 0
        if w is not None:
            self.balance[w["agent_id"]] -= price
            self.last_served = w["agent_id"]
            self.picks += 1
        base.update({"eligible": rows, "winner": w and w["agent_id"], "event_id": w and w["event_id"],
                     "reason": reason, "price": price, "runner_up": ru, "last_served": prev,
                     "balances": dict(self.balance), "waits": dict(self.wait)})
        if self._errs:
            base["scorer_errors"] = list(self._errs)
        pick = {"agent_id": w and w["agent_id"], "event_id": w and w["event_id"], "reason": reason,
                "price": price, "turn": turn, "records": [base], "settled": False}
        self._open = pick if w is not None else None
        return pick

    def settle(self, pick, close_rec):
        """Idempotent: the escrow of one pick is settled once. Returns the records to append."""
        if not pick or pick.get("agent_id") is None or pick.get("settled") or pick is not self._open:
            return []
        pick["settled"] = True
        self._open = None
        a, verdict = pick["agent_id"], (close_rec or {}).get("verdict")
        refunded = pick["price"] if verdict in BROKER_FAULT else 0
        self.balance[a] += refunded
        income, clipped = 0, 0
        if (close_rec or {}).get("verify_frame_ref") is not None:  # a tick closed: everyone earns
            income = self.p.income
            for x in self.ring:
                new = self.balance[x] + income
                clipped += max(new - self.p.balance_cap, 0)
                self.balance[x] = min(new, self.p.balance_cap)
        over = max(self.balance[a] - self.p.balance_cap, 0)  # a refund never lifts above the cap
        self.balance[a] -= over
        clipped += over
        return [{"v": 1, "rec": "auction_settle", "turn": pick.get("turn"), "agent_id": a,
                 "event_id": pick["event_id"], "verdict": verdict, "charged": pick["price"],
                 "refunded": refunded, "held": verdict in UNCERTAIN, "income": income, "clipped": clipped,
                 "balance": self.balance[a], "balances": dict(self.balance)}]

    # ---- restart: state is rebuilt from the order of record, never from memory
    def restore(self, records):
        for r in records:
            if not isinstance(r, dict) or r.get("policy", POLICY) != POLICY:
                continue
            if r.get("rec") == "auction" and r.get("reason") != "hold":
                self.wait.update({a: v for a, v in (r.get("waits") or {}).items() if a in self.wait})
                self.balance.update({a: v for a, v in (r.get("balances") or {}).items() if a in self.balance})
                if r.get("winner") in self.balance:
                    self.last_served = r["winner"]
                    self.picks += 1
            elif r.get("rec") == "auction_settle":
                self.balance.update({a: v for a, v in (r.get("balances") or {}).items() if a in self.balance})
        return self


def verify_log(records, ring, params=None):
    """Replay oracle: re-decide every auction record from its raw fields and re-derive the ledger per
    agent along the log. Returns a list of violation strings (empty = consistent).

    Params are PINNED: to `params` (a Params or its public dict = the run configuration) when given,
    else to the first auction record; any later record that logs different params is flagged and is
    still re-decided with the pinned ones. A disabled safety switch is flagged unless the caller
    passed it explicitly in `params`. A settle is tied to the pick it closes (agent, event, charged ==
    price), refund is exactly the price on a broker fault and 0 otherwise, income is 0 or
    params.income, and every balance is recomputed (refund, income, cap clip), so creation, a
    transfer between agents or a balance above the cap cannot pass by keeping the sum."""
    bad, bal, n = [], None, len(ring)
    pin = params.public() if isinstance(params, Params) else (dict(params) if isinstance(params, dict) else None)
    explicit, p, open_pick = pin is not None, None, None
    for i, r in enumerate(records):
        kind = r.get("rec") if isinstance(r, dict) else None
        if kind == "auction":
            if pin is None:
                pin = dict(r.get("params") or {})
            if p is None:
                try:
                    p = Params.from_public(n, pin)
                except (TypeError, ValueError) as e:
                    return bad + ["record %d: invalid pinned params %r (%s)" % (i, pin, e)]
                off = [f for f in ("aging_guard", "clamp_bids", "use_readiness") if not getattr(p, f)]
                if off and not explicit:
                    bad.append("record %d: safety switch disabled in params: %s" % (i, ",".join(off)))
            if r.get("params") != pin:
                bad.append("record %d: params drift: logged %r, pinned %r" % (i, r.get("params"), pin))
        if kind == "auction" and r.get("reason") != "hold":
            rows, w, reason, price, ru = decide(r["eligible"], ring, r.get("last_served"), p)
            got = (w and w["agent_id"], w and w["event_id"], reason, price, ru)
            want = (r.get("winner"), r.get("event_id"), r.get("reason"), r.get("price"), r.get("runner_up"))
            if got != want:
                bad.append("record %d: logged %r, replay %r" % (i, want, got))
            if rows != r["eligible"]:
                bad.append("record %d: derived columns (bid_eff/bonus/score) do not match raw fields" % i)
            for row in rows:
                if bal is not None and bal.get(row["agent_id"]) != row["balance"]:
                    bad.append("record %d: %s balance %r, ledger says %r" % (i, row["agent_id"], row["balance"],
                                                                           bal.get(row["agent_id"])))
                if p.aging_guard and row["wait"] > p.bound - 1:
                    bad.append("record %d: %s waited %d > bound" % (i, row["agent_id"], row["wait"]))
            after = r.get("balances") or {}
            exp = dict(bal) if bal is not None else dict(after, **{x["agent_id"]: x["balance"] for x in rows})
            if w is not None:
                if price < 0 or (p.clamp_bids and (price > w["bid_eff"] or price > w["balance"])):
                    bad.append("record %d: price %r outside [0, bid_eff]" % (i, price))
                exp[w["agent_id"]] = exp.get(w["agent_id"], 0) - price
            if after != exp:
                bad.append("record %d: balances after pick %r, ledger says %r" % (i, after, exp))
            for a in sorted(after):
                if a not in ring or _int(after[a], -1) < 0 and p.clamp_bids or _int(after[a], p.balance_cap + 1) > p.balance_cap:
                    bad.append("record %d: %s balance %r outside [0, balance_cap] or unknown agent" % (i, a, after[a]))
            bal = dict(r.get("balances") or {}) or bal
            open_pick = (w["agent_id"], w["event_id"], price) if w is not None else None
        elif kind == "auction_settle" and bal is not None:
            a = r.get("agent_id")
            charged, refunded, income = r.get("charged"), r.get("refunded"), r.get("income")
            if open_pick is None:
                bad.append("record %d: settle without an open pick" % i)
            else:
                if (a, r.get("event_id")) != open_pick[:2]:
                    bad.append("record %d: settle for %r, open pick is %r" % (i, (a, r.get("event_id")), open_pick[:2]))
                if charged != open_pick[2]:
                    bad.append("record %d: charged %r, pick price was %r" % (i, charged, open_pick[2]))
            open_pick = None
            want_ref = charged if r.get("verdict") in BROKER_FAULT else 0
            if refunded not in (0, charged):
                bad.append("record %d: partial refund" % i)
            if refunded and r.get("verdict") not in BROKER_FAULT:
                bad.append("record %d: refund on %s" % (i, r.get("verdict")))
            if refunded != want_ref:
                bad.append("record %d: refunded %r, rule says %r on %s" % (i, refunded, want_ref, r.get("verdict")))
            if income not in (0, p.income):
                bad.append("record %d: income %r, params say 0 or %r" % (i, income, p.income))
            ref, inc = _int(refunded), _int(income)
            exp, clipped = {}, 0
            for x in set(bal) | set(ring):
                new = bal.get(x, 0) + inc + (ref if x == a else 0)
                clipped += max(new - p.balance_cap, 0)
                exp[x] = min(new, p.balance_cap)
            got = r.get("balances") or {}
            if got != exp:
                diff = {x: (got.get(x), exp.get(x)) for x in sorted(set(got) | set(exp)) if got.get(x) != exp.get(x)}
                bad.append("record %d: balances after settle differ from ledger (logged, expected): %r" % (i, diff))
            if r.get("clipped") != clipped:
                bad.append("record %d: clipped %r, ledger says %r" % (i, r.get("clipped"), clipped))
            if sum(_int(v) for v in got.values()) != sum(bal.values()) + ref + inc * n - _int(r.get("clipped")):
                bad.append("record %d: money not conserved at settle" % i)
            if r.get("balance") != got.get(a):
                bad.append("record %d: settle balance %r != balances[%s] %r" % (i, r.get("balance"), a, got.get(a)))
            bal = dict(got)
    return bad
