"""MAIN's FC-BOOT-L boot lifecycle and frozen record verification."""

import base64
import hashlib
import json
import os
from pathlib import Path
import time

import rfc8785
from cryptography.exceptions import InvalidSignature
from jsonschema import Draft202012Validator, RefResolver, ValidationError


FIXTURE_GRANT = "FIXTURE_GRANT"
ACCEPTANCE_SHA256 = "662adb6020c7f8de2c1294e03262a277effe6027cb4f8fa80127fa6763245f01"
FREEZE_SHA256 = "6d1dbe4460796f2aead8562791034cd30bbb58cd86769cd5fa3a900ff5c4f170"
BINDING = ("slot_id", "lease_id", "epoch", "attempt_id")
WITNESS_BINDING = ("host_id", "boot_id", *BINDING, "profile_sha256", "backend")
GRANT_BINDING = ("host_id", "boot_id", "principal_id", *BINDING, "admission_request_id")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


class Refusal(ValueError):
    pass


class RuntimeFailure(RuntimeError):
    """A backend failure that requires an owned teardown."""


class PinnedResolver(RefResolver):
    def resolve_remote(self, uri):
        raise ValueError(f"reference outside accepted schema closure: {uri}")


def reject(code):
    raise Refusal(code)


def decode(raw):
    def unique(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("duplicate JSON key")
            fields[key] = value
        return fields

    def invalid_constant(_):
        raise ValueError("non-JSON constant")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=invalid_constant)


def canonical(value):
    return rfc8785.dumps(value)


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return hash_bytes(canonical(value))


def selected(value, fields):
    return {key: value[key] for key in fields}


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def signing_bytes(record):
    signer = {key: value for key, value in record["signer"].items() if key != "sig"}
    return b"viewpoint.fc.v1\n" + canonical({**record, "signer": signer})


