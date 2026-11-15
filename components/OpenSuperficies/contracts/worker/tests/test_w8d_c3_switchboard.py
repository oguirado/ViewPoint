"""Executable C3 interface proposal, not a production Switchboard/Bridge adapter.

All numeric fixture values, bounds and protocol versions here are [TARGET].
The inherited speech-act field bounds and allowed W8 exit codes are [CITED]:
contracts/worker/os.cu.worker-task.v1.schema.json:2995-3149 and AGENTS.md:16,
at c8cb7dc8c0946be08edcb2a419629b6debd33c96. The native act mapping is [CITED]
docs/design/w8d/packs/D4.md:42-46; real sender/score authentication remains a
host-adapter obligation. A trusted Python object models that boundary; an
untrusted JSON claim is never an authentication proof.

All data fixtures are inline. No sockets, subprocesses, clocks, images, frames,
credentials, repository mutations or filesystem writes are used by this suite.
FakePinnedVerifier is deliberately a fake: tests do not certify real
auto_ingest parity or os-capsule writer authorization. Missing deployment pins
fail closed. The unchanged existing W8 schema remains the receipt authority.
"""

import copy
import hashlib
import json
import re
import sys
import unittest
from collections import deque
from dataclasses import asdict, dataclass, replace


HEX = re.compile(r"[0-9a-f]{64}")
REF = re.compile(r"sha256:[0-9a-f]{64}")
EXITS = frozenset((0, 2, 3, 4, 5, 6, 7))


class Refused(ValueError):
    def __init__(self, reason, rc=5):
        if rc not in EXITS or rc == 0:
            raise ValueError("bad refusal code")
        self.reason, self.rc = reason, rc
        super().__init__(reason)


def require(ok, reason, rc=5):
    if not ok:
        raise Refused(reason, rc)


def integer(value, minimum=0, maximum=9007199254740991):
    require(type(value) is int and minimum <= value <= maximum, "integer")
    return value


def closed(value, keys):
    require(type(value) is dict and set(value) == set(keys), "closed_keys")


def text(value, limit=4096):
    require(type(value) is str and 0 < len(value) <= limit, "text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise Refused("unicode") from exc
    return value


def digest(value):
    require(type(value) is str and HEX.fullmatch(value) is not None, "digest")
    return value


def ref(value):
    require(type(value) is str and REF.fullmatch(value) is not None, "body_ref")
    return value


def sha(data):
    require(type(data) is bytes, "bytes")
    return hashlib.sha256(data).hexdigest()


def body_ref(data):
    return "sha256:" + sha(data)


def canonical_json(value):
    """Integer-only JSON; Unicode is preserved unless a typed normalizer says so."""
    def check(item):
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            require(-9007199254740991 <= item <= 9007199254740991, "integer_range")
        elif type(item) is str:
            try:
                item.encode("utf-8", "strict")
            except UnicodeError as exc:
                raise Refused("unicode") from exc
        elif type(item) is list:
            for child in item:
                check(child)
        elif type(item) is dict:
            require(all(type(key) is str for key in item), "string_keys")
            for key, child in item.items():
                check(key)
                check(child)
        else:
            raise Refused("non_json_or_float")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def bad_number(_):
        raise Refused("float_or_constant")
    require(type(data) is bytes, "bytes")
    try:
        result = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=pairs,
                            parse_float=bad_number, parse_constant=bad_number)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Refused("json") from exc
    canonical_json(result)
    return result


def safe_relative(path):
    text(path)
    require(not path.startswith("/") and "\\" not in path and
            not any(part in ("", ".", "..", ".git") for part in path.split("/")) and
            not any(ord(char) < 32 for char in path), "relative_path")
    return path


REPRO_KEYS = frozenset(("normalizer", "repo", "commit", "base_image_digest",
    "base_recipe_sha256", "cache_manifest_sha256", "guest_kernel_sha256",
    "guest_rootfs_sha256", "extract_policy_sha256", "gate_argv", "gate_cwd",
    "failure_kind", "normalized_exit", "affected", "assertion", "frames",
    "repro_steps", "workspace_prefix", "pid", "time_ms"))


def normalize_repro(record, trusted_workspace_prefix):
    """Remove typed volatility, never regex-strip literals from assertions/argv."""
    closed(record, REPRO_KEYS)
    require(record["normalizer"] == "w8-repro/1", "normalizer")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text(record["repo"])), "repo")
    require(re.fullmatch(r"[0-9a-f]{40}", text(record["commit"])), "commit")
    for key in ("base_image_digest", "base_recipe_sha256", "cache_manifest_sha256", "extract_policy_sha256"):
        digest(record[key])
    for key in ("guest_rootfs_sha256", "guest_kernel_sha256"):
        if record[key] is not None:
            digest(record[key])
    require((record["guest_rootfs_sha256"] is None) == (record["guest_kernel_sha256"] is None), "guest_pins")
    require(type(record["normalized_exit"]) is int and record["normalized_exit"] in EXITS, "normalized_exit")
    if record["gate_cwd"] != ".":
        safe_relative(record["gate_cwd"])
    require(type(record["affected"]) is list and len(record["affected"]) <= 64, "affected")
    for path in record["affected"]:
        safe_relative(path)
    require(record["workspace_prefix"] == trusted_workspace_prefix and
            trusted_workspace_prefix.startswith("/") and
            trusted_workspace_prefix.endswith("/"), "workspace_provenance")
    integer(record["pid"])
    integer(record["time_ms"])
    for key in ("gate_argv", "repro_steps"):
        require(type(record[key]) is list and 0 < len(record[key]) <= 64, "ordered_list")
        for item in record[key]:
            text(item)
    require(type(record["frames"]) is list and len(record["frames"]) <= 64, "frames")
    frames = []
    for frame in record["frames"]:
        closed(frame, ("path", "symbol", "line", "address"))
        path = text(frame["path"])
        if path.startswith(trusted_workspace_prefix):
            path = path[len(trusted_workspace_prefix):]
        safe_relative(path)
        text(frame["symbol"])
        integer(frame["line"], 1)
        require(re.fullmatch(r"0x[0-9a-f]+", text(frame["address"])), "address")
        frames.append({"path": path,
                       "symbol": frame["symbol"],
                       "line": frame["line"]})
    result = {key: copy.deepcopy(record[key]) for key in REPRO_KEYS -
              {"workspace_prefix", "pid", "time_ms", "frames"}}
    text(result["failure_kind"])
    text(result["assertion"])
    result["frames"] = frames
    return canonical_json(result)


def repro_hash(record, prefix):
    return sha(normalize_repro(record, prefix))


class ExactDedup:
    def __init__(self):
        self.items = {}

    def add(self, canonical, supplied_hash):
        require(sha(canonical) == digest(supplied_hash), "repro_digest")
        prior = self.items.get(supplied_hash)
        if prior is not None:
            require(prior == canonical, "canonical_collision")
            return "duplicate"
        self.items[supplied_hash] = canonical
        return "new"


@dataclass(frozen=True)
class LeaseBinding:
    request_id: str
    wave_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    target_id: str
    worker_id: str
    epoch: int
    source_commit: str
    task_sha256: str
    lease_nonce: str
    seat_grant_sha256: str
    backend_surface: str
    extension: int
    host_id: str
    expires_ms: int
    collect_until_ms: int
    logical_request_sha256: str = "e" * 64

    def identity(self):
        integer(self.epoch, 1)
        integer(self.extension, 1)
        integer(self.expires_ms)
        integer(self.collect_until_ms, self.expires_ms + 1)
        require(re.fullmatch(r"[0-9a-f]{40}", self.source_commit), "source_commit")
        digest(self.task_sha256)
        digest(self.lease_nonce)
        digest(self.seat_grant_sha256)
        digest(self.logical_request_sha256)
        require(self.backend_surface in ("os-capsule", "chatgpt-mcp", "jules"), "surface")
        for key, value in asdict(self).items():
            if key not in ("epoch", "extension", "expires_ms", "collect_until_ms"):
                text(value)
        return sha(canonical_json(asdict(self)))


@dataclass(frozen=True)
class HostPrincipal:
    """Created by the trusted channel adapter, never decoded from message JSON."""
    host_id: str
    extension: int


@dataclass(frozen=True)
class CollectorPrincipal:
    collector_id: str
    host_id: str


class LeaseRegistry:
    def __init__(self, bindings):
        self.active = {(b.wave_id, b.target_id): b for b in bindings}
        self.interrupted = {}
        self.retained_extracts = {}
        self.reservations_held = {b.identity() for b in bindings}

    def check(self, binding, principal, now, collection=False):
        require(type(principal) is HostPrincipal, "authenticated_channel", 7)
        require(principal == HostPrincipal(binding.host_id, binding.extension), "sender", 7)
        current = self.active.get((binding.wave_id, binding.target_id)) == binding
        retained = self.retained_extracts.get(binding.identity())
        require(current or (collection and retained is not None and retained[0] == binding), "lease_fence", 7)
        require(now < (binding.collect_until_ms if collection else binding.expires_ms), "expired", 3)
        binding.identity()

    def reassign(self, old, new, fence_committed):
        require(fence_committed is True, "fence_unconfirmed", 6)
        require(self.active.get((old.wave_id, old.target_id)) == old, "lease_fence", 7)
        require((new.wave_id, new.target_id) == (old.wave_id, old.target_id) and
                new.epoch == old.epoch + 1 and new.attempt_id != old.attempt_id and
                new.lease_id != old.lease_id and new.worker_id != old.worker_id and
                new.request_id == old.request_id and new.wave_id == old.wave_id and
                new.task_id == old.task_id and new.source_commit == old.source_commit and
                new.logical_request_sha256 == old.logical_request_sha256 and
                new.seat_grant_sha256 != old.seat_grant_sha256 and
                new.lease_nonce != old.lease_nonce, "reassignment")
        self.interrupted[old.identity()] = "interrupted_duplicate"
        self.active[(new.wave_id, new.target_id)] = new
        self.reservations_held.add(new.identity())

    def retain_extract(self, binding, principal, now, extract_sha256):
        self.check(binding, principal, now)
        self.retained_extracts[binding.identity()] = (binding, digest(extract_sha256))

    def cleanup_confirmed(self, old, stopped, purged):
        require(stopped is True and purged is True, "cleanup_unconfirmed", 6)
        self.reservations_held.discard(old.identity())


