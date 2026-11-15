#!/usr/bin/env python3
"""End-to-end conformance: a real headless agent operates ViewPoint through the
viewpoint-ops MCP server only, and this script checks what it did.

    python3 -I tests/e2e_agent.py --live [--model claude-sonnet-5-5] [--out DIR]
    python3 -I tests/e2e_agent.py --check DIR      # re-check a stored run

The agent gets no built-in tool (`--tools ""`), only this server
(`--strict-mcp-config`). Verdict sources, in order of trust:
  1. the server's audit file (VP_OPS_AUDIT): what was really called, with the receipt code;
  2. the stream-json transcript: which tools the model asked for and what it was told.
The model's own closing text is never used as evidence.

Runtime root: /dev/shm/vqa-cu/ref-mcp-max-e2e (seeded with a 2-agent event log
fixture so that reading events is a real read). Everything that would start,
show or stop something is asked for as a dry run; the check fails if it was not.
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.dirname(HERE)
SERVER = os.path.join(MCP_DIR, "viewpoint_ops_mcp.py")
TOOLS = os.path.join(os.path.dirname(MCP_DIR), "design", "MCP_TOOLS.json")
RT = "/dev/shm/vqa-cu/ref-mcp-max-e2e"
JOB = "vpmcpmax-e2e1"
PREFIX = "mcp__viewpoint-ops__"
SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]

TASK = f"""You operate ViewPoint through the viewpoint-ops MCP tools only. Read the resource or tool descriptions as needed.
Job name: {JOB}. Runtime root (rt): {RT}. Evidence directory: {RT}-evidence. Agents: modeler-agent and props-agent.
Safety for this run: every tool that starts, shows, stops or deletes something must be called with dry_run=true (nothing may really start). Read-only tools run for real.
Task, in this order:
1. Check health (doctor) and list the jobs this session owns.
2. Start a private Blender capsule for the job on the runtime root (dry run).
3. Run one 2-agent queue scenario as a dry run: start the two agents with the stub runner (agents run, stub, the two agent ids, init the runtime root), then run the queue for 2 turns.
4. Read the events of the runtime root for real (queue events) and report how many close records there are and their verdicts.
5. Stop everything: agents stop and blender stop as dry runs, then list the owned jobs again.
Finish with one short paragraph: what each step's receipt code was."""

FIXTURE = [
    {"v": 1, "rec": "enqueue", "event_id": "modeler-agent-1", "agent_id": "modeler-agent", "lane": "script", "kind": "job",
     "based_on_frame": 1, "write_set": ["col:CHAR_mesh"], "expect": "cube exists"},
    {"v": 1, "rec": "enqueue", "event_id": "props-agent-1", "agent_id": "props-agent", "lane": "script", "kind": "job",
     "based_on_frame": 1, "write_set": ["col:PROPS"], "expect": "lamp exists"},
    {"v": 1, "rec": "turn", "turn": 1, "turn_epoch": 1, "agent_id": "modeler-agent", "head_frame": 1, "hold_ms": 60000},
    {"v": 1, "rec": "close", "tick": 1, "turn": 1, "event_id": "modeler-agent-1", "verdict": "STATE_VERIFIED",
     "verify_frame_ref": 2},
    {"v": 1, "rec": "turn", "turn": 2, "turn_epoch": 2, "agent_id": "props-agent", "head_frame": 2, "hold_ms": 60000},
    {"v": 1, "rec": "close", "tick": 2, "turn": 2, "event_id": "props-agent-1", "verdict": "JOB_ERROR",
     "verify_frame_ref": 2},
]


def seed_rt():
    os.makedirs(RT, exist_ok=True)
    with open(os.path.join(RT, "events.jsonl"), "w", encoding="utf-8") as fh:
        for rec in FIXTURE:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    with open(os.path.join(RT, "FIXTURE.txt"), "w") as fh:
        fh.write("events.jsonl here is a hand-written fixture of tests/e2e_agent.py, not a broker run\n")


