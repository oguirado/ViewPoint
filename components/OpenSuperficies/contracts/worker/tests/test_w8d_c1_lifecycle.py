"""C1 executable design contract; stdlib only, no capsule or host operations.

All numeric literals in models and inline fixtures are [TARGET] protocol/test
values, not runtime measurements. Existing W8 constants are [CITED] below.
Source commit: c8cb7dc8c0946be08edcb2a419629b6debd33c96.
Sources: docs/design/w8d/W8_AMENDMENTS.md:22-27;
docs/design/W8_WORKER_BANK.md:54-147,183-236;
contracts/worker/os.cu.worker-task.v1.schema.json:5,1040-1049,
2419-2460,2896-2993,3157-3238,3488-3530,3619-3838.

These pure reference functions validate separate C1 sidecars; they do not add
keys to the closed W8 schema. Observations below are synthetic fixture evidence.
Physical cgroup, rootless, KVM, storage and purge checks remain operator gates.
No test creates a frame, executes a task, accesses Docker, or extracts to disk.
"""

import copy
import hashlib
import json
import re
import sys
import tarfile
import unittest


BOUND_COMMIT = "c8cb7dc8c0946be08edcb2a419629b6debd33c96"
ALLOWED_EXITS = frozenset((0, 2, 3, 4, 5, 6, 7))
PIN_KEYS = frozenset(("base_image", "base_recipe", "base_approval",
                      "cache_manifest", "guest_rootfs", "kernel", "vmm",
                      "normalizer", "export_policy"))
STAGES = ("preflight", "launch", "probe", "ingest", "offline_cache",
          "gate", "extract", "purge")
VERBS = {
    "ingest": frozenset(("repo_bundle", "cache_object", "guest_rootfs")),
    "gate": frozenset(("run_gate", "ui_action")),
    "status": frozenset(("progress", "ui_status")),
    "extract": frozenset(("text_manifest",)),
    "purge": frozenset(("cohort",)),
}
ENV_NAMES = frozenset(("LC_ALL", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
                       "TINI_SUBREAPER", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS",
                       "GOFLAGS", "GOCACHE", "GOMODCACHE", "GOTOOLCHAIN",
                       "CARGO_HOME", "CARGO_NET_OFFLINE", "PIP_NO_INDEX",
                       "HOME", "TMPDIR"))


class Refused(ValueError):
    def __init__(self, reason, code=5):
        if code not in ALLOWED_EXITS or code == 0:
            raise ValueError("invalid refusal exit")
        super().__init__(reason)
        self.code = code


def need(condition, reason, code=5):
    if not condition:
        raise Refused(reason, code)


def closed(value, keys, code=5):
    need(type(value) is dict and set(value) == set(keys), "closed object", code)


def integer(value, low=0, high=(1 << 63) - 1):
    need(type(value) is int and low <= value <= high, "integer range", 2)
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pin(value):
    need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "digest pin", 2)


def ident(value):
    need(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value),
         "identifier", 2)


def canonical(value):
    def check(item, depth=0):
        need(depth <= 32, "JSON nesting bound", 2)
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int:
            integer(item, -(1 << 63))
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) is dict:
            need(all(type(key) is str for key in item), "JSON key", 2)
            for child in item.values():
                check(child, depth + 1)
        else:
            raise Refused("non-integer JSON number or unsupported value", 2)
    check(value)
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise Refused("invalid UTF-8 JSON", 2) from exc


def parse_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out, "duplicate JSON key", 2)
            out[key] = value
        return out
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Refused("JSON constant", 2)))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, Refused):
            raise
        raise Refused("invalid JSON", 2) from exc
    need(canonical(value) == data, "noncanonical JSON", 2)
    return value


def pins_admit(pins):
    closed(pins, PIN_KEYS, 2)
    guest_keys = ("guest_rootfs", "kernel", "vmm")
    guest_nulls = [pins[key] is None for key in guest_keys]
    need(all(guest_nulls) or not any(guest_nulls), "partial guest pins", 2)
    for key, value in pins.items():
        if key not in guest_keys or value is not None:
            pin(value)


def binding_admit(binding):
    closed(binding, ("task_id", "attempt_id", "lease_id", "target_id", "epoch", "nonce"), 2)
    for key in ("task_id", "attempt_id", "lease_id", "target_id"):
        ident(binding[key])
    integer(binding["epoch"], 1)
    pin(binding["nonce"])


def recipe_admit(graph):
    closed(graph, ("schema", "base_recipe", "approval", "outputs", "pins", "builds", "context",
                   "input_manifest_sha256", "build_policy_sha256"))
    need(graph["schema"] == "os.cu.c1-recipe/v1", "recipe schema", 2)
    pin(graph["base_recipe"])
    pin(graph["input_manifest_sha256"])
    pin(graph["build_policy_sha256"])
    approval = graph["approval"]
    closed(approval, ("mode", "recipe_sha256", "approval_sha256"))
    need(approval["mode"] == "per_build", "approval mode", 7)
    need(approval["recipe_sha256"] == graph["base_recipe"], "operator_recipe", 7)
    pin(approval["approval_sha256"])
    closed(graph["outputs"], ("base_image", "guest_rootfs"))
    for value in graph["outputs"].values():
        pin(value)
    closed(graph["pins"], ("arch", "toolchains", "kernel_sha256", "vmm_sha256"))
    need(graph["pins"]["arch"] == "aarch64", "architecture", 2)
    pin(graph["pins"]["kernel_sha256"])
    pin(graph["pins"]["vmm_sha256"])
    need(type(graph["pins"]["toolchains"]) is list and graph["pins"]["toolchains"], "toolchain pins")
    for tool in graph["pins"]["toolchains"]:
        closed(tool, ("name", "version", "sha256"))
        ident(tool["name"])
        ident(tool["version"])
        need(tool["version"] not in ("latest", "stable", "nightly"), "mutable version")
        pin(tool["sha256"])
    builds = graph["builds"]
    need(type(builds) is list and len(builds) == 2, "independent rebuild pair")
    for build in builds:
        closed(build, ("builder_id", "workspace_id", "cache", "recipe_sha256", "outputs", "inputs_sha256"))
        ident(build["builder_id"])
        ident(build["workspace_id"])
        need(build["cache"] == "disabled", "rebuild cache")
        need(build["recipe_sha256"] == graph["base_recipe"], "rebuild recipe")
        need(build["inputs_sha256"] == graph["input_manifest_sha256"], "rebuild declared inputs")
        need(build["outputs"] == graph["outputs"], "non-reproducible output")
    need(builds[0]["builder_id"] != builds[1]["builder_id"] and
         builds[0]["workspace_id"] != builds[1]["workspace_id"], "not independent")
    need(type(graph["context"]) is list and graph["context"], "empty context")
    for item in graph["context"]:
        closed(item, ("source", "kind", "origin", "sha256"))
        source = item["source"]
        need(type(source) is str and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", source),
             "context source")
        parts = source.split("/")
        need(all(part not in (".", "..", ".config", ".ssh", ".git", "home", "repo",
                              "secrets", "credentials", "prompt-memory", ".beckett") for part in parts), "context leak")
        need(parts[0] in ("Dockerfile", "tools", "packages", "guest"), "context root")
        need(not any(part.endswith((".key", ".token")) or part in ("auth.json", "operator.token")
                     for part in parts), "secret path")
        need(item["kind"] == "file" and item["origin"] == "recipe-dir", "context escape")
        pin(item["sha256"])
    declared = {key: graph[key] for key in ("base_recipe", "build_policy_sha256", "pins", "context")}
    need(digest(canonical(declared)) == graph["input_manifest_sha256"], "recipe input graph changed")
    return graph["outputs"]


def cache_admit(cache):
    closed(cache, ("volume", "target", "read_only", "manifest_sha256", "objects",
                   "driver", "driver_options", "prompt_memory_paths"))
    need(cache["volume"] == "os_worker_cache" and cache["target"] == "/opt/cache", "cache location")
    need(cache["read_only"] is True and cache["driver"] == "local" and
         cache["driver_options"] == {} and cache["prompt_memory_paths"] == [], "cache mount")
    need(type(cache["objects"]) is list and cache["objects"], "cache objects")
    ids = set()
    for item in cache["objects"]:
        closed(item, ("object_id", "kind", "sha256", "bytes", "repo_commit", "base_image", "toolchain_lock"))
        ident(item["object_id"])
        need(item["object_id"] not in ids, "duplicate cache object")
        ids.add(item["object_id"])
        need(item["kind"] == "dependency", "cache kind")
        integer(item["bytes"], 1)
        need(re.fullmatch(r"[0-9a-f]{40}", item["repo_commit"]) is not None, "cache commit", 2)
        for key in ("sha256", "base_image", "toolchain_lock"):
            pin(item[key])
    need(digest(canonical(cache["objects"])) == cache["manifest_sha256"], "cache manifest mismatch")
    return cache["manifest_sha256"]


