#!/usr/bin/env python3
"""Dry-run validator for a ViewPoint multi-agent capsule spec (vp-capsule/v1).

Track P reference code (claude-fable-5-2). Pure stdlib, no process execution, no
network, no docker calls: it reads one JSON file and prints findings.

Usage:  python3 -I validate.py SPEC.json [--json] [--skip RULE_ID ...]
Exit:   0 = no findings, 1 = findings, 2 = spec unreadable / wrong shape.
"""
import json
import posixpath
import re
import sys

SCHEMA = "vp-capsule/v1"
NAME_RE = re.compile(r"^vpref-[a-z0-9][a-z0-9-]{0,48}$")
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,23}-agent$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
SUBPATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
ENVKEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEALTH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:=@-]+$")

ROLES = ("program", "broker", "agent")
VOLUME_KINDS = ("x11", "rt", "ckpt", "scratch")
X_DST = "/tmp/.X11-unix"
# Host bind sources are allowed only under these prefixes (COMMON.md: RT under
# /dev/shm/vqa-cu/ref*/). Everything else is refused, which covers display :1
# (/tmp/.X11-unix), hybrid/, swarm/, the docker socket, /run/user, $HOME.
BIND_ROOT_RE = re.compile(r"^/dev/shm/vqa-cu/ref[A-Za-z0-9_-]*(/[A-Za-z0-9._-]+)*$")
# What an agent sidecar may see of the shared runtime root RT.
AGENT_RT_RO = ("frames", "events.jsonl", "cursor.jsonl", "claims.json")
DISPLAY_ENV = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
               "DBUS_SESSION_BUS_ADDRESS", "DOCKER_HOST")
SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSW|PWD|CREDENTIAL|AUTH|COOKIE|SESSION|BEARER|PAT$|_PAT_"
                           r"|OPENAI|ANTHROPIC|GEMINI|GOOGLE_API|GITHUB|GITLAB|AWS_|AZURE|HF_|HUGGING)", re.I)
# Values are scanned too: a secret under an innocent key name is still a secret (xr C5).
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|hf_[A-Za-z0-9]{20,}|sk_live_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[abpsr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.)")
# Every string that ends up inside a `--mount`/`--tmpfs` option value is allow-listed
# per field: no `,` `=` `:` quotes or whitespace can reach docker's option parser
# (xr C1, C2, C11). render.py re-checks this structurally.
VOLKEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
DST_RE = re.compile(r"^(/[A-Za-z0-9._-]+)+$")
# Networks: 'none' or the single declared capsule egress network. `container:<x>`,
# `host`, `ns:<path>` share a network namespace and with it the X server's abstract
# socket (network_namespaces(7)); they are refused by name shape (xr C3).
NETWORK_RE = re.compile(r"^vpref-[a-z0-9][a-z0-9-]{0,48}$")
# Fail-closed schema (xr C6): a key the validator does not know is a shape error,
# so cgroup_parent, security_opt, network_mode, group_add... cannot ride along.
SPEC_KEYS = frozenset(("v", "name", "program", "volumes", "networks", "budget", "containers", "rewind"))
CONTAINER_KEYS = frozenset((
    "name", "role", "agent_id", "image", "user", "input", "read_only", "cap_drop", "cap_add", "privileged",
    "no_new_privileges", "devices", "env", "network", "pid_mode", "ipc_mode", "userns_mode", "uts_mode",
    "volumes_from", "mounts", "tmpfs", "shm_mib", "memory_mib", "memory_swap_mib", "pids_limit", "depends_on",
    "health", "stop", "restart"))
