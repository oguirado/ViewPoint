"""Trusted scope waiter and network namespace entry point."""

import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import re
import secrets
import time
import sys


PROBE_MESSAGE = "QUAL_FETCH_DENIED"
GPU_SCOPE_LIMITS = {"high": 22 * 1024**3, "maximum": 24 * 1024**3, "swap": 0}
GPU_UNIT = re.compile(r"af-job-[0-9a-f]{32}\.service", re.ASCII)
MAX_HANDOFF_AGE_NS = 30 * 10**9
SOCKET_MESSAGE_LIMIT = 4096


def validate_gpu_scope(scope, unit):
    if (type(scope) is not dict or set(scope) != {
                "cgroup_path", "limits", "cpu_affinity", "cpuset_cpus_effective", "nice", "pid", "start_ticks"} or
            type(unit) is not str or GPU_UNIT.fullmatch(unit) is None or
            type(scope["cgroup_path"]) is not str or not scope["cgroup_path"].startswith("/") or
            ".." in Path(scope["cgroup_path"]).parts or Path(scope["cgroup_path"]).name != unit or
            type(scope["limits"]) is not dict or
            any(type(value) is not int for value in scope["limits"].values()) or
            scope["limits"] != GPU_SCOPE_LIMITS or type(scope["cpu_affinity"]) is not list or
            any(type(cpu) is not int for cpu in scope["cpu_affinity"]) or
            scope["cpu_affinity"] != list(range(16)) or
            scope["cpuset_cpus_effective"] != "0-15" or
            type(scope["pid"]) is not int or scope["pid"] <= 1 or
            type(scope["start_ticks"]) is not int or scope["start_ticks"] <= 0 or
            type(scope["nice"]) is not int or scope["nice"] != 15):
        raise RuntimeError("gpu_scope_unproven")
    return scope


def read_peer_identity(pid):
    process = Path("/proc") / str(pid)

    def start_ticks():
        return int((process / "stat").read_text().rsplit(")", 1)[1].split()[19])

    started = start_ticks()
    matches = [line[3:] for line in (process / "cgroup").read_text().splitlines()
               if line.startswith("0::/")]
    if len(matches) != 1 or ".." in Path(matches[0]).parts or start_ticks() != started:
        raise RuntimeError("gpu_scope_unproven")
    return matches[0], started


def gpu_scope():
    pid = os.getpid()
    relative, started = read_peer_identity(pid)
    cgroup = Path("/sys/fs/cgroup") / relative.lstrip("/")
    limits = {key: int((cgroup / ("memory." + setting)).read_text())
              for key, setting in (("high", "high"), ("maximum", "max"), ("swap", "swap.max"))}
    return validate_gpu_scope({
        "cgroup_path": relative, "limits": limits,
        "cpuset_cpus_effective": (cgroup / "cpuset.cpus.effective").read_text().strip(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)), "nice": os.getpriority(os.PRIO_PROCESS, 0),
        "pid": pid, "start_ticks": started,
    }, cgroup.name)


def publish_scope(path, evidence):
    staging = path.with_suffix(".pending")
    with staging.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
    staging.replace(path)


def wait_json(path, deadline):
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("gpu_authority_unavailable")
        time.sleep(0.01)
    if time.monotonic() >= deadline:
        raise RuntimeError("gpu_authority_unavailable")
    with path.open("rb") as stream:
        data = stream.read(SOCKET_MESSAGE_LIMIT + 1)
    if len(data) > SOCKET_MESSAGE_LIMIT:
        raise RuntimeError("gpu_authority_invalid")
    return json.loads(data)


def redeem_authority(endpoint, permit_sha256, deadline):
    nonce = secrets.token_hex(16)
    request = {"nonce": nonce, "permit_sha256": permit_sha256}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("gpu_authority_unavailable")
        client.settimeout(min(remaining, 5))
        client.connect(endpoint)
        client.sendall(json.dumps(request).encode())
        with client.makefile("rb") as stream:
            data = stream.read(SOCKET_MESSAGE_LIMIT + 1)
    if len(data) > SOCKET_MESSAGE_LIMIT:
        raise RuntimeError("gpu_authority_invalid")
    response = json.loads(data)
    if type(response) is not dict or type(response.get("rc")) is not int:
        raise RuntimeError("gpu_authority_invalid")
    if response["rc"] != 0:
        raise RuntimeError(response.get("reason", "gpu_authority_invalid"))
    if response.get("nonce") != nonce or response.get("permit_sha256") != permit_sha256:
        raise RuntimeError("gpu_authority_binding_changed")


def start_gpu_scope(folder, timeout_s, command):
    folder = Path(folder)
    evidence = gpu_scope()
    publish_scope(folder / "gpu-scope.json", evidence)
    deadline = time.monotonic() + int(timeout_s)
    challenge = wait_json(folder / "gpu-challenge.json", deadline)
    permit_sha256 = challenge["permit_sha256"]
    redeem_authority(challenge["socket"], permit_sha256, deadline)
    handoff = wait_json(folder / "gpu-authority.json", deadline)
    if handoff["permit_sha256"] != permit_sha256:
        raise RuntimeError("gpu_authority_binding_changed")
    observed = gpu_scope()
    if observed != evidence:
        raise RuntimeError("gpu_scope_changed")
    publish_scope(folder / "gpu-scope-exec.json", observed)
    if not handoff["authorized_monotonic_ns"] <= time.monotonic_ns() <= handoff["expires_monotonic_ns"]:
        raise RuntimeError("gpu_exec_stale")
    os.execv(command[0], command)


def establish_namespace(caller_inode):
    inode = os.stat("/proc/self/ns/net").st_ino
    if inode == caller_inode:
        raise RuntimeError("offline_namespace_unproven")
    devices = Path("/proc/self/net/dev").read_text().splitlines()[2:]
    interfaces = sorted(row.partition(":")[0].strip() for row in devices)
    ipv4 = Path("/proc/self/net/route").read_text().splitlines()
    ipv6 = Path("/proc/self/net/ipv6_route").read_text().splitlines()
    # The fresh namespace may list the IPv6 reject routes via loopback.
    only_reject_routes = all(
        row.split()[-1] == "lo" and int(row.split()[-2], 16) & 0x200 for row in ipv6
    )
    with socket.socket() as sock:
        reply = fcntl.ioctl(sock.fileno(), 0x8913, struct.pack("256s", b"lo"))  # SIOCGIFFLAGS
    loopback_up = bool(struct.unpack_from("H", reply, 16)[0] & 1)
    ipv4_empty = not ipv4 or (len(ipv4) == 1 and ipv4[0].startswith("Iface"))
    if interfaces != ["lo"] or loopback_up or not ipv4_empty or not only_reject_routes:
        raise RuntimeError("offline_namespace_unproven")
    return {"inode": inode, "interfaces": interfaces, "loopback_up": False, "routes": False}


def fetch_probe():
    with socket.socket() as sock:
        try:
            # RFC 5737 TEST-NET-1. The inherited kernel filter denies connect.
            sock.connect(("192.0.2.1", 443))
        except OSError as exc:
            if exc.errno == errno.EACCES:
                print(PROBE_MESSAGE)
                return 0
    return 5


def main(argv):
    if argv[:1] == ["gpu-scope"]:
        return start_gpu_scope(argv[1], argv[2], argv[3:])
    if argv == ["probe"]:
        return fetch_probe()
    caller_inode, destination, command, *args = argv
    evidence = establish_namespace(int(caller_inode))
    with Path(destination).open("x") as stream:
        json.dump(evidence, stream, sort_keys=True)
    os.execv(command, [command, *args])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
