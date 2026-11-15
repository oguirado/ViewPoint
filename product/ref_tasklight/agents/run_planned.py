#!/usr/bin/env python3
"""product/ref_agents runner with planning agents (lane tl-agents). ref_agents is not edited:
this wrapper swaps three module attributes in its own process, then calls runner.main.

  python3 run_planned.py run --stub --init-rt --rt RT --evidence DIR [--agents a,b]
     --stub  -> planned_agent.py (plan first, one microtask at a time) instead of stub_agent.py
     real    -> the agy brief gets briefs/microtask_protocol.md appended (prompt.md shows it)
  the final intent check accepts `tm` and REQUIRES tm.microtask_id (MICROTASK_MISSING).
Extra stub flags: env VP_TL_AGENT_ARGS, e.g. "--rewind --deps --max-seconds 40".
"""
import os
import shlex
import sys

import protocol as P

import briefs    # noqa: E402  (product/ref_agents)
import runner    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
_agent_cmd, _build_brief = runner.agent_cmd, briefs.build_brief


def protocol_section(agent_id, rt):
    with open(os.path.join(HERE, "briefs", "microtask_protocol.md"), encoding="utf-8") as fh:
        s = fh.read()
    return s.replace("{{AGENT_ID}}", agent_id).replace("{{RT}}", rt).replace("{{TOOLS}}", HERE)


def build_brief(agent_id, rt, *a, **kw):
    return _build_brief(agent_id, rt, *a, **kw) + "\n" + protocol_section(agent_id, rt)


def agent_cmd(agent_id, rt, evdir, brief, stub=False, *a, **kw):
    if not stub:
        return _agent_cmd(agent_id, rt, evdir, brief, stub, *a, **kw)
    cmd = [sys.executable, os.path.join(HERE, "planned_agent.py"), "--agent", agent_id, "--rt", rt]
    count = kw.get("stub_count", a[2] if len(a) > 2 else None)
    if count is not None:
        cmd += ["--count", str(count)]
    return cmd + shlex.split(os.environ.get("VP_TL_AGENT_ARGS", ""))


def validate_intent(obj, claims=None, **kw):
    return P.validate_planned_intent(obj, require_plan=False, claims=claims, **kw)


def install():
    runner.agent_cmd = agent_cmd
    briefs.build_brief = build_brief
    P.contract.validate_intent = validate_intent


if __name__ == "__main__":
    install()
    sys.exit(runner.main())