VOLUME_KEYS = frozenset(("kind", "backing", "size_mib"))
MOUNT_KEYS = frozenset(("volume", "dst", "mode", "subpath"))
TMPFS_KEYS = frozenset(("dst", "size_mib"))
HEALTH_KINDS = {
    # A process-exists probe proves nothing about the Blender main thread: the
    # program is healthy only if a nonce job makes the inbox -> outbox round trip.
    "program": ("bridge_roundtrip",),
    "broker": ("events_heartbeat",),
    "agent": ("heartbeat_file",),
}
STOP_SIGNALS = ("SIGTERM", "SIGINT")
RESTART_POLICIES = ("no", "on-failure")
REWIND_OK = ("A2", "A3")
REWIND_REFUSED = {
    "P": "CRIU is absent on this host and needs root to install (rw-state.md section 3)",
    "C1": "docker checkpoint needs the experimental daemon flag and CRIU: root (rw-state.md rung C1)",
    "C2": "the program rootfs is read-only and all writable state is tmpfs: docker commit captures nothing",
    "V": "KVM guest snapshot has no graphical profile in the admitted backend (rw-state.md rung V)",
    "A1": "the Blender undo stack has no checkable state identity (rw-state.md rung A1)",
}
POLICY = {
    "capsule_memory_max_mib": 8192,   # [TARGET] same ceiling as the wave's MemoryMax=8G
    "agent_memory_max_mib": 2048,     # [TARGET]
    "agent_pids_max": 256,            # [TARGET]
    "pids_max": 1024,                 # [TARGET]
    "grace_max_s": 120,               # [TARGET]
    "restart_max": 5,                 # [TARGET]
}


class ShapeError(Exception):
    pass


def _need(cond, msg):
    if not cond:
        raise ShapeError(msg)


def check_shape(spec):
    """Structural check. Raises ShapeError; rules below may then index freely."""
    _need(isinstance(spec, dict), "spec must be an object")
    _need(spec.get("v") == SCHEMA, "v must be %r" % SCHEMA)
    _need(isinstance(spec.get("name"), str), "name must be a string")
    _need(set(spec) <= SPEC_KEYS, "unknown spec keys %s" % sorted(set(spec) - SPEC_KEYS))
    vols = spec.get("volumes")
    _need(isinstance(vols, dict) and vols, "volumes must be a non-empty object")
    for vn, v in vols.items():
        _need(isinstance(v, dict), "volume %s must be an object" % vn)
        _need(set(v) <= VOLUME_KEYS, "volume %s: unknown keys %s" % (vn, sorted(set(v) - VOLUME_KEYS)))
        _need(v.get("kind") in VOLUME_KINDS, "volume %s: kind must be one of %s" % (vn, VOLUME_KINDS))
        b = v.get("backing")
        _need(isinstance(b, dict) and b.get("type") in ("volume", "bind"),
              "volume %s: backing.type must be volume|bind" % vn)
        if b["type"] == "bind":
            _need(isinstance(b.get("src"), str), "volume %s: bind backing needs src" % vn)
    _need(isinstance(spec.get("networks", []), list)
          and all(isinstance(x, str) for x in spec.get("networks", [])), "networks must be a list of strings")
    _need(isinstance(spec.get("budget"), dict), "budget must be an object")
    cs = spec.get("containers")
    _need(isinstance(cs, list) and cs, "containers must be a non-empty list")
    for i, c in enumerate(cs):
        w = "containers[%d]" % i
        _need(isinstance(c, dict), w + " must be an object")
        _need(isinstance(c.get("name"), str), w + ".name must be a string")
        _need(c.get("role") in ROLES, w + ".role must be one of %s" % (ROLES,))
        _need(set(c) <= CONTAINER_KEYS, "%s: unknown keys %s (unknown keys are refused, not ignored)"
              % (w, sorted(set(c) - CONTAINER_KEYS)))
        _need(isinstance(c.get("network", ""), str), w + ".network must be a string")
        for k, t in (("mounts", list), ("tmpfs", list), ("env", dict), ("depends_on", list),
                     ("cap_drop", list), ("cap_add", list), ("devices", list)):
            _need(isinstance(c.get(k, t()), t), "%s.%s must be %s" % (w, k, t.__name__))
        for m in c.get("mounts", []):
            _need(isinstance(m, dict) and isinstance(m.get("volume"), str)
                  and isinstance(m.get("dst"), str) and m.get("mode") in ("ro", "rw")
                  and isinstance(m.get("subpath", ""), str) and set(m) <= MOUNT_KEYS,
                  w + ".mounts entries need volume, dst, mode ro|rw, optional subpath")
        for t in c.get("tmpfs", []):
            _need(isinstance(t, dict) and isinstance(t.get("dst"), str) and set(t) <= TMPFS_KEYS,
                  w + ".tmpfs entries need dst (and only dst, size_mib)")
        for k in ("health", "stop", "restart"):
            _need(isinstance(c.get(k, {}), dict), "%s.%s must be an object" % (w, k))
    _need(isinstance(spec.get("rewind"), dict), "rewind must be an object")