def launch_admit(launch, observed):
    keys = ("schema", "image_digest", "user", "init", "read_only", "cap_drop", "security_opt",
            "privileged", "network", "tmpfs", "volumes", "devices", "memory_bytes", "memswap_bytes",
            "pids_limit", "nofile", "oom_score_adj", "cpuset", "env_names", "log_driver", "restart", "ulimit_core")
    closed(launch, keys)
    need(launch["schema"] == "os.cu.launch-spec/v1", "launch schema", 2)
    pin(launch["image_digest"])
    need(launch["user"] == "10001:10001" and launch["init"] is True and
         launch["read_only"] is True and launch["cap_drop"] == ["ALL"] and
         launch["security_opt"] == ["no-new-privileges:true"] and launch["privileged"] is False,
         "launch hardening")
    need(launch["network"] == "none" and launch["log_driver"] == "none" and
         launch["restart"] == "no" and type(launch["ulimit_core"]) is int and launch["ulimit_core"] == 0,
         "launch defaults")
    integer(launch["memory_bytes"], 33554432)
    integer(launch["memswap_bytes"], 33554432)
    need(launch["memory_bytes"] == launch["memswap_bytes"], "swap")
    integer(launch["pids_limit"], 16, 4096)
    integer(launch["nofile"], 64, 65536)
    integer(launch["oom_score_adj"], 500, 1000)
    need(type(launch["cpuset"]) is list and launch["cpuset"] and
         len(set(launch["cpuset"])) == len(launch["cpuset"]), "cpuset")
    for cpu in launch["cpuset"]:
        integer(cpu)
    need(type(launch["env_names"]) is list and all(type(name) is str for name in launch["env_names"]) and
         len(set(launch["env_names"])) == len(launch["env_names"]) and
         set(launch["env_names"]) <= ENV_NAMES, "environment")
    targets = set()
    need(type(launch["tmpfs"]) is list, "tmpfs list")
    for mount in launch["tmpfs"]:
        closed(mount, ("target", "options"))
        need(mount["target"] in ("/workspace", "/tmp", "/home/worker", "/dev/shm") and
             mount["target"] not in targets, "tmpfs target")
        targets.add(mount["target"])
        expected = "rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001" if mount["target"] == "/workspace" else "rw,noexec,nosuid,nodev"
        need(mount["options"] == expected, "tmpfs options")
    need(targets == {"/workspace", "/tmp", "/home/worker", "/dev/shm"}, "missing RAM mount")
    need(type(launch["volumes"]) is list and len(launch["volumes"]) == 1, "volume list")
    closed(launch["volumes"][0], ("source", "target", "read_only"))
    need(launch["volumes"][0]["read_only"] is True and
         launch["volumes"] == [{"source": "os_worker_cache", "target": "/opt/cache", "read_only": True}], "volume")
    closed(observed, ("daemon", "backend", "inspect", "delegated_controllers", "interactive_cpus",
                      "rootfs_probe", "host_home", "egress_probe", "inference_processes", "frames_fs",
                      "frames_root", "frames_realpath", "frames_disk_bytes", "swap_max", "host_swap_active",
                      "core_dumps", "kvm_present", "kvm_openable", "kvm_ioctl_ok", "volume_driver",
                      "volume_options", "cpuset_effective", "memory_max", "pids_max"))
    need(observed["daemon"] == "rootless", "rootless unavailable", 4)
    need(set(observed["delegated_controllers"]) >= {"cpu", "cpuset", "memory", "pids"}, "delegation unavailable", 4)
    need(observed["backend"] in ("docker", "microvm"), "backend", 2)
    expected_devices = ["/dev/kvm"] if observed["backend"] == "microvm" else []
    need(launch["devices"] == expected_devices, "device phase")
    if observed["backend"] == "microvm":
        need(observed["kvm_present"] is True and observed["kvm_openable"] is True and
             observed["kvm_ioctl_ok"] is True, "KVM qualification unavailable", 4)
    need(canonical(observed["inspect"]) == canonical(launch), "inspect mismatch")
    need(set(launch["cpuset"]).isdisjoint(observed["interactive_cpus"]), "interactive CPU")
    for key in ("memory_max", "pids_max", "swap_max", "frames_disk_bytes", "inference_processes"):
        integer(observed[key])
    need(canonical(observed["cpuset_effective"]) == canonical(launch["cpuset"]) and observed["memory_max"] == launch["memory_bytes"] and
         observed["pids_max"] == launch["pids_limit"] and observed["swap_max"] == 0, "effective limits")
    root = observed["frames_root"]
    need(type(root) is str and re.fullmatch(r"/dev/shm/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", root) and
         root == observed["frames_realpath"] and observed["frames_fs"] == "tmpfs" and
         observed["frames_disk_bytes"] == 0, "storage law")
    need(observed["host_swap_active"] is False and observed["core_dumps"] is False, "RAM persistence")
    need(observed["rootfs_probe"] == "read_only" and observed["host_home"] == "absent" and
         observed["egress_probe"] == "BLOCKED" and observed["inference_processes"] == 0, "unsafe probe")
    need(observed["volume_driver"] == "local" and observed["volume_options"] == {}, "bind-backed cache")
    return True


def exec_admit(envelope, operation, authority):
    closed(envelope, ("schema", "lease_id", "verb", "user", "tini"))
    need(envelope["schema"] == "os.cu.capsule-exec/v1" and type(envelope["verb"]) is str and envelope["verb"] in VERBS and
         envelope["user"] == "10001:10001" and envelope["tini"] is True, "exec envelope")
    closed(operation, ("op", "target_id", "epoch", "object_id", "byte_limit", "payload"))
    closed(authority, ("lease_id", "target_id", "epoch", "objects", "max_bytes", "ui_granted", "expires_ms", "now_ms"))
    ident(envelope["lease_id"])
    for key in ("lease_id", "target_id"):
        ident(authority[key])
    integer(authority["epoch"], 1)
    integer(authority["max_bytes"], 1)
    integer(authority["expires_ms"], 1)
    integer(authority["now_ms"])
    need(type(authority["ui_granted"]) is bool and type(authority["objects"]) is dict, "authority types", 2)
    for object_id, object_op in authority["objects"].items():
        ident(object_id)
        need(type(object_op) is str and object_op in set().union(*VERBS.values()), "authority object type", 2)
    need(envelope["lease_id"] == authority["lease_id"] and operation["target_id"] == authority["target_id"] and
         operation["epoch"] == authority["epoch"], "exec fence", 6)
    integer(operation["epoch"], 1)
    ident(operation["target_id"])
    ident(operation["object_id"])
    need(type(operation["op"]) is str and operation["op"] in VERBS[envelope["verb"]], "verb operation")
    need(authority["objects"].get(operation["object_id"]) == operation["op"], "ungranted object")
    integer(operation["byte_limit"], 1)
    need(operation["byte_limit"] <= authority["max_bytes"], "exec byte cap")
    need(len(canonical({"envelope": envelope, "operation": operation})) <= operation["byte_limit"], "exec actual byte cap")
    need(authority["now_ms"] < authority["expires_ms"], "expired exec", 6)
    if operation["op"] in ("ui_action", "ui_status"):
        need(authority["ui_granted"] is True and operation["byte_limit"] <= 4096, "UI admission")
        payload = operation["payload"]
        if operation["op"] == "ui_status":
            need(payload == {"want": "state"}, "text-only UI status")
        else:
            closed(payload, ("event", "x", "y", "key"))
            need(payload["event"] in ("click", "key"), "UI event")
            integer(payload["x"], 0, 32767)
            integer(payload["y"], 0, 32767)
            need(payload["key"] in (None, "ENTER", "ESCAPE", "TAB", "SPACE"), "UI key")
            need((payload["event"] == "click") == (payload["key"] is None), "UI event payload")
    else:
        need(operation["payload"] is None, "unexpected operation payload")
    return operation["op"]


def ingress_admit(body, object_record, operation, envelope):
    """Length-delimited raw stdin segment following a separately validated exec header."""
    closed(object_record, ("object_id", "op", "bytes", "sha256"))
    ident(object_record["object_id"])
    need(object_record["op"] in VERBS["ingest"], "ingress object kind")
    integer(object_record["bytes"], 1, 16777216)
    pin(object_record["sha256"])
    closed(operation, ("op", "target_id", "epoch", "object_id", "byte_limit", "payload"))
    integer(operation["byte_limit"], 1)
    closed(envelope, ("schema", "lease_id", "verb", "user", "tini"))
    need(envelope["verb"] == "ingest", "ingress outer verb")
    need(operation["op"] == object_record["op"] and operation["object_id"] == object_record["object_id"] and
         operation["payload"] is None, "ingress operation binding")
    header_bytes = len(canonical({"envelope": envelope, "operation": operation}))
    need(header_bytes + object_record["bytes"] <= operation["byte_limit"], "ingress operation byte cap")
    need(type(body) is bytes and len(body) == object_record["bytes"], "ingress length")
    need(digest(body) == object_record["sha256"], "ingress digest")
    return body  # remains RAM-only; caller verifies bundle/cache/rootfs semantics before use


def microvm_admit(plan, observed):
    """Separate qualified VMM lock; no VMM binary, mount or device is accessed.

    Host-owned observations are a required trust boundary, not guest declarations.
    The returned pin maps into the C1 manifest without extending W8 runtime.vmm.
    [CITED] W8_WORKER_BANK.md:233-237,331; research/R1.md:98-135,158-178.
    """
    closed(plan, ("schema", "base_image_sha256", "outer_image_sha256", "recipe_sha256",
                  "rootfs_recipe_sha256", "rootfs_sha256", "kernel_sha256", "outer_seccomp_sha256",
                  "vmm", "control", "guest_arch", "page_pairs"))
    need(plan["schema"] == "os.cu.c1-microvm/v1", "microvm schema", 2)
    for key in ("base_image_sha256", "outer_image_sha256", "recipe_sha256", "rootfs_recipe_sha256",
                "rootfs_sha256", "kernel_sha256", "outer_seccomp_sha256"):
        pin(plan[key])
    need(plan["base_image_sha256"] == plan["outer_image_sha256"] and
         plan["recipe_sha256"] == plan["rootfs_recipe_sha256"], "guest environment lineage")
    closed(plan["vmm"], ("name", "version", "binary_sha256", "seccomp_sha256"))
    need(plan["vmm"]["name"] == "firecracker", "unqualified VMM", 4)
    ident(plan["vmm"]["version"])
    need(plan["vmm"]["version"] not in ("latest", "stable", "nightly"), "mutable VMM version")
    pin(plan["vmm"]["binary_sha256"])
    pin(plan["vmm"]["seccomp_sha256"])
    closed(plan["control"], ("kind", "policy_sha256", "endpoint_scope", "ui_mode"))
    need(plan["control"]["kind"] == "vsock_uds" and
         plan["control"]["endpoint_scope"] == "outer-private" and
         plan["control"]["ui_mode"] == "text-control", "closed VMM channel")
    pin(plan["control"]["policy_sha256"])
    need(plan["guest_arch"] == "aarch64", "guest architecture", 4)
    need(type(plan["page_pairs"]) is list and plan["page_pairs"], "missing page qualification", 4)
    for pair in plan["page_pairs"]:
        closed(pair, ("host_bytes", "guest_bytes"))
        for value in pair.values():
            integer(value, 4096, 65536)
            need(value & (value - 1) == 0, "page granule", 2)
    need(len({canonical(pair) for pair in plan["page_pairs"]}) == len(plan["page_pairs"]), "duplicate page pair")
    closed(observed, ("qualification_sha256", "host_arch", "guest_arch", "host_page_bytes", "guest_page_bytes",
                      "outer_image_sha256", "vmm_binary_sha256", "kernel_sha256", "rootfs_sha256",
                      "outer_seccomp_sha256", "vmm_seccomp_sha256", "device_opens", "guest_rootfs_read_only",
                      "guest_nics", "guest_host_shares", "persistent_writes", "guest_swap", "snapshots", "serial_disk_log",
                      "control_kind", "control_private", "uds_fs", "uds_root", "uds_realpath", "uds_type",
                      "frames_fs", "frames_root", "frames_realpath", "vmm_backing_fs", "vmm_backing_root", "vmm_backing_realpath"))
    need(observed["qualification_sha256"] == digest(canonical(plan)), "VMM lock not qualified", 4)
    for key in ("outer_image_sha256", "kernel_sha256", "rootfs_sha256", "outer_seccomp_sha256"):
        need(observed[key] == plan[key], "observed VMM pin: " + key)
    need(observed["vmm_binary_sha256"] == plan["vmm"]["binary_sha256"] and
         observed["vmm_seccomp_sha256"] == plan["vmm"]["seccomp_sha256"], "observed VMM binary/seccomp")
    need(observed["host_arch"] == observed["guest_arch"] == plan["guest_arch"], "architecture qualification", 4)
    integer(observed["host_page_bytes"], 4096, 65536)
    integer(observed["guest_page_bytes"], 4096, 65536)
    need({"host_bytes": observed["host_page_bytes"], "guest_bytes": observed["guest_page_bytes"]} in plan["page_pairs"],
         "unqualified page pair", 4)
    need(observed["device_opens"] == ["/dev/kvm"], "extra device dependency")
    need(observed["guest_rootfs_read_only"] is True and observed["guest_nics"] == [] and
         observed["guest_host_shares"] == [] and observed["persistent_writes"] is False and
         observed["guest_swap"] is False and observed["snapshots"] is False and
         observed["serial_disk_log"] is False, "guest persistence or network")
    need(observed["control_kind"] == "vsock_uds" and observed["control_private"] is True and
         observed["uds_fs"] == "tmpfs" and observed["uds_type"] == "socket" and
         type(observed["uds_root"]) is str and
         re.fullmatch(r"/workspace/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", observed["uds_root"]) and
         observed["uds_root"] == observed["uds_realpath"], "private control endpoint")
    need(observed["frames_fs"] == "tmpfs" and type(observed["frames_root"]) is str and
         re.fullmatch(r"/dev/shm/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", observed["frames_root"]) and
         observed["frames_root"] == observed["frames_realpath"], "guest frame storage")
    backing = observed["vmm_backing_root"]
    need((observed["vmm_backing_fs"] == "anonymous" and backing is None and observed["vmm_backing_realpath"] is None) or
         (observed["vmm_backing_fs"] == "tmpfs" and type(backing) is str and
          re.fullmatch(r"/dev/shm/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", backing) and
          backing == observed["vmm_backing_realpath"]), "VMM RAM backing")
    return {"vmm": digest(canonical(plan)), "kernel": plan["kernel_sha256"], "guest_rootfs": plan["rootfs_sha256"]}


