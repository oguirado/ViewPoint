# @module {"id":"wa.learn.catalogue","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.learn.script_store"],"book":"HB-WA-s4-learn"}
"""Immutable, probed catalogue generations. Passive metadata never enters Z2."""
import dataclasses as _dc
import html as _html
import re as _re
import unicodedata as _unicode
from wender.scripts import types as _t, header as _h
from wender.learn.script_store import ArtifactKey, ScriptStore, _canonical_json_bytes, _domain_digest, _scope_tuple

_need, _check, _closed = _h._need, _h._check, _h._closed
_ROLE = _re.compile(r'\b(SYSTEM|DEVELOPER|USER|ASSISTANT|TOOL)\s*:', _re.I)
_ID = _re.compile(r'[A-Za-z0-9][A-Za-z0-9_.\-/]*\Z')


def _normalize_terms(raw_terms):
    _need(type(raw_terms) is tuple and all(type(t) is str for t in raw_terms), _t.Reason.TYPE)
    terms = set()
    for text in raw_terms:
        text.encode('utf-8', 'strict')
        _need('\x00' not in text, _t.Reason.TYPE)
        text = _unicode.normalize('NFC', _unicode.normalize('NFC', text).casefold())
        for term in text.split():
            while term and _unicode.category(term[0]).startswith('P'):
                term = term[1:]
            while term and _unicode.category(term[-1]).startswith('P'):
                term = term[:-1]
            if term:
                terms.add(term)
    return tuple(sorted(terms, key=lambda s: s.encode('utf-8')))


def _passive(text):
    if text is None:
        return None
    clean = ''.join(c for c in text if not _unicode.category(c).startswith('C'))
    return _ROLE.sub(lambda m: m.group(1) + '&#58;', _html.escape(clean, quote=True)) or None


# @anchor:wa.learn.catalogue.CatalogueEntry
@_dc.dataclass(frozen=True, slots=True)
class CatalogueEntry(_t._Record):
    key: ArtifactKey
    identity: _t.ArtifactIdentity
    lifecycle: _t.Lifecycle
    lifecycle_revision: int
    authority_revision: _t.AuthorityRevision
    resident: bool
    abis: _t.ABISet
    capabilities: tuple[str, ...]
    args_schema: tuple[_t.ArgSpec, ...]
    normalized_keywords: tuple[str, ...]
    passive_card_fts: tuple[str, ...]
    passive_card_uso: str | None
    static_projection_line: str
    export_receipt: _t.ExportReceipt | None = _dc.field(default=None, repr=False)

    def _validate(self):
        _need(self.key.scope == self.identity.scope
              and self.authority_revision.scope == self.key.scope.scope_revision, _t.Reason.STALE)
        _need(self.normalized_keywords == tuple(sorted(set(self.normalized_keywords), key=lambda s: s.encode('utf-8'))), _t.Reason.SCHEMA)
        _need(not _ROLE.search(self.static_projection_line), _t.Reason.PRIVACY)
        _need(self.passive_card_uso is None or self.passive_card_uso not in self.static_projection_line, _t.Reason.PRIVACY)


# @anchor:wa.learn.catalogue.CatalogueProbe
@_dc.dataclass(frozen=True, slots=True)
class CatalogueProbe(_t._Record):
    probe_id: str
    kind: str
    site_contract: str
    goal_contract: str
    signature_schema: str
    structural_screen_digest: _t.PlanDigest
    query_terms: tuple[str, ...] = ()
    expected_digest: _t.ArtifactDigest | None = None
    supplied_args: tuple[tuple[str, str | int | bool], ...] = ()
    slots: tuple[_t.SlotDescriptor, ...] = ()
    episode_id: str = 'probe-episode'

    def _validate(self):
        _need(self.kind in ('positive', 'abstain', 'tie'), _t.Reason.SCHEMA)
        _need((self.kind == 'positive') == (self.expected_digest is not None), _t.Reason.MISSING)


