# @module {"id":"wa.learn.script_store","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.scripts.compiler"],"book":"HB-WA-s4-learn"}
"""Process-local script ownership. Host fence precedes this owner's lock.

Equal records never substitute for capabilities. No disk persistence or automatic
rehydration is provided. Cancellation proofs, not timeouts, release ownership.
"""
import dataclasses as _dc
import enum as _enum
import hashlib as _hashlib
import json as _json
import threading as _threading
import weakref as _weakref
from wender.scripts import types as _t, header as _h, compiler as _c

_need, _check, _closed = _h._need, _h._check, _h._closed


def _scope_tuple(scope):
    return (scope.principal, scope.tenant, scope.browser_profile_incarnation,
            scope.top_level_origin, scope.target_frame_origin, scope.frame_ancestry,
            scope.scope_revision)


def _canonical_json_bytes(value):
    def project(item):
        if isinstance(item, _enum.Enum):
            return item.value
        if isinstance(item, _t._Record):
            return {f.name: project(getattr(item, f.name)) for f in _dc.fields(item)
                    if f.name not in _h._OMIT.get(type(item), ())}
        if type(item) in (tuple, list):
            return [project(v) for v in item]
        if type(item) is dict:
            return {k: project(v) for k, v in item.items()}
        _need(item is None or type(item) in (str, int, bool), _t.Reason.TYPE)
        return item
    return _json.dumps(project(value), sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')


def _domain_digest(cls, domain, payload):
    return cls('sha256', _hashlib.sha256(domain.encode('ascii') + b'\0' + payload).hexdigest())


def _decoded_footprint_bytes(root, budget):
    stack, charge = [(root, 0)], 0
    while stack:
        value, depth = stack.pop()
        charge += 128 + (12 * len(value) if type(value) is str else
                         len(value) if type(value) is bytes else 0)
        _need(charge <= budget and depth <= 64, _t.Reason.CAPACITY)
        children = (tuple(getattr(value, f.name) for f in _dc.fields(value))
                    if isinstance(value, _t._Record) else
                    value if type(value) is tuple else ())
        _need(len(children) + len(stack) <= budget // 128, _t.Reason.CAPACITY)
        stack.extend((child, depth + 1) for child in children)
    return charge


# @anchor:wa.learn.script_store.ArtifactKey
@_dc.dataclass(frozen=True, slots=True)
class ArtifactKey(_t._Record):
    scope: _t.ScopeIdentity
    site_contract: str
    signature_schema: str
    structural_screen_digest: _t.PlanDigest
    goal_contract: str
    logical_id: str
    revision: int
    plan_digest: _t.PlanDigest
    metadata_digest: _t.MetadataDigest
    artifact_digest: _t.ArtifactDigest

    def _validate(self):
        _need(self.revision >= 1, _t.Reason.BOUNDS)
        _need(self.scope.attestation is not None and self.scope.attestation_reason is None, _t.Reason.FOREIGN)

    # @anchor:wa.learn.script_store.ArtifactKey.slot_key
    @_closed
    def slot_key(self):
        return (_scope_tuple(self.scope), self.site_contract, self.signature_schema,
                self.structural_screen_digest.full_digest, self.goal_contract, self.logical_id)

    # @anchor:wa.learn.script_store.ArtifactKey.canonical_sort_key
    @_closed
    def canonical_sort_key(self):
        return _canonical_json_bytes((_scope_tuple(self.scope), self.logical_id,
                                      self.revision, self.artifact_digest.full_digest))


# @anchor:wa.learn.script_store.ByteAccounting
@_dc.dataclass(frozen=True, slots=True)
class ByteAccounting(_t._Record):
    source_bytes: int
    decoded_bytes: int
    canonical_bytes: int
    metadata_bytes: int
    support_bytes: int
    shared_dedup_bytes: int = 0

    # @anchor:wa.learn.script_store.ByteAccounting.total_charged_bytes
    @_closed
    def total_charged_bytes(self):
        return (self.source_bytes + self.decoded_bytes + self.canonical_bytes
                + self.metadata_bytes + self.support_bytes)


# @anchor:wa.learn.script_store.ArtifactCell
@_dc.dataclass(frozen=True, slots=True)
class ArtifactCell(_t._Record):
    key: ArtifactKey
    identity: _t.ArtifactIdentity
    lifecycle: _t.Lifecycle
    lifecycle_revision: int
    authority_revision: _t.AuthorityRevision
    resident: bool
    plan: _t.ValidatedPlan | None = _dc.field(repr=False)
    source_bytes: bytes | None = _dc.field(repr=False)
    canonical_plan_bytes: bytes | None = _dc.field(repr=False)
    accounting: ByteAccounting
    holder_count: int
    active_step_permits: int
    pending_admissions: int
    last_use_ns: int
    last_unpin_ns: int
    fts_terms: tuple[str, ...] = ()
    uso_text: str | None = _dc.field(default=None, repr=False)
    cli_text: str | None = _dc.field(default=None, repr=False)
    predecessor_digest: _t.ArtifactDigest | None = None
    negative_lineage: tuple[_t.ArtifactDigest, ...] = ()
    quarantine_environments: tuple[str, ...] = ()
    denied_environments: tuple[str, ...] = ()
    cell_token: _t.RegistryRef = _dc.field(default=None, repr=False)

    def _validate(self):
        _need(self.lifecycle_revision >= 1, _t.Reason.BOUNDS)
        _need(self.cell_token is not None, _t.Reason.MISSING)
        _need(self.key.scope == self.identity.scope and
              self.authority_revision.scope == self.key.scope.scope_revision, _t.Reason.STALE)
        if self.resident:
            _need(self.plan is not None and bool(self.canonical_plan_bytes)
                  and self.accounting.total_charged_bytes() > 0, _t.Reason.MISSING)
        else:
            _need(self.plan is None and self.source_bytes is None and self.canonical_plan_bytes is None
                  and self.holder_count == self.active_step_permits == 0
                  and self.accounting.total_charged_bytes() == 0, _t.Reason.CONFLICT)


# @anchor:wa.learn.script_store.ScriptLease
@_dc.dataclass(frozen=True, slots=True, weakref_slot=True)
class ScriptLease(_t._Record):
    artifact: _t.ArtifactIdentity
    key: ArtifactKey
    scope: _t.ScopeIdentity
    episode_id: str
    process_incarnation: str
    consumer_kind: str
    lifecycle_revision: int
    authority_revision: _t.AuthorityRevision
    issued_ns: int
    absolute_deadline_ns: int
    holder_contract: _t.HolderContract
    retained_owner: _t.PrivateRef = _dc.field(repr=False)
    holder_token: _t.RegistryRef = _dc.field(repr=False)

    def _validate(self):
        _need(self.consumer_kind in ('episode', 'replay', 'context', 'dispatch'), _t.Reason.SCHEMA)
        _need(self.lifecycle_revision >= 1 and self.issued_ns < self.absolute_deadline_ns, _t.Reason.BOUNDS)
        _need(self.scope == self.key.scope == self.artifact.scope == self.holder_contract.scope
              and self.authority_revision.scope == self.scope.scope_revision, _t.Reason.STALE)


# @anchor:wa.learn.script_store.CandidateRef
@_dc.dataclass(frozen=True, slots=True, weakref_slot=True)
class CandidateRef(_t._Record):
    artifact: _t.ArtifactIdentity
    key: ArtifactKey
    scope: _t.ScopeIdentity
    lifecycle: _t.Lifecycle
    lifecycle_revision: int
    authority_revision: _t.AuthorityRevision
    snapshot_generation: int
    deny_sequence: int
    owner_ref: _t.RegistryRef = _dc.field(repr=False)

    def _validate(self):
        _need(self.lifecycle_revision >= 1 and self.snapshot_generation >= 1, _t.Reason.BOUNDS)
        _need(self.scope == self.key.scope == self.artifact.scope and
              self.authority_revision.scope == self.scope.scope_revision, _t.Reason.STALE)


class _CellState:
    def __init__(self, cell, grant, holder):
        self.cell, self.grant, self.holder = cell, grant, holder
        self.metadata = (cell.plan.definition.header.capabilities, cell.plan.definition.header.arguments)
        self.manifest_allocation = None
        self.holders, self.released, self.permits = {}, set(), {}
        self.open_by_lease = {}


# @anchor:wa.learn.script_store.ScriptStore
class ScriptStore:
    # @anchor:wa.learn.script_store.ScriptStore.__init__
    @_closed
    def __init__(self, *, owner, incarnation, generation, compiler, validation_registry,
                 store_policy, resolver_policy, resource_policy, slot_policy, clock,
                 attempts, reservations, cancellation, endpoint, boundary, holder_factory,
                 telemetry, scope_check, screen_check, slot_check=None, vocabulary_check=None,
                 export_receipt_port=None, accounting_revision=1, max_evidence_records=256):
        for value, cls in ((owner, _t.PrivateRef), (compiler, _t.PrivateRef),
                           (endpoint, _t.RegistryRef), (boundary, _t.RegistryRef),
                           (store_policy, _t.StorePolicy), (resolver_policy, _t.ResolverPolicy),
                           (resource_policy, _t.ResourcePolicy), (slot_policy, _t.SlotPolicy)):
            _need(type(value) is cls, _t.Reason.TYPE)
        _need(incarnation == owner.incarnation, _t.Reason.FOREIGN)
        _need(type(generation) is int and generation > 0 and type(accounting_revision) is int
              and accounting_revision >= 0 and type(max_evidence_records) is int
              and max_evidence_records > 0, _t.Reason.BOUNDS)
        for port in (validation_registry, clock, attempts, reservations, cancellation,
                     holder_factory, telemetry, scope_check, screen_check):
            _need(port is not None, _t.Reason.MISSING)
        self._owner, self._incarnation, self._generation = owner, incarnation, generation
        self._compiler, self._validation_registry = compiler, validation_registry
        self._store_policy, self._resolver_policy = store_policy, resolver_policy
        self._resource_policy, self._slot_policy = resource_policy, slot_policy
        self._clock, self._attempts, self._reservations = clock, attempts, reservations
        self._cancellation, self._endpoint, self._boundary = cancellation, endpoint, boundary
        self._holder_factory, self._telemetry = holder_factory, telemetry
        self._scope_check, self._screen_check = scope_check, screen_check
        self._slot_check, self._vocabulary_check = slot_check, vocabulary_check
        self._export_receipt_port = export_receipt_port
        self._accounting_revision, self._max_evidence_records = accounting_revision, max_evidence_records
        self._lock = _threading.RLock()
        self._serial = self._lifecycle_counter = self._deny_sequence = self._deny_watermark = 0
        self._service_stopped, self._stop_reason = False, None
        self._handles, self._cells, self._authority = {}, {}, {}
        self._released_holders = set()
        self._retired_refs = []
        self._default_by_slot, self._tombstones, self._evidence_records = {}, {}, {}
        self._degraded_environments, self._blocked_consumers = {}, set()
        self._published_snapshots, self._snapshot_pins, self._snapshot_allocations = {}, {}, {}
        self._snapshot_pin_tokens, self._snapshot_bindings = {}, {}
        self._pending_allocations = []
        self._allocations = {}

    def _now(self):
        now = self._clock.now_ns()
        _need(type(now) is int and now >= 0, _t.Reason.MISSING)
        return now

    def _scope(self, scope):
        _need(type(scope) is _t.ScopeIdentity, _t.Reason.TYPE)
        _need(scope.attestation is not None and scope.attestation_reason is None, _t.Reason.FOREIGN)
        _check(self._scope_check(scope))
        return _scope_tuple(scope)

    def _live(self):
        _need(not self._service_stopped, _t.Reason.CANCELLED)

    def _bump(self):
        self._lifecycle_counter += 1

    def _register_obj(self, obj):
        oid = id(obj)
        retained = obj
        if hasattr(type(obj), '__weakref__'):
            # The active holder/published snapshot is a strong owner root.
            # Registry authentication itself must not retain discarded readers.
            attached = getattr(obj, 'holder_token', getattr(obj, 'owner_ref', None))
            def gone(reference):
                with self._lock:
                    row = self._handles.get(oid)
                    if row is not None and row[0] is reference:
                        self._handles.pop(oid, None)
                        if attached is not None:
                            self._handles.pop(id(attached), None)
                        self._snapshot_bindings.pop(oid, None)
                        self._released_holders.discard(oid)
                        for state in self._cells.values():
                            state.released.discard(oid)
            retained = _weakref.ref(obj, gone)
        self._handles[oid] = (retained, self._incarnation, self._generation, False)
        return obj

    def _mint_ref(self, kind):
        self._serial += 1
        return self._register_obj(_t.RegistryRef(kind, self._incarnation, self._generation,
            _t.PrivateRef(self._owner.owner, self._incarnation, 'store:' + str(self._serial))))

    def _check_obj(self, obj, *, allow_revoked=False):
        row = self._handles.get(id(obj))
        retained = row[0]() if row is not None and isinstance(row[0], _weakref.ReferenceType) else row[0] if row is not None else None
        _need(row is not None and retained is obj and row[1] == self._incarnation
              and row[2] == self._generation, _t.Reason.FOREIGN)
        _need(allow_revoked or not row[3], _t.Reason.STALE)

    def _unregister(self, obj):
        self._check_obj(obj, allow_revoked=True)
        row = self._handles[id(obj)]
        self._handles[id(obj)] = row[:3] + (True,)
        if type(obj) is _t.RegistryRef and obj.kind in ('step_permit', 'snapshot_pin') and not row[3]:
            self._retired_refs.append(id(obj))
            while len(self._retired_refs) > self._max_evidence_records:
                self._handles.pop(self._retired_refs.pop(0), None)

    def _lookup(self, target, scope):
        sid = self._scope(scope)
        if type(target) in (ArtifactKey, CandidateRef):
            key = target if type(target) is ArtifactKey else target.key
            _need(key.scope == scope, _t.Reason.FOREIGN)
            digest = key.artifact_digest.full_digest
        else:
            _need(type(target) is _t.ArtifactDigest, _t.Reason.TYPE)
            digest = target.full_digest
        state = self._cells.get((sid, digest))
        _need(state is not None, _t.Reason.MISSING)
        if type(target) is ArtifactKey:
            _need(state.cell.key == target, _t.Reason.FOREIGN)
        return state

    def _view(self, state):
        return _dc.replace(state.cell, holder_count=len(state.holders),
                           active_step_permits=len(state.permits))

    def _change(self, state, **changes):
        state.cell = _dc.replace(state.cell, **changes)
        self._bump()

    def _reserve(self, scope, peak, operation, deadline, retained=()):
        used = self._telemetry()
        _need(type(used) is int and used >= 0, _t.Reason.MISSING)
        p = self._resource_policy
        _need(used + peak + p.emergency_reserve_bytes <= p.host_bytes
              and peak <= p.process_bytes and peak <= p.cancellation.consumer_byte_ceiling,
              _t.Reason.CAPACITY)
        now = self._now()
        _need(now < deadline, _t.Reason.DEADLINE)
        req = _t.ReservationRequest(self._owner, self._owner, scope, operation, peak,
                                   retained, (), self._accounting_revision, deadline)
        outcome = self._reservations.reserve(req, p)
        _need(type(outcome) is _t.ReservationOutcome, _t.Reason.TYPE)
        _need(outcome.grant is not None, outcome.reason or _t.Reason.CAPACITY)
        grant = outcome.grant
        _need(grant.request is req, _t.Reason.FOREIGN)
        # Keep a root even when a trusted factory fails; never invent a release proof.
        self._pending_allocations.append(grant)
        try:
            holder = self._holder_factory(grant, scope, p.cancellation, self._endpoint, self._boundary)
            _need(type(holder) is _t.HolderContract and holder.scope == scope
                  and holder.retained_bytes == grant.charged_bytes
                  and holder.absolute_deadline_ns == grant.absolute_deadline_ns
                  and holder.policy == p.cancellation and holder.cancellation_endpoint is self._endpoint
                  and holder.supervisor_boundary is self._boundary, _t.Reason.FOREIGN)
        except BaseException:
            self._service_stopped, self._stop_reason = True, 'unresolved_allocation'
            raise
        self._pending_allocations.remove(grant)
        self._allocations[id(grant)] = (grant, holder)
        return grant, holder

    def _drain(self, grant, holder, proof=None):
        if proof is None:
            proof = self._cancellation.request(holder)
        _need(type(proof) is _t.CancellationResult, _t.Reason.MISSING)
        _check(self._cancellation.verify(holder, proof))
        _need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined))
              and any(tok is grant.reservation for tok in proof.released_tokens), _t.Reason.FOREIGN)
        _check(self._reservations.release_unused(grant, grant.charged_bytes, proof))
        self._allocations.pop(id(grant), None)

    # @anchor:wa.learn.script_store.ScriptStore.store_policy
    @property
    def store_policy(self):
        return self._store_policy

    # @anchor:wa.learn.script_store.ScriptStore.resolver_policy
    @property
    def resolver_policy(self):
        return self._resolver_policy

    # @anchor:wa.learn.script_store.ScriptStore.slot_policy
    @property
    def slot_policy(self):
        return self._slot_policy

    # @anchor:wa.learn.script_store.ScriptStore.slot_check
    @property
    def slot_check(self):
        return self._slot_check

    # @anchor:wa.learn.script_store.ScriptStore.vocabulary_check
    @property
    def vocabulary_check(self):
        return self._vocabulary_check

    # @anchor:wa.learn.script_store.ScriptStore.deny_sequence
    @property
    def deny_sequence(self):
        return self._deny_sequence

    # @anchor:wa.learn.script_store.ScriptStore.store_lifecycle_revision
    @property
    def store_lifecycle_revision(self):
        return self._lifecycle_counter

    # @anchor:wa.learn.script_store.ScriptStore.service_stopped
    @property
    def service_stopped(self):
        return self._service_stopped

    # @anchor:wa.learn.script_store.ScriptStore.current_authority_revision
    @_closed
    def current_authority_revision(self, scope):
        with self._lock:
            sid = self._scope(scope)
            _need(sid in self._authority, _t.Reason.MISSING)
            return self._authority[sid]

    # @anchor:wa.learn.script_store.ScriptStore.mint_certificate
    @_closed
    def mint_certificate(self, kind, token):
        """Trusted host-only provisioning API; metadata cannot invoke this API."""
        with self._lock:
            _need(kind in ('promotion_certificate', 'shadow_certificate', 'recovery_certificate',
                           'certificate', 'evidence'), _t.Reason.SCHEMA)
            _need(type(token) is str and bool(token), _t.Reason.MISSING)
            return self._mint_ref(kind)

    def _certificate(self, certificate, kinds):
        self._check_obj(certificate)
        _need(certificate.kind in kinds, _t.Reason.FOREIGN)

    # @anchor:wa.learn.script_store.ScriptStore.admit_artifact
    @_closed
    def admit_artifact(self, plan, *, scope, authority_revision, source_bytes=b'', auxiliary=(),
                       support_bytes=0, decoded_bytes_override=None, initial_lifecycle=_t.Lifecycle.DRAFT,
                       predecessor_digest=None, negative_lineage=(), certificate=None,
                       session=None, admission=None, inputs=None, screen_check=None):
        self._live()
        sid = self._scope(scope)
        _need(type(plan) is _t.ValidatedPlan and type(authority_revision) is _t.AuthorityRevision,
              _t.Reason.TYPE)
        _need(authority_revision.scope == scope.scope_revision, _t.Reason.STALE)
        now = self._now()
        _check(self._attempts.charge(scope, self._owner, 'store-admit', self._resource_policy, now))
        _need(initial_lifecycle in (_t.Lifecycle.DRAFT, _t.Lifecycle.SHADOW, _t.Lifecycle.PROMOTED), _t.Reason.SCHEMA)
        if initial_lifecycle is not _t.Lifecycle.DRAFT:
            with self._lock:
                self._certificate(certificate, ('certificate', 'promotion_certificate') if
                                  initial_lifecycle is _t.Lifecycle.PROMOTED else ('certificate', 'shadow_certificate'))
        _need(type(source_bytes) is bytes and type(auxiliary) is tuple and type(support_bytes) is int
              and support_bytes >= 0, _t.Reason.TYPE)
        _need(decoded_bytes_override is None or (type(decoded_bytes_override) is int and
                                                 decoded_bytes_override >= 0), _t.Reason.TYPE)
        # Reserve the bounded workspace before source-custody verification,
        # graph traversal or canonical metadata serialization allocates scratch.
        _need(support_bytes + len(source_bytes) + len(plan.canonical_bytes) <= self._store_policy.artifact_bytes,
              _t.Reason.CAPACITY)
        _need(decoded_bytes_override is None or decoded_bytes_override <= self._store_policy.artifact_bytes,
              _t.Reason.CAPACITY)
        with self._lock:
            self._live()
            staging = self._reserve(scope, self._store_policy.artifact_bytes, 'store-admit-staging',
                                    plan.limits.absolute_deadline_ns, (plan.compiler_handle,))
        try:
            effective = screen_check or self._screen_check
            _check(effective(plan.definition.structural_screen_digest))
            meta = None
            if session is not None or admission is not None:
                _need(session is not None and type(admission) is _h.SourceAdmission, _t.Reason.MISSING)
                if inputs is not None and type(self._validation_registry) is _c.CompilerRegistry:
                    plan = _c.verify_pairing(plan, session=session, admission=admission,
                                             inputs=inputs, registry=self._validation_registry)
                else:
                    _h.verify_pins(plan.definition, session=session, admission=admission, screen_check=effective,
                                   plan=plan, compiler=self._compiler, registry=self._validation_registry)
                # Pairing above authenticates equal source/header content across
                # parser passes. Canonicalization requires the live custody object.
                meta = _h.canonical_metadata(session._header, session._auxiliary,
                                             session=session, admission=admission)
                auxiliary = session._auxiliary
                if not source_bytes:
                    source_bytes = session._body
            _check(self._validation_registry.check(plan, self._compiler, plan.identity, scope, plan.abis))
            h = plan.definition.header
            _need(plan.scope == scope == h.scope == plan.identity.scope, _t.Reason.FOREIGN)
            _need(h.supplied_pins == plan.identity if h.supplied_pins is not None
                  else h.pins_reason is not None, _t.Reason.STALE)
            decoded = max(_decoded_footprint_bytes(plan, self._store_policy.artifact_bytes), decoded_bytes_override or 0)
            meta = meta if meta is not None else _canonical_json_bytes((h, auxiliary))
            accounting = ByteAccounting(len(source_bytes), decoded, len(plan.canonical_bytes), len(meta), support_bytes)
            _need(accounting.total_charged_bytes() <= self._store_policy.artifact_bytes, _t.Reason.CAPACITY)
            identity = plan.identity
            key = ArtifactKey(scope, h.site_contract, identity.signature_schema, identity.structural_screen_digest,
                              identity.goal_contract, identity.logical_id, identity.revision, identity.plan,
                              identity.metadata, identity.artifact)
            aux = dict(auxiliary)
            _need(len(aux) == len(auxiliary) and set(aux) <= {'META:uso', 'META:fts', 'META:cli', 'MUTANT_PROOF'}, _t.Reason.SCHEMA)
            _need(all(type(aux[k]) is str for k in ('META:uso', 'META:cli') if k in aux), _t.Reason.TYPE)
            _need(type(aux.get('META:fts', ())) is tuple and all(type(v) is str for v in aux.get('META:fts', ())), _t.Reason.TYPE)
            with self._lock:
                self._live()
                _need(sid not in self._authority or self._authority[sid] == authority_revision, _t.Reason.STALE)
                digest = identity.artifact.full_digest
                _need(digest not in self._tombstones.get(sid, set()), _t.Reason.CONFLICT)
                old = self._cells.get((sid, digest))
                _need(old is None or not old.cell.resident, _t.Reason.CONFLICT)
                if sum(s.cell.resident for s in self._cells.values()) >= self._store_policy.resident_artifacts:
                    self.evict_idle(now_ns=now, max_evictions=1)
                _need(sum(s.cell.resident for s in self._cells.values()) < self._store_policy.resident_artifacts,
                      _t.Reason.CAPACITY)
                if len(self._evidence_records) >= self._max_evidence_records:
                    self.compact_evidence()
                _need(len(self._evidence_records) < self._max_evidence_records, _t.Reason.CAPACITY)
                _need(old is not None or len(self._cells) < self._max_evidence_records + self._store_policy.resident_artifacts, _t.Reason.CAPACITY)
                grant, holder = self._reserve(scope, accounting.total_charged_bytes(), 'store-admit',
                                               plan.limits.absolute_deadline_ns, (plan.compiler_handle,))
                if old is not None and old.manifest_allocation is not None:
                    try:
                        self._drain(*old.manifest_allocation)
                    except BaseException:
                        self._drain(grant, holder)
                        raise
                token = self._mint_ref('artifact_cell')
                cell = ArtifactCell(key, identity, initial_lifecycle, 1 if old is None else old.cell.lifecycle_revision + 1,
                    authority_revision, True, plan, source_bytes, plan.canonical_bytes, accounting,
                    0, 0, 0, now, now, aux.get('META:fts', ()), aux.get('META:uso') or None,
                    aux.get('META:cli') or None, predecessor_digest, negative_lineage, (), (), token)
                self._authority[sid] = authority_revision
                self._cells[(sid, digest)] = _CellState(cell, grant, holder)
                self._register_obj(cell)
                self._bump()
                return cell
        finally:
            with self._lock:
                try:
                    self._drain(*staging)
                except BaseException:
                    self._service_stopped, self._stop_reason = True, 'unresolved_staging_drain'
                    raise


    # @anchor:wa.learn.script_store.ScriptStore.acquire
    @_closed
    def acquire(self, target, *, scope, authority_revision, episode_id, consumer_kind='episode',
                consumer_identity='episode-consumer', purpose=_t.Purpose.QUERY, ttl_ns=None):
        with self._lock:
            self._live()
            self._scope(scope)
            _need(consumer_identity not in self._blocked_consumers, _t.Reason.CANCELLED)
            if type(target) is CandidateRef:
                _check(self.check_candidate(target, target.artifact, scope, target.lifecycle_revision))
            state = self._lookup(target, scope)
            c = state.cell
            _need(authority_revision == c.authority_revision == self.current_authority_revision(scope), _t.Reason.STALE)
            _need(c.resident, _t.Reason.MISSING)
            _need(c.key.artifact_digest.full_digest not in self._tombstones.get(_scope_tuple(scope), set()), _t.Reason.CONFLICT)
            _need(str(authority_revision.environment_compatibility) not in c.denied_environments
                  and str(authority_revision.environment_compatibility) not in
                  self._degraded_environments.get(_scope_tuple(scope), set()), _t.Reason.FOREIGN)
            eligible = {'episode': (_t.Lifecycle.PROMOTED,), 'dispatch': (_t.Lifecycle.PROMOTED,),
                        'context': (_t.Lifecycle.PROMOTED, _t.Lifecycle.SHADOW),
                        'replay': (_t.Lifecycle.DRAFT, _t.Lifecycle.SHADOW, _t.Lifecycle.PROMOTED, _t.Lifecycle.QUARANTINED)}
            _need(consumer_kind in eligible and c.lifecycle in eligible[consumer_kind], _t.Reason.CONFLICT)
            retained = [l for s in self._cells.values() for l in s.holders.values()
                        if l.holder_contract.consumer_identity == consumer_identity]
            p = self._resource_policy.cancellation
            _need(len(retained) < p.consumer_holder_ceiling and
                  sum(l.holder_contract.retained_bytes for l in retained) + c.accounting.total_charged_bytes()
                  <= p.consumer_byte_ceiling, _t.Reason.CAPACITY)
            now = self._now()
            _need(ttl_ns is None or (type(ttl_ns) is int and ttl_ns > 0), _t.Reason.BOUNDS)
            deadline = min(c.plan.limits.absolute_deadline_ns, now + ttl_ns) if ttl_ns is not None else c.plan.limits.absolute_deadline_ns
            _need(now < deadline, _t.Reason.DEADLINE)
            token = self._mint_ref('lease_holder')
            owner = _t.PrivateRef(self._owner.owner, self._incarnation, 'retained:' + token.handle_identity.token)
            holder = _t.HolderContract(consumer_identity, purpose, scope, c.accounting.total_charged_bytes(),
                                       deadline, self._endpoint, self._boundary, p)
            lease = ScriptLease(c.identity, c.key, scope, episode_id, self._incarnation, consumer_kind,
                                c.lifecycle_revision, authority_revision, now, deadline, holder, owner, token)
            self._register_obj(lease)
            state.holders[id(lease)] = lease
            self._change(state, last_use_ns=now)
            return lease

    # @anchor:wa.learn.script_store.ScriptStore.retain
    @_closed
    def retain(self, lease, *, consumer_kind=None, consumer_identity=None, episode_id=None, ttl_ns=None):
        with self._lock:
            _check(self.check_lease(lease, lease.artifact, lease.scope, lease.episode_id,
                                    lease.process_incarnation, lease.retained_owner))
            return self.acquire(lease.key, scope=lease.scope, authority_revision=lease.authority_revision,
                episode_id=episode_id or lease.episode_id, consumer_kind=consumer_kind or lease.consumer_kind,
                consumer_identity=consumer_identity or lease.holder_contract.consumer_identity,
                purpose=lease.holder_contract.purpose, ttl_ns=ttl_ns)

    # @anchor:wa.learn.script_store.ScriptStore.release
    @_closed
    def release(self, lease, retained_token=None):
        with self._lock:
            _need(type(lease) is ScriptLease, _t.Reason.TYPE)
            token = lease.holder_token if retained_token is None else retained_token
            _need(token is lease.holder_token, _t.Reason.FOREIGN)
            self._check_obj(lease, allow_revoked=True)
            self._check_obj(token, allow_revoked=True)
            if id(lease) in self._released_holders:
                return _t.AdmissionCheck(_t.Truth.TRUE, None)
            state = self._lookup(lease.key, lease.scope)
            _need(id(lease) not in state.open_by_lease, _t.Reason.CONFLICT)
            if id(lease) not in state.released:
                _need(id(lease) in state.holders, _t.Reason.FOREIGN)
                del state.holders[id(lease)]
                state.released.add(id(lease))
                self._released_holders.add(id(lease))
                self._unregister(lease)
                self._unregister(token)
                if not state.holders and not state.permits:
                    self._change(state, last_unpin_ns=self._now())
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_store.ScriptStore.check_lease
    @_closed
    def check_lease(self, lease, artifact, scope, episode_id, process_incarnation, retained_owner):
        with self._lock:
            self._live()
            _need(type(lease) is ScriptLease, _t.Reason.TYPE)
            self._check_obj(lease)
            self._check_obj(lease.holder_token)
            _need(lease.retained_owner is retained_owner and lease.artifact == artifact
                  and lease.scope == scope and lease.episode_id == episode_id
                  and lease.process_incarnation == process_incarnation == self._incarnation, _t.Reason.FOREIGN)
            state = self._lookup(lease.key, scope)
            c = state.cell
            _need(c.resident, _t.Reason.MISSING)
            _need(c.key.artifact_digest.full_digest not in self._tombstones.get(_scope_tuple(scope), set())
                  and c.lifecycle not in (_t.Lifecycle.SUSPENDED, _t.Lifecycle.QUARANTINED, _t.Lifecycle.RETIRED), _t.Reason.CONFLICT)
            _need(lease.authority_revision == c.authority_revision == self.current_authority_revision(scope), _t.Reason.STALE)
            _need(self._now() < lease.absolute_deadline_ns, _t.Reason.DEADLINE)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_store.ScriptStore.begin_step
    @_closed
    def begin_step(self, lease, intent, scope, authority):
        with self._lock:
            _need(type(intent) is _t.StepIntent and type(scope) is _t.ScopeIdentity
                  and type(authority) is _t.AuthorityRevision, _t.Reason.TYPE)
            _check(self.check_lease(lease, intent.artifact, scope, lease.episode_id,
                                    lease.process_incarnation, lease.retained_owner))
            state = self._lookup(lease.key, scope)
            c = state.cell
            _need(c.lifecycle is _t.Lifecycle.PROMOTED, _t.Reason.CONFLICT)
            _need(intent.scope == scope == lease.scope and intent.authority == authority
                  == lease.authority_revision == c.authority_revision, _t.Reason.STALE)
            _need(0 <= intent.order < len(c.plan.nodes), _t.Reason.BOUNDS)
            node = c.plan.nodes[intent.order]
            _need(node.step_id == intent.step_id and node.effect is not None
                  and node.effect.action is intent.action and node.effect.selector == intent.selector
                  and node.effect.postcondition == intent.expected_transition, _t.Reason.CONFLICT)
            _need(id(lease) not in state.open_by_lease, _t.Reason.CONFLICT)
            permit = self._mint_ref('step_permit')
            state.permits[id(permit)] = (permit, lease, authority)
            state.open_by_lease[id(lease)] = permit
            self._change(state, last_use_ns=self._now())
            return permit

    # @anchor:wa.learn.script_store.ScriptStore.end_step
    @_closed
    def end_step(self, permit, result, scope, authority):
        with self._lock:
            _need(type(permit) is _t.RegistryRef and type(result) is _t.StepResult
                  and type(scope) is _t.ScopeIdentity and type(authority) is _t.AuthorityRevision, _t.Reason.TYPE)
            self._check_obj(permit)
            _need(permit.kind == 'step_permit', _t.Reason.FOREIGN)
            states = [s for s in self._cells.values() if id(permit) in s.permits]
            _need(len(states) == 1, _t.Reason.FOREIGN)
            state = states[0]
            _, lease, issued_authority = state.permits[id(permit)]
            _need(scope == lease.scope, _t.Reason.FOREIGN)
            _need(authority == issued_authority and authority.scope == scope.scope_revision, _t.Reason.STALE)
            _need((result.pre is None or result.pre.scope == scope)
                  and (result.post is None or result.post.scope == scope), _t.Reason.STALE)
            self._close_permit(state, permit)
            self._change(state, last_use_ns=self._now())
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    def _close_permit(self, state, permit):
        _, lease, _ = state.permits.pop(id(permit))
        state.open_by_lease.pop(id(lease), None)
        self._unregister(permit)

    # @anchor:wa.learn.script_store.ScriptStore.evict_idle
    @_closed
    def evict_idle(self, *, now_ns=None, max_evictions=None, drain_proofs=None):
        with self._lock:
            now = self._now() if now_ns is None else now_ns
            _need(type(now) is int and now >= 0 and
                  (max_evictions is None or (type(max_evictions) is int and max_evictions >= 0)), _t.Reason.BOUNDS)
            def eligible(s):
                c = s.cell
                return (c.resident and not s.holders and not s.permits and c.pending_admissions == 0
                        and now - max(c.last_use_ns, c.last_unpin_ns) >= self._resolver_policy.idle_grace_ns)
            victims = sorted((s for s in self._cells.values() if eligible(s)),
                key=lambda s: (max(s.cell.last_use_ns, s.cell.last_unpin_ns), s.cell.key.canonical_sort_key()))
            out = []
            for state in victims[:max_evictions]:
                if not eligible(state):
                    continue
                c = state.cell
                manifest_bytes = max(1, c.accounting.metadata_bytes)
                retained_metadata = sum(a[0].charged_bytes for s in self._cells.values()
                                        if (a := s.manifest_allocation) is not None)
                _need(retained_metadata + manifest_bytes <= self._store_policy.catalogue_bytes,
                      _t.Reason.CAPACITY)
                manifest = self._reserve_snapshot_bytes(c.key.scope, manifest_bytes)
                try:
                    self._drain(state.grant, state.holder,
                                (drain_proofs or {}).get(c.key.artifact_digest.full_digest))
                except BaseException:
                    self._drain(*manifest)
                    raise
                state.manifest_allocation = manifest
                if hasattr(self._validation_registry, 'release') and not any(
                        s is not state and s.cell.resident and s.cell.plan is c.plan for s in self._cells.values()):
                    _check(self._validation_registry.release(c.plan))
                self._change(state, resident=False, plan=None, source_bytes=None,
                    canonical_plan_bytes=None, accounting=ByteAccounting(0, 0, 0, 0, 0),
                    lifecycle_revision=c.lifecycle_revision + 1)
                # Remove owner-held views of payloads. Only compact manifests remain.
                for oid, row in tuple(self._handles.items()):
                    if type(row[0]) is ArtifactCell and row[0].key == c.key:
                        del self._handles[oid]
                out.append(c.key)
            return tuple(out)

    # @anchor:wa.learn.script_store.ScriptStore.publish_default
    @_closed
    def publish_default(self, key_or_digest, *, scope, expected_predecessor_revision, certificate):
        with self._lock:
            self._live()
            self._certificate(certificate, ('promotion_certificate', 'certificate'))
            state = self._lookup(key_or_digest, scope)
            c = state.cell
            _need(c.resident, _t.Reason.MISSING)
            _need(c.lifecycle in (_t.Lifecycle.DRAFT, _t.Lifecycle.SHADOW, _t.Lifecycle.PROMOTED)
                  and c.key.artifact_digest.full_digest not in self._tombstones.get(_scope_tuple(scope), set()), _t.Reason.CONFLICT)
            _need(c.authority_revision == self.current_authority_revision(scope), _t.Reason.STALE)
            previous = self._default_by_slot.get(c.key.slot_key())
            if previous is None:
                _need(expected_predecessor_revision == 0, _t.Reason.STALE)
            else:
                old = self._lookup(_t.ArtifactDigest('sha256', previous), scope).cell
                _need(old.lifecycle_revision == expected_predecessor_revision, _t.Reason.STALE)
                _need(c.key.revision > old.key.revision, _t.Reason.CONFLICT)
            self._change(state, lifecycle=_t.Lifecycle.PROMOTED, lifecycle_revision=c.lifecycle_revision + 1)
            self._default_by_slot[c.key.slot_key()] = c.key.artifact_digest.full_digest
            return self._view(state)

    # @anchor:wa.learn.script_store.ScriptStore.check_candidate
    @_closed
    def check_candidate(self, candidate, artifact, scope, lifecycle_revision):
        with self._lock:
            self._live()
            _need(type(candidate) is CandidateRef, _t.Reason.TYPE)
            self._check_obj(candidate)
            self._check_obj(candidate.owner_ref)
            _need(candidate.artifact == artifact and candidate.scope == scope, _t.Reason.FOREIGN)
            c = self._lookup(candidate.key, scope).cell
            _need(candidate.lifecycle_revision == lifecycle_revision == c.lifecycle_revision
                  and candidate.authority_revision == c.authority_revision == self.current_authority_revision(scope)
                  and candidate.deny_sequence == self._deny_sequence, _t.Reason.STALE)
            _need(c.resident and c.lifecycle is _t.Lifecycle.PROMOTED and
                  c.key.artifact_digest.full_digest not in self._tombstones.get(_scope_tuple(scope), set()), _t.Reason.CONFLICT)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_store.ScriptStore.check_snapshot
    @_closed
    def check_snapshot(self, snapshot, scope, generation, digest, current_deny_sequence):
        with self._lock:
            self._check_obj(snapshot)
            self._check_obj(snapshot.owner_ref)
            _need(snapshot.scope == scope, _t.Reason.FOREIGN)
            self._scope(scope)
            _need(snapshot.generation == generation and snapshot.snapshot_digest == digest
                  and current_deny_sequence == self._deny_sequence == snapshot.deny_sequence
                  and snapshot.authority_revision == self.current_authority_revision(scope), _t.Reason.STALE)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_store.ScriptStore.check_event
    @_closed
    def check_event(self, *args, **kwargs):
        raise _t.ContractError(_t.Reason.FOREIGN)

    # @anchor:wa.learn.script_store.ScriptStore.check_seal
    @_closed
    def check_seal(self, *args, **kwargs):
        raise _t.ContractError(_t.Reason.FOREIGN)

    # @anchor:wa.learn.script_store.ScriptStore.check_certificate
    @_closed
    def check_certificate(self, certificate, *args, **kwargs):
        # Dream certificates belong to a separate owner, not this store.
        raise _t.ContractError(_t.Reason.FOREIGN)

    def _deny_lease(self, state, lease):
        # Denial removes execution authority, never retained ownership.
        if id(lease) in state.open_by_lease:
            self._close_permit(state, state.open_by_lease[id(lease)])
        self._unregister(lease)
        self._unregister(lease.holder_token)

    # @anchor:wa.learn.script_store.ScriptStore.revoke
    @_closed
    def revoke(self, classification, *, target_digest=None, lease=None):
        with self._lock:
            _need(type(classification) is _t.FailureClassification, _t.Reason.TYPE)
            sid = self._scope(classification.scope)
            for ev in classification.evidence:
                self._check_obj(ev)
            if lease is not None:
                self._check_obj(lease)
                _need(lease.scope == classification.scope and lease.episode_id == classification.episode_id, _t.Reason.FOREIGN)
                if target_digest is None:
                    target_digest = lease.key.artifact_digest
            state = self._lookup(target_digest, classification.scope) if target_digest is not None else None
            if state is not None:
                _need(classification.lifecycle_revision == state.cell.lifecycle_revision, _t.Reason.STALE)
            cause = classification.cause
            episode_causes = (_t.FailureClass.EPISODE_CANCELLATION, _t.FailureClass.EPISODE_DEADLINE)
            _need(cause not in episode_causes or lease is not None, _t.Reason.MISSING)
            outage_causes = (_t.FailureClass.BACKEND_OUTAGE, _t.FailureClass.AUDIT_OUTAGE)
            _need(state is not None or cause in outage_causes or cause is _t.FailureClass.INCOMPATIBLE_SCOPE,
                  _t.Reason.MISSING)
            # Reserve evidence space before mutating denial state.
            if len(self._evidence_records) >= self._max_evidence_records:
                self.compact_evidence()
            if len(self._evidence_records) >= self._max_evidence_records:
                self._service_stopped, self._stop_reason = True, 'evidence_capacity'
                raise _t.ContractError(_t.Reason.CAPACITY)
            self._deny_sequence += 1
            seq = self._deny_sequence
            digest = target_digest.full_digest if target_digest is not None else None
            self._evidence_records[seq] = (sid, digest, classification)
            if cause in episode_causes:
                _need(lease is not None, _t.Reason.MISSING)
                self._deny_lease(state, lease)
            elif cause in outage_causes:
                self._degraded_environments.setdefault(sid, set()).add(classification.environment)
                for s in self._cells.values():
                    if _scope_tuple(s.cell.key.scope) == sid:
                        for l in tuple(s.holders.values()):
                            if str(l.authority_revision.environment_compatibility) == classification.environment:
                                self._deny_lease(s, l)
            elif cause is _t.FailureClass.INCOMPATIBLE_SCOPE:
                old = self.current_authority_revision(classification.scope)
                self.invalidate_authority(classification.scope, _dc.replace(old, policy=old.policy + 1))
            else:
                c = state.cell
                if cause is _t.FailureClass.ARTIFACT_DEFECT:
                    lifecycle = _t.Lifecycle.SUSPENDED
                    self._tombstones.setdefault(sid, set()).add(digest)
                    self._default_by_slot.pop(c.key.slot_key(), None)
                    self._change(state, lifecycle=lifecycle, lifecycle_revision=c.lifecycle_revision + 1)
                elif cause is _t.FailureClass.UNCLASSIFIED:
                    self._change(state, lifecycle=_t.Lifecycle.QUARANTINED,
                        quarantine_environments=tuple(sorted(set(c.quarantine_environments + (classification.environment,)))),
                        lifecycle_revision=c.lifecycle_revision + 1)
                elif cause is _t.FailureClass.INCOMPATIBLE_ENVIRONMENT:
                    self._change(state, denied_environments=tuple(sorted(set(c.denied_environments + (classification.environment,)))),
                                 lifecycle_revision=c.lifecycle_revision + 1)
                else:
                    raise _t.ContractError(_t.Reason.SCHEMA)
                for l in tuple(state.holders.values()):
                    if cause is not _t.FailureClass.INCOMPATIBLE_ENVIRONMENT or str(l.authority_revision.environment_compatibility) == classification.environment:
                        self._deny_lease(state, l)
            self._bump()
            return self._deny_sequence

    # @anchor:wa.learn.script_store.ScriptStore.invalidate_authority
    @_closed
    def invalidate_authority(self, scope, new_authority):
        with self._lock:
            sid = self._scope(scope)
            _need(type(new_authority) is _t.AuthorityRevision and new_authority.scope == scope.scope_revision, _t.Reason.STALE)
            old = self.current_authority_revision(scope)
            _need(new_authority != old and all(getattr(new_authority, name) >= getattr(old, name)
                  for name in ('policy', 'capability_set', 'schema', 'scope', 'environment_compatibility')), _t.Reason.STALE)
            self._authority[sid] = new_authority
            self._deny_sequence += 1
            # Keep stale registered leases so public checks report STALE; permits
            # lose send authority synchronously at this transaction boundary.
            for s in self._cells.values():
                if _scope_tuple(s.cell.key.scope) == sid:
                    for permit, _, _ in tuple(s.permits.values()):
                        self._close_permit(s, permit)
            self._bump()
            return self._deny_sequence

    # @anchor:wa.learn.script_store.ScriptStore.reinstate_quarantined
    @_closed
    def reinstate_quarantined(self, artifact_digest, *, scope, expected_lifecycle_revision,
                             recovery_certificate, target_lifecycle=_t.Lifecycle.PROMOTED):
        with self._lock:
            self._live()
            state = self._lookup(artifact_digest, scope)
            c = state.cell
            _need(artifact_digest.full_digest not in self._tombstones.get(_scope_tuple(scope), set()), _t.Reason.CONFLICT)
            self._certificate(recovery_certificate, ('recovery_certificate',))
            _need(c.lifecycle is _t.Lifecycle.QUARANTINED and c.resident, _t.Reason.CONFLICT)
            _need(c.lifecycle_revision == expected_lifecycle_revision
                  and c.authority_revision == self.current_authority_revision(scope), _t.Reason.STALE)
            _need(target_lifecycle in (_t.Lifecycle.PROMOTED, _t.Lifecycle.SHADOW), _t.Reason.SCHEMA)
            self._change(state, lifecycle=target_lifecycle, lifecycle_revision=c.lifecycle_revision + 1,
                         quarantine_environments=())
            return self._view(state)

    # @anchor:wa.learn.script_store.ScriptStore.enforce_holder_deadlines
    @_closed
    def enforce_holder_deadlines(self, *, now_ns=None):
        with self._lock:
            now = self._now() if now_ns is None else now_ns
            _need(type(now) is int and now >= 0, _t.Reason.BOUNDS)
            overdue = []
            for s in self._cells.values():
                for lease in tuple(s.holders.values()):
                    if now >= lease.absolute_deadline_ns:
                        consumer = lease.holder_contract.consumer_identity
                        self._blocked_consumers.add(consumer)
                        overdue.append(consumer)
                        # Stop is recorded before callback; failure cannot bypass deadline.
                        if now >= lease.absolute_deadline_ns + lease.holder_contract.policy.join_deadline_ns:
                            self._service_stopped, self._stop_reason = True, 'recoverable_service_stop'
                        self._cancellation.request(lease.holder_contract)
            return tuple(sorted(set(overdue)))

    # @anchor:wa.learn.script_store.ScriptStore.complete_holder_cancellation
    @_closed
    def complete_holder_cancellation(self, lease, proof):
        with self._lock:
            self._check_obj(lease, allow_revoked=True)
            _need(type(proof) is _t.CancellationResult, _t.Reason.TYPE)
            _check(self._cancellation.verify(lease.holder_contract, proof))
            _need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined))
                  and any(tok is lease.holder_token for tok in proof.released_tokens), _t.Reason.FOREIGN)
            state = self._lookup(lease.key, lease.scope)
            if id(lease) in state.open_by_lease:
                self._close_permit(state, state.open_by_lease[id(lease)])
            self.release(lease)
            consumer = lease.holder_contract.consumer_identity
            if not any(l.holder_contract.consumer_identity == consumer and self._now() >= l.absolute_deadline_ns
                       for s in self._cells.values() for l in s.holders.values()):
                self._blocked_consumers.discard(consumer)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.script_store.ScriptStore.compact_evidence
    @_closed
    def compact_evidence(self):
        with self._lock:
            # Unknown/drained producer reachability is not inferred. Tombstones
            # remain authoritative; only episode evidence with no holder roots dies.
            roots = set()
            for (sid, digest), s in self._cells.items():
                c = s.cell
                if c.resident or s.holders or s.permits or c.key.slot_key() in self._default_by_slot:
                    roots.add((sid, digest))
                if c.lifecycle is not _t.Lifecycle.RETIRED:
                    roots.update((sid, d.full_digest) for d in c.negative_lineage)
                    if c.predecessor_digest is not None:
                        roots.add((sid, c.predecessor_digest.full_digest))
            for snap in self._published_snapshots.values():
                roots.update((_scope_tuple(snap.scope), e.key.artifact_digest.full_digest)
                             for e in snap.entries + snap.shadow_entries)
            for (sid, _), (snap, _, _) in self._snapshot_allocations.items():
                roots.update((sid, e.key.artifact_digest.full_digest) for e in snap.entries + snap.shadow_entries)
            removed = 0
            for seq, (sid, digest, classification) in tuple(self._evidence_records.items()):
                episode_only = classification.cause in (_t.FailureClass.EPISODE_CANCELLATION,
                                                          _t.FailureClass.EPISODE_DEADLINE)
                episode_root = any(_scope_tuple(s.cell.key.scope) == sid and
                    any(l.episode_id == classification.episode_id for l in s.holders.values())
                    for s in self._cells.values())
                unreachable = ((episode_only and not episode_root) or
                               ((sid, digest) not in roots and digest not in self._tombstones.get(sid, set())))
                if unreachable:
                    del self._evidence_records[seq]
                    removed += 1
            self._deny_watermark = max(self._deny_watermark,
                min(self._evidence_records, default=self._deny_sequence + 1) - 1)
            return removed

    # @anchor:wa.learn.script_store.ScriptStore.pinned_generation_count
    @_closed
    def pinned_generation_count(self, scope):
        with self._lock:
            sid = self._scope(scope)
            return sum(1 for (s, _), count in self._snapshot_pins.items() if s == sid and count > 0)

    def _reserve_snapshot_bytes(self, scope, size):
        return self._reserve(scope, size, 'catalogue-publish',
                             self._now() + self._resource_policy.cancellation.join_deadline_ns)

    def _retire_snapshot(self, key):
        allocation = self._snapshot_allocations.get(key)
        if allocation is not None and not self._snapshot_pins.get(key, 0):
            snap, grant, holder = allocation
            if self._published_snapshots.get(key[0]) is not snap:
                self._drain(grant, holder)
                del self._snapshot_allocations[key]

    # @anchor:wa.learn.script_store.ScriptStore.release_snapshot_pin
    @_closed
    def release_snapshot_pin(self, token):
        with self._lock:
            self._check_obj(token, allow_revoked=True)
            _need(token.kind == 'snapshot_pin', _t.Reason.FOREIGN)
            key = self._snapshot_pin_tokens.get(id(token))
            if key is not None:
                self._snapshot_pins[key] -= 1
                del self._snapshot_pin_tokens[id(token)]
                self._unregister(token)
                self._retire_snapshot(key)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)


