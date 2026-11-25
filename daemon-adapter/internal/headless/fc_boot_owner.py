"""The MAIN boot owner and same-user client for the frozen Tools service."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time
from types import ModuleType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from jsonschema import ValidationError

from fc_boot import (BINDING, BOOT_ID, Boot, Contract, GRANT_BINDING, Refusal, WITNESS_BINDING,
                     canonical, decode, digest, hash_bytes, reject, selected,
                     signing_bytes, sync_directory, verify_signature)

OWNER = "local-user"
OWNER_SLICE = "newton-ia.slice"
TOOLS_SOCKET = "TOOLS_SOCKET"
MAX_FRAME = 256 * 1024
OPERATIONS = {"grant", "claim", "release", "paired-open", "paired-draw", "paired-close", "tombstone"}
RECONCILE_CODES = {"REPLAY_SUPERSEDED", "TRANSACTION_ID_CONFLICT"}


class OutcomeUnknown(Exception):
    """Tools may have committed the pending operation."""

    def __init__(self, transaction_id):
        self.reply = {"outcome": "unknown", "code": "OUTCOME_UNKNOWN", "transaction_id": transaction_id}
        super().__init__("OUTCOME_UNKNOWN")


class ReconciliationRequired(Exception):
    """A definitive response fenced the transaction and its journal."""

    def __init__(self, reply):
        self.reply = reply
        super().__init__(reply["code"])


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


@contextmanager
def owned(root):
    lock = root / "owner.lock"
    with os.fdopen(os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("unsafe owner lock")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def launch_owner(command):
    unit = f"mv-fcbootl-{secrets.token_hex(8)}.scope"
    ram_admission(unit)
    return subprocess.call(["systemd-run", "--user", "--scope", f"--unit={unit}",
                            f"--slice={OWNER_SLICE}", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G",
                            "--", "taskset", "-c", "0-15", "env", "-u", "DISPLAY",
                            "-u", "WAYLAND_DISPLAY", *command])


def default_socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "viewpoint-tools/fcbootl.sock"
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "viewpoint-tools/fcbootl/fcbootl.sock"


class SocketTools:
    def __init__(self, root, contract, path=None):
        self.path = default_socket_path() if path is None else Path(path)
        if not self.path.is_absolute() or os.getuid() != os.geteuid():
            raise ValueError("a same-user Tools socket with an absolute path is required")
        self.contract = contract
        self.pending = root / "pending-tools.json"

    def _validate_reply(self, reply, request):
        op, body, tx = (request[field] for field in ("op", "body", "transaction_id"))
        if not isinstance(reply, dict):
            raise ValueError("invalid Tools reply")
        outcome = reply.get("outcome")
        if "code" in reply:
            if reply["code"] == "OUTCOME_UNKNOWN":
                if reply != OutcomeUnknown(tx).reply:
                    raise ValueError("uncorrelated Tools reply")
                return
            if isinstance(outcome, str) and isinstance(reply["code"], str) and reply["code"]:
                return
        elif op in {"grant", "paired-draw"}:
            self.contract.validate("admission_grant", reply)
            admission = body if op == "grant" else body["request"]
            fields = ("host_id", "boot_id", "principal_id", "backend", "profile_sha256")
            if (reply["admission_request_id"] == admission["request_id"]
                    and selected(reply, fields) == selected(admission, fields)):
                return
        elif op in {"release", "tombstone"}:
            self.contract.validate("release_receipt", reply)
            fields = WITNESS_BINDING if op == "release" else ("host_id", "boot_id", *BINDING)
            if selected(reply, fields) == selected(body, fields):
                return
        elif op == "claim":
            expiry = reply.get("expires_mono_ns")
            if (outcome in ("claimed", "already_claimed") and isinstance(expiry, str)
                    and re.fullmatch("[0-9]+", expiry)
                    and reply == {"outcome": outcome, **body, "expires_mono_ns": expiry}):
                return
        elif outcome in ("closing", "closed", "open"):
            if op == "paired-open" and reply == {"outcome": outcome, "reservation": body}:
                return
            if op == "paired-close" and outcome != "open" and reply == {"outcome": outcome, **body}:
                return
        raise ValueError("invalid or uncorrelated Tools reply")

    def _exchange(self, frame, request):
        may_have_sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(5)
                channel.connect(str(self.path))
                _, uid, _ = struct.unpack("iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise PermissionError("TOOLS_PEER_MISMATCH")
                may_have_sent = True
                channel.sendall(frame)
                with channel.makefile("rb") as stream:
                    raw = stream.readline(MAX_FRAME + 1)
                    if len(raw) > MAX_FRAME or not raw.endswith(b"\n") or stream.read(1):
                        raise ValueError("invalid Tools reply frame")
                reply = decode(raw[:-1])
                canonical(reply)
                self._validate_reply(reply, request)
                return reply
        except (OSError, ValueError, ValidationError, KeyError, RecursionError) as error:
            if may_have_sent:
                raise OutcomeUnknown(request["transaction_id"]) from error
            raise

    def _persist(self, request):
        with NamedTemporaryFile(dir=self.pending.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(canonical({"socket": str(self.path), "request": request}))
                stream.flush()
                os.fsync(stream.fileno())
                # The owner lock serializes journal updates across processes.
                if self.pending.exists():
                    raise FileExistsError(self.pending)
                temporary.replace(self.pending)
                sync_directory(self.pending.parent)
            finally:
                temporary.unlink(missing_ok=True)

    def _load(self):
        record = decode(private_read(self.pending, limit=MAX_FRAME * 2))
        if record["socket"] != str(self.path):
            raise ValueError("pending transaction belongs to a different Tools socket")
        request = record["request"]
        if (set(request) != {"op", "body", "transaction_id"} or request["op"] not in OPERATIONS
                or not isinstance(request["body"], dict)
                or not re.fullmatch("[0-9a-f]{32}", request["transaction_id"])):
            raise ValueError("invalid pending Tools transaction")
        return request

    def _finish(self):
        self.pending.unlink()
        sync_directory(self.pending.parent)

    def _send(self, request, *, replay):
        frame = canonical(request) + b"\n"
        tx = request["transaction_id"]
        pause = 0.1
        try:
            while True:
                try:
                    reply = self._exchange(frame, request)
                except OSError:
                    if not replay:
                        self._finish()
                        raise
                except OutcomeUnknown:
                    pass
                else:
                    code = reply.get("code")
                    if code != "OUTCOME_UNKNOWN":
                        if code in RECONCILE_CODES:
                            raise ReconciliationRequired(reply)
                        self._finish()
                        if "code" in reply:
                            reject(code)
                        return reply
                replay = True
                time.sleep(pause)
                pause = min(2, pause * 2)
        except KeyboardInterrupt as error:
            raise OutcomeUnknown(tx) from error

    def call(self, op, body):
        if op not in OPERATIONS or not isinstance(body, dict):
            reject("INVALID_REQUEST")
        try:
            request = self._load()
        except FileNotFoundError:
            request = {"op": op, "body": body, "transaction_id": secrets.token_hex(16)}
            if len(canonical(request)) + 1 > MAX_FRAME:
                reject("INVALID_REQUEST")
            self._persist(request)
            return self._send(request, replay=False)
        if request["op"] != op or canonical(request["body"]) != canonical(body):
            raise OutcomeUnknown(request["transaction_id"])
        return self._send(request, replay=True)

    def replay_pending(self):
        return self._send(self._load(), replay=True)

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
        reply = self.client.call("claim", call)
        # Tools replays the original claimed reply for this transaction. An
        # already_claimed reply belongs to an earlier transaction's launch.
        if reply["outcome"] != "claimed":
            reject("GRANT_UNAVAILABLE")
        return reply

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
    except BaseException:
        if not client.pending.exists():
            client.paired_close(reservation_id)
        raise
    return {"attempts": results, "pair_state": client.paired_close(reservation_id)}


def private_read(path, limit=MAX_FRAME):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid != os.getuid() or not 0 < info.st_size <= limit):
            raise PermissionError("unsafe MAIN owner input")
        data = source.read(limit + 1)
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


def run_config(config, *, replay_pending=False):
    if config["mode"] != TOOLS_SOCKET:
        raise ValueError("owner configuration must select TOOLS_SOCKET")
    root = Path(config["journal_root"])
    info = root.lstat()
    if (root.resolve(strict=True) != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise PermissionError("a canonical private journal root is required")
    contract = Contract(config["contract_root"])
    client = SocketTools(root, contract, config.get("socket_path"))
    with owned(root):
        if replay_pending:
            return client.replay_pending()
        if client.pending.exists():
            raise OutcomeUnknown(client._load()["transaction_id"])
        return run_jobs(config, root, contract, client)


def run_jobs(config, root, contract, client):
    tools, main = config["tools_signer"], config["main_signer"]
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
    parser.add_argument("--replay-pending", action="store_true")
    parser.add_argument("--inside-owner", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not args.inside_owner:
            command = [sys.executable, "-B", str(Path(__file__).resolve()),
                       "--inside-owner", "--config", str(Path(args.config).resolve())]
            if args.replay_pending:
                command.append("--replay-pending")
            return launch_owner(command)
        result = run_config(decode(private_read(args.config)), replay_pending=args.replay_pending)
        print(canonical(result).decode())
        if args.replay_pending:
            return 0
        attempts = [result["single"], *result.get("pair", {}).get("attempts", [])]
        return int(any("code" in attempt[0] for attempt in attempts)
                   or ("pair" in result and result["pair"]["pair_state"] != "closed"))
    except OutcomeUnknown as error:
        print(canonical(error.reply).decode(), file=sys.stderr)
        return 4
    except ReconciliationRequired as error:
        print(canonical(error.reply).decode())
        return 1
    except Refusal as error:
        print(canonical({"outcome": "refused", "code": str(error)}).decode())
        return 1
    except (OSError, ValueError, KeyError, ValidationError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
