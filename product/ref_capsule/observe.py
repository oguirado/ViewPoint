#!/usr/bin/env python3
"""Desired-versus-observed check: compare a vp-capsule/v1 spec with `docker inspect` JSON.

Track P reference code (claude-fable-5-2). Reads two files; it does not call docker.
A spec that validates only says what was ASKED. This says what the engine reports it
APPLIED (HostConfig, Mounts, State). The operator produces the inspect file with the
`inspect` argv of the render plan.

Usage:  python3 -I observe.py SPEC.json INSPECT.json
Exit:   0 = observed state matches, 1 = drift, 2 = unreadable input.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works under python3 -I
import validate as V  # noqa: E402

MIB = 1024 * 1024


def observe(spec, inspected):
    out = []

    def bad(rule, where, msg):
        out.append({"rule": rule, "where": where, "msg": msg})

    by_name = {}
    for d in inspected if isinstance(inspected, list) else []:
        if isinstance(d, dict):
            by_name[str(d.get("Name", "")).lstrip("/")] = d
    for c in spec["containers"]:
        n = c["name"]
        d = by_name.get(n)
        if d is None:
            bad("O-00", n, "container not present in the inspect data")
            continue
        hc, cfg, st = d.get("HostConfig") or {}, d.get("Config") or {}, d.get("State") or {}
        # O-01 limits as applied by the engine
        for key, want in (("Memory", c["memory_mib"] * MIB), ("MemorySwap", c["memory_swap_mib"] * MIB),
                          ("PidsLimit", c["pids_limit"]), ("ShmSize", c["shm_mib"] * MIB)):
            if hc.get(key) != want:
                bad("O-01", n, "HostConfig.%s is %r, spec wants %r" % (key, hc.get(key), want))
        # O-02 hardening as applied
        if hc.get("ReadonlyRootfs") is not True:
            bad("O-02", n, "root filesystem is not read-only")
        if hc.get("Privileged") is not False:
            bad("O-02", n, "container is privileged")
        if "ALL" not in (hc.get("CapDrop") or []) or (hc.get("CapAdd") or []):
            bad("O-02", n, "capabilities differ from cap-drop ALL / no cap-add")
        if not any("no-new-privileges" in s for s in (hc.get("SecurityOpt") or [])):
            bad("O-02", n, "no-new-privileges not applied")
        if cfg.get("User") != c["user"]:
            bad("O-02", n, "user is %r, spec wants %r" % (cfg.get("User"), c["user"]))
        if hc.get("NetworkMode") != c["network"]:
            bad("O-02", n, "network mode is %r, spec wants %r" % (hc.get("NetworkMode"), c["network"]))
        for key in ("PidMode", "IpcMode"):
            if hc.get(key, "") not in ("", "private"):
                bad("O-02", n, "%s is %r" % (key, hc.get(key)))
        if hc.get("VolumesFrom") or hc.get("Devices"):
            bad("O-02", n, "VolumesFrom/Devices present")
        # O-03 mounts: exactly the declared destinations, same writability
        want = {m["dst"]: m["mode"] == "rw" for m in c.get("mounts", [])}
        seen = {}
        for m in d.get("Mounts") or []:
            seen[m.get("Destination")] = m
        for dst in sorted(set(seen) - set(want)):
            bad("O-03", n, "undeclared mount at %s (source %r)" % (dst, seen[dst].get("Source") or seen[dst].get("Name")))
        for dst, rw in sorted(want.items()):
            if dst not in seen:
                bad("O-03", n, "declared mount %s is missing" % dst)
            elif bool(seen[dst].get("RW")) != rw:
                bad("O-03", n, "mount %s RW=%r, spec wants %r" % (dst, seen[dst].get("RW"), rw))
        if c["role"] == "agent":
            for m in d.get("Mounts") or []:
                blob = "%s %s %s" % (m.get("Destination"), m.get("Source"), m.get("Name"))
                if ".X11-unix" in blob or "xsock" in blob:
                    bad("O-04", n, "agent sidecar can reach the X socket through %s" % m.get("Destination"))
            for e in cfg.get("Env") or []:
                if e.split("=", 1)[0] in V.DISPLAY_ENV:
                    bad("O-04", n, "agent sidecar env carries %s" % e.split("=", 1)[0])
        # O-05 health and restarts
        health = (st.get("Health") or {}).get("Status")
        if health != "healthy":
            bad("O-05", n, "health status is %r, not 'healthy'" % health)
        if st.get("OOMKilled"):
            bad("O-06", n, "container was OOM-killed")
        limit = c["restart"].get("max", 0) if c["restart"]["policy"] == "on-failure" else 0
        if (d.get("RestartCount") or 0) > limit:
            bad("O-06", n, "RestartCount %r above the declared maximum %d" % (d.get("RestartCount"), limit))
    declared = {c["name"] for c in spec["containers"]}
    for n in sorted(set(by_name) - declared):
        labels = ((by_name[n].get("Config") or {}).get("Labels") or {})
        if labels.get("vp.capsule") == spec["name"]:
            bad("O-07", n, "container carries this capsule's label but is not in the spec")
    return out


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as f:
            spec = json.load(f)
        with open(argv[2], encoding="utf-8") as f:
            inspected = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("unreadable input: %s\n" % e)
        return 2
    pre = V.validate(spec)
    if pre:
        sys.stderr.write("spec does not validate (%d finding(s)); run validate.py first\n" % len(pre))
        return 2
    out = observe(spec, inspected)
    for f in out:
        print("DRIFT %-5s %s: %s" % (f["rule"], f["where"], f["msg"]))
    print("%s  %d drift finding(s)" % ("RED" if out else "GREEN", len(out)))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
