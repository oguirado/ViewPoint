# @module {"id":"wa.act.t2_trace","layer":1,"owner":"ACT","deps":[],"book":"HB-WA-s2-act"}
"""Source-agnostic, RAM-only trace for a locally submitted T2 command.

The adapter owns transport and authentication.  A trace never treats successful
local admission, a confirmation proposal, or message arrival as engine commit.
No backend or pending JQ2-A/JQ2-B modules are imported here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping


class TraceError(ValueError):
    """Invalid or uncorroborated trace fact (never silently repaired)."""


class TraceConflictError(TraceError):
    """A conflicting outcome, identity or duplicate was observed."""


class TraceUnavailableError(TraceError):
    """A required trusted source is absent rather than measured as zero."""


PHASES = frozenset({"REFUSED", "PROPOSED", "PENDING", "ACCEPTED", "REJECTED", "UNKNOWN"})
_COMMAND_KEYS = frozenset({"tick", "seq", "cmd", "args"})
_OUTCOME_KEYS = frozenset({
    "seq", "requested_tick", "applied_tick", "order_seq_id",
    "status", "reason", "state_sha256",
})
_TRUST_KEYS = frozenset({
    "principal", "company", "role", "mandate", "session", "ticket",
    "stream_ref", "action_id", "source_policy", "basis_sha256", "next_seq",
})
_AUTH_KEYS = frozenset({
    "actor", "channel", "principal", "role", "slot", "slot_kind", "company",
    "mandate", "mandates", "ticket", "source_ticket", "session", "stream_ref",
    "action_id", "consent", "consent_token", "consent_authority",
    "consent_verifier", "main_save_consent", "allow_main_save",
    "automation_active", "automation-active", "order_seq_id",
})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REASON = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def _integer(value: Any, *, min_value: int, field: str) -> int:
    if type(value) is not int or value < min_value:
        raise TraceError(f"{field} must be an integer >= {min_value}")
    return value


def _str(value: Any, field: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or value != value.strip():
        raise TraceError(f"{field} must be a bounded, trimmed string")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise TraceError(f"{field} has invalid UTF-8") from exc
    if len(raw) > limit or any(ch in value for ch in "\r\n\u2028\u2029"):
        raise TraceError(f"{field} exceeds approved metadata or contains line separators")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise TraceError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _json_obj(value: Any, *, path: str = "args") -> None:
    if value is None or type(value) in (bool, int, str):
        if isinstance(value, str):
            value.encode("utf-8", "strict")
        return
    if type(value) is float:
        raise TraceError(f"{path}: floats are not admitted by this T2 contract")
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json_obj(item, path=f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, item in value.items():
            if not isinstance(k, str):
                raise TraceError(f"{path}: JSON keys must be strings")
            k.encode("utf-8", "strict")
            if k in _AUTH_KEYS and not (k == "principal" and type(item) is int):
                raise TraceError(f"{path}: trusted authority field {k!r} in command")
            _json_obj(item, path=f"{path}.{k}")
        return
    raise TraceError(f"{path}: unsupported or opaque data, including frame/receipt bytes")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _handle_key(handle: Any) -> tuple[str, str, int]:
    def attr(name: str) -> Any:
        return handle.get(name) if isinstance(handle, Mapping) else getattr(handle, name, None)

    kind = attr("kind")
    canonical_id = attr("canonical_id")
    generation = attr("generation")
    if not isinstance(kind, str) or not kind or not isinstance(canonical_id, str) or not canonical_id:
        raise TraceError("A canonical handle with kind/id/generation is required (AUSENTE)")
    _integer(generation, min_value=0, field="handle.generation")
    return kind, canonical_id, generation


@dataclass(frozen=True)
class AttemptDraft:
    attempt_id: str
    action_id: str
    stream_ref: str
    source_ticket: str
    command: Mapping[str, Any]


@dataclass(frozen=True)
class EngineOutcome:
    seq: int
    requested_tick: int
    applied_tick: int
    order_seq_id: int
    status: str
    reason: str | None
    state_sha256: str

    @classmethod
    def parse(cls, record: Mapping[str, Any], max_metadata_bytes: int) -> "EngineOutcome":
        if not isinstance(record, Mapping) or set(record) != _OUTCOME_KEYS:
            raise TraceError("engine outcome must have the seven exact Holdings fields")
        for k, minimum in (("seq", 1), ("requested_tick", 0),
                           ("applied_tick", 0), ("order_seq_id", 1)):
            _integer(record[k], min_value=minimum, field=k)
        if record["status"] not in ("accepted", "rejected", "proposed"):
            raise TraceError("invalid engine outcome status")
        reason = record["reason"]
        if reason is not None and not _REASON.fullmatch(_str(reason, "reason", max_metadata_bytes)):
            raise TraceError("invalid engine outcome reason")
        return cls(record["seq"], record["requested_tick"], record["applied_tick"],
                   record["order_seq_id"], record["status"], reason,
                   _sha(record["state_sha256"], "outcome.state_sha256"))


@dataclass(frozen=True)
class TraceAttempt:
    attempt_id: str
    action_id: str
    stream_ref: str
    source_ticket: str
    command: Mapping[str, Any]
    local_status: str
    local_reason: str | None
    emitted_sha256: str | None
    engine_outcome: EngineOutcome | None
    phase: str
    committed_state_sha256: str | None


@dataclass(frozen=True)
class CommitKey:
    """Lookup key: no hash supplied by the untrusted outcome itself."""
    source_policy: str
    stream_ref: str
    source_ticket: str
    session: str
    seq: int
    requested_tick: int
    order_seq_id: int


@dataclass(frozen=True)
class _Trust:
    handle: tuple[str, str, int]
    source_policy: str
    principal: str
    company: str
    role: str
    mandate: str
    session: str
    ticket: str
    stream_ref: str
    action_id: str
    basis_sha256: str
    next_seq: int

    @property
    def cursor_key(self) -> tuple[Any, ...]:
        return (self.source_policy, self.handle, self.principal, self.company,
                self.role, self.mandate, self.session, self.ticket, self.stream_ref)


class TraceRecorder:
    """Immutable attempt snapshots, authenticated join and conservative dispatch halt.

    Limits are mandatory caller policy, not guesses about the VM/host.
    `dispatch` observes bytes only after its injected sink returns successfully.
    Trusted broker and post-commit lookup provenance must be enforced by adapters.
    """

    def __init__(self, *, max_attempts: int, max_command_bytes: int,
                 max_metadata_bytes: int, max_emitted_bytes: int,
                 timeout_ns: int, approved_arg_keys: tuple[str, ...]) -> None:
        for k, v in (("max_attempts", max_attempts),
                     ("max_command_bytes", max_command_bytes),
                     ("max_metadata_bytes", max_metadata_bytes),
                     ("max_emitted_bytes", max_emitted_bytes),
                     ("timeout_ns", timeout_ns)):
            _integer(v, min_value=1, field=k)
        if type(approved_arg_keys) is not tuple or any(type(x) is not str for x in approved_arg_keys):
            raise TraceError("approved_arg_keys must be an explicitly configured tuple")
        self.max_attempts = max_attempts
        self.max_command_bytes = max_command_bytes
        self.max_metadata_bytes = max_metadata_bytes
        self.max_emitted_bytes = max_emitted_bytes
        self.timeout_ns = timeout_ns
        self.approved_arg_keys = frozenset(approved_arg_keys)
        self._drafts: dict[str, AttemptDraft] = {}
        self._attempts: dict[str, TraceAttempt] = {}
        self._trust: dict[str, _Trust] = {}
        self._clock: dict[str, Callable[[], int]] = {}
        self._began: dict[str, int] = {}
        self._context: dict[str, Mapping[str, Any]] = {}
        self._handle: dict[str, Any] = {}
        self._cursors: dict[tuple[Any, ...], int] = {}
        self._open: set[tuple[tuple[Any, ...], int]] = set()
        self._proposals: dict[str, Mapping[str, Any]] = {}
        self._events: list[tuple[str, str, str | None]] = []
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted

    def begin(self, *, command: Mapping[str, Any], handle: Any,
              trusted_context: Mapping[str, Any], clock: Callable[[], int]) -> AttemptDraft:
        if self._halted:
            raise TraceConflictError("dispatch halted after incomplete sink/adapter error")
        if len(self._drafts) >= self.max_attempts or not callable(clock):
            raise TraceError("attempt limit or required injected clock absent")
        if not isinstance(command, Mapping) or set(command) != _COMMAND_KEYS:
            raise TraceError("command must contain exactly tick, seq, cmd, args")
        _integer(command["tick"], min_value=0, field="tick")
        _integer(command["seq"], min_value=1, field="seq")
        _str(command["cmd"], "cmd", self.max_metadata_bytes)
        if type(command["args"]) is not dict:
            raise TraceError("args must be an object")
        _json_obj(command["args"])
        cmd_copy = {k: _thaw(command[k]) for k in ("tick", "seq", "cmd", "args")}
        encoded = json.dumps(cmd_copy, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > self.max_command_bytes:
            raise TraceError("command exceeds configured byte limit")
        if not isinstance(trusted_context, Mapping) or set(trusted_context) != _TRUST_KEYS:
            raise TraceUnavailableError("AUSENTE: trusted stream, ticket, session and mandate context")
        trusted = {}
        for k in _TRUST_KEYS - {"basis_sha256", "next_seq"}:
            trusted[k] = _str(trusted_context[k], f"trusted_context.{k}", self.max_metadata_bytes)
        trusted["basis_sha256"] = _sha(trusted_context["basis_sha256"], "basis_sha256")
        trusted["next_seq"] = _integer(trusted_context["next_seq"], min_value=1, field="next_seq")
        hk = _handle_key(handle)
        if (trusted["source_policy"], hk[0]) not in (("godot", "godot_window"), ("cdp", "cdp_tab")):
            raise TraceError("source-specific handle/policy mismatch; no cross-policy relabeling")
        binding = _Trust(handle=hk, **trusted)
        key = binding.cursor_key
        cursor = self._cursors.setdefault(key, binding.next_seq)
        if cursor != binding.next_seq or cmd_copy["seq"] != cursor:
            raise TraceConflictError("trusted sequence cursor mismatch; no arrival-order repair")
        if (key, cursor) in self._open:
            raise TraceConflictError("another unadmitted attempt owns this sequence")
        now = _integer(clock(), min_value=0, field="clock")
        ident = f"trace-{len(self._drafts) + 1:08d}"
        draft = AttemptDraft(ident, binding.action_id, binding.stream_ref,
                             binding.ticket, _freeze(cmd_copy))
        self._drafts[ident] = draft
        self._trust[ident] = binding
        self._clock[ident] = clock
        self._began[ident] = now
        self._context[ident] = MappingProxyType(dict(trusted))
        self._handle[ident] = handle  # volatile only; receipt checked against immutable _Trust.handle
        self._open.add((key, cursor))
        self._events.append((ident, "BEGIN", None))
        return draft

    def _need_draft(self, attempt_id: str) -> AttemptDraft:
        if attempt_id not in self._drafts:
            raise TraceUnavailableError("AUSENTE: unknown attempt_id")
        if attempt_id in self._attempts:
            raise TraceConflictError("attempt has already passed local admission")
        return self._drafts[attempt_id]

    def admit(self, *, attempt_id: str, receipt: Any,
              emitted_bytes: bytes | None, proposal: Mapping[str, Any] | None = None) -> TraceAttempt:
        draft = self._need_draft(attempt_id)
        trust = self._trust[attempt_id]
        def field(name: str) -> Any:
            return receipt.get(name) if isinstance(receipt, Mapping) else getattr(receipt, name, None)

        if field("action_id") != draft.action_id or _handle_key(field("handle")) != trust.handle:
            raise TraceConflictError("receipt action_id/handle disagrees with frozen attempt")
        status = field("status")
        reason = field("reason")
        if type(status) is not str or status not in ("admitted", "rejected", "proposed", "needs_confirm"):
            raise TraceError("receipt has unsupported local admission status")
        if reason is not None:
            reason = _str(reason, "receipt.reason", self.max_metadata_bytes)
        if emitted_bytes is not None and (type(emitted_bytes) is not bytes or not emitted_bytes
                                          or len(emitted_bytes) > self.max_emitted_bytes):
            raise TraceError("emitted_bytes must be observed nonempty, bounded bytes or None")
        is_proposal = status in ("proposed", "needs_confirm") or (status == "rejected" and reason == "NEEDS_CONFIRM")
        if is_proposal:
            if emitted_bytes is not None or not isinstance(proposal, Mapping):
                raise TraceConflictError("confirmation proposal cannot emit or omit proposal facts")
            expected = {"modal_id", "cmd", "target_id", "terms", "basis_sha256"}
            if set(proposal) != expected:
                raise TraceError("proposal must provide exact confirmation binding fields")
            if proposal["basis_sha256"] != trust.basis_sha256:
                raise TraceConflictError("proposal basis disagrees with trusted pre-action basis")
            meta = {
                "modal_id": _str(proposal["modal_id"], "proposal.modal_id", self.max_metadata_bytes),
                "cmd": _str(proposal["cmd"], "proposal.cmd", self.max_metadata_bytes),
                "target_id": _str(proposal["target_id"], "proposal.target_id", self.max_metadata_bytes, empty=True),
                "basis_sha256": _sha(proposal["basis_sha256"], "proposal.basis_sha256"),
            }
            if not isinstance(proposal["terms"], dict):
                raise TraceError("proposal.terms must be an object")
            _json_obj(proposal["terms"])
            terms_raw = json.dumps(proposal["terms"], sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")
            if len(terms_raw) > self.max_command_bytes:
                raise TraceError("proposal terms exceed configured limit")
            meta["terms_sha256"] = hashlib.sha256(terms_raw).hexdigest()
            self._proposals[attempt_id] = MappingProxyType(meta)
            local, phase, emitted_digest = "PROPOSED", "PROPOSED", None
        elif status == "rejected":
            if emitted_bytes is not None or proposal is not None:
                raise TraceConflictError("local refusal must not invent emitted bytes or proposal")
            local, phase, emitted_digest = "REFUSED", "REFUSED", None
        else:
            if proposal is not None:
                raise TraceConflictError("admission cannot masquerade as a confirmation proposal")
            if emitted_bytes is None:
                local, phase, emitted_digest = "INCOMPLETE", "UNKNOWN", None
                reason = "EMISSION_UNOBSERVED"
                self._halted = True
            else:
                local, phase = "ADMITTED", "PENDING"
                emitted_digest = hashlib.sha256(emitted_bytes).hexdigest()
                self._cursors[trust.cursor_key] += 1
        row = TraceAttempt(draft.attempt_id, draft.action_id, draft.stream_ref,
                           draft.source_ticket, draft.command, local, reason, emitted_digest,
                           None, phase, None)
        self._attempts[attempt_id] = row
        self._open.discard((trust.cursor_key, trust.next_seq))
        self._events.append((attempt_id, phase, reason))
        return row

    def dispatch(self, *, attempt_id: str, adapter: Callable[..., Any],
                 sink: Callable[[bytes], Any]) -> TraceAttempt:
        """Adapter(command, handle, trusted_context, witnessed_sink) -> Receipt or (Receipt, proposal).

        Witness only hashes bytes handed to a sink which returned successfully.
        Any adapter/sink failure creates an UNKNOWN incomplete row and halts dispatch.
        """
        draft = self._need_draft(attempt_id)
        if self._halted or not callable(adapter) or not callable(sink):
            raise TraceError("dispatch halted or required structural adapter/sink missing")
        observed: list[bytes] = []
        attempted = False

        def witnessed_sink(payload: bytes) -> None:
            nonlocal attempted
            if attempted:
                raise TraceConflictError("multiple sink emissions for one attempt")
            attempted = True
            if type(payload) is not bytes or not payload or len(payload) > self.max_emitted_bytes:
                raise TraceError("invalid emitted bytes")
            sink(payload)
            observed.append(payload)

        try:
            answer = adapter(_thaw(draft.command), self._handle[attempt_id],
                             self._context[attempt_id], witnessed_sink)
            if isinstance(answer, tuple) and len(answer) == 2:
                receipt, proposal = answer
            else:
                receipt, proposal = answer, None
            return self.admit(attempt_id=attempt_id, receipt=receipt,
                              emitted_bytes=observed[0] if observed else None, proposal=proposal)
        except Exception:
            if attempt_id not in self._attempts:
                self.sink_failure(attempt_id=attempt_id,
                                  observed_bytes=observed[0] if observed else None)
            raise

    def sink_failure(self, *, attempt_id: str, observed_bytes: bytes | None = None) -> TraceAttempt:
        """Conservatively halt: an uncertain send must never be reused as a local refusal."""
        draft = self._need_draft(attempt_id)
        trust = self._trust[attempt_id]
        if observed_bytes is not None and (type(observed_bytes) is not bytes or
                                          not observed_bytes or len(observed_bytes) > self.max_emitted_bytes):
            raise TraceError("invalid witnessed bytes during sink failure")
        digest = hashlib.sha256(observed_bytes).hexdigest() if observed_bytes is not None else None
        if digest is not None:
            self._cursors[trust.cursor_key] += 1
        self._halted = True
        row = TraceAttempt(draft.attempt_id, draft.action_id, draft.stream_ref,
                           draft.source_ticket, draft.command, "INCOMPLETE", "SINK_OR_ADAPTER_FAILURE",
                           digest, None, "UNKNOWN", None)
        self._attempts[attempt_id] = row
        self._open.discard((trust.cursor_key, trust.next_seq))
        self._events.append((attempt_id, "UNKNOWN", "SINK_OR_ADAPTER_FAILURE"))
        return row

    def record_outcome(self, *, attempt_id: str, outcome: Mapping[str, Any],
                       authenticated_source: Mapping[str, str],
                       lookup_post_commit: Callable[[CommitKey], Mapping[str, Any]]) -> TraceAttempt:
        """Join only by known attempt plus broker identity, seq/tick and independent commit proof."""
        row = self._attempts.get(attempt_id)
        if row is None or row.emitted_sha256 is None or row.local_status != "ADMITTED":
            raise TraceConflictError("only actually emitted admitted attempts can receive engine outcomes")
        trust = self._trust[attempt_id]
        required_auth = {"stream_ref": trust.stream_ref, "source_ticket": trust.ticket,
                         "session": trust.session, "source_policy": trust.source_policy}
        if not isinstance(authenticated_source, Mapping) or dict(authenticated_source) != required_auth:
            raise TraceConflictError("unauthenticated stream/ticket/session or cross-source join")
        parsed = EngineOutcome.parse(outcome, self.max_metadata_bytes)
        if parsed.seq != row.command["seq"] or parsed.requested_tick != row.command["tick"]:
            raise TraceConflictError("sequence/requested tick disagree; arrival order cannot repair")
        if not callable(lookup_post_commit):
            raise TraceUnavailableError("AUSENTE: independent post-commit lookup")
        key = CommitKey(trust.source_policy, trust.stream_ref, trust.ticket, trust.session,
                        parsed.seq, parsed.requested_tick, parsed.order_seq_id)
        proof = lookup_post_commit(key)
        proof_keys = {"stream_ref", "source_ticket", "session", "seq", "requested_tick",
                      "applied_tick", "order_seq_id", "state_sha256"}
        if not isinstance(proof, Mapping) or set(proof) != proof_keys:
            raise TraceUnavailableError("AUSENTE: revision-bound post-commit proof")
        for k, v in (("stream_ref", trust.stream_ref), ("source_ticket", trust.ticket),
                     ("session", trust.session), ("seq", parsed.seq),
                     ("requested_tick", parsed.requested_tick),
                     ("applied_tick", parsed.applied_tick),
                     ("order_seq_id", parsed.order_seq_id)):
            if proof[k] != v or type(proof[k]) is not type(v):
                raise TraceConflictError(f"post-commit proof {k} mismatch")
        committed_hash = _sha(proof["state_sha256"], "trusted post-commit hash")
        if committed_hash != parsed.state_sha256:
            raise TraceConflictError("reported engine hash differs from verified post-commit hash")
        if parsed.status == "rejected" and committed_hash != trust.basis_sha256:
            raise TraceConflictError("engine rejection mutated the committed simulation hash")
        if row.engine_outcome is not None:
            if row.engine_outcome == parsed and row.committed_state_sha256 == committed_hash:
                return row  # authenticated identical retransmission
            raise TraceConflictError("conflicting duplicate engine outcome")
        phase = {"accepted": "ACCEPTED", "rejected": "REJECTED", "proposed": "PROPOSED"}[parsed.status]
        updated = replace(row, phase=phase, engine_outcome=parsed,
                          committed_state_sha256=committed_hash)
        self._attempts[attempt_id] = updated
        self._events.append((attempt_id, phase, parsed.reason))
        return updated

    def timeout(self, attempt_id: str) -> TraceAttempt:
        row = self._attempts.get(attempt_id)
        if row is None:
            raise TraceUnavailableError("AUSENTE: no admission to time out")
        if row.phase != "PENDING":
            return row
        elapsed = _integer(self._clock[attempt_id](), min_value=0, field="clock") - self._began[attempt_id]
        if elapsed < 0:
            raise TraceConflictError("clock moved backwards")
        if elapsed < self.timeout_ns:
            return row
        updated = replace(row, phase="UNKNOWN")
        self._attempts[attempt_id] = updated
        self._events.append((attempt_id, "UNKNOWN", "TIMEOUT"))
        return updated

    def attempt(self, attempt_id: str) -> TraceAttempt | AttemptDraft:
        if attempt_id not in self._drafts:
            raise TraceUnavailableError("AUSENTE: unknown attempt")
        return self._attempts.get(attempt_id, self._drafts[attempt_id])

    def cursor(self, attempt_id: str) -> int:
        if attempt_id not in self._trust:
            raise TraceUnavailableError("AUSENTE: unknown stream")
        return self._cursors[self._trust[attempt_id].cursor_key]

    def proposal_metadata(self, attempt_id: str) -> dict[str, Any] | None:
        meta = self._proposals.get(attempt_id)
        return None if meta is None else dict(meta)

    def audit(self) -> list[dict[str, Any]]:
        """Approved, bounded metadata only: no raw bytes, receipts, tree dumps or terms."""
        result: list[dict[str, Any]] = []
        for ident in self._drafts:
            if ident not in self._attempts:
                continue
            row = self._attempts[ident]
            command = row.command
            approved_args: dict[str, Any] = {}
            for k, v in command["args"].items():
                if k in self.approved_arg_keys and type(v) in (str, int, bool):
                    if type(v) is not str or len(v.encode("utf-8")) <= self.max_metadata_bytes:
                        approved_args[k] = v
            result.append({
                "attempt_id": row.attempt_id, "action_id": row.action_id,
                "stream_ref": row.stream_ref, "source_ticket": row.source_ticket,
                "command": {"tick": command["tick"], "seq": command["seq"],
                            "cmd": command["cmd"], "args": approved_args},
                "local_status": row.local_status, "local_reason": row.local_reason,
                "emitted_sha256": row.emitted_sha256,
                "engine_outcome": asdict(row.engine_outcome) if row.engine_outcome else None,
                "phase": row.phase, "committed_state_sha256": row.committed_state_sha256,
            })
        return result

    def events(self) -> tuple[tuple[str, str, str | None], ...]:
        return tuple(self._events)
