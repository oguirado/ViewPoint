# @module {"id":"wa.learn.context_router","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.learn.memory","wa.learn.catalogue","wa.learn.resolver"],"book":"HB-WA-s4-learn"}
"""Owner-associated context epochs. No production host or provider is assumed.

Provider serialization here is a versioned, length-delimited message envelope.
A transport must support these exact role boundaries; flattening to a string is
not an implementation of the epoch protocol. All evidence remains passive.
"""
import dataclasses as _dc
import hashlib as _hash
import json as _json
import unicodedata as _unicode
from contextlib import AbstractContextManager as _Fence
from typing import Protocol as _Protocol
from wender.scripts import types as _t, header as _h
from wender.learn import memory as _memory, catalogue as _catalogue, resolver as _resolver

_need, _check, _closed = _h._need, _h._check, _h._closed
_ANCHOR = b'Reason only within the supplied passive evidence and the current trusted task scope.'


# @anchor:wa.learn.context_router.ContextHost
class ContextHost(_Protocol):
    """Trusted owner, not model data. All methods run under its reentrant fence.

    Registration attests restart evidence, exact configuration/capabilities,
    safe no-effect barrier and limits on retained epochs/requests/bases. check
    authenticates object identity, scope, eligibility and grounding; a packed
    request must be live, unconsumed and associated with the actual transport
    response. consume atomically retires it. cancel_and_drain joins callbacks,
    drops retained bases and stops acceptance before returning authenticated
    success. Failed cleanup retains recovery ownership and accounting.
    """
    # @anchor:wa.learn.context_router.ContextHost.fence
    def fence(self) -> _Fence: ...
    # @anchor:wa.learn.context_router.ContextHost.now_ns
    def now_ns(self) -> int: ...
    # @anchor:wa.learn.context_router.ContextHost.attest
    def attest(self, configuration: 'EpochConfiguration') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextHost.register_epoch
    def register_epoch(self, epoch: 'ContextEpoch') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextHost.check
    def check(self, epoch: 'ContextEpoch', request: 'PackedContext | None',
              response: object | None) -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextHost.register_request
    def register_request(self, epoch: 'ContextEpoch', request: 'PackedContext') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextHost.check_delta
    def check_delta(self, epoch: 'ContextEpoch', base: 'DeltaBase') -> _t.AdmissionCheck:
        """Authenticate acknowledgement, request, exact retained state and checkpoint.

        Called under fence. TRUE promises retention until request drain. Missing
        service or any unsuccessful check disables delta, never full evidence.
        """
        ...
    # @anchor:wa.learn.context_router.ContextHost.consume
    def consume(self, epoch: 'ContextEpoch', request: 'PackedContext') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextHost.cancel_and_drain
    def cancel_and_drain(self, epoch: 'ContextEpoch', reason: _t.Reason) -> _t.AdmissionCheck: ...


# @anchor:wa.learn.context_router.ExportProjectionOwner
class ExportProjectionOwner(_Protocol):
    # @anchor:wa.learn.context_router.ExportProjectionOwner.receipt_for
    def receipt_for(self, payload: bytes, *, scope: _t.ScopeIdentity,
                    destination: _t.ExportDestination, purpose: _t.Purpose,
                    transform_chain: tuple[str, ...]) -> _t.ExportReceipt:
        """Authenticate exact final bytes/pixels to an owner-registered receipt.

        Private hashes are never public identifiers. Composition, encoding,
        cropping and overlay each require a new derived approval. A receipt
        for the original observation cannot approve changed output.
        """
        ...


