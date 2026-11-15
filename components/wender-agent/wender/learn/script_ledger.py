# @module {"id":"wa.learn.script_ledger","layer":2,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s3-remember"}
"""Owner-RAM evidence. Injected host checks, never decoded files, grant authority.

Origin: scripts_mesh/build/spec/s3_w.md. No actor, daemon or persistence.
The caller must serialize the input fence with admission; a ledger grant alone
is not permission to send. Transport custody must be supplied by the host.
"""
from __future__ import annotations

from dataclasses import dataclass as _dataclass, fields as _fields, is_dataclass as _is_dataclass
from enum import Enum as _Enum
import hashlib as _hashlib
import json as _json
import os as _os
import stat as _stat
import sys as _sys
from threading import RLock as _RLock
from typing import get_type_hints as _get_type_hints
from wender.scripts import types as _t


def _need(ok, reason=_t.Reason.SCHEMA):
    if not ok:
        raise _t.ContractError(reason)


def _positive(value):
    _need(type(value) is int and value > 0, _t.Reason.BOUNDS)


def _checked(check):
    _need(type(check) is _t.AdmissionCheck and check.result is _t.Truth.TRUE,
          check.reason if type(check) is _t.AdmissionCheck and check.reason else _t.Reason.MISSING)


def _host_check(callback, *args):
    failed, reason = False, _t.Reason.UNKNOWN
    try:
        result = callback(*args)
    except _t.ContractError as exc:
        failed, reason = True, exc.reason
    except Exception:
        failed = True
    _need(not failed, reason)
    _checked(result)


def _size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = _sys.getsizeof(value)
    if _is_dataclass(value):
        size += sum(_size(getattr(value, f.name), seen) for f in _fields(value))
    elif type(value) in (tuple, list, set, frozenset):
        size += sum(_size(item, seen) for item in value)
    elif type(value) is dict:
        size += sum(_size(k, seen) + _size(v, seen) for k, v in value.items())
    return size


def _wire(value):
    if isinstance(value, _Enum):
        return value.value
    if _is_dataclass(value):
        return {f.name: _wire(getattr(value, f.name)) for f in _fields(value)}
    if type(value) is tuple:
        return [_wire(v) for v in value]
    return value