# Top-level entry points mirror the owner methods without separate state.


# @anchor:wa.learn.script_store.admit_artifact
@_closed
def admit_artifact(store, plan, *, scope, authority_revision, source_bytes=b'', auxiliary=(), support_bytes=0, decoded_bytes_override=None, initial_lifecycle=_t.Lifecycle.DRAFT, predecessor_digest=None, negative_lineage=(), certificate=None, session=None, admission=None, inputs=None, screen_check=None):
    return store.admit_artifact(plan, scope=scope, authority_revision=authority_revision, source_bytes=source_bytes, auxiliary=auxiliary, support_bytes=support_bytes, decoded_bytes_override=decoded_bytes_override, initial_lifecycle=initial_lifecycle, predecessor_digest=predecessor_digest, negative_lineage=negative_lineage, certificate=certificate, session=session, admission=admission, inputs=inputs, screen_check=screen_check)


# @anchor:wa.learn.script_store.acquire
@_closed
def acquire(store, target, *, scope, authority_revision, episode_id, consumer_kind='episode', consumer_identity='episode-consumer', purpose=_t.Purpose.QUERY, ttl_ns=None):
    return store.acquire(target, scope=scope, authority_revision=authority_revision, episode_id=episode_id, consumer_kind=consumer_kind, consumer_identity=consumer_identity, purpose=purpose, ttl_ns=ttl_ns)


