# @module {"id":"wa.act.jq3_t2_broker_client","layer":2,"owner":"ACT","deps":["wa.state.schema"],"book":"HB-WA-s2-act"}
"""Closed brokered IPC T2 batch client with pre-effect validation and attempt reconciliation.

Enforces:
- INV-E2-1: Closes batch membership (`producer_done`) before admission.
- INV-E2-2: Validates entire batch structure and per-producer sequence before deterministic
  sorting and before any effect; a malformed final record leaves zero effects.
- INV-E2-3: Preserves accepted predecessor effects on structurally valid domain rejection
  and advances only trusted attempt bookkeeping.
- INV-E2-4: Fences stale or mismatched `port_epoch` values on both submit_batch and reconcile.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Mapping

from wender.state import schema

RECORD_KEYS = ("tick", "seq", "cmd", "args")
RECORD_KEY_SET = frozenset(RECORD_KEYS)

RECEIPT_KEYS = (
    "accepted_count",
    "acknowledged",
    "attempt_seq",
    "effects",
    "error",
    "ok",
    "port_epoch",
    "reconciled",
    "rejected_index",
    "status",
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# @anchor:wa.act.jq3_t2_broker_client.BrokerClientError
class BrokerClientError(ValueError):
    """Base error for fail-closed T2 broker batch admission, structure, epoch, or reconciliation."""


class PrerequisiteAbsent(BrokerClientError):
    """Raised when a required broker or closed command registry dependency is absent."""


class BatchMembershipIncomplete(BrokerClientError):
    """Raised when batch membership is not closed before admission (INV-E2-1)."""


class MalformedBatchError(BrokerClientError):
    """Raised when any batch record or per-producer sequence is malformed (INV-E2-2)."""


class StalePortEpochError(BrokerClientError):
    """Raised when a batch or reconciliation uses a stale or invalid port epoch (INV-E2-4)."""


class DomainRejectionError(BrokerClientError):
    """Raised by a domain handler when a structurally valid command violates domain constraints (INV-E2-3)."""


class LostAckError(RuntimeError):
    """Raised by a broker transport when commit succeeds but acknowledgment is lost."""


class BrokerReceipt(dict):
    """Canonical integer/string JSON-compatible receipt supporting both key and attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@dataclass(frozen=True)
class CommandSpec:
    """Closed command specification with optional structural and domain validators."""

    name: str
    required_args: tuple[str, ...] = ()
    optional_args: tuple[str, ...] = ("producer",)
    arg_types: Mapping[str, type | tuple[type, ...]] | None = None
    domain_validator: Callable[[Mapping[str, Any], list[dict[str, Any]]], bool | None] | None = None

    def validate_structure(self, args: Mapping[str, Any]) -> None:
        if not isinstance(args, dict):
            raise MalformedBatchError("command args must be a dict")
        allowed = set(self.required_args) | set(self.optional_args)
        missing = [k for k in self.required_args if k not in args]
        if missing:
            raise MalformedBatchError(
                "command %r missing required args: %s" % (self.name, missing)
            )
        if allowed:
            extra = [k for k in args if k not in allowed]
            if extra:
                raise MalformedBatchError(
                    "command %r received unknown args: %s" % (self.name, extra)
                )
        if self.arg_types:
            for key, expected_type in self.arg_types.items():
                if key in args:
                    val = args[key]
                    if expected_type is int or (
                        isinstance(expected_type, tuple) and int in expected_type
                    ):
                        if isinstance(val, bool):
                            raise MalformedBatchError(
                                "command %r arg %r must not be bool" % (self.name, key)
                            )
                    if not isinstance(val, expected_type):
                        raise MalformedBatchError(
                            "command %r arg %r has invalid type" % (self.name, key)
                        )

    def check_domain(self, args: Mapping[str, Any], prior_effects: list[dict[str, Any]]) -> None:
        if self.domain_validator is not None:
            result = self.domain_validator(args, prior_effects)
            if result is False:
                raise DomainRejectionError("domain validator rejected %r" % (self.name,))


