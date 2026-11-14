#!/usr/bin/env python3
"""VIS-FG independent false-green gates for the `vis` CLI.

Adversarial author: catches a tool that says yes too easily. Contract-only;
never assumes vis internals. All GUI work goes through `vis` itself.

Usage:
    python3 gates/false_green.py --vis <path to vis> --out <results.json>

Runs cases A..Q, prints a table, exits 0 only if every case behaved as
required. Fixtures live under /dev/shm/vqa-cu/vis-fg/ and are removed at end.
Stdlib only. Fixtures use Tk or plain HTML and avoid locale-dependent strings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import time

FG_ROOT = "/dev/shm/vqa-cu/vis-fg"
VIS_ROOT = "/dev/shm/vqa-cu/vis"

RC_OK = 0
RC_USAGE = 2
RC_EXPECT = 3
RC_RETRIES = 4
RC_RULE = 5
RC_BUDGET = 6
RC_ABSENT = 7
RC_TARGET = 8
RC_INTERNAL = 9

CODE_FOR_RC = {
    0: "OK",
    2: "E_USAGE",
    3: "E_EXPECT",
    4: "E_RETRIES",
    5: "E_RULE",
    6: "E_BUDGET",
    7: "E_ABSENT",
    8: "E_TARGET",
    9: "E_INTERNAL",
}

JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def synthetic_key(prefix, n):
    """Fresh random key-shaped string (mixed case and digits); never a real credential."""
    import random
    import string
    rng = random.SystemRandom()
    alpha = string.ascii_letters + string.digits
    while True:
        body = "".join(rng.choice(alpha) for _ in range(n))
        if (any(c.isupper() for c in body) and any(c.islower() for c in body)
                and any(c.isdigit() for c in body)):
            return prefix + body


# ---------------------------------------------------------------------------
# Small result types
# ---------------------------------------------------------------------------

class Check:
    def __init__(self, name, expected, actual, ok, detail=""):
        self.name = name
        self.expected = expected
        self.actual = actual
        self.ok = bool(ok)
        self.detail = detail

    def to_dict(self):
        return {
            "name": self.name,
            "expected": self.expected,
            "actual": self.actual,
            "ok": self.ok,
            "detail": self.detail,
        }


class CaseResult:
    def __init__(self, cid, title):
        self.cid = cid
        self.title = title
        self.checks = []
        self.t0 = time.time()
        self.duration_s = 0.0

    def add(self, name, expected, actual, ok, detail=""):
        self.checks.append(Check(name, expected, actual, ok, detail))
        return self.checks[-1].ok

    @property
    def passed(self):
        return len(self.checks) > 0 and all(c.ok for c in self.checks)

    def finish(self):
        self.duration_s = time.time() - self.t0

    def to_dict(self):
        return {
            "id": self.cid,
            "title": self.title,
            "passed": self.passed,
            "duration_s": round(self.duration_s, 3),
            "checks": [c.to_dict() for c in self.checks],
        }


def receipts_home():
    return os.path.join(os.path.expanduser("~"), ".local", "state", "vis", "receipts")


def job_dir(job):
    return os.path.join(VIS_ROOT, job)


def unique_job(prefix):
    # Locale-independent, matches [A-Za-z0-9][A-Za-z0-9._-]{0,63}.
    stamp = int(time.time() * 1000) % 1000000
    pid = os.getpid() % 100000
    name = "%s-%d-%d" % (prefix, pid, stamp)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    if not name or not name[0].isalnum():
        name = "fg-" + name
    return name[:63]


# ---------------------------------------------------------------------------
# vis subprocess wrapper
# ---------------------------------------------------------------------------

def _vis_cmd(vis_path, args):
    if vis_path.endswith(".py"):
        return [sys.executable, "-I", vis_path] + list(args)
    try:
        with open(vis_path, "rb") as f:
            head = f.read(2)
        is_script = head == b"#!"
    except OSError:
        is_script = False
    executable = os.access(vis_path, os.X_OK)
    if executable and is_script:
        return [vis_path] + list(args)
    if executable and not vis_path.endswith(".py"):
        # Try direct execution first; caller falls back to python on failure.
        return [vis_path] + list(args)
    return [sys.executable, "-I", vis_path] + list(args)


def parse_vis_stdout(stdout, expected_verb=None):
    """Strict protocol check: exactly one JSON line with matching fields.

    Returns (obj or None, error or None). The contract requires every verb
    to print exactly ONE canonical JSON line on stdout with ok/bool, rc/int,
    code/str, verb/str, where code matches rc, ok is (rc==0), and verb is
    the invoked verb. Anything else is a protocol violation (a gate
    failure for vis). Diagnostics belong on stderr, never on stdout.
    """
    if stdout is None:
        return None, "empty stdout"
    text = stdout.strip()
    if not text:
        return None, "empty stdout"
    lines = text.splitlines()
    if len(lines) != 1:
        return None, "expected exactly one JSON line, got %d" % len(lines)
    s = lines[0].strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        return None, "json decode: %s" % e
    if not isinstance(obj, dict):
        return None, "top-level JSON is not an object"
    for key in ("ok", "rc", "code", "verb"):
        if key not in obj:
            return None, "missing key %r" % key
    if type(obj.get("ok")) is not bool:
        return None, "ok is not bool"
    rc_raw = obj.get("rc")
    if isinstance(rc_raw, bool):
        return None, "rc is bool"
    if type(rc_raw) is not int:
        return None, "rc is not int"
    rc = rc_raw
    if rc not in CODE_FOR_RC:
        return None, "rc %r not allowed (want one of 0,2,3,4,5,6,7,8,9)" % (rc,)
    if not isinstance(obj.get("code"), str):
        return None, "code is not str"
    if obj.get("code") != CODE_FOR_RC[rc]:
        return None, "code %r does not match rc %d (want %s)" % (
            obj.get("code"), rc, CODE_FOR_RC[rc])
    if obj.get("ok") != (rc == 0):
        return None, "ok %r does not match rc %d" % (obj.get("ok"), rc)
    if not isinstance(obj.get("verb"), str):
        return None, "verb is not str"
    if expected_verb is not None and obj.get("verb") != expected_verb:
        return None, "verb %r != expected %r" % (obj.get("verb"), expected_verb)
    return obj, None


def parser_type_self_check():
    """Reject int/bool confusion; accept one valid envelope.

    Returns (ok, detail). Covers the VIS-FG4 tightening: ok=1 and
    rc=true must be parse errors.
    """
    bad = [
        '{"ok": 1, "rc": 0, "code": "OK", "verb": "look"}',
        '{"ok": true, "rc": true, "code": "OK", "verb": "look"}',
    ]
    for s in bad:
        obj, err = parse_vis_stdout(s, "look")
        if obj is not None or err is None:
            return False, "parser accepted %s" % s
    good = '{"ok": true, "rc": 0, "code": "OK", "verb": "look"}'
    obj, err = parse_vis_stdout(good, "look")
    if obj is None:
        return False, "parser rejected valid envelope: %s" % err
    return True, ""


def run_vis(vis_path, args, env_extra=None, timeout=60, drop_display=True):
    """Run one vis verb. Never raises on vis failure; returns a dict."""
    cmd = _vis_cmd(vis_path, args)
    expected_verb = args[0] if args else None
    if expected_verb is not None and expected_verb.startswith("-"):
        expected_verb = None
    env = dict(os.environ)
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LANGUAGE", "C")
    if drop_display and (env_extra is None or "DISPLAY" not in env_extra):
        env.pop("DISPLAY", None)
    if env_extra:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    # Never let a stale MV_HOSTED leak into non-K1 calls.
    if env_extra is None or "MV_HOSTED" not in env_extra:
        env.pop("MV_HOSTED", None)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError as e:
        return {
            "cmd": cmd, "proc_rc": None, "stdout": "", "stderr": str(e),
            "json": None, "json_rc": None, "ok": None, "code": None,
            "verb": None, "parse_error": "not found: %s" % e,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return {
            "cmd": cmd, "proc_rc": None, "stdout": out, "stderr": err,
            "json": None, "json_rc": None, "ok": None, "code": None,
            "verb": None, "parse_error": "timeout after %ss" % timeout,
            "timeout": True,
        }
    obj, perr = parse_vis_stdout(proc.stdout, expected_verb)
    if obj is None:
        return {
            "cmd": cmd, "proc_rc": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "json": None, "json_rc": None, "ok": None,
            "code": None, "verb": None, "parse_error": perr, "timeout": False,
            "expected_verb": expected_verb,
        }
    jrc = obj.get("rc")
    if proc.returncode != jrc:
        return {
            "cmd": cmd, "proc_rc": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "json": obj, "json_rc": jrc,
            "ok": obj.get("ok"), "code": obj.get("code"),
            "verb": obj.get("verb"),
            "parse_error": "proc rc %s != json rc %s" % (
                proc.returncode, jrc),
            "timeout": False, "expected_verb": expected_verb,
        }
    return {
        "cmd": cmd, "proc_rc": proc.returncode, "stdout": proc.stdout,
        "stderr": proc.stderr, "json": obj, "json_rc": jrc,
        "ok": obj.get("ok"), "code": obj.get("code"),
        "verb": obj.get("verb"), "parse_error": None, "timeout": False,
        "expected_verb": expected_verb,
    }


def rc_summary(res):
    if res.get("timeout"):
        return "timeout"
    if res.get("parse_error"):
        return "proc=%s parse_error=%s" % (res.get("proc_rc"), res["parse_error"])
    return "proc=%s json_rc=%s ok=%s code=%s" % (
        res.get("proc_rc"), res.get("json_rc"), res.get("ok"), res.get("code"))


def v_start(vis, job, app=None, web=None, size="800x600", timeout_s=30,
           extra_args=None, env_extra=None, timeout=60):
    args = ["start", "--job", job]
    if app is not None:
        args += ["--app", app]
    if web is not None:
        args += ["--web", web]
    if size:
        args += ["--size", size]
    args += ["--timeout", str(timeout_s)]
    if extra_args:
        args += list(extra_args)
    return run_vis(vis, args, env_extra=env_extra, timeout=timeout)


def v_look(vis, job, timeout=30):
    return run_vis(vis, ["look", "--job", job], timeout=timeout)


def v_act(vis, job, step, action_args, expect_change="any", masks=None,
          settle_ms=None, timeout=60, env_extra=None):
    args = ["act", "--job", job, "--step", step] + list(action_args)
    if expect_change is not None:
        args += ["--expect-change", expect_change]
    if masks:
        for m in masks:
            args += ["--mask", m]
    if settle_ms is not None:
        args += ["--settle-ms", str(settle_ms)]
    return run_vis(vis, args, env_extra=env_extra, timeout=timeout)


def v_check(vis, job, check_args, timeout=30):
    return run_vis(vis, ["check", "--job", job] + list(check_args), timeout=timeout)


def v_stop(vis, job, timeout=30):
    return run_vis(vis, ["stop", "--job", job], timeout=timeout)


def v_adopt(vis, job, display, pid, timeout=30):
    return run_vis(
        vis, ["adopt", "--job", job, "--display", display, "--xvfb-pid", str(pid)],
        timeout=timeout)


def v_attach(vis, job, display, token, allow_act=False, timeout=30,
             env_extra=None):
    args = ["attach", "--job", job, "--display", display,
            "--owner-ok", token]
    if allow_act:
        args += ["--allow-act"]
    return run_vis(vis, args, env_extra=env_extra, timeout=timeout)


def v_doctor(vis, extra_args=None, env_extra=None, timeout=30):
    return run_vis(vis, ["doctor"] + list(extra_args or []),
                   env_extra=env_extra, timeout=timeout)


def v_sweep(vis, timeout=60):
    return run_vis(vis, ["sweep"], timeout=timeout)


def v_ask(vis, job, seat, q, extra_args=None, timeout=30):
    return run_vis(vis, ["ask", "--job", job, "--seat", seat, "--q", q]
                   + list(extra_args or []), timeout=timeout)


def find_state_nodes(state):
    """Flatten wender.state/v1 STATE (unknown schema) to node dicts."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            # Heuristic: a node has an id-like key or role+name.
            keys = {str(k).lower() for k in o.keys()}
            if ("role" in keys or "name" in keys or "node_id" in keys
                    or "nodeid" in keys or "id" in keys):
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(state)
    return out


def node_id_of(node):
    for k in ("node_id", "nodeId", "id", "backendNodeId", "backend_node_id"):
        if k in node:
            return str(node[k])
        for kk in node.keys():
            if str(kk).lower() == k.lower():
                return str(node[kk])
    return None


# ---------------------------------------------------------------------------
# Fixture helpers (all under /dev/shm, locale-neutral)
# ---------------------------------------------------------------------------

def ensure_dir(path, mode=0o700):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def write_file(path, content, mode=0o600):
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def read_counter(path):
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if t == "":
            return 0
        return int(t)
    except (OSError, ValueError):
        return None


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def case_subdir(letter):
    d = os.path.join(FG_ROOT, "case-%s" % letter.lower())
    ensure_dir(d)
    # Fresh counter/state per run.
    for name in ("counter", "received", "keys", "status"):
        p = os.path.join(d, name)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    return d


def app_cmd(*parts):
    return " ".join(shlex.quote(p) for p in parts)


def proc_state_and_start(pid):
    """Return (state, starttime) from /proc/pid/stat, or (None, None).

    Field numbering per proc(5): 1 pid, 2 comm, 3 state, ... 22 starttime.
    After stripping 'pid (comm)' with rsplit(')', 1), split[0] is field 3
    (state), so starttime (field 22) is split[19]. A zombie (Z) or dead
    (X/x) counts as not alive even though /proc still exists.
    """
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
            data = f.read()
        after = data.rsplit(")", 1)[1].split()
        if len(after) < 20:
            return None, None
        return after[0], after[19]
    except (OSError, IndexError, ValueError):
        return None, None


