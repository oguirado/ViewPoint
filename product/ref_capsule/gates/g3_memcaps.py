"""G3 memory caps: heavy work runs under a memory cap that REALLY applies.

Desired: MemoryMax <= 8G and MemorySwapMax = 0 for tests, Blender, ffmpeg and docker.
Observed, three independent sources (declared is not effective: KU-0007):
  (a) declared   - recorded commands in proof logs / *.sh: is the heavy command inside
                   `systemd-run --user --scope -p MemoryMax=.. -p MemorySwapMax=0`?
                   `docker run` additionally needs its own --memory and --pids-limit, because the
                   container is a child of the docker daemon, not of the calling scope.
                   `vision start` needs --mem.
  (b) effective  - for a live PID: min(memory.max) over the cgroup-v2 ancestors of /proc/<pid>/cgroup
                   and memory.swap.max of the leaf, read from the kernel, not from the command line.
"""
import os
import re

from common import analyze, existing_logs, finding, iter_commands, result, source_files, _mem_bytes, WAIVER

CAP_BYTES = 8 * 2**30
_TEST_FILE = re.compile(r"(?:^|/)test_[\w\-]*\.py$|_test\.py$")
_PROBE = {"--version", "-v", "-version", "-h", "--help", "-formats", "-encoders", "-codecs", "-hide_banner"}
_DOCKER_HEAVY = {"run", "create", "build", "compose", "start", "buildx"}


def heavy_kind(c):
    head, argv = c["head"], c["argv"]
    rest = argv[1:]
    if head == "ffmpeg":
        return head if "-i" in rest else None  # no input: -version / -filters / -h probes
    if head == "blender":
        if rest and all(a in _PROBE for a in rest):
            return None
        return head
    if head == "pytest":
        return "tests"
    if re.match(r"^python[\d.]*$", head):
        for i, a in enumerate(rest):
            if a == "-m" and i + 1 < len(rest):
                return "tests" if rest[i + 1] in ("unittest", "pytest") else None
            if a == "-c":
                return None
            if not a.startswith("-"):
                if os.path.basename(a) == "vision" and "start" in rest[i + 1:i + 2]:
                    return "vision start"
                return "tests" if _TEST_FILE.search(a) else None
        return None
    if head == "docker":
        sub = next((a for a in rest if not a.startswith("-")), "")
        return "docker " + sub if sub in _DOCKER_HEAVY else None
    if head == "vision" and "start" in rest[:3]:
        return "vision start"
    return None


def _has_opt(argv, *names):
    return any(a == n or a.startswith(n + "=") for a in argv for n in names)


def check_command(cmd):
    out = []
    for c in analyze(cmd):
        kind = heavy_kind(c)
        if not kind:
            continue
        raw = c["raw"][:110]
        if kind == "vision start":
            if not _has_opt(c["argv"], "--mem"):
                out.append(("VISION_NO_MEM", "`%s` has no --mem" % raw))
            continue
        if kind in ("docker run", "docker create"):
            if not _has_opt(c["argv"], "--memory", "-m"):
                out.append(("DOCKER_NO_MEMORY", "`%s`: container is not in the caller's scope; needs --memory" % raw))
            if not _has_opt(c["argv"], "--pids-limit"):
                out.append(("DOCKER_NO_PIDS_LIMIT", "`%s` has no --pids-limit" % raw))
        cap = c["cap"]
        if cap is None:
            out.append(("UNCAPPED", "%s outside systemd-run: `%s`" % (kind, raw)))
            continue
        mx = _mem_bytes(cap.get("MemoryMax", ""))
        if mx is None:
            out.append(("NO_MEMORY_MAX", "%s under systemd-run without MemoryMax: `%s`" % (kind, raw)))
        elif mx > CAP_BYTES:
            out.append(("CAP_TOO_HIGH", "%s MemoryMax=%s > 8G: `%s`" % (kind, cap.get("MemoryMax"), raw)))
        if str(cap.get("MemorySwapMax", "")).strip() != "0":
            out.append(("SWAP_ALLOWED", "%s without MemorySwapMax=0: `%s`" % (kind, raw)))
    return out


def scan_logs(paths):
    findings, n = [], 0
    for p in paths:
        for ln, cmd in iter_commands(p):
            n += 1
            for rule, detail in check_command(cmd):
                findings.append(finding(rule, "%s:%d" % (p, ln), detail))
    return findings, n


def scan_scripts(roots):
    findings, n = [], 0
    for path in source_files(roots, exts=(".sh",)):
        n += 1
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        if WAIVER in src:
            src = "\n".join(l for l in src.split("\n") if WAIVER not in l)
        for rule, detail in check_command(src):
            findings.append(finding(rule, path, detail))
    return findings, n


def effective_limits(pid, cgroot="/sys/fs/cgroup", procroot="/proc"):
    """Kernel view: (effective memory.max in bytes or None, leaf memory.swap.max, cgroup path)."""
    with open(os.path.join(procroot, str(pid), "cgroup")) as f:
        rel = next((l.strip().split("::", 1)[1] for l in f if l.startswith("0::")), None)
    if rel is None:
        raise OSError("no cgroup v2 entry for pid %s" % pid)
    parts = [p for p in rel.split("/") if p]
    eff, swap = None, None
    for i in range(1, len(parts) + 1):
        d = os.path.join(cgroot, *parts[:i])
        try:
            with open(os.path.join(d, "memory.max")) as f:
                v = f.read().strip()
        except OSError:
            continue
        if v != "max":
            eff = int(v) if eff is None else min(eff, int(v))
        if i == len(parts):
            try:
                with open(os.path.join(d, "memory.swap.max")) as f:
                    swap = f.read().strip()
            except OSError:
                swap = None
    return eff, swap, rel


def check_pid(pid, cap_bytes=CAP_BYTES, cgroot="/sys/fs/cgroup", procroot="/proc"):
    try:
        eff, swap, rel = effective_limits(pid, cgroot, procroot)
    except (OSError, ValueError) as e:
        return [finding("PID_UNOBSERVED", "pid %s" % pid, str(e)[:120], "unobserved")], {}
    obs = {"pid": pid, "cgroup": rel, "effective_memory_max": eff, "memory_swap_max": swap}
    out = []
    if eff is None:
        out.append(finding("EFFECTIVE_UNCAPPED", "pid %s" % pid, "no finite memory.max on any ancestor of %s" % rel))
    elif eff > cap_bytes:
        out.append(finding("EFFECTIVE_CAP_TOO_HIGH", "pid %s" % pid, "effective memory.max %d > %d (%s)"
                           % (eff, cap_bytes, rel)))
    if swap != "0":
        out.append(finding("EFFECTIVE_SWAP_ALLOWED", "pid %s" % pid, "memory.swap.max=%s (%s)" % (swap, rel)))
    return out, obs


def run(logs=(), scripts=(), pids=(), cgroot="/sys/fs/cgroup", procroot="/proc"):
    files, missing = existing_logs(logs)
    findings, n = scan_logs(files)
    findings += missing
    f, k = scan_scripts(list(scripts))
    findings += f
    n += k
    observed = {"logs": len(logs), "script_roots": len(scripts), "pids": []}
    for pid in pids:
        f, obs = check_pid(pid, cgroot=cgroot, procroot=procroot)
        findings += f
        observed["pids"].append(obs)
        n += 1
    return result("G3_memory_caps", {"MemoryMax_bytes_max": CAP_BYTES, "MemorySwapMax": "0",
                                     "docker": ["--memory", "--pids-limit"], "vision_start": ["--mem"]},
                  observed, findings, n)
