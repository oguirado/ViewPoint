# @module {"id":"wa.learn.resolver","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.learn.script_store","wa.learn.catalogue"],"book":"HB-WA-s4-learn"}
"""Exact L0 and complete distinct-term L1 matching; ambiguity always abstains."""
import dataclasses as _dc
from wender.scripts import types as _t, header as _h
from wender.learn.script_store import CandidateRef, ScriptStore, _scope_tuple
from wender.learn.catalogue import CatalogueSnapshot, _normalize_terms


# @anchor:wa.learn.resolver.ResolveRequest
@_dc.dataclass(frozen=True, slots=True)
class ResolveRequest(_t._Record):
    scope: _t.ScopeIdentity
    episode_id: str
    site_contract: str
    goal_contract: str
    signature_schema: str
    structural_screen_digest: _t.PlanDigest
    required_abis: _t.ABISet
    available_capabilities: tuple[str, ...]
    authority_revision: _t.AuthorityRevision
    query_terms: tuple[str, ...] = ()
    supplied_args: tuple[tuple[str, str | int | bool], ...] = ()
    slots: tuple[_t.SlotDescriptor, ...] = ()
    logical_id: str | None = None
    shadow_mode: bool = False


# @anchor:wa.learn.resolver.ResolveResult
@_dc.dataclass(frozen=True, slots=True)
class ResolveResult(_t._Record):
    closure: str
    candidate: CandidateRef | None
    validated_args: tuple[tuple[str, str | int | bool], ...]
    reason: _t.Reason | None
    detail: str
    snapshot_generation: int
    deny_sequence: int
    scanned_candidates: int

    def _validate(self):
        _h._need(self.closure in ('completo', 'vacio'), _t.Reason.SCHEMA)
        _h._need((self.closure == 'completo') == (self.candidate is not None), _t.Reason.MISSING)
        _h._need((self.closure == 'completo') == (self.reason is None), _t.Reason.MISSING)
        _h._need(self.closure != 'vacio' or self.validated_args == (), _t.Reason.SCHEMA)