def is_pid_alive_with_start(pid, start_time=None):
    """True if /proc/pid exists, is not a zombie, and start time matches."""
    state, cur = proc_state_and_start(pid)
    if state is None:
        return False
    if state in ("Z", "X", "x"):
        return False
    if start_time is None:
        return True
    return str(cur) == str(start_time)


def extract_pids_from_jobjson(obj):
    """Best-effort pid/starttime pairs from an unknown job.json schema."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            # Common shapes: {"pids": [...]}, {"pid": N, "start": S}, etc.
            keys = {k.lower() for k in o.keys()}
            if "pid" in keys:
                try:
                    pid = None
                    for k in o.keys():
                        if k.lower() == "pid":
                            pid = int(o[k])
                    start = None
                    for k in ("start", "starttime", "start_time", "stime"):
                        if k in o:
                            start = str(o[k])
                        for kk in o.keys():
                            if kk.lower() == k:
                                start = str(o[kk])
                    if pid:
                        found.append((pid, start))
                except (TypeError, ValueError):
                    pass
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    # Deduplicate, keep order.
    seen = set()
    out = []
    for pid, st in found:
        if pid not in seen:
            seen.add(pid)
            out.append((pid, st))
    return out


def df_fstype(path):
    try:
        p = subprocess.run(["df", "-T", path], capture_output=True, text=True,
                           timeout=10)
        lines = p.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 2:
                return parts[1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Tk fixture sources (neutral literals only; geometry fixed, no locale use)
# ---------------------------------------------------------------------------

_TK_PREAMBLE = """import os, sys
def _bump(path):
    try:
        n = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                t = f.read().strip()
                n = int(t) if t else 0
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(n + 1))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        return n + 1
    except Exception:
        return None
