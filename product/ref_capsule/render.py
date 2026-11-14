#!/usr/bin/env python3
"""Render a validated vp-capsule/v1 spec into a launch PLAN (argv lists) without running it.

Track P reference code (claude-fable-5-2). This module never executes anything: it
imports no process module, and tests/test_capsule.py fails if one is ever added.
A spec with validator findings is refused (exit 1): there is no --force.

Usage:  python3 -I render.py SPEC.json      prints the plan as JSON
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works under python3 -I
import validate as V  # noqa: E402


def start_order(spec):
    """program -> broker -> agents (stable); the stop order is the reverse."""
    rank = {"program": 0, "broker": 1, "agent": 2}
    return sorted(spec["containers"], key=lambda c: rank[c["role"]])


def _vol_name(spec, vn):
    return "%s-%s" % (spec["name"], vn)


def _mount_arg(spec, m):
    v = spec["volumes"][m["volume"]]
    sp = m.get("subpath", "")
    if v["backing"]["type"] == "bind":
        src = v["backing"]["src"] + ("/" + sp if sp else "")
        arg = "type=bind,src=%s,dst=%s" % (src, m["dst"])
    else:
        arg = "type=volume,src=%s,dst=%s" % (_vol_name(spec, m["volume"]), m["dst"])
        if sp:
            arg += ",volume-subpath=%s" % sp
    # Structural backstop for xr C1/C2: the validator allow-lists every field, and the
    # renderer independently refuses to emit an option string with extra fields.
    for part in (m["dst"], sp, m["volume"], v["backing"].get("src", "")):
        if any(ch in part for ch in ",=:\"' \t\n"):
            raise ValueError("mount field %r would inject docker --mount options" % part)
    return arg + (",readonly" if m["mode"] == "ro" else "")


def run_argv(spec, c):
    uid, gid = c["user"].split(":")
    a = ["docker", "run", "-d", "--name", c["name"], "--label", "vp.capsule=" + spec["name"],
         "--label", "vp.role=" + c["role"], "--user", c["user"], "--read-only",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "--network", c["network"], "--ipc", "private",
         "--memory", "%dm" % c["memory_mib"], "--memory-swap", "%dm" % c["memory_swap_mib"],
         "--pids-limit", str(c["pids_limit"]), "--shm-size", "%dm" % c["shm_mib"]]
    # No --pid flag: the engine default is already a private PID namespace.
    for t in c.get("tmpfs", []):
        a += ["--tmpfs", "%s:rw,nosuid,nodev,mode=0700,uid=%s,gid=%s,size=%dm" % (t["dst"], uid, gid, t["size_mib"])]
    for m in c.get("mounts", []):
        a += ["--mount", _mount_arg(spec, m)]
    for k in sorted(c.get("env", {})):
        a += ["--env", "%s=%s" % (k, c["env"][k])]
    h = c["health"]
    a += ["--health-cmd", " ".join(h["cmd"]), "--health-interval", "%ds" % h["interval_s"],
          "--health-timeout", "%ds" % h["timeout_s"], "--health-retries", str(h["retries"]),
          "--health-start-period", "%ds" % h["start_period_s"],
          "--stop-signal", c["stop"]["signal"], "--stop-timeout", str(c["stop"]["grace_s"])]
    r = c["restart"]
    a += ["--restart", "no" if r["policy"] == "no" else "on-failure:%d" % r["max"]]
    return a + [c["image"]]


def plan(spec):
    findings = V.validate(spec)
    if findings:
        raise ValueError("spec has %d validator finding(s); nothing rendered" % len(findings))
    order = start_order(spec)
    vols = []
    for vn, v in sorted(spec["volumes"].items()):
        if v["backing"]["type"] == "volume":
            # tmpfs-backed named volume: bounded, gone on `docker volume rm`, survives a
            # program container restart (which a --tmpfs mount would not).
            vols.append(["docker", "volume", "create", "--driver", "local", "--opt", "type=tmpfs",
                         "--opt", "device=tmpfs", "--opt", "o=size=%dm,nosuid,nodev,uid=10001,gid=10001" % v["size_mib"],
                         "--label", "vp.capsule=" + spec["name"], _vol_name(spec, vn)])
    return {
        "schema": "vp-capsule-plan/v1", "capsule": spec["name"], "executed": False,
        "volumes": vols,
        "start": [{"name": c["name"], "wait_healthy": True, "argv": run_argv(spec, c)} for c in order],
        "stop": [{"name": c["name"], "argv": ["docker", "stop", "--time", str(c["stop"]["grace_s"]), c["name"]]}
                 for c in reversed(order)],
        "inspect": ["docker", "inspect"] + [c["name"] for c in order],
    }


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as f:
        spec = json.load(f)
    try:
        print(json.dumps(plan(spec), indent=1))
    except ValueError as e:
        sys.stderr.write("REFUSED: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