def _is_int(x, lo=None, hi=None):
    if isinstance(x, bool) or not isinstance(x, int):
        return False
    return (lo is None or x >= lo) and (hi is None or x <= hi)


def _by_role(spec, role):
    return [c for c in spec["containers"] if c["role"] == role]


def _under(path, root):
    p = posixpath.normpath(path)
    return p == root or p.startswith(root + "/")


def _covers(subpath, target):
    """True if a mount of `subpath` exposes `target` (both relative to the volume)."""
    return subpath == "" or subpath == target or target.startswith(subpath + "/")


# ---------------------------------------------------------------- rules
# Each rule: fn(spec) -> iterable of (where, message).

def r_names(spec):
    if not NAME_RE.match(spec["name"]):
        yield "name", "capsule name must match %s" % NAME_RE.pattern
    seen = set()
    for c in spec["containers"]:
        if not NAME_RE.match(c["name"]):
            yield c["name"], "container name must carry the vpref- prefix (%s)" % NAME_RE.pattern
        if c["name"] in seen:
            yield c["name"], "duplicate container name"
        seen.add(c["name"])


def r_topology(spec):
    for role in ("program", "broker"):
        n = len(_by_role(spec, role))
        if n != 1:
            yield "containers", "exactly one %s container required, found %d" % (role, n)
    agents = _by_role(spec, "agent")
    if not agents:
        yield "containers", "at least one agent sidecar required"
    ids = [a.get("agent_id") for a in agents]
    for a in agents:
        if not (isinstance(a.get("agent_id"), str) and AGENT_ID_RE.match(a["agent_id"])):
            yield a["name"], "agent_id must match %s" % AGENT_ID_RE.pattern
    if len(set(map(str, ids))) != len(ids):
        yield "containers", "agent_id values must be unique"
    for c in spec["containers"]:
        if c["role"] != "agent" and "agent_id" in c:
            yield c["name"], "only agent sidecars carry an agent_id"


def r_start_order(spec):
    names = {c["name"]: c for c in spec["containers"]}
    for c in spec["containers"]:
        deps = c.get("depends_on", [])
        for d in deps:
            if d not in names:
                yield c["name"], "depends_on unknown container %r" % d
            elif names[d]["role"] == "agent":
                yield c["name"], "nothing may depend on an agent sidecar (%s): an agent crash must not block the capsule" % d
        roles = {names[d]["role"] for d in deps if d in names}
        if c["role"] == "broker" and "program" not in roles:
            yield c["name"], "broker must depend_on the program container"
        if c["role"] == "agent" and "broker" not in roles:
            yield c["name"], "agent must depend_on the broker"
        if c["role"] == "program" and deps:
            yield c["name"], "program must not depend on other containers"


def r_image(spec):
    for c in spec["containers"]:
        if not (isinstance(c.get("image"), str) and IMAGE_RE.match(c["image"])):
            yield c["name"], "image must be pinned by digest (repo[:tag]@sha256:<64 hex>)"