def _canonical(value):
    failed = False
    try:
        result = _json.dumps(_wire(value), sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        failed = True
    _need(not failed, _t.Reason.TYPE)
    return result


# @anchor:wa.learn.script_ledger.LedgerPolicy
@_dataclass(frozen=True, slots=True)
class LedgerPolicy(_t._Record):
    """All limits supplied by trusted configuration, with architecture ceilings."""
    ledger_bytes: int
    queue_slots: int
    record_bytes: int
    ingress_bytes: int
    ingress_peak_bytes: int
    depth: int
    members: int
    sequence: int
    string_bytes: int
    batch: int
    deadline_ns: int

    def _validate(self):
        for f in _fields(self):
            _positive(getattr(self, f.name))
        _need(self.ledger_bytes <= 8388608 and self.batch <= 16, _t.Reason.BOUNDS)
        # Conservative encoded/decoded/member-hook/canonical/scratch envelope
        # [TARGET]; reservation remains supplied by the trusted aggregate owner.
        _need(self.ingress_peak_bytes >= self.ingress_bytes * 128, _t.Reason.CAPACITY)


# @anchor:wa.learn.script_ledger.RunEvent
@_dataclass(frozen=True, slots=True, repr=False)
class RunEvent(_t._Record):
    schema: str
    producer: _t.PrivateRef
    run_id: str
    episode_id: str
    action_id: int
    event_id: int
    event_index: int
    artifact: _t.ArtifactIdentity
    scope: _t.ScopeIdentity
    source: _t.EvidenceSource
    state: _t.EventState
    disposition: _t.InputDisposition
    verification: _t.Truth
    observations: tuple[_t.ObservationRef, ...]
    reason: _t.Reason | None
    usage: _t.MeasuredValue
    cost: _t.MeasuredValue
    arrival_ns: int
    recorded_ns: int
    exit_code: int | None
    score_q16: int | None

    def __post_init__(self):
        annotations = _get_type_hints(type(self))
        for field in _fields(self):
            value = getattr(self, field.name)
            _need(_t._matches(value, annotations[field.name]), _t.Reason.TYPE)
            _t._scalar_check(value, allow_signed=field.name in ('exit_code', 'score_q16'))
        self._validate()

    def _validate(self):
        _need(self.schema == 'wa.script.event/v1')
        _need(self.artifact.scope == self.scope, _t.Reason.STALE)
        _need(all(o.scope == self.scope for o in self.observations), _t.Reason.FOREIGN)
        _need(self.recorded_ns >= self.arrival_ns, _t.Reason.DEADLINE)
        if self.state is _t.EventState.VERIFIED:
            _need(self.verification is _t.Truth.TRUE and type(self.exit_code) is int
                  and type(self.score_q16) is int, _t.Reason.MISSING)
        else:
            _need(self.verification is not _t.Truth.TRUE, _t.Reason.CONFLICT)
        expected = {_t.EventState.NOT_SENT: _t.InputDisposition.NOT_SENT,
                    _t.EventState.ADMITTED: _t.InputDisposition.NOT_SENT,
                    _t.EventState.APPLIED: _t.InputDisposition.APPLIED,
                    _t.EventState.INPUT_UNCERTAIN: _t.InputDisposition.INPUT_UNCERTAIN,
                    _t.EventState.REFUSED: _t.InputDisposition.NOT_SENT}
        _need(self.state not in expected or self.disposition is expected[self.state], _t.Reason.CONFLICT)


# @anchor:wa.learn.script_ledger.EpisodeSeal
@_dataclass(frozen=True, slots=True, repr=False)
class EpisodeSeal(_t._Record):
    schema: str
    producer: _t.PrivateRef
    run_id: str
    episode_id: str
    artifact: _t.ArtifactIdentity
    scope: _t.ScopeIdentity
    expected_count: int
    chain_head: str
    complete: bool
    terminal_goal: _t.TerminalGoalRef
    authority: _t.AuthorityRevision
    # Exact order: policy, compiler, verifier, environment, suite.
    digests: tuple[_t.PlanDigest, ...]
    provenance: _t.PrivateRef
    support_identity: _t.PrivateRef

    def _validate(self):
        _need(self.schema == 'wa.script.seal/v1')
        _need(self.expected_count > 0 and self.complete, _t.Reason.MISSING)
        _need(len(self.chain_head) == 64 and all(c in '0123456789abcdef' for c in self.chain_head))
        _need(len(self.digests) == 5, _t.Reason.MISSING)
        _need(self.scope == self.artifact.scope == self.terminal_goal.scope
              and self.authority.scope == self.scope.scope_revision, _t.Reason.STALE)


# @anchor:wa.learn.script_ledger.LedgerOwner
class LedgerOwner:
    """Single mutation boundary; all authentication callbacks are trusted inputs.

    check_intent(intent), check_event(event), check_seal(seal) and
    check_recovery(scope, evidence) and check_reconciliation(scope, proof)
    return S0 AdmissionCheck. They must validate
    independent host evidence, not merely record shape or self-reported hashes.
    """
    def __init__(self, *, producer, run_id, episode_id, artifact, authority, policy,
                 clock, check_intent, check_event, check_seal, check_recovery, check_reconciliation):
        _need(type(producer) is _t.PrivateRef and type(artifact) is _t.ArtifactIdentity, _t.Reason.TYPE)
        _need(type(authority) is _t.AuthorityRevision and type(policy) is LedgerPolicy, _t.Reason.MISSING)
        _need(type(run_id) is str and bool(run_id) and type(episode_id) is str and bool(episode_id))
        _need(authority.scope == artifact.scope.scope_revision, _t.Reason.STALE)
        self._producer, self._run, self._episode = producer, run_id, episode_id
        self._artifact, self._authority, self._policy = artifact, authority, policy
        self._clock = clock
        self._check_intent, self._check_event = check_intent, check_event
        self._check_seal, self._check_recovery = check_seal, check_recovery
        self._check_reconciliation = check_reconciliation
        self._lock = _RLock()
        self._reservations, self._events, self._seals = {}, [], []
        self._dedup, self._minted = {}, {}
        self._charged = _size((producer, run_id, episode_id, artifact, authority, policy))
        _need(self._charged <= policy.ledger_bytes, _t.Reason.CAPACITY)
        self._serial, self._generation = 0, 0
        self._quarantined, self._closed, self._barrier = False, False, False
        self._deny = False
        self._head = '0' * 64
        self._roots = None  # Unknown reachability retains evidence.

    def _live(self):
        _need(not (self._barrier or self._quarantined or self._closed or self._deny), _t.Reason.RECONCILIATION)
        _need(self._clock() < self._policy.deadline_ns, _t.Reason.DEADLINE)

    def _reservation(self, token):
        record = self._reservations.get(id(token))
        _need(record is not None and record['token'] is token, _t.Reason.FOREIGN)
        return record

    # @anchor:wa.learn.script_ledger.LedgerOwner.reserve
    def reserve(self, intent, terminal_bytes, refusal_bytes, scope, authority):
        with self._lock:
            self._live()
            _need(type(intent) is _t.StepIntent, _t.Reason.TYPE)
            _positive(terminal_bytes)
            _positive(refusal_bytes)
            _need(intent.artifact == self._artifact and intent.scope == scope == self._artifact.scope
                  and intent.authority == authority == self._authority, _t.Reason.STALE)
            _host_check(self._check_intent, intent)
            _need(not self._seals and len(self._reservations) < self._policy.queue_slots, _t.Reason.CAPACITY)
            _need(not any(r['step'] == intent.step_id for r in self._reservations.values()), _t.Reason.CONFLICT)
            # Reserve overlapping encoded/decoded/canonical/scratch plus retained
            # owner graphs before minting any action identity.
            charge = 4 * (self._policy.record_bytes + terminal_bytes + refusal_bytes) + _size(intent)
            _need(charge + self._charged <= self._policy.ledger_bytes, _t.Reason.CAPACITY)
            self._serial += 1
            token = _t.RegistryRef('ledger', self._producer.incarnation, self._serial, self._producer)
            self._reservations[id(token)] = dict(token=token, action=self._serial, step=intent.step_id,
                state=None, disposition=_t.InputDisposition.NOT_SENT, charge=charge, consumed=False,
                arrival=self._clock())
            self._charged += charge
            self._generation += 1
            return token

    # @anchor:wa.learn.script_ledger.LedgerOwner.event
    def event(self, reservation, *, state, disposition, verification, observations,
              reason, usage, cost, exit_code, score_q16):
        """Mint a projection for immediate append; never attests execution."""
        with self._lock:
            record = self._reservation(reservation)
            possible = disposition in (_t.InputDisposition.APPLIED, _t.InputDisposition.INPUT_UNCERTAIN)
            if possible:
                record.update(consumed=True, disposition=disposition)
            try:
                self._live()
                _need(not record.get('pending') and not record.get('released'), _t.Reason.CONFLICT)
                self._serial += 1
                event = RunEvent('wa.script.event/v1', self._producer, self._run, self._episode,
                    record['action'], self._serial, len(self._events), self._artifact, self._artifact.scope,
                    _t.EvidenceSource.HOST, state, disposition, verification, observations, reason,
                    usage, cost, record['arrival'], self._clock(), exit_code, score_q16)
                _need(_size(event) + len(_canonical(event)) <= self._policy.record_bytes, _t.Reason.CAPACITY)
                self._minted[id(event)] = event
                record['pending'] = event
                self._generation += 1
                return event
            except _t.ContractError:
                if possible:
                    self._barrier = True
                    raise _t.ContractError(_t.Reason.RECONCILIATION) from None
                raise

    # @anchor:wa.learn.script_ledger.LedgerOwner.append
    def append(self, reservation, event):
        with self._lock:
            record = self._reservation(reservation)
            _need(type(event) is RunEvent, _t.Reason.TYPE)
            key = (event.producer.incarnation, event.run_id, event.event_id)
            _need(_size(event) <= self._policy.record_bytes, _t.Reason.CAPACITY)
            canonical = _canonical(event)
            if key in self._dedup:
                old, ack = self._dedup[key]
                if canonical != old:
                    self._quarantined = self._barrier = True
                    raise _t.ContractError(_t.Reason.CONFLICT)
                _need(not self._deny, _t.Reason.STALE)
                self._live()
                return ack
            possible = event.disposition is not _t.InputDisposition.NOT_SENT
            # Mint membership precedes trusting any submitted disposition.
            _need(self._minted.get(id(event)) is event and record.get('pending') is event, _t.Reason.FOREIGN)
            if possible:
                record['consumed'] = True
                record['disposition'] = event.disposition
            try:
                self._live()
                _need(event.event_index == len(self._events), _t.Reason.CONFLICT)
                _need(event.action_id == record['action'] and event.artifact == self._artifact
                      and event.scope == self._artifact.scope and event.producer == self._producer
                      and event.run_id == self._run and event.episode_id == self._episode, _t.Reason.FOREIGN)
                prior = record['state']
                transitions = {
                    None: (_t.EventState.NOT_SENT, _t.EventState.ADMITTED, _t.EventState.REFUSED,
                           _t.EventState.CANCELLED, _t.EventState.SUSPENDED),
                    _t.EventState.NOT_SENT: (_t.EventState.ADMITTED, _t.EventState.REFUSED, _t.EventState.CANCELLED),
                    _t.EventState.ADMITTED: (_t.EventState.APPLIED, _t.EventState.INPUT_UNCERTAIN,
                                           _t.EventState.REFUSED, _t.EventState.SUSPENDED, _t.EventState.CANCELLED),
                    _t.EventState.APPLIED: (_t.EventState.VERIFIED, _t.EventState.SUSPENDED),
                }
                _need(event.state in transitions.get(prior, ()), _t.Reason.CONFLICT)
                if prior is _t.EventState.APPLIED:
                    _need(event.disposition is _t.InputDisposition.APPLIED, _t.Reason.CONFLICT)
                expected_generation = self._generation
                _host_check(self._check_event, event)
                _need(self._generation == expected_generation, _t.Reason.STALE)
                self._live()  # Callback latency cannot produce a late success.
                # A reservation bounds the whole action, including accumulated
                # events, acknowledgements, canonical bytes and hash scratch.
                used = record.get('used', 0) + 4 * (_size(event) + len(canonical) + 256)
                _need(used <= record['charge'], _t.Reason.CAPACITY)
                head = _hashlib.sha256(bytes.fromhex(self._head) + canonical).hexdigest()
                ack = _t.AdmissionCheck(_t.Truth.TRUE, None)
                self._events.append(event)
                self._dedup[key] = (canonical, ack)
                record['used'], record['state'] = used, event.state
                record['pending'] = None
                self._head = head
                self._generation += 1
                return ack
            except _t.ContractError:
                if possible or record['consumed']:
                    self._barrier = True
                    return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.RECONCILIATION)
                raise
            except Exception:
                self._barrier = True
                return _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.RECONCILIATION)

    # @anchor:wa.learn.script_ledger.LedgerOwner.release_unused
    def release_unused(self, reservation):
        """Refund only scratch/unspent capacity, retaining consumed identities."""
        with self._lock:
            record = self._reservation(reservation)
            _need(not record.get('pending'), _t.Reason.CONFLICT)
            _need(record['state'] in (None, _t.EventState.VERIFIED, _t.EventState.REFUSED,
                  _t.EventState.CANCELLED, _t.EventState.SUSPENDED, _t.EventState.INPUT_UNCERTAIN),
                  _t.Reason.CONFLICT)
            retained = max(record.get('used', 0), _size(record))
            refund = max(0, record['charge'] - retained)
            record['charge'] -= refund
            record['released'] = True
            self._charged -= refund
            return refund

    # @anchor:wa.learn.script_ledger.LedgerOwner.seal_episode
    def seal_episode(self, seal):
        with self._lock:
            self._live()
            _need(type(seal) is EpisodeSeal, _t.Reason.TYPE)
            _need(not self._seals and bool(self._events), _t.Reason.MISSING)
            _need(seal.producer == self._producer and seal.artifact == self._artifact
                  and seal.scope == self._artifact.scope and seal.run_id == self._run
                  and seal.episode_id == self._episode and seal.authority == self._authority, _t.Reason.FOREIGN)
            _need(seal.expected_count == len(self._events) and seal.chain_head == self._head, _t.Reason.CONFLICT)
            _need(all(event.event_index == i for i, event in enumerate(self._events)), _t.Reason.CONFLICT)
            _need(all(record['state'] is _t.EventState.VERIFIED and not record.get('pending')
                      for record in self._reservations.values()), _t.Reason.MISSING)
            _need(self._events[-1].state is _t.EventState.VERIFIED
                  and self._events[-1].exit_code == 0, _t.Reason.MISSING)
            expected_generation = self._generation
            _host_check(self._check_seal, seal)
            _need(self._generation == expected_generation, _t.Reason.STALE)
            self._live()
            charge = 4 * (_size(seal) + len(_canonical(seal)))
            _need(self._charged + charge <= self._policy.ledger_bytes, _t.Reason.CAPACITY)
            self._charged += charge
            self._seals.append(seal)
            self._generation += 1
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_ledger.LedgerOwner.acknowledge_seal
    def acknowledge_seal(self, seal, scope):
        with self._lock:
            _need(scope == self._artifact.scope and any(s is seal for s in self._seals), _t.Reason.FOREIGN)
            _need(not self._barrier and not self._quarantined and not self._deny, _t.Reason.RECONCILIATION)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_ledger.LedgerOwner.snapshot
    def snapshot(self):
        """Host-private immutable evidence, not an export receipt."""
        with self._lock:
            return (tuple(self._events), self._head, self._charged, self._generation)

    # @anchor:wa.learn.script_ledger.LedgerOwner.recover
    def recover(self, *, scope, evidence, reconciliation):
        """Always close first, including on absent/renamed recovery evidence."""
        with self._lock:
            self._barrier = True
            self._generation += 1
            _need(type(scope) is _t.ScopeIdentity and scope == self._artifact.scope, _t.Reason.RECONCILIATION)
            _host_check(self._check_recovery, scope, evidence)
            _need(evidence is not None, _t.Reason.MISSING)
            # Reconciliation proves fresh observation/new grant and disposition
            # of every interrupted surface independently of evidence eligibility.
            _host_check(self._check_reconciliation, scope, reconciliation)
            _need(reconciliation is not None, _t.Reason.RECONCILIATION)
            self._barrier = False
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_ledger.LedgerOwner.retire
    def retire(self, *, generation, proof, check_drain):
        """Host proof includes permanent closure, callbacks, roots and lineage.

        Unknown reachability must never be certified by check_drain. Its check
        runs outside the lock; publication rechecks root generation.
        """
        _host_check(check_drain, self._producer, proof)
        with self._lock:
            _need(type(generation) is int and generation == self._generation, _t.Reason.STALE)
            _need(not self._barrier, _t.Reason.RECONCILIATION)
            _need(not any(r.get('pending') for r in self._reservations.values()), _t.Reason.CONFLICT)
            # Retain a deny watermark even after dropping all dedup/event bodies.
            self._closed = self._deny = True
            self._events.clear()
            self._dedup.clear()
            self._minted.clear()
            self._reservations.clear()
            self._seals.clear()
            self._charged = _size(self._producer) + _size(self._artifact) + _size(self._authority)
            self._generation += 1
            return _t.AdmissionCheck(_t.Truth.TRUE, None)


