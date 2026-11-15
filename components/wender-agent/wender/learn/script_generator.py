# @module {"id":"wa.learn.script_generator","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.scripts.compiler","wa.learn.context_router","wa.learn.dream_replay"],"book":"HB-WA-s4-learn"}
"""Verified prospective support to inert source; generated source never runs."""
import ast as _ast
import dataclasses as _dc
import json as _json
from typing import Protocol as _Protocol
from wender.scripts import types as _t, header as _header, compiler as _compiler
from wender.learn import dream_replay as _replay, context_router as _router

_need, _check, _closed = _header._need, _header._check, _header._closed


# @anchor:wa.learn.script_generator.ProvenanceOwner
class ProvenanceOwner(_Protocol):
    """Prospectively retains ledger/sidecar/observation/slot associations.

    loader_inputs supplies exactly the complete original trace, never a filtered
    reconstruction. check authenticates that trace and historical sidecars with
    original indices and same-observation image/tag associations. project emits
    only structural selectors/symbolic slots and approved pre/action/post data.
    register/check_brief and register/check_draft authenticate identity custody.
    Host-owned receipts stay local. No generated object constructs authority.
    """
    # @anchor:wa.learn.script_generator.ProvenanceOwner.loader_inputs
    def loader_inputs(self, ledger_owner: object, seal: object, sidecars: object) -> dict: ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.check
    def check(self, episode: _replay.VerifiedEpisode, sidecars: object,
              start_index: int, stop_index: int) -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.project
    def project(self, episode: _replay.VerifiedEpisode, start_index: int,
                stop_index: int) -> bytes: ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.register_brief
    def register_brief(self, brief: 'GenerationBrief') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.check_brief
    def check_brief(self, brief: 'GenerationBrief') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.approve_lineage
    def approve_lineage(self, brief: 'GenerationBrief', proposal: tuple) -> tuple:
        """Return approved (ArtifactDigest | None, tuple[ArtifactDigest, ...])."""
        ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.register_draft
    def register_draft(self, draft: 'SourceDraft') -> _t.AdmissionCheck: ...
    # @anchor:wa.learn.script_generator.ProvenanceOwner.check_draft
    def check_draft(self, draft: 'SourceDraft') -> _t.AdmissionCheck: ...


# @anchor:wa.learn.script_generator.GenerationTransport
class GenerationTransport(_Protocol):
    """Owner-associated bounded transport; no legacy subprocess fallback.

    exchange enforces GenerationPolicy limits while streaming, absolute deadline
    and cancellation/drain before returning. It sends only these approved bytes
    as passive evidence, with this separate source schema. Responses are bound
    to the actual brief/job by the host, not echoed model association fields.
    """
    # @anchor:wa.learn.script_generator.GenerationTransport.exchange
    def exchange(self, payload: bytes, *, schema: bytes, association: 'GenerationBrief',
                 policy: 'GenerationPolicy', host: object) -> bytes: ...


# @anchor:wa.learn.script_generator.GenerationHost
class GenerationHost(_Protocol):
    # @anchor:wa.learn.script_generator.GenerationHost.check
    def check(self, brief: 'GenerationBrief', policy: 'GenerationPolicy',
              response: bytes | None) -> _t.AdmissionCheck:
        """Serialize eligibility, authority, association and cancellation checks."""
        ...


# @anchor:wa.learn.script_generator.GenerationPolicy
@_dc.dataclass(frozen=True, slots=True, repr=False)
class GenerationPolicy:
    source: _t.SourcePolicy
    request_bytes: int
    response_bytes: int
    absolute_deadline_ns: int
    forbidden_substrings: tuple[str, ...]


# @anchor:wa.learn.script_generator.GenerationBrief
@_dc.dataclass(frozen=True, slots=True, eq=False, repr=False)
class GenerationBrief:
    episode: _replay.VerifiedEpisode
    start_index: int
    stop_index: int
    projection: bytes
    provenance_owner: object
    export_adapter: object
    resources: object
    allocation: object
    absolute_deadline_ns: int
    structural_selectors: tuple[str, ...]
    symbolic_slots: tuple[str, ...]
    ordered_evidence: tuple
    approved_images: tuple = ()
    missingness: tuple[str, ...] = ()
    predecessor_proposal: tuple | None = None


# @anchor:wa.learn.script_generator.SourceDraft
@_dc.dataclass(frozen=True, slots=True, eq=False, repr=False)
class SourceDraft:
    source: bytes
    brief: GenerationBrief
    allocation: object
    predecessor_proposal: tuple | None = None


# @anchor:wa.learn.script_generator.GenerationResult
@_dc.dataclass(frozen=True, slots=True)
class GenerationResult:
    disposition: str
    reason: _t.Reason | None
    value: object = _dc.field(default=None, repr=False)
    usage: tuple = ()
    usage_unavailable_reason: _t.Reason | None = _t.Reason.MISSING


