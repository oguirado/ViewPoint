"""Shared helpers for the ViewPoint capsule gate suite (Track P, Claude-written: claude-fable-5-2).

A gate compares a DESIRED state (a rule) with an OBSERVED state (a log, a source tree, a cgroup, an
event ledger) and answers with one of three statuses:

    GREEN       every observation satisfies the rule
    RED         at least one observation violates it (finding with file:line)
    UNOBSERVED  nothing (or not enough) was observed: this is NOT a pass (KU-0001 C1, CT-0022 C1)

Stdlib only. No network. Never prints a matched secret.
"""
import json
import os
import re
import shlex

GREEN, RED, UNOBSERVED = "GREEN", "RED", "UNOBSERVED"
RC = {GREEN: 0, RED: 1, UNOBSERVED: 2}


def finding(rule, where, detail, severity="red"):
    return {"rule": rule, "where": where, "detail": detail, "severity": severity}


def result(gate, desired, observed, findings, observed_count):
    """observed_count = number of things actually inspected; 0 means UNOBSERVED, never GREEN."""
    if any(f["severity"] == "red" for f in findings):
        status = RED
    elif observed_count <= 0 or any(f["severity"] == "unobserved" for f in findings):
        status = UNOBSERVED
    else:
        status = GREEN
    return {"gate": gate, "status": status, "desired": desired, "observed": observed,
            "observed_count": observed_count, "findings": findings}