@dataclass(frozen=True)
class EvidenceAttestation:
    binding_digest: str
    source_body_ref: str
    evidence_body_ref: str
    repro_sha256: str
    score_milli: int
    verifier_digest: str
    merkle_root_sha256: str


def native_score(attestation, binding, raw, evidence, repro, trusted_attestations):
    require(type(attestation) is EvidenceAttestation and attestation in trusted_attestations,
            "untrusted_evidence_score", 7)
    require(attestation.binding_digest == binding.identity() and
            attestation.source_body_ref == body_ref(raw) and
            attestation.evidence_body_ref == body_ref(evidence) and
            attestation.repro_sha256 == repro, "evidence_binding", 7)
    digest(attestation.verifier_digest)
    digest(attestation.merkle_root_sha256)
    value = integer(attestation.score_milli, 800, 1000)
    return str(value // 1000) + "." + str(value % 1000).zfill(3)


def validate_extract(manifest, objects):
    closed(manifest, ("method", "text_only", "entries"))
    require(manifest["method"] == "tar-exec" and manifest["text_only"] is True, "extract_method", 7)
    require(type(manifest["entries"]) is list and 0 < len(manifest["entries"]) <= 16, "extract_entries")
    listed, total = set(), 0
    for entry in manifest["entries"]:
        closed(entry, ("path", "kind", "media_type", "bytes", "sha256", "mode", "regular", "owner_asserted"))
        path = safe_relative(entry["path"])
        suffix = path.rsplit(".", 1)[-1]
        types = {"json": "application/json", "md": "text/markdown", "txt": "text/plain",
                 "patch": "text/plain", "diff": "text/plain"}
        require("/" not in path and suffix in types, "extract_flat_text", 7)
        require(path not in listed, "duplicate_extract_path")
        listed.add(path)
        require(entry["kind"] in ("speech", "evidence", "receipt") and
                entry["media_type"] == types[suffix] and
                (entry["kind"] == "evidence" or suffix == "json") and
                entry["mode"] == "0644" and entry["regular"] is True and
                entry["owner_asserted"] is True, "extract_policy", 7)
        require(path in objects and type(objects[path]) is bytes, "extract_missing")
        raw = objects[path]
        require(len(raw) == integer(entry["bytes"], 1, 102400) and
                sha(raw) == digest(entry["sha256"]), "extract_digest")
        try:
            decoded = raw.decode("utf-8", "strict")
        except UnicodeError as exc:
            raise Refused("extract_not_text", 7) from exc
        require("\0" not in decoded, "extract_not_text", 7)
        total += len(raw)
    require(set(objects) == listed and total <= 102400, "extract_budget")
    return {body_ref(raw): raw for raw in objects.values()}


SPEECH_KEYS = frozenset(("schema", "act", "wave_id", "task_id", "attempt_id", "lease_id",
                        "seq", "repro_sha256", "affected", "status", "body"))


def speech(data, binding):
    value = parse_json(data)
    closed(value, SPEECH_KEYS)
    require(value["schema"] == "os.cu.speech-act/v1", "speech_schema")
    require(value["act"] in ("finding", "repro", "affected", "status"), "speech_act")
    for key in ("wave_id", "task_id", "attempt_id", "lease_id"):
        require(value[key] == getattr(binding, key), "speech_binding", 7)
    integer(value["seq"], 0, 1000000)
    require(type(value["body"]) is str and len(value["body"]) <= 2048, "speech_body")
    require(type(value["affected"]) is list and len(value["affected"]) <= 64 and
            all(type(path) is str for path in value["affected"]) and
            len(set(value["affected"])) == len(value["affected"]), "affected")
    for path in value["affected"]:
        safe_relative(path)
    if value["repro_sha256"] is not None:
        digest(value["repro_sha256"])
    if value["act"] in ("finding", "repro"):
        digest(value["repro_sha256"])
    if value["act"] == "status":
        require(value["status"] in ("working", "blocked", "done", "stuck"), "status")
    else:
        require(value["status"] is None, "status")
    if value["act"] == "affected":
        require(bool(value["affected"]), "empty_affected")
    native = {"finding": "claim", "repro": "evidence", "affected": "query",
              "status": "yield" if value["status"] in ("blocked", "stuck") else "commit"}
    return value, native[value["act"]]


class FakeClock:
    def __init__(self, now=0):
        self.now = integer(now)

    def advance(self, delta):
        self.now += integer(delta)


class Admission:
    """Host-clock pulse, aggregate worker quota across routes and reconnects."""
    def __init__(self, clock, registry, pulse_ms=100, max_sends=2, max_bytes=8192):
        self.clock, self.registry = clock, registry
        self.pulse_ms, self.max_sends, self.max_bytes = pulse_ms, max_sends, max_bytes
        self.counters, self.holds, self.messages, self.sequences = {}, [], [], {}

    def send(self, binding, principal, raw, objects, route="mailbox", room_authorized=False,
             attestation=None, evidence=b"", trusted_attestations=frozenset()):
        self.registry.check(binding, principal, self.clock.now)
        value, act = speech(raw, binding)
        require(body_ref(raw) in objects and objects[body_ref(raw)] == raw, "full_body_required")
        require(route == "mailbox" or (route.startswith("topic:") and room_authorized is True),
                "room_not_authorized", 7)
        score = None
        if value["act"] == "repro":
            require(objects.get(body_ref(evidence)) == evidence, "full_evidence_required")
            score = native_score(attestation, binding, raw, evidence, value["repro_sha256"], trusted_attestations)
        seq_key = (binding.identity(), value["seq"])
        if seq_key in self.sequences:
            require(self.sequences[seq_key] == body_ref(raw), "sequence_conflict")
            return "redelivery"
        pulse = self.clock.now // self.pulse_ms
        key = (binding.wave_id, binding.worker_id, pulse)
        count, size = self.counters.get(key, (0, 0))
        require(type(raw) is bytes, "bytes")
        if count >= self.max_sends or size + len(raw) > self.max_bytes:
            self.holds.append((binding.identity(), pulse, self.clock.now))
            raise Refused("throttled", 3)
        envelope = {"schema": "w8-switchboard/1", "binding": asdict(binding),
                    "route": route, "native_act": act, "sender_extension": principal.extension,
                    "pulse": pulse, "source_body_ref": body_ref(raw),
                    "source_body_bytes": len(raw), "score": score,
                    "evidence_merkle_root": attestation.merkle_root_sha256 if score else None}
        self.counters[key] = (count + 1, size + len(raw))
        self.sequences[seq_key] = body_ref(raw)
        self.messages.append(envelope)
        return envelope


def terminal_id(binding, receipt_sha256):
    binding.identity()
    return "receipt:" + sha(canonical_json({"domain": "w8-terminal/1", "lease": asdict(binding),
                                           "receipt_sha256": digest(receipt_sha256)}))


def ack_id(binding, receipt_id, receipt_sha256, stage, consumer_sha256, collector_id="collector-a"):
    require(receipt_id == terminal_id(binding, receipt_sha256), "receipt_identity")
    digest(receipt_sha256)
    digest(consumer_sha256)
    require(stage in ("staged", "valid", "rejected"), "ack_stage")
    return "ack:" + sha(canonical_json({"domain": "w8-ack/1", "lease": asdict(binding),
        "receipt_id": receipt_id, "receipt_sha256": receipt_sha256,
        "stage": stage, "consumer_sha256": consumer_sha256, "collector_id": text(collector_id)}))


class CreditQueue:
    """One reserved terminal slot per admitted lease; independent coalesced STOP lane."""
    def __init__(self, bindings, terminal_bytes=102400, collector_id="collector-a"):
        self.bindings = {b.identity(): b for b in bindings}
        self.terminal_bytes = terminal_bytes
        self.pending, self.ready, self.stop_lane = {}, deque(), {}
        self.quarantined = set()
        self.collector_id, self.acks, self.last_was_stop = collector_id, {}, False
        self.collector_principal = CollectorPrincipal(collector_id, "host-collector")

    def publish(self, binding, raw):
        key = binding.identity()
        require(key in self.bindings and key not in self.quarantined, "credit_unreserved", 3)
        require(len(raw) <= self.terminal_bytes, "terminal_too_large", 3)
        if key in self.pending:
            require(self.pending[key] == raw, "terminal_conflict")
            return "redelivery"
        self.pending[key] = raw
        self.ready.append(key)
        return "staged"

    def stop(self, binding, reason="operator_stop"):
        key = binding.identity()
        require(key in self.bindings, "stop_target", 7)
        self.stop_lane[key] = reason

    def drain(self):
        if self.stop_lane and (not self.last_was_stop or not self.ready):
            key = next(iter(self.stop_lane))
            self.last_was_stop = True
            return "STOP", key, self.stop_lane.pop(key)
        if self.ready:
            key = self.ready.popleft()
            self.last_was_stop = False
            return "terminal", key, self.pending[key]
        return None

    def acknowledge(self, binding, receipt_sha256, supplied_ack, consumer_sha256,
                    stage="staged", principal=None):
        require(type(principal) is CollectorPrincipal and principal == self.collector_principal,
                "collector_auth", 7)
        key = binding.identity()
        if key in self.acks:
            require(self.acks[key] == supplied_ack, "ack_replay_conflict", 7)
            return "redelivery"
        require(key in self.pending, "ack_unknown")
        require(receipt_sha256 == sha(self.pending[key]), "ack_body")
        require(supplied_ack == ack_id(binding, terminal_id(binding, receipt_sha256), receipt_sha256, stage,
                                     consumer_sha256, self.collector_id), "ack_binding", 7)
        self.acks[key] = supplied_ack
        self.pending.pop(key)
        self.bindings.pop(key)
        self.ready = deque(item for item in self.ready if item != key)
        return "released"

    def expire(self, now):
        for key, binding in self.bindings.items():
            if now >= binding.expires_ms:
                self.stop_lane[key] = "execution_deadline"
            if now >= binding.collect_until_ms:
                self.quarantined.add(key)
                self.stop_lane[key] = "collect_deadline"
        return set(self.quarantined)


class Accounting:
    def __init__(self, budget):
        self.budget = integer(budget)
        self.attempts, self.cost_events, self.yield_keys = {}, {}, set()
        self.requests, self.coverage_proofs, self.coverage_history = {}, {}, []
        self.budget_stopped = False

    def reserve(self, binding, provider_operation_id, liability, finalize):
        integer(liability)
        integer(finalize)
        key = (binding.request_id, binding.attempt_id)
        request = (binding.wave_id, binding.task_id, binding.logical_request_sha256, binding.source_commit)
        if binding.request_id in self.requests:
            require(self.requests[binding.request_id] == request, "request_conflict", 7)
        plan = (provider_operation_id, liability, finalize, binding.identity())
        if key in self.attempts:
            require(self.attempts[key]["plan"] == plan, "attempt_conflict")
            return "redelivery"
        require(not self.budget_stopped, "budget_stop", 3)
        held = sum(a["plan"][1] + a["plan"][2] for a in self.attempts.values())
        require(held + liability + finalize <= self.budget, "budget", 3)
        self.attempts[key] = {"plan": plan, "state": "reserved", "cost": None, "finalized": 0}
        self.requests[binding.request_id] = request
        return "reserved"

    def row(self, binding):
        key = (binding.request_id, binding.attempt_id)
        require(key in self.attempts, "unreserved_cost", 7)
        row = self.attempts[key]
        require(row["plan"][3] == binding.identity(), "cost_binding", 7)
        return row

    def settle(self, binding, cost_event_id, amount, certain=True):
        key = (binding.request_id, binding.attempt_id)
        row = self.row(binding)
        integer(amount, 0, row["plan"][1])
        if not certain:
            require(row["cost"] is None, "attempt_already_settled")
            row["state"] = "indeterminate"
            return "hold_reservation"
        event = (key, amount)
        if cost_event_id in self.cost_events:
            require(self.cost_events[cost_event_id] == event, "cost_event_conflict")
            return "redelivery"
        require(row["cost"] is None, "attempt_already_settled")
        self.cost_events[cost_event_id] = event
        row["cost"], row["state"] = amount, "settled"
        return "charged"

    def retry_allowed(self, old, provider_idempotency_proven=False):
        row = self.row(old)
        require(row["state"] != "indeterminate" or provider_idempotency_proven is True,
                "uncertain_provider_call", 6)

    def stop_budget(self):
        self.budget_stopped = True

    def finalize(self, binding, amount):
        row = self.row(binding)
        integer(amount, 0, row["plan"][2] - row["finalized"])
        row["finalized"] += amount

    def coverage(self, corpus_commit, coverage_id, repro_sha256, verified):
        require(verified is True, "unverified_yield")
        digest(repro_sha256)
        key = (corpus_commit, coverage_id)
        novel = key not in self.yield_keys
        self.yield_keys.add(key)
        self.coverage_proofs.setdefault(key, set()).add(repro_sha256)
        self.coverage_history.append(("verified", key, repro_sha256))
        return novel

    def revoke_yield(self, corpus_commit, coverage_id, repro_sha256):
        key = (corpus_commit, coverage_id)
        self.coverage_proofs.setdefault(key, set()).discard(repro_sha256)
        if not self.coverage_proofs[key]:
            self.yield_keys.discard(key)
        self.coverage_history.append(("revoked", key, repro_sha256))


def bus_event(binding, bus_epoch, cursor, previous, raw, kind="finding", target=None,
              verdict_generation=1, origin_id="collector-a"):
    event = {"schema": "w8-bus/1", "bus_id": binding.wave_id, "bus_epoch": bus_epoch,
             "origin_id": origin_id,
             "cursor": cursor, "previous_digest": previous,
             "lease_digest": binding.identity(), "kind": kind, "target": target,
             "source_body_ref": body_ref(raw), "source_body_bytes": len(raw),
             "bounded_digest": sha(raw), "digest_bytes": 64,
             "dropped_events": 0, "overflow": False,
             "repro_sha256": "d" * 64, "verdict_generation": verdict_generation}
    return event, body_ref(canonical_json(event))


class ReplayConsumer:
    def __init__(self, wave_id, epoch=1, origin_id="collector-a"):
        self.wave_id, self.epoch, self.cursor = wave_id, epoch, 0
        self.origin_id = origin_id
        self.head = "0" * 64
        self.seen, self.live_findings, self.prefix = {}, {}, []
        self.tombstones = {}

    def accept(self, event, msg_id, raw, binding, ratified):
        closed(event, ("schema", "bus_id", "bus_epoch", "cursor", "previous_digest", "lease_digest",
                       "kind", "target", "source_body_ref", "source_body_bytes", "bounded_digest",
                       "origin_id", "digest_bytes", "dropped_events", "overflow", "repro_sha256", "verdict_generation"))
        require(event["schema"] == "w8-bus/1" and event["bus_id"] == self.wave_id and
                event["origin_id"] == self.origin_id, "bus")
        require(len(canonical_json(event)) <= 2048, "event_budget", 3)
        require(integer(event["digest_bytes"]) == 64 and len(digest(event["bounded_digest"])) == 64, "digest_bound")
        require(integer(event["dropped_events"]) == 0 and event["overflow"] is False, "gap", 6)
        digest(event["repro_sha256"])
        integer(event["verdict_generation"], 1)
        integer(event["cursor"], 1)
        integer(event["bus_epoch"], 1)
        require(event["bus_epoch"] == self.epoch, "bus_epoch", 7)
        require(ref(msg_id) == body_ref(canonical_json(event)), "event_digest")
        require(event["lease_digest"] == binding.identity(), "event_lease", 7)
        require(event["source_body_ref"] == body_ref(raw) and
                integer(event["source_body_bytes"], 1, 102400) == len(raw) and
                event["bounded_digest"] == sha(raw), "event_body")
        key = (self.epoch, event["cursor"])
        if key in self.seen:
            require(self.seen[key] == msg_id, "overlap_conflict")
            return "redelivery"
        require(event["cursor"] == self.cursor + 1, "gap", 6)
        require(event["previous_digest"] == self.head, "predecessor", 6)
        require(ratified is True, "unratified_memory", 7)
        require(event["kind"] in ("finding", "revoke"), "event_kind")
        repro, generation = event["repro_sha256"], event["verdict_generation"]
        if event["kind"] == "finding":
            require(event["target"] is None, "finding_target")
            existing_generation, existing_body = self.live_findings.get(repro, (0, None))
            if generation == existing_generation:
                require(existing_body == event["source_body_ref"], "verdict_generation_conflict")
            if generation > max(self.tombstones.get(repro, 0), existing_generation):
                self.live_findings[repro] = (generation, event["source_body_ref"])
        else:
            require(event["target"] == repro, "revoke_target")
            self.tombstones[repro] = max(generation, self.tombstones.get(repro, 0))
            if self.live_findings.get(repro, (0, None))[0] <= generation:
                self.live_findings.pop(repro, None)
        self.cursor, self.head = event["cursor"], msg_id[len("sha256:"):]
        self.seen[key] = msg_id
        self.prefix.append((msg_id, event["source_body_ref"]))
        return "applied"

    def checkpoint(self, new_epoch, previous_head, authenticated):
        integer(new_epoch, 1)
        require(authenticated is True and previous_head == self.head and
                new_epoch == self.epoch + 1, "checkpoint_authority", 7)
        self.epoch, self.cursor = new_epoch, 0
        # Keep the prior seal as predecessor and retain the already-issued prefix.

    def snapshot(self):
        return canonical_json({"wave_id": self.wave_id, "origin_id": self.origin_id,
            "epoch": self.epoch, "cursor": self.cursor, "head": self.head,
            "seen": [[epoch, cursor, msg] for (epoch, cursor), msg in sorted(self.seen.items())],
            "live_findings": {key: list(value) for key, value in self.live_findings.items()},
            "tombstones": self.tombstones, "prefix": [list(row) for row in self.prefix]})

    @classmethod
    def restore(cls, raw, trusted_snapshot_sha256):
        require(sha(raw) == digest(trusted_snapshot_sha256), "snapshot_seal", 7)
        state = parse_json(raw)
        c = cls(state["wave_id"], state["epoch"], state["origin_id"])
        c.cursor, c.head = state["cursor"], state["head"]
        c.seen = {(epoch, cursor): msg for epoch, cursor, msg in state["seen"]}
        c.live_findings = {key: tuple(value) for key, value in state["live_findings"].items()}
        c.tombstones, c.prefix = state["tombstones"], [tuple(row) for row in state["prefix"]]
        return c


@dataclass(frozen=True)
class ConsumerPin:
    entry_point: str
    code_sha256: str
    writer_surfaces: frozenset
    repository_commit: str = "f" * 40
    contract_version: str = "fixture-auto-ingest/1"


class FakePinnedVerifier:
    def __init__(self, verdict="valid", reason="fixture"):
        self.verdict, self.reason, self.calls = verdict, reason, []

    def check(self, raw, binding):
        self.calls.append((raw, binding))
        return self.verdict, self.reason


class Aggregator:
    def __init__(self, pin, verifier):
        self.pin, self.verifier = pin, verifier
        self.receipts, self.decisions = {}, {}
        self.pending = {}

    def ready(self, marker):
        text(marker)
        return "READY_ONLY"

    def terminal(self, binding, raw, confirmed_publish=True):
        require(type(self.pin) is ConsumerPin and self.verifier is not None, "consumer_unavailable", 4)
        require(self.pin.entry_point == "fixture:auto_ingest.check", "consumer_entry_point", 4)
        digest(self.pin.code_sha256)
        require(re.fullmatch(r"[0-9a-f]{40}", self.pin.repository_commit) and
                self.pin.contract_version == "fixture-auto-ingest/1", "consumer_version", 4)
        require(binding.backend_surface in self.pin.writer_surfaces, "writer_unauthorized", 4)
        receipt = parse_json(raw)
        require(type(receipt) is dict, "receipt")
        for key in ("task_id", "attempt_id", "lease_id", "task_sha256", "lease_nonce", "seat_grant_sha256"):
            require(receipt.get(key) == getattr(binding, key), "receipt_binding", 7)
        require(receipt.get("schema") == "os.cu.worker-receipt/v1", "receipt_schema")
        require(receipt.get("repo", {}).get("commit") == binding.source_commit, "receipt_commit", 7)
        runtime = receipt.get("runtime", {})
        expected_surface = "os-capsule" if runtime.get("backend") in ("docker", "microvm") else runtime.get("web_surface")
        require(expected_surface == binding.backend_surface, "backend_surface", 7)
        require(type(receipt.get("exit_code")) is int and receipt["exit_code"] in EXITS, "exit_code")
        slot, rid = binding.identity(), terminal_id(binding, sha(raw))
        if slot in self.receipts:
            require(self.receipts[slot] == raw, "terminal_conflict")
        else:
            validate_receipt_schema(receipt)
            self.receipts[slot] = raw
        if rid in self.decisions:
            verdict, reason = self.decisions[rid]
            return verdict.upper(), ack_id(binding, rid, sha(raw), verdict, self.pin.code_sha256)
        if rid in self.pending:
            return "INDETERMINATE", None
        if not confirmed_publish:
            self.pending[rid] = "publish_unknown"
            return "INDETERMINATE", None
        if rid not in self.decisions:
            # Journal intent before the possibly effectful consumer call. Any
            # unknown verdict or exception remains query-only thereafter.
            self.pending[rid] = "verification_unknown"
            try:
                verdict, reason = self.verifier.check(raw, binding)
            except Exception as exc:
                raise Refused("consumer_error", 6) from exc
            require(verdict in ("valid", "rejected", "indeterminate"), "consumer_unknown", 6)
            if verdict == "indeterminate":
                self.pending[rid] = "verification_unknown"
                return "INDETERMINATE", None
            self.decisions[rid] = verdict, reason
            self.pending.pop(rid)
        verdict, reason = self.decisions[rid]
        return verdict.upper(), ack_id(binding, rid, sha(raw), verdict, self.pin.code_sha256)

    def resolve(self, binding, raw, verdict, principal):
        require(type(principal) is CollectorPrincipal and
                principal == CollectorPrincipal("collector-a", "host-collector"), "collector_auth", 7)
        slot, rid = binding.identity(), terminal_id(binding, sha(raw))
        require(self.receipts.get(slot) == raw and rid in self.pending, "pending_identity")
        stage = self.pending[rid]
        if stage == "publish_unknown":
            require(verdict == "published", "query_disposition")
            self.pending.pop(rid)
            return self.terminal(binding, raw)
        require(verdict in ("valid", "rejected"), "query_disposition")
        self.pending.pop(rid)
        self.decisions[rid] = verdict, "authenticated_query"
        return verdict.upper(), ack_id(binding, rid, sha(raw), verdict, self.pin.code_sha256)


def validate_receipt_schema(value):
    # Reuse the unchanged repository validator; all actual fixture data are inline.
    if __package__:
        from . import test_worker_task_schema as inherited
    else:
        import test_worker_task_schema as inherited
    try:
        inherited.ENGINE.validate(value, "receipt")
    except inherited.ValidationError as exc:
        raise Refused("receipt_schema_validation") from exc


def logical_request_digest(task):
    """Trusted adapter first validates complete task/bridge; then freezes the work."""
    stable = ("schema", "task_id", "repo", "objective", "sources", "write_paths",
              "gate", "evidence", "consumer", "host_metric")
    require(type(task) is dict and all(key in task for key in stable), "logical_task")
    projection = {key: task[key] for key in stable}
    projection["policy"] = {"present": "policy" in task, "value": task.get("policy")}
    return sha(canonical_json({"domain": "w8-logical-request/1", "work": projection}))


def validate_a4(policy):
    expected = {"base_image": "os-worker-base", "per_commit_image": False,
        "workspace": "/workspace", "workspace_kind": "tmpfs", "clone_verified": True, "extract": "tar-exec",
        "text_only": True, "inference_inside": False, "rootless": True,
        "rootfs_read_only": True, "cap_drop": ["ALL"], "no_new_privileges": True,
        "bind_mounts": [], "env_file": None, "runtime_socket": False,
        "devices": [], "phase": "docker", "egress": "none", "frames_exported": False}
    closed(policy, expected)
    require(policy["phase"] in ("docker", "microvm"), "phase", 7)
    expected["phase"] = policy["phase"]
    expected["devices"] = ["/dev/kvm"] if policy["phase"] == "microvm" else []
    require(policy == expected and all(type(policy[k]) is bool for k, v in expected.items()
                                    if type(v) is bool), "a4", 7)
    return "allowed"


def binding(**updates):
    base = LeaseBinding("request-a", "wave-a", "task-a", "attempt-a", "lease-a",
        "target-a", "worker-a", 1, "a" * 40, "b" * 64, "a" * 64, "c" * 64,
        "os-capsule", 17, "host-a", 1000, 1200)
    return replace(base, **updates)


def repro_fixture(**updates):
    value = {"normalizer": "w8-repro/1", "repo": "example/repo", "commit": "a" * 40,
        "base_image_digest": "b" * 64, "base_recipe_sha256": "e" * 64,
        "cache_manifest_sha256": "c" * 64, "extract_policy_sha256": "f" * 64,
        "guest_rootfs_sha256": None, "guest_kernel_sha256": None,
        "gate_argv": ["go", "test", "./..."], "gate_cwd": ".", "affected": ["src/a.go"],
        "failure_kind": "assertion", "normalized_exit": 5, "assertion": "caf\u00e9: expected 7\nactual 8",
        "frames": [{"path": "/workspace/repo/src/a.go", "symbol": "TestA", "line": 7, "address": "0xabc"}],
        "repro_steps": ["open", "save"], "workspace_prefix": "/workspace/repo/", "pid": 23, "time_ms": 45}
    value.update(updates)
    return value


def speech_fixture(lease=None, act="finding", seq=0, body=None):
    lease = lease or binding()
    value = {"schema": "os.cu.speech-act/v1", "act": act, "wave_id": lease.wave_id,
        "task_id": lease.task_id, "attempt_id": lease.attempt_id, "lease_id": lease.lease_id,
        "seq": seq, "repro_sha256": "d" * 64 if act in ("finding", "repro") else None,
        "affected": ["src/a.go"] if act == "affected" else [],
        "status": "working" if act == "status" else None,
        "body": body if body is not None else "detail " + "x" * 900}
    return value


def extracted(*raws):
    objects = {"result-" + str(i) + ".json": raw for i, raw in enumerate(raws)}
    manifest = {"method": "tar-exec", "text_only": True, "entries": [
        {"path": path, "kind": "speech" if i == 0 else "evidence",
         "media_type": "application/json", "bytes": len(raw), "sha256": sha(raw),
         "mode": "0644", "regular": True, "owner_asserted": True}
        for i, (path, raw) in enumerate(objects.items())]}
    return manifest, objects


def receipt_fixture(lease=None):
    """Full inline W8 receipt; real cross-document provenance stays verifier-owned."""
    lease = lease or binding()
    value = {'schema': 'os.cu.worker-receipt/v1',
     'task_id': 'task-example',
     'attempt_id': 'a1',
     'lease_id': 'lease-example',
     'task_sha256': '9ace335c541e1d70e8d60e01acd62c3d39aed757445e72739d010e20997a4707',
     'repo': {'url': 'https://example.invalid/team/project',
              'commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'},
     'seat_id': 'seat-example',
     'lease_nonce': 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
     'seat_grant_sha256': '3bb1b24aafbec3068fd7cad66846450e542aab6a9d4a6b23f3f8636211d04ff0',
     'started_at_ms': 100,
     'finished_at_ms': 200,
     'exit_code': 0,
     'failure_reason': None,
     'runtime': {'backend': 'docker',
                 'profile': 'worker',
                 'vmm': None,
                 'base_image': {'name': 'os-worker-base',
                                'digest': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                                'recipe_sha256': '9999999999999999999999999999999999999999999999999999999999999999',
                                'approval_sha256': '6666666666666666666666666666666666666666666666666666666666666666'},
                 'commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                 'workspace': {'kind': 'tmpfs',
                               'root': '/workspace',
                               'options': 'rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001',
                               'size_bytes': 134217728,
                               'purge': 'task_end',
                               'ingest': 'git-bundle-over-exec',
                               'cache': {'volume': 'os_worker_cache',
                                         'target': '/opt/cache',
                                         'read_only': True,
                                         'manifest_sha256': '5555555555555555555555555555555555555555555555555555555555555555'}},
                 'extract': 'tar-exec',
                 'inference': 'host'},
     'gate': {'argv': ['go', 'test', './...'],
              'cwd': '.',
              'timeout_ms': 10000,
              'raw_exit_code': 0,
              'exit_code': 0,
              'terminated_by': None,
              'duration_ms': 50,
              'stdout': {'sha256': None, 'retained_bytes': 0, 'total_bytes': 0, 'truncated': False},
              'stderr': {'sha256': None, 'retained_bytes': 0, 'total_bytes': 0, 'truncated': False}},
     'sources_observed': [{'path': 'src/work.go',
                           'sha256': 'b2c70bba7bf0ef43959341e03b923efd23fed1a0b3ed1895736736d88a5b39b0'}],
     'output': {'kind': 'branch',
                'repo_url': 'https://example.invalid/team/project',
                'branch': 'bridge/task-example/a1',
                'base_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'head_commit': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'files': [{'path': 'src/work.go',
                           'operation': 'modify',
                           'mode': '100644',
                           'sha256': '0c5cd5c85fd0b8947903d56a38e40920e023454470bd519f5f0fc5f9efbf3398',
                           'bytes': 17}]},
     'evidence': {'answers': [{'request_id': 'source',
                               'answer': 'b2c70bba7bf0ef43959341e03b923efd23fed1a0b3ed1895736736d88a5b39b0'}],
                  'notes': 'Synthetic fixture; no live runtime was started.'},
     'host_metric': {'name': 'elapsed_ms',
                     'unit': 'ms',
                     'value': None,
                     'provenance': 'unavailable',
                     'sample_count': 0},
     'observations': {'capsule_id': 'capsule-example',
                      'uid': 10001,
                      'rootfs_read_only': True,
                      'cap_drop': ['ALL'],
                      'no_new_privileges': True,
                      'no_host_mounts': True,
                      'no_runtime_socket': True,
                      'seccomp_profile_sha256': 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                      'cpuset': [10, 11],
                      'memory_limit_bytes': 268435456,
                      'peak_memory_bytes': 4096,
                      'pids_limit': 64,
                      'peak_pids': 2,
                      'workspace_limit_bytes': 134217728,
                      'workspace_peak_bytes': 2048,
                      'workspace_root': '/workspace/repo',
                      'frames_root': '/dev/shm/worker-lease-example/frames',
                      'frames_disk_bytes': 0,
                      'swap_enabled': False,
                      'core_dumps_enabled': False,
                      'network': {'mode': 'none', 'allowlist': []},
                      'denied_network_attempts': 0,
                      'ttl_enforced': True,
                      'xpra': 'off',
                      'x11_listeners': 0,
                      'public_listeners': 0,
                      'probes': {'inspect_sha256': '3333333333333333333333333333333333333333333333333333333333333333',
                                 'etc_write': 'read_only',
                                 'home_admin': 'absent',
                                 'egress_probe': 'BLOCKED',
                                 'orphans_after_task': 0,
                                 'memswap_equals_memory': True,
                                 'oom_score_adj': 800,
                                 'nofile': 8192,
                                 'devices': [],
                                 'bind_mounts': 0,
                                 'env_file_used': False,
                                 'env_secret_names': 0,
                                 'runtime_socket_visible': False,
                                 'daemon': 'rootless',
                                 'inference_processes': 0}},
     'cleanup': {'status': 'verified',
                 'remaining_pids': 0,
                 'remaining_workspace_bytes': 0,
                 'resources_released': True},
     'extract': {'method': 'tar-exec',
                 'entries': 2,
                 'bytes': 4096,
                 'text_only': True,
                 'mode': '0644',
                 'owner_asserted': True,
                 'staging_residue': 0,
                 'sha256': '2222222222222222222222222222222222222222222222222222222222222222'}}
    for key in ("task_id", "attempt_id", "lease_id", "task_sha256", "lease_nonce", "seat_grant_sha256"):
        value[key] = getattr(lease, key)
    value["repo"]["commit"] = lease.source_commit
    value["runtime"]["commit"] = lease.source_commit
    value["output"]["base_commit"] = lease.source_commit
    return canonical_json(value)