# @anchor:wa.learn.catalogue.CatalogueSnapshot
@_dc.dataclass(frozen=True, slots=True, weakref_slot=True)
class CatalogueSnapshot(_t._Record):
    scope: _t.ScopeIdentity
    generation: int
    store_lifecycle_revision: int
    deny_sequence: int
    authority_revision: _t.AuthorityRevision
    entries: tuple[CatalogueEntry, ...]
    shadow_entries: tuple[CatalogueEntry, ...]
    keyword_index: tuple[tuple[str, tuple[str, ...]], ...]
    static_projection_text: str
    static_meta_tokens: int
    byte_size: int
    snapshot_digest: _t.MetadataDigest
    probe_ids_passed: tuple[str, ...]
    owner_ref: _t.RegistryRef = _dc.field(repr=False)

    def _validate(self):
        _need(self.generation >= 1 and self.byte_size >= 1, _t.Reason.BOUNDS)
        _need(bool(self.probe_ids_passed), _t.Reason.MISSING)
        _need(self.authority_revision.scope == self.scope.scope_revision, _t.Reason.STALE)


def _receipt(port, receipt, scope, destination, revision, identity):
    failure = False
    try:
        _need(port is not None and type(receipt) is _t.ExportReceipt and receipt.scope == scope
              and receipt.content_identity == identity, _t.Reason.PRIVACY)
        _check(port.check(receipt, receipt.source_observation, receipt.transform_chain,
                          receipt.content_identity, destination, _t.Purpose.QUERY, revision))
    except Exception:
        failure = True
    _need(not failure, _t.Reason.PRIVACY)


def _render(cell, metadata=None):
    key, h = cell.key, cell.plan.definition.header if cell.plan is not None else None
    _need(h is not None or metadata is not None, _t.Reason.MISSING)
    capabilities, arguments = (h.capabilities, h.arguments) if h is not None else metadata
    names = (key.logical_id, key.site_contract, key.goal_contract, key.signature_schema,
             *(_dc.astuple(cell.identity.abis)), *(a.name for a in arguments))
    _need(all(_ID.fullmatch(n) for n in names), _t.Reason.PRIVACY)
    summary = ','.join(f'{a.name}:{a.kind.value}:{a.purpose.value}' for a in arguments)
    line = f'{key.logical_id}@r{key.revision}:{key.site_contract}/{key.goal_contract}/{key.signature_schema}[{cell.identity.abis.dsl}]({summary})'
    return line, capabilities, arguments