class ExecLedger:
    """One-shot UI authority, consumed durably before effect in the production adapter."""

    def __init__(self):
        self.entries = {}
        self.effect_count = 0

    def perform(self, envelope, operation, authority, grant, crash=False):
        exec_admit(envelope, operation, authority)
        need(operation["op"] == "ui_action", "one-shot operation")
        closed(grant, ("sequence", "operation_sha256", "expires_ms"))
        integer(grant["sequence"])
        integer(grant["expires_ms"])
        wanted = digest(canonical({"envelope": envelope, "operation": operation}))
        need(grant["operation_sha256"] == wanted and authority["now_ms"] < grant["expires_ms"],
             "UI one-shot grant", 6)
        key = (envelope["lease_id"], operation["target_id"], operation["epoch"], operation["object_id"])
        if key in self.entries:
            previous = self.entries[key]
            need(previous["grant"] == grant and previous["state"] == "acknowledged", "uncertain or reused UI action", 6)
            return previous["ack"]
        self.entries[key] = {"grant": copy.deepcopy(grant), "state": "consumed", "ack": None}
        if crash:
            raise Refused("UI consumption indeterminate; do not replay", 6)
        self.effect_count += 1
        ack = digest(canonical({"binding": list(key), "sequence": grant["sequence"], "operation_sha256": wanted}))
        self.entries[key].update(state="acknowledged", ack=ack)
        return ack


def tar_header(name, size):
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    return info.tobuf(format=tarfile.USTAR_FORMAT)


def tar_bytes(members):
    chunks = []
    for name, data in members:
        chunks.extend((tar_header(name, len(data)), data, b"\0" * ((-len(data)) % 512)))
    return b"".join(chunks) + b"\0" * 1024


def tar_members(data, max_wire_bytes):
    need(type(data) is bytes and len(data) <= max_wire_bytes, "tar wire cap")
    need(len(data) % 512 == 0, "truncated tar")
    members = []
    seen = set()
    offset = 0
    while offset + 512 <= len(data):
        header = data[offset:offset + 512]
        if header == b"\0" * 512:
            need(data[offset:] == b"\0" * 1024, "tar end or trailing data")
            return members
        try:
            info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.TarError, UnicodeError, ValueError) as exc:
            raise Refused("invalid tar header") from exc
        name = info.name
        need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.(?:md|json|txt|patch|diff)", name) is not None,
             "unsafe flat filename")
        need(name not in seen and info.type == tarfile.REGTYPE, "duplicate or special tar member")
        need(type(info.size) is int and 0 <= info.size <= max_wire_bytes, "tar declared size")
        need(header == tar_header(name, info.size), "noncanonical USTAR header")
        seen.add(name)
        need(len(seen) <= 256, "tar member cap")
        offset += 512
        end = offset + info.size
        padded = end + ((-info.size) % 512)
        need(end <= len(data) and padded <= len(data), "truncated member")
        need(data[end:padded] == b"\0" * (padded - end), "tar data padding")
        members.append((name, data[offset:end]))
        offset = padded
    raise Refused("missing tar terminator")


def text_member_admit(data, expected):
    need(data and b"\0" not in data, "empty or NUL text")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise Refused("binary text member") from exc
    kind = expected["kind"]
    need(kind in ("message", "evidence", "report", "patch"), "forbidden export kind")
    if kind in ("message", "evidence"):
        obj = parse_json(data)
        need(type(obj) is dict and set(expected["required_fields"]) <= set(obj), "missing evidence field")
        if kind == "evidence":
            closed(obj, ("answers", "notes"))
            need(type(obj["notes"]) is str and len(obj["notes"]) <= 8192, "notes")
            need(type(obj["answers"]) is list, "answers")
            ids = []
            for answer in obj["answers"]:
                closed(answer, ("request_id", "answer"))
                ident(answer["request_id"])
                need(type(answer["answer"]) is str and len(answer["answer"]) <= 4096, "answer")
                ids.append(answer["request_id"])
            need(ids == expected["evidence_ids"] and len(set(ids)) == len(ids), "incomplete evidence")
        else:
            closed(obj, ("schema", "act", "wave_id", "task_id", "attempt_id", "lease_id", "seq",
                         "repro_sha256", "affected", "status", "body"))
            need(obj["schema"] == "os.cu.speech-act/v1" and obj["act"] in ("finding", "repro", "affected", "status"),
                 "message schema")
            for key in ("wave_id", "task_id", "attempt_id", "lease_id"):
                ident(obj[key])
            integer(obj["seq"], 0, 1000000)
            need(type(obj["body"]) is str and len(obj["body"]) <= 2048, "message body")
            need(type(obj["affected"]) is list and all(type(path) is str for path in obj["affected"]) and
                 len(obj["affected"]) <= 64 and
                 len(set(obj["affected"])) == len(obj["affected"]), "affected paths")
            for path in obj["affected"]:
                need(type(path) is str and len(path) <= 1024 and
                     re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path) and
                     not set(path.split("/")) & {".", "..", ".git"}, "affected path")
            if obj["act"] in ("finding", "repro"):
                pin(obj["repro_sha256"])
            elif obj["repro_sha256"] is not None:
                pin(obj["repro_sha256"])
            need(obj["status"] in ("working", "blocked", "done", "stuck") if obj["act"] == "status"
                 else obj["status"] is None, "message status")
            need(obj["act"] != "affected" or bool(obj["affected"]), "empty affected")
    elif kind == "patch":
        need(text.startswith("diff --git ") and "GIT binary patch" not in text, "nontext patch")
    return text


def extract_admit(data, authority, evidence_limit):
    closed(authority, ("binding", "pins", "repo_commit", "required_files", "receipt_reserve", "max_wire_bytes",
                      "wave_id", "source_seals"))
    binding_admit(authority["binding"])
    pins_admit(authority["pins"])
    ident(authority["wave_id"])
    need(re.fullmatch(r"[0-9a-f]{40}", authority["repo_commit"]) is not None, "extract commit", 2)
    integer(evidence_limit, 1024, 102400)
    integer(authority["receipt_reserve"], 1, evidence_limit)
    integer(authority["max_wire_bytes"], 1024, 1048576)
    members = tar_members(data, authority["max_wire_bytes"])
    need(members and members[0][0] == "manifest.json", "manifest must be first")
    manifest = parse_json(members[0][1])
    closed(manifest, ("schema", "binding", "pins", "repo_commit", "files", "total_bytes"))
    need(manifest["schema"] == "os.cu.c1-extract/v1", "manifest schema", 2)
    binding_admit(manifest["binding"])
    pins_admit(manifest["pins"])
    for key in ("binding", "pins", "repo_commit"):
        need(manifest[key] == authority[key], "manifest binding: " + key)
    required = authority["required_files"]
    need(type(required) is list and required, "trusted export roster")
    for item in required:
        closed(item, ("name", "kind", "evidence_ids", "required_fields"))
        need(type(item["evidence_ids"]) is list and all(type(value) is str for value in item["evidence_ids"]),
             "evidence coverage shape", 2)
        need(type(item["required_fields"]) is list and all(type(value) is str for value in item["required_fields"]),
             "required field shape", 2)
    names = [item["name"] for item in required]
    need(names == sorted(names) and len(set(names)) == len(names) and "manifest.json" not in names,
         "trusted roster shape")
    closed(authority["source_seals"], names)
    for seal in authority["source_seals"].values():
        closed(seal, ("bytes", "sha256"))
        integer(seal["bytes"], 1, evidence_limit)
        pin(seal["sha256"])
    need([name for name, _ in members[1:]] == names, "export set differs from task roster")
    need(type(manifest["files"]) is list and len(manifest["files"]) == len(required), "manifest set")
    total = 0
    for claim, expected, (name, body) in zip(manifest["files"], required, members[1:]):
        closed(claim, ("name", "kind", "bytes", "sha256", "evidence_ids"))
        need(claim["name"] == name and claim["kind"] == expected["kind"] and
             claim["evidence_ids"] == expected["evidence_ids"], "export kind or coverage")
        integer(claim["bytes"], 1, evidence_limit)
        pin(claim["sha256"])
        need(len(body) == claim["bytes"] and digest(body) == claim["sha256"], "incomplete evidence bytes")
        seal = authority["source_seals"][name]
        need(len(body) == seal["bytes"] and digest(body) == seal["sha256"], "source-object seal mismatch")
        text_member_admit(body, expected)
        if expected["kind"] == "message":
            obj = parse_json(body)
            need(all(obj[key] == authority["binding"][key] for key in ("task_id", "attempt_id", "lease_id")),
                 "message attempt binding")
            need(obj["wave_id"] == authority["wave_id"], "message wave binding")
        total += len(body)
    need(type(manifest["total_bytes"]) is int and manifest["total_bytes"] == total, "payload total")
    accepted_bytes = total + len(members[0][1])
    need(accepted_bytes + authority["receipt_reserve"] <= min(evidence_limit, 102400), "receipt-first evidence cap")
    return {"method": "tar-exec", "entries": len(members), "bytes": accepted_bytes,
            "text_only": True, "mode": "0644", "owner_asserted": True,
            "staging_residue": 0, "sha256": digest(data)}


