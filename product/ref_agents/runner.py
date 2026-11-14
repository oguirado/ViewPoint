#!/usr/bin/env python3
"""ViewPoint reference capsule, unit F4: the agent runner.

Starts one process per role (real headless agy agents, or scripted stubs with
--stub), saves each stream to an evidence directory, records PIDs, waits, stops
whatever is still alive BY PID, then validates every intent file the agents wrote.

    runner.py run  --rt RT --evidence DIR [--agents a,b] [--stub] [--max-seconds S]
                   [--max-intents N] [--model M] [--init-rt]
    runner.py stop --evidence DIR        # stop a run from its pids.json, by PID
    runner.py status --evidence DIR

Exit code of `run`: 0 = every started agent wrote >= 1 intent and all intents are
valid; 1 = otherwise; 2 = usage / environment problem.
Stdlib only. The runner never calls vision, never writes RT/inbox, RT/outbox,
RT/frames or RT/events.jsonl, and never passes VP_SWARM_NONCE to an agent.
"""
import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import briefs  # noqa: E402
import contract  # noqa: E402

# Antigravity CLI: $VP_AGY, else `agy` on PATH.
AGY = os.environ.get("VP_AGY") or shutil.which("agy") or "agy"
DEFAULT_MODEL = "gemini-4-hydrogen-high"
RT_ALLOWED_PREFIX = "/dev/shm/vqa-cu/ref"
# Never handed to an agent: the broker's per-run bridge nonce.
ENV_STRIP = ("VP_SWARM_NONCE",)


def check_rt(rt):
    """RT must resolve under /dev/shm/vqa-cu/ref* (never hybrid/, swarm/ or elsewhere)."""
    real = os.path.realpath(rt)
    if not real.startswith(RT_ALLOWED_PREFIX):
        raise ValueError("RT %r resolves outside %s*" % (rt, RT_ALLOWED_PREFIX))
    return real


def init_rt(rt):
    """Create the agent-side RT layout. Does not create frames or events."""
    for d in ("intents", "frames"):
        os.makedirs(os.path.join(rt, d), exist_ok=True)
    for a in contract.AGENT_IDS:
        os.makedirs(os.path.join(rt, "intents", a), exist_ok=True)
    cp = os.path.join(rt, "claims.json")
    if not os.path.exists(cp):
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(contract.DEFAULT_CLAIMS, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, cp)


def child_env(env=None):
    e = dict(os.environ if env is None else env)
    for k in ENV_STRIP:
        e.pop(k, None)
    return e


def agent_cmd(agent_id, rt, evdir, brief, stub=False, model=DEFAULT_MODEL,
              print_timeout="1800s", stub_count=None):
    """argv for one agent. Real mode: the agy command with -p <brief> LAST."""
    if stub:
        cmd = [sys.executable, os.path.join(HERE, "stub_agent.py"), "--agent", agent_id, "--rt", rt]
        if stub_count is not None:
            cmd += ["--count", str(stub_count)]
        return cmd
    return [AGY, "--model", model, "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--print-timeout", print_timeout,
            "--add-dir", rt, "--log-file", os.path.join(evdir, "agy_cli.log"),
            "-p", brief]