# ---------------------------------------------------------------- stream logs (proof/*.jsonl)
def _walk_tool_calls(node, out):
    if isinstance(node, dict):
        name = node.get("name")
        args = None
        for k in ("input", "args", "arguments", "parameters"):
            if isinstance(node.get(k), dict):
                args = node[k]
                break
        if isinstance(name, str) and args is not None:
            out.append((name, args))
        for v in node.values():
            _walk_tool_calls(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_tool_calls(v, out)


def iter_tool_calls(path):
    """Yield (lineno, tool_name, args) for every tool call recorded in a stream-json log.

    Works on `claude -p --output-format stream-json` logs and on any JSONL whose records carry
    {"name": ..., "input"|"args"|"arguments"|"parameters": {...}} at any depth (agy streams).
    Only records emitted by the model side are tool calls; tool RESULTS are text and are skipped,
    so a file that merely quotes a rule ("never pkill -f") is not an observation of a command.
    """
    seen_steps = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (rec.get("type") or rec.get("event")) in ("user", "system", "result", "init"):
                continue
            step = rec.get("step_update")
            if isinstance(step, dict):  # agy: the same tool step is logged as ACTIVE and again as DONE
                key = (step.get("conversation_id"), step.get("step_index"))
                if key in seen_steps:
                    continue
                if isinstance(step.get("tool_info"), dict):
                    seen_steps.add(key)
            calls = []
            _walk_tool_calls(rec, calls)
            for name, args in calls:
                yield n, name, args


def iter_commands(path):
    """Yield (lineno, command_string) for shell commands found in the tool calls of a stream log."""
    for n, name, args in iter_tool_calls(path):
        for k in ("command", "cmd", "CommandLine", "script"):
            v = args.get(k)
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                # array-form argv (Codex logs: ["bash", "-lc", "..."]) is a command too (xr C8)
                v = shlex.join(v)
            if isinstance(v, str) and v.strip():
                yield n, v
                break


def existing_logs(logs):
    """Split declared proof logs into (files, findings). A declared log that does not exist
    (a glob that matched nothing) is missing evidence: UNOBSERVED, never silently dropped (xr C7)."""
    files, findings = [], []
    for p in logs:
        if os.path.isfile(p):
            files.append(p)
        else:
            findings.append(finding("LOG_MISSING", p, "declared proof log does not exist", "unobserved"))
    return files, findings


def stream_is_closed(path):
    """True when the stream log has a `result` record with no assistant/user activity after it.

    Trailing `system` bookkeeping records (task summaries of background shells) do not reopen it.
    """
    closed = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    t = rec.get("type") or rec.get("event")
                except (ValueError, AttributeError):
                    closed = False
                    continue
                if t == "result":
                    closed = True
                elif t in ("assistant", "user", "step_update"):
                    closed = False
    except OSError:
        return False
    return closed


# ---------------------------------------------------------------- shell command analysis
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_WRAPPERS = {"sudo", "nohup", "exec", "time", "command", "setsid", "env", "nice", "ionice", "stdbuf",
             "timeout", "xargs", "then", "do", "else", "if", "while", "!", "{", "(", "busybox", "doas",
             "chrt", "taskset", "unbuffer"}
# wrapper options that take a separate argument: without this table `sudo -u x CMD` and
# `timeout -s KILL 5 CMD` hid CMD behind the option value (xr C8)
_WRAP_ARG_OPTS = {
    "sudo": {"-u", "-g", "-U", "-h", "-p", "-C", "-r", "-t", "-D", "-R", "--user", "--group"},
    "doas": {"-u", "-C"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "xargs": {"-n", "-P", "-I", "-L", "-d", "-E", "-s", "-a"},
    "stdbuf": {"-i", "-o", "-e"},
    "chrt": {"-p"},
    "taskset": {"-c"},
}
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "ash")
_SHELL_HEREDOC = re.compile(r"(?:^|[\s;&|(])(?:bash|sh|zsh|dash|ksh|ash)\b[^|;&\n]*<<")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SDRUN_ARG_OPTS = {"-p", "--property", "-u", "--unit", "--slice", "-E", "--setenv", "--working-directory",
                   "--description", "-M", "--machine", "-H", "--host", "--uid", "--gid", "--nice", "-t"}


_REDIR_BARE = re.compile(r"^(?:\d*>>?|\d*<|&>>?)$")
_REDIR = re.compile(r"^(?:\d*>>?&?|&>>?|\d*<&?)[^<>]+$")


def restatus(r):
    """Recompute a result's status from its findings (used after waivers are applied)."""
    sev = {f["severity"] for f in r["findings"]}
    r["status"] = RED if "red" in sev else UNOBSERVED if (r["observed_count"] <= 0 or "unobserved" in sev) else GREEN
    return r


def strip_heredocs(cmd):
    """Remove heredoc bodies (they are data for the command, not shell commands)."""
    out, tag, keep = [], None, False
    for line in cmd.split("\n"):
        if tag is not None:
            if line.strip() == tag:
                tag = None
            elif keep:  # a heredoc fed to a shell is a script, not data (xr C8)
                out.append(line)
            continue
        m = _HEREDOC.search(line)
        if m:
            tag = m.group(2)
            keep = bool(_SHELL_HEREDOC.search(line))
            if keep:
                line = line[:m.start()]
        out.append(line)
    return "\n".join(out)


def split_segments(cmd):
    """Split a shell line into simple commands on ; && || | & newline ( ) outside quotes."""
    segs, cur, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                cur.append(cmd[i + 1])
                i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
            cur.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            if cmd[i + 1] != "\n":
                cur.append(c + cmd[i + 1])
            i += 1
        elif c == "$" and cmd[i + 1:i + 2] == "(":
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += (cmd[j] == "(") - (cmd[j] == ")")
                j += 1
            cur.append(cmd[i:j])  # keep $(...) inside the token; analysed separately by subshells()
            i = j - 1
        elif c == "&" and ((cur and cur[-1] == ">") or cmd[i + 1:i + 2] == ">"):
            cur.append(c)  # 2>&1, >&2, &>file are redirections, not separators
        elif c in ";|&\n()":
            segs.append("".join(cur))
            cur = []
        elif c == "#" and (not cur or cur[-1].isspace()):
            while i < len(cmd) and cmd[i] != "\n":
                i += 1
            continue
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def subshells(seg):
    """Bodies of $(...) and `...` inside a segment (they execute even inside double quotes)."""
    out, i = [], 0
    while True:
        i = seg.find("$(", i)
        if i < 0:
            break
        depth, j = 1, i + 2
        while j < len(seg) and depth:
            depth += (seg[j] == "(") - (seg[j] == ")")
            j += 1
        out.append(seg[i + 2:j - 1])
        i = j
    out += re.findall(r"`([^`]*)`", seg)
    return out


def _tokens(seg):
    """shlex split that keeps $(...) and `...` substitutions as part of one token."""
    spans, out, i = [], [], 0
    while i < len(seg):
        j = None
        if seg.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(seg) and depth:
                depth += (seg[j] == "(") - (seg[j] == ")")
                j += 1
        elif seg[i] == "`":
            k = seg.find("`", i + 1)
            j = k + 1 if k > 0 else None
        if j:
            out.append("\x00%d\x00" % len(spans))
            spans.append(seg[i:j])
            i = j
        else:
            out.append(seg[i])
            i += 1
    flat = "".join(out)
    try:
        toks = shlex.split(flat, posix=True)
    except ValueError:
        toks = flat.split()
    return [re.sub("\x00(\\d+)\x00", lambda m: spans[int(m.group(1))], t) for t in toks]


def _mem_bytes(v):
    v = str(v).strip()
    if v.lower() in ("infinity", "max", ""):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?$", v, re.I)
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}[m.group(2).upper()])