# @anchor:wa.learn.context_router.ReceiptExportAdapter
@_dc.dataclass(frozen=True, slots=True, repr=False)
class ReceiptExportAdapter:
    receipt_owner: ExportProjectionOwner
    receipt_port: _t.ExportReceiptPort
    privacy_policy_revision: int
    purpose: _t.Purpose

    # @anchor:wa.learn.context_router.ReceiptExportAdapter.approve
    @_closed
    def approve(self, payload, *, scope, destination, purpose, transform_chain):
        _need(type(payload) is bytes and type(purpose) is _t.Purpose and
              purpose is self.purpose and type(self.privacy_policy_revision) is int and
              self.privacy_policy_revision > 0, _t.Reason.PRIVACY)
        receipt = self.receipt_owner.receipt_for(payload, scope=scope,
            destination=destination, purpose=purpose, transform_chain=transform_chain)
        _need(type(receipt) is _t.ExportReceipt and receipt.scope == scope and
              receipt.destination is destination and receipt.purpose is purpose and
              receipt.transform_chain == transform_chain and
              receipt.privacy_policy_revision == self.privacy_policy_revision, _t.Reason.PRIVACY)
        _check(self.receipt_port.check(receipt, receipt.source_observation, transform_chain,
            receipt.content_identity, destination, purpose, self.privacy_policy_revision))
        return _t.AdmissionCheck(_t.Truth.TRUE, None)


# @anchor:wa.learn.context_router.ContextResources
class ContextResources(_Protocol):
    """Configured allocation owner; no size/deadline/telemetry defaults.

    reserve charges attempted work and reserves the configured operation's full
    overlapping peak before any copy/serialization/tokenization. It validates
    retained-owner tokens, aggregate admission, deadlines and finite queues.
    retain transfers lifetime to an authenticated owner; drain authenticates
    actual release, never refunds performed work. recovery retains failed drain.
    """
    # @anchor:wa.learn.context_router.ContextResources.reserve
    def reserve(self, operation: str, *, scope: _t.ScopeIdentity,
                absolute_deadline_ns: int, retained: tuple[object, ...]) -> object: ...
    # @anchor:wa.learn.context_router.ContextResources.retain
    def retain(self, allocation: object, owner: object) -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextResources.drain
    def drain(self, allocation: object) -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.context_router.ContextResources.recovery
    def recovery(self, allocation: object, reason: _t.Reason) -> None: ...


# @anchor:wa.learn.context_router.EpochConfiguration
@_dc.dataclass(frozen=True, slots=True, repr=False)
class EpochConfiguration:
    scope: _t.ScopeIdentity
    authority: _t.AuthorityRevision
    restart_evidence: _t.MissingRef
    absolute_deadline_ns: int
    model_identity: str
    tokenizer_identity: str
    unicode_identity: str
    normalizer_identity: str
    trusted_instructions: bytes
    output_schema: bytes
    capabilities: tuple[str, ...]
    image_order: tuple[str, ...]
    cycle_version: int
    forbidden_substrings: tuple[str, ...]
    passive_roles: bool


# @anchor:wa.learn.context_router.ContextCard
@_dc.dataclass(frozen=True, slots=True, repr=False)
class ContextCard:
    # Exact canonical (scope, logical-id, revision, artifact, kind, card-id).
    identity: tuple[_t.ScopeIdentity, str, int, str, str, str]
    payload: bytes
    declared_terms: tuple[str, ...]


# @anchor:wa.learn.context_router.SuffixEvidence
@_dc.dataclass(frozen=True, slots=True, repr=False)
class SuffixEvidence:
    # Opaque references/approved projections only; no raw slot values.
    task_slots: bytes
    deny_uncertainty: bytes
    active_plan_checkpoint: bytes
    observation_completeness: bytes
    latest_semantic_result: bytes
    candidate_grounding: bytes
    trusted_reminder: bytes
    omissions: tuple[str, ...]
    cards: tuple[ContextCard, ...]


# @anchor:wa.learn.context_router.ContextEpoch
@_dc.dataclass(frozen=True, slots=True, eq=False, repr=False)
class ContextEpoch:
    configuration: EpochConfiguration
    policy: _t.ContextPolicy
    display_policy: _t.DisplayPolicy
    prefix: bytes
    prefix_identity: _t.MetadataDigest
    snapshot: _catalogue.CatalogueSnapshot
    pin_token: object
    memory_projection: bytes
    host: object
    store: object
    tokenizer: object
    export_adapter: object
    resources: object
    allocation: object


# @anchor:wa.learn.context_router.DeltaBase
@_dc.dataclass(frozen=True, slots=True, eq=False, repr=False)
class DeltaBase:
    """Exact retained state; constructing this record does not acknowledge it."""
    acknowledgement: object
    epoch: ContextEpoch
    request: object
    generation: int
    checkpoint: bytes
    sufficient_state: bytes
    digest: _t.MetadataDigest