def proc_starttime(pid):
    """Kernel start time (clock ticks) of pid, or None: guards against PID reuse."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            s = fh.read()
        return int(s[s.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def alive(pid, starttime):
    st = proc_starttime(pid)
    if st is None or st != starttime:
        return False
    try:
        with open("/proc/%d/stat" % pid) as fh:
            s = fh.read()
        return s[s.rindex(")") + 2:].split()[0] != "Z"
    except OSError:
        return False


def group_pids(pgid):
    """PIDs whose process group is pgid (the agent and what it spawned)."""
    out = []
    for n in os.listdir("/proc"):
        if not n.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % n) as fh:
                s = fh.read()
            f = s[s.rindex(")") + 2:].split()
            if int(f[2]) == pgid and f[0] != "Z":
                out.append(int(n))
        except (OSError, ValueError, IndexError):
            continue
    return out


def stop_pid(pid, starttime, grace=5.0):
    """Stop one recorded agent by PID (and its own process group, which it leads).

    Returns "gone" (was not running / not ours any more), "term" or "kill".
    Never matches by name; a PID whose start time differs is left alone.
    """
    st = proc_starttime(pid)
    leader_ok = st is not None and st == starttime
    if not leader_ok and st is not None:
        return "gone"               # PID reused by something else: do not touch
    if not leader_ok and not group_pids(pid):
        return "gone"
    how = "term"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)     # pid == pgid: started with start_new_session
        except ProcessLookupError:
            return how
        except PermissionError:
            return "gone"
        t0 = time.time()
        while time.time() - t0 < grace:
            if not group_pids(pid):
                return how
            time.sleep(0.1)
        how = "kill"
    return how


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _file_sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(rt, evidence, agents, stub=False, model=DEFAULT_MODEL, max_seconds=1800.0,
        max_intents=6, do_init=False, print_timeout="1800s", stub_count=None, log=print):
    rt = check_rt(rt)
    if do_init:
        init_rt(rt)
    if not os.path.isdir(os.path.join(rt, "intents")):
        raise ValueError("RT %s has no intents/ directory (use --init-rt)" % rt)
    os.makedirs(evidence, exist_ok=True)
    pids_path = os.path.join(evidence, "pids.json")
    state = {"v": 1, "rt": rt, "mode": "stub" if stub else "agy", "model": None if stub else model,
             "runner_pid": os.getpid(), "started_at": _now(), "agents": {}}
    # claims.json lives in the agents' writable --add-dir RT (xr-io A4): pin its bytes now
    # and fail the run if they differ at the end.
    state["claims_sha256"] = _file_sha256(os.path.join(rt, "claims.json"))
    procs = {}
    files = []

    def stop_all():
        for aid, p in procs.items():
            info = state["agents"][aid]
            if p.poll() is None or group_pids(p.pid):
                info["stopped_by_runner"] = stop_pid(p.pid, info["starttime"])
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    sig = {"stopping": False, "seen": []}

    def on_signal(signum, _frame):
        # Only the FIRST signal interrupts the wait loop.  Every later one (a second
        # SIGTERM inside the stop grace, a Ctrl-C after a timeout) is only recorded:
        # raising inside stop_all would skip the SIGKILL escalation and the summary and
        # orphan the agents (xr-io A5).
        sig["seen"].append(signum)
        if sig["stopping"]:
            return
        sig["stopping"] = True
        raise KeyboardInterrupt("signal %d" % signum)

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    interrupted = False
    try:
        for aid in agents:
            ev = os.path.join(evidence, aid)
            os.makedirs(ev, exist_ok=True)
            os.makedirs(os.path.join(rt, "intents", aid), exist_ok=True)
            brief = briefs.build_brief(aid, rt, max_intents)
            with open(os.path.join(ev, "prompt.md"), "w", encoding="utf-8") as fh:
                fh.write(brief)
            cmd = agent_cmd(aid, rt, ev, brief, stub, model, print_timeout, stub_count)
            out = open(os.path.join(ev, "agy_stream.jsonl"), "wb")
            err = open(os.path.join(ev, "agy_stderr.log"), "wb")
            files += [out, err]
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                 cwd=rt, env=child_env(), start_new_session=True)
            procs[aid] = p
            shown = list(cmd)
            if not stub:
                shown[-1] = "<brief: prompt.md sha256 %s>" % hashlib.sha256(
                    brief.encode("utf-8")).hexdigest()
            state["agents"][aid] = {
                "pid": p.pid, "starttime": proc_starttime(p.pid), "cmd": shown,
                "started_at": _now(), "stream": os.path.join(ev, "agy_stream.jsonl"),
                "prefix_sha256": hashlib.sha256(
                    briefs.shared_prefix(rt).encode("utf-8")).hexdigest(),
            }
            _write_json(pids_path, state)        # PIDs on disk before anything else
            log("started %s pid %d" % (aid, p.pid))
        deadline = time.time() + max_seconds
        while time.time() < deadline and any(p.poll() is None for p in procs.values()):
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        sig["stopping"] = True          # from here on signals are recorded, never raised
        timed_out = [a for a, p in procs.items() if p.poll() is None]
        for _try in range(3):           # a signal that raced the flag must not skip the stop
            try:
                stop_all()
                break
            except KeyboardInterrupt:
                interrupted = True
        for f in files:
            try:
                f.close()
            except OSError:
                pass
    try:
        return _finish(rt, evidence, state, procs, timed_out, interrupted, pids_path, log,
                       sig["seen"])
    finally:
        for s, h in old.items():        # handlers stay armed (record-only) until the summary is on disk
            signal.signal(s, h)


def _finish(rt, evidence, state, procs, timed_out, interrupted, pids_path, log, signals):
    res = contract.validate_rt(rt)
    ok = not interrupted
    claims_now = _file_sha256(os.path.join(rt, "claims.json"))
    claims_changed = claims_now != state.get("claims_sha256")
    if claims_changed:
        ok = False
        log("claims.json changed during the run (pinned %s, now %s): run refused"
            % (state.get("claims_sha256"), claims_now))
    for aid, p in procs.items():
        info = state["agents"][aid]
        mine = {q: e for q, e in res.items() if os.path.basename(os.path.dirname(q)) == aid}
        bad = {q: e for q, e in mine.items() if e}
        info.update(rc=p.returncode, timed_out=aid in timed_out, finished_at=_now(),
                    intents=len(mine), invalid=len(bad), errors=bad)
        with open(os.path.join(evidence, aid, "finished_rc.txt"), "w") as fh:
            fh.write("rc=%s\ntimed_out=%s\n%s\n" % (p.returncode, aid in timed_out, _now()))
        if not mine or bad:
            ok = False
        log("%s rc=%s timed_out=%s intents=%d invalid=%d"
            % (aid, p.returncode, aid in timed_out, len(mine), len(bad)))
    state.update(finished_at=_now(), interrupted=interrupted, ok=ok, signals=list(signals),
                 claims_changed=claims_changed)
    _write_json(pids_path, state)
    _write_json(os.path.join(evidence, "summary.json"),
                {"ok": ok, "rt": rt, "mode": state["mode"], "interrupted": interrupted,
                 "signals": list(signals), "claims_changed": claims_changed,
                 "agents": {a: {k: i[k] for k in ("pid", "rc", "timed_out", "intents", "invalid", "errors")}
                            for a, i in state["agents"].items()}})
    return 0 if ok else 1


def stop_from_evidence(evidence, log=print):
    with open(os.path.join(evidence, "pids.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    for aid, info in sorted(state.get("agents", {}).items()):
        log("%s pid %d: %s" % (aid, info["pid"], stop_pid(info["pid"], info["starttime"])))
    return 0


def status_from_evidence(evidence, log=print):
    with open(os.path.join(evidence, "pids.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    for aid, info in sorted(state.get("agents", {}).items()):
        log("%s pid %d %s" % (aid, info["pid"],
                              "alive" if alive(info["pid"], info["starttime"]) else "not running"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="ViewPoint reference agent runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--rt", default=None)
    r.add_argument("--evidence", required=True)
    r.add_argument("--agents", default=",".join(contract.AGENT_IDS))
    r.add_argument("--stub", action="store_true", help="scripted fake agents, no model")
    r.add_argument("--stub-count", type=int, default=None)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--max-seconds", type=float, default=1800.0)
    r.add_argument("--print-timeout", default="1800s")
    r.add_argument("--max-intents", type=int, default=6)
    r.add_argument("--init-rt", action="store_true", help="create RT/intents, RT/frames, claims.json")
    for name in ("stop", "status"):
        s = sub.add_parser(name)
        s.add_argument("--evidence", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run":
            agents = [x for x in a.agents.split(",") if x]
            for x in agents:
                if x not in contract.AGENT_IDS:
                    raise ValueError("unknown agent %r" % x)
            if len(set(agents)) != len(agents):
                raise ValueError("duplicate agent in --agents")
            return run(contract.rt_root(a.rt), os.path.abspath(a.evidence), agents, a.stub,
                       a.model, a.max_seconds, a.max_intents, a.init_rt, a.print_timeout,
                       a.stub_count)
        if a.cmd == "stop":
            return stop_from_evidence(os.path.abspath(a.evidence))
        return status_from_evidence(os.path.abspath(a.evidence))
    except (ValueError, OSError) as e:
        print("runner: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
