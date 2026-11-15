"""Deterministic model broker for scheduler tests and the matched A/B against round-robin
(Track P, Claude-written). No Blender, no vision, no clock, no randomness beyond a seeded LCG.

Model: one FIFO per agent; a turn serves the FIFO head of the picked agent. A GUI intent drafted on
an older frame closes STALE_FRAME and stays queued (the agent must re-observe); anything else closes
VERIFIED and advances the head frame. An agent re-observes every `refresh` rounds.

    python3 sim.py        prints the A/B table on the built-in synthetic trace
"""
import json

from scheduler import AuctionScheduler, RoundRobinScheduler

RING = ("modeler-agent", "props-agent", "look-agent", "camera-agent")


class Lcg(object):
    def __init__(self, seed):
        self.s = (seed * 2654435761 + 1) % (1 << 32)

    def below(self, n):
        self.s = (self.s * 1664525 + 1013904223) % (1 << 32)
        return (self.s >> 8) % n


class Agent(object):
    """lane 'script'|'gui'; bid int or f(round)->value; refresh: re-observe every N rounds (None = never);
    arrive: f(round)->bool, a new intent is queued when true and the FIFO is empty (default: saturated)."""

    def __init__(self, lane="script", bid=0, refresh=1, arrive=None, verdict=None):
        self.lane, self.bid, self.refresh, self.arrive, self.verdict = lane, bid, refresh, arrive, verdict


class Sim(object):
    def __init__(self, ring, sched, agents):
        self.ring, self.sched, self.agents = tuple(ring), sched, agents
        self.queues = {a: [] for a in self.ring}
        self.turn = self.tick = self.frame = 0
        self.n_ev = {a: 0 for a in self.ring}
        self.log, self.served, self.verdicts = [], [], {}
        self.eligible_since, self.max_gap = {}, {a: 0 for a in self.ring}
        self.picks = 0

    def round(self):
        r = self.turn
        for a in self.ring:
            ag, q = self.agents[a], self.queues[a]
            if not q and (ag.arrive is None or ag.arrive(r)):
                self.n_ev[a] += 1
                q.append({"event_id": "%s-%d" % (a, self.n_ev[a]), "agent_id": a, "lane": ag.lane,
                          "based_on_frame": self.frame})
            elif q and ag.refresh and r % ag.refresh == 0:
                q[0]["based_on_frame"] = self.frame
            if q:
                self.eligible_since.setdefault(a, self.picks)
        eligible = [{"agent_id": a, "event_id": self.queues[a][0]["event_id"], "lane": self.queues[a][0]["lane"],
                     "intent": self.queues[a][0]} for a in self.ring if self.queues[a]]
        bids = {}
        for e in eligible:
            b = self.agents[e["agent_id"]].bid
            bids[e["agent_id"]] = {e["event_id"]: b(r) if callable(b) else b}
        view = {"turn": self.turn, "tick": self.tick, "mode": "IDLE", "ring": list(self.ring),
                "head_frame": self.frame, "eligible": eligible, "bids": bids}
        pick = self.sched.pick(view)
        self.log.extend(pick["records"])
        agent = pick["agent_id"] or self.ring[self.turn % len(self.ring)]
        self.turn += 1
        q = self.queues[agent]
        close = {"rec": "close", "turn": self.turn, "agent_id": agent, "event_id": None, "verdict": "PASS",
                 "verify_frame_ref": None}
        if q:
            ev, ag = q[0], self.agents[agent]
            self.picks += 1
            gap = self.picks - self.eligible_since.pop(agent)
            self.max_gap[agent] = max(self.max_gap[agent], gap)
            close["event_id"] = ev["event_id"]
            if ag.verdict is not None:
                close["verdict"] = ag.verdict(self.turn)
            elif ev["lane"] == "gui" and ev["based_on_frame"] != self.frame:
                close["verdict"] = "STALE_FRAME"
            else:
                close["verdict"] = "VERIFIED"
            if close["verdict"] == "VERIFIED":
                q.pop(0)
                self.tick += 1
                self.frame += 1
                close["verify_frame_ref"] = self.frame
            if q:
                self.eligible_since[agent] = self.picks
            self.served.append(agent)
        self.verdicts[close["verdict"]] = self.verdicts.get(close["verdict"], 0) + 1
        self.log.append(close)
        self.log.extend(self.sched.settle(pick, close))
        return close

    def run(self, rounds):
        for _ in range(rounds):
            self.round()
        return self

    def stats(self):
        n = max(len(self.served), 1)
        return {"turns": self.turn, "verdicts": dict(sorted(self.verdicts.items())),
                "share_pct": {a: 100 * self.served.count(a) // n for a in self.ring},
                "max_gap": dict(self.max_gap)}


def ab_trace():
    """Synthetic trace for the matched A/B: one script agent, three GUI agents that re-observe at
    different paces (1, 2 and 5 rounds). Same agents, same arrivals, two schedulers."""
    return {RING[0]: Agent("script"), RING[1]: Agent("gui", refresh=1),
            RING[2]: Agent("gui", refresh=2), RING[3]: Agent("gui", refresh=5)}


def ab(rounds=400):
    out = {}
    for name, mk in (("round_robin", lambda: RoundRobinScheduler(RING)), ("auction", lambda: AuctionScheduler(RING))):
        out[name] = Sim(RING, mk(), ab_trace()).run(rounds).stats()
    return out


if __name__ == "__main__":
    print(json.dumps(ab(), indent=1, sort_keys=True))