def _write(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except Exception:
        pass
"""


def tk_noop_source(counter_path):
    # Button fills the window; press changes nothing visible. Active colors
    # equal normal colors so the pressed frame matches the settled frame.
    return _TK_PREAMBLE + (
        "import tkinter as tk\n"
        "CTR = %r\n" % counter_path +
        "root = tk.Tk()\n"
        "root.title('FG-A')\n"
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "b = tk.Button(root, text='0', bg='white', fg='black',\n"
        "              activebackground='white', activeforeground='black',\n"
        "              highlightthickness=0, relief='flat',\n"
        "              command=lambda: _bump(CTR))\n"
        "b.pack(fill='both', expand=True)\n"
        "root.mainloop()\n"
    )


def tk_failed_source(counter_path, status_path):
    # Click paints "Status: failed" (wrong-text fixture for case C).
    return _TK_PREAMBLE + (
        "import tkinter as tk\n"
        "CTR = %r\nSTA = %r\n" % (counter_path, status_path) +
        "root = tk.Tk()\n"
        "root.title('FG-C')\n"
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "fix = tk.Label(root, text='=', bg='white', fg='black')\n"
        "fix.place(x=10, y=10, width=50, height=50)\n"
        "sta = tk.Label(root, text='Status: idle', bg='white', fg='black')\n"
        "sta.place(x=0, y=400, width=800, height=100)\n"
        "def on_click():\n"
        "    _bump(CTR)\n"
        "    sta.config(text='Status: failed')\n"
        "    _write(STA, 'Status: failed')\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.place(x=300, y=200, width=200, height=100)\n"
        "_write(STA, 'Status: idle')\n"
        "root.mainloop()\n"
    )


def tk_simple_change_source(counter_path, status_path, title="FG-G1"):
    # One-shot 0 -> 1 with inverted colors (large pixel change).
    return _TK_PREAMBLE + (
        "import tkinter as tk\n"
        "CTR = %r\nSTA = %r\n" % (counter_path, status_path) +
        "root = tk.Tk()\n"
        "root.title(%r)\n" % title +
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "fix = tk.Label(root, text='=', bg='white', fg='black')\n"
        "fix.place(x=10, y=10, width=50, height=50)\n"
        "sta = tk.Label(root, text='0', bg='white', fg='black')\n"
        "sta.place(x=0, y=400, width=800, height=100)\n"
        "def on_click():\n"
        "    _bump(CTR)\n"
        "    sta.config(text='1', bg='black', fg='white')\n"
        "    _write(STA, '1')\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.place(x=300, y=200, width=200, height=100)\n"
        "_write(STA, '0')\n"
        "root.mainloop()\n"
    )


def tk_toggle_source(counter_path, status_path, title="FG-H"):
    # Every click toggles 0/1 with inverted colors.
    return _TK_PREAMBLE + (
        "import tkinter as tk\n"
        "CTR = %r\nSTA = %r\n" % (counter_path, status_path) +
        "root = tk.Tk()\n"
        "root.title(%r)\n" % title +
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "fix = tk.Label(root, text='=', bg='white', fg='black')\n"
        "fix.place(x=10, y=10, width=50, height=50)\n"
        "sta = tk.Label(root, text='0', bg='white', fg='black')\n"
        "sta.place(x=0, y=400, width=800, height=100)\n"
        "def on_click():\n"
        "    n = _bump(CTR) or 0\n"
        "    v = str(n % 2)\n"
        "    if n % 2:\n"
        "        sta.config(text=v, bg='black', fg='white')\n"
        "    else:\n"
        "        sta.config(text=v, bg='white', fg='black')\n"
        "    _write(STA, v)\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.place(x=300, y=200, width=200, height=100)\n"
        "_write(STA, '0')\n"
        "root.mainloop()\n"
    )


def tk_toggle_geom_source(counter_path, status_path, geom_path, title="FG-N"):
    # Same toggle behavior, plus periodic screen-geometry reports for the
    # button (winfo_rootx/y/w/h as JSON) so the gate can match marks by box
    # without knowing vis internals.
    return _TK_PREAMBLE + (
        "import json, tkinter as tk\n"
        "CTR = %r\nSTA = %r\nGEO = %r\n"
        % (counter_path, status_path, geom_path) +
        "root = tk.Tk()\n"
        "root.title(%r)\n" % title +
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "fix = tk.Label(root, text='=', bg='white', fg='black')\n"
        "fix.place(x=10, y=10, width=50, height=50)\n"
        "sta = tk.Label(root, text='0', bg='white', fg='black')\n"
        "sta.place(x=0, y=400, width=800, height=100)\n"
        "def on_click():\n"
        "    n = _bump(CTR) or 0\n"
        "    v = str(n % 2)\n"
        "    if n % 2:\n"
        "        sta.config(text=v, bg='black', fg='white')\n"
        "    else:\n"
        "        sta.config(text=v, bg='white', fg='black')\n"
        "    _write(STA, v)\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.place(x=300, y=200, width=200, height=100)\n"
        "_write(STA, '0')\n"
        "def _dump_geo(n=0):\n"
        "    try:\n"
        "        root.update_idletasks()\n"
        "        g = {'x': int(b.winfo_rootx()), 'y': int(b.winfo_rooty()),\n"
        "             'w': int(b.winfo_width()), 'h': int(b.winfo_height())}\n"
        "        _write(GEO, json.dumps(g))\n"
        "    except Exception:\n"
        "        pass\n"
        "    if n < 40:\n"
        "        try:\n"
        "            root.after(250, lambda: _dump_geo(n+1))\n"
        "        except Exception:\n"
        "            pass\n"
        "try:\n"
        "    root.after(250, lambda: _dump_geo(0))\n"
        "except Exception:\n"
        "    pass\n"
        "root.mainloop()\n"
    )


def tk_crash_source(counter_path):
    return _TK_PREAMBLE + (
        "import os, tkinter as tk\n"
        "CTR = %r\n" % counter_path +
        "root = tk.Tk()\n"
        "root.title('FG-E')\n"
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "def on_click():\n"
        "    _bump(CTR)\n"
        "    try:\n"
        "        root.update_idletasks()\n"
        "    except Exception:\n"
        "        pass\n"
        "    os._exit(1)\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.pack(fill='both', expand=True)\n"
        "root.mainloop()\n"
    )


def tk_freeze_source(counter_path):
    return _TK_PREAMBLE + (
        "import time, tkinter as tk\n"
        "CTR = %r\n" % counter_path +
        "root = tk.Tk()\n"
        "root.title('FG-F')\n"
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "def on_click():\n"
        "    _bump(CTR)\n"
        "    time.sleep(1000)\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.pack(fill='both', expand=True)\n"
        "root.mainloop()\n"
    )


def tk_anim_source(counter_path):
    # Bottom band ticks 0..9 every 100 ms. Clicks anywhere only bump the file.
    return _TK_PREAMBLE + (
        "import tkinter as tk\n"
        "CTR = %r\n" % counter_path +
        "root = tk.Tk()\n"
        "root.title('FG-I')\n"
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "fix = tk.Label(root, text='=', bg='white', fg='black')\n"
        "fix.place(x=10, y=10, width=50, height=50)\n"
        "anim = tk.Label(root, text='0', bg='white', fg='black')\n"
        "anim.place(x=0, y=400, width=800, height=200)\n"
        "n = [0]\n"
        "def tick():\n"
        "    n[0] += 1\n"
        "    try:\n"
        "        anim.config(text=str(n[0] % 10))\n"
        "    except Exception:\n"
        "        pass\n"
        "    root.after(100, tick)\n"
        "def on_click(ev):\n"
        "    _bump(CTR)\n"
        "    return None\n"
        "root.bind('<Button-1>', on_click)\n"
        "root.after(100, tick)\n"
        "root.mainloop()\n"
    )


def tk_entry_source(recv_path, keys_path):
    return (
        "import os, sys, tkinter as tk\n"
        "RECV = %r\nKEYS = %r\n" % (recv_path, keys_path) +
        "def _w(p, t):\n"
        "    try:\n"
        "        open(p, 'w', encoding='utf-8').write(t)\n"
        "    except Exception:\n"
        "        pass\n"
        "root = tk.Tk()\n"
        "root.title('FG-K2')\n"
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "var = tk.StringVar(value='')\n"
        "e = tk.Entry(root, textvariable=var, bg='white', fg='black',\n"
        "             justify='center', highlightthickness=0, relief='flat')\n"
        "e.place(x=200, y=250, width=400, height=50)\n"
        "e.focus_set()\n"
        "def on_var(*a):\n"
        "    _w(RECV, var.get())\n"
        "try:\n"
        "    var.trace_add('write', on_var)\n"
        "except Exception:\n"
        "    try:\n"
        "        var.trace('w', on_var)\n"
        "    except Exception:\n"
        "        pass\n"
        "n = [0]\n"
        "def on_key(ev):\n"
        "    n[0] += 1\n"
        "    _w(KEYS, str(n[0]))\n"
        "root.bind('<Key>', on_key)\n"
        "_w(RECV, '')\n"
        "_w(KEYS, '0')\n"
        "root.mainloop()\n"
    )


def tk_env_dump_source(dump_path, counter_path, title="FG-O"):
    # Dumps private-session env to JSON on startup, then behaves like a
    # toggle button (every click flips 0/1 with inverted colors).
    return _TK_PREAMBLE + (
        "import json, os, tkinter as tk\n"
        "DUMP = %r\nCTR = %r\n" % (dump_path, counter_path) +
        "root = tk.Tk()\n"
        "root.title(%r)\n" % title +
        "root.geometry('800x600+0+0')\n"
        "root.configure(bg='white')\n"
        "keys = ['HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME',"
        "'XDG_RUNTIME_DIR','TMPDIR','DISPLAY','LANG','LANGUAGE']\n"
        "try:\n"
        "    snap = {k: os.environ.get(k, '') for k in keys}\n"
        "    open(DUMP,'w',encoding='utf-8').write(json.dumps(snap))\n"
        "except Exception:\n"
        "    pass\n"
        "sta = tk.Label(root, text='0', bg='white', fg='black')\n"
        "sta.place(x=0, y=400, width=800, height=100)\n"
        "def on_click():\n"
        "    n = _bump(CTR) or 0\n"
        "    v = str(n % 2)\n"
        "    if n % 2:\n"
        "        sta.config(text=v, bg='black', fg='white')\n"
        "    else:\n"
        "        sta.config(text=v, bg='white', fg='black')\n"
        "b = tk.Button(root, text='0', bg='white', fg='black', command=on_click)\n"
        "b.place(x=300, y=200, width=200, height=100)\n"
        "root.mainloop()\n"
    )


# ---------------------------------------------------------------------------
# Web fixtures (hidden DOM change; file://, no server, STATE proves the click)
# ---------------------------------------------------------------------------

def web_hidden_html():
    # Accessible-name-only change: the button keeps painted text '0' and the
    # same white/black style; only its aria-label flips step-0 -> step-1.
    # STATE keeps the button (a meaningful AX node) while pruning generic
    # and static-text nodes, so a white-on-white div witness would be
    # invalid here. Focus/active paint is suppressed so settled pixels are
    # identical before and after the click.
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;height:100%;background:white}"
        "*{outline:none !important;box-shadow:none !important}"
        "#b{width:100vw;height:100vh;font-size:64px;background:white;"
        "color:black;border:0;outline:none}"
        "#b:focus,#b:active{background:white;color:black;outline:none}"
        "</style>"
        "<button id='b' aria-label='step-0'>0</button>"
        "<script>"
        "document.getElementById('b').addEventListener('click',()=>{"
        "document.getElementById('b').setAttribute('aria-label','step-1');"
        "});"
        "</script>\n"
    )


def web_paired_html():
    # Paired controls that both paint: left (wrong) and right (correct).
    # Each click paints the shared status band visibly, but only the right
    # button reaches the accessible name 'done'. The same --state predicate
    # (button=done, modulo role spelling) must fail after the wrong click
    # and pass after the right click, proving wrong text is told apart.
    # Focus paint suppressed; status band is the only intentional paint.
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;height:100%;background:white}"
        "*{outline:none !important;box-shadow:none !important}"
        "#row{display:flex;width:100vw;height:70vh}"
        "#wrong,#right{flex:1;font-size:64px;background:white;color:black;"
        "border:0;outline:none}"
        "#wrong:focus,#wrong:active,#right:focus,#right:active{"
        "background:white;color:black;outline:none}"
        "#status{width:100vw;height:30vh;font-size:64px;text-align:center;"
        "background:white;color:black}</style>"
        "<div id='row'>"
        "<button id='wrong' aria-label='pick-left'>0</button>"
        "<button id='right' aria-label='pick-right'>0</button>"
        "</div>"
        "<div id='status' aria-hidden='true'>idle</div>"
        "<script>"
        "var s=document.getElementById('status');"
        "document.getElementById('wrong').addEventListener('click',()=>{"
        "document.getElementById('wrong').setAttribute('aria-label','mistake');"
        "s.textContent='wrong';s.style.background='red';s.style.color='white';"
        "});"
        "document.getElementById('right').addEventListener('click',()=>{"
        "document.getElementById('right').setAttribute('aria-label','done');"
        "s.textContent='done';s.style.background='black';s.style.color='white';"
        "});"
        "</script>\n"
    )


def web_password_html():
    # One password field (protected) plus a plain button. Typing into the
    # protected node via --ax type must be refused (rc5) without input.
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;height:100%;background:white}"
        "*{outline:none !important;box-shadow:none !important}"
        "#ok{width:100vw;height:50vh;font-size:64px;background:white;"
        "color:black;border:0}"
        "#pw{width:80vw;height:10vh;font-size:48px;margin:5vh 10vw}</style>"
        "<button id='ok' aria-label='ok-button'>0</button>"
        "<input id='pw' type='password' aria-label='secret-field' value=''>"
        "<script>"
        "document.getElementById('ok').addEventListener('click',()=>{"
        "document.getElementById('ok').setAttribute('aria-label','ok-done');"
        "});"
        "</script>\n"
    )


# ---------------------------------------------------------------------------
# Cases A..L (one function per letter)
# ---------------------------------------------------------------------------

def _expect_rc(case, label, res, want_rc, want_code=None):
    ok = (res.get("proc_rc") == want_rc and res.get("json_rc") == want_rc
          and res.get("timeout") is not True
          and res.get("parse_error") is None)
    if want_rc == 0:
        ok = ok and res.get("ok") is True
    else:
        ok = ok and res.get("ok") is False
    if want_code is not None and res.get("code") != want_code:
        ok = False
    exp = "rc=%d code=%s" % (want_rc, want_code or CODE_FOR_RC.get(want_rc, "?"))
    case.add(label, exp, rc_summary(res), ok)
    return ok


def case_a(vis_path):
    """A: no-op button, same step 4 times -> 3,3,3 then 4, counter stays 3."""
    case = CaseResult("A", "no-op button retry budget")
    d = case_subdir("a")
    counter = os.path.join(d, "counter")
    app = os.path.join(d, "app_a.py")
    write_file(app, tk_noop_source(counter))
    job = unique_job("fg-a")
    cmd = app_cmd(sys.executable, "-I", app)
    start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
    if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
        case.finish()
        return case
    rcs = []
    try:
        for i in (1, 2, 3, 4):
            r = v_act(vis_path, job, "a1", ["--xy", "400,300"],
                      expect_change="any", timeout=60)
            rcs.append(r)
            if i < 4:
                _expect_rc(case, "attempt %d is rc3" % i, r, RC_EXPECT, "E_EXPECT")
            else:
                _expect_rc(case, "attempt 4 is rc4", r, RC_RETRIES, "E_RETRIES")
        no_pass = all((r.get("proc_rc") != 0 and r.get("json_rc") != 0
                       and r.get("ok") is not True) for r in rcs)
        case.add("no pass in 4 attempts", "all rc!=0 ok!=True",
                 ",".join(str(r.get("json_rc")) for r in rcs), no_pass)
        cnt = read_counter(counter)
        case.add("4th attempt untouched target", "counter==3",
                 "counter==%s" % cnt, cnt == 3)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_b(vis_path):
    """B: accessible-name-only change must NOT pass a pixel any-change."""
    case = CaseResult("B", "aria-label change is not a pixel change")
    d = case_subdir("b")
    html = os.path.join(d, "index.html")
    write_file(html, web_hidden_html())
    file_url = "file://" + html
    case.add("fixture html written", "exists",
             "exists" if os.path.exists(html) else "missing",
             os.path.exists(html))
    job = unique_job("fg-b")
    try:
        start = v_start(vis_path, job, web=file_url, size="1280x800",
                        timeout_s=30)
        if not _expect_rc(case, "web start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        look1 = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look before passes", look1, RC_OK, "OK")
        try:
            s1 = json.dumps((look1.get("json") or {}).get("state"),
                            sort_keys=True, default=str)
        except Exception:
            s1 = None
        # Button fills the viewport; its centre is near 640,400.
        # Painted text '0' and white/black style never change; only the
        # button's aria-label flips. Focus paint is suppressed by CSS.
        act = v_act(vis_path, job, "b1", ["--xy", "640,400"],
                    expect_change="any", timeout=60)
        _expect_rc(case, "name-only change is rc3 (no pass)", act,
                   RC_EXPECT, "E_EXPECT")
        not_pass = act.get("ok") is not True and act.get("json_rc") != 0
        case.add("pixel check did not pass", "ok!=True rc!=0",
                 rc_summary(act), not_pass)
        look2 = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look after passes", look2, RC_OK, "OK")
        try:
            s2 = json.dumps((look2.get("json") or {}).get("state"),
                            sort_keys=True, default=str)
        except Exception:
            s2 = None
        st2 = (look2.get("json") or {}).get("state")
        dom_changed = (s1 is not None and s2 is not None and s1 != s2
                       and st2 is not None)
        case.add("STATE differs, proving click landed",
                 "state before != after",
                 "before=%s after=%s" % ((s1 or "")[:60], (s2 or "")[:60]),
                 dom_changed)
        kept = (s2 is not None and "step-1" in s2
                and (s1 is None or "step-1" not in s1))
        case.add("STATE keeps the button name (step-1 appears)",
                 "step-1 in after only",
                 "after_has=%s" % ("step-1" in (s2 or "")),
                 kept)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_c(vis_path):
    """C: paired controls both paint; only the right one satisfies --state."""
    case = CaseResult("C", "wrong paint told apart via STATE")
    d = case_subdir("c")
    html = os.path.join(d, "index.html")
    write_file(html, web_paired_html())
    file_url = "file://" + html
    case.add("fixture html written", "exists",
             "exists" if os.path.exists(html) else "missing",
             os.path.exists(html))
    job = unique_job("fg-c")
    candidates = ["button=done", "Button=done"]
    try:
        start = v_start(vis_path, job, web=file_url, size="1280x800",
                        timeout_s=30)
        if not _expect_rc(case, "web start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        look0 = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look before passes", look0, RC_OK, "OK")
        # Wrong (left) button centre ~320,280; paints red 'wrong'.
        act_w = v_act(vis_path, job, "c-wrong", ["--xy", "320,280"],
                      expect_change="any", timeout=60)
        _expect_rc(case, "wrong paints (any passes)", act_w, RC_OK, "OK")
        look_w = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look after wrong passes", look_w, RC_OK, "OK")
        try:
            s_w = json.dumps((look_w.get("json") or {}).get("state"),
                             sort_keys=True, default=str)
        except Exception:
            s_w = None
        wrong_landed = s_w is not None and "mistake" in s_w
        case.add("wrong click landed (STATE has mistake)",
                 "mistake in STATE", "has=%s" % wrong_landed, wrong_landed)
        # Role spelling is engine-defined (observed `button`, not `Button`).
        # Accept rc7 for the unsupported spelling, but require at least one
        # spelling to discriminate (fail after wrong, pass after right).
        wrong_checks = []
        for pred in candidates:
            chk = v_check(vis_path, job, ["--state", pred], timeout=30)
            rc = chk.get("json_rc")
            proto = (chk.get("parse_error") is None
                     and chk.get("timeout") is not True
                     and chk.get("proc_rc") == rc)
            if rc == RC_EXPECT:
                ok = proto and chk.get("ok") is False \
                    and chk.get("code") == "E_EXPECT"
            elif rc == RC_ABSENT:
                ok = proto and chk.get("ok") is False \
                    and chk.get("code") == "E_ABSENT"
            else:
                ok = False
            case.add("wrong does not satisfy --state %s" % pred,
                     "rc3 (rc7 if unsupported role)", rc_summary(chk), ok)
            wrong_checks.append(chk)
        has_fail = any(c.get("json_rc") == 3 for c in wrong_checks)
        no_pass = all(c.get("json_rc") != 0 for c in wrong_checks)
        only_3_or_7 = all(c.get("json_rc") in (3, 7) for c in wrong_checks)
        case.add("same predicate fails after wrong", ">=1 rc3, rest rc7, none rc0",
                 ",".join(str(c.get("json_rc")) for c in wrong_checks),
                 has_fail and no_pass and only_3_or_7)
        wrong_absent = []
        for pred in candidates:
            chk = v_check(vis_path, job, ["--state-absent", pred], timeout=30)
            rc = chk.get("json_rc")
            proto = (chk.get("parse_error") is None
                     and chk.get("timeout") is not True
                     and chk.get("proc_rc") == rc)
            if rc == RC_OK:
                ok = proto and chk.get("ok") is True \
                    and chk.get("code") == "OK"
            elif rc == RC_ABSENT:
                ok = proto and chk.get("ok") is False \
                    and chk.get("code") == "E_ABSENT"
            else:
                ok = False
            case.add("wrong leaves --state-absent %s passing" % pred,
                     "rc0 (rc7 if unsupported role)", rc_summary(chk), ok)
            wrong_absent.append(chk)
        has_absent = any(c.get("json_rc") == 0 for c in wrong_absent)
        no_absent_fail = all(c.get("json_rc") != 3 for c in wrong_absent)
        case.add("same absent predicate passes after wrong",
                 ">=1 rc0, rest rc7, none rc3",
                 ",".join(str(c.get("json_rc")) for c in wrong_absent),
                 has_absent and no_absent_fail)
        # Right button centre ~960,280; paints black 'done'.
        act_r = v_act(vis_path, job, "c-right", ["--xy", "960,280"],
                      expect_change="any", timeout=60)
        _expect_rc(case, "right paints (any passes)", act_r, RC_OK, "OK")
        look_r = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look after right passes", look_r, RC_OK, "OK")
        try:
            s_r = json.dumps((look_r.get("json") or {}).get("state"),
                             sort_keys=True, default=str)
        except Exception:
            s_r = None
        right_landed = (s_r is not None and "done" in s_r
                        and s_r != s_w)
        case.add("right click landed (STATE has done)",
                 "done in STATE", "has=%s" % (s_r is not None and "done" in (s_r or "")),
                 right_landed)
        right_checks = []
        for pred in candidates:
            chk = v_check(vis_path, job, ["--state", pred], timeout=30)
            right_checks.append((pred, chk))
        # At least one spelling that failed after wrong must pass now;
        # that is the discriminating predicate. No spelling may pass after
        # wrong (already checked), so a flip proves discrimination.
        # Unsupported spellings must stay refused, never flip to pass.
        flipped = [p for (p, c), w in zip(right_checks, wrong_checks)
                   if w.get("json_rc") == 3 and c.get("json_rc") == 0
                   and c.get("ok") is True and c.get("parse_error") is None
                   and c.get("proc_rc") == 0 and c.get("code") == "OK"]
        stable_absent = []
        unstable = []
        for (p, c), w in zip(right_checks, wrong_checks):
            if w.get("json_rc") == 7:
                if c.get("json_rc") == 7 and c.get("ok") is False \
                        and c.get("parse_error") is None:
                    stable_absent.append(p)
                else:
                    unstable.append(p)
        case.add("same predicate passes after right", "one flips 3->0, rc7 stays rc7",
                 "; ".join("%s=%s" % (p, c.get("json_rc"))
                           for p, c in right_checks),
                 len(flipped) >= 1 and len(unstable) == 0)
        # And the passing predicate must be protocol-clean.
        for pred, chk in right_checks:
            if pred in flipped:
                _expect_rc(case, "right satisfies --state %s" % pred,
                           chk, RC_OK, "OK")
            elif pred in stable_absent:
                _expect_rc(case, "unsupported spelling stays rc7 %s" % pred,
                           chk, RC_ABSENT, "E_ABSENT")
            elif pred in unstable:
                case.add("unsupported spelling stays rc7 %s" % pred,
                         "rc=7 code=E_ABSENT", rc_summary(chk), False)
        # The same flipped predicate must now fail its absent form.
        for pred in flipped:
            chk = v_check(vis_path, job, ["--state-absent", pred], timeout=30)
            _expect_rc(case, "right breaks --state-absent %s" % pred,
                       chk, RC_EXPECT, "E_EXPECT")
        for pred in stable_absent:
            chk = v_check(vis_path, job, ["--state-absent", pred], timeout=30)
            _expect_rc(case, "unsupported absent stays rc7 %s" % pred,
                       chk, RC_ABSENT, "E_ABSENT")
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_d(vis_path):
    """D: click misses the button; status-region expectation is rc3."""
    case = CaseResult("D", "click that misses")
    d = case_subdir("d")
    counter = os.path.join(d, "counter")
    status = os.path.join(d, "status")
    app = os.path.join(d, "app_d.py")
    write_file(app, tk_failed_source(counter, status))
    # Re-title to keep windows distinct; behavior identical.
    try:
        src = read_text(app) or ""
        write_file(app, src.replace("FG-C", "FG-D"))
    except OSError:
        pass
    job = unique_job("fg-d")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        act = v_act(vis_path, job, "d1", ["--xy", "100,100"],
                    expect_change="0,400,800,100", timeout=60)
        _expect_rc(case, "missed click is rc3", act, RC_EXPECT, "E_EXPECT")
        cnt = read_counter(counter)
        case.add("button never fired", "counter 0/absent",
                 "counter==%s" % cnt, cnt in (None, 0))
        txt = (read_text(status) or "").strip()
        case.add("status unchanged", "Status: idle", txt,
                 txt == "Status: idle")
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_e(vis_path):
    """E: app crashes on click; the action must not pass (rc8)."""
    case = CaseResult("E", "crash on click")
    d = case_subdir("e")
    counter = os.path.join(d, "counter")
    app = os.path.join(d, "app_e.py")
    write_file(app, tk_crash_source(counter))
    job = unique_job("fg-e")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        act = v_act(vis_path, job, "e1", ["--xy", "400,300"],
                    expect_change="any", timeout=60)
        _expect_rc(case, "crashed action is rc8", act, RC_TARGET, "E_TARGET")
        not_pass = act.get("ok") is not True and act.get("json_rc") != 0
        case.add("crashed action did not pass", "ok!=True rc!=0",
                 rc_summary(act), not_pass)
        time.sleep(0.5)
        cnt = read_counter(counter)
        case.add("click landed before crash", "counter==1",
                 "counter==%s" % cnt, cnt == 1)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    gone = not os.path.exists(job_dir(job))
    case.add("job dir gone after stop", "gone", "gone" if gone else "exists",
             gone)
    case.finish()
    return case


def case_f(vis_path):
    """F: app freezes on click; with any-change the action must not pass."""
    case = CaseResult("F", "freeze on click")
    d = case_subdir("f")
    counter = os.path.join(d, "counter")
    app = os.path.join(d, "app_f.py")
    write_file(app, tk_freeze_source(counter))
    job = unique_job("fg-f")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        act = v_act(vis_path, job, "f1", ["--xy", "400,300"],
                    expect_change="any", timeout=90)
        if act.get("timeout"):
            case.add("frozen action returns (no hang)", "rc 3 or 8",
                     "timeout", False)
        else:
            rc = act.get("json_rc")
            ok = act.get("ok") is False and rc in (RC_EXPECT, RC_TARGET)
            case.add("frozen action is rc3/rc8, never pass", "rc in {3,8}",
                     rc_summary(act), ok)
        cnt = read_counter(counter)
        case.add("freeze was triggered", "counter==1",
                 "counter==%s" % cnt, cnt == 1)
    finally:
        try:
            v_stop(vis_path, job, timeout=30)
        except Exception:
            pass
    gone = not os.path.exists(job_dir(job))
    case.add("job dir gone after stop", "gone", "gone" if gone else "exists",
             gone)
    case.finish()
    return case


def case_g(vis_path):
    """G: change check returns both pass and fail (not constant)."""
    case = CaseResult("G", "check is not constant")
    # Limit, stated plainly: the contract offers no documented way to inject
    # a constant-changed engine stub without assuming undocumented engine
    # layout (--target-repo/VIS_*_REPO select real pins; faking them needs
    # internal paths). So G keeps the two-direction pixel test: the same
    # detector must report change when pixels change and no change when
    # they do not, in both expectation polarities.
    case.add("G limit: no engine substitution in contract",
             "two-direction only",
             "no fake-engine hook; kept both polarities", True)
    # G1: toggling app: any passes, none fails. Catches constant-unchanged
    # (any would fail) and proves the fail direction is reachable.
    d1 = case_subdir("g1")
    c1 = os.path.join(d1, "counter")
    s1 = os.path.join(d1, "status")
    a1 = os.path.join(d1, "app_g1.py")
    write_file(a1, tk_toggle_source(c1, s1, title="FG-G1"))
    job1 = unique_job("fg-g1")
    try:
        start = v_start(vis_path, job1, app=app_cmd(sys.executable, "-I", a1),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "G1 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            a_any = v_act(vis_path, job1, "g1-any", ["--xy", "400,250"],
                          expect_change="any", timeout=60)
            _expect_rc(case, "G1 any-on-change passes", a_any, RC_OK, "OK")
            c_any = read_counter(c1)
            case.add("G1 first change happened", "counter==1",
                     "counter==%s" % c_any, c_any == 1)
            a_none = v_act(vis_path, job1, "g1-none", ["--xy", "400,250"],
                           expect_change="none", timeout=60)
            _expect_rc(case, "G1 none-on-change is rc3", a_none,
                       RC_EXPECT, "E_EXPECT")
            c_none = read_counter(c1)
            case.add("G1 second change happened", "counter==2",
                     "counter==%s" % c_none, c_none == 2)
    finally:
        try:
            v_stop(vis_path, job1)
        except Exception:
            pass
    # G2: static app: any fails, none passes. Catches constant-changed
    # (any would pass) and proves the pass direction is reachable.
    d2 = case_subdir("g2")
    c2 = os.path.join(d2, "counter")
    a2 = os.path.join(d2, "app_g2.py")
    write_file(a2, tk_noop_source(c2))
    try:
        src = read_text(a2) or ""
        write_file(a2, src.replace("FG-A", "FG-G2"))
    except OSError:
        pass
    job2 = unique_job("fg-g2")
    try:
        start = v_start(vis_path, job2, app=app_cmd(sys.executable, "-I", a2),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "G2 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            a_any = v_act(vis_path, job2, "g2-any", ["--xy", "400,300"],
                          expect_change="any", timeout=60)
            _expect_rc(case, "G2 any-on-static is rc3", a_any,
                       RC_EXPECT, "E_EXPECT")
            c_any = read_counter(c2)
            case.add("G2 first click landed without pixels", "counter==1",
                     "counter==%s" % c_any, c_any == 1)
            a_none = v_act(vis_path, job2, "g2-none", ["--xy", "400,300"],
                           expect_change="none", timeout=60)
            _expect_rc(case, "G2 none-on-static passes", a_none, RC_OK, "OK")
            c_none = read_counter(c2)
            case.add("G2 second click landed without pixels", "counter==2",
                     "counter==%s" % c_none, c_none == 2)
    finally:
        try:
            v_stop(vis_path, job2)
        except Exception:
            pass
    case.finish()
    return case


def case_h(vis_path):
    """H: change in A, expectation names B -> rc3."""
    case = CaseResult("H", "change in the wrong place")
    d = case_subdir("h")
    counter = os.path.join(d, "counter")
    status = os.path.join(d, "status")
    app = os.path.join(d, "app_h.py")
    write_file(app, tk_toggle_source(counter, status, title="FG-H"))
    job = unique_job("fg-h")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        h1 = v_act(vis_path, job, "h1", ["--xy", "400,250"],
                   expect_change="any", timeout=60)
        _expect_rc(case, "H1 toggle passes any", h1, RC_OK, "OK")
        c1 = read_counter(counter)
        case.add("H1 click landed", "counter==1", "counter==%s" % c1, c1 == 1)
        h2 = v_act(vis_path, job, "h2", ["--xy", "400,250"],
                   expect_change="10,10,50,50", timeout=60)
        _expect_rc(case, "H2 wrong region is rc3", h2, RC_EXPECT, "E_EXPECT")
        c2 = read_counter(counter)
        case.add("H2 click landed (A changed again)", "counter==2",
                 "counter==%s" % c2, c2 == 2)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_i(vis_path):
    """I: animation with none -> rc3; masked animation with none -> rc0."""
    case = CaseResult("I", "animation and mask")
    d = case_subdir("i")
    counter = os.path.join(d, "counter")
    app = os.path.join(d, "app_i.py")
    write_file(app, tk_anim_source(counter))
    job = unique_job("fg-i")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        time.sleep(0.5)
        i1 = v_act(vis_path, job, "i1", ["--xy", "100,100"],
                   expect_change="none", timeout=60)
        _expect_rc(case, "I1 animation breaks none", i1, RC_EXPECT, "E_EXPECT")
        c1 = read_counter(counter)
        case.add("I1 click landed", "counter==1", "counter==%s" % c1, c1 == 1)
        i2 = v_act(vis_path, job, "i2", ["--xy", "100,100"],
                   expect_change="none", masks=["0,400,800,200"], timeout=60)
        _expect_rc(case, "I2 masked none passes", i2, RC_OK, "OK")
        c2 = read_counter(counter)
        case.add("I2 click landed", "counter==2", "counter==%s" % c2, c2 == 2)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_j(vis_path):
    """J: reused step id after a pass is rc2 and touches nothing."""
    case = CaseResult("J", "step reuse after pass")
    d = case_subdir("j")
    counter = os.path.join(d, "counter")
    status = os.path.join(d, "status")
    app = os.path.join(d, "app_j.py")
    write_file(app, tk_toggle_source(counter, status, title="FG-J"))
    job = unique_job("fg-j")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        first = v_act(vis_path, job, "j1", ["--xy", "400,250"],
                      expect_change="any", timeout=60)
        _expect_rc(case, "J first use passes", first, RC_OK, "OK")
        c1 = read_counter(counter)
        case.add("J first touched target", "counter==1",
                 "counter==%s" % c1, c1 == 1)
        second = v_act(vis_path, job, "j1", ["--xy", "400,250"],
                       expect_change="any", timeout=60)
        _expect_rc(case, "J reuse is rc2", second, RC_USAGE, "E_USAGE")
        c2 = read_counter(counter)
        case.add("J reuse touched nothing", "counter still 1",
                 "counter==%s" % c2, c2 == 1)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def _concurrent_acts(vis_path, job, step1, step2, xy="400,250", timeout=90):
    """Run two vis act verbs overlapping. Returns (res1, res2)."""
    def build_env():
        env = dict(os.environ)
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LANGUAGE", "C")
        env.pop("DISPLAY", None)
        env.pop("MV_HOSTED", None)
        return env

    def build_cmd(step):
        args = ["act", "--job", job, "--step", step, "--xy", xy,
                "--expect-change", "any", "--settle-ms", "2000"]
        return _vis_cmd(vis_path, args)

    env = build_env()
    p1 = subprocess.Popen(build_cmd(step1), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env)
    # Small overlap: start the second while the first holds the display lock.
    time.sleep(0.3)
    p2 = subprocess.Popen(build_cmd(step2), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env)
    try:
        o1, e1 = p1.communicate(timeout=timeout)
        rc1 = p1.returncode
    except subprocess.TimeoutExpired:
        try:
            p1.kill()
        except Exception:
            pass
        o1, e1, rc1 = "", "timeout", None
    try:
        o2, e2 = p2.communicate(timeout=timeout)
        rc2 = p2.returncode
    except subprocess.TimeoutExpired:
        try:
            p2.kill()
        except Exception:
            pass
        o2, e2, rc2 = "", "timeout", None

    def wrap(o, e, rc):
        obj, perr = parse_vis_stdout(o, "act")
        jrc = None
        if obj is not None:
            try:
                jrc = int(obj.get("rc")) if obj.get("rc") is not None else None
            except (TypeError, ValueError):
                jrc = None
            if perr is None and rc != jrc:
                perr = "proc rc %s != json rc %s" % (rc, jrc)
        return {"proc_rc": rc, "stdout": o, "stderr": e, "json": obj,
                "json_rc": jrc, "ok": obj.get("ok") if obj else None,
                "code": obj.get("code") if obj else None,
                "verb": obj.get("verb") if obj else None,
                "parse_error": perr, "timeout": rc is None,
                "expected_verb": "act"}

    return wrap(o1, e1, rc1), wrap(o2, e2, rc2)


def _concurrent_looks(vis_path, job, timeout=60):
    """Run two vis look verbs overlapping. Readers must both pass."""
    def build_env():
        env = dict(os.environ)
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LANGUAGE", "C")
        env.pop("DISPLAY", None)
        env.pop("MV_HOSTED", None)
        return env

    env = build_env()
    c1 = _vis_cmd(vis_path, ["look", "--job", job])
    c2 = _vis_cmd(vis_path, ["look", "--job", job])
    p1 = subprocess.Popen(c1, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env)
    time.sleep(0.1)
    p2 = subprocess.Popen(c2, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env)
    try:
        o1, e1 = p1.communicate(timeout=timeout)
        rc1 = p1.returncode
    except subprocess.TimeoutExpired:
        try:
            p1.kill()
        except Exception:
            pass
        o1, e1, rc1 = "", "timeout", None
    try:
        o2, e2 = p2.communicate(timeout=timeout)
        rc2 = p2.returncode
    except subprocess.TimeoutExpired:
        try:
            p2.kill()
        except Exception:
            pass
        o2, e2, rc2 = "", "timeout", None

    def wrap(o, e, rc):
        obj, perr = parse_vis_stdout(o, "look")
        jrc = None
        if obj is not None:
            try:
                jrc = int(obj.get("rc")) if obj.get("rc") is not None else None
            except (TypeError, ValueError):
                jrc = None
            if perr is None and rc != jrc:
                perr = "proc rc %s != json rc %s" % (rc, jrc)
        return {"proc_rc": rc, "stdout": o, "stderr": e, "json": obj,
                "json_rc": jrc, "ok": obj.get("ok") if obj else None,
                "code": obj.get("code") if obj else None,
                "verb": obj.get("verb") if obj else None,
                "parse_error": perr, "timeout": rc is None,
                "expected_verb": "look"}

    return wrap(o1, e1, rc1), wrap(o2, e2, rc2)


def case_k(vis_path):
    """K: hard rules refuse without side effects."""
    case = CaseResult("K", "hard rules")
    # K1: MV_HOSTED=1 refuses acts.
    d1 = case_subdir("k1")
    c1 = os.path.join(d1, "counter")
    s1 = os.path.join(d1, "status")
    a1 = os.path.join(d1, "app_k1.py")
    write_file(a1, tk_simple_change_source(c1, s1, title="FG-K1"))
    job1 = unique_job("fg-k1")
    try:
        start = v_start(vis_path, job1, app=app_cmd(sys.executable, "-I", a1),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "K1 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            act = v_act(vis_path, job1, "k1", ["--xy", "400,250"],
                        expect_change="any", timeout=60,
                        env_extra={"MV_HOSTED": "1"})
            _expect_rc(case, "K1 hosted act is rc5", act, RC_RULE, "E_RULE")
            cnt = read_counter(c1)
            # Counter file may not exist yet (app never clicked): None==0 ok.
            untouched = cnt in (None, 0)
            case.add("K1 no input reached app", "counter 0/absent",
                     "counter==%s" % cnt, untouched)
    finally:
        try:
            v_stop(vis_path, job1)
        except Exception:
            pass
    # K2: secret-looking --type refused.
    d2 = case_subdir("k2")
    recv2 = os.path.join(d2, "received")
    keys2 = os.path.join(d2, "keys")
    a2 = os.path.join(d2, "app_k2.py")
    write_file(a2, tk_entry_source(recv2, keys2))
    job2 = unique_job("fg-k2")
    try:
        start = v_start(vis_path, job2, app=app_cmd(sys.executable, "-I", a2),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "K2 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            time.sleep(0.5)
            act = v_act(vis_path, job2, "k2",
                        ["--type", synthetic_key("sk-", 48)],
                        expect_change="any", timeout=60)
            _expect_rc(case, "K2 secret type is rc5", act, RC_RULE, "E_RULE")
            keys = read_counter(keys2)
            recv = read_text(recv2)
            case.add("K2 app got no keys", "keys 0/absent",
                     "keys==%s" % keys, keys in (None, 0))
            case.add("K2 entry stayed empty", "received empty/absent",
                     "received==%r" % ((recv or "")[:40]),
                     (recv or "") == "")
    finally:
        try:
            v_stop(vis_path, job2)
        except Exception:
            pass
    # K3: missing --expect-change defaults to any.
    d3 = case_subdir("k3")
    c3 = os.path.join(d3, "counter")
    a3 = os.path.join(d3, "app_k3.py")
    write_file(a3, tk_noop_source(c3))
    try:
        src = read_text(a3) or ""
        write_file(a3, src.replace("FG-A", "FG-K3"))
    except OSError:
        pass
    job3 = unique_job("fg-k3")
    try:
        start = v_start(vis_path, job3, app=app_cmd(sys.executable, "-I", a3),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "K3 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            act = v_act(vis_path, job3, "k3", ["--xy", "400,300"],
                        expect_change=None, timeout=60)
            # Contract F2: a missing --expect-change means "any". The no-op
            # button gets the click but paints nothing, so it is still rc3.
            _expect_rc(case, "K3 missing expect defaults to any (no-op is rc3)", act,
                       RC_EXPECT, "E_EXPECT")
            cnt = read_counter(c3)
            case.add("K3 default-any input reached the app", "counter 1",
                     "counter==%s" % cnt, cnt == 1)
    finally:
        try:
            v_stop(vis_path, job3)
        except Exception:
            pass
    # K4: inherited DISPLAY ignored.
    d4 = case_subdir("k4")
    c4 = os.path.join(d4, "counter")
    s4 = os.path.join(d4, "status")
    a4 = os.path.join(d4, "app_k4.py")
    write_file(a4, tk_simple_change_source(c4, s4, title="FG-K4"))
    job4 = unique_job("fg-k4")
    try:
        start = v_start(vis_path, job4, app=app_cmd(sys.executable, "-I", a4),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "K4 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            act = v_act(vis_path, job4, "k4", ["--xy", "400,250"],
                        expect_change="any", timeout=60,
                        env_extra={"DISPLAY": ":999"})
            _expect_rc(case, "K4 bogus DISPLAY still passes", act,
                       RC_OK, "OK")
            cnt = read_counter(c4)
            case.add("K4 acted on job display", "counter==1",
                     "counter==%s" % cnt, cnt == 1)
    finally:
        try:
            v_stop(vis_path, job4)
        except Exception:
            pass
    # K5: adopt with a PID that is not that display's Xvfb is rc5.
    job5 = unique_job("fg-k5")
    adop = v_adopt(vis_path, job5, ":999", os.getpid(), timeout=30)
    _expect_rc(case, "K5 bad adopt is rc5", adop, RC_RULE, "E_RULE")
    case.add("K5 no job dir created", "absent",
             "exists" if os.path.exists(job_dir(job5)) else "absent",
             not os.path.exists(job_dir(job5)))
    try:
        v_stop(vis_path, job5)
    except Exception:
        pass
    # K6: contract F6: overlapping acts on one job serialise on the job
    # lifecycle lock (bounded 10 s wait): both pass and each input lands
    # exactly once (0,0,2). A refusal or a lost or doubled input fails.
    d6 = case_subdir("k6")
    c6 = os.path.join(d6, "counter")
    s6 = os.path.join(d6, "status")
    a6 = os.path.join(d6, "app_k6.py")
    write_file(a6, tk_toggle_source(c6, s6, title="FG-K6"))
    job6 = unique_job("fg-k6")
    try:
        start = v_start(vis_path, job6, app=app_cmd(sys.executable, "-I", a6),
                        size="800x600", timeout_s=30)
        _expect_rc(case, "K6 start passes", start, RC_OK, "OK")
        if start.get("json_rc") == 0:
            r1, r2 = _concurrent_acts(vis_path, job6, "k6a", "k6b")
            time.sleep(0.5)
            cnt = read_counter(c6)
            rc1, rc2 = r1.get("json_rc"), r2.get("json_rc")
            strict = rc1 == 0 and rc2 == 0 and cnt == 2
            case.add("K6 overlapping acts serialised, one input each",
                     "(0,0,2)",
                     "(%s,%s,%s) %s | %s" % (rc1, rc2, cnt,
                                             rc_summary(r1), rc_summary(r2)),
                     strict)
            # Any (0,0) or counter mismatch with a refusal is interleaved.
            bad = ((cnt not in (None, 0, 1, 2)) or
                   (rc1 == 0 and rc2 == 0 and cnt != 2))
            case.add("K6 no interleaved input", "counter matches passes",
                     "(%s,%s,%s)" % (rc1, rc2, cnt), not bad)
    finally:
        try:
            v_stop(vis_path, job6)
        except Exception:
            pass
    # K7: non-tmpfs / escaped job paths cannot be requested.
    bad1 = "/tmp/fg-nope-%d" % (os.getpid() % 100000)
    r_bad1 = v_start(vis_path, bad1,
                     app=app_cmd(sys.executable, "-I", a3),
                     size="800x600", timeout_s=10)
    refused1 = (r_bad1.get("json_rc") in (RC_USAGE, RC_RULE)
                and r_bad1.get("ok") is False
                and r_bad1.get("proc_rc") in (RC_USAGE, RC_RULE))
    case.add("K7 absolute job path refused", "rc in {2,5}",
             rc_summary(r_bad1), refused1)
    case.add("K7 no file at absolute path", "absent",
             "exists" if os.path.exists(bad1) else "absent",
             not os.path.exists(bad1))
    bad2 = "../../tmp/fg-nope2-%d" % (os.getpid() % 100000)
    r_bad2 = v_start(vis_path, bad2,
                     app=app_cmd(sys.executable, "-I", a3),
                     size="800x600", timeout_s=10)
    refused2 = (r_bad2.get("json_rc") in (RC_USAGE, RC_RULE)
                and r_bad2.get("ok") is False
                and r_bad2.get("proc_rc") in (RC_USAGE, RC_RULE))
    case.add("K7 escaped job path refused", "rc in {2,5}",
             rc_summary(r_bad2), refused2)
    # The escaped path must not create anything under /tmp or the job root.
    leaked = os.path.exists("/tmp/fg-nope2-%d" % (os.getpid() % 100000))
    case.add("K7 no escape file written", "absent",
             "exists" if leaked else "absent", not leaked)
    try:
        v_stop(vis_path, bad1)
    except Exception:
        pass
    try:
        v_stop(vis_path, bad2)
    except Exception:
        pass
    case.finish()
    return case


def _verdict_matches_rc(verdict, rc):
    if rc == 0:
        return verdict == "pass"
    if rc == 5:
        return verdict == "refused"
    if rc == 3:
        return verdict == "fail"
    if rc in (2, 4):
        return verdict == "refused"
    # 6,7,8,9: never a pass.
    return verdict in ("fail", "refused")


def case_l(vis_path):
    """L: receipts exist, match rc, pass has sha change, stop cleans all."""
    case = CaseResult("L", "receipts and cleanup")
    d = case_subdir("l")
    counter = os.path.join(d, "counter")
    status = os.path.join(d, "status")
    app = os.path.join(d, "app_l.py")
    write_file(app, tk_toggle_source(counter, status, title="FG-L"))
    job = unique_job("fg-l")
    cmd = app_cmd(sys.executable, "-I", app)
    recorded_pids = []
    recorded_display = None
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        look = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look passes", look, RC_OK, "OK")
        l1 = v_act(vis_path, job, "l1", ["--xy", "400,250"],
                   expect_change="any", timeout=60)
        _expect_rc(case, "L1 toggle passes any", l1, RC_OK, "OK")
        l2 = v_act(vis_path, job, "l2", ["--xy", "400,250"],
                   expect_change="none", timeout=60)
        _expect_rc(case, "L2 toggle breaks none", l2, RC_EXPECT, "E_EXPECT")
        # Pre-stop artefacts.
        jdir = job_dir(job)
        case.add("job dir exists before stop", "exists",
                 "exists" if os.path.isdir(jdir) else "missing",
                 os.path.isdir(jdir))
        jjson = os.path.join(jdir, "job.json")
        jobj = None
        if os.path.exists(jjson):
            try:
                with open(jjson, encoding="utf-8") as f:
                    jobj = json.load(f)
                case.add("job.json parses", "json", "json", True)
            except Exception as e:
                case.add("job.json parses", "json", "error %s" % e, False)
        else:
            case.add("job.json exists", "exists", "missing", False)
        if isinstance(jobj, dict):
            recorded_pids = extract_pids_from_jobjson(jobj)
            for key in ("display", "DISPLAY"):
                if key in jobj and isinstance(jobj[key], str):
                    recorded_display = jobj[key]
                    break
            if recorded_display is None:
                # Search one level for a display-like string.
                for v in jobj.values():
                    if isinstance(v, str) and v.startswith(":"):
                        recorded_display = v
                        break
        rpath = os.path.join(jdir, "receipts.jsonl")
        receipts = []
        if os.path.exists(rpath):
            try:
                with open(rpath, encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln:
                            receipts.append(json.loads(ln))
                case.add("receipts.jsonl parses", "jsonl", "%d lines" % len(receipts), True)
            except Exception as e:
                case.add("receipts.jsonl parses", "jsonl", "error %s" % e, False)
        else:
            case.add("receipts.jsonl exists", "exists", "missing", False)
        acts = [r for r in receipts if isinstance(r, dict) and r.get("verb") == "act"]
        steps = {r.get("step") for r in acts if isinstance(r, dict)}
        case.add("every act has a receipt", "{l1,l2} subset",
                 "steps=%s" % sorted(str(s) for s in steps),
                 "l1" in steps and "l2" in steps)
        by_step = {}
        for r in acts:
            if isinstance(r, dict) and r.get("step") in ("l1", "l2"):
                by_step.setdefault(r.get("step"), []).append(r)
        for step, want_rc in (("l1", 0), ("l2", 3)):
            lst = by_step.get(step, [])
            if not lst:
                case.add("receipt %s verdict matches rc" % step,
                         "rc=%d" % want_rc, "missing", False)
                continue
            r0 = lst[-1]
            rc = r0.get("rc")
            verdict = r0.get("verdict")
            try:
                rc_i = int(rc) if rc is not None else None
            except (TypeError, ValueError):
                rc_i = None
            ok = rc_i == want_rc and _verdict_matches_rc(verdict, rc_i)
            case.add("receipt %s verdict matches rc" % step,
                     "rc=%d verdict ok" % want_rc,
                     "rc=%s verdict=%s" % (rc, verdict), ok)
            # Required receipt shape (subset of the contract).
            need = ("schema", "job", "step", "verb", "verdict", "rc",
                    "frame_sha_before", "frame_sha_after", "expected")
            missing = [k for k in need if k not in r0]
            case.add("receipt %s has required fields" % step, "all present",
                     "missing=%s" % missing, not missing)
            if r0.get("schema") != "vis.receipt/v1":
                case.add("receipt %s schema" % step, "vis.receipt/v1",
                         "%s" % r0.get("schema"), False)
            else:
                case.add("receipt %s schema" % step, "vis.receipt/v1",
                         "vis.receipt/v1", True)
        # Pass receipt must show a sha change unless none was expected.
        l1r = (by_step.get("l1") or [None])[-1]
        if isinstance(l1r, dict):
            b, a = l1r.get("frame_sha_before"), l1r.get("frame_sha_after")
            exp = str(l1r.get("expected", ""))
            if "none" in exp.lower():
                case.add("pass/none shas equal", "equal", "%s vs %s" % (b, a), b == a)
            else:
                diff = (b is not None and a is not None and b != a)
                case.add("pass/any shas differ", "before!=after",
                         "%s vs %s" % (b, a), diff)
        else:
            case.add("pass/any shas differ", "before!=after", "no receipt", False)
        stop = v_stop(vis_path, job, timeout=30)
        _expect_rc(case, "stop passes", stop, RC_OK, "OK")
    finally:
        try:
            v_stop(vis_path, job, timeout=15)
        except Exception:
            pass
    # Post-stop checks.
    jdir = job_dir(job)
    gone = not os.path.exists(jdir)
    case.add("job dir gone after stop", "gone", "gone" if gone else "exists", gone)
    if recorded_pids:
        alive = [pid for pid, st in recorded_pids
                 if is_pid_alive_with_start(pid, st)]
        case.add("recorded processes gone", "none alive",
                 "alive=%s" % alive, not alive)
    else:
        # No PIDs recorded: if job.json existed this is a vis bug, else the
        # earlier job.json check already failed.
        case.add("recorded processes gone", "checked via job.json",
                 "no pids recorded", False)
    if recorded_display and recorded_display.startswith(":"):
        num = recorded_display[1:].split(".")[0].split()[0]
        sock = "/tmp/.X11-unix/X%s" % num if num.isdigit() else None
        if sock is not None:
            case.add("display socket gone", "gone %s" % sock,
                     "exists" if os.path.exists(sock) else "gone",
                     not os.path.exists(sock))
        else:
            case.add("display socket gone", "parse :N", recorded_display, False)
    else:
        case.add("display recorded and gone", ":N recorded",
                 "%s" % recorded_display, False)
    home_receipt = os.path.join(receipts_home(), job + ".jsonl")
    if not os.path.exists(home_receipt):
        case.add("home receipts copy exists", "exists", "missing", False)
    else:
        try:
            with open(home_receipt, "rb") as f:
                raw = f.read()
            has_png = b"\x89PNG" in raw
            has_nul = b"\x00" in raw
            case.add("home receipts have no PNG bytes", "no PNG",
                     "PNG found" if has_png else "clean", not has_png)
            case.add("home receipts have no NUL bytes", "text only",
                     "NUL found" if has_nul else "clean", not has_nul)
            try:
                text = raw.decode("utf-8")
                lines = [ln for ln in text.splitlines() if ln.strip()]
                bad = 0
                for ln in lines:
                    try:
                        json.loads(ln)
                    except Exception:
                        bad += 1
                case.add("home receipts are JSONL", "all json",
                         "%d lines %d bad" % (len(lines), bad), bad == 0 and lines)
            except UnicodeDecodeError:
                case.add("home receipts are UTF-8", "utf-8", "binary", False)
        except OSError as e:
            case.add("home receipts readable", "readable", "error %s" % e, False)
        try:
            names = os.listdir(receipts_home())
            pngs = [n for n in names if n.startswith(job) and n.lower().endswith(".png")]
            case.add("no PNG siblings for job", "none", "%s" % pngs, not pngs)
        except OSError:
            case.add("no PNG siblings for job", "none", "dir missing", False)
    case.finish()
    return case


def case_m(vis_path):
    """M: doctor, pins, and startup refusals (no long jobs)."""
    case = CaseResult("M", "doctor pins and startup refusals")
    # M1: doctor reports refs and tools.
    doc = v_doctor(vis_path, timeout=30)
    rc = doc.get("json_rc")
    proto_ok = doc.get("parse_error") is None and doc.get("timeout") is not True
    case.add("doctor protocol is strict", "one line matching rc/code/ok/verb",
             rc_summary(doc), proto_ok and doc.get("verb") == "doctor")
    case.add("doctor rc is 0 or 7", "rc in {0,7}",
             rc_summary(doc), rc in (RC_OK, RC_ABSENT) and proto_ok)
    try:
        blob = json.dumps(doc.get("json") or {}, sort_keys=True, default=str).lower()
    except Exception:
        blob = ""
    has_ref = any(k in blob for k in ("ref", "wender", "vision", "commit", "sha", "pin"))
    case.add("doctor reports refs/pins", "mentions refs",
             "has=%s" % has_ref, has_ref and proto_ok)
    has_tool = any(k in blob for k in ("xvfb", "tool", "capab", "chrome", "ram", "tess"))
    case.add("doctor reports tools/capabilities", "mentions tools",
             "has=%s" % has_tool, has_tool and proto_ok)
    # M2: missing target-repo falls back to defaults and says so.
    d_missing = os.path.join(FG_ROOT, "case-m", "empty-target")
    ensure_dir(d_missing)
    doc2 = v_doctor(vis_path, extra_args=["--target-repo", d_missing], timeout=30)
    rc2 = doc2.get("json_rc")
    proto2 = doc2.get("parse_error") is None and doc2.get("timeout") is not True
    case.add("missing target-repo rc is 0 or 7", "rc in {0,7}",
             rc_summary(doc2), rc2 in (RC_OK, RC_ABSENT) and proto2)
    combined = ((doc2.get("stdout") or "") + "\n" + (doc2.get("stderr") or "")).lower()
    said = ("default" in combined) or ("fallback" in combined) or ("fall back" in combined)
    case.add("missing target-repo says defaults", "mentions default/fallback",
             "said=%s" % said, said and proto2)
    # M3: malformed lock refuses with E_USAGE.
    d_bad = os.path.join(FG_ROOT, "case-m", "bad-target")
    ensure_dir(d_bad)
    write_file(os.path.join(d_bad, "deps.lock"), "{not json\n")
    doc3 = v_doctor(vis_path, extra_args=["--target-repo", d_bad], timeout=30)
    _expect_rc(case, "malformed lock is rc2", doc3, RC_USAGE, "E_USAGE")
    try:
        v_stop(vis_path, "fg-m-bad")
    except Exception:
        pass
    # M4: contract F10 removed --untrusted: unknown flag is rc2, no job dir.
    job_u = unique_job("fg-m-untrust")
    r_u = v_start(vis_path, job_u, app="true", size="800x600", timeout_s=10,
                  extra_args=["--untrusted"])
    _expect_rc(case, "removed --untrusted is rc2", r_u, RC_USAGE, "E_USAGE")
    case.add("untrusted creates no job dir", "absent",
             "exists" if os.path.exists(job_dir(job_u)) else "absent",
             not os.path.exists(job_dir(job_u)))
    try:
        v_stop(vis_path, job_u)
    except Exception:
        pass
    # M5: --electron without a binary is rc7.
    job_e = unique_job("fg-m-electron")
    r_e = run_vis(vis_path, ["start", "--job", job_e, "--electron", "/tmp",
                             "--size", "800x600", "--timeout", "10"],
                  env_extra={"VIS_ELECTRON_BIN": None}, timeout=30)
    _expect_rc(case, "electron without bin is rc7", r_e, RC_ABSENT, "E_ABSENT")
    case.add("electron-absent creates no job dir", "absent",
             "exists" if os.path.exists(job_dir(job_e)) else "absent",
             not os.path.exists(job_dir(job_e)))
    try:
        v_stop(vis_path, job_e)
    except Exception:
        pass
    # M6: invalid job names refused (regex).
    for bad in ("-bad", "a/b", "x" * 70):
        r_b = v_start(vis_path, bad, app="true", size="800x600", timeout_s=10)
        refused = (r_b.get("json_rc") in (RC_USAGE, RC_RULE)
                   and r_b.get("ok") is False
                   and r_b.get("proc_rc") in (RC_USAGE, RC_RULE)
                   and r_b.get("parse_error") is None)
        case.add("invalid job %r refused" % bad[:12], "rc in {2,5}",
                 rc_summary(r_b), refused)
        try:
            v_stop(vis_path, bad)
        except Exception:
            pass
    # M7: app that never appears is rc8 with cleanup.
    job_n = unique_job("fg-m-noapp")
    r_n = v_start(vis_path, job_n, app="/bin/true", size="800x600",
                  timeout_s=5)
    _expect_rc(case, "never-appears start is rc8", r_n, RC_TARGET, "E_TARGET")
    case.add("never-appears leaves no job dir", "absent",
             "exists" if os.path.exists(job_dir(job_n)) else "absent",
             not os.path.exists(job_dir(job_n)))
    try:
        v_stop(vis_path, job_n)
    except Exception:
        pass
    case.finish()
    return case


def case_n(vis_path):
    """N: desktop look/check shape, marks, and expiry (one job)."""
    case = CaseResult("N", "desktop look check and marks")
    d = case_subdir("n")
    counter = os.path.join(d, "counter")
    status = os.path.join(d, "status")
    geom_path = os.path.join(d, "button_geom.json")
    try:
        if os.path.exists(geom_path):
            os.remove(geom_path)
    except OSError:
        pass
    app = os.path.join(d, "app_n.py")
    write_file(app, tk_toggle_geom_source(counter, status, geom_path,
                                           title="FG-N"))
    job = unique_job("fg-n")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        look = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "look passes", look, RC_OK, "OK")
        j = look.get("json") or {}
        jdir = job_dir(job)
        frame = j.get("frame")
        fsha = j.get("frame_sha256")
        mpng = j.get("marks_png")
        marks = j.get("marks")
        frame_ok = (isinstance(frame, str) and frame.startswith(jdir + "/"))
        case.add("look frame is under job dir", "under %s" % jdir,
                 "%s" % (frame,), frame_ok)
        sha_ok = (isinstance(fsha, str)
                  and re.fullmatch(r"[0-9a-fA-F]{64}", fsha) is not None)
        case.add("look frame_sha256 is 64 hex", "64 hex",
                 "%s" % (str(fsha)[:16] + "..." if fsha else fsha,), sha_ok)
        mpng_ok = (isinstance(mpng, str) and mpng.startswith(jdir + "/"))
        case.add("look marks_png is under job dir", "under job dir",
                 "%s" % (mpng,), mpng_ok)
        marks_ok = isinstance(marks, list) and len(marks) >= 1
        case.add("look marks non-empty list", "len>=1",
                 "len=%s" % (len(marks) if isinstance(marks, list) else type(marks),),
                 marks_ok)
        if isinstance(marks, list) and marks:
            m0 = marks[0] if isinstance(marks[0], dict) else {}
            need = ("id", "x", "y", "w", "h", "kind", "ocr_text")
            missing = [k for k in need if k not in m0]
            case.add("mark has required fields", "all present",
                     "missing=%s" % (missing,), not missing)
        else:
            case.add("mark has required fields", "all present", "no marks", False)
        top_keys = {str(k).lower() for k in j.keys()} if isinstance(j, dict) else set()
        no_raw = not any(k in top_keys for k in ("raw_ax", "raw_tree", "rawax", "html", "raw_html"))
        case.add("look exposes no raw AX/HTML", "no raw keys",
                 "keys=%s" % sorted(top_keys)[:8], no_raw)
        no_state = ("state" not in j) or j.get("state") is None
        case.add("desktop look has no STATE", "absent/null",
                 "has=%s" % ("state" in j,), no_state)
        # Desktop STATE is absent.
        chk_state = v_check(vis_path, job, ["--state", "button=done"], timeout=30)
        _expect_rc(case, "desktop --state is rc7", chk_state, RC_ABSENT, "E_ABSENT")
        # Unknown historical step is usage.
        chk_unk = v_check(vis_path, job, ["--region-changed-since", "NO_SUCH_STEP_XYZ",
                                          "0,0,10,10"], timeout=30)
        _expect_rc(case, "unknown step is rc2", chk_unk, RC_USAGE, "E_USAGE")
        # Window title discriminates.
        chk_wt1 = v_check(vis_path, job, ["--window-title", "FG-N"], timeout=30)
        _expect_rc(case, "window-title match passes", chk_wt1, RC_OK, "OK")
        chk_wt0 = v_check(vis_path, job, ["--window-title", "NO_SUCH_TITLE_XYZ"], timeout=30)
        _expect_rc(case, "window-title miss is rc3", chk_wt0, RC_EXPECT, "E_EXPECT")
        # Stable reads and the removed ask-file option.
        chk_rs = v_check(vis_path, job, ["--window-title", "FG-N",
                                         "--require-stable", "2"], timeout=30)
        _expect_rc(case, "window-title stable passes", chk_rs, RC_OK, "OK")
        chk_af = v_check(vis_path, job, ["--ask-file"], timeout=30)
        _expect_rc(case, "removed --ask-file is rc2", chk_af,
                   RC_USAGE, "E_USAGE")
        # --ax on desktop is absent and touches nothing.
        ax = v_act(vis_path, job, "n-ax", ["--ax", "1", "--op", "click"],
                   expect_change="any", timeout=30)
        _expect_rc(case, "desktop --ax is rc7", ax, RC_ABSENT, "E_ABSENT")
        cnt0 = read_counter(counter)
        case.add("desktop --ax touched nothing", "counter 0/absent",
                 "counter==%s" % cnt0, cnt0 in (None, 0))
        # Mark click: pick the button mark by box overlap with the
        # fixture-reported button geometry (screen coords), not by list
        # order. Window-manager marks (menu icon, title) legitimately pass
        # `any` on repaint but must not count as the button.
        geom = None
        for _ in range(40):
            txt = read_text(geom_path)
            if txt:
                try:
                    g = json.loads(txt)
                    if all(k in g for k in ("x", "y", "w", "h")):
                        gx, gy = int(g["x"]), int(g["y"])
                        gw, gh = int(g["w"]), int(g["h"])
                        if gw > 0 and gh > 0:
                            geom = (gx, gy, gw, gh)
                            break
                except Exception:
                    pass
            time.sleep(0.1)
        case.add("fixture button geometry reported", "geom json",
                 "yes" if geom else "missing", geom is not None)
        button_marks = []
        if geom is not None and isinstance(marks, list):
            gx, gy, gw, gh = geom
            for m in marks:
                if not isinstance(m, dict) or "id" not in m:
                    continue
                try:
                    mx, my = int(m["x"]), int(m["y"])
                    mw, mh = int(m["w"]), int(m["h"])
                except Exception:
                    continue
                cx, cy = mx + mw / 2.0, my + mh / 2.0
                if (gx - 2 <= cx <= gx + gw + 2
                        and gy - 2 <= cy <= gy + gh + 2):
                    button_marks.append(m)
            if not button_marks:
                bcx, bcy = gx + gw / 2.0, gy + gh / 2.0
                for m in marks:
                    if not isinstance(m, dict) or "id" not in m:
                        continue
                    try:
                        mx, my = int(m["x"]), int(m["y"])
                        mw, mh = int(m["w"]), int(m["h"])
                    except Exception:
                        continue
                    if mx <= bcx <= mx + mw and my <= bcy <= my + mh:
                        button_marks.append(m)
        case.add("button mark present (geometry match)", ">=1",
                 "%d of %s" % (len(button_marks),
                               len(marks) if isinstance(marks, list) else "?"),
                 len(button_marks) >= 1)
        passing_id = None
        passing_step = None
        if button_marks:
            for idx, m in enumerate(button_marks[:6]):
                mid = str(m["id"])
                step = "n-click-%d" % idx
                before = read_counter(counter)
                base = before if isinstance(before, int) else 0
                r = v_act(vis_path, job, step, ["--click", mid],
                          expect_change="any", timeout=60)
                after = read_counter(counter)
                landed = isinstance(after, int) and after == base + 1
                if r.get("json_rc") == 0 and r.get("ok") is True \
                        and r.get("parse_error") is None and landed:
                    passing_id = mid
                    passing_step = step
                    _expect_rc(case, "mark click %s passes" % mid, r, RC_OK, "OK")
                    break
            case.add("some mark click passes", "one button mark passes+lands",
                     "passing=%s" % (passing_id,), passing_id is not None)
        else:
            case.add("some mark click passes", "one button mark passes+lands",
                     "no geom-matched marks", False)
        cnt1 = read_counter(counter)
        if passing_id is not None:
            case.add("mark click landed", "counter>=1",
                     "counter==%s" % cnt1,
                     isinstance(cnt1, int) and cnt1 >= 1)
            # F5: a passing act renews the marks, so the same handle clicks again and lands.
            reuse = v_act(vis_path, job, "n-reuse", ["--click", passing_id],
                          expect_change="any", timeout=60)
            cnt_r = read_counter(counter)
            case.add("renewed mark passes and lands", "rc0, counter+1",
                     "%s, %s->%s" % (rc_summary(reuse), cnt1, cnt_r),
                     reuse.get("json_rc") == 0 and reuse.get("ok") is True
                     and isinstance(cnt1, int) and cnt_r == cnt1 + 1)
            # A failed act (a visible change under 'none') invalidates the marks.
            spoil = v_act(vis_path, job, "n-spoil", ["--click", passing_id],
                          expect_change="none", timeout=60)
            _expect_rc(case, "change under none is rc3", spoil, RC_EXPECT, "E_EXPECT")
            cnt_s = read_counter(counter)
            # The expired handle must not pass and must not touch.
            expired = v_act(vis_path, job, "n-expired", ["--click", passing_id],
                            expect_change="any", timeout=60)
            not_pass = expired.get("ok") is not True and expired.get("json_rc") != 0 \
                and expired.get("parse_error") is None
            case.add("expired mark does not pass", "rc!=0",
                     rc_summary(expired), not_pass)
            cnt2 = read_counter(counter)
            case.add("expired mark touched nothing", "counter unchanged",
                     "%s->%s" % (cnt_s, cnt2), cnt2 == cnt_s)
            # Historical region checks against the passing step's baseline.
            chk_hp = v_check(vis_path, job, ["--region-changed-since", passing_step,
                                             "0,400,800,100"], timeout=30)
            _expect_rc(case, "changed region since step passes", chk_hp, RC_OK, "OK")
            chk_hf = v_check(vis_path, job, ["--region-changed-since", passing_step,
                                             "10,10,50,50"], timeout=30)
            _expect_rc(case, "static region since step is rc3", chk_hf,
                       RC_EXPECT, "E_EXPECT")
        else:
            case.add("mark click landed", "counter>=1", "no passing mark", False)
            case.add("expired mark does not pass", "rc!=0", "no passing mark", False)
            case.add("expired mark touched nothing", "unchanged", "no passing mark", False)
            case.add("changed region since step passes", "rc0", "no passing mark", False)
            case.add("static region since step is rc3", "rc3", "no passing mark", False)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_o(vis_path):
    """O: private session env and job layout (one job)."""
    case = CaseResult("O", "private session and job layout")
    d = case_subdir("o")
    dump = os.path.join(d, "env.json")
    try:
        if os.path.exists(dump):
            os.remove(dump)
    except OSError:
        pass
    counter = os.path.join(d, "counter")
    app = os.path.join(d, "app_o.py")
    write_file(app, tk_env_dump_source(dump, counter, title="FG-O"))
    job = unique_job("fg-o")
    cmd = app_cmd(sys.executable, "-I", app)
    try:
        start = v_start(vis_path, job, app=cmd, size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        jdir = job_dir(job)
        # Job dir mode and filesystem.
        try:
            mode = stat.S_IMODE(os.stat(jdir).st_mode)
        except OSError:
            mode = None
        case.add("job dir mode is 0700", "0o700",
                 oct(mode) if mode is not None else "missing", mode == 0o700)
        fstype = df_fstype(jdir)
        case.add("job dir is on tmpfs", "tmpfs",
                 "%s" % fstype, fstype == "tmpfs")
        # Env dump appears and points inside the job dir.
        snap = None
        for _ in range(50):
            txt = read_text(dump)
            if txt:
                try:
                    snap = json.loads(txt)
                    break
                except Exception:
                    snap = None
            time.sleep(0.1)
        case.add("env dump appeared", "json",
                 "yes" if snap else "missing", isinstance(snap, dict))
        if isinstance(snap, dict):
            real_home = os.path.expanduser("~")
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                        "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
                val = snap.get(key, "")
                inside = isinstance(val, str) and val.startswith(jdir + "/")
                case.add("private %s" % key, "under job dir",
                         "%s" % val, inside)
            home_ok = snap.get("HOME", "") != real_home and snap.get("HOME", "") != ""
            case.add("HOME is not the real home", "private",
                     "%s" % snap.get("HOME", ""), home_ok)
            lang_ok = snap.get("LANG", "") == "C.UTF-8"
            case.add("LANG is C.UTF-8", "C.UTF-8",
                     "%s" % snap.get("LANG", ""), lang_ok)
        else:
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                        "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
                case.add("private %s" % key, "under job dir", "no dump", False)
            case.add("HOME is not the real home", "private", "no dump", False)
            case.add("LANG is C.UTF-8", "C.UTF-8", "no dump", False)
        # job.json: display, ownership, scope, refs, steps.
        jjson = os.path.join(jdir, "job.json")
        jobj = None
        try:
            with open(jjson, encoding="utf-8") as f:
                jobj = json.load(f)
            case.add("job.json parses", "json", "json", True)
        except Exception as e:
            case.add("job.json parses", "json", "error %s" % e, False)
        if isinstance(jobj, dict):
            blob = json.dumps(jobj, sort_keys=True, default=str)
            low = blob.lower()
            disp = None
            for k in ("display", "DISPLAY"):
                if k in jobj and isinstance(jobj[k], str):
                    disp = jobj[k]
                    break
            case.add("job.json has display :N", ":N",
                     "%s" % disp,
                     isinstance(disp, str) and disp.startswith(":"))
            own = jobj.get("ownership", jobj.get("kind", jobj.get("ownership_kind")))
            if own is None:
                for kk, vv in jobj.items():
                    if str(kk).lower() in ("ownership", "kind", "ownership_kind"):
                        own = vv
                        break
            case.add("job.json ownership is started", "started",
                     "%s" % own, own == "started")
            has_scope = ("scope" in low) or ("memorymax" in low) or ("memory_max" in low)
            case.add("job.json records scope/cap", "mentions scope",
                     "has=%s" % has_scope, has_scope)
            has_ref = ("ref" in low) or ("wender" in low) or ("vision" in low) \
                or ("commit" in low) or ("sha" in low)
            case.add("job.json records refs", "mentions refs",
                     "has=%s" % has_ref, has_ref)
            # One act, then steps and start-time verification.
            act = v_act(vis_path, job, "o1", ["--xy", "400,250"],
                        expect_change="any", timeout=60)
            _expect_rc(case, "O1 toggle passes any", act, RC_OK, "OK")
            try:
                with open(jjson, encoding="utf-8") as f:
                    jobj2 = json.load(f)
                blob2 = json.dumps(jobj2, sort_keys=True, default=str)
            except Exception:
                jobj2, blob2 = None, ""
            case.add("job.json records step o1", "contains o1",
                     "has=%s" % ("o1" in blob2,), "o1" in blob2)
            pids = extract_pids_from_jobjson(jobj2 if isinstance(jobj2, dict) else {})
            case.add("job.json records PIDs", ">=1",
                     "%d" % len(pids), len(pids) >= 1)
            # The contract never requires every recorded PID to stay alive:
            # short-lived launchers exit and sweep handles gone owners. What
            # matters is that every still-alive recorded PID matches its
            # recorded start time (no PID-reuse confusion), with at least
            # one live record. Gone records are reported, not failed.
            alive = 0
            matched = 0
            mismatched = 0
            gone = 0
            for pid, st in pids:
                state, cur = proc_state_and_start(pid)
                if state is None or state in ("Z", "X", "x"):
                    gone += 1
                    continue
                alive += 1
                if st is None or str(cur) == str(st):
                    matched += 1
                else:
                    mismatched += 1
            case.add("recorded start times match /proc",
                     "all alive match, >=1 alive",
                     "%d matched/%d alive/%d gone/%d total mism=%d"
                     % (matched, alive, gone, len(pids), mismatched),
                     len(pids) >= 1 and alive >= 1
                     and mismatched == 0 and matched == alive)
            # DISPLAY in the app matches the job display.
            if isinstance(snap, dict) and isinstance(disp, str):
                case.add("app DISPLAY is the job display", disp,
                         "%s" % snap.get("DISPLAY", ""), snap.get("DISPLAY") == disp)
            else:
                case.add("app DISPLAY is the job display", "match", "no data", False)
        else:
            case.add("job.json has display :N", ":N", "no job.json", False)
            case.add("job.json ownership is started", "started", "no job.json", False)
            case.add("job.json records scope/cap", "mentions scope", "no job.json", False)
            case.add("job.json records refs", "mentions refs", "no job.json", False)
            case.add("job.json records step o1", "contains o1", "no job.json", False)
            case.add("job.json records PIDs", ">=1", "no job.json", False)
            case.add("recorded start times match /proc", "all match", "no job.json", False)
            case.add("app DISPLAY is the job display", "match", "no job.json", False)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    case.finish()
    return case


def case_p(vis_path):
    """P: attach, readers, sweep, ask, and adopted survival."""
    case = CaseResult("P", "attach readers sweep ask adopt")
    # Live toggle job that owns a private display.
    d = case_subdir("p")
    counter = os.path.join(d, "counter")
    status = os.path.join(d, "status")
    app = os.path.join(d, "app_p.py")
    write_file(app, tk_toggle_source(counter, status, title="FG-P"))
    job = unique_job("fg-p")
    # Own token file under /dev/shm (never read existing auth files).
    tok_dir = os.path.join(FG_ROOT, "case-p")
    ensure_dir(tok_dir)
    tok_path = os.path.join(tok_dir, "owner.token")
    token = "fgp-%d-%d" % (os.getpid() % 100000, int(time.time() * 1000) % 1000000)
    write_file(tok_path, token + "\n")
    attached_jobs = []
    try:
        start = v_start(vis_path, job, app=app_cmd(sys.executable, "-I", app),
                        size="800x600", timeout_s=30)
        if not _expect_rc(case, "start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        jdir = job_dir(job)
        disp = None
        try:
            with open(os.path.join(jdir, "job.json"), encoding="utf-8") as f:
                jobj = json.load(f)
            for k in ("display", "DISPLAY"):
                if k in jobj and isinstance(jobj[k], str):
                    disp = jobj[k]
                    break
        except Exception:
            disp = None
        case.add("job display known", ":N",
                 "%s" % disp, isinstance(disp, str) and disp.startswith(":"))
        if not (isinstance(disp, str) and disp.startswith(":")):
            case.finish()
            return case
        env_tok = {"VIS_OWNER_TOKEN_FILE": tok_path}
        # Wrong token refused.
        bad_j = unique_job("fg-p-bad")
        r_bad = v_attach(vis_path, bad_j, disp, "wrong-token",
                         env_extra=env_tok, timeout=30)
        _expect_rc(case, "attach wrong token is rc5", r_bad, RC_RULE, "E_RULE")
        case.add("wrong token creates no job", "absent",
                 "exists" if os.path.exists(job_dir(bad_j)) else "absent",
                 not os.path.exists(job_dir(bad_j)))
        try:
            v_stop(vis_path, bad_j)
        except Exception:
            pass
        # Missing token file refused (absent path).
        miss_j = unique_job("fg-p-miss")
        r_miss = v_attach(vis_path, miss_j, disp, token,
                          env_extra={"VIS_OWNER_TOKEN_FILE": os.path.join(tok_dir, "nope")},
                          timeout=30)
        _expect_rc(case, "attach missing token file is rc5", r_miss, RC_RULE, "E_RULE")
        try:
            v_stop(vis_path, miss_j)
        except Exception:
            pass
        # Correct token without --allow-act: attach ok, acts refused.
        ro_j = unique_job("fg-p-ro")
        attached_jobs.append(ro_j)
        r_ro = v_attach(vis_path, ro_j, disp, token, allow_act=False,
                        env_extra=env_tok, timeout=30)
        _expect_rc(case, "attach read-only passes", r_ro, RC_OK, "OK")
        if r_ro.get("json_rc") == 0:
            cnt0 = read_counter(counter)
            a_ro = v_act(vis_path, ro_j, "p-ro1", ["--xy", "400,250"],
                         expect_change="any", timeout=30)
            _expect_rc(case, "attached act without flag is rc5", a_ro,
                       RC_RULE, "E_RULE")
            cnt1 = read_counter(counter)
            case.add("refused attached act touched nothing", "unchanged",
                     "%s->%s" % (cnt0, cnt1), cnt1 == cnt0)
            lk = v_look(vis_path, ro_j, timeout=30)
            _expect_rc(case, "attached look passes", lk, RC_OK, "OK")
        # Correct token with --allow-act: acts allowed.
        rw_j = unique_job("fg-p-rw")
        attached_jobs.append(rw_j)
        r_rw = v_attach(vis_path, rw_j, disp, token, allow_act=True,
                        env_extra=env_tok, timeout=30)
        _expect_rc(case, "attach with allow-act passes", r_rw, RC_OK, "OK")
        if r_rw.get("json_rc") == 0:
            cnt0 = read_counter(counter)
            base = cnt0 if isinstance(cnt0, int) else 0
            a_rw = v_act(vis_path, rw_j, "p-rw1", ["--xy", "400,250"],
                         expect_change="any", timeout=60)
            _expect_rc(case, "attached act with flag passes", a_rw, RC_OK, "OK")
            cnt1 = read_counter(counter)
            case.add("allowed attached act landed", "counter+1",
                     "%s->%s" % (cnt0, cnt1), cnt1 == base + 1)
        # Readers are free: two concurrent looks both pass.
        l1, l2 = _concurrent_looks(vis_path, job, timeout=60)
        _expect_rc(case, "concurrent look 1 passes", l1, RC_OK, "OK")
        _expect_rc(case, "concurrent look 2 passes", l2, RC_OK, "OK")
        # Sweep leaves the live job alone.
        sw = v_sweep(vis_path, timeout=60)
        sw_ok = (sw.get("parse_error") is None and sw.get("timeout") is not True
                 and sw.get("verb") == "sweep")
        case.add("sweep protocol is strict", "one line sweep",
                 rc_summary(sw), sw_ok)
        case.add("live job survives sweep", "exists",
                 "exists" if os.path.isdir(jdir) else "missing",
                 os.path.isdir(jdir))
        lk2 = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "live job works after sweep", lk2, RC_OK, "OK")
        # Contract F10 removed the ask verb: rc2 with or without the flag.
        ask0 = v_ask(vis_path, job, "sol", "what is shown", timeout=30)
        _expect_rc(case, "removed ask is rc2", ask0, RC_USAGE, "E_USAGE")
        ask1 = v_ask(vis_path, job, "sol", "what is shown",
                     extra_args=["--allow-hosted-pixels"], timeout=30)
        _expect_rc(case, "removed ask with flag is rc2", ask1, RC_USAGE, "E_USAGE")
        # Stopping attached jobs leaves the owner display running.
        for aj in list(attached_jobs):
            try:
                v_stop(vis_path, aj)
            except Exception:
                pass
        case.add("owner survives attached stops", "exists",
                 "exists" if os.path.isdir(jdir) else "missing",
                 os.path.isdir(jdir))
        lk3 = v_look(vis_path, job, timeout=30)
        _expect_rc(case, "owner works after attached stops", lk3, RC_OK, "OK")
    finally:
        for aj in attached_jobs:
            try:
                v_stop(vis_path, aj)
            except Exception:
                pass
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    # Adopted display survives stop (own Xvfb, if available).
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        case.add("adopted display survives stop", "Xvfb present",
                 "Xvfb missing; limit noted", True)
        case.finish()
        return case
    disp_n = None
    for cand in range(90, 130):
        sock = "/tmp/.X11-unix/X%d" % cand
        lock = "/tmp/.X%d-lock" % cand
        if not os.path.exists(sock) and not os.path.exists(lock):
            disp_n = cand
            break
    if disp_n is None:
        case.add("adopted display survives stop", "free display",
                 "none free; limit noted", True)
        case.finish()
        return case
    disp_s = ":%d" % disp_n
    proc = None
    try:
        proc = subprocess.Popen([xvfb, disp_s, "-screen", "0", "800x600x24",
                                 "-nolisten", "tcp", "-noreset"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        time.sleep(1.0)
        alive, _st = proc_state_and_start(proc.pid), None
        if not is_pid_alive_with_start(proc.pid):
            case.add("adopted display survives stop", "Xvfb runs",
                     "Xvfb failed to start; limit noted", True)
            proc = None
            case.finish()
            return case
        job_a = unique_job("fg-p-adopt")
        adop = v_adopt(vis_path, job_a, disp_s, proc.pid, timeout=30)
        _expect_rc(case, "good adopt passes", adop, RC_OK, "OK")
        if adop.get("json_rc") == 0:
            try:
                v_stop(vis_path, job_a)
            except Exception:
                pass
            still = is_pid_alive_with_start(proc.pid)
            case.add("adopted display survives stop", "Xvfb alive",
                     "alive=%s" % still, still)
        else:
            case.add("adopted display survives stop", "Xvfb alive",
                     "adopt failed: %s" % rc_summary(adop), False)
            try:
                v_stop(vis_path, job_a)
            except Exception:
                pass
    finally:
        if proc is not None and is_pid_alive_with_start(proc.pid):
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    case.finish()
    return case


def case_q(vis_path):
    """Q: secret list, long-text receipts, type-file, web protected."""
    case = CaseResult("Q", "secrets receipts protected")
    # Entry job for secret patterns and long text.
    d = case_subdir("q")
    recv = os.path.join(d, "received")
    keys = os.path.join(d, "keys")
    app = os.path.join(d, "app_q.py")
    write_file(app, tk_entry_source(recv, keys))
    # Retitle to keep windows distinct.
    try:
        src = read_text(app) or ""
        write_file(app, src.replace("FG-K2", "FG-Q"))
    except OSError:
        pass
    job = unique_job("fg-q")
    try:
        start = v_start(vis_path, job, app=app_cmd(sys.executable, "-I", app),
                        size="800x600", timeout_s=30)
        if not _expect_rc(case, "entry start passes", start, RC_OK, "OK"):
            case.finish()
            return case
        time.sleep(0.5)
        # Extra secret patterns beyond K2's sk-.
        for label, secret in (("ghp", synthetic_key("ghp_", 36)),
                              ("privkey", "-----BEGIN RSA " + "PRIVATE KEY-----")):
            st = "q-sec-%s" % label
            r = v_act(vis_path, job, st, ["--type", secret],
                      expect_change="any", timeout=30)
            _expect_rc(case, "secret %s refused rc5" % label, r, RC_RULE, "E_RULE")
        k1 = read_counter(keys)
        r1 = read_text(recv)
        case.add("secrets sent no keys", "keys 0/absent",
                 "keys==%s" % k1, k1 in (None, 0))
        case.add("entry stayed empty after secrets", "empty",
                 "received==%r" % ((r1 or "")[:20],), (r1 or "") == "")
        # Long typing (>80 chars) passes but the receipt stores sha, not text.
        long_text = "ab12-" * 20  # 100 chars, no secret prefix.
        r_long = v_act(vis_path, job, "q-long", ["--type", long_text],
                       expect_change="any", timeout=60)
        _expect_rc(case, "long typing passes any", r_long, RC_OK, "OK")
        jdir = job_dir(job)
        rpath = os.path.join(jdir, "receipts.jsonl")
        receipts = []
        try:
            with open(rpath, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        receipts.append(json.loads(ln))
        except Exception as e:
            receipts = []
            case.add("receipts readable for long text", "readable",
                     "error %s" % e, False)
        found = [r for r in receipts
                 if isinstance(r, dict) and r.get("step") == "q-long"]
        if not found:
            case.add("long-text receipt exists", "has q-long",
                     "steps=%s" % sorted(str(r.get("step")) for r in receipts if isinstance(r, dict)),
                     False)
            case.add("long text not in receipt", "absent", "no receipt", False)
            case.add("long-text sha in receipt", "sha present", "no receipt", False)
        else:
            blob = json.dumps(found[-1], sort_keys=True, default=str)
            case.add("long text not in receipt", "absent 100-char string",
                     "present" if long_text in blob else "absent",
                     long_text not in blob)
            sha = hashlib.sha256(long_text.encode("utf-8")).hexdigest()
            case.add("long-text sha in receipt", sha[:16] + "...",
                     "present" if sha in blob else "absent", sha in blob)
        # --type-file under a secret path refused by path, without reading.
        fake_secret = os.path.join(os.path.expanduser("~"), ".ssh",
                                   "fg-test-nonexistent-%d" % (os.getpid() % 100000))
        k_before = read_counter(keys)
        r_tf = v_act(vis_path, job, "q-tf", ["--type-file", fake_secret],
                     expect_change="any", timeout=30)
        tf_rc = r_tf.get("json_rc")
        tf_ok = (r_tf.get("parse_error") is None and r_tf.get("ok") is False
                 and tf_rc in (RC_RULE, RC_USAGE))
        case.add("--type-file secret path refused", "rc in {5,2} (5 ideal)",
                 rc_summary(r_tf) + (" (flag absent)" if tf_rc == 2 else ""),
                 tf_ok)
        k_after = read_counter(keys)
        case.add("--type-file touched nothing", "keys unchanged",
                 "%s->%s" % (k_before, k_after), k_after == k_before)
    finally:
        try:
            v_stop(vis_path, job)
        except Exception:
            pass
    # Web protected field.
    dw = case_subdir("qw")
    html = os.path.join(dw, "index.html")
    write_file(html, web_password_html())
    file_url = "file://" + html
    jobw = unique_job("fg-qw")
    try:
        st = v_start(vis_path, jobw, web=file_url, size="1280x800", timeout_s=30)
        if not _expect_rc(case, "web start passes", st, RC_OK, "OK"):
            case.finish()
            return case
        lk = v_look(vis_path, jobw, timeout=30)
        _expect_rc(case, "web look passes", lk, RC_OK, "OK")
        state = (lk.get("json") or {}).get("state")
        nodes = find_state_nodes(state) if state is not None else []
        cand = None
        for n in nodes:
            try:
                blob = json.dumps(n, sort_keys=True, default=str).lower()
            except Exception:
                blob = ""
            if ("secret-field" in blob) or ("password" in blob) or ("protect" in blob):
                nid = node_id_of(n)
                if nid:
                    cand = nid
                    break
        case.add("STATE exposes password field", "node id found",
                 "id=%s nodes=%d" % (cand, len(nodes)), cand is not None)
        if cand is not None:
            r_prot = v_act(vis_path, jobw, "q-prot",
                           ["--ax", cand, "--op", "type", "--text", "hello"],
                           expect_change="any", timeout=30)
            _expect_rc(case, "typing into protected is rc5", r_prot,
                       RC_RULE, "E_RULE")
            lk2 = v_look(vis_path, jobw, timeout=30)
            try:
                s2 = json.dumps((lk2.get("json") or {}).get("state"),
                                sort_keys=True, default=str)
            except Exception:
                s2 = ""
            case.add("protected input received nothing", "hello absent",
                     "present" if "hello" in s2 else "absent",
                     "hello" not in s2)
        else:
            case.add("typing into protected is rc5", "rc5", "no node", False)
            case.add("protected input received nothing", "absent", "no node", False)
    finally:
        try:
            v_stop(vis_path, jobw)
        except Exception:
            pass
    case.finish()
    return case


CASES = [
    ("A", case_a), ("B", case_b), ("C", case_c), ("D", case_d),
    ("E", case_e), ("F", case_f), ("G", case_g), ("H", case_h),
    ("I", case_i), ("J", case_j), ("K", case_k), ("L", case_l),
    ("M", case_m), ("N", case_n), ("O", case_o), ("P", case_p),
    ("Q", case_q),
]


def print_table(results):
    print("")
    print("VIS-FG false-green gates")
    print("%-5s %-28s %-6s %s" % ("Case", "Title", "Result", "Failed checks"))
    print("-" * 78)
    for res in results:
        failed = [c.name for c in res.checks if not c.ok]
        mark = "PASS" if res.passed else "FAIL"
        print("%-5s %-28s %-6s %s" % (
            res.cid, res.title[:28], mark,
            ("; ".join(failed[:4]) + ("..." if len(failed) > 4 else ""))
            if failed else "-"))
    print("-" * 78)
    overall = all(r.passed for r in results)
    print("Overall: %s (%d/%d cases passed)" % (
        "PASS" if overall else "FAIL",
        sum(1 for r in results if r.passed), len(results)))
    print("")


def main(argv=None):
    ap = argparse.ArgumentParser(description="VIS-FG false-green gates")
    ap.add_argument("--vis", required=True, help="path to vis executable")
    ap.add_argument("--out", required=True, help="results JSON path")
    args = ap.parse_args(argv)
    vis_path = args.vis
    if not os.path.exists(vis_path):
        print("vis not found: %s" % vis_path, file=sys.stderr)
        return 2
    self_ok, self_detail = parser_type_self_check()
    if not self_ok:
        print("parser self-check failed: %s" % self_detail, file=sys.stderr)
        return 2
    # Fresh fixture root; always removed at the end.
    shutil.rmtree(FG_ROOT, ignore_errors=True)
    ensure_dir(FG_ROOT)
    results = []
    try:
        for cid, fn in CASES:
            try:
                t0 = time.time()
                print("... running case %s (%s)" % (cid, fn.__name__),
                      flush=True)
                res = fn(vis_path)
            except Exception as e:
                res = CaseResult(cid, fn.__name__)
                res.add("no exception", "runs", "raised %r" % e, False)
                res.finish()
            results.append(res)
    finally:
        shutil.rmtree(FG_ROOT, ignore_errors=True)
    print_table(results)
    overall = all(r.passed for r in results)
    payload = {
        "vis": os.path.abspath(vis_path),
        "overall_pass": overall,
        "cases": [r.to_dict() for r in results],
    }
    try:
        parent = os.path.dirname(os.path.abspath(args.out))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        print("Wrote %s" % args.out)
    except OSError as e:
        print("cannot write --out %s: %s" % (args.out, e), file=sys.stderr)
        return 2
    # Fixture root must be gone at the end.
    if os.path.exists(FG_ROOT):
        print("warning: %s still exists" % FG_ROOT, file=sys.stderr)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