# @anchor:wa.learn.resolver.resolve
@_h._closed
def resolve(store, request, *, catalogue_snapshot=None, resolver_policy=None,
            slot_policy=None, slot_check=None, vocabulary_check=None):
    _h._need(type(store) is ScriptStore and type(request) is ResolveRequest, _t.Reason.TYPE)
    r = request
    policy = resolver_policy if resolver_policy is not None else store.resolver_policy
    _h._need(type(policy) is _t.ResolverPolicy, _t.Reason.TYPE)
    # Caller policy may tighten, never widen owner limits.
    _h._need(policy.distinct_terms <= store.resolver_policy.distinct_terms
             and policy.candidates <= store.resolver_policy.candidates, _t.Reason.BOUNDS)
    generation = scanned = 0
    def empty(reason, detail):
        return ResolveResult('vacio', None, (), reason, detail, generation, store.deny_sequence, scanned)
    if r.scope.attestation is None or r.scope.attestation_reason is not None:
        return empty(_t.Reason.FOREIGN, 'empty_scope')
    bad_scope = False
    try:
        _h._check(store._scope_check(r.scope))
    except Exception:
        bad_scope = True
    if bad_scope:
        return empty(_t.Reason.FOREIGN, 'scope_mismatch')
    if r.authority_revision.scope != r.scope.scope_revision:
        return empty(_t.Reason.STALE, 'stale_authority')
    with store._lock:
        if store.service_stopped:
            return empty(_t.Reason.CANCELLED, 'service_stopped')
        snap = catalogue_snapshot if catalogue_snapshot is not None else store._published_snapshots.get(_scope_tuple(r.scope))
        if snap is None:
            return empty(_t.Reason.MISSING, 'no_match')
        _h._need(type(snap) is CatalogueSnapshot, _t.Reason.TYPE)
        generation = snap.generation
        if snap.scope != r.scope:
            return empty(_t.Reason.FOREIGN, 'scope_mismatch')
        invalid_snapshot = False
        try:
            store._check_obj(snap)
            store._check_obj(snap.owner_ref)
        except _t.ContractError:
            invalid_snapshot = True
        if invalid_snapshot:
            return empty(_t.Reason.FOREIGN, 'foreign_snapshot')
        if r.authority_revision != snap.authority_revision or snap.authority_revision != store.current_authority_revision(r.scope):
            return empty(_t.Reason.STALE, 'stale_authority')
        terms = _normalize_terms(r.query_terms)
        if len(terms) > policy.distinct_terms:
            return empty(_t.Reason.BOUNDS, 'overflow')
        pool = snap.shadow_entries if r.shadow_mode else snap.entries
        scanned = len(pool)
        if scanned > policy.candidates:
            return empty(_t.Reason.BOUNDS, 'overflow')
        qualifying, revoked_hit, nonresident_hit, shadow_hit = [], False, False, False
        for entry in pool:
            key = entry.key
            if not (key.scope == r.scope and key.site_contract == r.site_contract
                    and key.goal_contract == r.goal_contract and key.signature_schema == r.signature_schema
                    and key.structural_screen_digest == r.structural_screen_digest
                    and (r.logical_id is None or key.logical_id == r.logical_id)
                    and entry.abis == r.required_abis
                    and set(entry.capabilities) <= set(r.available_capabilities)):
                continue
            default = store._default_by_slot.get(key.slot_key())
            if default is not None and default != key.artifact_digest.full_digest:
                continue
            state = store._cells.get((_scope_tuple(r.scope), key.artifact_digest.full_digest))
            if state is None:
                revoked_hit = True
                continue
            c = state.cell
            if (key.artifact_digest.full_digest in store._tombstones.get(_scope_tuple(r.scope), set())
                    or c.lifecycle in (_t.Lifecycle.SUSPENDED, _t.Lifecycle.QUARANTINED, _t.Lifecycle.RETIRED)
                    or c.authority_revision != r.authority_revision
                    or str(r.authority_revision.environment_compatibility) in c.denied_environments
                    or str(r.authority_revision.environment_compatibility) in store._degraded_environments.get(_scope_tuple(r.scope), set())):
                revoked_hit = True
                continue
            if not r.shadow_mode and (entry.lifecycle is not _t.Lifecycle.PROMOTED or c.lifecycle is not _t.Lifecycle.PROMOTED):
                shadow_hit = True
                continue
            if r.shadow_mode and c.lifecycle is not _t.Lifecycle.SHADOW:
                shadow_hit = True
                continue
            if not entry.resident or not c.resident:
                nonresident_hit = True
                continue
            if not set(terms) <= set(entry.normalized_keywords):
                continue
            qualifying.append((entry, c))
        if not qualifying:
            # A shadow-only match remains prediction-only and reports why.
            if not r.shadow_mode and any(e.key.site_contract == r.site_contract
                    and e.key.goal_contract == r.goal_contract and e.key.signature_schema == r.signature_schema
                    and e.key.structural_screen_digest == r.structural_screen_digest for e in snap.shadow_entries):
                shadow_hit = True
            return empty(_t.Reason.CONFLICT if revoked_hit else _t.Reason.MISSING if nonresident_hit
                         else _t.Reason.CONFLICT if shadow_hit else _t.Reason.MISSING,
                         'revoked' if revoked_hit else 'needs_load' if nonresident_hit else
                         'shadow_ineligible' if shadow_hit else 'no_match')
        if len(qualifying) > 1:
            return empty(_t.Reason.CONFLICT, 'ambiguous_tie')
        winner, cell = qualifying[0]
        before_deny = store.deny_sequence
        invalid_args = False
        try:
            _h._need(len({k for k, _ in r.supplied_args}) == len(r.supplied_args), _t.Reason.SCHEMA)
            validated = _h.validate_arguments(winner.args_schema, dict(r.supplied_args), scope=r.scope,
                episode_id=r.episode_id, slots=r.slots,
                slot_check=slot_check if slot_check is not None else store.slot_check,
                vocabulary_check=vocabulary_check if vocabulary_check is not None else store.vocabulary_check,
                slot_policy=slot_policy if slot_policy is not None else store.slot_policy)
        except Exception:
            invalid_args = True
        if invalid_args:
            return empty(_t.Reason.SCHEMA, 'invalid_args')
        # Trusted validators can re-enter; never mint after an intervening deny.
        if store._cells[(_scope_tuple(r.scope), winner.key.artifact_digest.full_digest)].cell is not cell:
            return empty(_t.Reason.STALE, 'revoked')
        if store.deny_sequence != before_deny or store.service_stopped or cell.authority_revision != store.current_authority_revision(r.scope):
            return empty(_t.Reason.STALE, 'stale_authority')
        ref = store._mint_ref('candidate')
        candidate = CandidateRef(cell.identity, cell.key, r.scope, cell.lifecycle,
            cell.lifecycle_revision, cell.authority_revision, snap.generation, store.deny_sequence, ref)
        store._register_obj(candidate)
        return ResolveResult('completo', candidate, validated, None, 'unique_match', generation, store.deny_sequence, scanned)