def analyze(cmd, cap=None, depth=0):
    """Flatten a shell command into simple commands.

    Returns a list of {"head": basename, "argv": [...], "raw": segment, "cap": props|None} where
    cap is the property dict of the enclosing `systemd-run` (None when the command is not inside one).
    """
    out = []
    if depth > 6:
        return out
    for seg in split_segments(strip_heredocs(cmd)):
        for sub in subshells(seg):
            out += analyze(sub, cap, depth + 1)
        toks = _tokens(seg)
        i, via = 0, []
        while i < len(toks):
            t = toks[i]
            if _ASSIGN.match(t):
                i += 1
            elif os.path.basename(t) in _WRAPPERS:
                t = os.path.basename(t)
                via.append(t)
                i += 1
                argopts = _WRAP_ARG_OPTS.get(t, ())
                while i < len(toks) and (toks[i].startswith("-") or (t == "timeout" and re.match(r"^\d", toks[i]))
                                         or (t in ("nice", "ionice") and toks[i].isdigit())):
                    i += 2 if toks[i] in argopts else 1
            else:
                break
        toks, kept, skip = toks[i:], [], False
        for t in toks:  # redirections are not arguments
            if skip:
                skip = False
            elif _REDIR_BARE.match(t):
                skip = True
            elif not _REDIR.match(t):
                kept.append(t)
        toks = kept
        if not toks or (len(toks) > 1 and toks[1] == "="):
            continue
        head = os.path.basename(toks[0])
        if head == "systemd-run":
            props, j = {"_scope": "--scope" in toks}, 1
            while j < len(toks) and toks[j].startswith("-"):
                o = toks[j]
                val = None
                if o in _SDRUN_ARG_OPTS and j + 1 < len(toks):
                    val, j = toks[j + 1], j + 1
                elif o.startswith("--property="):
                    o, val = "-p", o.split("=", 1)[1]
                elif o.startswith("-p") and len(o) > 2:
                    o, val = "-p", o[2:]
                if o in ("-p", "--property") and val and "=" in val:
                    k, v = val.split("=", 1)
                    props[k] = v
                j += 1
            out.append({"head": head, "argv": toks, "raw": seg, "cap": cap, "via": via})
            if j < len(toks):
                out += analyze(shlex.join(toks[j:]), props, depth + 1)
            continue
        out.append({"head": head, "argv": toks, "raw": seg, "cap": cap, "via": via})
        if head in _SHELLS:
            # -c may be bundled (-lc, -ec, -xec) and preceded by other options (xr C8)
            k = 1
            while k < len(toks) and toks[k][:1] in "-+" and len(toks[k]) > 1:
                if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", toks[k]):
                    if k + 1 < len(toks):
                        out += analyze(toks[k + 1], cap, depth + 1)
                    break
                k += 2 if toks[k] in ("-o", "+o", "-O", "+O") else 1
        elif head == "eval" and len(toks) > 1:
            out += analyze(" ".join(toks[1:]), cap, depth + 1)
    return out


def source_files(roots, exts=(".py", ".sh")):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", ".git", "node_modules"))
            for name in sorted(files):
                if name.endswith(exts):
                    yield os.path.join(d, name)


def py_string_constants(path):
    """Yield (lineno, value) for every non-docstring string constant of a Python file."""
    import ast
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return
    lines = src.split("\n")
    doc = set()
    for node in ast.walk(tree):
        # docstrings and any other bare string statement: text for readers, never used by the program
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            doc.add(id(node.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc:
            ln = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", ln) or ln
            if any(WAIVER in lines[k - 1] for k in range(ln, min(end, len(lines)) + 1) if k >= 1):
                continue
            yield ln, node.value


WAIVER = "gate-ok:"  # put `# gate-ok: <reason>` on a source line to waive a static finding (it is reported in GATES.md)