# @anchor:wa.learn.script_ledger.reserve
def reserve(owner, *args, **kwargs):
    return owner.reserve(*args, **kwargs)


# @anchor:wa.learn.script_ledger.append
def append(owner, reservation, event):
    return owner.append(reservation, event)


# @anchor:wa.learn.script_ledger.seal_episode
def seal_episode(owner, seal):
    return owner.seal_episode(seal)


# @anchor:wa.learn.script_ledger.recover
def recover(owner, *, scope, evidence, reconciliation):
    return owner.recover(scope=scope, evidence=evidence, reconciliation=reconciliation)


def _decode(raw, policy):
    _need(type(raw) is bytes and len(raw) <= policy.ingress_bytes, _t.Reason.BOUNDS)
    # Bound lexical nesting BEFORE the recursive JSON decoder runs.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            _need(depth <= policy.depth, _t.Reason.BOUNDS)
        elif byte in (93, 125):
            depth -= 1
    def pairs(items):
        _need(len(items) <= policy.members, _t.Reason.BOUNDS)
        result = {}
        for key, value in items:
            _need(key not in result, _t.Reason.CONFLICT)
            result[key] = value
        return result
    def invalid(_):
        raise _t.ContractError(_t.Reason.TYPE)
    failed = False
    try:
        value = _json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=pairs,
                            parse_float=invalid, parse_constant=invalid)
    except _t.ContractError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        failed = True
    _need(not failed)
    def shape(v, level=0):
        _need(level <= policy.depth, _t.Reason.BOUNDS)
        if type(v) is dict:
            _need(len(v) <= policy.members, _t.Reason.BOUNDS)
            for k, child in v.items():
                shape(k, level + 1)
                shape(child, level + 1)
        elif type(v) is list:
            _need(len(v) <= policy.sequence, _t.Reason.BOUNDS)
            for child in v:
                shape(child, level + 1)
        elif type(v) is str:
            valid = True
            try:
                length = len(v.encode('utf-8', 'strict'))
            except UnicodeError:
                valid = False
            _need(valid, _t.Reason.TYPE)
            _need(length <= policy.string_bytes, _t.Reason.BOUNDS)
    shape(value)
    _need(type(value) is dict, _t.Reason.TYPE)
    return value