# @anchor:wa.learn.catalogue.build_snapshot
@_closed
def build_snapshot(store, *, scope, authority_revision, probes, tokenizer,
                   export_receipt_port=None, export_receipts=None, privacy_policy_revision=1,
                   secret_canaries=(), generation=None):
    _need(type(store) is ScriptStore, _t.Reason.TYPE)
    _need(callable(tokenizer), _t.Reason.MISSING)
    _need(type(probes) in (tuple, list) and bool(probes), _t.Reason.MISSING)
    _need(all(type(p) is CatalogueProbe for p in probes), _t.Reason.TYPE)
    _need(len({p.probe_id for p in probes}) == len(probes), _t.Reason.CONFLICT)
    _need(type(secret_canaries) is tuple and all(type(c) is str and c for c in secret_canaries), _t.Reason.TYPE)
    with store._lock:
        store._live()
        sid = store._scope(scope)
        _need(authority_revision == store.current_authority_revision(scope), _t.Reason.STALE)
        _check(store._attempts.charge(scope, store._owner, 'catalogue-build', store._resource_policy, store._now()))
        revision, deny = store.store_lifecycle_revision, store.deny_sequence
        old = store._published_snapshots.get(sid)
        gen = (old.generation + 1 if old is not None else 1) if generation is None else generation
        _need(type(gen) is int and gen > (old.generation if old else 0), _t.Reason.STALE)
        _need(len(store._snapshot_bindings) < store._max_evidence_records, _t.Reason.CAPACITY)
        # Reserve staging peak before projecting/copying metadata. Cells retain
        # bodies independently; building metadata never pins a program body.
        staging = store._reserve_snapshot_bytes(scope, store.store_policy.catalogue_bytes)
        cells = tuple((s.cell, s.metadata) for (s_id, _), s in store._cells.items() if s_id == sid)
    snap = None
    try:
        port = export_receipt_port or store._export_receipt_port
        entries, shadows = [], []
        receipts = export_receipts or {}
        for cell, metadata in sorted(cells, key=lambda item: item[0].key.canonical_sort_key()):
            if cell.lifecycle not in (_t.Lifecycle.PROMOTED, _t.Lifecycle.SHADOW):
                continue
            values = (cell.uso_text or '', cell.cli_text or '', *cell.fts_terms,
                      cell.key.logical_id, cell.key.site_contract, cell.key.goal_contract, cell.key.signature_schema)
            _need(not any(canary in text for canary in secret_canaries for text in values), _t.Reason.PRIVACY)
            approved = receipts.get(cell.key.artifact_digest.full_digest)
            # A distinct receipt is needed for each export destination. A pair
            # binds FROZEN_PREFIX and CATALOGUE_CARD respectively.
            prefix, card = approved if type(approved) is tuple and len(approved) == 2 else (approved, None)
            _receipt(port, prefix, scope, _t.ExportDestination.FROZEN_PREFIX, privacy_policy_revision, cell.key.artifact_digest.full_digest)
            if cell.uso_text is not None or cell.fts_terms or cell.cli_text is not None:
                _receipt(port, card, scope, _t.ExportDestination.CATALOGUE_CARD, privacy_policy_revision, cell.key.artifact_digest.full_digest)
            line, capabilities, args = _render(cell, metadata)
            _need(not any(canary in line for canary in secret_canaries), _t.Reason.PRIVACY)
            entry = CatalogueEntry(cell.key, cell.identity, cell.lifecycle, cell.lifecycle_revision,
                cell.authority_revision, cell.resident, cell.identity.abis, capabilities, args,
                _normalize_terms(cell.fts_terms), tuple(_passive(t) for t in cell.fts_terms if _passive(t)),
                _passive(cell.uso_text), line, prefix)
            (shadows if cell.lifecycle is _t.Lifecycle.SHADOW else entries).append(entry)
        entries, shadows = tuple(entries), tuple(shadows)
        kinds = {p.kind for p in probes}
        _need(('positive' in kinds and bool(kinds & {'abstain', 'tie'})) if
              any(e.resident for e in entries) else 'abstain' in kinds, _t.Reason.MISSING)
        text = '\n'.join(e.static_projection_line for e in entries) or 'catalogue:empty'
        tokens = tokenizer(text)
        _need(type(tokens) is int and tokens >= 0, _t.Reason.TYPE)
        _need(tokens <= store.store_policy.static_meta_tokens, _t.Reason.CAPACITY)
        index = {}
        for entry in entries:
            for term in entry.normalized_keywords:
                index.setdefault(term, []).append(entry.key.artifact_digest.full_digest)
        index = tuple((k, tuple(sorted(v))) for k, v in sorted(index.items(), key=lambda kv: kv[0].encode('utf-8')))
        # Receipts and owner handles are authority, not semantic payload.
        payload = (scope, authority_revision, tuple(
            (e.key, e.identity, e.lifecycle, e.lifecycle_revision, e.resident, e.abis, e.capabilities,
             e.args_schema, e.normalized_keywords, e.passive_card_fts, e.passive_card_uso,
             e.static_projection_line) for e in entries + shadows), index, text)
        raw = _canonical_json_bytes(payload)
        _need(len(raw) <= store.store_policy.catalogue_bytes, _t.Reason.CAPACITY)
        with store._lock:
            owner = store._mint_ref('catalogue_snapshot')
        snap = CatalogueSnapshot(scope, gen, revision, deny, authority_revision, entries, shadows,
            index, text or 'catalogue:empty', tokens, len(raw),
            _domain_digest(_t.MetadataDigest, 'wender-metadata/v1', raw),
            tuple(sorted(p.probe_id for p in probes)), owner)
        # Internal staging authorization is removed on any probe failure.
        with store._lock:
            store._register_obj(snap)
        from wender.learn.resolver import ResolveRequest, resolve
        for probe in probes:
            result = resolve(store, ResolveRequest(scope, probe.episode_id, probe.site_contract,
                probe.goal_contract, probe.signature_schema, probe.structural_screen_digest,
                _probe_abis(entries, shadows, probe), _probe_capabilities(entries, shadows),
                authority_revision, probe.query_terms, probe.supplied_args, probe.slots), catalogue_snapshot=snap)
            _need((result.closure == 'completo' and result.candidate.key.artifact_digest == probe.expected_digest)
                  if probe.kind == 'positive' else result.closure == 'vacio', _t.Reason.CONFLICT)
        with store._lock:
            _need(revision == store.store_lifecycle_revision and deny == store.deny_sequence,
                  _t.Reason.STALE)
            store._snapshot_bindings[id(snap)] = (revision, deny)
        return snap
    finally:
        with store._lock:
            if snap is not None and id(snap) not in store._snapshot_bindings:
                store._unregister(snap)
                store._unregister(snap.owner_ref)
            store._drain(*staging)


