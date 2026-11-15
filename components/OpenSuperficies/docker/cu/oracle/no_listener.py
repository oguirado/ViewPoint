#!/usr/bin/env python3
"""No-listener assert for the oracle capsule (W6a lane P, AW6-06). Prints JSON, rc 0 iff clean, 5 otherwise (2 usage).
Fails on: any TCP/TCP6 state 0A row; any unix listener (flags 00010000, __SO_ACCEPTCON) that is abstract ('@'), unnamed, or
named *devtools*/*debug*, or - unless --allow-capsule-baseline - any at all; with the flag the declared baseline
(/run/newton-tenant/bus, /tmp/chromium-*/SingletonSocket, and since OD-2 the operator-approved Chromium ProcessSingleton socket
/tmp/org.chromium.Chromium.XXXXXX/SingletonSocket plus the Xvfb abstract @/tmp/.X11-unix/X100) is reported but tolerated, and every
tolerated path socket must be owned by --baseline-uid (10001) inside a dir owned by that uid with mode 0700, else rc 5 (owner/mode
drift); any DevToolsActivePort under <tmp-root>/chromium-*.
Unreadable /proc/net/{tcp,unix} fails closed (tcp6 may be absent when IPv6 is off)."""
import argparse, json, os, re, stat, sys

# OD-2 (operator, 2026-10-18, option A): exactly two anchored patterns added after stat evidence (os-cu-0 SYNC_VQA); never widen here.
BASELINE = (re.compile(r"^/run/newton-tenant/bus$"), re.compile(r"^/tmp/chromium-[^/]+/SingletonSocket$"),
            re.compile(r"^/tmp/org\.chromium\.Chromium\.[A-Za-z0-9]{6}/SingletonSocket$"))
ABSTRACT_BASELINE = (re.compile(r"^@/tmp/\.X11-unix/X100$"),)
BASELINE_UID = 10001
ACCEPTCON = 0x00010000


def _lines(path, required):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[1:]
    except OSError:
        if required:
            raise
        return None


def owner_ok(fs_root, path, uid):
    """OD-2 permanent assertion: socket and its dir owned by uid, dir mode 0700. Any lstat error is a failure (fail closed)."""
    try:
        a = os.lstat(os.path.join(fs_root, path.lstrip("/")))
        d = os.lstat(os.path.join(fs_root, os.path.dirname(path).lstrip("/")))
    except OSError:
        return False
    return a.st_uid == uid and d.st_uid == uid and stat.S_IMODE(d.st_mode) == 0o700


def scan(proc_root, tmp_root, allow_baseline, fs_root="/", uid=BASELINE_UID):
    r = {"tcp_listen": [], "unix_listen": [], "baseline_unix": [], "baseline_drift": [], "devtools_port_files": [],
         "violations": [], "tcp6": "ok"}
    net = os.path.join(proc_root, "net")
    for name in ("tcp", "tcp6"):
        rows = _lines(os.path.join(net, name), name == "tcp")
        if rows is None:
            r["tcp6"] = "absent"
            continue
        for l in rows:
            c = l.split()
            if len(c) > 3 and c[3] == "0A":
                r["tcp_listen"].append("%s %s" % (name, c[1]))
    for l in _lines(os.path.join(net, "unix"), True):
        c = l.split(None, 7)
        if len(c) < 6:
            continue
        try:
            fl = int(c[3], 16)
        except ValueError:
            continue
        if not fl & ACCEPTCON:
            continue
        path = c[7] if len(c) > 7 else ""
        low = path.lower()
        if "devtools" in low or "debug" in low or not path:
            r["unix_listen"].append(path or "<unnamed>")
        elif path.startswith("@"):
            if allow_baseline and any(p.match(path) for p in ABSTRACT_BASELINE):
                r["baseline_unix"].append(path)
            else:
                r["unix_listen"].append(path)
        elif allow_baseline and any(p.match(path) for p in BASELINE):
            r["baseline_unix"].append(path)
            if not owner_ok(fs_root, path, uid):
                r["baseline_drift"].append(path)
        else:
            r["unix_listen"].append(path)
    try:
        for d in sorted(os.listdir(tmp_root)):
            if d.startswith("chromium-"):
                p = os.path.join(tmp_root, d, "DevToolsActivePort")
                if os.path.lexists(p):
                    r["devtools_port_files"].append(p)
    except OSError:
        pass
    if r["tcp_listen"]:
        r["violations"].append("tcp listener")
    if r["unix_listen"]:
        r["violations"].append("unix listener")
    if r["baseline_drift"]:
        r["violations"].append("baseline owner/mode drift")
    if r["devtools_port_files"]:
        r["violations"].append("DevToolsActivePort file")
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(prog="no_listener.py")
    ap.add_argument("--proc-root", default="/proc")
    ap.add_argument("--tmp-root", default="/tmp")
    ap.add_argument("--allow-capsule-baseline", action="store_true")
    ap.add_argument("--fs-root", default="/")
    ap.add_argument("--baseline-uid", type=int, default=BASELINE_UID)
    a = ap.parse_args(argv)
    try:
        r = scan(a.proc_root, a.tmp_root, a.allow_capsule_baseline, a.fs_root, a.baseline_uid)
    except OSError as e:
        print(json.dumps({"error": "E_PROC %s" % type(e).__name__, "violations": ["proc unreadable"]}))
        return 5
    print(json.dumps(r, sort_keys=True))
    return 5 if r["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