def _reserve(resources, operation, scope, deadline, retained):
    _need(type(deadline) is int and deadline > 0, _t.Reason.DEADLINE)
    return resources.reserve(operation, scope=scope,
                             absolute_deadline_ns=deadline, retained=retained)


# @anchor:wa.learn.script_generator.extract_segment
@_closed
def extract_segment(*, ledger_owner, seal, abis, sidecars,
                    start_index, stop_index, replay_policy,
                    provenance_owner, export_adapter, resources):
    _need(type(start_index) is int and type(stop_index) is int and
          0 <= start_index < stop_index, _t.Reason.BOUNDS)
    allocation = _reserve(resources, 'generation-extract', seal.scope,
                           resources.absolute_deadline_ns, (ledger_owner, sidecars))
    try:
        loader = provenance_owner.loader_inputs(ledger_owner, seal, sidecars)
        _need(type(loader) is dict and set(loader) == {'entry_observation', 'entry_evidence',
              'transitions', 'terminal_observation', 'terminal_evidence'}, _t.Reason.SCHEMA)
        episode = _replay.load_verified(ledger_owner, seal=seal, abis=abis,
                                        replay_policy=replay_policy, **loader)
        _need(stop_index <= len(episode.transitions), _t.Reason.BOUNDS)
        selected = episode.transitions[start_index:stop_index]
        _need(all(t.step_index == start_index + i and t.privacy_complete and not t.safety_vetoes
                  for i, t in enumerate(selected)), _t.Reason.PRIVACY)
        for tr in selected:
            if tr.node.effect is not None:
                _need(tr.event is not None and tr.event.state is _t.EventState.VERIFIED and
                      tr.event.disposition is _t.InputDisposition.APPLIED and
                      tr.event.verification is _t.Truth.TRUE, _t.Reason.CONFLICT)
        _check(provenance_owner.check(episode, sidecars, start_index, stop_index))
        projection = provenance_owner.project(episode, start_index, stop_index)
        _need(type(projection) is bytes and bool(projection), _t.Reason.TYPE)
        _router._approve(export_adapter, projection, episode.scope,
            _t.ExportDestination.GENERATION_BRIEF, ('verified-contiguous-structural-segment',))
        structural = _json.loads(projection)
        _need(type(structural) is dict and type(structural.get('structural_selector')) is str and
              bool(structural['structural_selector']) and type(structural.get('symbolic_slot')) is str and
              bool(structural['symbolic_slot']), _t.Reason.MISSING)
        # Sensitive observation/evidence handles stay host-local; only the
        # separately approved projection crosses the transport boundary.
        brief = GenerationBrief(episode, start_index, stop_index, projection,
            provenance_owner, export_adapter, resources, allocation, resources.absolute_deadline_ns,
            (structural['structural_selector'],), (structural['symbolic_slot'],),
            tuple((tr.pre_observation, tr.node, tr.post_observation) for tr in selected))
        _check(resources.retain(allocation, brief))
        _check(provenance_owner.register_brief(brief))
        return brief
    except Exception:
        _router._drain(resources, allocation)
        raise


def _unique(pairs):
    out = {}
    for k, v in pairs:
        _need(k not in out, _t.Reason.SCHEMA)
        out[k] = v
    return out


