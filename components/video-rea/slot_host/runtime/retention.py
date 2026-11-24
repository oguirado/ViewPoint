# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-configured Tools readiness, launch admission and durable association."""

import copy
import json
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable

from . import evidence_projection as projection
from .evidence_projection import INDEX_SHA
from .receipts import read_retained, verify_inspection

_HASH = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}")


class RetentionConflict(ValueError):
    """The durable request already binds a different intent or claim."""


class RetentionUncertain(OSError):
    """An admission was already handed off or its acknowledgement is unresolved."""


@dataclass(frozen=True)
class RetentionIdentity:
    client: object
    principal: str


@dataclass(frozen=True)
class ComparisonHandoff:
    """Ports handed to the host adapter after the coordinator claims the request."""

    runner: RetentionIdentity
    inspector: RetentionIdentity
    admit_comparison: Callable
    read_source: Callable


@dataclass(frozen=True)
class TerminalEvidence:
    """Independent, host-supplied proof bound to one verified start."""

    start_sha256: str
    artifact_observer_id: str
    artifact_observation_id: str
    cleanup_observer_id: str
    cleanup_observation_id: str
    cleanup_retirement_sha256: str | None = None
    no_spawn: bool = False
    process: dict | None = None
    launch_observation: dict | None = None
    attribution_receipt_sha256: str | None = None
    captured_sha256: str | None = None
    recordings: dict | None = None
    media: dict | None = None
    timeline: dict | None = None

    def terminal_fields(self, *, cleanup_observed):
        retirement = self.cleanup_retirement_sha256 if cleanup_observed else None
        return {
            "artifact_observer_id": self.artifact_observer_id,
            "artifact_observation_id": self.artifact_observation_id,
            "cleanup_observer_id": self.cleanup_observer_id,
            "cleanup_observation_id": self.cleanup_observation_id,
            "cleanup_state": "confirmed_retired" if retirement is not None else "unknown",
            "cleanup_retirement_sha256": retirement,
        }