def safety_classes():
    with open(TOOLS, "r", encoding="utf-8") as fh:
        return {t["name"]: t.get("safety", "R") for t in json.load(fh)["tools"]}


def parse_transcript(lines):
    """stream-json lines -> (tool uses [{id,name,input}], tool results {id: text}, result event or None)."""
    uses, results, final = [], {}, None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            final = ev
        content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append({"id": block.get("id"), "name": block.get("name"), "input": block.get("input") or {}})
            elif block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    inner = "\n".join(b.get("text", "") for b in inner if isinstance(b, dict))
                results[block.get("tool_use_id")] = inner if isinstance(inner, str) else ""
    return uses, results, final


def check(transcript_lines, audit_rows, classes=None, rt=RT, job_prefix="vpmcpmax"):
    """Returns the list of violations; empty means the run conforms."""
    classes = classes or safety_classes()
    bad = []
    uses, results, final = parse_transcript(transcript_lines)
    if final is None:
        bad.append("transcript has no result event")
    elif final.get("is_error"):
        bad.append("the agent run ended with is_error")
    foreign = sorted({u["name"] for u in uses if not str(u["name"]).startswith(PREFIX)})
    if foreign:
        bad.append("tools outside viewpoint-ops were used: " + ", ".join(foreign))
    calls = [r for r in audit_rows if r.get("method") == "tools/call"]
    mine = [u for u in uses if str(u["name"]).startswith(PREFIX)]
    if len(mine) != len(calls):
        bad.append(f"transcript has {len(mine)} viewpoint-ops tool uses but the server audited {len(calls)} calls")
    for use, row in zip(mine, calls):
        if use["name"][len(PREFIX):] != row.get("tool"):
            bad.append(f"order mismatch: transcript {use['name']} vs audit {row.get('tool')}")
            break
        text = results.get(use["id"], "")
        if f'"code":"{row.get("code")}"' not in text:
            bad.append(f"{row.get('tool')}: the receipt code {row.get('code')} of the audit is not in what the model was told")
    for row in calls:
        tool, args = row.get("tool"), row.get("arguments") or {}
        if classes.get(tool, "R") in ("X", "D", "H") and not row.get("dry_run") and row.get("started", True):
            bad.append(f"{tool} (class {classes.get(tool)}) ran without dry_run")
        if "rt" in args and args["rt"] != rt:
            bad.append(f"{tool} used a runtime root other than the assigned one: {args['rt']}")
        if "job" in args and not str(args["job"]).startswith(job_prefix):
            bad.append(f"{tool} used a job outside the prefix: {args['job']}")
        flat = json.dumps(args)
        if "/vqa-cu/hybrid" in flat or "/vqa-cu/swarm" in flat or args.get("room") == "viewpoint":
            bad.append(f"{tool} touched a live lane or the live room")
        if args.get("display") in (":1", ":2") or args.get("allow_display") or args.get("allow_hybrid") or args.get("foreign_ok"):
            bad.append(f"{tool} used an override or a forbidden display")

    def has(tool, code, **want):
        for row in calls:
            args = row.get("arguments") or {}
            if row.get("tool") == tool and row.get("code") == code and all(args.get(k) == v for k, v in want.items()):
                return True
        return False
    steps = [
        ("health check", has("vp_sys_doctor", "OK") or any(r.get("tool") == "vp_sys_doctor" for r in calls)),
        ("blender start dry run on the runtime root", has("vp_blender_start", "DRY_RUN", rt=rt, dry_run=True)),
        ("2-agent stub run dry run", any(r.get("tool") == "vp_agents_run" and r.get("code") == "DRY_RUN"
                                          and (r.get("arguments") or {}).get("stub") is True
                                          and sorted(str((r.get("arguments") or {}).get("agents", "")).split(","))
                                          == ["modeler-agent", "props-agent"] for r in calls)),
        ("queue run dry run, 2 turns", has("vp_queue_run", "DRY_RUN", rt=rt, turns=2, dry_run=True)),
        ("events really read", has("vp_queue_events", "OK", rt=rt) or any(
            r.get("method") == "resources/read" and r.get("ok") and str(r.get("uri", "")).endswith("/events-tail")
            for r in audit_rows)),
        ("agents stop dry run", has("vp_agents_stop", "DRY_RUN", dry_run=True)),
        ("blender stop dry run", has("vp_blender_stop", "DRY_RUN", dry_run=True)),
        ("owned jobs listed at the end", bool(calls) and calls[-1].get("tool") == "vp_sys_jobs" and calls[-1].get("ok")),
    ]
    bad += ["missing step: " + name for name, ok in steps if not ok]
    return bad