class ClosedCommandRegistry:
    """Explicit injected closed command registry for T2 batch records."""

    def __init__(self, commands: Mapping[str, CommandSpec | Callable[..., Any] | Mapping[str, Any]]):
        if not isinstance(commands, Mapping) or not commands:
            raise PrerequisiteAbsent("closed command registry prerequisite AUSENTE")
        self._commands: dict[str, CommandSpec | Callable[..., Any] | Mapping[str, Any]] = {}
        for name, spec in commands.items():
            if not isinstance(name, str) or not name.strip():
                raise MalformedBatchError("command name must be a non-empty string")
            if not (isinstance(spec, (CommandSpec, Mapping)) or callable(spec)):
                raise MalformedBatchError("invalid command specification for %r" % (name,))
            self._commands[name] = spec

    def __contains__(self, cmd: object) -> bool:
        return isinstance(cmd, str) and cmd in self._commands

    def __len__(self) -> int:
        return len(self._commands)

    def get(self, cmd: str) -> Any:
        return self._commands.get(cmd)

    def validate_structure(self, cmd: str, args: Mapping[str, Any]) -> None:
        if cmd not in self._commands:
            raise MalformedBatchError("unregistered command %r in closed registry" % (cmd,))
        spec = self._commands[cmd]
        if isinstance(spec, CommandSpec):
            spec.validate_structure(args)
        elif isinstance(spec, Mapping):
            req = spec.get("required_args", ())
            opt = spec.get("optional_args", ("producer",))
            for k in req:
                if k not in args:
                    raise MalformedBatchError("command %r missing required arg %r" % (cmd, k))
            if "strict_keys" in spec and spec["strict_keys"]:
                allowed = set(req) | set(opt)
                for k in args:
                    if k not in allowed:
                        raise MalformedBatchError("command %r unknown arg %r" % (cmd, k))
            struct_fn = spec.get("validate_structure")
            if callable(struct_fn):
                struct_fn(args)

    def evaluate_domain(
        self, cmd: str, args: Mapping[str, Any], prior_effects: list[dict[str, Any]]
    ) -> None:
        spec = self._commands[cmd]
        if isinstance(spec, CommandSpec):
            spec.check_domain(args, prior_effects)
        elif isinstance(spec, Mapping):
            domain_fn = spec.get("domain_validator")
            if callable(domain_fn):
                if domain_fn(args, prior_effects) is False:
                    raise DomainRejectionError("domain rejected %r" % (cmd,))
        elif callable(spec):
            if spec(args, prior_effects) is False:
                raise DomainRejectionError("domain rejected %r" % (cmd,))