@dataclass(frozen=True)
class RetentionAttempt:
    """Handoff to the runner, scoped to its own writer and inspector."""

    admission: dict
    tenant: str
    runner_client: object
    inspector_client: object
    validate_profile: Callable | None = None
    observe_terminal: Callable | None = None
    custody: object | None = None

    @property
    def configured(self):
        return callable(self.validate_profile) and callable(self.observe_terminal)

    def __call__(self, opened):
        attempt = opened["attempt"]
        return self.configured and all(attempt.get(key) == self.admission[key]
                                       for key in ("attempt_id", "request_id", "operation"))

    def start(self, record):
        raw = projection.record_bytes(record)
        attempt, request = (self.admission[key] for key in ("attempt_id", "request_sha256"))
        expected = {"attempt_id": attempt, "request_sha256": request,
                    "start_sha256": sha256(raw).hexdigest()}
        self.runner_client.validator.validate("Start", record)
        if self.custody is not None and self.admission["operation"] == "compare":
            self.custody.compare_start(self, record)
        with self.runner_client.session(self.tenant) as session:
            if session.start(attempt, raw, request_sha256=request) != expected:
                raise RetentionUncertain("start acknowledgement does not bind the staged invocation")
        return expected

    def claim(self, acknowledgement):
        if self.custody is not None:
            self.custody.claim(self, acknowledgement)
        with self.runner_client.session(self.tenant) as session:
            if session.claim(dict(acknowledgement)) != acknowledgement:
                raise RetentionUncertain("launch acknowledgement does not bind the verified start")

    def inspect(self, started):
        attempt = self.admission["attempt_id"]
        with self.inspector_client.session(self.tenant) as inspector:
            inspection = inspector.inspect(attempt)
        verify_inspection(inspection, tenant=self.tenant, attempt_id=attempt,
                          admission=self.admission, start_raw=projection.record_bytes(started))
        return inspection

    def observation(self, started, facts):
        evidence = self.observe_terminal(copy.deepcopy(self.admission), copy.deepcopy(started), dict(facts))
        if (type(evidence) is not TerminalEvidence or evidence.start_sha256 != projection.record_digest(started)
                or self.admission["executor_id"] in (evidence.artifact_observer_id, evidence.cleanup_observer_id)):
            raise RetentionUncertain("no independent terminal evidence for the verified start")
        return evidence

    def finish(self, started, terminal, profile, *, recordings=None, payloads=None):
        """Write one frozen envelope and resolve it independently, including a lost reply."""
        admission = self.admission
        attempt = admission["attempt_id"]
        start_raw = projection.record_bytes(started)
        start_sha = sha256(start_raw).hexdigest()
        if terminal["start_sha256"] != start_sha:
            raise ValueError("terminal does not bind the verified start")
        validator = self.runner_client.validator
        with self.runner_client.session(self.tenant) as session:
            def upload(upload_id, role, body):
                if type(body) is not bytes:
                    raise ValueError("retained payloads must be exact octets")
                digest = sha256(body).hexdigest()
                reply = session.upload(attempt, upload_id, len(body), digest, body)
                validator.validate("ObjectMeta", reply)
                if (reply["bytes"], reply["sha256"]) != (len(body), digest):
                    raise ValueError("upload acknowledgement changed the payload binding")
                return {**reply, "role": role}

            terminal, index, artifact = projection.build_uploaded_index(
                profile, admission, terminal, upload, recordings=recordings, payloads=payloads,
            )
        validator.validate_index(index)
        validator.validate("Terminal", terminal)
        if self.validate_profile(admission, profile, terminal) is not True:
            raise ValueError("cannot retain an unapproved or unbound REA profile")
        terminal_raw = projection.record_bytes(terminal)
        expected = {"attempt_id": attempt, "start_sha256": start_sha,
                    "terminal_sha256": sha256(terminal_raw).hexdigest()}

        if self.custody is not None:
            self.custody.finish(self, terminal, artifact, index)

        def send():
            with self.runner_client.session(self.tenant) as session:
                if session.finish(attempt, terminal_raw, artifact, start_sha256=start_sha) != expected:
                    raise RetentionUncertain("finish acknowledgement does not bind the frozen terminal")

        try:
            send()
        except Exception as exc:
            # A definitive refusal is terminal for this invocation. After an
            # ambiguous write, only a matching inspect can authorize a retry.
            if _definite_refusal(exc):
                raise
            inspection = self.inspect(started)
            if inspection["state"] == "unknown":
                try:
                    send()
                except Exception as retry:
                    if _definite_refusal(retry):
                        raise
            elif inspection["state"] != "committed":
                raise RetentionUncertain("finish state cannot be resolved") from exc
        with self.inspector_client.session(self.tenant) as inspector:
            return read_retained(
                inspector, self.inspector_client.validator, self.validate_profile, tenant=self.tenant,
                attempt_id=attempt, admission=admission, start_raw=start_raw,
                terminal_raw=terminal_raw, artifact_raw=artifact,
            )


def _definite_refusal(exc):
    status = getattr(exc, "status", None)
    return type(status) is int and 400 <= status < 500


