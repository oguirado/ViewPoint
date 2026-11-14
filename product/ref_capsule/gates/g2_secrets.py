"""G2 secrets in logs: proof/*.jsonl record every command and every tool output, and they get published.

Desired: no credential value in a proof log, and no recorded tool call that reads a credential file
         or dumps the environment.
Observed: every line of the given logs (text level, format independent) and their tool calls.
A match is NEVER printed: findings carry rule id, file:line:col, length and a salted-free sha256[:12]
of the match so two reports can be compared without disclosing the value.
"""
import hashlib
import os
import re

from common import analyze, finding, iter_tool_calls, result, strip_heredocs

VALUE_RULES = [
    ("GOOGLE_API_KEY", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("GOOGLE_OAUTH_TOKEN", re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("GOOGLE_REFRESH_TOKEN", re.compile(r"(?<![0-9A-Za-z/])1//[0-9A-Za-z_\-]{30,}")),
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[0-9A-Za-z_\-]{20,}")),
    ("OPENAI_KEY", re.compile(r"(?<![0-9A-Za-z_\-])sk-(?:proj-)?[0-9A-Za-z]{32,}")),
    ("GITHUB_TOKEN", re.compile(r"(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{40,})")),
    ("SLACK_TOKEN", re.compile(r"xox[abprs]-[0-9A-Za-z\-]{10,}")),
    ("AWS_ACCESS_KEY", re.compile(r"(?<![0-9A-Z])AKIA[0-9A-Z]{16}(?![0-9A-Z])")),
    ("PRIVATE_KEY_BLOCK", re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[0-9A-Za-z_\-]{10,}\.eyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}")),
    ("BEARER_HEADER", re.compile(r"(?i)authorization[\"'\\]*\s*[:=]\s*[\"'\\]*bearer\s+[0-9A-Za-z._\-]{20,}")),
]
# lower-confidence: reported as warnings, they do not turn the gate red on their own
WARN_RULES = [
    ("GENERIC_ASSIGNMENT", re.compile(
        r"(?i)\b(?:api[_-]?key|client[_-]?secret|password|passwd|access[_-]?token|refresh[_-]?token)"
        r"[\"'\\]*\s*[:=]\s*[\"'\\]*(?=[0-9A-Za-z_\-/+]*\d)(?=[0-9A-Za-z_\-/+]*[A-Za-z])[0-9A-Za-z_\-/+]{24,}")),
    ("RUN_NONCE_LITERAL", re.compile(r"VP_SWARM_NONCE=[\"'\\]*[0-9A-Za-z]{8,}")),
]
_CRED_PATH = re.compile(
    r"(?:(?:^|[/\s~\"'=])\.gemini(?:/(?!skills(?:/|\b))|[\s\"']|$)"
    r"|oauth_creds|\.credentials\.json|application_default_credentials|/\.ssh/id_|(?:^|[/\s])\.netrc\b"
    r"|(?:^|[/\s\"'])\.env(?:\.[a-z]+)?(?=[\s\"']|$)|\.pem\b|(?:^|/)credentials(?:\.json)?(?=[\s\"']|$)"
    r"|/\.config/gcloud/|/\.aws/credentials|\.claude/\.credentials)")
_READERS = {"cat", "head", "tail", "less", "more", "grep", "rg", "sed", "awk", "cp", "jq", "strings", "xxd",
            "base64", "python3", "python", "source", ".", "tar", "zip", "scp", "curl", "od", "nl", "tac", "bat"}
_ENV_DUMP = re.compile(r"(?:^|[;&|(]|\bthen\b|\bdo\b)[ \t]*(env|printenv|export -p|declare -x|set)[ \t]*"
                       r"(\|[ \t]*[^|;&\n]*|>>?[^;&|\n]*)?(?=$|[;&\n)|])", re.M)
_FILTERS = {"grep", "egrep", "fgrep", "rg"}
_READ_TOOLS = {"Read", "read_file", "view_file", "NotebookRead"}


def _redact(m):
    v = m.group(0)
    return "len=%d sha256[:12]=%s" % (len(v), hashlib.sha256(v.encode()).hexdigest()[:12])


def scan_text(path):
    out, n = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            n += 1
            for rules, sev in ((VALUE_RULES, "red"), (WARN_RULES, "warn")):
                for rule, rx in rules:
                    for m in rx.finditer(line):
                        out.append(finding(rule, "%s:%d:%d" % (path, ln, m.start() + 1), _redact(m), sev))
    return out, n


def check_command(cmd):
    out = []
    for m in _ENV_DUMP.finditer(strip_heredocs(cmd)):
        sink = (m.group(2) or "").lstrip("|").split()
        if sink and sink[0] in ("wc",) or (sink and sink[0] in _FILTERS and any(
                a.startswith("-") and not a.startswith("--") and "c" in a for a in sink[1:])):
            continue  # only a count reaches the log
        if sink and sink[0] in _FILTERS:
            out.append(("ENV_FILTERED", "`%s | %s ...` prints the matching part of the environment" % (m.group(1), sink[0]),
                        "warn"))
        else:
            out.append(("ENV_DUMP", "`%s` writes the whole environment into the log" % m.group(1), "red"))
    for c in analyze(cmd):
        argv = c["argv"]
        if c["head"] in _READERS and _CRED_PATH.search(" ".join(argv[1:])):
            out.append(("CREDENTIAL_FILE_READ", "`%s ...` names a credential path" % c["head"], "red"))
    return out


def scan_calls(path):
    out = []
    for ln, name, args in iter_tool_calls(path):
        if name in _READ_TOOLS:
            fp = str(args.get("file_path") or args.get("path") or args.get("Path") or args.get("AbsolutePath") or "")
            if _CRED_PATH.search(fp):
                out.append(finding("CREDENTIAL_FILE_READ", "%s:%d" % (path, ln), "%s of a credential path" % name))
        for k in ("command", "cmd", "CommandLine"):
            if isinstance(args.get(k), str):
                for rule, detail, sev in check_command(args[k]):
                    out.append(finding(rule, "%s:%d" % (path, ln), detail, sev))
    return out


def run(logs=()):
    findings, n, seen = [], 0, 0
    for p in logs:
        if not os.path.isfile(p):
            findings.append(finding("LOG_MISSING", p, "declared proof log does not exist", "unobserved"))
            continue
        seen += 1
        f, k = scan_text(p)
        findings += f + scan_calls(p)
        n += k
    return result("G2_secrets_in_logs",
                  {"credential_values": 0, "credential_file_reads": 0, "env_dumps": 0},
                  {"logs": seen, "lines": n}, findings, n)
