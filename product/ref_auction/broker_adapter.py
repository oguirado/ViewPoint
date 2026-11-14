"""Glue between the plug-in scheduler and product/ref_broker (Track P, Claude-written).

Nothing here edits ref_broker. The broker needs ONE seam (see broker_hook.patch / patched_source):

    -        agent = self.agents[self.turn % len(self.agents)]
    +        agent = self._pick_agent()

with a default `_pick_agent` that returns the same ring agent, so a broker without a scheduler
behaves exactly as before. `make_auction_broker(Broker)` returns a subclass that fills the seam.
"""
import json
import os
import re
import sys

SEAM_OLD = "        agent = self.agents[self.turn % len(self.agents)]\n"
SEAM_NEW = "        agent = self._pick_agent()\n"
SEAM_METHOD = '''    def _pick_agent(self):
        """Scheduler seam (ref_auction plugs in here). Default: the frozen round-robin rule."""
        return self.agents[self.turn % len(self.agents)]

'''
SEAM_ANCHOR = "    def step(self):\n"
BID_MAX_BYTES = 4096
_ID_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*\Z")  # one path component, no '/', no '..'


def patched_source(src):
    """broker.py text with the seam. Raises if the round-robin line is not there exactly once."""
    if "def _pick_agent" in src:
        return src
    if src.count(SEAM_OLD) != 1 or src.count(SEAM_ANCHOR) != 1:
        raise ValueError("broker.py does not contain the round-robin line / step() exactly once")
    return src.replace(SEAM_OLD, SEAM_NEW).replace(SEAM_ANCHOR, SEAM_METHOD + SEAM_ANCHOR)


def read_bid(rt, agent_id, event_id):
    """RT/bids/<agent_id>/<event_id>.json {v:1,rec:'bid',agent_id,event_id,bid:int>=0}. Anything
    missing, oversized, unparseable, mislabelled or not a plain non-negative int is a bid of 0."""
    for x in (agent_id, event_id):  # agent-chosen ids never steer the path outside RT/bids/<agent>/
        if not isinstance(x, str) or len(x) > 128 or not _ID_OK.match(x):
            return 0
    p = os.path.join(rt, "bids", agent_id, event_id + ".json")
    try:
        if os.path.islink(p) or os.path.getsize(p) > BID_MAX_BYTES:
            return 0
        with open(p, "rb") as f:
            d = json.loads(f.read(BID_MAX_BYTES).decode("utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(d, dict) or d.get("agent_id") != agent_id or d.get("event_id") != event_id:
        return 0
    b = d.get("bid")
    return b if isinstance(b, int) and not isinstance(b, bool) and b >= 0 else 0


def view_from_broker(b):
    """Broker state only: FIFO head of every unparked agent, its sidecar bid, the head frame."""
    eligible, bids = [], {}
    for a in b.agents:
        q = b.queues.get(a) or []
        if a in b.parked or not q:
            continue
        ev = q[0]
        eligible.append({"agent_id": a, "event_id": ev["event_id"], "lane": ev.get("lane"),
                         "enq_seq": ev.get("seq"), "intent": ev})
        bids[a] = {ev["event_id"]: read_bid(b.rt, a, ev["event_id"])}
    return {"turn": b.turn, "tick": b.tick, "mode": b.mode, "ring": list(b.agents), "head_frame": b.frame,
            "eligible": eligible, "bids": bids}


def make_auction_broker(broker_cls):
    """Subclass of a seam-patched Broker. Set `.scheduler` after construction (None = round-robin)."""
    if not hasattr(broker_cls, "_pick_agent"):
        raise TypeError("broker class has no _pick_agent seam; apply broker_hook.patch first")

    class AuctionBroker(broker_cls):
        scheduler = None
        judge = None
        judge_errors = 0
        _pick = None
        _last_close = None
        _base_module = broker_cls.__module__

        def _append(self, rec):
            rec = broker_cls._append(self, rec)
            if rec.get("rec") == "close":
                self._last_close = rec
            if self.judge is not None:
                try:
                    self.judge.observe(rec)
                except Exception:
                    self.judge_errors += 1  # a judge fault never stops the broker
            return rec

        def _pick_agent(self):
            ring_agent = broker_cls._pick_agent(self)
            if self.scheduler is None:
                return ring_agent
            self._pick = self.scheduler.pick(view_from_broker(self))
            for r in self._pick["records"]:  # the decision is on record BEFORE the turn it orders
                self._append(r)
            return self._pick["agent_id"] or ring_agent

        def step(self):
            self._pick = self._last_close = None
            try:
                return broker_cls.step(self)
            finally:
                if self._pick is not None and self._last_close is not None:
                    for r in self.scheduler.settle(self._pick, self._last_close):
                        self._append(r)

    return AuctionBroker


def judge_scorer(judge):
    """scorer(intent, head_frame) -> int 0..1000 backed by au-judge's Judge (judge/judge.py)."""
    def scorer(intent, head_frame):
        return judge.scorer(intent, head_frame)
    return scorer


def attach_judge(broker, judge=None):
    """Wire the judge into an AuctionBroker: it is fed every record already in events.jsonl and every
    record appended from now on (its verdict-history feature), and it scores the auction rows."""
    if judge is None:
        from judge import Judge  # imported lazily: the judge is a separate unit
        gui = getattr(sys.modules.get(broker._base_module), "GUI_AGENTS", None)
        judge = Judge(broker.claims, gui_agents=gui)
    try:
        with open(broker.p_events) as f:
            for line in f:
                try:
                    judge.observe(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    broker.judge = judge
    broker.scheduler.scorer = judge_scorer(judge)
    return judge