class FakeT2Broker:
    """In-RAM deterministic fake broker for offline T2 batch commit and reconciliation tests."""

    def __init__(self, *, port_epoch: int = 1, lose_ack_attempts: set[int] | None = None):
        if not _is_int(port_epoch) or port_epoch < 0:
            raise StalePortEpochError("broker port_epoch must be a non-negative integer")
        self._lock = threading.RLock()
        self.port_epoch = port_epoch
        self.effect_log: list[dict[str, Any]] = []
        self.committed_attempts: dict[tuple[int, int], dict[str, Any]] = {}
        self.lose_ack_attempts: set[int] = set(lose_ack_attempts or ())
        self.lose_next_ack: bool = False

    def advance_port_epoch(self, new_epoch: int) -> int:
        if not _is_int(new_epoch) or new_epoch <= self.port_epoch:
            raise StalePortEpochError(
                "new port_epoch %r must be an integer > current %r"
                % (new_epoch, self.port_epoch)
            )
        with self._lock:
            self.port_epoch = new_epoch
            return self.port_epoch

    def append_effect(self, effect: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            copied = copy.deepcopy(effect)
            self.effect_log.append(copied)
            return copy.deepcopy(copied)

    def record_attempt(self, port_epoch: int, attempt_seq: int, record: dict[str, Any]) -> None:
        with self._lock:
            self.committed_attempts[(port_epoch, attempt_seq)] = copy.deepcopy(record)

    def get_committed_attempt(self, port_epoch: int, attempt_seq: int) -> dict[str, Any] | None:
        with self._lock:
            rec = self.committed_attempts.get((port_epoch, attempt_seq))
            return copy.deepcopy(rec) if rec is not None else None

    def acknowledge(self, attempt_seq: int, *, port_epoch: int) -> bool:
        with self._lock:
            if port_epoch != self.port_epoch:
                raise StalePortEpochError("broker port_epoch changed before ack")
            if self.lose_next_ack or attempt_seq in self.lose_ack_attempts:
                self.lose_next_ack = False
                self.lose_ack_attempts.discard(attempt_seq)
                raise LostAckError("commit succeeded for attempt %d but ack was lost" % attempt_seq)
            return True


def _validate_structure_and_sequences(
    records: Any,
    command_registry: ClosedCommandRegistry | Mapping[str, Any],
    committed_producer_seq: Mapping[str, int],
    committed_producer_tick: Mapping[str, int],
    producer_done: Any,
) -> list[dict[str, Any]]:
    """Validate membership completion, closed record keys/types, and per-producer seq before sorting."""
    if isinstance(producer_done, bool):
        if not producer_done:
            raise BatchMembershipIncomplete(
                "INV-E2-1: batch membership is not closed (producer_done=False)"
            )
        producer_done_map = None
    elif isinstance(producer_done, Mapping):
        if not producer_done:
            raise BatchMembershipIncomplete(
                "INV-E2-1: producer_done mapping cannot be empty"
            )
        for pk, pv in producer_done.items():
            if not isinstance(pk, str) or not pk.strip() or not isinstance(pv, bool):
                raise BatchMembershipIncomplete(
                    "INV-E2-1: producer_done keys must be non-empty strings and values bool"
                )
            if not pv:
                raise BatchMembershipIncomplete(
                    "INV-E2-1: producer %r has not completed batch membership" % (pk,)
                )
        producer_done_map = dict(producer_done)
    else:
        raise BatchMembershipIncomplete(
            "INV-E2-1: producer_done must be True or an all-True producer mapping"
        )

    if not isinstance(records, (list, tuple)) or len(records) == 0:
        raise MalformedBatchError("INV-E2-2: records must be a non-empty list or tuple")

    registry_obj = (
        command_registry
        if isinstance(command_registry, ClosedCommandRegistry)
        else ClosedCommandRegistry(command_registry)
    )

    staged_producer_seq: dict[str, int] = dict(committed_producer_seq)
    staged_producer_tick: dict[str, int] = dict(committed_producer_tick)
    validated: list[dict[str, Any]] = []

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise MalformedBatchError("INV-E2-2: record[%d] must be a dict" % idx)
        if set(rec.keys()) != RECORD_KEY_SET:
            raise MalformedBatchError(
                "INV-E2-2: record[%d] keys must be exactly %s, got %s"
                % (idx, sorted(RECORD_KEY_SET), sorted(rec.keys()))
            )
        tick = rec["tick"]
        seq = rec["seq"]
        cmd = rec["cmd"]
        args = rec["args"]

        if not _is_int(tick) or tick < 0:
            raise MalformedBatchError(
                "INV-E2-2: record[%d].tick must be a non-negative int" % idx
            )
        if not _is_int(seq) or seq < 0:
            raise MalformedBatchError(
                "INV-E2-2: record[%d].seq must be a non-negative int" % idx
            )
        if not isinstance(cmd, str) or not cmd.strip():
            raise MalformedBatchError(
                "INV-E2-2: record[%d].cmd must be a non-empty string" % idx
            )
        if not isinstance(args, dict):
            raise MalformedBatchError(
                "INV-E2-2: record[%d].args must be a dict" % idx
            )
        try:
            schema.dumps(args)
        except Exception as exc:
            raise MalformedBatchError(
                "INV-E2-2: record[%d].args failed canonical JSON validation: %s" % (idx, exc)
            ) from exc

        producer = args.get("producer", "__default__")
        if not isinstance(producer, str) or not producer.strip():
            raise MalformedBatchError(
                "INV-E2-2: record[%d].args.producer must be a non-empty string" % idx
            )
        if producer_done_map is not None:
            if producer == "__default__":
                if "__default__" not in producer_done_map and len(producer_done_map) != 1:
                    raise BatchMembershipIncomplete(
                        "INV-E2-1: default producer missing from producer_done mapping"
                    )
            elif producer not in producer_done_map:
                raise BatchMembershipIncomplete(
                    "INV-E2-1: producer %r missing from producer_done mapping" % (producer,)
                )

        registry_obj.validate_structure(cmd, args)

        # Check per-producer sequence in input order BEFORE deterministic sorting.
        if producer not in staged_producer_seq:
            if seq not in (0, 1):
                raise MalformedBatchError(
                    "INV-E2-2: initial seq for producer %r must be 0 or 1, got %d"
                    % (producer, seq)
                )
        else:
            expected_seq = staged_producer_seq[producer] + 1
            if seq != expected_seq:
                raise MalformedBatchError(
                    "INV-E2-2: out-of-order or non-contiguous seq for producer %r at record[%d]: "
                    "expected %d, got %d" % (producer, idx, expected_seq, seq)
                )
        if producer in staged_producer_tick and tick < staged_producer_tick[producer]:
            raise MalformedBatchError(
                "INV-E2-2: tick regression for producer %r at record[%d]: %d < %d"
                % (producer, idx, tick, staged_producer_tick[producer])
            )

        staged_producer_seq[producer] = seq
        staged_producer_tick[producer] = tick
        validated.append(
            {
                "tick": tick,
                "seq": seq,
                "cmd": cmd,
                "args": copy.deepcopy(args),
            }
        )

    return validated


def _deterministic_sort(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda r: (
            r["tick"],
            r["seq"],
            str(r["args"].get("producer", "")),
            r["cmd"],
            schema.dumps(r["args"]),
        ),
    )


def _build_receipt(
    *,
    ok: int,
    status: str,
    attempt_seq: int,
    port_epoch: int,
    accepted_count: int,
    rejected_index: int | None,
    acknowledged: bool,
    reconciled: bool,
    effects: list[dict[str, Any]],
    error: str | None,
) -> BrokerReceipt:
    payload = {
        "ok": int(ok),
        "status": str(status),
        "attempt_seq": int(attempt_seq),
        "port_epoch": int(port_epoch),
        "accepted_count": int(accepted_count),
        "rejected_index": None if rejected_index is None else int(rejected_index),
        "acknowledged": int(bool(acknowledged)),
        "reconciled": int(bool(reconciled)),
        "effects": copy.deepcopy(effects),
        "error": None if error is None else str(error),
    }
    schema.dumps(payload)
    return BrokerReceipt(payload)


# @anchor:wa.act.jq3_t2_broker_client.T2BrokerClient
class T2BrokerClient:
    """Fail-closed T2 IPC batch client with injected command registry and broker."""

    def __init__(
        self,
        broker: Any,
        command_registry: ClosedCommandRegistry | Mapping[str, Any],
        *,
        port_epoch: int | None = None,
        clock: Callable[[], int] | None = None,
    ):
        if broker is None:
            raise PrerequisiteAbsent("broker prerequisite AUSENTE")
        if command_registry is None:
            raise PrerequisiteAbsent("closed command registry prerequisite AUSENTE")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.broker = broker
        self.command_registry = (
            command_registry
            if isinstance(command_registry, ClosedCommandRegistry)
            else ClosedCommandRegistry(command_registry)
        )
        initial_epoch = (
            getattr(broker, "port_epoch", 1) if port_epoch is None else port_epoch
        )
        if not _is_int(initial_epoch) or initial_epoch < 0:
            raise StalePortEpochError("port_epoch must be a non-negative integer")
        self._port_epoch = initial_epoch
        self._clock = time.monotonic_ns if clock is None else clock
        self._lock = threading.RLock()
        self._trusted_attempt_seq = 0
        self._producer_seq: dict[str, int] = {}
        self._producer_tick: dict[str, int] = {}
        self._attempts: dict[int, dict[str, Any]] = {}
        self._uncertain_attempt_seq: int | None = None

    @property
    def port_epoch(self) -> int:
        with self._lock:
            broker_epoch = getattr(self.broker, "port_epoch", self._port_epoch)
            if _is_int(broker_epoch) and broker_epoch > self._port_epoch:
                self._port_epoch = broker_epoch
            return self._port_epoch

    @property
    def trusted_attempt_seq(self) -> int:
        with self._lock:
            return self._trusted_attempt_seq

    @property
    def uncertain_attempt_seq(self) -> int | None:
        with self._lock:
            return self._uncertain_attempt_seq

    @property
    def producer_sequences(self) -> dict[str, int]:
        with self._lock:
            return dict(self._producer_seq)

    @property
    def effect_log(self) -> list[dict[str, Any]]:
        log = getattr(self.broker, "effect_log", None)
        if isinstance(log, list):
            return copy.deepcopy(log)
        return []

    def advance_port_epoch(self, new_epoch: int) -> int:
        with self._lock:
            if not _is_int(new_epoch) or new_epoch <= self.port_epoch:
                raise StalePortEpochError(
                    "new port_epoch %r must exceed current %r" % (new_epoch, self.port_epoch)
                )
            if hasattr(self.broker, "advance_port_epoch"):
                self.broker.advance_port_epoch(new_epoch)
            elif hasattr(self.broker, "port_epoch"):
                self.broker.port_epoch = new_epoch
            self._port_epoch = new_epoch
            self._uncertain_attempt_seq = None
            return self._port_epoch

    def _check_port_epoch(self, port_epoch: Any) -> int:
        if not _is_int(port_epoch) or port_epoch < 0:
            raise StalePortEpochError("INV-E2-4: port_epoch must be a non-negative int")
        active_epoch = self.port_epoch
        if port_epoch != active_epoch:
            raise StalePortEpochError(
                "INV-E2-4: stale or mismatched port_epoch %d (current=%d)"
                % (port_epoch, active_epoch)
            )
        return active_epoch

    # @anchor:wa.act.jq3_t2_broker_client.submit_batch
    def submit_batch(
        self, records: Any, *, port_epoch: int, producer_done: Any
    ) -> BrokerReceipt:
        """Validate and submit a closed T2 command batch at `port_epoch`."""
        with self._lock:
            active_epoch = self._check_port_epoch(port_epoch)
            if self._uncertain_attempt_seq is not None:
                raise BrokerClientError(
                    "attempt %d is uncertain after lost ack; call reconcile first"
                    % self._uncertain_attempt_seq
                )

            validated = _validate_structure_and_sequences(
                records,
                self.command_registry,
                self._producer_seq,
                self._producer_tick,
                producer_done,
            )
            ordered = _deterministic_sort(validated)

            # Structural admission succeeded: advance trusted attempt bookkeeping.
            attempt_seq = self._trusted_attempt_seq + 1
            self._trusted_attempt_seq = attempt_seq

            accepted_effects: list[dict[str, Any]] = []
            rejected_index: int | None = None
            rejection_error: str | None = None

            prior_effects = self.effect_log
            for idx, rec in enumerate(ordered):
                cmd = rec["cmd"]
                args = rec["args"]
                try:
                    self.command_registry.evaluate_domain(
                        cmd, args, prior_effects + accepted_effects
                    )
                except DomainRejectionError as exc:
                    rejected_index = idx
                    rejection_error = str(exc)
                    break

                effect = {
                    "attempt_seq": attempt_seq,
                    "port_epoch": active_epoch,
                    "tick": rec["tick"],
                    "seq": rec["seq"],
                    "cmd": cmd,
                    "args": copy.deepcopy(args),
                }
                self.broker.append_effect(effect)
                accepted_effects.append(effect)
                producer = args.get("producer", "__default__")
                self._producer_seq[producer] = rec["seq"]
                self._producer_tick[producer] = rec["tick"]

            committed_meta = {
                "attempt_seq": attempt_seq,
                "port_epoch": active_epoch,
                "accepted_count": len(accepted_effects),
                "rejected_index": rejected_index,
                "effects": copy.deepcopy(accepted_effects),
                "error": rejection_error,
                "domain_rejected": rejected_index is not None,
            }
            self.broker.record_attempt(active_epoch, attempt_seq, committed_meta)

            try:
                self.broker.acknowledge(attempt_seq, port_epoch=active_epoch)
            except (LostAckError, TimeoutError) as exc:
                self._uncertain_attempt_seq = attempt_seq
                uncertain_receipt = _build_receipt(
                    ok=0,
                    status="uncertain",
                    attempt_seq=attempt_seq,
                    port_epoch=active_epoch,
                    accepted_count=len(accepted_effects),
                    rejected_index=rejected_index,
                    acknowledged=False,
                    reconciled=False,
                    effects=accepted_effects,
                    error=str(exc),
                )
                self._attempts[attempt_seq] = {
                    "port_epoch": active_epoch,
                    "receipt": uncertain_receipt,
                    "committed_meta": committed_meta,
                    "uncertain": True,
                }
                return uncertain_receipt

            status = "domain_rejected" if rejected_index is not None else "committed"
            ok = 0 if rejected_index is not None else 1
            receipt = _build_receipt(
                ok=ok,
                status=status,
                attempt_seq=attempt_seq,
                port_epoch=active_epoch,
                accepted_count=len(accepted_effects),
                rejected_index=rejected_index,
                acknowledged=True,
                reconciled=False,
                effects=accepted_effects,
                error=rejection_error,
            )
            self._attempts[attempt_seq] = {
                "port_epoch": active_epoch,
                "receipt": receipt,
                "committed_meta": committed_meta,
                "uncertain": False,
            }
            return receipt

    # @anchor:wa.act.jq3_t2_broker_client.reconcile
    def reconcile(self, attempt_seq: int, *, port_epoch: int) -> BrokerReceipt:
        """Reconcile an attempt at `port_epoch` without minting a new sequence."""
        with self._lock:
            active_epoch = self._check_port_epoch(port_epoch)
            if not _is_int(attempt_seq) or attempt_seq < 1:
                raise BrokerClientError("attempt_seq must be a positive integer")
            if (
                self._uncertain_attempt_seq is not None
                and attempt_seq != self._uncertain_attempt_seq
            ):
                raise BrokerClientError(
                    "must reconcile pending uncertain attempt %d before %d"
                    % (self._uncertain_attempt_seq, attempt_seq)
                )

            broker_record = self.broker.get_committed_attempt(active_epoch, attempt_seq)
            local_entry = self._attempts.get(attempt_seq)
            if broker_record is None and local_entry is None:
                raise BrokerClientError(
                    "unknown attempt_seq %d for port_epoch %d" % (attempt_seq, active_epoch)
                )
            meta = broker_record if broker_record is not None else local_entry["committed_meta"]
            if meta["port_epoch"] != active_epoch:
                raise StalePortEpochError(
                    "INV-E2-4: attempt %d belongs to port_epoch %d, not %d"
                    % (attempt_seq, meta["port_epoch"], active_epoch)
                )

            if self._uncertain_attempt_seq == attempt_seq:
                self._uncertain_attempt_seq = None

            rejected_index = meta["rejected_index"]
            ok = 0 if rejected_index is not None else 1
            status = "domain_rejected" if rejected_index is not None else "committed"
            receipt = _build_receipt(
                ok=ok,
                status=status,
                attempt_seq=attempt_seq,
                port_epoch=active_epoch,
                accepted_count=meta["accepted_count"],
                rejected_index=rejected_index,
                acknowledged=True,
                reconciled=True,
                effects=meta["effects"],
                error=meta["error"],
            )
            self._attempts[attempt_seq] = {
                "port_epoch": active_epoch,
                "receipt": receipt,
                "committed_meta": meta,
                "uncertain": False,
            }
            return receipt


_DEFAULT_CLIENT: T2BrokerClient | None = None


def bind_default_client(client: T2BrokerClient | None) -> T2BrokerClient | None:
    """Bind or clear a module-level T2BrokerClient for module-function calls."""
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client
    return _DEFAULT_CLIENT


def submit_batch(
    records: Any,
    *,
    port_epoch: int,
    producer_done: Any,
    client: T2BrokerClient | None = None,
) -> BrokerReceipt:
    """Module-level entry point delegating to an explicit or bound T2BrokerClient."""
    target = _DEFAULT_CLIENT if client is None else client
    if target is None:
        raise PrerequisiteAbsent("T2BrokerClient prerequisite AUSENTE")
    return target.submit_batch(records, port_epoch=port_epoch, producer_done=producer_done)


def reconcile(
    attempt_seq: int,
    *,
    port_epoch: int,
    client: T2BrokerClient | None = None,
) -> BrokerReceipt:
    """Module-level entry point delegating to an explicit or bound T2BrokerClient."""
    target = _DEFAULT_CLIENT if client is None else client
    if target is None:
        raise PrerequisiteAbsent("T2BrokerClient prerequisite AUSENTE")
    return target.reconcile(attempt_seq, port_epoch=port_epoch)
