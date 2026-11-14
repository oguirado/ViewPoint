# @module {"id":"wa.learn.dream_replay","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.scripts.predicates","wa.learn.script_store","wa.learn.script_ledger","wa.learn.dream_journal"],"book":"HB-WA-s4-learn"}
"""Host-owned supported replay and fail-closed lifecycle contracts."""
import dataclasses as _dc
from wender.scripts import types as _t, header as _h
from wender.learn import script_store as _ss
import hashlib as _hashlib
import threading as _threading
import weakref as _weakref
from wender.scripts import predicates as _p
from wender.learn import script_ledger as _sl, dream_journal as _dj

_need, _check, _closed = _h._need, _h._check, _h._closed


# @anchor:wa.learn.dream_replay.SupportedTransition
@_dc.dataclass(frozen=True, slots=True)
class SupportedTransition(_t._Record):
    step_index: int
    node: _t.StepPlan
    pre_observation: _t.ObservationRef
    post_observation: _t.ObservationRef
    history_digest: str
    next_history_digest: str
    pre_evidence: tuple[_p._PredicateEvidence, ...] = _dc.field(repr=False)
    post_evidence: tuple[_p._PredicateEvidence, ...] = _dc.field(repr=False)
    safety_vetoes: tuple[_t.PredicateResult, ...] = ()
    privacy_complete: bool = True
    event: _sl.RunEvent | None = _dc.field(default=None, repr=False)

    def _validate(self):
        _need(all(len(d) == 64 and all(c in '0123456789abcdef' for c in d) for d in (self.history_digest, self.next_history_digest)))
        _need(self.pre_observation.scope == self.post_observation.scope, _t.Reason.STALE)
        _need(all(type(e) is _p._PredicateEvidence for e in self.pre_evidence + self.post_evidence), _t.Reason.TYPE)
        if self.node.effect is not None:
            _need(self.event is not None and self.event.state is _t.EventState.VERIFIED and self.event.disposition is _t.InputDisposition.APPLIED and self.event.verification is _t.Truth.TRUE and self.event.exit_code == 0, _t.Reason.MISSING)


# @anchor:wa.learn.dream_replay.VerifiedEpisode
@_dc.dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedEpisode(_t._Record):
    episode_id: str
    run_id: str
    scope: _t.ScopeIdentity
    authority: _t.AuthorityRevision
    abis: _t.ABISet
    seal: _sl.EpisodeSeal
    events: tuple[_sl.RunEvent, ...]
    entry_observation: _t.ObservationRef
    entry_evidence: tuple[_p._PredicateEvidence, ...] = _dc.field(repr=False)
    transitions: tuple[SupportedTransition, ...]
    terminal_goal: _t.TerminalGoalRef
    terminal_observation: _t.ObservationRef
    terminal_evidence: tuple[_p._PredicateEvidence, ...] = _dc.field(repr=False)
    support_identity: _t.PrivateRef = _dc.field(repr=False)
    owner_ref: _t.RegistryRef = _dc.field(repr=False)

    def _validate(self):
        _need(self.scope == self.seal.scope == self.entry_observation.scope == self.terminal_observation.scope == self.terminal_goal.scope, _t.Reason.STALE)
        _need(self.authority == self.seal.authority and self.authority.scope == self.scope.scope_revision, _t.Reason.STALE)
        _need(self.episode_id == self.seal.episode_id and self.run_id == self.seal.run_id and self.support_identity == self.seal.support_identity, _t.Reason.FOREIGN)
        _need(bool(self.events) and self.seal.complete and bool(self.transitions), _t.Reason.MISSING)
        _need(all(e.state is _t.EventState.VERIFIED and e.disposition is _t.InputDisposition.APPLIED and e.verification is _t.Truth.TRUE for e in self.events), _t.Reason.CONFLICT)
        _need(self.events[-1].exit_code == 0 and all(e.exit_code is not None and e.score_q16 is not None for e in self.events), _t.Reason.MISSING)
        _chain(self.transitions, self.scope, self.entry_observation, self.terminal_observation)
        _need(all(t.event is None or any(t.event is e for e in self.events) for t in self.transitions), _t.Reason.FOREIGN)