# @anchor:wa.learn.context_router.PackedContext
@_dc.dataclass(frozen=True, slots=True, eq=False, repr=False)
class PackedContext:
    epoch: ContextEpoch
    request_id: str
    request: _t.ContextRequest
    envelope: bytes
    selected: tuple[tuple[_t.ScopeIdentity, str, int, str, str, str], ...]
    omissions: tuple[str, ...]
    suffix_tokens: int
    total_bytes: int
    allocation: object
    sufficient_state: bytes
    checkpoint: bytes
    delta_base: DeltaBase | None = _dc.field(default=None, repr=False)


# @anchor:wa.learn.context_router.ContextResult
@_dc.dataclass(frozen=True, slots=True)
class ContextResult:
    status: str
    reason: _t.Reason | None
    packed: PackedContext | None = _dc.field(default=None, repr=False)


def _wire(value):
    return _json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False).encode('utf-8')


def _message(role, payload):
    _need(type(payload) is bytes, _t.Reason.TYPE)
    payload.decode('utf-8', 'strict')
    return role.encode('ascii') + b':' + str(len(payload)).encode('ascii') + b':' + payload + b'\n'


def _count(tokenizer, payload):
    n = tokenizer.count(payload)
    _need(type(n) is int and n > 0 and tokenizer.count(payload) == n, _t.Reason.BOUNDS)
    return n


def _approve(adapter, payload, scope, destination, chain):
    _check(adapter.approve(payload, scope=scope, destination=destination,
        purpose=adapter.purpose, transform_chain=chain))


def _drain(resources, allocation):
    try:
        _check(resources.drain(allocation))
    except Exception:
        resources.recovery(allocation, _t.Reason.RECONCILIATION)
        raise _t.ContractError(_t.Reason.RECONCILIATION) from None


def _live(epoch):
    _need(type(epoch) is ContextEpoch, _t.Reason.TYPE)
    _check(epoch.host.check(epoch, None, None))
    c = epoch.configuration
    _need(epoch.host.now_ns() < c.absolute_deadline_ns, _t.Reason.DEADLINE)
    _need(epoch.tokenizer.identity == c.tokenizer_identity, _t.Reason.STALE)
    _need(epoch.store.current_authority_revision(c.scope) == c.authority, _t.Reason.STALE)
    _check(epoch.store.check_snapshot(epoch.snapshot, c.scope, epoch.snapshot.generation,
        epoch.snapshot.snapshot_digest, epoch.store.deny_sequence))