def semantic_repro(pins, repo_commit, gate_digest, evidence_digest):
    pins_admit(pins)
    need(re.fullmatch(r"[0-9a-f]{40}", repo_commit) is not None, "repro commit", 2)
    pin(gate_digest)
    pin(evidence_digest)
    return digest(canonical({"schema": "os.cu.c1-repro/v1", "pins": pins,
                             "repo_commit": repo_commit, "gate_sha256": gate_digest,
                             "evidence_sha256": evidence_digest}))


def child_admit(child, observed, parent_budget):
    closed(child, ("target_id", "process", "limits", "manager", "binds"))
    closed(observed, ("process", "effective", "paused", "attached_before_exec", "controllers"))
    closed(parent_budget, ("memory_bytes", "overhead_bytes", "siblings_memory_bytes", "pids", "siblings_pids", "cpuset",
                           "overhead_pids", "quota_us", "period_us", "siblings_quota_us", "siblings_period_us"))
    ident(child["target_id"])
    closed(child["process"], ("pid", "start_time", "cgroup_inode"))
    for value in child["process"].values():
        integer(value, 1)
    limits = child["limits"]
    closed(limits, ("memory_bytes", "swap_bytes", "pids", "cpuset", "quota_us", "period_us", "weight"))
    integer(limits["memory_bytes"], 1)
    integer(limits["pids"], 1)
    integer(limits["swap_bytes"])
    integer(limits["quota_us"], 1)
    integer(limits["period_us"], 1)
    integer(limits["weight"], 1, 10000)
    for key in ("memory_bytes", "overhead_bytes", "siblings_memory_bytes", "pids", "siblings_pids", "overhead_pids",
                "siblings_quota_us"):
        integer(parent_budget[key])
    for key in ("quota_us", "period_us", "siblings_period_us"):
        integer(parent_budget[key], 1)
    need(type(limits["cpuset"]) is list and type(parent_budget["cpuset"]) is list, "child cpuset shape", 2)
    for cpu in limits["cpuset"] + parent_budget["cpuset"]:
        integer(cpu)
    need(limits["swap_bytes"] == 0 and child["manager"] == "host-delegated" and child["binds"] == [],
         "child enforcement surface")
    need(observed["paused"] is True and observed["attached_before_exec"] is True and
         canonical(observed["process"]) == canonical(child["process"]), "process identity or launch race", 6)
    closed(observed["effective"], ("memory_bytes", "swap_bytes", "pids", "cpuset", "quota_us", "period_us", "weight"))
    for key in ("memory_bytes", "swap_bytes", "pids", "quota_us", "period_us", "weight"):
        integer(observed["effective"][key])
    need(type(observed["effective"]["cpuset"]) is list, "effective cpuset shape", 2)
    for cpu in observed["effective"]["cpuset"]:
        integer(cpu)
    need(observed["effective"] == limits and
         set(observed["controllers"]) >= {"cpu", "cpuset", "memory", "pids"}, "child effective limits", 4)
    need(limits["memory_bytes"] + parent_budget["overhead_bytes"] + parent_budget["siblings_memory_bytes"] <=
         parent_budget["memory_bytes"], "ancestor memory budget")
    need(limits["pids"] + parent_budget["siblings_pids"] + parent_budget["overhead_pids"] <= parent_budget["pids"],
         "ancestor pids budget")
    # Exact rational CPU sum: child quota/period + sibling quota/period <= parent.
    # Python integers do not overflow; the Go adapter must use checked wide arithmetic.
    left = (limits["quota_us"] * parent_budget["siblings_period_us"] +
            parent_budget["siblings_quota_us"] * limits["period_us"]) * parent_budget["period_us"]
    right = parent_budget["quota_us"] * limits["period_us"] * parent_budget["siblings_period_us"]
    need(left <= right, "ancestor CPU quota budget")
    need(limits["cpuset"] and set(limits["cpuset"]) <= set(parent_budget["cpuset"]), "ancestor cpuset")
    return {"start_permitted": True, "process": copy.deepcopy(child["process"])}


class PurgeLedger:
    """CAS reference transaction. Physical deletion precedes commit; never atomic by assertion."""

    def __init__(self):
        self.owner = None
        self.proof = None
        self.state = "RUNNING"
        self.reservation_held = True
        self.outer_reservation_held = True
        self.release_count = 0

    def begin(self, owner):
        self.proof = None  # every recovery/reconciliation pass needs fresh physical evidence
        closed(owner, ("lease_id", "target_id", "epoch", "cohort_id", "reservation_id", "release_key",
                       "processes", "scope", "siblings_active", "owner_epoch", "cohort_generation", "daemon_id", "container_id"))
        for key in ("lease_id", "target_id", "cohort_id", "reservation_id", "release_key", "daemon_id", "container_id"):
            ident(owner[key])
        integer(owner["epoch"], 1)
        integer(owner["owner_epoch"], 1)
        integer(owner["cohort_generation"], 1)
        integer(owner["siblings_active"])
        need(type(owner["processes"]) is list and owner["processes"], "missing owned process identities", 6)
        seen = set()
        for process in owner["processes"]:
            closed(process, ("pid", "start_time", "cgroup_inode"), 6)
            for value in process.values():
                integer(value, 1)
            identity = (process["pid"], process["start_time"], process["cgroup_inode"])
            need(identity not in seen, "duplicate process identity", 6)
            seen.add(identity)
        need(owner["scope"] in ("child", "cohort"), "purge scope", 2)
        need(self.owner is None or self.owner == owner, "purge owner changed", 6)
        self.owner = copy.deepcopy(owner)
        if self.release_count == 0:
            self.state = "STOPPING"

    def observe(self, proof):
        self.proof = None  # a failed refresh must never leave a previously good proof reusable
        need(self.owner is not None, "no purge owner", 6)
        closed(proof, ("lease_id", "target_id", "epoch", "checked_processes", "remaining_pids",
                       "remaining_workspace_bytes", "listeners", "ram_roots", "containers", "host_rules",
                       "outer_containers", "fenced", "collection_frozen", "extract_status", "pending_launch_intents",
                       "owner_epoch", "cohort_generation", "daemon_id", "container_id", "cohort_id", "reservation_id",
                       "release_key", "scope", "siblings_active"))
        for key in ("epoch", "owner_epoch", "cohort_generation"):
            integer(proof[key], 1)
        integer(proof["siblings_active"])
        integer(proof["outer_containers"])
        integer(proof["pending_launch_intents"])
        need(all(canonical(proof[key]) == canonical(value) for key, value in self.owner.items() if key != "processes"),
             "purge fence", 6)
        need(canonical(proof["checked_processes"]) == canonical(self.owner["processes"]), "purge process identities", 6)
        need(proof["fenced"] is True and proof["collection_frozen"] is True, "purge action still active", 6)
        need(proof["extract_status"] in ("complete", "refused", "unavailable"), "collection uncertain", 6)
        self.proof = copy.deepcopy(proof)

    def commit(self, owner, crash=False):
        need(canonical(self.owner) == canonical(owner), "stale CAS owner", 6)
        if self.release_count:
            return False
        need(self.proof is not None, "purge unobserved", 6)
        for key in ("remaining_pids", "remaining_workspace_bytes", "listeners", "ram_roots", "containers", "host_rules",
                    "pending_launch_intents"):
            need(type(self.proof[key]) is int and self.proof[key] == 0, "physical purge incomplete", 6)
        if owner["scope"] == "cohort":
            need(owner["siblings_active"] == 0 and self.proof["outer_containers"] == 0, "cohort still owned", 6)
        if crash:
            self.proof = None
            self.state = "INDETERMINATE"
            raise Refused("crash before release transaction", 6)
        self.reservation_held = False
        if owner["scope"] == "cohort":
            self.outer_reservation_held = False
        self.release_count = 1
        self.state = "PURGE_COMMITTED"  # internal sidecar phase, not a W8 scheduler state
        return True


def trace_admit(trace, reservation):
    closed(reservation, ("ttl_ms", "memory_bytes", "stage_caps_ms", "resident_bytes", "clone_bytes",
                         "cache_copy_bytes", "guest_bytes", "vmm_bytes", "frames_bytes", "extract_bytes", "purge_headroom_bytes"))
    closed(reservation["stage_caps_ms"], STAGES)
    for key, value in reservation.items():
        if key != "stage_caps_ms":
            integer(value)
    for value in reservation["stage_caps_ms"].values():
        integer(value, 1)
    need(sum(reservation["stage_caps_ms"].values()) <= reservation["ttl_ms"], "deadline headroom")
    memory = sum(value for key, value in reservation.items() if key.endswith("_bytes") and key != "memory_bytes")
    need(memory <= reservation["memory_bytes"], "RAM reservation")
    need(type(trace) is list and all(type(item) is dict for item in trace) and
         [item.get("stage") for item in trace] == list(STAGES), "stage order")
    total_ms = 0
    for stage in trace:
        closed(stage, ("stage", "duration_ms", "peak_ram_bytes", "outcome"))
        integer(stage["duration_ms"])
        integer(stage["peak_ram_bytes"])
        need(stage["outcome"] in ("ok", "miss", "skipped"), "stage outcome")
        need(stage["duration_ms"] <= reservation["stage_caps_ms"][stage["stage"]] and
             stage["peak_ram_bytes"] <= reservation["memory_bytes"], "stage reservation exceeded")
        total_ms += stage["duration_ms"]
    need(total_ms <= reservation["ttl_ms"], "elapsed deadline")
    miss = trace[4]["outcome"] == "miss"
    need(not miss or trace[5]["outcome"] == "skipped", "offline miss ran gate")
    need(all(trace[index]["outcome"] == "ok" for index in (6, 7)), "finalization not complete")
    return 4 if miss else 0


