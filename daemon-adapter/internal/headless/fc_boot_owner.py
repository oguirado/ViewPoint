"""The MAIN boot owner and same-user client for the frozen Tools service."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import time
from types import ModuleType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from jsonschema import ValidationError

from fc_boot import (BINDING, BOOT_ID, Boot, Contract, GRANT_BINDING, Refusal, RuntimeFailure, UnsupportedProfile, WITNESS_BINDING,
                     canonical, decode, digest, hash_bytes, make_witness, reject, selected,
                     signing_bytes, sync_directory, verify_release, verify_signature, write_record)

OWNER = "local-user"
OWNER_SLICE = "newton-ia.slice"
TOOLS_SOCKET = "TOOLS_SOCKET"
MAX_FRAME = 256 * 1024
EXCHANGE_TIMEOUT = 5
MAX_ATTEMPTS = 3
OPERATIONS = {"grant", "claim", "release", "paired-open", "paired-draw", "paired-close", "tombstone"}
RECONCILE_CODES = {"REPLAY_SUPERSEDED", "TRANSACTION_ID_CONFLICT"}
RETRY_CODES = {"OUTCOME_UNKNOWN", "SERVER_BUSY"}
EARLY_BUSY = {"outcome": "refused", "code": "SERVER_BUSY"}


class OutcomeUnknown(Exception):
    """Tools may have committed the pending operation."""

    def __init__(self, transaction_id, recovery=None):
        self.reply = {"outcome": "unknown", "code": "OUTCOME_UNKNOWN", "transaction_id": transaction_id}
        if recovery is not None:
            self.reply.update(retirement=recovery["code"], recovery_required=True)
            if "cause" in recovery:
                self.reply["cause"] = recovery["cause"]
        super().__init__("OUTCOME_UNKNOWN")


class ReconciliationRequired(Exception):
    """A definitive response fenced the transaction and its journal."""

    def __init__(self, reply):
        self.reply = reply
        super().__init__(reply["code"])


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
    # Tools owns workload RAM admission. The owner scope only limits the process.
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
    def __init__(self, root, contract, path=None, *, tier1=False):
        self.path = default_socket_path() if path is None else Path(path)
        if not self.path.is_absolute() or os.getuid() != os.geteuid():
            raise ValueError("a same-user Tools socket with an absolute path is required")
        self.contract = contract
        self.tier1 = tier1
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
                deadline = time.monotonic() + EXCHANGE_TIMEOUT
                channel.settimeout(EXCHANGE_TIMEOUT)
                channel.connect(str(self.path))
                _, uid, _ = struct.unpack("iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise PermissionError("TOOLS_PEER_MISMATCH")
                self._set_remaining_timeout(channel, deadline)
                may_have_sent = True
                try:
                    channel.sendall(frame)
                    send_failed = False
                except OSError:
                    # A saturated service can refuse and close before reading the request.
                    send_failed = True
                raw = bytearray()
                while True:
                    self._set_remaining_timeout(channel, deadline)
                    chunk = channel.recv(min(65536, MAX_FRAME + 1 - len(raw)))
                    if not chunk:
                        break
                    if len(raw) + len(chunk) > MAX_FRAME or raw.endswith(b"\n") or b"\n" in chunk[:-1]:
                        raise ValueError("invalid Tools reply frame")
                    raw.extend(chunk)
                if not raw.endswith(b"\n"):
                    raise ValueError("invalid Tools reply frame")
                reply = decode(raw[:-1])
                canonical(reply)
                if send_failed and reply != EARLY_BUSY:
                    raise ValueError("no definitive early Tools busy reply")
                self._validate_reply(reply, request)
                return reply
        except (OSError, ValueError, ValidationError, KeyError, RecursionError) as error:
            if may_have_sent:
                raise OutcomeUnknown(request["transaction_id"]) from error
            raise

    @staticmethod
    def _set_remaining_timeout(channel, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Tools exchange deadline expired")
        channel.settimeout(remaining)

    def _persist(self, request, *, replace=False):
        # The owner lock serializes journal updates across processes.
        if not replace and os.path.lexists(self.pending):
            raise FileExistsError(self.pending)
        write_record(self.pending, {"socket": str(self.path), "request": request}, replace=True)

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

    def _send(self, request, *, replay, keep_release=False):
        frame = canonical(request) + b"\n"
        tx = request["transaction_id"]
        try:
            for attempt in range(MAX_ATTEMPTS):
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
                    if code not in RETRY_CODES:
                        if code in RECONCILE_CODES:
                            raise ReconciliationRequired(reply)
                        if keep_release:
                            if code == "CUSTODY_UNPROVED":
                                # Tools cached a definite refusal. Recheck using the same witness.
                                self._persist({**request, "transaction_id": secrets.token_hex(16)}, replace=True)
                        elif code is not None or request["op"] != "claim" or reply.get("outcome") != "claimed":
                            self._finish()
                        if code is not None:
                            if self.tier1 and code == "OWNERSHIP_REFUSED" and "next_action" in reply:
                                raise UnsupportedProfile("Tools has not approved the profile digest for Tier 1")
                            reject(code)
                        return reply
                replay = True
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(0.1 * (2 ** attempt))
        except KeyboardInterrupt as error:
            raise OutcomeUnknown(tx) from error
        raise OutcomeUnknown(tx)

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

    def replay_pending(self, *, recover_claim=None, recover_release=None):
        request = self._load()
        op = request["op"]
        reply = self._send(request, replay=True, keep_release=op == "release")
        if op == "claim":
            if reply["outcome"] != "claimed":
                reject("GRANT_UNAVAILABLE")
            if recover_claim is None:
                raise OutcomeUnknown(request["transaction_id"])
            return recover_claim(request, reply)
        if op == "release":
            if recover_release is None:
                raise OutcomeUnknown(request["transaction_id"])
            return recover_release(request["body"], reply)
        return reply

    def release_claim(self, claim, witness):
        pending = self._load()
        if pending["op"] != "claim" or pending["body"] != claim:
            raise OutcomeUnknown(pending["transaction_id"])
        request = {"op": "release", "body": witness, "transaction_id": secrets.token_hex(16)}
        if len(canonical(request)) + 1 > MAX_FRAME:
            reject("INVALID_REQUEST")
        self._persist(request, replace=True)
        return self._send(request, replay=True, keep_release=True)

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
        self.claim_call = call
        return reply

    def release(self, witness):
        return self.client.release_claim(self.claim_call, witness)


def run_attempt(client, contract, trust, job, root, adapter, reservation_id=None, *, policy=None):
    if policy is not None:
        authorize_job(contract, job, policy)
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
                 prepare_runtime=lambda: adapter(job, authority.grant), after_release=client._finish,
                 authorize_profile=policy.authorize if policy is not None else None)
    boot.write("admission.json", admission)
    boot.write("request.json", request)
    try:
        result = boot.run(canonical(request))
    except OSError as error:
        if policy is not None and os.path.lexists(client.pending):
            raise OutcomeUnknown(client._load()["transaction_id"]) from error
        raise
    if policy is not None and result[2] is None:
        recovery = read_record(journal / "recovery-required.json")
        raise OutcomeUnknown(client._load()["transaction_id"], recovery)
    return result


def run_pair(client, contract, trust, descriptor, jobs, root, adapter, *, policy=None):
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
            result = run_attempt(client, contract, trust, job, root, adapter, reservation_id, policy=policy)
            results.append(result)
            if result[2] is None or policy is not None and result[1] is None:
                break
    except BaseException:
        if not os.path.lexists(client.pending):
            client.paired_close(reservation_id)
        raise
    if policy is not None and os.path.lexists(client.pending):
        raise OutcomeUnknown(client._load()["transaction_id"])
    return {"attempts": results, "pair_state": client.paired_close(reservation_id)}


def private_read(path, limit=MAX_FRAME, *, public=False):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb", buffering=0) as source:
        info = os.fstat(source.fileno())
        forbidden_mode = 0o022 if public else 0o077
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & forbidden_mode
                or info.st_uid != os.getuid() or not 0 < info.st_size <= limit):
            raise PermissionError("unsafe MAIN owner input")
        prefix = b""
        if public:
            header = b"-----BEGIN PUBLIC KEY-----"
            prefix = source.read(len(header))
            if prefix != header:
                raise ValueError("Tools signer pin must be a SubjectPublicKeyInfo PEM")
        data = prefix + source.read(limit + 1 - len(prefix))
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


def load_signers(config):
    tools, main = config["tools_signer"], config["main_signer"]
    public_key = load_pem_public_key(private_read(tools["key_path"], public=True))
    private_key = load_pem_private_key(private_read(main["key_path"]), password=None)
    if not isinstance(public_key, Ed25519PublicKey) or not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Ed25519 signer pins required")
    main_key, sign = signing_key(main["iss"], main["kid"], private_key)
    return (tools["iss"], tools["kid"], public_key), main_key, sign


def load_adapter(config):
    adapter_pin = config["adapter"]
    source = Path(adapter_pin["path"]).read_bytes()
    if hash_bytes(source) != adapter_pin["sha256"]:
        raise ValueError("runtime and observer adapter pin differs")
    module = ModuleType("fc_boot_adapter")
    module.__file__ = adapter_pin["path"]
    exec(compile(source, module.__file__, "exec"), vars(module))
    return module.prepare


def read_record(path):
    return decode(private_read(path))


def record_once(path, record):
    try:
        write_record(path, record)
    except FileExistsError:
        if canonical(read_record(path)) != canonical(record):
            raise ValueError(f"conflicting attempt record: {path.name}")
        sync_directory(path.parent)


def recovery_request(config, root, contract, binding, tools_key):
    attempt_id = binding["attempt_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", attempt_id):
        raise ValueError("invalid recovery attempt ID")
    attempt = root / attempt_id
    info = attempt.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("a private attempt journal is required")
    request = contract.validate("boot_request", read_record(attempt / "request.json"))
    grant = contract.validate("admission_grant", request["grant"])
    verify_signature(grant, *tools_key)
    if (selected(grant, ("host_id", "boot_id", *BINDING)) != selected(binding, ("host_id", "boot_id", *BINDING))
            or grant["host_id"] != config["host_id"] or grant["boot_id"] != BOOT_ID.read_text().strip()
            or grant["principal_id"] != OWNER or grant["profile_sha256"] != digest(request["profile"])
            or grant["backend"] != "firecracker" or request["op"] != "create"):
        reject("GRANT_MISMATCH")
    return attempt, request, grant


def settle_release(client, contract, attempt, grant, witness, reply, tools_key, main_key):
    contract.validate("reap_witness", witness)
    verify_signature(witness, *main_key)
    if (witness != read_record(attempt / "witness.json")
            or selected(witness, WITNESS_BINDING) != selected(grant, WITNESS_BINDING)):
        reject("WITNESS_INVALID")
    receipt = verify_release(contract, reply, grant, witness, tools_key)
    record_once(attempt / "release.json", receipt)
    client._finish()
    return receipt


def recover_claim(config, root, contract, client, pending, reply, *, policy=None):
    tools_key, main_key, sign = load_signers(config)
    claim = pending["body"]
    attempt, request, grant = recovery_request(config, root, contract, claim, tools_key)
    if (claim != read_record(attempt / "claim-intent.json") or claim["boot_owner"] != OWNER
            or claim["create_request_sha256"] != digest(request)
            or reply["expires_mono_ns"] != grant["expires_mono_ns"]):
        reject("GRANT_MISMATCH")
    record_once(attempt / "claim.json", reply)
    try:
        witness = read_record(attempt / "witness.json")
    except FileNotFoundError:
        # The lock and the exclusive attempt journal fence any new launch. With
        # launch intent present, only an already published witness can settle.
        if os.path.lexists(attempt / "launch-intent.json"):
            try:
                recovery = read_record(attempt / "recovery-required.json")
            except FileNotFoundError:
                recovery = None
            raise OutcomeUnknown(pending["transaction_id"], recovery)
        admission = contract.validate("admission_request", read_record(attempt / "admission.json"))
        pair = config.get("pair")
        jobs = [config["single"], *(pair["jobs"] if pair else ())]
        job = next((job for job in jobs if job["create_request_id"] == request["request_id"]
                    and job["admission"] == admission), None)
        if job is None or admission["request_id"] != grant["admission_request_id"]:
            reject("GRANT_MISMATCH")
        recovery = attempt / "claim-recovery.json"
        try:
            intent = read_record(recovery)
        except FileNotFoundError:
            intent = {"claim": claim, "observed_mono_ns": str(time.monotonic_ns())}
            write_record(recovery, intent)
        if intent["claim"] != claim:
            reject("GRANT_MISMATCH")
        observed = intent["observed_mono_ns"]
        # prepare resolves the runtime and observer without spawning a runtime.
        # Replays call the observer with the same claim, proof and timestamp.
        _, observe = (policy.prepare(job, grant, root) if policy is not None
                      else load_adapter(config)(job, grant))
        if not callable(observe):
            raise RuntimeFailure("custody observer required")
        proof = {"kind": "never_started", "launch_fenced": True, "spawn_count": 0,
                 "runtime_inventory_absent": True, "sockets_absent": True}
        evidence_ref = observe(claim, proof, observed)
        witness = make_witness(contract, grant, proof, evidence_ref, observed, sign, main_key)
        write_record(attempt / "witness.json", witness)
    contract.validate("reap_witness", witness)
    verify_signature(witness, *main_key)
    if selected(witness, WITNESS_BINDING) != selected(grant, WITNESS_BINDING):
        reject("WITNESS_INVALID")
    receipt = client.release_claim(claim, witness)
    return settle_release(client, contract, attempt, grant, witness, receipt, tools_key, main_key)


def recover_release(config, root, contract, client, witness, receipt):
    tools_key, main_key, _ = load_signers(config)
    attempt, _, grant = recovery_request(config, root, contract, witness, tools_key)
    return settle_release(client, contract, attempt, grant, witness, receipt, tools_key, main_key)


def run_config(config, *, replay_pending=False, live_lines=None, native=False):
    if config["mode"] != TOOLS_SOCKET:
        raise ValueError("owner configuration must select TOOLS_SOCKET")
    root = Path(config["journal_root"])
    info = root.lstat()
    if (root.resolve(strict=True) != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise PermissionError("a canonical private journal root is required")
    contract = Contract(config["contract_root"])
    policy = None
    if native or "tier1_policy" in config:
        if "tier" in config or "adapter" in config:
            raise UnsupportedProfile("The native boot owner rejects caller-selected tiers and adapters")
        from fc_boot_tier1 import Tier1Policy
        pin = config.get("tier1_policy")
        if not isinstance(pin, dict):
            raise UnsupportedProfile("An approved Tier 1 policy pin is required")
        value = decode(private_read(pin["path"]))
        if digest(value) != pin["sha256"]:
            raise UnsupportedProfile("The provisioned Tier 1 policy digest differs")
        policy = Tier1Policy(value)
    client = SocketTools(root, contract, config.get("socket_path"), tier1=policy is not None)
    with owned(root):
        if replay_pending:
            return client.replay_pending(
                recover_claim=lambda pending, reply: recover_claim(config, root, contract, client, pending, reply, policy=policy),
                recover_release=lambda witness, receipt: recover_release(config, root, contract, client, witness, receipt))
        if os.path.lexists(client.pending):
            raise OutcomeUnknown(client._load()["transaction_id"])
        if policy is not None:
            pair = config["pair"]
            contract.validate_pair(pair["reservation"])
            jobs = [config["single"], *pair["jobs"]]
            if len(jobs) != 3 or len({job["admission"]["request_id"] for job in jobs}) != 3:
                reject("INVALID_REQUEST")
            for job in jobs:
                authorize_job(contract, job, policy)
            with live_markers(live_lines, jobs) as marker:
                result = run_jobs(config, root, contract, client, policy=policy)
                marker["status"] = "completed" if native_complete(result) else "failed"
                return result
        return run_jobs(config, root, contract, client)


def authorize_job(contract, job, policy):
    admission = job["admission"]
    profile = admission.get("profile")
    if "tier" in job or "tier" in admission or isinstance(profile, dict) and "tier" in profile:
        raise UnsupportedProfile("Caller tier overrides are forbidden")
    contract.validate("admission_request", admission)
    policy.authorize(admission["profile_sha256"], profile)


def native_complete(result):
    pair = result.get("pair", {})
    attempts = [result["single"], *pair.get("attempts", [])]
    return (len(attempts) == 3 and pair.get("pair_state") == "closed"
            and all(attempt[1] == 385 and attempt[2] is not None for attempt in attempts))


@contextmanager
def live_markers(path, jobs):
    if path is None:
        raise ValueError("The R7 live output path is required")
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("The R7 live output path must be canonical")
    run = secrets.token_hex(16)
    # The owner writes shared evidence outside the sandbox. The VMM does not
    # inherit the log or any other evidence descriptor.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW), "ab", buffering=0) as log:
        info = os.fstat(log.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PermissionError("Unsafe R7 live output")

        def emit(event, **fields):
            record = {"event": event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "front": "MAIN", "what": "FC-BOOT-L", "run": run, **fields}
            row = canonical(record) + b"\n"
            if log.write(row) != len(row):
                raise OSError("Short R7 live record")
            os.fsync(log.fileno())

        marker = {"status": "failed"}
        emit("LIVE_START", attempts=dict(zip(("n1", "a1", "a2"),
                                             (job["stage_identity"]["run_id"] for job in jobs))))
        try:
            yield marker
        finally:
            emit("LIVE_END", **marker)


def run_jobs(config, root, contract, client, *, policy=None):
    tools_key, main_key, sign = load_signers(config)
    psi_policy = decode(private_read(config["psi_policy_path"]))
    if digest(psi_policy) != config["psi_policy_sha256"] or psi_policy["mode"] not in {"shadow", "enforce"}:
        raise ValueError("invalid Tools PSI policy pin")
    ttl_ns = config["accepted_grant_ttl_ns"]
    if not ttl_ns or any(type(value) is not int or value <= 0 for value in ttl_ns):
        raise ValueError("approved grant TTLs required")
    trust = {"host_id": config["host_id"], "ttl_ns": tuple(ttl_ns), "psi_policy": psi_policy,
             "tools_key": tools_key, "main_key": main_key, "sign_witness": sign}
    prepare = load_adapter(config) if policy is None else lambda job, grant: policy.prepare(job, grant, root)
    result = {"single": run_attempt(client, contract, trust, config["single"], root, prepare, policy=policy)}
    if policy is not None and os.path.lexists(client.pending):
        raise OutcomeUnknown(client._load()["transaction_id"])
    if (config["pair"] is not None and result["single"][2] is not None
            and (policy is None or result["single"][1] is not None)):
        descriptor, jobs = config["pair"]["reservation"], config["pair"]["jobs"]
        result["pair"] = run_pair(client, contract, trust, descriptor, jobs, root, prepare, policy=policy)
    return result


def main(argv=None, *, offline_fixture=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    native = bool(argv and argv[0] == "fcbootl")
    if native:
        argv.pop(0)
    parser = argparse.ArgumentParser(
        prog="fcbootl" if native else None, description=__doc__,
        epilog="Set tools_signer.key_path to the Ed25519 SubjectPublicKeyInfo PEM exported by the Tools provisioner.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--replay-pending", action="store_true")
    parser.add_argument("--inside-owner", action="store_true", help=argparse.SUPPRESS)
    if native:
        parser.add_argument("--socket", required=True, help="Absolute same-user Tools service socket")
        parser.add_argument("--live-lines", help="Existing operator-owned LIVE_LINES.jsonl")
        parser.add_argument("--tier", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if native and args.tier is not None:
            raise UnsupportedProfile("Caller tier overrides are forbidden")
        if not native and not args.replay_pending and not offline_fixture:
            raise UnsupportedProfile("Firecracker launch requires the native fcbootl command")
        if not args.inside_owner:
            command = [sys.executable, "-B", str(Path(__file__).resolve()), *(["fcbootl"] if native else []),
                       "--inside-owner", "--config", str(Path(args.config).resolve())]
            if native:
                command.extend(["--socket", args.socket])
                if args.live_lines is not None:
                    command.extend(["--live-lines", args.live_lines])
            if args.replay_pending:
                command.append("--replay-pending")
            return launch_owner(command)
        config = decode(private_read(args.config))
        if native:
            if config.get("socket_path") != args.socket:
                raise ValueError("The Tools socket must match the private owner configuration")
            result = run_config(config, replay_pending=args.replay_pending, live_lines=args.live_lines, native=True)
        else:
            result = run_config(config, replay_pending=args.replay_pending)
        print(canonical(result).decode())
        if args.replay_pending:
            return 0
        if native:
            return int(not native_complete(result))
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
        print(canonical({"outcome": "refused", "code": str(error),
                         **getattr(error, "action", {})}).decode())
        return 1
    except (OSError, ValueError, KeyError, ValidationError, InvalidSignature, RuntimeFailure,
            subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