# @anchor:wa.learn.context_router.open_epoch
@_closed
def open_epoch(*, host, store, memory, policy, display_policy,
               tokenizer, export_adapter, configuration, resources):
    _need(type(configuration) is EpochConfiguration and type(policy) is _t.ContextPolicy
          and type(display_policy) is _t.DisplayPolicy, _t.Reason.TYPE)
    c = configuration
    _need(type(c.scope) is _t.ScopeIdentity and c.scope.attestation is not None
          and c.scope.attestation_reason is None, _t.Reason.FOREIGN)
    _need(type(c.authority) is _t.AuthorityRevision and c.authority.scope == c.scope.scope_revision,
          _t.Reason.STALE)
    _need(type(c.absolute_deadline_ns) is int and host.now_ns() < c.absolute_deadline_ns,
          _t.Reason.DEADLINE)
    _need(c.normalizer_identity == 'nfc-casefold-nfc-split/v1' and
          c.unicode_identity == _unicode.unidata_version and
          type(c.model_identity) is str and bool(c.model_identity) and
          type(c.tokenizer_identity) is str and bool(c.tokenizer_identity) and
          tokenizer.identity == c.tokenizer_identity, _t.Reason.STALE)
    _need(type(c.passive_roles) is bool and type(c.capabilities) is tuple and
          all(type(v) is str for v in c.capabilities) and type(c.image_order) is tuple and
          all(type(v) is str for v in c.image_order), _t.Reason.TYPE)
    _need(type(c.trusted_instructions) is bytes and type(c.output_schema) is bytes and
          type(c.forbidden_substrings) is tuple and
          all(type(v) is str for v in c.forbidden_substrings), _t.Reason.TYPE)
    with host.fence():
        _check(host.attest(c))
        allocation = resources.reserve('context-open', scope=c.scope,
            absolute_deadline_ns=c.absolute_deadline_ns, retained=())
        pin = None
        try:
            snap, pin = _catalogue.snapshot(store, c.scope, pin=True)
            _need(snap.authority_revision == c.authority, _t.Reason.STALE)
            _check(store.check_snapshot(snap, c.scope, snap.generation,
                                       snap.snapshot_digest, store.deny_sequence))
            try:
                mem = _memory.freeze_context_memory(memory, scope=c.scope,
                    cycle_version=c.cycle_version, forbidden_substrings=c.forbidden_substrings,
                    tokenizer=tokenizer, max_tokens=policy.z2_tokens, export_adapter=export_adapter)
            except _memory.MemoryRejected as exc:
                reason = str(exc)
                raise _t.ContractError(_t.Reason[reason] if reason in
                    {'PRIVACY', 'SCHEMA', 'BOUNDS', 'FOREIGN'} else _t.Reason.UNKNOWN) from None
            trusted = _wire({'instructions': c.trusted_instructions.decode('utf-8'),
                'output_schema': c.output_schema.decode('utf-8'),
                'capabilities': c.capabilities, 'image_order': c.image_order,
                'model': c.model_identity, 'tokenizer': c.tokenizer_identity,
                'unicode': c.unicode_identity, 'normalizer': c.normalizer_identity})
            z1 = _message('trusted', trusted)
            static = snap.static_projection_text.encode('utf-8') if c.passive_roles else b''
            _need(_count(tokenizer, _message('passive', static)) <= store.store_policy.static_meta_tokens,
                  _t.Reason.BOUNDS)
            _approve(export_adapter, static, c.scope, _t.ExportDestination.FROZEN_PREFIX,
                     ('catalogue-static',))
            z2 = (_message('passive', mem) + (_message('passive', static) if c.passive_roles else b'') +
                  _message('trusted', _ANCHOR) + b'\n')
            _need(_count(tokenizer, z1) <= policy.z1_tokens and
                  _count(tokenizer, z2) <= policy.z2_tokens, _t.Reason.BOUNDS)
            prefix = z1 + z2
            _approve(export_adapter, prefix, c.scope, _t.ExportDestination.FROZEN_PREFIX,
                     ('provider-static-envelope',))
            epoch = ContextEpoch(c, policy, display_policy, prefix,
                _t.MetadataDigest('sha256', _hash.sha256(prefix).hexdigest()), snap, pin,
                mem, host, store, tokenizer, export_adapter, resources, allocation)
            _check(resources.retain(allocation, epoch))
            _check(host.register_epoch(epoch))
            return epoch
        except Exception:
            if pin is not None:
                try:
                    store.release_snapshot_pin(pin)
                except Exception:
                    resources.recovery(allocation, _t.Reason.RECONCILIATION)
                    raise _t.ContractError(_t.Reason.RECONCILIATION) from None
            _drain(resources, allocation)
            raise


def _terms(terms):
    _need(type(terms) is tuple and all(type(t) is str for t in terms), _t.Reason.TYPE)
    result = set()
    for term in terms:
        term.encode('utf-8', 'strict')
        result.update(_unicode.normalize('NFC', _unicode.normalize('NFC', term).casefold()).split())
    return tuple(sorted(result, key=lambda t: t.encode('utf-8')))


def _card_key(card):
    _need(type(card) is ContextCard and type(card.identity) is tuple and
          len(card.identity) == 6, _t.Reason.TYPE)
    _need(type(card.identity[0]) is _t.ScopeIdentity and
          all(type(card.identity[i]) is str for i in (1, 3, 4, 5)) and
          type(card.identity[2]) is int and card.identity[2] > 0, _t.Reason.TYPE)
    scope = card.identity[0]
    scope_key = _wire((scope.principal, scope.tenant, scope.browser_profile_incarnation,
        scope.top_level_origin, scope.target_frame_origin, scope.frame_ancestry, scope.scope_revision))
    return (scope_key,) + tuple(v.encode('utf-8') if type(v) is str else v
                                for v in card.identity[1:])


