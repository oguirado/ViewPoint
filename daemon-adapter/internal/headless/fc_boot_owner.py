"""The fixed-cgroup MAIN owner and client for the frozen Tools service."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
from types import ModuleType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from jsonschema import ValidationError

from fc_boot import (BOOT_ID, Boot, Contract, GRANT_BINDING, Refusal,
                     canonical, decode, digest, hash_bytes, reject, selected,
                     signing_bytes, verify_signature)

OWNER_UID = 1000
OWNER = "viewpoint-main"
OWNER_UNIT = "mv-fcbootl-owner.scope"
OWNER_SLICE = "newton-ia.slice"
OWNER_CGROUP = "/user.slice/user-1000.slice/user@1000.service/newton.slice/newton-ia.slice/" + OWNER_UNIT
TOOLS_SOCKET = "TOOLS_SOCKET"
MAX_FRAME = 256 * 1024


def ram_admission(unit):
    gib = 1 << 30
    available = int(next(row.split()[1] for row in Path("/proc/meminfo").read_text().splitlines()
                         if row.startswith("MemAvailable:"))) * 1024
    reserved = 0
    for limit in Path("/sys/fs/cgroup").rglob("*.scope/memory.max"):
        try:
            if limit.read_text().strip() != str(8 * gib):
                continue
            current = int((limit.parent / "memory.current").read_text())
        except FileNotFoundError:
            continue
        if current >= 256 << 20:
            reserved += max(0, 8 * gib - current)
    remaining = available - reserved - 8 * gib
    record = {"scope": unit, "mem_available_bytes": available, "live_scope_reserve_bytes": reserved,
              "new_scope_bytes": 8 * gib, "remaining_bytes": remaining, "floor_bytes": 20 * gib}
    print("RAM_ADMISSION " + json.dumps(record), file=sys.stderr, flush=True)
    if remaining < 20 * gib:
        raise Refusal("RAM_ADMISSION_REFUSED")


def cgroup(pid="self"):
    rows = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    groups = [row[3:] for row in rows if row.startswith("0::/")]
    if len(groups) != 1:
        raise PermissionError("unverified cgroup")
    return groups[0]


def require_owner():
    if os.getuid() != OWNER_UID or os.geteuid() != OWNER_UID or cgroup() != OWNER_CGROUP:
        raise PermissionError("MAIN must use the provisioned owner UID and scope")


@contextmanager
def owned():
    require_owner()
    lock = Path(f"/run/user/{OWNER_UID}") / (OWNER_UNIT + ".lock")
    with os.fdopen(os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != OWNER_UID
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("unsafe owner lock")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def launch_owner(command):
    # The explicit check rejects an already loaded unit. StartTransientUnit
    # independently rejects a same-name race between this check and launch.
    state = subprocess.run(["systemctl", "--user", "show", OWNER_UNIT,
                            "--property=LoadState", "--value"], capture_output=True, check=True)
    if state.stdout.strip() != b"not-found":
        raise PermissionError(f"{OWNER_UNIT} already exists")
    ram_admission(OWNER_UNIT)
    return subprocess.call(["systemd-run", "--user", "--scope", f"--unit={OWNER_UNIT}",
                            f"--slice={OWNER_SLICE}", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G",
                            "--", "taskset", "-c", "0-15", "env", "-u", "DISPLAY",
                            "-u", "WAYLAND_DISPLAY", *command])


class SocketTools:
    def __init__(self, path, uid, group):
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or type(uid) is not int or not 0 <= uid < (1 << 32) - 1 or uid == OWNER_UID
                or not isinstance(group, str) or not group.startswith("/") or "\n" in group):
            raise ValueError("invalid Tools socket or peer pin")
        self.path = path
        self.peer = (uid, group)

    def _authenticate(self, channel):
        pid, uid, _ = struct.unpack("iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        fd = channel.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERPIDFD", 77))
        try:
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if pid <= 0 or poll.poll(0) or (uid, cgroup(pid)) != self.peer or poll.poll(0):
                reject("TOOLS_PEER_MISMATCH")
        finally:
            os.close(fd)

    def _exchange(self, frame):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(5)
            channel.connect(self.path)
            self._authenticate(channel)
            channel.sendall(frame)
            with channel.makefile("rb") as stream:
                reply = stream.readline(MAX_FRAME + 1)
                if not reply or (len(reply) < MAX_FRAME and not reply.endswith(b"\n")):
                    raise ConnectionError("incomplete Tools reply")
                if len(reply) > MAX_FRAME or not reply.endswith(b"\n") or stream.read(1):
                    reject("INVALID_TOOLS_REPLY")
            try:
                value = decode(reply[:-1])
                canonical(value)
            except (ValueError, RecursionError):
                reject("INVALID_TOOLS_REPLY")
            if type(value) is not dict:
                reject("INVALID_TOOLS_REPLY")
            if "code" in value:
                if set(value) != {"outcome", "code"} or any(
                        not isinstance(value[k], str) or not value[k] for k in value):
                    reject("INVALID_TOOLS_REPLY")
                reject(value["code"])
            return value

    def call(self, op, body):
        require_owner()
        frame = canonical({"op": op, "body": body}) + b"\n"
        if len(frame) > MAX_FRAME:
            reject("INVALID_REQUEST")
        try:
            return self._exchange(frame)
        except (ConnectionError, TimeoutError):
            # A lost reply can follow a durable decision. Replay the same bytes.
            try:
                return self._exchange(frame)
            except OSError as error:
                raise Refusal("GRANT_UNAVAILABLE") from error
        except OSError as error:
            raise Refusal("GRANT_UNAVAILABLE") from error

    def paired_open(self, descriptor):
        reply = self.call("paired-open", descriptor)
        if reply != {"outcome": "open", "reservation": descriptor}:
            reject("RESERVATION_CLOSED" if reply.get("reservation") == descriptor
                   and reply.get("outcome") in ("closing", "closed") else "INVALID_TOOLS_REPLY")

    def paired_close(self, reservation_id):
        body = {"reservation_id": reservation_id}
        reply = self.call("paired-close", body)
        if reply not in ({"outcome": "closing", **body}, {"outcome": "closed", **body}):
            reject("INVALID_TOOLS_REPLY")
        return reply["outcome"]


class SocketGrant:
    """A verified admission and its claim/release operations."""

    boot_owner = OWNER

    def __init__(self, client, contract, admission, trust, reservation_id=None):
        self.client = client
        self.psi_sha256 = digest(trust["psi_policy"])
        self.guardian_sha256 = admission["profile"]["guardian_policy_sha256"]
        self.minimum_ram = admission["requested_ram_bytes"]
        if reservation_id is None:
            grant = client.call("grant", admission)
        else:
            grant = client.call("paired-draw", {"reservation_id": reservation_id, "request": admission})
        try:
            contract.validate("admission_grant", grant)
            verify_signature(grant, *trust["tools_key"])
        except (ValueError, ValidationError, InvalidSignature) as error:
            raise Refusal("GRANT_INVALID") from error
        expected = {**selected(admission, ("host_id", "boot_id", "principal_id", "backend", "profile_sha256")),
                    "admission_request_id": admission["request_id"],
                    "priority_class": admission["profile"]["priority_class"]}
        if selected(grant, expected) != expected:
            reject("GRANT_MISMATCH")
        self.grant = grant
        self.expected = selected(grant, GRANT_BINDING)
        self.admission_ref = grant["receipt_ref"]

    def claim(self, call):
        return self.client.call("claim", call)

    def release(self, witness):
        return self.client.call("release", witness)


def run_attempt(client, contract, trust, job, root, adapter, reservation_id=None):
    admission = contract.validate("admission_request", job["admission"])
    profile = admission["profile"]
    if (admission["host_id"] != trust["host_id"] or admission["boot_id"] != BOOT_ID.read_text().strip()
            or admission["principal_id"] != OWNER or admission["backend"] != profile["backend"]
            or admission["profile_sha256"] != digest(profile)):
        reject("GRANT_MISMATCH")
    authority = SocketGrant(client, contract, admission, trust, reservation_id)
    request = {"schema": "vp.boot_request/v1", "request_id": job["create_request_id"],
               "op": "create", "profile": profile, "grant": authority.grant}
    contract.validate("boot_request", request)
    journal = root / authority.grant["attempt_id"]
    boot = Boot(contract, authority, None, journal, job["stage_identity"],
                {"tools": trust["tools_key"], "main": trust["main_key"]}, trust["sign_witness"],
                authority=TOOLS_SOCKET, psi_mode=trust["psi_policy"]["mode"], accepted_ttl_ns=trust["ttl_ns"],
                prepare_runtime=lambda: adapter(job, authority.grant))
    boot.write("admission.json", admission)
    boot.write("request.json", request)
    return boot.run(canonical(request))


def run_pair(client, contract, trust, descriptor, jobs, root, adapter):
    contract.validate_pair(descriptor)
    if ((descriptor["host_id"], descriptor["boot_id"]) != (trust["host_id"], BOOT_ID.read_text().strip())
            or descriptor["design"] != "A/A" or len(jobs) != 2
            or jobs[0]["admission"]["request_id"] == jobs[1]["admission"]["request_id"]
            or jobs[0]["stage_identity"]["run_id"] == jobs[1]["stage_identity"]["run_id"]
            or any(job["admission"]["profile_sha256"] != descriptor["profile_sha256"]
                   or selected(job["stage_identity"], ("arm", "cell_id", "block_id"))
                   != {"arm": "A", **selected(descriptor, ("cell_id", "block_id"))} for job in jobs)):
        reject("INVALID_REQUEST")
    reservation_id = descriptor["reservation_id"]
    client.paired_open(descriptor)
    try:
        results = []
        for job in jobs:
            result = run_attempt(client, contract, trust, job, root, adapter, reservation_id)
            results.append(result)
            if result[2] is None:
                break
    finally:
        state = client.paired_close(reservation_id)
    return {"attempts": results, "pair_state": state}


def private_read(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid != OWNER_UID or not 0 < info.st_size <= MAX_FRAME):
            raise PermissionError("unsafe MAIN owner input")
        data = source.read(MAX_FRAME + 1)
        if len(data) != info.st_size or os.fstat(source.fileno()).st_ctime_ns != info.st_ctime_ns:
            raise ValueError("MAIN owner input changed during read")
        return data


def signing_key(issuer, kid, key):
    def sign(value):
        record = {**value, "signer": {"iss": issuer, "kid": kid, "alg": "Ed25519"}}
        signature = key.sign(signing_bytes(record))
        record["signer"]["sig"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return record
    return (issuer, kid, key.public_key()), sign


def run_config(config):
    require_owner()
    if config["mode"] != TOOLS_SOCKET:
        raise ValueError("owner configuration must select TOOLS_SOCKET")
    root = Path(config["journal_root"])
    info = root.lstat()
    if (root.resolve(strict=True) != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != OWNER_UID or stat.S_IMODE(info.st_mode) != 0o700):
        raise PermissionError("a canonical private journal root is required")
    socket_pin, tools, main = (config[name] for name in ("socket", "tools_signer", "main_signer"))
    client = SocketTools(socket_pin["path"], socket_pin["uid"], socket_pin["cgroup"])
    public_key = load_pem_public_key(private_read(tools["key_path"]))
    private_key = load_pem_private_key(private_read(main["key_path"]), password=None)
    if not isinstance(public_key, Ed25519PublicKey) or not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Ed25519 signer pins required")
    main_key, sign = signing_key(main["iss"], main["kid"], private_key)
    policy = decode(private_read(config["psi_policy_path"]))
    if digest(policy) != config["psi_policy_sha256"] or policy["mode"] not in {"shadow", "enforce"}:
        raise ValueError("invalid Tools PSI policy pin")
    ttl_ns = config["accepted_grant_ttl_ns"]
    if not ttl_ns or any(type(value) is not int or value <= 0 for value in ttl_ns):
        raise ValueError("approved grant TTLs required")
    trust = {"host_id": config["host_id"], "ttl_ns": tuple(ttl_ns), "psi_policy": policy,
             "tools_key": (tools["iss"], tools["kid"], public_key), "main_key": main_key, "sign_witness": sign}
    contract = Contract(config["contract_root"])
    adapter_pin = config["adapter"]
    source = Path(adapter_pin["path"]).read_bytes()
    if hash_bytes(source) != adapter_pin["sha256"]:
        raise ValueError("runtime and observer adapter pin differs")
    module = ModuleType("fc_boot_adapter")
    module.__file__ = adapter_pin["path"]
    exec(compile(source, module.__file__, "exec"), vars(module))
    result = {"single": run_attempt(client, contract, trust, config["single"], root, module.prepare)}
    if config["pair"] is not None and result["single"][2] is not None:
        descriptor, jobs = config["pair"]["reservation"], config["pair"]["jobs"]
        result["pair"] = run_pair(client, contract, trust, descriptor, jobs, root, module.prepare)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--inside-owner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not args.inside_owner:
            return launch_owner([sys.executable, "-B", str(Path(__file__).resolve()),
                                 "--inside-owner", "--config", str(Path(args.config).resolve())])
        with owned():
            result = run_config(decode(private_read(args.config)))
            print(canonical(result).decode())
            attempts = [result["single"], *result.get("pair", {}).get("attempts", [])]
            return int(any(attempt[0]["outcome"] != "known" for attempt in attempts)
                       or ("pair" in result and result["pair"]["pair_state"] != "closed"))
    except (OSError, ValueError, KeyError, ValidationError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