# @anchor:wa.learn.dream_replay.DreamCertificate
@_dc.dataclass(frozen=True, slots=True, weakref_slot=True)
class DreamCertificate(_t._Record):
    artifact: _t.ArtifactIdentity
    scope: _t.ScopeIdentity
    authority: _t.AuthorityRevision
    abis: _t.ABISet
    policy_digest: _t.PlanDigest
    compiler_digest: _t.PlanDigest
    verifier_digest: _t.PlanDigest
    environment_digest: _t.PlanDigest
    suite_digest: _t.PlanDigest
    support_identity: _t.PrivateRef = _dc.field(repr=False)
    support_cohort_ids: tuple[str, ...]
    coverage_kind: str
    covered_step_ids: tuple[str, ...]
    terminal_goal_verified: bool
    replay_runs: int
    semantic_trace_digest: _t.PlanDigest
    disposition: str
    owner_ref: _t.RegistryRef = _dc.field(repr=False)

    def _validate(self):
        _need(self.artifact.scope == self.scope and self.authority.scope == self.scope.scope_revision and self.artifact.abis == self.abis, _t.Reason.STALE)
        _need(self.coverage_kind in ('full_plan', 'window'))
        _need(self.disposition in _DISPOSITIONS)
        _need(self.replay_runs >= 2 and bool(self.covered_step_ids) and bool(self.support_cohort_ids), _t.Reason.BOUNDS)
        _need(len(set(self.support_cohort_ids)) == len(self.support_cohort_ids), _t.Reason.CONFLICT)
        if self.coverage_kind == 'full_plan':
            _need(self.terminal_goal_verified and self.disposition == 'supported_reproduction', _t.Reason.MISSING)


# @anchor:wa.learn.dream_replay.ScreeningOutcome
@_dc.dataclass(frozen=True, slots=True)
class ScreeningOutcome(_t._Record):
    disposition: str
    reason: _t.Reason | None
    coverage_kind: str
    covered_step_ids: tuple[str, ...]
    terminal_goal_verified: bool
    replay_runs: int
    semantic_trace_digest: _t.PlanDigest | None
    support_cohort_ids: tuple[str, ...]
    certificate: DreamCertificate | None = _dc.field(default=None, repr=False)

    def _validate(self):
        _need(self.disposition in _DISPOSITIONS and self.coverage_kind in ('full_plan', 'window'))
        _need((self.disposition == 'supported_reproduction') == (self.certificate is not None and self.reason is None), _t.Reason.MISSING)

_DISPOSITIONS = ('supported_reproduction', 'improvement_unproven', 'off_support', 'corrupt_evidence')
_verified = {}  # Weak identity roots; the owner stays live only with its episode.
_verification_serial = 0


def _slot_contract_tuple(s):
    return (s.slot_name, s.kind.value, s.purpose.value, s.destination_contract,
            s.capability_contract, s.key_vocabulary.version if s.key_vocabulary is not None else None)


def _selector_contract_tuple(s):
    return (s.role, s.canonical_label_reference, s.landmark, s.container_owner, s.turn_owner,
            s.row, s.column, s.header_contract.value, s.action.value,
            tuple(m.value for m in s.allowed_match_levels), s.structural_identity_requirements)


def _step_action_contract(n):
    if n.effect is not None:
        e = n.effect
        return ('EFFECT', e.action.value, _selector_contract_tuple(e.selector),
                tuple(_slot_contract_tuple(a) for a in e.arguments),
                _ss._canonical_json_bytes(e.precondition).decode(), _ss._canonical_json_bytes(e.postcondition).decode())
    if n.find is not None:
        q = n.find.query
        return ('FIND', q.name, _selector_contract_tuple(q.selector), tuple(o.value for o in q.operators))
    if n.wait is not None:
        return ('WAIT_UNTIL', _ss._canonical_json_bytes(n.wait.predicate).decode(), n.wait.budget.semantic_wait_ms)
    if n.assertion is not None:
        return ('ASSERT_TAG', _ss._canonical_json_bytes(n.assertion.predicate).decode(), n.assertion.code.value)
    if n.harvest is not None:
        h = n.harvest
        return ('HARVEST', h.query_id, h.result.name, h.result.kind.value, h.result.purpose.value,
                h.result.destination_contract, h.max_bytes)
    _need(n.terminal is not None)
    return ('TERMINAL', n.terminal.reference)