def _suffix(evidence, selected, omissions):
    # Whole active fragment remains one message. Current grounding/reminder last.
    first = _wire({'deny_uncertainty': evidence.deny_uncertainty.decode('utf-8'),
                   'task_slots': evidence.task_slots.decode('utf-8')})
    middle = _wire({'active_plan_checkpoint': evidence.active_plan_checkpoint.decode('utf-8'),
        'observation_completeness': evidence.observation_completeness.decode('utf-8'),
        'latest_semantic_result': evidence.latest_semantic_result.decode('utf-8'),
        'cards': [c.payload.decode('utf-8') for c in selected], 'omissions': omissions})
    last = _wire({'candidate_grounding': evidence.candidate_grounding.decode('utf-8'),
                  'trusted_reminder': evidence.trusted_reminder.decode('utf-8')})
    return _message('passive', first) + _message('passive', middle) + _message('passive', last)


def _state(evidence, selected, omissions):
    state = {f.name: getattr(evidence, f.name).decode('utf-8')
             for f in _dc.fields(evidence) if f.name not in ('cards', 'omissions')}
    state['omissions'] = list(omissions)
    for card in selected:
        key = _card_key(card)
        identity = _wire([v.decode('utf-8') if type(v) is bytes else v for v in key]).decode('utf-8')
        state['card/' + identity] = card.payload.decode('utf-8')
    return _wire(state)


def _delta(epoch, base, state, checkpoint):
    # Public data is never retention proof. Hosts without this optional service
    # are stateless and receive the full sufficient payload.
    if type(base) is not DeltaBase or not callable(getattr(epoch.host, 'check_delta', None)):
        return None
    try:
        _check(epoch.host.check_delta(epoch, base))
        _need(base.epoch is epoch and type(base.request) is PackedContext and
              base.request.epoch is epoch and base.generation == epoch.snapshot.generation and
              base.checkpoint == base.request.checkpoint == checkpoint and bool(checkpoint) and
              base.sufficient_state == base.request.sufficient_state and
              base.digest == _t.MetadataDigest('sha256', _hash.sha256(base.sufficient_state).hexdigest()),
              _t.Reason.STALE)
        old, new = _json.loads(base.sufficient_state), _json.loads(state)
        changes = {k: v for k, v in new.items() if k not in old or old[k] != v}
        removals = sorted(set(old) - set(new))
        patch = {'changes': changes, 'removals': removals,
                 'current': _hash.sha256(state).hexdigest(), 'base': base.digest.full_digest}
        # Verify the actual encoded patch, including deletions, before sending.
        encoded = _wire(patch)
        decoded = _json.loads(encoded)
        rebuilt = dict(old)
        for key in decoded['removals']:
            del rebuilt[key]
        rebuilt.update(decoded['changes'])
        _need(_wire(rebuilt) == state, _t.Reason.RECONCILIATION)
        return _message('passive', encoded)
    except Exception:
        return None