def good_pins():
    return {name: digest(name.encode()) for name in sorted(PIN_KEYS)}


def good_binding():
    return {"task_id": "task-a", "attempt_id": "a1", "lease_id": "lease-a", "target_id": "vm-a",
            "epoch": 3, "nonce": digest(b"synthetic nonce")}


def good_recipe():
    recipe = digest(b"synthetic recipe")
    outputs = {"base_image": digest(b"synthetic base"), "guest_rootfs": digest(b"synthetic rootfs")}
    graph = {"schema": "os.cu.c1-recipe/v1", "base_recipe": recipe,
            "approval": {"mode": "per_build", "recipe_sha256": recipe, "approval_sha256": digest(b"approval")},
            "outputs": outputs, "pins": {"arch": "aarch64", "kernel_sha256": digest(b"kernel"),
                "vmm_sha256": digest(b"vmm"), "toolchains": [{"name": "go", "version": "fixture-v1", "sha256": digest(b"go")}]},
            "builds": [{"builder_id": "builder-" + suffix, "workspace_id": "fresh-" + suffix,
                        "cache": "disabled", "recipe_sha256": recipe, "outputs": copy.deepcopy(outputs)} for suffix in ("a", "b")],
            "context": [{"source": "tools/runner.go", "kind": "file", "origin": "recipe-dir", "sha256": digest(b"runner")}]}
    graph["build_policy_sha256"] = digest(b"synthetic builder/export policy")
    graph["input_manifest_sha256"] = digest(canonical({key: graph[key] for key in
        ("base_recipe", "build_policy_sha256", "pins", "context")}))
    for build in graph["builds"]:
        build["inputs_sha256"] = graph["input_manifest_sha256"]
    return graph


def good_cache():
    objects = [{"object_id": "gomod-a", "kind": "dependency", "sha256": digest(b"dependency"), "bytes": 42,
                "repo_commit": BOUND_COMMIT, "base_image": good_pins()["base_image"], "toolchain_lock": digest(b"lock")}]
    return {"volume": "os_worker_cache", "target": "/opt/cache", "read_only": True,
            "manifest_sha256": digest(canonical(objects)), "objects": objects, "driver": "local",
            "driver_options": {}, "prompt_memory_paths": []}


def good_launch(backend="docker"):
    launch = {"schema": "os.cu.launch-spec/v1", "image_digest": good_pins()["base_image"], "user": "10001:10001",
              "init": True, "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
              "privileged": False, "network": "none", "tmpfs": [
                  {"target": path, "options": "rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001" if path == "/workspace" else "rw,noexec,nosuid,nodev"}
                  for path in ("/workspace", "/tmp", "/home/worker", "/dev/shm")],
              "volumes": [{"source": "os_worker_cache", "target": "/opt/cache", "read_only": True}],
              "devices": ["/dev/kvm"] if backend == "microvm" else [], "memory_bytes": 1073741824,
              "memswap_bytes": 1073741824, "pids_limit": 256, "nofile": 8192, "oom_score_adj": 800,
              "cpuset": [4, 5], "env_names": ["LC_ALL", "HOME", "TMPDIR", "GIT_TERMINAL_PROMPT"],
              "log_driver": "none", "restart": "no", "ulimit_core": 0}
    observed = {"daemon": "rootless", "backend": backend, "inspect": copy.deepcopy(launch),
                "delegated_controllers": ["cpu", "cpuset", "memory", "pids"], "interactive_cpus": [0, 1],
                "rootfs_probe": "read_only", "host_home": "absent", "egress_probe": "BLOCKED", "inference_processes": 0,
                "frames_fs": "tmpfs", "frames_root": "/dev/shm/w8/task", "frames_realpath": "/dev/shm/w8/task",
                "frames_disk_bytes": 0, "swap_max": 0, "host_swap_active": False, "core_dumps": False,
                "kvm_present": backend == "microvm", "kvm_openable": backend == "microvm", "kvm_ioctl_ok": backend == "microvm",
                "volume_driver": "local", "volume_options": {}, "cpuset_effective": [4, 5],
                "memory_max": 1073741824, "pids_max": 256}
    return launch, observed


def good_microvm():
    plan = {"schema": "os.cu.c1-microvm/v1", "base_image_sha256": digest(b"base"),
            "outer_image_sha256": digest(b"base"), "recipe_sha256": digest(b"recipe"),
            "rootfs_recipe_sha256": digest(b"recipe"), "rootfs_sha256": digest(b"rootfs"),
            "kernel_sha256": digest(b"kernel"), "outer_seccomp_sha256": digest(b"outer seccomp"),
            "vmm": {"name": "firecracker", "version": "fixture-v1", "binary_sha256": digest(b"vmm"),
                    "seccomp_sha256": digest(b"VMM seccomp")},
            "control": {"kind": "vsock_uds", "policy_sha256": digest(b"typed channel"),
                        "endpoint_scope": "outer-private", "ui_mode": "text-control"},
            "guest_arch": "aarch64", "page_pairs": [{"host_bytes": 4096, "guest_bytes": 4096}]}
    observed = {"qualification_sha256": digest(canonical(plan)), "host_arch": "aarch64", "guest_arch": "aarch64",
                "host_page_bytes": 4096, "guest_page_bytes": 4096,
                "outer_image_sha256": plan["outer_image_sha256"], "vmm_binary_sha256": plan["vmm"]["binary_sha256"],
                "kernel_sha256": plan["kernel_sha256"], "rootfs_sha256": plan["rootfs_sha256"],
                "outer_seccomp_sha256": plan["outer_seccomp_sha256"], "vmm_seccomp_sha256": plan["vmm"]["seccomp_sha256"],
                "device_opens": ["/dev/kvm"], "guest_rootfs_read_only": True, "guest_nics": [], "guest_host_shares": [],
                "persistent_writes": False, "guest_swap": False, "snapshots": False, "serial_disk_log": False,
                "control_kind": "vsock_uds", "control_private": True, "uds_fs": "tmpfs", "uds_type": "socket",
                "uds_root": "/workspace/guests/vm-a", "uds_realpath": "/workspace/guests/vm-a",
                "frames_fs": "tmpfs", "frames_root": "/dev/shm/ui", "frames_realpath": "/dev/shm/ui",
                "vmm_backing_fs": "tmpfs", "vmm_backing_root": "/dev/shm/vmm", "vmm_backing_realpath": "/dev/shm/vmm"}
    return plan, observed


def good_exec(verb="ingest", op="repo_bundle"):
    binding = good_binding()
    envelope = {"schema": "os.cu.capsule-exec/v1", "lease_id": binding["lease_id"], "verb": verb,
                "user": "10001:10001", "tini": True}
    operation = {"op": op, "target_id": binding["target_id"], "epoch": binding["epoch"],
                 "object_id": "object-a", "byte_limit": 4096, "payload": None}
    authority = {"lease_id": binding["lease_id"], "target_id": binding["target_id"], "epoch": binding["epoch"],
                 "objects": {"object-a": op}, "max_bytes": 8192, "ui_granted": True, "expires_ms": 100, "now_ms": 10}
    if op == "ui_action":
        operation["payload"] = {"event": "click", "x": 20, "y": 40, "key": None}
    elif op == "ui_status":
        operation["payload"] = {"want": "state"}
    return envelope, operation, authority


def good_export():
    binding = good_binding()
    message = {"schema": "os.cu.speech-act/v1", "act": "finding", "wave_id": "wave-a",
               "task_id": binding["task_id"], "attempt_id": binding["attempt_id"], "lease_id": binding["lease_id"],
               "seq": 2, "repro_sha256": digest(b"repro"), "affected": ["src/example.go"], "status": None,
               "body": "a" * 1400 + "complete tail"}
    bodies = {"evidence.json": canonical({"answers": [{"request_id": "E0", "answer": "alpha"},
                                                       {"request_id": "E1", "answer": "beta"}], "notes": "typed evidence"}),
              "message.json": canonical(message), "report.md": b"# Report\nTyped result, no raw stream.\n"}
    required = [{"name": "evidence.json", "kind": "evidence", "evidence_ids": ["E0", "E1"], "required_fields": ["answers", "notes"]},
                {"name": "message.json", "kind": "message", "evidence_ids": [], "required_fields": ["body", "seq", "task_id", "attempt_id", "lease_id"]},
                {"name": "report.md", "kind": "report", "evidence_ids": [], "required_fields": []}]
    authority = {"binding": binding, "pins": good_pins(), "repo_commit": BOUND_COMMIT,
                 "required_files": required, "receipt_reserve": 2048, "max_wire_bytes": 131072,
                 "wave_id": "wave-a", "source_seals": {}}
    reseal_export(authority, bodies)
    return authority, bodies


def reseal_export(authority, bodies):
    """Fixture-only: simulate a NEW independent frozen source commitment, never auto-reseal a tar."""
    authority["source_seals"] = {name: {"bytes": len(body), "sha256": digest(body)} for name, body in bodies.items()}


def export_archive(authority, bodies):
    files = [{"name": item["name"], "kind": item["kind"], "bytes": len(bodies[item["name"]]),
              "sha256": digest(bodies[item["name"]]), "evidence_ids": item["evidence_ids"]}
             for item in authority["required_files"]]
    manifest = {"schema": "os.cu.c1-extract/v1", "binding": authority["binding"], "pins": authority["pins"],
                "repo_commit": authority["repo_commit"], "files": files, "total_bytes": sum(x["bytes"] for x in files)}
    return tar_bytes([("manifest.json", canonical(manifest))] + [(item["name"], bodies[item["name"]]) for item in files])


def good_child():
    process = {"pid": 321, "start_time": 456, "cgroup_inode": 789}
    limits = {"memory_bytes": 256, "swap_bytes": 0, "pids": 8, "cpuset": [4],
              "quota_us": 50000, "period_us": 100000, "weight": 100}
    child = {"target_id": "vm-a", "process": process, "limits": limits, "manager": "host-delegated", "binds": []}
    observed = {"process": copy.deepcopy(process), "effective": copy.deepcopy(limits), "paused": True,
                "attached_before_exec": True, "controllers": ["cpu", "cpuset", "memory", "pids"]}
    parent = {"memory_bytes": 1024, "overhead_bytes": 128, "siblings_memory_bytes": 512,
              "pids": 32, "siblings_pids": 16, "overhead_pids": 2, "cpuset": [4, 5],
              "quota_us": 200000, "period_us": 100000, "siblings_quota_us": 100000, "siblings_period_us": 100000}
    return child, observed, parent


