"""Optional LLM judgement, OUT of the hot path (Track P, Claude Fable 5.2).

Run between turns (or never). For each queued intent it asks a cheap model for a bounded
adjustment and caches it in RT/judge_advice.json, keyed by event_id and bound to the intent
digest. score_intent() only reads that cache: no model call ever sits inside a turn, and a
missing, late, malformed or out-of-date answer simply means adjustment 0.

    VP_JUDGE_LLM_CMD='<command reading a prompt on stdin, printing {"adj": N, "why": "..."}>' \
        python3 -m judge.llm_advice --rt /dev/shm/vqa-cu/ref

The command is the operator's choice (a cheap seat such as Haiku). Nothing here opens a socket.
"""
import argparse
import glob
import json
import os
import shlex
import subprocess
import sys
import time

from .judge import History, intent_sig, score_intent, LLM_CLAMP

PROMPT = """You review one queued action of an AI agent that shares a single Blender session with other agents.
Deterministic readiness score (0..1000): {score}. Features: {features}.
Agent: {agent}. Lane: {lane}. Expected effect stated by the agent: {expect!r}
Action (truncated): {body!r}
Reply with ONE JSON object and nothing else: {{"adj": <integer from -{c} to {c}>, "why": "<one sentence>"}}.
Negative adj if the action looks unlikely to produce the stated effect or likely to disturb other agents' work."""


def parse_reply(text):
    """Accepts only a JSON object with an integer adj; anything else is None (no adjustment)."""
    try:
        i, j = text.index("{"), text.rindex("}")
        o = json.loads(text[i:j + 1])
    except ValueError:
        return None
    if not isinstance(o, dict) or type(o.get("adj")) is not int:
        return None
    return {"adj": max(-LLM_CLAMP, min(LLM_CLAMP, o["adj"])), "why": str(o.get("why", ""))[:200]}


def advise(intent, det, cmd, timeout_s=30):
    body = intent.get("script") if intent.get("lane") == "script" else " ".join(map(str, intent.get("keys") or []))
    prompt = PROMPT.format(score=det["score"], features=json.dumps(det["features"], sort_keys=True),
                           agent=intent.get("agent_id"), lane=intent.get("lane"),
                           expect=str(intent.get("expect", ""))[:400], body=str(body or "")[:1500], c=LLM_CLAMP)
    try:
        p = subprocess.run(shlex.split(cmd), input=prompt, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    r = parse_reply(p.stdout)
    if r:
        r.update({"intent_sig": intent_sig(intent), "ts_ms": int(time.time() * 1000), "cmd": shlex.split(cmd)[0]})
    return r


def run(rt, cmd, timeout_s=30):
    try:
        with open(os.path.join(rt, "claims.json")) as f:
            claims = json.load(f)
    except (OSError, ValueError):
        claims = {}
    h = History.from_events(os.path.join(rt, "events.jsonl"))
    out_path = os.path.join(rt, "judge_advice.json")
    try:
        with open(out_path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    for path in sorted(glob.glob(os.path.join(rt, "intents", "*", "*.json"))):
        try:
            with open(path) as f:
                it = json.load(f)
        except (OSError, ValueError):
            continue
        det = score_intent(it, h.head_frame, h, claims)
        if det["gate"]:
            continue  # nothing to advise on: the deterministic gate already said no
        eid = it["event_id"]
        if cache.get(eid, {}).get("intent_sig") == intent_sig(it):
            continue  # one call per intent version
        r = advise(it, det, cmd, timeout_s)
        if r:
            cache[eid] = r
    tmp = out_path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(cache, f, sort_keys=True)
    os.replace(tmp, out_path)
    return cache


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rt", default=os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref"))
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)
    cmd = os.environ.get("VP_JUDGE_LLM_CMD")
    if not cmd:
        print("VP_JUDGE_LLM_CMD is not set: no advice written (the judge stays deterministic)", file=sys.stderr)
        return 2
    print(json.dumps({"advised": len(run(a.rt, cmd, a.timeout))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