def r_hardening(spec):
    for c in spec["containers"]:
        n = c["name"]
        user = str(c.get("user", ""))
        m = re.match(r"^(\d+):(\d+)$", user)
        if not m or int(m.group(1)) == 0 or int(m.group(2)) == 0:
            yield n, "user must be numeric non-root uid:gid"
        if c.get("read_only") is not True:
            yield n, "read_only root filesystem required"
        if "ALL" not in c.get("cap_drop", []):
            yield n, "cap_drop must include ALL"
        if c.get("cap_add"):
            yield n, "cap_add must be empty"
        if c.get("privileged", False) is not False:
            yield n, "privileged is forbidden (this also rules out nested docker)"
        if c.get("no_new_privileges") is not True:
            yield n, "no_new_privileges required"
        if c.get("devices"):
            yield n, "devices must be empty"
        for k, val in c.get("env", {}).items():
            if not ENVKEY_RE.match(k):
                yield n, "bad env key %r" % k
            elif SECRET_ENV_RE.search(k):
                yield n, "env %s looks like a credential: secrets do not go into a capsule spec" % k
            elif k == "DOCKER_HOST":
                yield n, "env DOCKER_HOST is forbidden: no container talks to a docker daemon"
            if not isinstance(val, str):
                yield n, "env %s value must be a string" % k
            elif SECRET_VALUE_RE.search(val):
                yield n, "env %s value looks like a credential (value not shown)" % k


def r_namespaces(spec):
    declared = set(spec.get("networks", []))
    for net in spec.get("networks", []):
        if not NETWORK_RE.match(net):
            yield "networks", ("%r cannot be declared as a capsule network: only a vpref-* name "
                               "(never host, bridge, default, container:<x>, ns:<path>)" % net)
    if len(declared) > 1:
        yield "networks", "at most one capsule egress network may be declared (got %d)" % len(declared)
    for c in spec["containers"]:
        n = c["name"]
        net = c.get("network", "")
        if c["role"] in ("program", "broker"):
            if net != "none":
                yield n, "%s network must be 'none' (got %r)" % (c["role"], net)
        elif net != "none" and (net not in declared or not NETWORK_RE.match(net)):
            # host or container:<x> would share the network namespace, and with it
            # the X server's abstract socket @/tmp/.X11-unix/X<n>.
            yield n, "agent network must be 'none' or a declared egress network (got %r)" % net
        for k in ("pid_mode", "ipc_mode", "userns_mode", "uts_mode"):
            if c.get(k, "") not in ("", "private"):
                yield n, "%s must be private (got %r)" % (k, c.get(k))
        if c.get("volumes_from"):
            yield n, "volumes_from is forbidden: mounts are declared one by one"


def r_volumes(spec):
    kinds = [v["kind"] for v in spec["volumes"].values()]
    for k in ("x11", "rt", "ckpt"):
        if kinds.count(k) != 1:
            yield "volumes", "exactly one volume of kind %s required, found %d" % (k, kinds.count(k))
    binds = sorted((v["backing"]["src"], vn) for vn, v in spec["volumes"].items()
                   if v["backing"]["type"] == "bind")
    for i, (a, an) in enumerate(binds):
        for b_, bn in binds[i + 1:]:
            # Two bind volumes that are equal or nested alias each other: a scratch
            # volume over RT would hand an agent events.jsonl rw (xr C4).
            if a == b_ or _under(b_, a) or _under(a, b_):
                yield "volumes." + bn, "bind source %r overlaps volume %s (%r): bind sources must be disjoint" % (b_, an, a)
    for vn, v in spec["volumes"].items():
        if not VOLKEY_RE.match(vn):
            yield "volumes", "bad volume key %r (allowed: [a-z][a-z0-9_-]*)" % vn
        b = v["backing"]
        if b["type"] == "bind":
            src = b["src"]
            if posixpath.normpath(src) != src or not BIND_ROOT_RE.match(src):
                yield "volumes." + vn, ("bind source %r is outside /dev/shm/vqa-cu/ref*/ "
                                        "(display :1, hybrid/, swarm/, sockets and home are never mounted)" % src)
            if v["kind"] == "x11":
                yield "volumes." + vn, "the X socket directory must be a capsule-private volume, never a host bind"
        if not _is_int(v.get("size_mib"), 1):
            yield "volumes." + vn, "size_mib must be a positive integer"
    for c in spec["containers"]:
        dsts = set()
        for m in c.get("mounts", []):
            if m["volume"] not in spec["volumes"]:
                yield c["name"], "mount of undeclared volume %r" % m["volume"]
            sp = m.get("subpath", "")
            if sp and not SUBPATH_RE.match(sp) or ".." in sp.split("/"):
                yield c["name"], "bad subpath %r" % sp
            d = m["dst"]
            if not DST_RE.match(d) or posixpath.normpath(d) != d:
                yield c["name"], "bad mount destination %r" % d
            if d in dsts:
                yield c["name"], "duplicate mount destination %r" % d
            dsts.add(d)
        for t in c.get("tmpfs", []):
            d = t["dst"]
            if not DST_RE.match(d) or posixpath.normpath(d) != d:
                yield c["name"], "bad tmpfs destination %r" % d


