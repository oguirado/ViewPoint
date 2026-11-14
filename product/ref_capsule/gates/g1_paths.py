"""G1 forbidden paths: display :1, the hybrid and swarm runtimes, and the Gemini-only swarm/ tree.

Desired: no Track P command, source default or commit touches them.
Observed: (a) shell commands and Write/Edit targets recorded in proof stream logs,
          (b) non-docstring string constants of *.py and command lines of *.sh,
          (c) git: files under swarm/ changed on the branch or dirty in the worktree.
"""
import os
import re
import subprocess

from common import (analyze, existing_logs, finding, iter_tool_calls, py_string_constants, result, source_files, strip_heredocs,
                    WAIVER)

_SHM = "/dev/shm/vqa-cu/"
RT_FORBIDDEN = (_SHM + "hybrid", _SHM + "swarm")
_RT_RE = re.compile("(?:" + "|".join(re.escape(p) for p in RT_FORBIDDEN) + r")(?![A-Za-z0-9_\-])")
# X display 1 used as a display: DISPLAY=:1, --display :1, x11grab -i :1, record.sh :1 (":10", ":12" are fine)
_D1 = r"[\"']?:1(?:\.\d+)?(?![\d.])"
_DISPLAY_RE = re.compile(r"(?:\bDISPLAY=|--?display[= ]\s*|\s-i\s+|record\.sh\s+)" + _D1)
_DISPLAY_CONST = re.compile(r"^:1(?:\.\d+)?$")
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write_file", "replace", "edit_file", "create_file"}
_SWARM_DIR = re.compile(r"(?:^|/)viewpoint-hackathon/swarm/|(?:^|/)vp-wt-[^/]+/swarm/|^swarm/")
_REDIRECT_SWARM = re.compile(r">>?\s*[\"']?(?:\S*/)?swarm/(?!proof/|briefs/|run/)\S")


def check_command(cmd):
    """Return [(rule, detail)] for one shell command string."""
    out = []
    body = strip_heredocs(cmd)
    for c in analyze(cmd):
        raw = c["raw"]
        m = _RT_RE.search(raw)
        if m:
            out.append(("FORBIDDEN_RT", "%s in `%s`" % (m.group(0), raw[:120])))
        m = _DISPLAY_RE.search(" " + raw)
        if m:
            out.append(("DISPLAY_1", "display :1 in `%s`" % raw[:120]))
    if _REDIRECT_SWARM.search(body):
        out.append(("SWARM_TREE_WRITE", "redirect into swarm/ in `%s`" % body[:120].replace("\n", " ")))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def scan_logs(paths):
    findings, n = [], 0
    for p in paths:
        for ln, name, args in iter_tool_calls(p):
            n += 1
            if name in _WRITE_TOOLS:
                fp = str(args.get("file_path") or args.get("path") or args.get("Path") or args.get("TargetFile") or "")
                if _SWARM_DIR.search(fp):
                    findings.append(finding("SWARM_TREE_WRITE", "%s:%d" % (p, ln), "%s %s" % (name, fp)))
                if _RT_RE.search(fp):
                    findings.append(finding("FORBIDDEN_RT", "%s:%d" % (p, ln), "%s %s" % (name, fp)))
            for k in ("command", "cmd", "CommandLine"):
                if isinstance(args.get(k), str):
                    for rule, detail in check_command(args[k]):
                        findings.append(finding(rule, "%s:%d" % (p, ln), detail))
    return findings, n


def scan_sources(roots):
    findings, n = [], 0
    for path in source_files(roots):
        n += 1
        if path.endswith(".py"):
            for ln, val in py_string_constants(path):
                if _RT_RE.search(val):
                    findings.append(finding("FORBIDDEN_RT", "%s:%d" % (path, ln), "string constant names %s"
                                            % _RT_RE.search(val).group(0)))
                if _DISPLAY_CONST.match(val.strip()) or _DISPLAY_RE.search(" " + val):
                    findings.append(finding("DISPLAY_1", "%s:%d" % (path, ln), "string constant selects display :1"))
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


def scan_git(worktree, base=None):
    """Observed git state: tracked changes under swarm/ versus `base`, plus dirty/untracked swarm/ files."""
    findings = []

    def git(*a):
        return subprocess.run(("git", "-C", worktree) + a, capture_output=True, text=True, timeout=30)

    r = git("rev-parse", "--is-inside-work-tree")
    if r.returncode != 0:
        return [finding("GIT_UNOBSERVED", worktree, "not a git worktree", "unobserved")], 0
    if base:
        r = git("diff", "--name-only", "%s...HEAD" % base, "--", "swarm/")
        if r.returncode != 0:
            findings.append(finding("GIT_UNOBSERVED", worktree, "cannot diff against %s: %s"
                                    % (base, r.stderr.strip()[:120]), "unobserved"))
        for name in filter(None, r.stdout.split("\n")):
            findings.append(finding("SWARM_TREE_COMMIT", "%s:%s" % (worktree, name),
                                    "branch changes a Gemini-only file versus %s" % base))
    r = git("status", "--porcelain", "--", "swarm/")
    for line in filter(None, r.stdout.split("\n")):
        findings.append(finding("SWARM_TREE_DIRTY", "%s:%s" % (worktree, line[3:]), "uncommitted change under swarm/"))
    return findings, 1


def run(logs=(), sources=(), worktrees=(), base=None):
    files, missing = existing_logs(logs)
    findings, n = list(missing), 0
    for fn, arg in ((scan_logs, files), (scan_sources, list(sources))):
        f, k = fn(arg)
        findings += f
        n += k
    for wt in worktrees:
        f, k = scan_git(wt, base)
        findings += f
        n += k
    return result("G1_forbidden_paths",
                  {"never_touch": ["X display 1", RT_FORBIDDEN[0] + "/", RT_FORBIDDEN[1] + "/",
                                   "repo swarm/ (Gemini-only)"]},
                  {"logs": len(logs), "source_roots": len(sources), "worktrees": len(worktrees)}, findings, n)
