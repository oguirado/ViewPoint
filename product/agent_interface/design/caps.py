#!/usr/bin/env python3
"""Capability inventory with verified file:line (Track P, claude-fable-5-2, lane ops-design).

    python3 -I caps.py           # rewrite CAPABILITIES.md
    python3 -I caps.py --check   # rc 0 when every anchor is still on its cited line, rc 1 on drift or a missing anchor

Each row names a real entry point by file and an anchor string; the line number is found on disk, never typed.
Read-only on every cited file. Stdlib only.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# No absolute host path in this file or in CAPABILITIES.md: the workspace root that holds the checkouts is
# $VP_OPS_ROOT (default: the directory that holds this checkout); cites are printed as `$VP_OPS_ROOT/...` or `~/...`.
WS = os.environ.get("VP_OPS_ROOT") or os.path.normpath(os.path.join(HERE, "..", "..", "..", ".."))
HOME = os.path.expanduser("~")
D = WS + "/"
VP = D + "ViewPoint/"
HK = D + "viewpoint-hackathon/"
AG = VP + "repo/agents/metaviewer_agents/"
PB = VP + "hackathon/playbooks/"
SK = HOME + "/.claude/skills/"
TL = D + "FireBase_BuildHybridAI/repo/"
NP = D + "newton-py/"


def W(unit):
    return D + "vp-wt-fable-%s/product/" % unit


# (section, capability, file, anchor, vp command(s), note)
ROWS = [
    ("1. ViewPoint take launcher", "fresh start of the take (4 capsules on :98)", VP + "ctl/vp_demo_take.sh", "start)", "capsule start", "stops a running take first"),
    ("1. ViewPoint take launcher", "clean stop", VP + "ctl/vp_demo_take.sh", "stop) stop ;;", "capsule stop", ""),
    ("1. ViewPoint take launcher", "open the :98 screen for the operator (VNC, localhost)", VP + "ctl/vp_demo_take.sh", "view)", "capsule view", ""),
    ("1. ViewPoint take launcher", "native window (xpra), on HOLD upstream", VP + "ctl/vp_demo_take.sh", "show)", "capsule show", "rc 3 unless VP_SHOW_OK=1"),
    ("1. ViewPoint take launcher", "hide window or viewer", VP + "ctl/vp_demo_take.sh", "hide)", "capsule hide", ""),

    ("2. ViewPoint MCP server (vp_mcp)", "list capsules", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_list_capsules"', "capsule list, capsule status", ""),
    ("2. ViewPoint MCP server (vp_mcp)", "navigate a capsule", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_navigate"', "browser navigate", "https only, no query or fragment"),
    ("2. ViewPoint MCP server (vp_mcp)", "bounded browser goal runner", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_browser_task"', "browser task", "serialised by browser_lane_lock"),
    ("2. ViewPoint MCP server (vp_mcp)", "read page as AX text (untrusted)", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_read_page"', "browser read", ""),
    ("2. ViewPoint MCP server (vp_mcp)", "identity list", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_identity_list"', "identity list", "metadata only"),
    ("2. ViewPoint MCP server (vp_mcp)", "identity create", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_identity_create"', "identity create", ""),
    ("2. ViewPoint MCP server (vp_mcp)", "identity label", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_identity_set_label"', "identity label", ""),
    ("2. ViewPoint MCP server (vp_mcp)", "identity delete (confirm == id)", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_identity_delete"', "identity delete", "class D"),
    ("2. ViewPoint MCP server (vp_mcp)", "show / hide tools", VP + "hackathon/vp_mcp/vp_mcp.py", '"name": "vp_show"', "capsule show, capsule hide", ""),
    ("2. ViewPoint MCP server (vp_mcp)", "session file contract (VP_MCP_SESSION)", VP + "hackathon/vp_mcp/vp_mcp.py", "def load_session_file(path):", "browser *, identity *, engine *", "absolute path, validated"),
    ("2. ViewPoint MCP server (vp_mcp)", "stdio entry point", VP + "hackathon/vp_mcp/vp_mcp.py", "def main():", "(all vp_mcp wraps)", ""),
    ("2. ViewPoint MCP server (vp_mcp)", "published copy of the server", HK + "mcp/vp_mcp/vp_mcp.py", "def main():", "(same tools)", "public repo copy; the CLI wraps the ViewPoint one"),

    ("3. Engine agents (metaviewer_agents)", "mvctl v0.2 commands observe, list, propose, wait-verify", AG + "mvctl.py", 'parser.add_argument("command", choices=("observe", "list", "propose", "wait-verify",', "engine observe, engine list, engine propose, engine wait-verify", "record-start/record-stop exist upstream, not wrapped"),
    ("3. Engine agents (metaviewer_agents)", "mvctl entry point", AG + "mvctl.py", "def main(argv: Sequence[str] | None = None) -> int:", "engine *", ""),
    ("3. Engine agents (metaviewer_agents)", "mw_run surface kinds browser and program", AG + "mw_run.py", "parser.add_argument('--surface-kind', choices=('browser', 'program'), default='browser')", "engine run, browser task", ""),
    ("3. Engine agents (metaviewer_agents)", "mw_run entry point", AG + "mw_run.py", "def main(argv=None, *, runner=None):", "engine run", ""),
    ("3. Engine agents (metaviewer_agents)", "guest rewind session (pause, resume, mark, restore)", AG + "rewind.py", "class RewindSession:", "(library; the CLI path is rewind *)", "no CLI of its own"),
    ("3. Engine agents (metaviewer_agents)", "handoff latch", AG + "mw_handoff.py", "class HandoffLatch:", "human handback", "library; vp composes queue control + room handoff"),

    ("4. vision skill", "verbs doctor, start, look, act, check, stop, sweep, adopt, attach", D + "vis-skill/vis", "for verb in ('doctor','start','look','act','check','stop','sweep','adopt','attach'):", "vision doctor, vision start, vision look, vision act, vision check, vision stop, vision sweep", "adopt and attach are not wrapped (other sessions' displays)"),
    ("4. vision skill", "skill text (loop, failure codes)", SK + "vision/SKILL.md", "vision start --job J --app 'CMD' --wait-title 'Title'", "vision *", ""),

    ("5. Reference units, Claude Track P", "turn broker run / control", W("f1") + "ref_broker/broker.py", 'r = sub.add_parser("run")', "queue run", "rc 3 = FROZEN or RECONCILE"),
    ("5. Reference units, Claude Track P", "broker control stop, resume, reconcile", W("f1") + "ref_broker/broker.py", 'c.add_argument("--op", required=True, choices=("stop", "resume", "reconcile"))', "queue control, human handback, human resume", ""),
    ("5. Reference units, Claude Track P", "Blender bridge with nonce (RT/inbox, RT/outbox)", W("f1") + "ref_broker/bridge.py", 'RT = os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref")', "blender start, blender exec", ""),
    ("5. Reference units, Claude Track P", "recorder (refuses :1)", W("f2") + "ref_overlay/record.sh", "# record.sh <display> <name> <outdir>", "record start", ""),
    ("5. Reference units, Claude Track P", "off-display cursor compositor", W("f2") + "ref_overlay/compose.py", 'ap.add_argument("--video", required=True, help="recording mp4")', "record compose", ""),
    ("5. Reference units, Claude Track P", "projection to osb and TaskLight", W("f3") + "ref_projection/project.py", 'ap.add_argument("--osb-room", default="viewpoint-capsule-test")', "projection run", ""),
    ("5. Reference units, Claude Track P", "projection reconcile", W("f3") + "ref_projection/reconcile.py", 'ap.add_argument("--ledger", required=True)', "projection reconcile", ""),
    ("5. Reference units, Claude Track P", "agent runner run / stop / status", W("f4") + "ref_agents/runner.py", 'r = sub.add_parser("run")', "agents run, agents stop, agents status", "--stub runs without a model"),
    ("5. Reference units, Claude Track P", "intent contract validator", W("f4") + "ref_agents/contract.py", "def validate_intent(obj, claims=None, agent_id=None, event_id=None, newest=None):", "queue enqueue, queue validate", ""),
    ("5. Reference units, Claude Track P", "bpy time-budget guard", W("f4") + "ref_agents/guard.py", 'ap.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S)', "blender exec --budget-s", ""),
    ("5. Reference units, Claude Track P", "agent briefs", W("f4") + "ref_agents/briefs.py", "def main(argv=None):", "agents briefs", ""),
    ("5. Reference units, Claude Track P", "rewind contract schema (rw1)", W("r4") + "ref_rewind/schema.py", "", "rewind audit", "library"),
    ("5. Reference units, Claude Track P", "rewindctl run, mark, rewind, export", W("r4") + "ref_rewind_demo/rewindctl.py", 'for name in ("run", "mark", "rewind", "export"):', "rewind run, rewind checkpoint, rewind restore, rewind export", "in progress on p-fable-r4"),
    ("5. Reference units, Claude Track P", "rewindctl status, audit", W("r4") + "ref_rewind_demo/rewindctl.py", 'for name in ("status", "audit"):', "rewind status, rewind audit, queue status", "no Blender contact"),
    ("5. Reference units, Claude Track P", "rewindctl emit-script", W("r4") + "ref_rewind_demo/rewindctl.py", 'e = sub.add_parser("emit-script")', "rewind script", ""),
    ("5. Reference units, Claude Track P", "checkpoint store and ops", W("r4") + "ref_rewind_ckpt/store.py", "", "(through rewindctl)", "library"),
    ("5. Reference units, Claude Track P", "rewind broker lanes", W("r4") + "ref_rewind_lanes/rewind_broker.py", "", "(through rewindctl)", "library"),
    ("5. Reference units, Claude Track P", "rewind judge", W("r4") + "ref_rewind_verify/judge.py", "", "(through rewindctl)", "library"),
    ("5. Reference units, Claude Track P", "capsule spec validate", W("ct") + "ref_capsule/validate.py", "def main(argv):", "spec validate", ""),
    ("5. Reference units, Claude Track P", "capsule spec render (plan only)", W("ct") + "ref_capsule/render.py", "def main(argv):", "spec render", ""),
    ("5. Reference units, Claude Track P", "capsule spec observe", W("ct") + "ref_capsule/observe.py", "def main(argv):", "spec observe", ""),
    ("5. Reference units, Claude Track P", "auction scheduler A/B simulation", W("au") + "ref_auction/sim.py", "def ab(rounds=400):", "queue simulate", "scheduler itself is a library hook into the broker"),

    ("6. Swarm units, Gemini Track H (read-only, never copied)", "interface contract", HK + "swarm/CONTRACT.md", "# ViewPoint Swarm Capsule Interface Contract (`swarm/CONTRACT.md`)", "queue *, agents * with --impl swarm", "source of codes QUEUE_FULL, STALE_FRAME"),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "broker", HK + "swarm/broker/broker.py", 'p.add_argument("--vision-mode", choices=("live", "mock"), default="live")', "queue run --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "intent CLI observe", HK + "swarm/agents/intent_cli.py", 'p_obs = sub.add_parser("observe", help="Read latest.json and agent lease/close state")', "queue observe --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "intent CLI enqueue-script", HK + "swarm/agents/intent_cli.py", 'p_scr = sub.add_parser("enqueue-script", help="Validate and enqueue a script intent")', "queue enqueue --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "intent CLI enqueue-gui", HK + "swarm/agents/intent_cli.py", 'p_gui = sub.add_parser("enqueue-gui", help="Validate and enqueue a camera-agent GUI intent")', "queue enqueue --kind gui --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "agent runner", HK + "swarm/agents/runner.py", 'parser.add_argument("--kill-all", action="store_true")', "agents run --impl swarm", "--kill-all is NOT wrapped (stop by PID only)"),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "overlay compositor", HK + "swarm/overlay/compositor.py", 'p.add_argument("--events", "-e", required=True, type=Path, help="Broker events.jsonl path")', "record compose --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "projector", HK + "swarm/projection/projector.py", "def main(argv: Optional[Sequence[str]] = None) -> int:", "projection run --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "reconciler", HK + "swarm/projection/reconciler.py", "def main(argv: Optional[Sequence[str]] = None) -> int:", "projection reconcile --impl swarm", ""),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "recorder", HK + "swarm/tools/record.sh", "# Usage: swarm/tools/record.sh [--allow-display0] <display> <name>", "record start --impl swarm", "--allow-display0 is never passed"),
    ("6. Swarm units, Gemini Track H (read-only, never copied)", "compose wrapper", HK + "swarm/tools/compose.sh", "# Usage: swarm/tools/compose.sh <input.mp4> <events.jsonl> <output.mp4> [extra compositor flags...]", "record compose --impl swarm", ""),

    ("7. Blender hybrid lane (single agent)", "playbook", PB + "PLAYBOOK_HYDROGEN_BLENDER_VIDEO.md", "# Playbook: Hydrogen construye assets 3D en Blender, grabado y con logs", "E2E scenario 1", ""),
    ("7. Blender hybrid lane (single agent)", "live bridge (fixed root /dev/shm/vqa-cu/hybrid)", PB + "blender_hybrid/blender_bridge.py", 'INBOX="/dev/shm/vqa-cu/hybrid/inbox"; OUTBOX="/dev/shm/vqa-cu/hybrid/outbox"', "blender start --bridge hybrid --allow-hybrid", "root is hard-coded upstream"),
    ("7. Blender hybrid lane (single agent)", "run one script in the live Blender", PB + "blender_hybrid/bpy_live", "# usage: bpy_live SCRIPT.py", "blender exec --bridge hybrid --allow-hybrid", ""),
    ("7. Blender hybrid lane (single agent)", "recorder of display :1", PB + "blender_hybrid/rec_start.sh", "# usage: rec_start.sh NAME", "record start --allow-display :1", "display hard-coded to :1 upstream"),
    ("7. Blender hybrid lane (single agent)", "headless model run with a brief", PB + "blender_hybrid/run_hydrogen.sh", "# usage: run_hydrogen.sh BRIEF.md EVIDENCE_DIR [MODEL]", "agents solo", "adds /dev/shm/vqa-cu/hybrid to the model, hence --allow-hybrid"),
    ("7. Blender hybrid lane (single agent)", "operator mirror (ffplay)", PB + "blender_hybrid/viewer1080.sh", "ffmpeg -hide_banner -loglevel error -f x11grab", "record viewer", ""),
    ("7. Blender hybrid lane (single agent)", "transcript renderer", PB + "blender_hybrid/agy_transcript.py", "", "agents transcript", ""),
    ("7. Blender hybrid lane (single agent)", ".glb verification template", PB + "blender_hybrid/verify_glb_template.py", "", "blender verify", ""),

    ("8. Rooms, waves, TaskLight", "OpenSwitchboard CLI (post, read, wait, mirror, status, doctor)", D + "switchboard-skill/bin/osb", "", "room post, room read, room wait, room status, room mirror", ""),
    ("8. Rooms, waves, TaskLight", "switchboard skill text", SK + "switchboard/SKILL.md", "", "room *", ""),
    ("8. Rooms, waves, TaskLight", "swarm skill (pack, plan, run, wait, metrics, mark)", SK + "swarm/SKILL.md", "swarm-skill/swarm`", "agents wave", "seat policy is enforced by the skill"),
    ("8. Rooms, waves, TaskLight", "TaskLight launcher (--check, --viewpoint, --no-observer)", TL + "tools/demo/run_tasklight.mjs", "//   node tools/demo/run_tasklight.mjs --check", "tasklight check, tasklight start", "never uses :98 or /dev/shm/vqa-cu"),
    ("8. Rooms, waves, TaskLight", "context-wall MCP tools post_status, post_plan, post_step, close_task", TL + "bridge/room/mcp.py", '"name": "post_plan"', "tasklight post", ""),
    ("8. Rooms, waves, TaskLight", "TaskLight room CLI post", TL + "bridge/room/__main__.py", 'ap.add_argument("--plan-file", default=None)', "tasklight post, tasklight read", ""),
    ("8. Rooms, waves, TaskLight", "Antigravity setup of both MCP servers", TL + "tools/demo/ANTIGRAVITY_SETUP.md", '"args": ["-m", "bridge.room.mcp"],', "(registration docs, lane ops-mcp)", "the file never reads the Antigravity config"),

    ("9. newton-py (reused as a dependency, called, not copied)", "bounded read / find / sh / tok.count / route executable", HOME + "/.local/share/newton-py/venv/bin/newton", "", "out read, out find, out sh, out tokens, docs route", ""),
    ("9. newton-py (reused as a dependency, called, not copied)", "skill text", SK + "newton-py/SKILL.md", "", "out *", ""),
    ("9. newton-py (reused as a dependency, called, not copied)", "route engine commands (card, ask, pack, locate, show, check, build)", NP + "route/route.py", "ap.add_argument('command', choices=['build', 'card', 'locate', 'ask', 'pack', 'find', 'show', 'file', 'map', 'error', 'tests', 'history', 'port', 'check', 'stats'])", "docs card, docs ask, docs pack, docs locate, docs show, docs check, docs build", "lane ops-docs imports its Map"),
    ("9. newton-py (reused as a dependency, called, not copied)", "route guide", NP + "route/GUIDE.md", "# How to use this route", "docs *", ""),
]


def show(path):
    """Portable spelling of a path for the generated page."""
    if path.startswith(WS + "/"):
        return "$VP_OPS_ROOT" + path[len(WS):]
    if path.startswith(HOME + "/"):
        return "~" + path[len(HOME):]
    return path


def locate(path, anchor):
    """Return (line, status). line 0 = file-level citation (no anchor)."""
    if not os.path.exists(path):
        return None, "AUSENTE"
    if not anchor:
        return 0, "OK"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if anchor in line:
                    return i, "OK"
    except OSError:
        return None, "AUSENTE"
    return None, "ANCHOR_LOST"


def render():
    o = ["# ViewPoint capability inventory\n",
         "Generated by `design/caps.py` (Track P, claude-fable-5-2, lane ops-design). Every line number below was found on disk by",
         "searching the anchor text; `python3 -I caps.py --check` re-verifies all of them (rc 1 on drift).",
         "The `vp` column names the command of `COMMANDS.md` that wraps the entry point. Wrapping means calling it; no code is copied.\n",
         "Worktrees: the Claude reference units live one per worktree (`vp-wt-fable-f1` .. `r4`, `ct`, `au`) until merged; the Gemini",
         "Track H units live in `viewpoint-hackathon/swarm/` (branch swarm-1114) and are read-only for this track.",
         "Paths: `$VP_OPS_ROOT` is the directory that holds the checkouts (ViewPoint, viewpoint-hackathon, the worktrees); `~` is the home directory.\n"]
    bad, sec = [], None
    for section, cap, path, anchor, cmd, note in ROWS:
        if section != sec:
            sec = section
            o.append("\n## %s\n" % section)
            o.append("| Capability | Entry point (file:line) | `vp` command | Note |\n|---|---|---|---|")
        line, st = locate(path, anchor)
        if st != "OK":
            bad.append((path, anchor, st))
            cite = "`%s` **%s**" % (show(path), st)
        else:
            cite = "`%s:%d`" % (show(path), line) if line else "`%s`" % show(path)
        o.append("| %s | %s | %s | %s |" % (cap, cite, cmd if cmd.startswith(("(", "E2E")) else ", ".join("`vp %s`" % c.strip() for c in cmd.split(", ")), note))
    o.append("\n## 10. Declared gaps (AUSENTE: not invented)\n")
    o.append("| Gap | Evidence | Effect on `vp` |\n|---|---|---|")
    for g in GAPS:
        o.append("| %s | %s | %s |" % g)
    return "\n".join(o) + "\n", bad


GAPS = [
    ("Direct script execution in the reference bridge lane", "`ref_broker/bridge.py` runs only `<nonce>_<event_id>.py` files written by the broker", "`vp blender exec --bridge ref` answers NOT_FOUND; a single agent uses `vp queue enqueue --file` + `vp queue run --turns 1`"),
    ("Building a reference-unit intent from flags", "`ref_agents/contract.py` validates intents but has no writer CLI", "`vp queue enqueue` takes `--file` for the reference units; the flag form works with `--impl swarm` (Gemini intent CLI)"),
    ("mvctl connection flags from the session file", "`mvctl.py` requires --socket, --session-id, --agent-instance, --core-instance on every call", "reported by lane ops-cli as not derived yet: `engine *` needs them passed or a session file"),
    ("Hybrid bridge root and recorder display are hard-coded", "`blender_bridge.py` INBOX line, `rec_start.sh` `-i :1`", "the hybrid lane needs `--allow-hybrid` and `--allow-display :1`; the default lane is `--bridge ref` with its own RT and a private display"),
    ("`mvctl record-start` / `record-stop` success path", "choices present in `mvctl.py`, no receipt seen by this lane", "not wrapped; `vp record start` uses the ffmpeg recorders"),
    ("`vision adopt` / `attach`", "verbs exist in `vis`", "not wrapped: they act on displays this session did not start"),
    ("Gemini `swarm/agents/runner.py --kill-all`", "flag exists", "never passed; `vp agents stop` signals recorded PIDs only"),
    ("3D-to-2D cursor projection for script intents", "`swarm/CONTRACT.md` AUSENTE registry", "`record compose` draws the per-agent anchor slots or the intent `cursor_xy`"),
    ("One merged tree of the reference units", "units are spread over 11 worktrees", "`VP_REF` resolves per unit; `vp sys doctor` reports which units are present"),
    ("Session file for `browser *`, `identity *`, `engine *`", "`VP_MCP_SESSION` must be an absolute path written by the take", "`--session` or the newest take; `NO_TAKE` when none"),
    ("TaskLight success path from a model session", "`run_tasklight.mjs --check` not run by this lane (it starts a browser)", "`tasklight check` is the gate before `tasklight start`"),
]


def main(argv):
    text, bad = render()
    p = os.path.join(HERE, "CAPABILITIES.md")
    drift = bool(bad)
    if "--check" in argv:
        try:
            drift = drift or open(p, encoding="utf-8").read() != text
        except OSError:
            drift = True
    else:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(p + ".tmp", p)
    print(json.dumps({"ok": not drift, "rows": len(ROWS), "bad": bad}))
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