def _sufficient_state_key(scope, authority, abis, digests, pre_obs, history_digest, action_contract):
    full = _ss._canonical_json_bytes((_ss._scope_tuple(scope), authority, abis,
        tuple(d.full_digest for d in digests), pre_obs.publication_identity, pre_obs.producer_identity,
        pre_obs.producer_generation, pre_obs.producer_schema, pre_obs.association.value,
        pre_obs.completeness.value, history_digest, action_contract))
    return _hashlib.sha256(b'wender-support-key/v1\0' + full).hexdigest()[:16], full


def _chain(transitions, scope, entry, terminal):
    _need(type(transitions) is tuple and bool(transitions), _t.Reason.MISSING)
    history, observation = '0' * 64, entry
    for i, t in enumerate(transitions):
        _need(type(t) is SupportedTransition, _t.Reason.TYPE)
        t.__post_init__()
        _need(t.step_index == i and t.history_digest == history and t.pre_observation == observation,
              _t.Reason.CONFLICT)
        _need(t.pre_observation.scope == t.post_observation.scope == scope, _t.Reason.STALE)
        history, observation = t.next_history_digest, t.post_observation
    _need(observation == terminal, _t.Reason.CONFLICT)


def _episode_keys(ep):
    s = ep.seal if type(ep) is VerifiedEpisode else ep
    return (s.episode_id, s.run_id, s.chain_head, s.support_identity.token, s.provenance.token)


def _independent(keys, seen):
    _need(not any(any(a == b for a, b in zip(keys, old)) for old in seen), _t.Reason.CONFLICT)


def _wire(v):
    # Predicate adapter dataclasses are private, but their complete payload must
    # participate in conflict detection. Never repr() them or omit their fields.
    if _dc.is_dataclass(v):
        return {f.name: _wire(getattr(v, f.name)) for f in _dc.fields(v)}
    if type(v) is tuple:
        return tuple(_wire(x) for x in v)
    return v


def _outcome(t):
    return _ss._canonical_json_bytes(_wire((t.post_observation, t.next_history_digest,
        t.privacy_complete, t.pre_evidence, t.post_evidence, t.safety_vetoes)))


def _reserve(index, peak, operation):
    p = index._resource_policy
    used = index._telemetry()
    _need(type(used) is int and used >= 0, _t.Reason.MISSING)
    _need(used + peak + p.emergency_reserve_bytes <= p.host_bytes and
          peak <= min(p.process_bytes, p.cancellation.consumer_byte_ceiling), _t.Reason.CAPACITY)
    deadline = index._clock.now_ns() + p.cancellation.join_deadline_ns
    req = _t.ReservationRequest(index._owner, index._owner, index._scope, operation, peak,
                                (), (), 1, deadline)
    outcome = index._reservations.reserve(req, p)
    _need(type(outcome) is _t.ReservationOutcome and outcome.grant is not None, _t.Reason.CAPACITY)
    g = outcome.grant
    _need(g.request is req and g.charged_bytes >= peak, _t.Reason.FOREIGN)
    holder = index._holder_factory(g, index._scope, p.cancellation, index._endpoint, index._boundary)
    _need(type(holder) is _t.HolderContract and holder.scope == index._scope and
          holder.retained_bytes == g.charged_bytes, _t.Reason.FOREIGN)
    return g, holder


def _drain(index, allocation):
    g, h = allocation
    proof = index._cancellation.request(h)
    _need(type(proof) is _t.CancellationResult, _t.Reason.MISSING)
    _check(index._cancellation.verify(h, proof))
    _need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined)) and
          any(t is g.reservation for t in proof.released_tokens), _t.Reason.FOREIGN)
    _check(index._reservations.release_unused(g, g.charged_bytes, proof))