# @anchor:wa.learn.script_store.retain
@_closed
def retain(store, lease, *, consumer_kind=None, consumer_identity=None, episode_id=None, ttl_ns=None):
    return store.retain(lease, consumer_kind=consumer_kind, consumer_identity=consumer_identity, episode_id=episode_id, ttl_ns=ttl_ns)


# @anchor:wa.learn.script_store.release
@_closed
def release(store, lease, retained_token=None):
    return store.release(lease, retained_token)


# @anchor:wa.learn.script_store.check_lease
@_closed
def check_lease(store, lease, artifact, scope, episode_id, process_incarnation, retained_owner):
    return store.check_lease(lease, artifact, scope, episode_id, process_incarnation, retained_owner)


# @anchor:wa.learn.script_store.begin_step
@_closed
def begin_step(store, lease, intent, scope, authority):
    return store.begin_step(lease, intent, scope, authority)


# @anchor:wa.learn.script_store.end_step
@_closed
def end_step(store, permit, result, scope, authority):
    return store.end_step(permit, result, scope, authority)


# @anchor:wa.learn.script_store.evict_idle
@_closed
def evict_idle(store, *, now_ns=None, max_evictions=None, drain_proofs=None):
    return store.evict_idle(now_ns=now_ns, max_evictions=max_evictions, drain_proofs=drain_proofs)


# @anchor:wa.learn.script_store.publish_default
@_closed
def publish_default(store, key_or_digest, *, scope, expected_predecessor_revision, certificate):
    return store.publish_default(key_or_digest, scope=scope, expected_predecessor_revision=expected_predecessor_revision, certificate=certificate)


# @anchor:wa.learn.script_store.revoke
@_closed
def revoke(store, classification, *, target_digest=None, lease=None):
    return store.revoke(classification, target_digest=target_digest, lease=lease)