def good_purge(scope="cohort", siblings=0):
    owner = {"lease_id": "lease-a", "target_id": "vm-a", "epoch": 3, "cohort_id": "cohort-a",
             "reservation_id": "reservation-a", "release_key": "release-a",
             "processes": [{"pid": 321, "start_time": 456, "cgroup_inode": 789}],
             "scope": scope, "siblings_active": siblings, "owner_epoch": 2, "cohort_generation": 1,
             "daemon_id": "daemon-a", "container_id": "container-a"}
    proof = {"lease_id": "lease-a", "target_id": "vm-a", "epoch": 3,
             "checked_processes": copy.deepcopy(owner["processes"]), "remaining_pids": 0, "remaining_workspace_bytes": 0,
             "listeners": 0, "ram_roots": 0, "containers": 0, "host_rules": 0,
             "outer_containers": 1 if siblings else 0, "fenced": True, "collection_frozen": True, "extract_status": "complete",
             "pending_launch_intents": 0}
    proof.update({key: value for key, value in owner.items() if key != "processes"})
    return owner, proof


def good_trace(miss=False):
    trace = [{"stage": stage, "duration_ms": 10, "peak_ram_bytes": 100, "outcome": "ok"} for stage in STAGES]
    if miss:
        trace[4]["outcome"] = "miss"
        trace[5]["outcome"] = "skipped"
        trace[5]["duration_ms"] = 0
    reservation = {"ttl_ms": 800, "memory_bytes": 1024, "stage_caps_ms": {stage: 100 for stage in STAGES},
                   "resident_bytes": 128, "clone_bytes": 64, "cache_copy_bytes": 64, "guest_bytes": 256,
                   "vmm_bytes": 128, "frames_bytes": 128, "extract_bytes": 128, "purge_headroom_bytes": 128}
    return trace, reservation