def _mounts(spec, c):
    for m in c.get("mounts", []):
        v = spec["volumes"].get(m["volume"])
        if v is not None:
            yield m, v["kind"], m.get("subpath", "")


def r_x_socket(spec):
    """Only the program (X server) and the broker (the one input holder) see X."""
    for c in spec["containers"]:
        n, role = c["name"], c["role"]
        x = [m for m, kind, _ in _mounts(spec, c) if kind == "x11"]
        if role == "agent":
            if x:
                yield n, "agent sidecar mounts the X socket volume: only the broker holds input"
            for m in c.get("mounts", []):
                if _under(m["dst"], X_DST):
                    yield n, "agent sidecar has a mount at %s" % m["dst"]
            for t in c.get("tmpfs", []):
                if _under(t["dst"], X_DST):
                    yield n, "agent sidecar has a tmpfs at %s" % t["dst"]
            for k in c.get("env", {}):
                if k in DISPLAY_ENV:
                    yield n, "agent sidecar env carries %s" % k
            if c.get("input", False) is not False:
                yield n, "agent sidecar declares input: only the broker holds input"
        else:
            if len(x) != 1 or x[0]["dst"] != X_DST or x[0].get("subpath", ""):
                yield n, "%s must mount the x11 volume once, whole, at %s" % (role, X_DST)
            elif role == "program" and x[0]["mode"] != "rw":
                yield n, "program must mount the x11 volume rw (it creates the socket)"
            want = role == "broker"
            if c.get("input", False) is not want:
                yield n, "input must be %s for role %s" % (str(want).lower(), role)


def r_rt_access(spec):
    """RT least privilege: events.jsonl has one writer; agents write only their intents."""
    for c in spec["containers"]:
        n, role = c["name"], c["role"]
        rt = [(m, sp) for m, kind, sp in _mounts(spec, c) if kind == "rt"]
        if role == "broker":
            if not any(sp == "" and m["mode"] == "rw" for m, sp in rt):
                yield n, "broker must mount the whole rt volume rw (single writer of events.jsonl)"
            continue
        for m, sp in rt:
            if m["mode"] == "rw":
                for f in ("events.jsonl", "cursor.jsonl", "claims.json", "frames"):
                    if _covers(sp, f):
                        yield n, "rw mount of rt/%s exposes %s: only the broker writes it" % (sp, f)
        if role == "program":
            for m, sp in rt:
                if sp not in ("inbox", "outbox"):
                    yield n, "program may mount only rt/inbox and rt/outbox (got rt/%s)" % sp
            if sorted(sp for _, sp in rt) != ["inbox", "outbox"]:
                yield n, "program needs rt/inbox and rt/outbox for the nonce bridge"
        if role == "agent":
            own = "intents/%s" % c.get("agent_id")
            # Sealed-per-writer bid channel (xr A3): an agent writes RT/bids/<own id>/ only,
            # so it can neither read 0 for every bid nor overwrite a rival's bid.
            own_bids = "bids/%s" % c.get("agent_id")
            have_own = False
            for m, sp in rt:
                if sp == own:
                    have_own = True
                elif sp == own_bids:
                    if m["mode"] != "rw":
                        yield n, "agent mount of rt/%s must be rw (its own bid directory)" % sp
                elif sp in AGENT_RT_RO:
                    if m["mode"] != "ro":
                        yield n, "agent mount of rt/%s must be ro" % sp
                else:
                    yield n, ("agent may mount only rt/%s, rt/%s (rw) and %s (ro); got rt/%s"
                              % (own, own_bids, ", ".join(AGENT_RT_RO), sp))
            if own_bids not in [sp for _, sp in rt]:
                yield n, "agent needs its own bid directory rt/%s (without it every bid reads 0)" % own_bids
            if not have_own:
                yield n, "agent needs its own intent directory rt/%s" % own