def _probe_abis(entries, shadows, probe):
    pool = entries + shadows
    for entry in pool:
        if entry.key.artifact_digest == probe.expected_digest:
            return entry.abis
    return pool[0].abis if pool else _h._ABIS


def _probe_capabilities(entries, shadows):
    return tuple(sorted({c for e in entries + shadows for c in e.capabilities}))


# @anchor:wa.learn.catalogue.publish
@_closed
def publish(store, candidate_snapshot, *, expected_generation=None, expected_store_revision=None, fault_hook=None):
    with store._lock:
        store._live()
        _need(type(candidate_snapshot) is CatalogueSnapshot, _t.Reason.TYPE)
        store._check_obj(candidate_snapshot)
        snap = candidate_snapshot
        sid = store._scope(snap.scope)
        _need(bool(snap.probe_ids_passed), _t.Reason.MISSING)
        _need(snap.authority_revision == store.current_authority_revision(snap.scope), _t.Reason.STALE)
        _need(store._snapshot_bindings.get(id(snap)) == (store.store_lifecycle_revision, store.deny_sequence), _t.Reason.STALE)
        previous = store._published_snapshots.get(sid)
        old_gen = previous.generation if previous is not None else 0
        _need(expected_generation is None or expected_generation == old_gen, _t.Reason.STALE)
        _need(expected_store_revision is None or expected_store_revision == store.store_lifecycle_revision, _t.Reason.STALE)
        _need(snap.generation > old_gen, _t.Reason.STALE)
        key = (sid, snap.generation)
        _need(store.pinned_generation_count(snap.scope) < store.store_policy.pinned_generations
              or store._snapshot_pins.get(key, 0) > 0, _t.Reason.CAPACITY)
        total = snap.byte_size + sum(s.byte_size for k, (s, _, _) in store._snapshot_allocations.items()
                                     if store._snapshot_pins.get(k, 0) > 0)
        _need(total <= store.store_policy.catalogue_bytes, _t.Reason.CAPACITY)
        grant, holder = store._reserve_snapshot_bytes(snap.scope, snap.byte_size)
        try:
            if fault_hook is not None:
                fault_hook('before_pointer_swap')
        except BaseException:
            store._drain(grant, holder)
            raise
        # A hook may re-enter the owner; commit must recheck its witnesses.
        if (store.store_lifecycle_revision != snap.store_lifecycle_revision or store.deny_sequence != snap.deny_sequence
                or store._published_snapshots.get(sid) is not previous):
            store._drain(grant, holder)
            raise _t.ContractError(_t.Reason.STALE)
        old_key = (sid, previous.generation) if previous is not None else None
        if old_key is not None and not store._snapshot_pins.get(old_key, 0):
            allocation = store._snapshot_allocations.get(old_key)
            if allocation is not None:
                try:
                    store._drain(allocation[1], allocation[2])
                except BaseException:
                    store._drain(grant, holder)
                    raise
                del store._snapshot_allocations[old_key]
        store._published_snapshots[sid] = snap
        store._snapshot_bindings.pop(id(snap), None)
        store._snapshot_allocations[key] = (snap, grant, holder)
        if not hasattr(store, '_catalogue_manifests'):
            store._catalogue_manifests = {}
        for entry in snap.entries + snap.shadow_entries:
            store._catalogue_manifests[(sid, entry.key.artifact_digest.full_digest)] = entry
        if previous is not None:
            store._retire_snapshot((sid, previous.generation))
        return snap


# @anchor:wa.learn.catalogue.snapshot
@_closed
def snapshot(store, scope, *, pin=False):
    with store._lock:
        sid = store._scope(scope)
        _need(type(pin) is bool, _t.Reason.TYPE)
        snap = store._published_snapshots.get(sid)
        _need(snap is not None, _t.Reason.MISSING)
        _need(snap.authority_revision == store.current_authority_revision(scope), _t.Reason.STALE)
        if not pin:
            return snap
        key = (sid, snap.generation)
        _need(store._snapshot_pins.get(key, 0) > 0 or
              store.pinned_generation_count(scope) < store.store_policy.pinned_generations, _t.Reason.CAPACITY)
        token = store._mint_ref('snapshot_pin')
        store._snapshot_pins[key] = store._snapshot_pins.get(key, 0) + 1
        store._snapshot_pin_tokens[id(token)] = key
        return snap, token