# @anchor:wa.learn.dream_replay.ReplayView
class ReplayView:
    """Reveals only the exact chosen recorded action, without live services."""
    __slots__ = ('__index', '__observation', '__history', '__actions')

    def __init__(self, index, observation, actions):
        self.__index, self.__observation = index, observation
        self.__history, self.__actions = '0' * 64, actions

    # @anchor:wa.learn.dream_replay.ReplayView.current_observation
    @property
    def current_observation(self):
        return self.__observation

    # @anchor:wa.learn.dream_replay.ReplayView.history_digest
    @property
    def history_digest(self):
        return self.__history

    # @anchor:wa.learn.dream_replay.ReplayView.declared_actions
    @property
    def declared_actions(self):
        return self.__actions

    # @anchor:wa.learn.dream_replay.ReplayView.reveal_step
    @_closed
    def reveal_step(self, node):
        _need(type(node) is _t.StepPlan, _t.Reason.TYPE)
        _need(node.step_id in self.__actions, _t.Reason.MISSING)
        ix = self.__index
        hint, full = _sufficient_state_key(ix._scope, ix._authority, ix._abis, ix._digests,
            self.__observation, self.__history, _step_action_contract(node))
        _need(full not in ix._conflicts, _t.Reason.CONFLICT)
        rows = [t for key, t, _ in ix._buckets.get(hint, ()) if key == full]
        _need(bool(rows), _t.Reason.MISSING)
        t = rows[0]
        self.__observation, self.__history = t.post_observation, t.next_history_digest
        return t


# @anchor:wa.learn.dream_replay.SupportIndex
class SupportIndex:
    # @anchor:wa.learn.dream_replay.SupportIndex.__init__
    @_closed
    def __init__(self, *, owner, incarnation, generation, scope, authority, abis, digests,
                 replay_policy, resource_policy, clock, attempts, reservations, cancellation,
                 endpoint, boundary, holder_factory, telemetry, predicate_registry,
                 max_nodes=32, max_evidence_rows=64, max_inventory_rows=64):
        for v, cls in ((owner, _t.PrivateRef), (scope, _t.ScopeIdentity), (authority, _t.AuthorityRevision),
                       (abis, _t.ABISet), (replay_policy, _t.ReplayPolicy), (resource_policy, _t.ResourcePolicy),
                       (endpoint, _t.RegistryRef), (boundary, _t.RegistryRef)):
            _need(type(v) is cls, _t.Reason.TYPE)
        _need(type(digests) is tuple and len(digests) == 5 and all(type(d) is _t.PlanDigest for d in digests))
        _need(authority.scope == scope.scope_revision, _t.Reason.STALE)
        _need(incarnation == owner.incarnation, _t.Reason.FOREIGN)
        _need(type(generation) is int and generation > 0 and replay_policy.screening_jobs == 1, _t.Reason.BOUNDS)
        for v in (clock, attempts, reservations, cancellation, holder_factory, telemetry, predicate_registry):
            _need(v is not None, _t.Reason.MISSING)
        for v in (max_nodes, max_evidence_rows, max_inventory_rows):
            _need(type(v) is int and v > 0, _t.Reason.BOUNDS)
        for name, value in locals().copy().items():
            if name != 'self':
                setattr(self, '_' + name, value)
        self._lock = _threading.RLock()
        self._active_screening_jobs = self._charged_bytes = self._serial = 0
        self._episodes, self._buckets, self._entry_states, self._certificates = {}, {}, {}, {}
        self._seen_episode_keys, self._conflicts = set(), set()
        self._allocation = None

    def _mint_ref(self, kind):
        self._serial += 1
        return _t.RegistryRef(kind, self._incarnation, self._generation,
            _t.PrivateRef(self._owner.owner, self._incarnation, str(self._serial)))

    # @anchor:wa.learn.dream_replay.SupportIndex.check_certificate
    @_closed
    def check_certificate(self, certificate, artifact, scope, authority, support_identity):
        with self._lock:
            _need(type(certificate) is DreamCertificate and self._certificates.get(id(certificate)) is certificate,
                  _t.Reason.FOREIGN)
            _need(certificate.owner_ref.owner_incarnation == self._incarnation and
                  certificate.owner_ref.generation == self._generation, _t.Reason.STALE)
            _need(certificate.artifact == artifact and certificate.scope == scope == self._scope and
                  certificate.authority == authority == self._authority and certificate.abis == self._abis and
                  certificate.support_identity == support_identity and _cert_digests(certificate) == self._digests,
                  _t.Reason.STALE)
            return _t.AdmissionCheck(_t.Truth.TRUE, None)

    # @anchor:wa.learn.dream_replay.SupportIndex.cohort_ids
    @property
    def cohort_ids(self):
        return tuple(sorted(self._episodes, key=lambda x: x.encode('utf-8')))

    # @anchor:wa.learn.dream_replay.SupportIndex.charged_bytes
    @property
    def charged_bytes(self):
        return self._charged_bytes

    # @anchor:wa.learn.dream_replay.SupportIndex.replay_view
    @_closed
    def replay_view(self, episode_id, declared_actions):
        _need(type(declared_actions) is tuple and all(type(a) is str for a in declared_actions), _t.Reason.TYPE)
        _need(episode_id in self._episodes, _t.Reason.MISSING)
        return ReplayView(self, self._episodes[episode_id].entry_observation, declared_actions)