def _tmpfs_mib(c):
    total = 0
    for t in c.get("tmpfs", []):
        total += t["size_mib"] if _is_int(t.get("size_mib"), 1) else 0
    return total + (c["shm_mib"] if _is_int(c.get("shm_mib"), 0) else 0)


def r_limits(spec):
    total = 0
    for c in spec["containers"]:
        n = c["name"]
        mem = c.get("memory_mib")
        if not _is_int(mem, 64):
            yield n, "memory_mib must be an integer >= 64 (no unlimited containers)"
            continue
        total += mem
        if c.get("memory_swap_mib") != mem:
            yield n, "memory_swap_mib must equal memory_mib (no swap; unset means twice the memory)"
        if not _is_int(c.get("pids_limit"), 1, POLICY["pids_max"]):
            yield n, "pids_limit must be an integer in 1..%d" % POLICY["pids_max"]
        if not _is_int(c.get("shm_mib"), 0):
            yield n, "shm_mib must be declared (docker default is implicit)"
        for t in c.get("tmpfs", []):
            if not _is_int(t.get("size_mib"), 1):
                yield n, "tmpfs %s needs size_mib (an unsized tmpfs defaults to half of host RAM)" % t["dst"]
        if _tmpfs_mib(c) > mem:
            yield n, ("tmpfs+shm (%d MiB) exceed memory_mib (%d): tmpfs pages are charged to the "
                      "container cgroup, filling them would OOM it" % (_tmpfs_mib(c), mem))
        if c["role"] == "agent":
            if mem > POLICY["agent_memory_max_mib"]:
                yield n, "agent memory_mib above policy %d" % POLICY["agent_memory_max_mib"]
            if _is_int(c.get("pids_limit")) and c["pids_limit"] > POLICY["agent_pids_max"]:
                yield n, "agent pids_limit above policy %d" % POLICY["agent_pids_max"]
    budget = spec["budget"].get("memory_mib")
    if not _is_int(budget, 1, POLICY["capsule_memory_max_mib"]):
        yield "budget", "budget.memory_mib must be an integer in 1..%d" % POLICY["capsule_memory_max_mib"]
    else:
        # Volumes are tmpfs-like state shared by containers: count them once.
        vol = sum(v["size_mib"] for v in spec["volumes"].values() if _is_int(v.get("size_mib"), 1))
        if total + vol > budget:
            yield "budget", ("sum of container memory (%d MiB) + volumes (%d MiB) exceeds "
                             "budget.memory_mib (%d)" % (total, vol, budget))