# @anchor:wa.learn.context_router.pack_suffix
@_closed
def pack_suffix(epoch, evidence, *, request_id, resolve_request, acknowledged_base=None):
    _need(type(epoch) is ContextEpoch, _t.Reason.TYPE)
    with epoch.host.fence():
        _live(epoch)
        c = epoch.configuration
        _need(type(evidence) is SuffixEvidence and type(request_id) is str and bool(request_id), _t.Reason.TYPE)
        _need(type(resolve_request) is _resolver.ResolveRequest and
              resolve_request.scope == c.scope and resolve_request.authority_revision == c.authority,
              _t.Reason.FOREIGN)
        allocation = epoch.resources.reserve('context-pack', scope=c.scope,
            absolute_deadline_ns=c.absolute_deadline_ns, retained=(epoch,))
        try:
            for f in _dc.fields(evidence):
                if f.name not in ('cards', 'omissions'):
                    v = getattr(evidence, f.name)
                    _need(type(v) is bytes and bool(v), _t.Reason.MISSING)
            _need(type(evidence.omissions) is tuple and all(type(v) is str for v in evidence.omissions)
                  and type(evidence.cards) is tuple, _t.Reason.TYPE)
            # Display limits apply to whole mandatory projections, never slices.
            dp = epoch.display_policy
            over_display = any(len(payload) > byte_cap or _count(epoch.tokenizer, payload) > token_cap
                for payload, byte_cap, token_cap in (
                    (evidence.active_plan_checkpoint, dp.step_bytes, dp.step_tokens),
                    (evidence.latest_semantic_result, dp.step_bytes, dp.step_tokens),
                    (evidence.deny_uncertainty, dp.failure_bytes, dp.failure_tokens)))
            # Strictly preserve every mandatory projection; never trim to fit.
            mandatory = _suffix(evidence, (), evidence.omissions)
            _approve(epoch.export_adapter, mandatory, c.scope, _t.ExportDestination.MODEL_PAYLOAD,
                     ('mandatory-suffix',))
            if _count(epoch.tokenizer, mandatory) > epoch.policy.z3_tokens or over_display:
                _drain(epoch.resources, allocation)
                return ContextResult('CONTEXT_INSUFFICIENT', _t.Reason.CAPACITY)
            resolved = _resolver.resolve(epoch.store, resolve_request, catalogue_snapshot=epoch.snapshot)
            if resolved.closure == 'vacio' and (resolved.detail == 'overflow' or resolved.reason in (
                    _t.Reason.STALE, _t.Reason.FOREIGN, _t.Reason.CAPACITY)):
                raise _t.ContractError(resolved.reason)
            cards = {}
            resolver_cards = []
            if resolved.candidate is not None:
                identity = resolved.candidate.artifact
                entry = next(e for e in epoch.snapshot.entries
                             if e.identity == identity)
                payload = _wire({'logical_id': identity.logical_id,
                    'revision': identity.revision, 'artifact': identity.artifact.full_digest,
                    'metadata': entry.passive_card_uso, 'terms': entry.passive_card_fts})
                _need(_count(epoch.tokenizer, payload) <= dp.resolver_tokens, _t.Reason.CAPACITY)
                # The projection remains passive; candidate eligibility is never
                # transferred from these bytes to the model or an actuator.
                resolver_cards.append(ContextCard((c.scope, identity.logical_id, identity.revision,
                    identity.artifact.full_digest, 'resolver', 'resolved'), payload,
                    entry.normalized_keywords))
            for card in evidence.cards + tuple(resolver_cards):
                key = _card_key(card)
                _need(card.identity[0] == c.scope, _t.Reason.FOREIGN)
                _need(type(card.payload) is bytes and bool(card.payload), _t.Reason.TYPE)
                _terms(card.declared_terms)
                _need(key not in cards or cards[key] == card, _t.Reason.CONFLICT)
                cards[key] = card
            canonical = tuple(cards[k] for k in sorted(cards))
            pool = []
            recency = receipts = 0
            for card in canonical[:dp.scanned_cards]:
                kind = card.identity[4]
                if kind == 'history':
                    recency += 1
                    if recency > dp.recency_candidates:
                        continue
                if kind == 'receipt':
                    receipts += 1
                    if receipts > dp.recent_receipts:
                        continue
                if kind in ('step', 'history', 'receipt') and (
                        len(card.payload) > dp.step_bytes or _count(epoch.tokenizer, card.payload) > dp.step_tokens):
                    continue
                if kind == 'failure' and (len(card.payload) > dp.failure_bytes or
                        _count(epoch.tokenizer, card.payload) > dp.failure_tokens):
                    continue
                pool.append(card)
            selected, covered = [], set()
            query = set(_terms(resolve_request.query_terms))
            omitted = tuple(sorted(set(evidence.omissions) |
                ({'optional-cards-truncated'} if len(canonical) > len(pool) else set()) |
                ({'optional-cards-omitted'} if canonical else set())))
            current = _suffix(evidence, (), omitted)
            if _count(epoch.tokenizer, current) > epoch.policy.z3_tokens:
                _drain(epoch.resources, allocation)
                return ContextResult('CONTEXT_INSUFFICIENT', _t.Reason.CAPACITY)
            while pool and len(selected) < min(epoch.display_policy.selected_cards,
                                               epoch.display_policy.prompt_rows):
                best = None
                base_cost = _count(epoch.tokenizer, current)
                for card in pool:
                    trial = _suffix(evidence, selected + [card], omitted)
                    cost = _count(epoch.tokenizer, trial) - base_cost
                    _need(cost > 0, _t.Reason.BOUNDS)
                    gain = len((set(_terms(card.declared_terms)) & query) - covered)
                    if gain and base_cost + cost <= epoch.policy.z3_tokens:
                        if best is None or gain * best[1] > best[0] * cost:
                            best = (gain, cost, card, trial)
                if best is None:
                    break
                _, _, card, current = best
                _approve(epoch.export_adapter, card.payload, c.scope,
                         _t.ExportDestination.MODEL_PAYLOAD, ('optional-card',))
                selected.append(card)
                covered.update(_terms(card.declared_terms))
                pool.remove(card)
            if len(selected) < len(canonical):
                omitted = tuple(sorted(set(omitted) | {'optional-cards-omitted'}))
                current = _suffix(evidence, selected, omitted)
                while selected and _count(epoch.tokenizer, current) > epoch.policy.z3_tokens:
                    selected.pop()
                    current = _suffix(evidence, selected, omitted)
            _need(_count(epoch.tokenizer, current) <= epoch.policy.z3_tokens, _t.Reason.CAPACITY)
            if len(selected) == len(canonical):
                omitted = tuple(v for v in omitted if v != 'optional-cards-omitted')
                current = _suffix(evidence, selected, omitted)
            state = _state(evidence, selected, omitted)
            delta = _delta(epoch, acknowledged_base, state, evidence.active_plan_checkpoint)
            used_base = None
            if delta is not None and _count(epoch.tokenizer, delta) <= epoch.policy.z3_tokens:
                current, used_base = delta, acknowledged_base
            envelope = epoch.prefix + current
            _approve(epoch.export_adapter, envelope, c.scope, _t.ExportDestination.MODEL_PAYLOAD,
                     ('provider-complete-envelope',))
            request = _t.ContextRequest(epoch.prefix_identity, c.scope, c.authority,
                                        c.absolute_deadline_ns, c.restart_evidence)
            packed = PackedContext(epoch, request_id, request, envelope,
                tuple(card.identity for card in selected), omitted,
                _count(epoch.tokenizer, current), len(envelope), allocation, state,
                evidence.active_plan_checkpoint, used_base)
            _check(epoch.resources.retain(allocation, packed))
            _check(epoch.host.register_request(epoch, packed))
            return ContextResult('PACKED', None, packed)
        except Exception:
            _drain(epoch.resources, allocation)
            raise