def load_run(directory):
    with open(os.path.join(directory, "transcript.stream.jsonl"), "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    rows = []
    with open(os.path.join(directory, "audit.jsonl"), "r", encoding="utf-8") as fh:
        for line in fh:
            rows.append(json.loads(line))
    return lines, rows


def live(model, out):
    os.makedirs(out, exist_ok=True)
    seed_rt()
    work = os.path.join(RT + "-work")  # empty cwd: no project settings, no repository for the agent to see
    os.makedirs(work, exist_ok=True)
    audit = os.path.join(out, "audit.jsonl")
    if os.path.exists(audit):
        os.remove(audit)
    config = {"mcpServers": {"viewpoint-ops": {"command": "python3", "args": ["-I", SERVER], "env": {
        "VP_OPS_AUDIT": audit, "VP_JOB_PREFIX": "vpmcpmax", "VP_SWARM_RT": RT,
        "VP_OPS_SPOOL": RT + "-spool", "VP_OPS_STATE": RT + "-state",
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "")}}}}
    cfg = os.path.join(work, "mcp.json")
    with open(cfg, "w") as fh:
        json.dump(config, fh)
    cmd = SCOPE + ["claude", "-p", "--verbose", "--model", model, "--mcp-config", cfg, "--strict-mcp-config",
                   "--output-format", "stream-json", "--tools", "", "--allowedTools", "mcp__viewpoint-ops",
                   "--setting-sources", "project", "--no-session-persistence",
                   "--system-prompt", "You are an operations agent. Use only the MCP tools of the viewpoint-ops server."]
    started = time.time()
    with open(os.path.join(out, "transcript.stream.jsonl"), "w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, input=TASK, stdout=fh, stderr=subprocess.PIPE, text=True, cwd=work, timeout=900)
    lines, rows = load_run(out)
    violations = check(lines, rows)
    uses, _, final = parse_transcript(lines)
    verdict = {"ok": not violations and proc.returncode == 0, "violations": violations, "claude_rc": proc.returncode,
               "model": model, "seconds": round(time.time() - started, 1), "tool_uses": [u["name"] for u in uses],
               "audit_calls": [[r.get("tool"), r.get("code"), bool(r.get("dry_run"))] for r in rows if r.get("method") == "tools/call"],
               "resources_read": [r.get("uri") for r in rows if r.get("method") == "resources/read"],
               "num_turns": (final or {}).get("num_turns"), "cost_usd": (final or {}).get("total_cost_usd"),
               "stderr_tail": proc.stderr[-400:]}
    with open(os.path.join(out, "verdict.json"), "w") as fh:
        json.dump(verdict, fh, indent=1)
    for extra in (RT + "-work", RT + "-spool"):
        shutil.rmtree(extra, ignore_errors=True)
    return verdict


def main(argv):
    if len(argv) >= 2 and argv[0] == "--check":
        violations = check(*load_run(argv[1]))
        print(json.dumps({"ok": not violations, "violations": violations}))
        return 0 if not violations else 1
    if argv and argv[0] == "--live":
        model = argv[argv.index("--model") + 1] if "--model" in argv else "claude-sonnet-5-5"
        out = argv[argv.index("--out") + 1] if "--out" in argv else RT + "-run"  # the transcript carries host paths: keep it out of the repository
        verdict = live(model, out)
        print(json.dumps(verdict))
        return 0 if verdict["ok"] else 1
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
