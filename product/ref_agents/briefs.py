#!/usr/bin/env python3
"""Role briefs for the four reference agents.

brief = shared prefix (byte-identical for all four agents of a run, ends at the
<!-- CACHE_BREAKPOINT --> marker) + role section + session tail.
Only {{RT}} and {{VALIDATOR}} are substituted in the prefix, and both are the same
for every agent of a run, so the prefix stays byte-identical across the team.

CLI: python3 briefs.py --agent modeler-agent [--rt RT] [--max-intents N]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import contract  # noqa: E402

BRIEF_DIR = os.path.join(HERE, "briefs")
ROLE_FILE = {
    "modeler-agent": "modeler.md",
    "props-agent": "props.md",
    "look-agent": "look.md",
    "camera-agent": "camera.md",
}
BREAKPOINT = "<!-- CACHE_BREAKPOINT -->\n"


def _read(name):
    with open(os.path.join(BRIEF_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def shared_prefix(rt):
    """The prefix every agent of a run receives, byte for byte."""
    txt = _read("_shared_prefix.md")
    txt = txt.replace("{{RT}}", rt).replace("{{VALIDATOR}}", os.path.join(HERE, "contract.py"))
    if not txt.endswith(BREAKPOINT):
        raise ValueError("shared prefix must end with the cache breakpoint marker")
    if "{{" in txt:
        raise ValueError("unsubstituted placeholder in shared prefix")
    return txt


def role_suffix(agent_id, rt, max_intents=6):
    if agent_id not in ROLE_FILE:
        raise ValueError("unknown agent_id %r" % (agent_id,))
    if not 1 <= int(max_intents) <= contract.FIFO_DEPTH:
        raise ValueError("max_intents must be 1..%d" % contract.FIFO_DEPTH)
    tail = _read("_task_tail.md")
    tail = (tail.replace("{{AGENT_ID}}", agent_id).replace("{{RT}}", rt)
            .replace("{{MAX_INTENTS}}", str(int(max_intents))))
    return _read(ROLE_FILE[agent_id]) + tail


def build_brief(agent_id, rt, max_intents=6):
    return shared_prefix(rt) + role_suffix(agent_id, rt, max_intents)


def main(argv=None):
    ap = argparse.ArgumentParser(description="print one agent's brief")
    ap.add_argument("--agent", required=True, choices=contract.AGENT_IDS)
    ap.add_argument("--rt", default=None)
    ap.add_argument("--max-intents", type=int, default=6)
    a = ap.parse_args(argv)
    sys.stdout.write(build_brief(a.agent, contract.rt_root(a.rt), a.max_intents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