# @anchor:wa.learn.context_router.validate_response
@_closed
def validate_response(epoch, request, response, *, host):
    _need(type(epoch) is ContextEpoch and host is epoch.host, _t.Reason.FOREIGN)
    with host.fence():
        _live(epoch)
        _need(type(request) is PackedContext and request.epoch is epoch, _t.Reason.FOREIGN)
        _need(request.request.scope == epoch.configuration.scope and
              request.request.current_authority == epoch.configuration.authority and
              request.request.prefix_identity == epoch.prefix_identity, _t.Reason.STALE)
        _need(host.now_ns() < request.request.absolute_deadline_ns, _t.Reason.DEADLINE)
        _check(host.check(epoch, request, response))
        _check(host.consume(epoch, request))
        return ContextResult('PROPOSAL', None)


# @anchor:wa.learn.context_router.close_epoch
@_closed
def close_epoch(epoch, *, host, reason):
    _need(type(epoch) is ContextEpoch and host is epoch.host and type(reason) is _t.Reason,
          _t.Reason.FOREIGN)
    with host.fence():
        # Owner authenticates idempotence and retains the epoch on failed drain.
        try:
            _check(host.cancel_and_drain(epoch, reason))
            _check(epoch.store.release_snapshot_pin(epoch.pin_token))
            _drain(epoch.resources, epoch.allocation)
        except Exception:
            epoch.resources.recovery(epoch.allocation, _t.Reason.RECONCILIATION)
            raise _t.ContractError(_t.Reason.RECONCILIATION) from None
