"""G4 PID-only stops: a process is stopped through a PID it recorded, never by name or pattern.

Desired: 0 name/pattern based stops (another session records video on this host; `pkill -f blender`
         would kill its Blender too).
Observed: recorded commands in proof logs, *.sh command lines, and argv-like string constants of *.py.
"""
import os
import re

from common import analyze, existing_logs, finding, iter_commands, py_string_constants, result, source_files, WAIVER

_NAME_KILLERS = {"pkill", "killall", "killall5", "skill", "slay"}  # gate-ok: rule table
_PID_OK = re.compile(r"^(?:-?\d+|%\d+|-?\$\{?[A-Za-z_!][A-Za-z0-9_]*\}?)$")
_PATTERN_SUB = re.compile(r"\b(?:pgrep|pidof|ps\s|docker\s+ps|lsof|fuser)\b")
_PIDFILE_SUB = re.compile(r"^\$\(\s*(?:cat|<)\s*[^|;&]+\)$|^`\s*cat\s+[^|;&`]+`$")
_PY_KILL = re.compile(r"^(?:pkill|killall|killall5)$|\b(?:pkill|killall)\s+-?\w")


def check_command(cmd):
    out = []
    cmds = analyze(cmd)
    for c in cmds:
        head, argv, raw = c["head"], c["argv"], c["raw"][:110]
        if head in _NAME_KILLERS:
            out.append(("NAME_BASED_STOP", "`%s`" % raw))
        elif head == "fuser" and any(a.startswith("-") and "k" in a for a in argv[1:]):
            out.append(("NAME_BASED_STOP", "`%s`" % raw))
        elif head == "kill":
            targets = [a for a in argv[1:] if not re.match(r"^-(?:[A-Za-z]+\d*|\d{1,2}|-)$", a) or a == "-1"]
            if "-l" in argv[1:] or "-L" in argv[1:]:
                continue
            if not targets:
                out.append(("PID_FROM_PIPE", "kill without an explicit PID (xargs/pipe?): `%s`" % raw))
            for t in targets:
                if t in ("-1", "0"):
                    out.append(("KILL_ALL", "`%s` signals every process / the whole group" % raw))
                elif "$(" in t or "`" in t:
                    # only `$(cat <pidfile>)` is a recorded PID; pgrep/pidof/ps resolve by name at kill time
                    if _PATTERN_SUB.search(t) or not _PIDFILE_SUB.match(t.strip('"')):
                        out.append(("PATTERN_PID", "PID not read from a pid file: `%s`" % raw))
                elif not _PID_OK.match(t):
                    out.append(("NOT_A_PID", "kill target %r is not a PID: `%s`" % (t[:30], raw)))
        elif head == "docker" and len(argv) > 2 and argv[1] in ("kill", "stop", "rm") \
                and any("$(" in a and "docker ps" in a for a in argv[2:]):
            out.append(("PATTERN_PID", "docker %s over a `docker ps` listing: `%s`" % (argv[1], raw)))
        elif head == "docker" and len(argv) > 1 and argv[1] in ("kill", "stop", "rm") \
                and "xargs" in c.get("via", ()):
            out.append(("PATTERN_PID", "docker %s fed by xargs (targets resolved from a listing): `%s`" % (argv[1], raw)))
        elif head == "systemctl":
            verbs = [k for k, a in enumerate(argv) if k and a in ("kill", "stop")]
            if verbs:
                units = [a for a in argv[verbs[0] + 1:] if not a.startswith("-")]
                # every unit must be a capsule unit: one vpref- argument does not exempt the rest (xr C8)
                if not units or any(not u.startswith("vpref-") for u in units):
                    out.append(("NAME_BASED_STOP", "systemctl %s of a unit outside vpref-*: `%s`"
                                % (argv[verbs[0]], raw)))
    return out


def scan_logs(paths):
    findings, n = [], 0
    for p in paths:
        for ln, cmd in iter_commands(p):
            n += 1
            for rule, detail in check_command(cmd):
                findings.append(finding(rule, "%s:%d" % (p, ln), detail))
    return findings, n


def scan_sources(roots):
    findings, n = [], 0
    for path in source_files(roots):
        n += 1
        if path.endswith(".py"):
            for ln, val in py_string_constants(path):
                if _PY_KILL.search(val.strip()) and len(val) < 400:
                    findings.append(finding("NAME_BASED_STOP", "%s:%d" % (path, ln),
                                            "string constant builds a name-based kill"))
        else:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read().split("\n")
            except OSError:
                continue
            for ln, line in enumerate(lines, 1):
                if line.lstrip().startswith("#") or WAIVER in line:
                    continue
                for rule, detail in check_command(line):
                    findings.append(finding(rule, "%s:%d" % (path, ln), detail))
    return findings, n


def run(logs=(), sources=()):
    files, missing = existing_logs(logs)
    findings, n = scan_logs(files)
    findings += missing
    f, k = scan_sources(list(sources))
    return result("G4_pid_only_stops", {"name_or_pattern_based_stops": 0},
                  {"logs": len(logs), "source_roots": len(sources)}, findings + f, n + k)