@dataclass(frozen=True)
class RetentionClients:
    """Trusted composition inputs. Keep distinct Client instances for each host role."""

    tenant: str
    authority: RetentionIdentity
    runner: RetentionIdentity
    inspector: RetentionIdentity
    policy_revision: str
    acl_revision: str
    profile_schema: str
    profile_sha256: str
    association_path: Path
    admission_bytes: Callable[[dict, object], bytes]  # Tools wire.admission_bytes
    validate_profile: Callable | None = None  # approved PinnedProfiles
    observe_terminal: Callable | None = None  # independently authenticated evidence

    local_open: bool = False
    successor_route: tuple[str, str, str] | None = None
    custody: object | None = None

    def __post_init__(self):
        ids = self.tenant, self.policy_revision, self.acl_revision, self.profile_schema
        identities = self.authority, self.runner, self.inspector
        principals = tuple(identity.principal for identity in identities)
        authority, runner, inspector = principals
        separate_roles = (authority == runner != inspector if self.local_open
                          else len(set(principals)) == len(identities))
        if (type(self.local_open) is not bool or not separate_roles
                or any(type(value) is not str or not _ID.fullmatch(value) for value in (*ids, *principals))
                or len({id(identity.client) for identity in identities}) != len(identities)
                or any(not callable(getattr(identity.client, "session", None)) for identity in identities)
                or type(self.profile_sha256) is not str or not _HASH.fullmatch(self.profile_sha256)
                or not callable(self.admission_bytes)):
            raise ValueError("approved retention tenant, role identities, revisions and profile are required")
        path = Path(self.association_path)
        if not path.is_absolute():
            raise ValueError("an absolute owner-controlled association path is required")
        object.__setattr__(self, "association_path", path)

    def claim_pins(self):
        return {
            "tenant": self.tenant, "authority": self.authority.principal,
            "runner": self.runner.principal, "inspector": self.inspector.principal,
            "policy": self.policy_revision, "acl": self.acl_revision,
            "profile": self.profile_schema, "profile_sha256": self.profile_sha256,
        }

    def matches_binding(self, binding):
        if not self.local_open:
            return True
        return (self.successor_route is not None
                and (getattr(binding, "provider_schema", None), binding.request_schema_ref,
                     binding.result_schema_ref) == self.successor_route)

    def ready(self, *, record_pin, operation_id):
        if self.local_open and not callable(self.observe_terminal):
            return False
        if operation_id not in ("record", "replay", "compare"):
            return False
        writer_verbs = ("start", "upload", "finish") if operation_id == "compare" else (
            "start", "claim", "upload", "finish",
        )
        challenges = set()
        try:
            for identity, verbs in (
                (self.authority, ("admit",)),
                (self.runner, writer_verbs),
                (self.inspector, ("inspect",)),
            ):
                with identity.client.session(self.tenant) as session:
                    for verb in verbs:
                        # The Client checks that the response echoes its newly
                        # generated challenge on the authenticated connection.
                        reply = session.ready(
                            verb, principal=identity.principal, policy_revision=self.policy_revision,
                            acl_revision=self.acl_revision,
                        )
                        challenge = reply.get("challenge")
                        if (type(challenge) is not str or not _HASH.fullmatch(challenge)
                                or challenge in challenges or reply != {
                                    "challenge": challenge, "verb": verb, "tenant": self.tenant,
                                    "principal": identity.principal, "policy_revision": self.policy_revision,
                                    "acl_revision": self.acl_revision, "record_pin": record_pin,
                                    "index_pin": INDEX_SHA,
                                }):
                            return False
                        challenges.add(challenge)
        except Exception:
            return False
        return True

    def _admission(self, binding, request_id, intent_sha256, attempt_id):
        admission = {
            "attempt_id": attempt_id, "request_id": request_id, "request_sha256": intent_sha256,
            "operation": binding.operation_id, "installation_ref": binding.installation_ref,
            "slot_id": binding.slot_id, "provider_id": binding.provider_id,
            "provider_revision": binding.provider_revision, "manifest_sha256": binding.manifest_digest,
            "implementation_sha256": binding.implementation_sha256, "source_url": binding.source_url,
            "executor_id": self.runner.principal, "profile_schema": self.profile_schema,
            "profile_schema_sha256": self.profile_sha256,
        }
        if binding.adapter_sha256 is not None:
            admission["adapter_sha256"] = binding.adapter_sha256
        admission["admission_sha256"] = sha256(
            self.admission_bytes(admission, self.authority.client.validator)
        ).hexdigest()
        return admission

    def associated_attempt(self, request_id, intent_sha256):
        """Look up a prior association without creating a state file."""
        if not self.association_path.exists():
            return None
        uri = self.association_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE name = 'retention_requests'").fetchone() is None:
                return None
            row = db.execute(
                "SELECT attempt_id, intent_sha256 FROM retention_requests WHERE request_id = ?", (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row[1] != intent_sha256:
            raise RetentionConflict("request already binds a different normalized intent")
        return row[0]

    def admit(self, binding, request_id, intent_sha256, claim_sha256, verify):
        """Persist before admitting, and hand off at most once after acknowledgement."""
        try:
            with closing(sqlite3.connect(self.association_path)) as db:
                db.execute("PRAGMA synchronous = FULL")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS retention_requests (
                            request_id TEXT PRIMARY KEY, intent_sha256 TEXT NOT NULL,
                            claim_sha256 TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE,
                            admission TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0
                        )
                    """)
                    row = db.execute(
                        "SELECT intent_sha256, claim_sha256, attempt_id FROM retention_requests WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if row is None:
                        attempt_id = secrets.token_hex(16)
                        admission = self._admission(binding, request_id, intent_sha256, attempt_id)
                        db.execute(
                            "INSERT INTO retention_requests VALUES (?, ?, ?, ?, ?, 0)",
                            (request_id, intent_sha256, claim_sha256, attempt_id, json.dumps(admission)),
                        )
                    else:
                        if row[:2] != (intent_sha256, claim_sha256):
                            raise RetentionConflict("request already binds a different execution claim")
                        attempt_id = row[2]

                # Serialize acknowledgement and handoff across processes. A
                # crash or lost reply before commit leaves the same admission
                # pending and permits a retry with exactly these stored fields.
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    raw, confirmed = db.execute(
                        "SELECT admission, confirmed FROM retention_requests WHERE request_id = ?", (request_id,),
                    ).fetchone()
                    if confirmed:
                        raise RetentionUncertain("the existing admission requires reconciliation")
                    admission = json.loads(raw)
                    with verify(attempt_id) as allowed:
                        if allowed is not True:
                            return None
                        with self.authority.client.session(self.tenant) as session:
                            if session.admit(dict(admission)) != admission:
                                raise RetentionUncertain("admission acknowledgement differs from the durable request")
                        db.execute("UPDATE retention_requests SET confirmed = 1 WHERE request_id = ?", (request_id,))
            return self._handoff(admission)
        except (RetentionConflict, RetentionUncertain):
            raise
        except Exception as exc:
            raise RetentionUncertain("existing or unconfirmed retention admission requires reconciliation or retry") from exc

    def _handoff(self, admission):
        return RetentionAttempt(
            admission, self.tenant, self.runner.client, self.inspector.client,
            self.validate_profile, self.observe_terminal, self.custody,
        )

    def comparison_handoff(self):
        def admit(**binding):
            return self.admit_comparison(**binding)

        def read(reference):
            return self.read_source(reference)

        return ComparisonHandoff(self.runner, self.inspector, admit, read)

    def read_source(self, reference):
        if not callable(self.validate_profile) or type(reference) is not str or not re.fullmatch(
            r"rcp_[0-9a-f]{32}", reference,
        ):
            raise ValueError("source must name an authentic retained receipt and approved profile")
        with self.inspector.client.session(self.tenant) as inspector:
            inspection = inspector.receipt(reference)
            return read_retained(
                inspector, self.inspector.client.validator, self.validate_profile, tenant=self.tenant,
                attempt_id=inspection["attempt_id"], receipt_ref=reference,
            )

    def admit_comparison(self, *, attempt_id, request_id, request_sha256, reference, candidate):
        handoff = self._handoff({
            "attempt_id": attempt_id, "request_id": request_id, "request_sha256": request_sha256,
            "operation": "compare", "executor_id": self.runner.principal,
            "profile_schema": self.profile_schema, "profile_schema_sha256": self.profile_sha256,
            "reference_attempt_id": reference, "candidate_attempt_id": candidate,
        })
        if not handoff.configured or not self.ready(record_pin=projection.V03_SHA, operation_id="compare"):
            raise ValueError("comparison retention is parked pending owner configuration")
        if len({attempt_id, reference, candidate}) != 3:
            raise ValueError("comparison requires distinct ordered source attempts")
        handoff.admission["admission_sha256"] = sha256(
            self.admission_bytes(handoff.admission, self.authority.client.validator),
        ).hexdigest()
        with self.authority.client.session(self.tenant) as authority:
            if authority.admit(dict(handoff.admission)) != handoff.admission:
                raise RetentionUncertain("comparison admission was not confirmed")
        return handoff