def _cert_digests(c):
    return (c.policy_digest, c.compiler_digest, c.verifier_digest, c.environment_digest, c.suite_digest)


# @anchor:wa.learn.dream_replay.load_verified
@_closed
def load_verified(ledger_owner, *, seal, abis, entry_observation, entry_evidence, transitions,
                  terminal_observation, terminal_evidence, replay_policy, fd=None, head=None,
                  grant=None, custody_check=None, allocation_check=None, seen_identities=None):
    for v, cls in ((ledger_owner, _sl.LedgerOwner), (seal, _sl.EpisodeSeal), (abis, _t.ABISet),
                   (replay_policy, _t.ReplayPolicy)):
        _need(type(v) is cls, _t.Reason.TYPE)
    _check(ledger_owner.acknowledge_seal(seal, seal.scope))
    if fd is not None:
        events = _dj.load_script_episode(fd, owner=ledger_owner, seal=seal, head=head or seal.chain_head,
            grant=grant, custody_check=custody_check, allocation_check=allocation_check)
    else:
        events, snapshot_head, _, _ = ledger_owner.snapshot()
        _need(snapshot_head == seal.chain_head and len(events) == seal.expected_count, _t.Reason.CONFLICT)
    # The owner authenticates the entire event chain. Only terminal per-action
    # outcomes constitute replay support; ADMITTED/APPLIED are not successes.
    _need(bool(events) and len(events) == seal.expected_count, _t.Reason.MISSING)
    final = {}
    for e in events:
        _need(e.episode_id == seal.episode_id and e.run_id == seal.run_id and
              e.artifact == seal.artifact and e.scope == seal.scope, _t.Reason.FOREIGN)
        final[e.action_id] = e
    outcomes = tuple(final.values())
    _need(all(e.state is _t.EventState.VERIFIED and e.disposition is _t.InputDisposition.APPLIED and
              e.verification is _t.Truth.TRUE for e in outcomes), _t.Reason.CONFLICT)
    _need(events[-1].exit_code == 0 and all(e.exit_code is not None and e.score_q16 is not None for e in outcomes), _t.Reason.MISSING)
    _chain(transitions, seal.scope, entry_observation, terminal_observation)
    effect_events = tuple(t.event for t in transitions if t.node.effect is not None)
    _need(len(effect_events) == len(outcomes) and all(any(e is t for t in effect_events) for e in outcomes), _t.Reason.FOREIGN)
    with ledger_owner._lock:
        action_steps = {r['action']: r['step'] for r in ledger_owner._reservations.values()}
        _need(all(action_steps.get(t.event.action_id) == t.node.step_id
                  for t in transitions if t.node.effect is not None), _t.Reason.FOREIGN)
    keys = _episode_keys(seal)
    if seen_identities is not None:
        _independent(keys, seen_identities)
    _ss._decoded_footprint_bytes((seal, events, transitions), replay_policy.support_bytes)
    ep = VerifiedEpisode(seal.episode_id, seal.run_id, seal.scope, seal.authority, abis, seal,
        outcomes, entry_observation, entry_evidence, transitions, seal.terminal_goal,
        terminal_observation, terminal_evidence, seal.support_identity,
        _t.RegistryRef('verified_episode', seal.producer.incarnation, 1, seal.producer))
    global _verification_serial
    _verification_serial += 1
    key = id(ep)
    _verified[key] = (_weakref.ref(ep, lambda ref: _verified.pop(key, None)), ledger_owner, _verification_serial)
    if seen_identities is not None:
        seen_identities.add(keys)
    return ep