# @anchor:wa.learn.script_generator.generate_draft
@_closed
def generate_draft(brief, *, transport, policy, host):
    _need(type(brief) is GenerationBrief and type(policy) is GenerationPolicy and
          type(policy.source) is _t.SourcePolicy, _t.Reason.TYPE)
    _need(all(type(v) is int and v > 0 for v in (policy.request_bytes,
          policy.response_bytes, policy.absolute_deadline_ns)), _t.Reason.BOUNDS)
    _need(type(policy.forbidden_substrings) is tuple and
          all(type(v) is str and bool(v) for v in policy.forbidden_substrings), _t.Reason.TYPE)
    _check(brief.provenance_owner.check_brief(brief))
    _check(host.check(brief, policy, None))
    allocation = _reserve(brief.resources, 'generation-transport', brief.episode.scope,
                           policy.absolute_deadline_ns, (brief,))
    try:
        schema = b'{"type":"object","required":["source"],"additionalProperties":false,"properties":{"source":{"type":"string"},"predecessor_proposal":{"type":"object","required":["predecessor_digest","negative_lineage"],"additionalProperties":false,"properties":{"predecessor_digest":{"type":["string","null"]},"negative_lineage":{"type":"array","items":{"type":"string"}}}}}}'
        _need(len(brief.projection) + len(schema) <= policy.request_bytes, _t.Reason.BOUNDS)
        _router._approve(brief.export_adapter, brief.projection, brief.episode.scope,
                         _t.ExportDestination.MODEL_PAYLOAD, ('generation-model-payload',))
        response = transport.exchange(brief.projection, schema=schema, association=brief,
                                      policy=policy, host=host)
        _need(type(response) is bytes and len(response) <= policy.response_bytes, _t.Reason.BOUNDS)
        _check(host.check(brief, policy, response))
        result = _json.loads(response.decode('utf-8', 'strict'), object_pairs_hook=_unique)
        _need(type(result) is dict and {'source'} <= set(result) <= {'source', 'predecessor_proposal'} and
              type(result['source']) is str, _t.Reason.SCHEMA)
        proposal = brief.predecessor_proposal
        if 'predecessor_proposal' in result:
            value = result['predecessor_proposal']
            _need(type(value) is dict and set(value) == {'predecessor_digest', 'negative_lineage'} and
                  (value['predecessor_digest'] is None or type(value['predecessor_digest']) is str) and
                  type(value['negative_lineage']) is list and
                  all(type(v) is str for v in value['negative_lineage']), _t.Reason.SCHEMA)
            proposal = (value['predecessor_digest'], tuple(value['negative_lineage']))
        text = result['source']
        source = text.encode('utf-8', 'strict')
        _need(len(text) <= policy.source.unicode_scalars and len(source) <= policy.source.utf8_bytes,
              _t.Reason.BOUNDS)
        header_lines = 0
        for line in text.replace('\r\n', '\n').replace('\r', '\n').splitlines():
            if not line.startswith('# ['):
                break
            header_lines += 1
        _need(header_lines <= policy.source.header_lines, _t.Reason.BOUNDS)
        _need(not any(v.casefold() in text.casefold() for v in policy.forbidden_substrings),
              _t.Reason.PRIVACY)
        try:
            tree = _ast.parse(text)
        except (SyntaxError, ValueError):
            raise _t.ContractError(_t.Reason.SCHEMA) from None
        _need(not any(isinstance(node, (_ast.Import, _ast.ImportFrom)) or
              (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and
               node.func.id in {'exec', 'eval', 'open', '__import__'})
              for node in _ast.walk(tree)), _t.Reason.SCHEMA)
        _router._approve(brief.export_adapter, source, brief.episode.scope,
                         _t.ExportDestination.REUSABLE_ARTIFACT, ('generated-complete-source',))
        draft = SourceDraft(source, brief, allocation, proposal)
        _check(brief.resources.retain(allocation, draft))
        _check(brief.provenance_owner.register_draft(draft))
        return GenerationResult('SOURCE_PROPOSAL', None, draft)
    except Exception:
        _router._drain(brief.resources, allocation)
        raise


# @anchor:wa.learn.script_generator.submit_draft
@_closed
def submit_draft(draft, *, admission, inputs, registry,
                 store, scope, authority_revision, screen_check,
                 export_adapter, resources):
    _need(type(draft) is SourceDraft and type(draft.source) is bytes and
          type(draft.brief) is GenerationBrief, _t.Reason.TYPE)
    _check(draft.brief.provenance_owner.check_draft(draft))
    _need(scope == draft.brief.episode.scope and authority_revision == draft.brief.episode.authority,
          _t.Reason.FOREIGN)
    allocation = _reserve(resources, 'generation-two-parser-passes', scope,
                           draft.brief.absolute_deadline_ns, (draft,))
    plan, session, transferred = None, None, False
    try:
        _router._approve(export_adapter, draft.source, scope,
            _t.ExportDestination.REUSABLE_ARTIFACT, ('source-before-two-parser-passes',))
        predecessor, negative = None, ()
        if draft.predecessor_proposal is not None:
            _need(callable(getattr(draft.brief.provenance_owner, 'approve_lineage', None)), _t.Reason.MISSING)
            predecessor, negative = draft.brief.provenance_owner.approve_lineage(
                draft.brief, draft.predecessor_proposal)
            _need((predecessor is None or type(predecessor) is _t.ArtifactDigest) and
                  type(negative) is tuple and all(type(v) is _t.ArtifactDigest for v in negative),
                  _t.Reason.TYPE)
        plan = _compiler.compile_draft(draft.source, admission=admission, inputs=inputs, registry=registry)
        session = _header.admit_source(draft.source, admission=admission)
        result = store.admit_artifact(plan, scope=scope, authority_revision=authority_revision,
            source_bytes=draft.source, initial_lifecycle=_t.Lifecycle.DRAFT,
            predecessor_digest=predecessor, negative_lineage=negative, certificate=None,
            session=session, admission=admission, inputs=inputs, screen_check=screen_check)
        transferred = True
        return GenerationResult('DRAFT', None, result)
    finally:
        # Failed cleanup is recovery, never an uncharged success. The store
        # retains the compiler registration on successful lifetime transfer.
        try:
            if session is not None:
                _check(admission.finish(session))
            if plan is not None and not transferred:
                _check(registry.release(plan))
            _router._drain(resources, allocation)
        except Exception:
            resources.recovery(allocation, _t.Reason.RECONCILIATION)
            raise _t.ContractError(_t.Reason.RECONCILIATION) from None