def r_health(spec):
    for c in spec["containers"]:
        n, h = c["name"], c.get("health", {})
        if h.get("kind") not in HEALTH_KINDS[c["role"]]:
            yield n, "health.kind for role %s must be one of %s" % (c["role"], HEALTH_KINDS[c["role"]])
        cmd = h.get("cmd")
        if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd)):
            yield n, "health.cmd must be a non-empty exec array (no shell string)"
        elif not all(HEALTH_TOKEN_RE.match(x) for x in cmd):
            yield n, "health.cmd tokens must match %s (docker runs the joined string in a shell)" % HEALTH_TOKEN_RE.pattern
        iv, to = h.get("interval_s"), h.get("timeout_s")
        if not _is_int(iv, 1, 300):
            yield n, "health.interval_s must be 1..300"
        if not _is_int(to, 1) or (_is_int(iv) and to >= iv):
            yield n, "health.timeout_s must be >= 1 and < interval_s"
        if not _is_int(h.get("retries"), 1, 10):
            yield n, "health.retries must be 1..10"
        if not _is_int(h.get("start_period_s"), 0, 600):
            yield n, "health.start_period_s must be 0..600"


def r_restart(spec):
    budget_s = spec["rewind"].get("ckpt_save_budget_s")
    for c in spec["containers"]:
        n, s, r = c["name"], c.get("stop", {}), c.get("restart", {})
        if s.get("signal") not in STOP_SIGNALS:
            yield n, "stop.signal must be one of %s" % (STOP_SIGNALS,)
        g = s.get("grace_s")
        if not _is_int(g, 1, POLICY["grace_max_s"]):
            yield n, "stop.grace_s must be 1..%d" % POLICY["grace_max_s"]
        elif c["role"] == "program" and _is_int(budget_s, 1) and g < budget_s:
            yield n, ("program stop.grace_s (%d) is shorter than rewind.ckpt_save_budget_s (%d): "
                      "SIGKILL would land in the middle of the final checkpoint" % (g, budget_s))
        pol = r.get("policy")
        if pol not in RESTART_POLICIES:
            yield n, "restart.policy must be one of %s (always/unless-stopped hide crash loops)" % (RESTART_POLICIES,)
        elif pol == "on-failure" and not _is_int(r.get("max"), 1, POLICY["restart_max"]):
            yield n, "restart.max must be 1..%d with on-failure" % POLICY["restart_max"]
        if c["role"] == "program" and pol != "no":
            # A restarted program is a new X window and a new scene incarnation; only the
            # broker may bring it back, after bumping scene_epoch and restoring a checkpoint.
            yield n, "program restart.policy must be 'no': the broker restarts it through rewind rung A3"
    if spec["rewind"].get("on_program_restart") != "bump_scene_epoch_and_restore":
        yield "rewind", "rewind.on_program_restart must be 'bump_scene_epoch_and_restore'"


def r_rewind(spec):
    rw = spec["rewind"]
    rung = rw.get("rung")
    if rung in REWIND_REFUSED:
        yield "rewind", "rung %s refused: %s" % (rung, REWIND_REFUSED[rung])
    elif rung not in REWIND_OK:
        yield "rewind", "rewind.rung must be one of %s" % (REWIND_OK,)
    ck = [(vn, v) for vn, v in spec["volumes"].items() if v["kind"] == "ckpt"]
    if rw.get("ckpt_volume") not in [vn for vn, _ in ck]:
        yield "rewind", "rewind.ckpt_volume must name the volume of kind ckpt"
    ring, per = rw.get("ring"), rw.get("max_blend_mib")
    if not _is_int(ring, 2, 64):
        yield "rewind", "rewind.ring must be 2..64 (one point is not a ring)"
    if not _is_int(per, 1):
        yield "rewind", "rewind.max_blend_mib must be a positive integer"
    if not _is_int(rw.get("ckpt_save_budget_s"), 1):
        yield "rewind", "rewind.ckpt_save_budget_s must be a positive integer"
    if _is_int(ring, 2) and _is_int(per, 1) and ck and _is_int(ck[0][1].get("size_mib"), 1):
        # +1: a new point is written before the oldest is evicted.
        if (ring + 1) * per > ck[0][1]["size_mib"]:
            yield "rewind", ("(ring+1) x max_blend_mib = %d MiB does not fit the ckpt volume (%d MiB)"
                             % ((ring + 1) * per, ck[0][1]["size_mib"]))
    if sorted(rw.get("state_identity", [])) != ["image_digest", "ring_manifest_sha256", "scene_epoch"]:
        yield "rewind", "rewind.state_identity must be [image_digest, scene_epoch, ring_manifest_sha256]"
    for c in spec["containers"]:
        cm = [m for m, kind, _ in _mounts(spec, c) if kind == "ckpt"]
        if c["role"] == "agent" and cm:
            yield c["name"], "agent sidecar mounts the checkpoint volume: agents never touch checkpoints"
        if c["role"] == "program" and not any(m["mode"] == "rw" and not m.get("subpath") for m in cm):
            yield c["name"], "program must mount the ckpt volume rw (Blender writes the .blend points)"
        if c["role"] == "broker" and not cm:
            yield c["name"], "broker must mount the ckpt volume (it hashes points and owns the ring manifest)"
        # A checkpoint on a container-private tmpfs dies with the container, which
        # removes the A3 fallback (kill and relaunch on the last good point).
        for t in c.get("tmpfs", []):
            for m in cm:
                if _under(m["dst"], t["dst"]) or _under(t["dst"], m["dst"]):
                    yield c["name"], "tmpfs %s overlaps the ckpt mount %s" % (t["dst"], m["dst"])