class C3ContractTests(unittest.TestCase):
    def assertRefused(self, reason, call, rc=None):
        with self.assertRaises(Refused) as caught:
            call()
        self.assertEqual(caught.exception.reason, reason)
        if rc is not None:
            self.assertEqual(caught.exception.rc, rc)

    def setup_bus(self):
        b = binding()
        clock = FakeClock()
        registry = LeaseRegistry([b])
        return b, HostPrincipal(b.host_id, b.extension), clock, registry, Admission(clock, registry)

    def test_canonical_golden_vectors(self):
        vectors = [
            ({"b": 2, "a": 1}, b'{"a":1,"b":2}', "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"),
            ({}, b'{}', "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
        ]
        for value, expected_bytes, expected_digest in vectors:
            with self.subTest(value=value):
                self.assertEqual(canonical_json(value), expected_bytes)
                self.assertEqual(sha(expected_bytes), expected_digest)

    def test_repro_golden_vector(self):
        expected = (b'{"affected":["src/a.go"],"assertion":"caf\xc3\xa9: expected 7\\nactual 8","base_image_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            b'"base_recipe_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",'
            b'"cache_manifest_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"extract_policy_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",'
            b'"failure_kind":"assertion","frames":[{"line":7,"path":"src/a.go","symbol":"TestA"}],"gate_argv":["go","test","./..."],'
            b'"gate_cwd":".","guest_kernel_sha256":null,"guest_rootfs_sha256":null,"normalized_exit":5,"normalizer":"w8-repro/1","repo":"example/repo","repro_steps":["open","save"]}')
        self.assertEqual(normalize_repro(repro_fixture(), "/workspace/repo/"), expected)
        self.assertEqual(sha(expected), "4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934")

    def test_repro_typed_volatility_normalizes(self):
        first = repro_fixture()
        second = repro_fixture(pid=99, time_ms=100, workspace_prefix="/workspace/retry/",
                               assertion="caf\u00e9: expected 7\nactual 8")
        second["frames"][0].update(path="/workspace/retry/src/a.go", address="0xdef")
        self.assertEqual(normalize_repro(first, "/workspace/repo/"), normalize_repro(second, "/workspace/retry/"))

    def test_repro_semantic_mutants_never_merge(self):
        baseline = repro_fixture()
        original = repro_hash(baseline, "/workspace/repo/")
        mutants = {"commit": "d" * 40, "base_image_digest": "d" * 64,
            "cache_manifest_sha256": "d" * 64, "base_recipe_sha256": "d" * 64, "extract_policy_sha256": "d" * 64,
            "gate_cwd": "src", "affected": ["src/b.go"], "normalized_exit": 2,
            "assertion": "caf\u00e9: expected 8\nactual 7", "gate_argv": ["test", "go", "./..."],
            "repro_steps": ["save", "open"], "failure_kind": "timeout"}
        for key, value in mutants.items():
            with self.subTest(key=key):
                changed = copy.deepcopy(baseline)
                changed[key] = value
                self.assertNotEqual(original, repro_hash(changed, "/workspace/repo/"))
        self.assertNotEqual(canonical_json(["ab", "c"]), canonical_json(["a", "bc"]))
        for assertion in ("cafe\u0301: expected 7\nactual 8", "caf\u00e9: expected 7\r\nactual 8",
                          "caf\u00e9: expected 0x7\nactual 8"):
            self.assertNotEqual(original, repro_hash(repro_fixture(assertion=assertion), "/workspace/repo/"))
        guest = repro_fixture(guest_kernel_sha256="d" * 64, guest_rootfs_sha256="e" * 64)
        self.assertNotEqual(original, repro_hash(guest, "/workspace/repo/"))
        for key in ("guest_kernel_sha256", "guest_rootfs_sha256"):
            changed = dict(guest, **{key: "f" * 64})
            self.assertNotEqual(repro_hash(guest, "/workspace/repo/"), repro_hash(changed, "/workspace/repo/"))

    def test_normalizer_rejects_unsafe_or_ambiguous_input(self):
        for key, value, reason in (("pid", True, "integer"), ("normalizer", "unknown", "normalizer"),
                                   ("workspace_prefix", "/tmp/task/", "workspace_provenance")):
            bad = repro_fixture(**{key: value})
            self.assertRefused(reason, lambda: normalize_repro(bad, "/workspace/repo/"))
        bad = repro_fixture()
        bad["frames"][0]["path"] = "/workspace/repo/../other/a.go"
        self.assertRefused("relative_path", lambda: normalize_repro(bad, "/workspace/repo/"))
        self.assertRefused("duplicate_json_key", lambda: parse_json(b'{"a":1,"a":2}'))
        self.assertRefused("float_or_constant", lambda: parse_json(b'{"a":0.8}'))
        self.assertRefused("unicode", lambda: canonical_json("\ud800"))
        self.assertRefused("non_json_or_float", lambda: canonical_json(float("nan")))

    def test_exact_dedup_never_uses_screening_hash_as_authority(self):
        d = ExactDedup()
        raw = normalize_repro(repro_fixture(), "/workspace/repo/")
        self.assertEqual(d.add(raw, sha(raw)), "new")
        self.assertEqual(d.add(raw, sha(raw)), "duplicate")
        self.assertRefused("repro_digest", lambda: d.add(raw + b" ", sha(raw)))
        # Deliberately corrupt the candidate cache: digest equality alone cannot merge it.
        d.items[sha(raw)] = b"different canonical bytes"
        self.assertRefused("canonical_collision", lambda: d.add(raw, sha(raw)))

    def test_full_noncanonical_payload_survives_native_mapping(self):
        b, principal, _, _, bus = self.setup_bus()
        for act in ("finding", "repro", "affected", "status"):
            raw = json.dumps(speech_fixture(b, act), ensure_ascii=False, indent=2).encode()
            value, native = speech(raw, b)
            self.assertEqual(native, {"finding": "claim", "repro": "evidence", "affected": "query", "status": "commit"}[act])
            self.assertGreater(len(value["body"]), 240)
            self.assertNotEqual(raw, canonical_json(value))
        raw = json.dumps(speech_fixture(b), indent=2).encode()
        manifest, objects = extracted(raw)
        full = validate_extract(manifest, objects)
        env = bus.send(b, principal, raw, full)
        self.assertEqual(full[env["source_body_ref"]], raw)
        self.assertEqual(env["source_body_bytes"], len(raw))

    def test_preview_truncation_and_malformed_references_fail(self):
        b, principal, _, _, bus = self.setup_bus()
        raw = canonical_json(speech_fixture(b))
        preview = raw[:240]
        self.assertRefused("full_body_required", lambda: bus.send(b, principal, raw, {body_ref(raw): preview}))
        for bad in (body_ref(raw) + "\n", body_ref(raw).upper(), "sha256:" + "0" * 63):
            self.assertRefused("body_ref", lambda: ref(bad))

    def test_speech_closed_schema_bounds_and_retarget_requirement(self):
        b = binding()
        mutations = [("seq", True, "integer"), ("seq", 1000001, "integer"),
                     ("body", "a" * 2049, "speech_body"), ("act", "chat", "speech_act")]
        for key, value, reason in mutations:
            v = speech_fixture(b)
            v[key] = value
            self.assertRefused(reason, lambda: speech(canonical_json(v), b))
        v = speech_fixture(b)
        v["evidence_ref"] = "not an inherited key"
        self.assertRefused("closed_keys", lambda: speech(canonical_json(v), b))
        v = speech_fixture(b, "affected")
        v["affected"] = []
        self.assertRefused("empty_affected", lambda: speech(canonical_json(v), b))

    def test_mailbox_default_room_requires_host_authorization(self):
        b, principal, _, _, bus = self.setup_bus()
        raw = canonical_json(speech_fixture(b))
        objects = {body_ref(raw): raw}
        self.assertRefused("room_not_authorized", lambda: bus.send(b, principal, raw, objects, route="topic:parser"), 7)
        self.assertEqual(bus.send(b, principal, raw, objects)["route"], "mailbox")
        raw = canonical_json(speech_fixture(b, seq=1))
        self.assertEqual(bus.send(b, principal, raw, {body_ref(raw): raw}, route="topic:parser", room_authorized=True)["route"], "topic:parser")

    def test_sender_is_channel_authenticated_and_fenced(self):
        b, principal, _, _, bus = self.setup_bus()
        raw = canonical_json(speech_fixture(b))
        objects = {body_ref(raw): raw}
        self.assertRefused("authenticated_channel", lambda: bus.send(b, {"extension": b.extension}, raw, objects), 7)
        self.assertRefused("sender", lambda: bus.send(b, HostPrincipal("host-b", b.extension), raw, objects), 7)
        self.assertRefused("lease_fence", lambda: bus.send(replace(b, epoch=2), principal, raw, objects), 7)

    def test_evidence_score_is_attested_to_complete_bytes_and_lease(self):
        b, principal, _, _, bus = self.setup_bus()
        raw, evidence = canonical_json(speech_fixture(b, "repro")), b'{"proof":"complete"}'
        full = validate_extract(*extracted(raw, evidence))
        att = EvidenceAttestation(b.identity(), body_ref(raw), body_ref(evidence), "d" * 64, 800, "e" * 64, "f" * 64)
        self.assertRefused("untrusted_evidence_score", lambda: bus.send(b, principal, raw, full, attestation=att, evidence=evidence), 7)
        good = bus.send(b, principal, raw, full, attestation=att, evidence=evidence, trusted_attestations={att})
        self.assertEqual(good["score"], "0.800")
        self.assertEqual(good["evidence_merkle_root"], "f" * 64)
        self.assertEqual(native_score(replace(att, score_milli=1000), b, raw, evidence, "d" * 64, {replace(att, score_milli=1000)}), "1.000")
        for bad in (replace(att, binding_digest="f" * 64), replace(att, evidence_body_ref=body_ref(evidence[:5]))):
            self.assertRefused("evidence_binding", lambda: native_score(bad, b, raw, evidence, "d" * 64, {bad}), 7)
        for score in (799, 1001, True):
            bad = replace(att, score_milli=score)
            self.assertRefused("integer", lambda: native_score(bad, b, raw, evidence, "d" * 64, {bad}))

    def test_extract_refuses_frames_links_raw_logs_and_hash_mismatch(self):
        raw = b'{"safe":"text"}'
        manifest, objects = extracted(raw)
        self.assertEqual(validate_extract(manifest, objects)[body_ref(raw)], raw)
        for key, value in (("kind", "frame"), ("kind", "raw_stdout"), ("regular", False),
                           ("mode", "0755"), ("media_type", "image/png"), ("owner_asserted", False)):
            bad = copy.deepcopy(manifest)
            bad["entries"][0][key] = value
            self.assertRefused("extract_policy", lambda: validate_extract(bad, objects), 7)
        bad = copy.deepcopy(manifest)
        bad["entries"][0]["sha256"] = "f" * 64
        self.assertRefused("extract_digest", lambda: validate_extract(bad, objects))
        bad = copy.deepcopy(manifest)
        bad["method"] = "docker-cp"
        self.assertRefused("extract_method", lambda: validate_extract(bad, objects), 7)

    def test_first_excess_send_throttles_at_same_tick_before_append(self):
        b, principal, clock, _, bus = self.setup_bus()
        for seq in range(2):
            raw = canonical_json(speech_fixture(b, seq=seq))
            bus.send(b, principal, raw, {body_ref(raw): raw})
        raw = canonical_json(speech_fixture(b, seq=2))
        self.assertRefused("throttled", lambda: bus.send(b, principal, raw, {body_ref(raw): raw}), 3)
        self.assertEqual(len(bus.messages), 2)
        self.assertEqual(bus.holds[-1][2], clock.now)
        self.assertEqual(clock.now, 0)
        clock.advance(99)
        self.assertRefused("throttled", lambda: bus.send(b, principal, raw, {body_ref(raw): raw}, route="topic:new", room_authorized=True), 3)
        clock.advance(1)
        self.assertEqual(bus.send(b, principal, raw, {body_ref(raw): raw})["pulse"], 1)

    def test_byte_quota_replay_and_conflicting_sequence(self):
        b, principal, clock, registry, _ = self.setup_bus()
        raw = canonical_json(speech_fixture(b))
        bus = Admission(clock, registry, max_bytes=len(raw))
        bus.send(b, principal, raw, {body_ref(raw): raw})
        self.assertEqual(bus.send(b, principal, raw, {body_ref(raw): raw}), "redelivery")
        changed = canonical_json(speech_fixture(b, body="different"))
        self.assertRefused("sequence_conflict", lambda: bus.send(b, principal, changed, {body_ref(changed): changed}))
        later = canonical_json(speech_fixture(b, seq=1))
        self.assertRefused("throttled", lambda: bus.send(b, principal, later, {body_ref(later): later}), 3)

    def test_duplicate_reassignment_fences_only_target_and_preserves_failure(self):
        old = binding()
        sibling = binding(target_id="target-b", worker_id="worker-b", lease_id="lease-b")
        registry = LeaseRegistry([old, sibling])
        new = replace(old, worker_id="worker-c", attempt_id="attempt-c", lease_id="lease-c", epoch=2,
                      lease_nonce="f" * 64, seat_grant_sha256="e" * 64)
        self.assertRefused("fence_unconfirmed", lambda: registry.reassign(old, new, False), 6)
        registry.reassign(old, new, True)
        self.assertRefused("lease_fence", lambda: registry.check(old, HostPrincipal(old.host_id, old.extension), 0), 7)
        registry.check(sibling, HostPrincipal(sibling.host_id, sibling.extension), 0)
        self.assertEqual(registry.interrupted[old.identity()], "interrupted_duplicate")
        self.assertIn(old.identity(), registry.reservations_held)
        self.assertRefused("cleanup_unconfirmed", lambda: registry.cleanup_confirmed(old, True, False), 6)
        registry.cleanup_confirmed(old, True, True)
        self.assertNotIn(old.identity(), registry.reservations_held)

    def test_terminal_and_ack_bind_every_lease_field(self):
        b = binding()
        raw = receipt_fixture(b)
        rid = terminal_id(b, sha(raw))
        aid = ack_id(b, rid, sha(raw), "staged", "e" * 64)
        for name in asdict(b):
            val = getattr(b, name)
            if type(val) is int:
                alt = val + 1
            elif name in ("task_sha256", "seat_grant_sha256", "logical_request_sha256", "lease_nonce"):
                alt = "f" * 64
            elif name == "source_commit":
                alt = "f" * 40
            elif name == "backend_surface":
                alt = "jules"
            else:
                alt = val + "-other"
            other = replace(b, **{name: alt})
            with self.subTest(field=name):
                self.assertNotEqual(terminal_id(other, sha(raw)), rid)
                self.assertNotEqual(ack_id(other, terminal_id(other, sha(raw)), sha(raw), "staged", "e" * 64), aid)
        self.assertNotEqual(aid, ack_id(b, rid, sha(raw), "valid", "e" * 64))
        self.assertNotEqual(aid, ack_id(b, terminal_id(b, sha(raw + b" ")), sha(raw + b" "), "staged", "e" * 64))

    def test_terminal_credit_stop_lane_fair_drain_and_bound_ack(self):
        a, b = binding(), binding(target_id="target-b", worker_id="worker-b", lease_id="lease-b", attempt_id="attempt-b")
        q = CreditQueue([a, b])
        ra, rb = receipt_fixture(a), receipt_fixture(b)
        q.publish(a, ra)
        q.publish(b, rb)
        self.assertEqual(q.publish(a, ra), "redelivery")
        q.stop(b)
        self.assertEqual(q.drain()[:2], ("STOP", b.identity()))
        self.assertEqual(q.drain()[:2], ("terminal", a.identity()))
        self.assertEqual(q.drain()[:2], ("terminal", b.identity()))
        self.assertEqual(len(q.pending), 2)  # dequeue alone never grants credit
        self.assertRefused("ack_binding", lambda: q.acknowledge(a, sha(ra), ack_id(b, terminal_id(b, sha(ra)), sha(ra), "staged", "e" * 64), "e" * 64, principal=CollectorPrincipal("collector-a", "host-collector")), 7)
        q.acknowledge(a, sha(ra), ack_id(a, terminal_id(a, sha(ra)), sha(ra), "staged", "e" * 64), "e" * 64, principal=CollectorPrincipal("collector-a", "host-collector"))
        self.assertEqual(len(q.pending), 1)
        self.assertRefused("credit_unreserved", lambda: q.publish(a, ra), 3)

    def test_collection_deadline_keeps_stop_and_quarantines_without_release(self):
        b = binding()
        q = CreditQueue([b])
        q.publish(b, receipt_fixture(b))
        self.assertEqual(q.expire(b.collect_until_ms - 1), set())
        self.assertEqual(q.expire(b.collect_until_ms), {b.identity()})
        self.assertEqual(q.drain()[0], "STOP")
        self.assertIn(b.identity(), q.pending)
        self.assertIn(b.identity(), q.bindings)
        self.assertRefused("credit_unreserved", lambda: q.publish(b, receipt_fixture(b)), 3)

    def test_credits_reject_oversize_and_conflicting_terminal(self):
        b = binding()
        q = CreditQueue([b], terminal_bytes=8)
        self.assertRefused("terminal_too_large", lambda: q.publish(b, b"x" * 9), 3)
        q.publish(b, b"good")
        self.assertRefused("terminal_conflict", lambda: q.publish(b, b"other"))

    def test_paid_retry_distinct_from_retransmission_and_repro_dedup(self):
        b = binding()
        retry = replace(b, attempt_id="attempt-b", lease_id="lease-b", epoch=2)
        accounts = Accounting(100)
        self.assertEqual(accounts.reserve(b, "provider-a", 30, 5), "reserved")
        self.assertEqual(accounts.reserve(b, "provider-a", 30, 5), "redelivery")
        accounts.settle(b, "cost-a", 20)
        self.assertEqual(accounts.settle(b, "cost-a", 20), "redelivery")
        accounts.reserve(retry, "provider-b", 30, 5)
        accounts.settle(retry, "cost-b", 20)
        self.assertEqual(sum(row["cost"] for row in accounts.attempts.values()), 40)
        self.assertTrue(accounts.coverage(b.source_commit, "coverage-a", "d" * 64, True))
        self.assertFalse(accounts.coverage(b.source_commit, "coverage-a", "d" * 64, True))
        self.assertEqual(len(accounts.cost_events), 2)

    def test_attempt_cost_and_uncertain_provider_conflicts_fail(self):
        b = binding()
        a = Accounting(100)
        a.reserve(b, "provider-a", 30, 5)
        self.assertRefused("attempt_conflict", lambda: a.reserve(b, "provider-b", 30, 5))
        self.assertEqual(a.settle(b, "pending", 20, certain=False), "hold_reservation")
        self.assertRefused("uncertain_provider_call", lambda: a.retry_allowed(b), 6)
        a.retry_allowed(b, provider_idempotency_proven=True)
        a.settle(b, "cost-a", 20)
        self.assertRefused("cost_event_conflict", lambda: a.settle(b, "cost-a", 21))
        self.assertRefused("attempt_already_settled", lambda: a.settle(b, "cost-b", 20))

    def test_budget_stop_only_spends_existing_finalization_reserve(self):
        b = binding()
        a = Accounting(40)
        a.reserve(b, "provider-a", 30, 5)
        a.stop_budget()
        self.assertRefused("budget_stop", lambda: a.reserve(replace(b, attempt_id="attempt-b"), "provider-b", 1, 1), 3)
        a.finalize(b, 5)
        self.assertRefused("integer", lambda: a.finalize(b, 1))
        self.assertRefused("unverified_yield", lambda: a.coverage(b.source_commit, "scope", "d" * 64, False))
        a.coverage(b.source_commit, "scope", "d" * 64, True)
        a.revoke_yield(b.source_commit, "scope", "d" * 64)
        self.assertEqual(a.yield_keys, set())

    def test_replay_redelivery_gap_conflict_and_full_body(self):
        b, raw = binding(), b'{"finding":"exact"}'
        c = ReplayConsumer(b.wave_id)
        event, msg = bus_event(b, 1, 1, c.head, raw)
        self.assertEqual(c.accept(event, msg, raw, b, True), "applied")
        self.assertEqual(c.accept(event, msg, raw, b, True), "redelivery")
        gap, gm = bus_event(b, 1, 3, c.head, raw)
        self.assertRefused("gap", lambda: c.accept(gap, gm, raw, b, True), 6)
        self.assertEqual(c.cursor, 1)
        conflict, cm = bus_event(b, 1, 1, "0" * 64, b"changed")
        self.assertRefused("overlap_conflict", lambda: c.accept(conflict, cm, b"changed", b, True))
        self.assertRefused("event_body", lambda: c.accept(event, msg, raw[:5], b, True))

    def test_bus_epoch_needs_authenticated_checkpoint_and_not_lease_epoch(self):
        b, raw = binding(), b'{"finding":"exact"}'
        c = ReplayConsumer(b.wave_id)
        event, msg = bus_event(b, 2, 1, c.head, raw)
        self.assertRefused("bus_epoch", lambda: c.accept(event, msg, raw, b, True), 7)
        self.assertRefused("checkpoint_authority", lambda: c.checkpoint(2, c.head, False), 7)
        c.checkpoint(2, c.head, True)
        self.assertEqual(c.accept(event, msg, raw, b, True), "applied")
        old, om = bus_event(b, 1, 2, c.head, raw)
        self.assertRefused("bus_epoch", lambda: c.accept(old, om, raw, b, True), 7)

    def test_revoke_appends_tombstone_without_rewriting_live_prefix(self):
        b, raw = binding(), b'{"finding":"exact"}'
        c = ReplayConsumer(b.wave_id)
        event, msg = bus_event(b, 1, 1, c.head, raw)
        self.assertRefused("unratified_memory", lambda: c.accept(event, msg, raw, b, False), 7)
        c.accept(event, msg, raw, b, True)
        issued_prefix = tuple(c.prefix)
        revoke, rm = bus_event(b, 1, 2, c.head, b'{"reason":"refuted"}', "revoke", "d" * 64)
        c.accept(revoke, rm, b'{"reason":"refuted"}', b, True)
        self.assertEqual(tuple(c.prefix[:1]), issued_prefix)
        self.assertEqual(c.live_findings, {})
        self.assertEqual(len(c.prefix), 2)

    def test_ready_valid_rejected_indeterminate_are_distinct(self):
        b, raw = binding(), receipt_fixture()
        pin = ConsumerPin("fixture:auto_ingest.check", "e" * 64, frozenset({"os-capsule"}))
        verifier = FakePinnedVerifier()
        a = Aggregator(pin, verifier)
        self.assertEqual(a.ready("READY_FOR_COLLECTION task-a attempt-a"), "READY_ONLY")
        self.assertEqual(verifier.calls, [])
        self.assertEqual(a.terminal(b, raw, confirmed_publish=False), ("INDETERMINATE", None))
        self.assertEqual(verifier.calls, [])
        self.assertEqual(a.terminal(b, raw), ("INDETERMINATE", None))
        result = a.resolve(b, raw, "published", CollectorPrincipal("collector-a", "host-collector"))
        self.assertEqual(result[0], "VALID")
        self.assertEqual(a.terminal(b, raw), result)
        self.assertEqual(verifier.calls, [(raw, b)])
        for verdict, expected in (("rejected", "REJECTED"), ("indeterminate", "INDETERMINATE")):
            other = Aggregator(pin, FakePinnedVerifier(verdict))
            result = other.terminal(b, raw)
            self.assertEqual(result[0], expected)
            self.assertEqual(result[1] is None, verdict == "indeterminate")

    def test_missing_real_consumer_pin_and_writer_authorization_fail_closed(self):
        b, raw = binding(), receipt_fixture()
        self.assertRefused("consumer_unavailable", lambda: Aggregator(None, None).terminal(b, raw), 4)
        denied = ConsumerPin("fixture:auto_ingest.check", "e" * 64, frozenset({"chatgpt-mcp"}))
        self.assertRefused("writer_unauthorized", lambda: Aggregator(denied, FakePinnedVerifier()).terminal(b, raw), 4)
        bad = replace(denied, entry_point="new_verifier")
        self.assertRefused("consumer_entry_point", lambda: Aggregator(bad, FakePinnedVerifier()).terminal(b, raw), 4)

    def test_aggregator_never_relabels_surface_or_merges_terminal_mutation(self):
        b, raw = binding(), receipt_fixture()
        a = Aggregator(ConsumerPin("fixture:auto_ingest.check", "e" * 64, frozenset({"os-capsule", "jules"})), FakePinnedVerifier())
        self.assertRefused("backend_surface", lambda: a.terminal(replace(b, backend_surface="jules"), raw), 7)
        a.terminal(b, raw)
        changed = parse_json(raw)
        changed["exit_code"] = 5
        self.assertRefused("terminal_conflict", lambda: a.terminal(b, canonical_json(changed)))
        changed["lease_nonce"] = "wrong"
        self.assertRefused("receipt_binding", lambda: a.terminal(b, canonical_json(changed)), 7)
        changed = parse_json(raw)
        changed["exit_code"] = 1
        self.assertRefused("exit_code", lambda: a.terminal(b, canonical_json(changed)))

    def test_a4_good_and_must_fail_security_fixtures(self):
        good = {"base_image": "os-worker-base", "per_commit_image": False,
            "workspace": "/workspace", "workspace_kind": "tmpfs", "clone_verified": True, "extract": "tar-exec",
            "text_only": True, "inference_inside": False, "rootless": True,
            "rootfs_read_only": True, "cap_drop": ["ALL"], "no_new_privileges": True,
            "bind_mounts": [], "env_file": None, "runtime_socket": False,
            "devices": [], "phase": "docker", "egress": "none", "frames_exported": False}
        self.assertEqual(validate_a4(good), "allowed")
        microvm = dict(good, phase="microvm", devices=["/dev/kvm"])
        self.assertEqual(validate_a4(microvm), "allowed")
        for key, value in {"per_commit_image": True, "workspace": "/tmp/task", "bind_mounts": ["/home/user"],
            "env_file": "secrets.env", "runtime_socket": True, "inference_inside": True,
            "rootless": False, "rootfs_read_only": False, "cap_drop": [],
            "no_new_privileges": False, "devices": ["/dev/kvm"], "egress": "all",
            "extract": "docker-cp", "frames_exported": True}.items():
            with self.subTest(key=key):
                self.assertRefused("a4", lambda: validate_a4(dict(good, **{key: value})), 7)

    def test_protocol_return_codes_exclude_one(self):
        self.assertEqual(EXITS, {0, 2, 3, 4, 5, 6, 7})
        with self.assertRaises(ValueError):
            Refused("forbidden", 1)

    def test_inline_receipt_matches_actual_unchanged_schema(self):
        raw = receipt_fixture()
        validate_receipt_schema(parse_json(raw))
        for mode in ("missing", "extra"):
            bad = parse_json(raw)
            if mode == "missing":
                bad.pop("cleanup")
            else:
                bad["bus_epoch"] = 1
            a = Aggregator(ConsumerPin("fixture:auto_ingest.check", "e" * 64, frozenset({"os-capsule"})), FakePinnedVerifier())
            self.assertRefused("receipt_schema_validation", lambda: a.terminal(binding(), canonical_json(bad)))
            self.assertEqual(a.verifier.calls, [])

    def test_unicode_payload_bound_counts_characters_and_retains_bytes(self):
        b, principal, _, _, bus = self.setup_bus()
        raw = json.dumps(speech_fixture(b, body="\u00e9" * 2048), ensure_ascii=False, indent=2).encode()
        full = validate_extract(*extracted(raw))
        envelope = bus.send(b, principal, raw, full)
        self.assertEqual(full[envelope["source_body_ref"]], raw)
        self.assertGreater(envelope["source_body_bytes"], 4096)
        bad = speech_fixture(b)
        bad["affected"] = [[]]
        self.assertRefused("affected", lambda: speech(canonical_json(bad), b))

    def test_flat_extract_and_nonce_validation_fail_closed(self):
        for path in ("nested/result.json", "frame.png", "archive.tar"):
            manifest, objects = extracted(b'{"text":"only"}')
            raw = next(iter(objects.values()))
            manifest["entries"][0]["path"] = path
            self.assertRefused("extract_flat_text", lambda: validate_extract(manifest, {path: raw}), 7)
        self.assertRefused("digest", lambda: binding(lease_nonce="not-a-digest").identity())

    def test_public_ack_hash_does_not_authenticate_collector_or_mint_credit(self):
        b = binding()
        q, raw = CreditQueue([b]), receipt_fixture(b)
        q.publish(b, raw)
        ack = ack_id(b, terminal_id(b, sha(raw)), sha(raw), "staged", "e" * 64)
        self.assertRefused("collector_auth", lambda: q.acknowledge(b, sha(raw), ack, "e" * 64), 7)
        self.assertIn(b.identity(), q.pending)
        principal = CollectorPrincipal("collector-a", "host-collector")
        self.assertEqual(q.acknowledge(b, sha(raw), ack, "e" * 64, principal=principal), "released")
        self.assertEqual(q.acknowledge(b, sha(raw), ack, "e" * 64, principal=principal), "redelivery")
        self.assertEqual((q.pending, q.bindings), ({}, {}))
        self.assertRefused("collector_auth", lambda: q.acknowledge(b, sha(raw), ack, "e" * 64,
            principal=CollectorPrincipal("collector-b", "host-collector")), 7)

    def test_repeated_stop_cannot_starve_other_terminal_or_extend_execution(self):
        a, b = binding(), binding(target_id="target-b", worker_id="worker-b", lease_id="lease-b")
        q = CreditQueue([a, b])
        q.publish(a, receipt_fixture(a)); q.publish(b, receipt_fixture(b))
        q.stop(a); q.stop(a)
        self.assertEqual(len(q.stop_lane), 1)
        self.assertEqual(q.drain()[0], "STOP")
        q.stop(a)
        self.assertEqual(q.drain()[:2], ("terminal", a.identity()))
        q.stop(a)
        self.assertEqual(q.drain()[0], "STOP")
        q.stop(a)
        self.assertEqual(q.drain()[:2], ("terminal", b.identity()))
        q.expire(a.expires_ms)
        self.assertEqual(q.stop_lane[a.identity()], "execution_deadline")
        self.assertNotIn(a.identity(), q.quarantined)

    def test_historical_retained_extract_collection_cannot_revive_worker(self):
        old = binding()
        p = HostPrincipal(old.host_id, old.extension)
        registry = LeaseRegistry([old])
        registry.retain_extract(old, p, 10, "d" * 64)
        new = replace(old, epoch=2, attempt_id="attempt-b", lease_id="lease-b", worker_id="worker-b",
                      lease_nonce="f" * 64, seat_grant_sha256="e" * 64)
        registry.reassign(old, new, True)
        registry.check(old, p, 1001, collection=True)
        self.assertRefused("lease_fence", lambda: registry.check(old, p, 1001), 7)
        self.assertRefused("expired", lambda: registry.check(old, p, old.collect_until_ms, collection=True), 3)
        self.assertEqual(registry.interrupted[old.identity()], "interrupted_duplicate")

    def test_request_projection_and_attempt_binding_prevent_cost_aliasing(self):
        task = {"schema": "os.cu.worker-task/v1", "task_id": "task-a", "repo": {"commit": "a" * 40},
            "objective": "prove assertion", "sources": [], "write_paths": ["src/a.go"],
            "gate": {"argv": ["go", "test"]}, "evidence": {"requests": []},
            "consumer": {"name": "auto_ingest"}, "host_metric": {}, "attempt_id": "attempt-a", "lease_id": "lease-a"}
        expected = logical_request_digest(task)
        self.assertEqual(expected, logical_request_digest(dict(task, attempt_id="attempt-b", lease_id="lease-b")))
        for key, value in (("objective", "different work"), ("policy", {"network": "none"}),
                           ("gate", {"argv": ["go", "vet"]}), ("repo", {"commit": "b" * 40})):
            self.assertNotEqual(expected, logical_request_digest(dict(task, **{key: value})))
        b = binding(logical_request_sha256=expected)
        a = Accounting(100)
        a.reserve(b, "provider-a", 30, 5)
        altered = replace(b, lease_nonce="f" * 64)
        self.assertRefused("cost_binding", lambda: a.settle(altered, "cost", 20), 7)
        self.assertRefused("cost_binding", lambda: a.finalize(altered, 1), 7)
        self.assertRefused("cost_binding", lambda: a.retry_allowed(altered), 7)
        self.assertRefused("request_conflict", lambda: a.reserve(replace(b, attempt_id="attempt-b",
            logical_request_sha256="f" * 64), "provider-b", 30, 5), 7)
        a.reserve(replace(b, attempt_id="attempt-b", task_sha256="f" * 64), "provider-b", 30, 5)
        a.settle(b, "cost", 20)
        self.assertRefused("attempt_already_settled", lambda: a.settle(b, "unknown", 20, certain=False))

    def test_coverage_is_separate_from_repro_and_last_proof_revocation(self):
        a = Accounting(100)
        self.assertTrue(a.coverage("commit-a", "task:policy:coverage-a", "d" * 64, True))
        self.assertFalse(a.coverage("commit-a", "task:policy:coverage-a", "e" * 64, True))
        a.revoke_yield("commit-a", "task:policy:coverage-a", "d" * 64)
        self.assertEqual(len(a.yield_keys), 1)
        a.revoke_yield("commit-a", "task:policy:coverage-a", "e" * 64)
        self.assertEqual(a.yield_keys, set())
        self.assertEqual(len(a.coverage_history), 4)

    def test_indeterminate_verifier_is_queried_never_reexecuted(self):
        b, raw = binding(), receipt_fixture()
        verifier = FakePinnedVerifier("indeterminate")
        a = Aggregator(ConsumerPin("fixture:auto_ingest.check", "e" * 64, frozenset({"os-capsule"})), verifier)
        self.assertEqual(a.terminal(b, raw), ("INDETERMINATE", None))
        self.assertEqual(a.terminal(b, raw), ("INDETERMINATE", None))
        self.assertEqual(len(verifier.calls), 1)
        self.assertRefused("collector_auth", lambda: a.resolve(b, raw, "valid", None), 7)
        self.assertEqual(a.resolve(b, raw, "rejected", CollectorPrincipal("collector-a", "host-collector"))[0], "REJECTED")
        self.assertEqual(a.terminal(b, raw)[0], "REJECTED")
        self.assertEqual(a.terminal(b, raw, confirmed_publish=False)[0], "REJECTED")
        self.assertEqual(len(verifier.calls), 1)
        unknown = FakePinnedVerifier("unknown")
        other = Aggregator(a.pin, unknown)
        self.assertRefused("consumer_unknown", lambda: other.terminal(b, raw), 6)
        self.assertEqual(other.terminal(b, raw), ("INDETERMINATE", None))
        self.assertEqual(len(unknown.calls), 1)

    def test_bounded_replay_digest_missing_intervals_and_snapshot_restart(self):
        b, raw = binding(), b'{"finding":"exact"}'
        c = ReplayConsumer(b.wave_id)
        event, msg = bus_event(b, 1, 1, c.head, raw)
        for key, value, reason in (("digest_bytes", 65, "digest_bound"),
                                   ("dropped_events", 1, "gap"), ("overflow", True, "gap"),
                                   ("source_body_bytes", True, "integer"),
                                   ("target", "x" * 2048, "event_budget")):
            altered = dict(event, **{key: value})
            self.assertRefused(reason, lambda: c.accept(altered, body_ref(canonical_json(altered)), raw, b, True))
        c.accept(event, msg, raw, b, True)
        snapshot = c.snapshot()
        restored = ReplayConsumer.restore(snapshot, sha(snapshot))
        self.assertEqual(restored.accept(event, msg, raw, b, True), "redelivery")
        self.assertEqual(restored.snapshot(), snapshot)
        self.assertRefused("snapshot_seal", lambda: ReplayConsumer.restore(snapshot + b" ", sha(snapshot)), 7)

    def test_tombstone_before_add_and_newer_verdict_cannot_be_downgraded(self):
        b, raw = binding(), b'{"finding":"exact"}'
        c = ReplayConsumer(b.wave_id)
        tomb, tm = bus_event(b, 1, 1, c.head, raw, "revoke", "d" * 64, 2)
        c.accept(tomb, tm, raw, b, True)
        old, om = bus_event(b, 1, 2, c.head, raw, verdict_generation=1)
        c.accept(old, om, raw, b, True)
        self.assertEqual(c.live_findings, {})
        new, nm = bus_event(b, 1, 3, c.head, raw, verdict_generation=3)
        c.accept(new, nm, raw, b, True)
        delayed, dm = bus_event(b, 1, 4, c.head, raw, verdict_generation=2)
        c.accept(delayed, dm, raw, b, True)
        revoke, rm = bus_event(b, 1, 5, c.head, raw, "revoke", "d" * 64, 2)
        c.accept(revoke, rm, raw, b, True)
        self.assertEqual(c.live_findings["d" * 64][0], 3)


if __name__ == "__main__":
    program = unittest.main(exit=False)
    sys.exit(0 if program.result.wasSuccessful() else 2)