def verify_signature(record, role, key_id, public_key):
    signer = record["signer"]
    if (signer["iss"], signer["kid"], signer["alg"]) != (role, key_id, "Ed25519"):
        raise InvalidSignature("untrusted signer")
    encoded = signer["sig"].encode("ascii")
    signature = base64.b64decode(encoded + b"==", altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(signature).rstrip(b"=") != encoded:
        raise InvalidSignature("non-canonical signature")
    public_key.verify(signature, signing_bytes(record))


class Contract:
    def __init__(self, root):
        root = Path(root)
        schemas = root / "FC_BOOT_CONTRACT.schemas"

        def pinned(path, expected):
            raw = path.read_bytes()
            if hash_bytes(raw) != expected:
                raise ValueError(f"contract pin differs: {path.name}")
            return raw

        accepted = decode(pinned(root / "FC_BOOT_CONTRACT.TOOLS_ACCEPTANCE.json", ACCEPTANCE_SHA256))
        freeze = pinned(root / "FC_BOOT_CONTRACT.FREEZE.json", FREEZE_SHA256)
        if (freeze != (schemas / "FREEZE.json").read_bytes()
                or accepted["freeze_id"] != decode(freeze)["freeze_id"]):
            raise ValueError("contract freeze differs")
        pinned(root / "FC_BOOT_CONTRACT.md", accepted["contract"]["sha256"])
        schemas_by_name = {}
        for artifact in accepted["frozen"] + accepted["companion_schemas"]:
            name = Path(artifact["path"]).name
            schema = decode(pinned(schemas / name, artifact["sha256"]))
            Draft202012Validator.check_schema(schema)
            schemas_by_name[name] = schema
        self.schemas = {
            name.removesuffix(".schema.json"): Draft202012Validator(
                schema, resolver=PinnedResolver.from_schema(schema, store=schemas_by_name))
            for name, schema in schemas_by_name.items()
        }
        self.paired_schema = schemas / "paired_reservation.schema.json"

    def validate(self, name, value):
        self.schemas[name].validate(value)
        return value

    def validate_pair(self, descriptor):
        raw = self.paired_schema.read_bytes()
        if hash_bytes(raw) != "c34657f8b406cd7e1eeb2ced70d0929016815027377a2178c12b56b34391da4f":
            raise ValueError("paired reservation schema differs from Tools pin")
        Draft202012Validator(decode(raw)).validate(descriptor)


class Boot:
    """One create and SUMSQ effect with an exclusive, durable attempt journal."""

    def __init__(self, contract, tools, runtime, root, observer, verify_keys,
                 sign_witness, *, authority, psi_mode, accepted_ttl_ns,
                 prepare_runtime=None, now=time.monotonic_ns):
        self.contract = contract
        self.tools = tools
        self.runtime = runtime
        self.root = Path(root)
        self.observer = dict(observer)
        self.verify_keys = dict(verify_keys)
        self.sign_witness = sign_witness
        self.authority = authority
        self.psi_mode = psi_mode
        self.accepted_ttl_ns = accepted_ttl_ns
        self.prepare_runtime = prepare_runtime
        self.observe_custody = None
        self.now = now
        self.sequence = 0
        self.used = False
        self.claim_call = None
        self.root.mkdir(mode=0o700, exist_ok=False)
        sync_directory(self.root.parent)

    def write(self, name, record, append=False):
        payload = canonical(record)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
        with os.fdopen(os.open(self.root / name, flags, 0o600), "wb") as stream:
            stream.write(payload + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self.root)
        return {"id": name, "sha256": hash_bytes(payload)}

    def verify_record(self, record, role):
        issuer, key_id, public_key = self.verify_keys[role]
        verify_signature(record, issuer, key_id, public_key)

    def stage(self, name, source="main", **fields):
        boot_id = BOOT_ID.read_text().strip()
        if boot_id != self.tools.expected["boot_id"]:
            raise RuntimeFailure("host boot changed")
        self.sequence += 1
        event = {"schema": "vp.boot_stage/v1", **self.observer,
                 "attempt_id": self.tools.expected["attempt_id"],
                 "boot_id": boot_id, "clock": "CLOCK_MONOTONIC",
                 "monotonic_ns": str(self.now()), "sequence": self.sequence,
                 "source": source, "stage": name, **fields}
        self.contract.validate("stage_event", event)
        self.write("stages.jsonl", event, append=True)

    def verify(self, raw):
        try:
            request = decode(raw)
        except ValueError as error:
            raise Refusal("INVALID_REQUEST") from error
        if type(request) is not dict or request.get("op") != "create":
            reject("INVALID_REQUEST")
        if "grant" not in request:
            reject("GRANT_REQUIRED")
        try:
            grant = self.contract.validate("admission_grant", request["grant"])
            self.verify_record(grant, "tools")
        except (ValidationError, InvalidSignature, ValueError) as error:
            raise Refusal("GRANT_INVALID") from error
        try:
            self.contract.validate("boot_request", request)
            profile = request["profile"]
            profile_sha256 = digest(profile)
            canonical(request)
        except (ValidationError, ValueError) as error:
            raise Refusal("INVALID_REQUEST") from error
        resources = profile["resources"]
        if (profile["backend"], profile["class"], profile["required_isolation"],
                profile["network_policy"], resources.get("guest_memory_mib"),
                resources["vcpu"], resources["memory_max_bytes"], resources["swap_max_bytes"],
                resources["pids_max"]) != (
                    "firecracker", "headless", "vm", {"mode": "none"}, 256, 1, 512 << 20, 0, 64):
            reject("PROFILE_UNSUPPORTED")
        if grant["epoch"] != self.tools.expected["epoch"]:
            reject("STALE_EPOCH")
        if (selected(grant, GRANT_BINDING) != self.tools.expected
                or grant["backend"] != profile["backend"]
                or grant["priority_class"] != profile["priority_class"]
                or grant["profile_sha256"] != profile_sha256):
            reject("GRANT_MISMATCH")
        if (grant["receipt_ref"] != self.tools.admission_ref
                 or grant["psi"]["mode"] != self.psi_mode
                or grant["psi"]["policy_sha256"] != self.tools.psi_sha256
                or profile["guardian_policy_sha256"] != self.tools.guardian_sha256
                or grant["ram_reservation_bytes"] < max(self.tools.minimum_ram,
                                                        profile["resources"]["memory_max_bytes"])):
            reject("GRANT_INVALID")
        if (self.accepted_ttl_ns is not None
                and int(grant["expires_mono_ns"]) - int(grant["issued_mono_ns"])
                not in self.accepted_ttl_ns):
            reject("GRANT_INVALID")
        self.require_fresh(grant)
        return request, grant

    def require_fresh(self, grant):
        now = self.now()
        if int(grant["issued_mono_ns"]) > now:
            reject("GRANT_INVALID")
        if now >= int(grant["expires_mono_ns"]):
            reject("GRANT_EXPIRED")

    def claim(self, request, grant):
        call = {**selected(grant, ("host_id", "boot_id", *BINDING)),
                "boot_owner": self.tools.boot_owner, "create_request_sha256": digest(request)}
        self.write("claim-intent.json", call)
        reply = self.tools.claim(call)
        if "code" in reply:
            reject(reply["code"])
        positive = {**call, "expires_mono_ns": grant["expires_mono_ns"]}
        if reply not in ({"outcome": "claimed", **positive}, {"outcome": "already_claimed", **positive}):
            reject("GRANT_UNAVAILABLE")
        self.write("claim.json", reply)
        self.claim_call = call
        self.stage("grant_claimed")

    def response(self, request, **fields):
        response = {"schema": "vp.boot_response/v1", "request_id": request["request_id"],
                    "op": "create", **fields}
        return self.contract.validate("boot_response", response)

    def terminal(self, outcome, status):
        evidence = self.write("terminal.json", {"authority": self.authority, **status})
        self.stage("terminal", "observer", outcome=outcome, evidence_ref=evidence)

    def retire(self, grant):
        if self.runtime is None or (self.prepare_runtime is not None and not callable(self.observe_custody)):
            raise RuntimeFailure("custody observer unavailable")
        try:
            self.stage("stop")
        finally:
            proof, observations = self.runtime.retire()
        if (proof["kind"] not in {"never_started", "reaped"} or observations["scope_gone"] is not True
                or observations["surviving_pids"] != []):
            raise RuntimeFailure("teardown unproved")
        evidence_ref = self.write("teardown.json", {"authority": self.authority, **observations})
        observed = str(self.now())
        if self.observe_custody is not None:
            evidence_ref = self.observe_custody(self.claim_call, proof, observed)
        binding = selected(grant, WITNESS_BINDING)
        witness = self.sign_witness({
            "schema": "vp.reap_witness/v1", **binding,
            "observed_mono_ns": observed,
            "witness_id": "witness-" + grant["attempt_id"], "proof": proof,
            "evidence_ref": evidence_ref,
        })
        self.contract.validate("reap_witness", witness)
        self.verify_record(witness, "main")
        if (selected(witness, WITNESS_BINDING) != binding or witness["proof"] != proof
                or witness["evidence_ref"] != evidence_ref):
            raise RuntimeFailure("teardown unproved")
        self.write("witness.json", witness)
        self.stage("reap")
        try:
            reply = self.tools.release(witness)
        except Refusal as error:
            self.write("release-refusal.json", {"code": str(error)})
            raise
        receipt = self.contract.validate("release_receipt", reply)
        self.verify_record(receipt, "tools")
        if (selected(receipt, WITNESS_BINDING) != binding
                or receipt["release_basis"] != proof["kind"]
                or receipt["disposition"] != "released"
                or receipt["ram_released_bytes"] != grant["ram_reservation_bytes"]
                or int(receipt["released_mono_ns"]) < int(witness["observed_mono_ns"])
                or receipt["prior_admission_receipt_ref"] != grant["receipt_ref"]
                or receipt["proof_ref"] != {"id": witness["witness_id"], "sha256": digest(witness)}):
            raise RuntimeFailure("release receipt does not settle this attempt")
        self.write("release.json", receipt)
        self.stage("release", "tools")
        return receipt

    def run(self, raw):
        if self.used:
            reject("REQUEST_ID_CONFLICT")
        self.used = True
        self.stage("pre_attach", "observer")
        try:
            request, grant = self.verify(raw)
            self.stage("grant_verified")
            self.claim(request, grant)
        except Refusal as error:
            self.terminal("refused", {"code": str(error)})
            raise

        result = None
        dispatched = False
        failure = None
        try:
            if self.prepare_runtime is not None:
                self.runtime, self.observe_custody = self.prepare_runtime()
                if not callable(self.observe_custody):
                    raise RuntimeFailure("custody observer required")
            self.write("launch-intent.json", selected(grant, WITNESS_BINDING))
            self.runtime.before_boot()
            self.require_fresh(grant)
            self.stage("create_request")
            self.runtime.launch(request["profile"], self.stage)
            self.stage("ready", "observer")
            self.require_fresh(grant)
            self.stage("work_start")
            dispatched = True
            result = self.runtime.sumsq(10)
            if type(result) is not int or result != sum(n * n for n in range(1, 11)):
                raise RuntimeFailure("SUMSQ oracle disagrees")
            self.require_fresh(grant)
            self.stage("work_end")
        except (Refusal, RuntimeFailure, OSError) as error:
            failure = error

        try:
            receipt = self.retire(grant)
        except (RuntimeFailure, OSError, ValueError, ValidationError, InvalidSignature):
            self.terminal("outcome_unknown", {"code": "TEARDOWN_UNVERIFIED"})
            return self.response(request, outcome="unknown", code="OUTCOME_UNKNOWN",
                                 binding=selected(grant, BINDING)), None, None
        if failure is not None:
            outcome = "outcome_unknown" if dispatched else "failed"
            self.terminal(outcome, {"code": str(failure)})
            return self.response(request, outcome="unknown", code="OUTCOME_UNKNOWN",
                                 binding=selected(grant, BINDING)), None, receipt
        self.terminal("succeeded", {"result": result})
        return self.response(request, outcome="known", binding=selected(grant, BINDING),
                             state="reaped", operation_state="completed"), result, receipt


class FixtureBoot(Boot):
    """Explicit test-only authority using the caller's fixture keys and doubles."""

    def __init__(self, contract, tools, runtime, root, observer, verify_keys,
                 sign_witness, now=time.monotonic_ns):
        super().__init__(contract, tools, runtime, root, observer,
                         {role: (role, *key) for role, key in verify_keys.items()},
                         sign_witness, authority=FIXTURE_GRANT, psi_mode="shadow",
                         accepted_ttl_ns=None, now=now)