# @anchor:wa.learn.dream_replay.build_index
@_closed
def build_index(episodes, **kwargs):
    _need(type(episodes) is tuple and bool(episodes), _t.Reason.MISSING)
    index = SupportIndex(**kwargs)
    _check(index._attempts.charge(index._scope, index._owner, 'dream-build-index', index._resource_policy, index._clock.now_ns()))
    footprint = _ss._decoded_footprint_bytes(episodes, index._replay_policy.support_bytes)
    index._allocation = _reserve(index, footprint, 'dream-support')
    try:
        _need(all(type(ep) is VerifiedEpisode for ep in episodes), _t.Reason.TYPE)
        for ep in sorted(episodes, key=lambda e: (e.episode_id.encode(), e.run_id.encode(), e.seal.chain_head.encode())):
            row = _verified.get(id(ep))
            _need(row is not None and row[0]() is ep, _t.Reason.FOREIGN)
            _check(row[1].acknowledge_seal(ep.seal, ep.scope))
            ep.__post_init__()
            _independent(_episode_keys(ep), index._seen_episode_keys)
            _need(ep.scope == index._scope and ep.authority == index._authority and
                  ep.abis == index._abis and ep.seal.digests == index._digests, _t.Reason.STALE)
            index._seen_episode_keys.add(_episode_keys(ep))
            index._episodes[ep.episode_id] = ep
            for t in ep.transitions:
                hint, full = _sufficient_state_key(ep.scope, ep.authority, ep.abis, index._digests,
                    t.pre_observation, t.history_digest, _step_action_contract(t.node))
                bucket = index._buckets.setdefault(hint, [])
                if any(key == full and _outcome(old) != _outcome(t) for key, old, _ in bucket):
                    index._conflicts.add(full)
                bucket.append((full, t, ep.episode_id))
        index._charged_bytes = footprint
        return index
    except BaseException:
        _drain(index, index._allocation)
        raise


def _predicate(index, predicate, observation, evidence, vetoes=()):
    fn = _p.evaluate_transition if predicate.operator in (_t.PredicateOperator.DECLARED_TRANSITION,
        _t.PredicateOperator.VALUE_LEN_DELTA) else _p.evaluate
    return fn(predicate, index._scope, evidence=evidence, registry=index._predicate_registry,
        owner=index._owner, generation=index._generation, observation=observation,
        max_nodes=index._max_nodes, max_evidence_rows=index._max_evidence_rows,
        max_inventory_rows=index._max_inventory_rows, safety_vetoes=vetoes)