RULES = [
    ("S-01", "names carry the vpref- prefix and are unique", r_names),
    ("T-01", "one program, one broker, >=1 agent with unique agent_id", r_topology),
    ("T-02", "start order program -> broker -> agents; nothing depends on an agent", r_start_order),
    ("I-01", "images pinned by digest", r_image),
    ("H-01", "non-root, read-only, cap-drop ALL, no privileged, no devices, no secrets in env", r_hardening),
    ("N-01", "private namespaces; agents never share the X server's network namespace", r_namespaces),
    ("V-01", "volumes declared, bind sources only under /dev/shm/vqa-cu/ref*/", r_volumes),
    ("X-01", "X socket only in program and broker; only the broker holds input", r_x_socket),
    ("A-01", "RT least privilege: broker is the single events writer; agents write only own intents", r_rt_access),
    ("L-01", "memory, swap, pids, shm and tmpfs limits explicit and within budget", r_limits),
    ("HC-01", "role-appropriate health checks", r_health),
    ("R-01", "graceful stop, bounded restart, program restart only through the broker", r_restart),
    ("RW-01", "rewind rung adoptable today; ckpt volume outlives the program and fits the ring", r_rewind),
]


def validate(spec, skip=()):
    """Return a list of findings {rule, where, msg}; [] means the spec passes."""
    try:
        check_shape(spec)
    except ShapeError as e:
        return [{"rule": "S-00", "where": "spec", "msg": str(e)}]
    out = []
    for rid, _, fn in RULES:
        if rid in skip:
            continue
        for where, msg in fn(spec):
            out.append({"rule": rid, "where": where, "msg": msg})
    return out


def main(argv):
    args, skip, as_json = [], [], False
    it = iter(argv[1:])
    for a in it:
        if a == "--json":
            as_json = True
        elif a == "--skip":
            skip.append(next(it, ""))
        else:
            args.append(a)
    if len(args) != 1:
        sys.stderr.write(__doc__)
        return 2
    try:
        with open(args[0], encoding="utf-8") as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("unreadable spec: %s\n" % e)
        return 2
    findings = validate(spec, skip)
    if as_json:
        print(json.dumps({"schema": SCHEMA, "ok": not findings, "skipped": skip,
                          "rules": [r[0] for r in RULES], "findings": findings}, indent=1))
    else:
        for f in findings:
            print("FAIL %-6s %s: %s" % (f["rule"], f["where"], f["msg"]))
        print("%s  %d finding(s), %d rule(s), spec %s"
              % ("RED" if findings else "GREEN", len(findings), len(RULES) - len(skip), args[0]))
    if findings and findings[0]["rule"] == "S-00":
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