def _read_fd(fd, policy):
    failed = False
    try:
        result = _read_regular(fd, policy)
    except OSError:
        failed = True
    _need(not failed, _t.Reason.FOREIGN)
    return result


def _read_regular(fd, policy):
    info = _os.fstat(fd)
    _need(_stat.S_ISREG(info.st_mode) and info.st_size <= policy.ingress_bytes, _t.Reason.BOUNDS)
    parts, count = [], 0
    while True:
        part = _os.read(fd, min(65536, policy.ingress_bytes - count + 1))
        if not part:
            break
        count += len(part)
        _need(count <= policy.ingress_bytes, _t.Reason.BOUNDS)
        parts.append(part)
    return b''.join(parts)


# @anchor:wa.learn.script_ledger.consolidate
def consolidate(owner, *, root_fd, names, custody_check, resolve_record):
    """Explicit descriptor and finalized names; never enumerate a guessed root.

    custody_check authenticates RAM placement, exclusive finalization and the
    expected (device, inode, uid) of each name. resolve_record maps a closed
    envelope to already retained (reservation,event); it cannot mint authority.
    """
    policy = owner._policy
    _need(type(names) is tuple and len(names) <= policy.batch, _t.Reason.BOUNDS)
    _need(all(type(name) is str and len(name) <= policy.string_bytes for name in names), _t.Reason.TYPE)
    outcomes = []
    with owner._lock:
        owner._live()
        _need(owner._charged + policy.ingress_peak_bytes <= policy.ledger_bytes, _t.Reason.CAPACITY)
        owner._charged += policy.ingress_peak_bytes
        try:
            root = _os.fstat(root_fd)
            _need(_stat.S_ISDIR(root.st_mode) and root.st_uid == _os.getuid()
                  and root.st_mode & 0o077 == 0, _t.Reason.FOREIGN)
            for name in sorted(names):
                fd = None
                try:
                    _need(type(name) is str and name not in ('.', '..') and '/' not in name)
                    expected = custody_check(root_fd, name)
                    before = _os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    _need(_stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                          and before.st_uid == root.st_uid, _t.Reason.FOREIGN)
                    fd = _os.open(name, _os.O_RDONLY | _os.O_NOFOLLOW | _os.O_NONBLOCK, dir_fd=root_fd)
                    after = _os.fstat(fd)
                    identity = (after.st_dev, after.st_ino, after.st_uid)
                    _need(identity == expected == (before.st_dev, before.st_ino, before.st_uid)
                          and _stat.S_ISREG(after.st_mode) and after.st_nlink == 1, _t.Reason.FOREIGN)
                    envelope = _decode(_read_fd(fd, policy), policy)
                    _need(set(envelope) == {'schema', 'event_id'} and envelope['schema'] == 'wa.script.spool/v1')
                    _need(type(envelope['event_id']) is int and envelope['event_id'] > 0, _t.Reason.TYPE)
                    token, event = resolve_record(envelope)
                    _need(type(event) is RunEvent and event.event_id == envelope['event_id'], _t.Reason.FOREIGN)
                    outcomes.append(owner.append(token, event))
                except _t.ContractError as exc:
                    outcomes.append(_t.AdmissionCheck(_t.Truth.FALSE, exc.reason))
                except OSError:
                    outcomes.append(_t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.FOREIGN))
                finally:
                    if fd is not None:
                        _os.close(fd)
        finally:
            owner._charged -= policy.ingress_peak_bytes
    return tuple(outcomes)


# @anchor:wa.learn.script_ledger.project_receipt
def project_receipt(owner, event, *, destination, purpose, approve, tokenizer, failure=False):
    """Fresh static vocabulary only; destination check never exports private IDs."""
    with owner._lock:
        _need(any(item is event for item in owner._events), _t.Reason.FOREIGN)
        _need(type(destination) is _t.ExportDestination and type(purpose) is _t.Purpose, _t.Reason.TYPE)
        _host_check(approve, event, destination, purpose)
        _need(callable(tokenizer), _t.Reason.MISSING)
        result = {'schema': 'cu.receipt/v1', 'event_ref': event.event_id,
                  'disposition': event.disposition.value, 'state': event.state.value,
                  'reason': event.reason.value if event.reason else None}
        encoded = _json.dumps(result, separators=(',', ':'), sort_keys=True).encode('ascii')
        failed = False
        try:
            count = tokenizer(encoded.decode('ascii'))
        except Exception:
            failed = True
        _need(not failed, _t.Reason.UNKNOWN)
        _need(type(count) is int and 0 <= count <= (40 if failure else 120), _t.Reason.BOUNDS)
        _need(len(encoded) <= (512 if failure else 4096), _t.Reason.BOUNDS)
        return encoded