# @anchor:wa.learn.dream_replay.screen
@_closed
def screen(index, plan, *, support_identity, replay_runs=2, window_step_ids=None, benefit_proven=True):
    _need(type(index) is SupportIndex and type(plan) is _t.ValidatedPlan and
          type(support_identity) is _t.PrivateRef, _t.Reason.TYPE)
    _need(type(replay_runs) is int and replay_runs >= 2, _t.Reason.BOUNDS)
    _need(type(benefit_proven) is bool, _t.Reason.TYPE)
    with index._lock:
        _need(index._active_screening_jobs < index._replay_policy.screening_jobs, _t.Reason.CAPACITY)
        index._active_screening_jobs += 1
    allocation = None
    try:
        _check(index._attempts.charge(index._scope, index._owner, 'dream-screen', index._resource_policy, index._clock.now_ns()))
        allocation = _reserve(index, max(128, index._charged_bytes), 'dream-scratch')
        _need(plan.scope == index._scope and plan.abis == index._abis, _t.Reason.STALE)
        nodes, coverage = plan.nodes, 'full_plan' if window_step_ids is None else 'window'
        if window_step_ids is not None:
            _need(type(window_step_ids) is tuple and bool(window_step_ids))
            ids = tuple(n.step_id for n in nodes)
            _need(window_step_ids[0] in ids)
            start = ids.index(window_step_ids[0])
            _need(ids[start:start + len(window_step_ids)] == window_step_ids)
            nodes = nodes[start:start + len(window_step_ids)]
        covered = tuple(n.step_id for n in nodes)
        terminal, digest = False, None
        def outcome(disposition, reason, cert=None):
            return ScreeningOutcome(disposition, reason, coverage, covered, terminal, replay_runs,
                                    digest, index.cohort_ids, cert)
        if coverage == 'full_plan' and (not nodes or nodes[-1].terminal != plan.terminal_goal):
            return outcome('off_support', _t.Reason.MISSING)
        _need(bool(index.cohort_ids), _t.Reason.MISSING)
        runs = []
        for _ in range(replay_runs):
            trace = []
            for eid in index.cohort_ids:
                ep = index._episodes[eid]
                view = index.replay_view(eid, tuple(n.step_id for n in plan.nodes))
                if coverage == 'full_plan':
                    if _predicate(index, plan.entry, ep.entry_observation, ep.entry_evidence).value is not _t.Truth.TRUE:
                        return outcome('off_support', _t.Reason.MISSING)
                elif nodes[0] != plan.nodes[0]:
                    # Authenticate/reveal the supported prefix to reach this window.
                    for n in plan.nodes[:plan.nodes.index(nodes[0])]:
                        view.reveal_step(n)
                for n in nodes:
                    if n.terminal is not None:
                        terminal = (ep.terminal_goal == n.terminal == plan.terminal_goal and
                            view.current_observation == ep.terminal_observation and
                            _predicate(index, plan.entry, ep.terminal_observation, ep.terminal_evidence).value is _t.Truth.TRUE)
                        if not terminal:
                            return outcome('off_support', _t.Reason.MISSING)
                        trace.append((eid, n.step_id, 'terminal', view.history_digest))
                        continue
                    try:
                        t = view.reveal_step(n)
                    except _t.ContractError as exc:
                        return outcome('corrupt_evidence' if exc.reason is _t.Reason.CONFLICT else 'off_support', exc.reason)
                    if not t.privacy_complete:
                        return outcome('off_support', _t.Reason.PRIVACY)
                    predicates = []
                    if n.effect is not None:
                        predicates = [(n.effect.precondition, t.pre_observation, t.pre_evidence),
                                      (n.effect.postcondition, t.post_observation, t.post_evidence)]
                    elif n.wait is not None or n.assertion is not None:
                        predicates = [((n.wait or n.assertion).predicate, t.post_observation, t.post_evidence)]
                    elif n.find is not None:
                        # FIND itself must have evidenced query predicates.
                        predicates = [(e.predicate, t.pre_observation, t.pre_evidence) for e in t.pre_evidence]
                        if not predicates:
                            return outcome('off_support', _t.Reason.MISSING)
                    for pred, obs, evidence in predicates:
                        result = _predicate(index, pred, obs, evidence, t.safety_vetoes)
                        if result.value is not _t.Truth.TRUE:
                            return outcome('off_support', result.reason or _t.Reason.MISSING)
                    trace.append((eid, n.step_id, _step_action_contract(n), t.history_digest,
                                  t.next_history_digest, t.post_observation.publication_identity))
            runs.append(_ss._domain_digest(_t.PlanDigest, 'wender-dream-trace/v1', _ss._canonical_json_bytes(tuple(trace))))
        digest = runs[0]
        if any(d != digest for d in runs):
            return outcome('corrupt_evidence', _t.Reason.CONFLICT)
        if not benefit_proven:
            return outcome('improvement_unproven', _t.Reason.MISSING)
        cert = DreamCertificate(plan.identity, index._scope, index._authority, index._abis,
            *index._digests, support_identity, index.cohort_ids, coverage, covered, terminal,
            replay_runs, digest, 'supported_reproduction', index._mint_ref('dream_certificate'))
        index._certificates[id(cert)] = cert
        return outcome('supported_reproduction', None, cert)
    finally:
        try:
            if allocation is not None:
                _drain(index, allocation)
        finally:
            with index._lock:
                index._active_screening_jobs -= 1