class C1LifecycleTests(unittest.TestCase):
    def refused(self, fn, *args, code=None):
        with self.assertRaises(Refused) as caught:
            fn(*args)
        self.assertIn(caught.exception.code, ALLOWED_EXITS - {0})
        if code is not None:
            self.assertEqual(caught.exception.code, code)

    def test_canonical_integer_json_and_duplicate_refusal(self):
        self.assertEqual(canonical({"z": 2, "a": "é"}), b'{"a":"\xc3\xa9","z":2}')
        self.assertEqual(canonical({"raw_exit_code": -15}), b'{"raw_exit_code":-15}')
        for value in (1.5, float("nan"), {"x": 1 << 63}):
            with self.subTest(value=str(value)):
                self.refused(canonical, value, code=2)
        self.refused(parse_json, b'{"x":1,"x":2}', code=2)
        self.refused(parse_json, b'{"x": 1}', code=2)
        self.refused(parse_json, b"[" * 40 + b"0" + b"]" * 40, code=2)

    def test_recipe_independent_rebuild_matches_both_outputs(self):
        graph = good_recipe()
        self.assertEqual(recipe_admit(graph), graph["outputs"])
        for key in ("base_image", "guest_rootfs"):
            bad = good_recipe()
            bad["builds"][1]["outputs"][key] = digest(b"different build")
            self.refused(recipe_admit, bad)

    def test_recipe_rejects_per_commit_images_and_code_output(self):
        for key in ("per_commit_image", "repo_code", "image_tag"):
            graph = good_recipe()
            graph["outputs"][key] = digest(b"stale output")
            self.refused(recipe_admit, graph)

    def test_recipe_rebuilds_bind_complete_input_graph(self):
        graph = good_recipe()
        graph["pins"]["toolchains"][0]["sha256"] = digest(b"different toolchain")
        self.refused(recipe_admit, graph)
        graph = good_recipe()
        graph["builds"][1]["inputs_sha256"] = digest(b"different input closure")
        self.refused(recipe_admit, graph)
        graph = good_recipe()
        graph["build_policy_sha256"] = digest(b"different exporter")
        self.refused(recipe_admit, graph)

    def test_recipe_rejects_shared_builder_cache_and_wrong_approval(self):
        for key in ("builder_id", "workspace_id"):
            graph = good_recipe()
            graph["builds"][1][key] = graph["builds"][0][key]
            self.refused(recipe_admit, graph)
        graph = good_recipe()
        graph["builds"][1]["cache"] = "enabled"
        self.refused(recipe_admit, graph)
        graph = good_recipe()
        graph["approval"]["recipe_sha256"] = digest(b"unapproved")
        self.refused(recipe_admit, graph, code=7)

    def test_recipe_secret_config_symlink_and_absolute_negatives(self):
        for source in ("~/.config/app", "/home/user/.ssh/id", "tools/../private", "tools/.config/app",
                       "tools/auth.json", "tools/private.key", "tools/.beckett/token", "repo/main.go", "$HOME/file", "prompt-memory/entry"):
            graph = good_recipe()
            graph["context"][0]["source"] = source
            self.refused(recipe_admit, graph)
        for key, value in (("kind", "symlink"), ("origin", "host-bind")):
            graph = good_recipe()
            graph["context"][0][key] = value
            self.refused(recipe_admit, graph)

    def test_readonly_cache_manifest_and_mutation_refusal(self):
        self.assertEqual(cache_admit(good_cache()), good_cache()["manifest_sha256"])
        for key, value in (("read_only", False), ("manifest_sha256", digest(b"changed"))):
            cache = good_cache()
            cache[key] = value
            self.refused(cache_admit, cache)

    def test_cache_refuses_bind_driver_options_and_prompt_paths(self):
        for options in ({"type": "none", "o": "bind", "device": "/host/cache"}, {"device": "/dev/shm/memory"}):
            cache = good_cache()
            cache["driver_options"] = options
            self.refused(cache_admit, cache)
        cache = good_cache()
        cache["prompt_memory_paths"] = ["/host/prompt-memory"]
        self.refused(cache_admit, cache)

    def test_docker_and_microvm_good_launch(self):
        for backend in ("docker", "microvm"):
            self.assertTrue(launch_admit(*good_launch(backend)))

    def test_launch_bind_envfile_docker_socket_must_fail(self):
        for key, value in (("binds", ["/host:/workspace"]), ("env_file", "vars.env"),
                           ("mounts", [{"source": "/var/run/docker.sock"}])):
            launch, observed = good_launch()
            launch[key] = value
            self.refused(launch_admit, launch, observed)
        launch, observed = good_launch()
        launch["volumes"][0]["source"] = "/var/run/docker.sock"
        self.refused(launch_admit, launch, observed)

    def test_launch_phase_devices_and_kvm_qualification(self):
        for backend, devices in (("docker", ["/dev/kvm"]), ("microvm", []),
                                  ("microvm", ["/dev/kvm", "/dev/vhost-vsock"])):
            launch, observed = good_launch(backend)
            launch["devices"] = devices
            self.refused(launch_admit, launch, observed)
        for key in ("kvm_present", "kvm_openable", "kvm_ioctl_ok"):
            launch, observed = good_launch("microvm")
            observed[key] = False
            self.refused(launch_admit, launch, observed, code=4)

    def test_microvm_pinned_lock_and_readonly_guest(self):
        plan, observed = good_microvm()
        admitted = microvm_admit(plan, observed)
        self.assertEqual(admitted["vmm"], digest(canonical(plan)))
        self.assertEqual(admitted["guest_rootfs"], plan["rootfs_sha256"])
        observed["vmm_backing_fs"] = "anonymous"
        observed["vmm_backing_root"] = observed["vmm_backing_realpath"] = None
        self.assertEqual(microvm_admit(plan, observed), admitted)

    def test_microvm_requires_qualified_lineage_and_actual_artifact_pins(self):
        for key in ("qualification_sha256", "outer_image_sha256", "vmm_binary_sha256", "kernel_sha256",
                    "rootfs_sha256", "outer_seccomp_sha256", "vmm_seccomp_sha256"):
            plan, observed = good_microvm()
            observed[key] = digest(b"wrong actual object")
            self.refused(microvm_admit, plan, observed)
        for key in ("outer_image_sha256", "rootfs_recipe_sha256"):
            plan, observed = good_microvm()
            plan[key] = digest(b"other lineage")
            observed["qualification_sha256"] = digest(canonical(plan))
            self.refused(microvm_admit, plan, observed)
        for key, value in (("name", "new-vmm"), ("version", "latest")):
            plan, observed = good_microvm()
            plan["vmm"][key] = value
            self.refused(microvm_admit, plan, observed)

    def test_microvm_no_extra_device_network_disk_or_unqualified_pages(self):
        negatives = (("device_opens", ["/dev/kvm", "/dev/vhost-vsock"]),
                     ("device_opens", ["/dev/kvm", "/dev/net/tun"]), ("guest_nics", ["eth0"]),
                     ("guest_host_shares", ["/host"]), ("guest_rootfs_read_only", False),
                     ("persistent_writes", True), ("guest_swap", True), ("snapshots", True),
                     ("serial_disk_log", True), ("host_arch", "x86_64"), ("guest_arch", "x86_64"),
                     ("host_page_bytes", 65536), ("guest_page_bytes", 65536))
        for key, value in negatives:
            with self.subTest(key=key, value=value):
                plan, observed = good_microvm()
                observed[key] = value
                self.refused(microvm_admit, plan, observed)

    def test_microvm_private_channel_and_guest_shm_only(self):
        negatives = (("control_private", False), ("control_kind", "serial"), ("uds_fs", "ext4"),
                     ("uds_realpath", "/host/socket"), ("uds_type", "file"), ("frames_fs", "ext4"),
                     ("frames_root", "/workspace/frames"), ("frames_realpath", "/tmp/frames"),
                     ("vmm_backing_fs", "ext4"), ("vmm_backing_root", "/workspace/memory"),
                     ("vmm_backing_realpath", "/host/memory"))
        for key, value in negatives:
            with self.subTest(key=key):
                plan, observed = good_microvm()
                observed[key] = value
                self.refused(microvm_admit, plan, observed)
        plan, observed = good_microvm()
        plan["control"]["ui_mode"] = "raw-xpra"
        self.refused(microvm_admit, plan, observed)

    def test_launch_rootless_delegation_and_effective_limits(self):
        for key, value in (("daemon", "root"), ("delegated_controllers", ["cpu", "memory"])):
            launch, observed = good_launch()
            observed[key] = value
            self.refused(launch_admit, launch, observed, code=4)
        for key, value in (("memory_max", 2147483648), ("pids_max", 512), ("cpuset_effective", [4, 6]), ("swap_max", 1)):
            launch, observed = good_launch()
            observed[key] = value
            self.refused(launch_admit, launch, observed)
        launch, observed = good_launch()
        launch["memswap_bytes"] = float(launch["memory_bytes"])
        self.refused(launch_admit, launch, observed, code=2)
        launch, observed = good_launch()
        launch["volumes"][0]["read_only"] = 1
        self.refused(launch_admit, launch, observed)

    def test_launch_storage_law_requires_real_tmpfs_no_symlink_swap_or_core(self):
        for key, value in (("frames_fs", "ext4"), ("frames_root", "/workspace/frames"),
                           ("frames_realpath", "/tmp/frames"), ("frames_disk_bytes", 1),
                           ("host_swap_active", True), ("core_dumps", True)):
            launch, observed = good_launch()
            observed[key] = value
            self.refused(launch_admit, launch, observed)

    def test_launch_safe_probes_inspect_and_host_cache_mount(self):
        for key, value in (("host_home", "visible"), ("egress_probe", "OPEN"), ("inference_processes", 1),
                           ("volume_options", {"o": "bind"}), ("interactive_cpus", [4])):
            launch, observed = good_launch()
            observed[key] = value
            self.refused(launch_admit, launch, observed)
        launch, observed = good_launch()
        observed["inspect"]["read_only"] = False
        self.refused(launch_admit, launch, observed)

    def test_exec_five_verbs_and_typed_ingest_objects(self):
        for verb, operations in VERBS.items():
            for op in operations:
                self.assertEqual(exec_admit(*good_exec(verb, op)), op)

    def test_exec_refuses_shell_paths_envfiles_and_ungranted_object(self):
        for key, value in (("argv", ["sh", "-c", "id"]), ("env_file", "vars.env"), ("socket", "/workspace/other.sock")):
            env, op, authority = good_exec()
            env[key] = value
            self.refused(exec_admit, env, op, authority)
        for key, value in (("object_id", "../../private"), ("op", "shell"), ("object_id", "missing")):
            env, op, authority = good_exec()
            op[key] = value
            self.refused(exec_admit, env, op, authority)

    def test_exec_expiry_epoch_and_sibling_fencing(self):
        for key, value in (("target_id", "vm-b"), ("epoch", 4)):
            env, op, authority = good_exec()
            op[key] = value
            self.refused(exec_admit, env, op, authority, code=6)
        env, op, authority = good_exec()
        authority["now_ms"] = authority["expires_ms"]
        self.refused(exec_admit, env, op, authority, code=6)

    def test_ingress_exact_length_and_digest_before_object_use(self):
        body = b"synthetic git bundle bytes"
        record = {"object_id": "object-a", "op": "repo_bundle", "bytes": len(body), "sha256": digest(body)}
        env, operation, _ = good_exec()
        self.assertEqual(ingress_admit(body, record, operation, env), body)
        for bad in (body[:-1], body + b"x", b"X" + body[1:]):
            self.refused(ingress_admit, bad, record, operation, env)
        operation["object_id"] = "other-object"
        self.refused(ingress_admit, body, record, operation, env)
        operation["object_id"] = "object-a"
        operation["byte_limit"] = len(body) - 1
        self.refused(ingress_admit, body, record, operation, env)
        record["op"] = "shell"
        self.refused(ingress_admit, body, record, operation, env)

    def test_ingress_header_and_object_share_one_budget(self):
        env, operation, authority = good_exec()
        body = b"bounded input"
        record = {"object_id": operation["object_id"], "op": operation["op"],
                  "bytes": len(body), "sha256": digest(body)}
        operation["byte_limit"] = 4096
        header = len(canonical({"envelope": env, "operation": operation}))
        operation["byte_limit"] = header
        self.refused(ingress_admit, body, record, operation, env)

    def test_exec_actual_control_bytes_and_authority_types(self):
        env, operation, authority = good_exec()
        operation["byte_limit"] = 1
        self.refused(exec_admit, env, operation, authority)
        for key, value in (("max_bytes", 8192.0), ("epoch", True), ("ui_granted", 1)):
            env, operation, authority = good_exec()
            authority[key] = value
            self.refused(exec_admit, env, operation, authority, code=2)

    def test_ui_one_shot_replay_returns_ack_without_second_effect(self):
        env, op, authority = good_exec("gate", "ui_action")
        grant = {"sequence": 7, "operation_sha256": digest(canonical({"envelope": env, "operation": op})), "expires_ms": 50}
        ledger = ExecLedger()
        ack = ledger.perform(env, op, authority, grant)
        self.assertEqual(ack, ledger.perform(env, op, authority, grant))
        self.assertEqual(ledger.effect_count, 1)
        grant["sequence"] += 1
        self.refused(ledger.perform, env, op, authority, grant, code=6)

    def test_ui_consumption_crash_or_mutated_action_is_not_replayed(self):
        env, op, authority = good_exec("gate", "ui_action")
        grant = {"sequence": 7, "operation_sha256": digest(canonical({"envelope": env, "operation": op})), "expires_ms": 50}
        ledger = ExecLedger()
        self.refused(ledger.perform, env, op, authority, grant, True, code=6)
        self.refused(ledger.perform, env, op, authority, grant, code=6)
        self.assertEqual(ledger.effect_count, 0)
        op["payload"]["x"] += 1
        self.refused(ExecLedger().perform, env, op, authority, grant, code=6)

    def test_ui_text_control_bounds_and_pixel_relay_refused(self):
        env, op, authority = good_exec("gate", "ui_action")
        self.assertEqual(exec_admit(env, op, authority), "ui_action")
        for payload in ({"pixels": "encoded frame"}, {"event": "click", "x": 32768, "y": 0, "key": None},
                        {"event": "key", "x": 0, "y": 0, "key": "shell"}):
            bad = copy.deepcopy(op)
            bad["payload"] = payload
            self.refused(exec_admit, env, bad, authority)
        env, op, authority = good_exec("status", "ui_status")
        op["payload"] = {"want": "xpra-stream"}
        self.refused(exec_admit, env, op, authority)

    def test_extract_full_message_evidence_and_existing_record(self):
        authority, bodies = good_export()
        wire = export_archive(authority, bodies)
        record = extract_admit(wire, authority, 102400)
        self.assertEqual(record["sha256"], digest(wire))
        self.assertEqual(record["entries"], 4)
        self.assertEqual(set(record), {"method", "entries", "bytes", "text_only", "mode", "owner_asserted", "staging_residue", "sha256"})
        self.assertEqual(record["bytes"], sum(len(data) for _, data in tar_members(wire, authority["max_wire_bytes"])))

    def test_docker_null_guest_pins_and_partial_null_refusal(self):
        authority, bodies = good_export()
        for key in ("guest_rootfs", "kernel", "vmm"):
            authority["pins"][key] = None
        self.assertTrue(extract_admit(export_archive(authority, bodies), authority, 102400)["text_only"])
        authority["pins"]["kernel"] = digest(b"partial guest")
        self.refused(extract_admit, export_archive(authority, bodies), authority, 102400, code=2)

    def test_extract_omitted_evidence_self_consistent_guest_manifest_fails(self):
        authority, bodies = good_export()
        guest = copy.deepcopy(authority)
        guest["required_files"] = guest["required_files"][1:]
        self.refused(extract_admit, export_archive(guest, bodies), authority, 102400)
        value = parse_json(bodies["evidence.json"])
        value["answers"] = value["answers"][:1]
        bodies["evidence.json"] = canonical(value)
        reseal_export(authority, bodies)
        self.refused(extract_admit, export_archive(authority, bodies), authority, 102400)

    def test_extract_missing_message_field_fails_even_with_new_hash(self):
        authority, bodies = good_export()
        message = parse_json(bodies["message.json"])
        del message["seq"]
        bodies["message.json"] = canonical(message)
        reseal_export(authority, bodies)
        self.refused(extract_admit, export_archive(authority, bodies), authority, 102400)
        for key, value in (("affected", [{}]), ("affected", ["x" * 1025]), ("seq", True), ("body", [])):
            authority, bodies = good_export()
            message = parse_json(bodies["message.json"])
            message[key] = value
            bodies["message.json"] = canonical(message)
            reseal_export(authority, bodies)
            self.refused(extract_admit, export_archive(authority, bodies), authority, 102400)

    def test_extract_rehashed_preview_fails_independent_source_seal(self):
        authority, bodies = good_export()
        message = parse_json(bodies["message.json"])
        message["body"] = "preview"
        bodies["message.json"] = canonical(message)
        self.refused(extract_admit, export_archive(authority, bodies), authority, 102400)
        reseal_export(authority, bodies)  # a newly committed source, not a transport rewrite
        self.assertTrue(extract_admit(export_archive(authority, bodies), authority, 102400)["text_only"])

    def test_extract_wave_and_bool_epoch_are_bound(self):
        authority, bodies = good_export()
        message = parse_json(bodies["message.json"])
        message["wave_id"] = "foreign-wave"
        bodies["message.json"] = canonical(message)
        reseal_export(authority, bodies)
        self.refused(extract_admit, export_archive(authority, bodies), authority, 102400)
        authority, bodies = good_export()
        authority["binding"]["epoch"] = 1
        guest = copy.deepcopy(authority)
        guest["binding"]["epoch"] = True
        self.refused(extract_admit, export_archive(guest, bodies), authority, 102400, code=2)

    def test_extract_full_tail_digest_not_preview_prefix(self):
        authority, bodies = good_export()
        wire = export_archive(authority, bodies)
        changed = wire.replace(b"complete tail", b"tampered tail")
        self.assertEqual(len(wire), len(changed))
        self.refused(extract_admit, changed, authority, 102400)

    def test_extract_wrong_attempt_epoch_pins_and_commit(self):
        authority, bodies = good_export()
        for key in ("base_image", "cache_manifest", "guest_rootfs"):
            guest = copy.deepcopy(authority)
            guest["pins"][key] = digest(b"wrong pin")
            self.refused(extract_admit, export_archive(guest, bodies), authority, 102400)
        guest = copy.deepcopy(authority)
        guest["binding"]["epoch"] += 1
        self.refused(extract_admit, export_archive(guest, bodies), authority, 102400)

    def test_extract_duplicate_extra_and_traversal_members(self):
        authority, bodies = good_export()
        members = tar_members(export_archive(authority, bodies), authority["max_wire_bytes"])
        for extras in ([members[-1]], [("extra.txt", b"extra")], [("../escape.txt", b"bad")]):
            self.refused(extract_admit, tar_bytes(members + extras), authority, 102400)

    def test_extract_symlink_hardlink_device_pax_gnu_are_refused(self):
        authority, bodies = good_export()
        members = tar_members(export_archive(authority, bodies), authority["max_wire_bytes"])
        for typeflag in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME):
            wire = bytearray(tar_bytes(members))
            wire[156:157] = typeflag
            wire[148:156] = b"        "
            checksum = sum(wire[:512])
            wire[148:156] = f"{checksum:06o}\0 ".encode()
            self.refused(extract_admit, bytes(wire), authority, 102400)

    def test_extract_truncated_tail_and_noncanonical_padding(self):
        authority, bodies = good_export()
        wire = export_archive(authority, bodies)
        for bad in (wire[:-1], wire[:-512], wire + b"\0" * 512, wire + b"x" * 512):
            self.refused(extract_admit, bad, authority, 102400)

    def test_extract_binary_nul_empty_and_raw_kinds(self):
        authority, bodies = good_export()
        for body in (b"", b"abc\0def", b"\xff\xfe"):
            bad = dict(bodies)
            bad["report.md"] = body
            committed = copy.deepcopy(authority)
            reseal_export(committed, bad)
            self.refused(extract_admit, export_archive(committed, bad), committed, 102400)
        for kind in ("raw_log", "frame", "xpra", "binary"):
            bad = copy.deepcopy(authority)
            bad["required_files"][-1]["kind"] = kind
            self.refused(extract_admit, export_archive(bad, bodies), bad, 102400)

    def test_extract_receipt_budget_and_wire_budget_never_truncate(self):
        authority, bodies = good_export()
        bodies["report.md"] = b"x" * 100000
        reseal_export(authority, bodies)
        self.refused(extract_admit, export_archive(authority, bodies), authority, 102400)
        authority, bodies = good_export()
        wire = export_archive(authority, bodies)
        authority["max_wire_bytes"] = len(wire) - 512
        self.refused(extract_admit, wire, authority, 102400)

    def test_repro_identity_all_semantic_pins_and_complete_evidence(self):
        pins = good_pins()
        base = semantic_repro(pins, BOUND_COMMIT, digest(b"gate"), digest(b"evidence"))
        for key in PIN_KEYS:
            changed = dict(pins)
            changed[key] = digest(b"changed pin")
            self.assertNotEqual(base, semantic_repro(changed, BOUND_COMMIT, digest(b"gate"), digest(b"evidence")))
        self.assertNotEqual(base, semantic_repro(pins, BOUND_COMMIT, digest(b"gate"), digest(b"evidence tail")))

    def test_repro_excludes_attempt_tuple_but_transport_binds_it(self):
        authority, bodies = good_export()
        semantic = semantic_repro(authority["pins"], BOUND_COMMIT, digest(b"gate"), digest(b"evidence"))
        changed = copy.deepcopy(authority)
        changed["binding"]["attempt_id"] = "a2"
        self.assertEqual(semantic, semantic_repro(changed["pins"], BOUND_COMMIT, digest(b"gate"), digest(b"evidence")))
        self.refused(extract_admit, export_archive(authority, bodies), changed, 102400)

    def test_child_limits_before_exec_and_effective_values(self):
        child, observed, parent = good_child()
        self.assertTrue(child_admit(child, observed, parent)["start_permitted"])
        observed["effective"]["memory_bytes"] += 1
        self.refused(child_admit, child, observed, parent, code=4)

    def test_child_pid_reuse_attach_race_and_host_bind_refused(self):
        for key, value in (("paused", False), ("attached_before_exec", False)):
            child, observed, parent = good_child()
            observed[key] = value
            self.refused(child_admit, child, observed, parent, code=6)
        child, observed, parent = good_child()
        observed["process"]["start_time"] += 1
        self.refused(child_admit, child, observed, parent, code=6)
        child, observed, parent = good_child()
        child["binds"] = ["/sys/fs/cgroup"]
        self.refused(child_admit, child, observed, parent)

    def test_child_ancestor_memory_pids_and_cpuset_budgets(self):
        for key, value in (("memory_bytes", 800), ("pids", 20), ("cpuset", [5])):
            child, observed, parent = good_child()
            parent[key] = value
            self.refused(child_admit, child, observed, parent)

    def test_child_cpu_quota_effective_match_and_rational_ancestor_bound(self):
        child, observed, parent = good_child()
        observed["effective"]["quota_us"] += 1
        self.refused(child_admit, child, observed, parent, code=4)
        child, observed, parent = good_child()
        parent["quota_us"] = 149999
        self.refused(child_admit, child, observed, parent)
        parent["quota_us"] = 150000
        self.assertTrue(child_admit(child, observed, parent)["start_permitted"])
        # Equivalent rational parent with a different period; no float conversion.
        parent["quota_us"], parent["period_us"] = 300000, 200000
        self.assertTrue(child_admit(child, observed, parent)["start_permitted"])
        for key, value in (("period_us", 0), ("quota_us", True), ("weight", 10001)):
            child, observed, parent = good_child()
            child["limits"][key] = value
            self.refused(child_admit, child, observed, parent, code=2)

    def test_purge_verified_commit_and_exactly_once_release(self):
        ledger = PurgeLedger()
        owner, proof = good_purge()
        ledger.begin(owner)
        ledger.observe(proof)
        self.assertTrue(ledger.commit(owner))
        self.assertFalse(ledger.commit(owner))
        self.assertEqual(ledger.release_count, 1)
        self.assertFalse(ledger.reservation_held)
        self.assertFalse(ledger.outer_reservation_held)

    def test_purge_missing_physical_proof_holds_reservation(self):
        for key in ("remaining_pids", "remaining_workspace_bytes", "listeners", "ram_roots", "containers", "host_rules",
                    "pending_launch_intents"):
            ledger = PurgeLedger()
            owner, proof = good_purge()
            ledger.begin(owner)
            proof[key] = 1
            ledger.observe(proof)
            self.refused(ledger.commit, owner, code=6)
            self.assertTrue(ledger.reservation_held)
            self.assertEqual(ledger.release_count, 0)

    def test_purge_crash_replay_retains_then_releases_once(self):
        ledger = PurgeLedger()
        owner, proof = good_purge()
        ledger.begin(owner)
        ledger.observe(proof)
        self.refused(ledger.commit, owner, True, code=6)
        self.assertEqual(ledger.state, "INDETERMINATE")
        self.assertTrue(ledger.reservation_held)
        ledger.begin(owner)
        self.refused(ledger.commit, owner, code=6)
        ledger.observe(proof)
        self.assertTrue(ledger.commit(owner))
        self.assertFalse(ledger.commit(owner))

    def test_purge_owner_epoch_process_and_late_collection_fences(self):
        for key, value in (("epoch", 4), ("checked_processes", []), ("fenced", False),
                           ("collection_frozen", False), ("extract_status", "pending"),
                           ("owner_epoch", 3), ("cohort_generation", 2), ("daemon_id", "other-daemon"),
                           ("container_id", "other-container")):
            ledger = PurgeLedger()
            owner, proof = good_purge()
            ledger.begin(owner)
            proof[key] = value
            self.refused(ledger.observe, proof, code=6)
            self.assertTrue(ledger.reservation_held)

    def test_purge_failed_refresh_invalidates_previously_good_proof(self):
        ledger = PurgeLedger()
        owner, proof = good_purge()
        ledger.begin(owner)
        ledger.observe(proof)
        stale = copy.deepcopy(proof)
        stale["owner_epoch"] += 1
        self.refused(ledger.observe, stale, code=6)
        self.refused(ledger.commit, owner, code=6)
        ledger.observe(proof)
        self.assertTrue(ledger.commit(owner))
        ledger = PurgeLedger()
        owner, proof = good_purge()
        ledger.begin(owner)
        proof["outer_containers"] = False
        self.refused(ledger.observe, proof, code=2)

    def test_child_stop_does_not_release_shared_outer_cohort(self):
        ledger = PurgeLedger()
        owner, proof = good_purge("child", 1)
        ledger.begin(owner)
        ledger.observe(proof)
        self.assertTrue(ledger.commit(owner))
        self.assertTrue(ledger.outer_reservation_held)
        ledger = PurgeLedger()
        owner, proof = good_purge("cohort", 1)
        ledger.begin(owner)
        ledger.observe(proof)
        self.refused(ledger.commit, owner, code=6)

    def test_trace_reserves_setup_extract_and_purge(self):
        self.assertEqual(trace_admit(*good_trace()), 0)
        trace, reservation = good_trace()
        reservation["stage_caps_ms"]["purge"] = 200
        self.refused(trace_admit, trace, reservation)

    def test_offline_cache_miss_still_extracts_and_purges(self):
        self.assertEqual(trace_admit(*good_trace(True)), 4)
        trace, reservation = good_trace(True)
        trace[5]["outcome"] = "ok"
        self.refused(trace_admit, trace, reservation)

    def test_trace_refuses_per_commit_build_and_unreserved_ram(self):
        trace, reservation = good_trace()
        trace[0]["stage"] = "image_build"
        self.refused(trace_admit, trace, reservation)
        trace, reservation = good_trace()
        reservation["frames_bytes"] += 1
        self.refused(trace_admit, trace, reservation)

    def test_trace_caps_and_integer_only_samples(self):
        for key, value in (("duration_ms", 101), ("peak_ram_bytes", 1025), ("duration_ms", 1.5)):
            trace, reservation = good_trace()
            trace[0][key] = value
            self.refused(trace_admit, trace, reservation)

    def test_normalized_exit_set_never_exposes_one(self):
        self.assertNotIn(1, ALLOWED_EXITS)
        for code in (2, 3, 4, 5, 6, 7):
            self.assertEqual(Refused("synthetic", code).code, code)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--selftest-fail"]):
        print("usage: test_w8d_c1_lifecycle.py [--selftest-fail]", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(C1LifecycleTests)
    if args:
        def intentional_failure():
            raise AssertionError("intentional C1 CLI failure-mapping fixture")
        suite.addTest(unittest.FunctionTestCase(intentional_failure))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 5


if __name__ == "__main__":
    sys.exit(main())
