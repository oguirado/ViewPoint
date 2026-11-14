# @module {"id":"wa.learn.script_promotion","layer":2,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.header","wa.scripts.predicates","wa.learn.script_store","wa.learn.catalogue","wa.learn.script_ledger","wa.learn.dream_replay"],"book":"HB-WA-s4-learn"}
"""Host-owned supported replay and fail-closed lifecycle contracts."""
import dataclasses as _dc
from wender.scripts import types as _t, header as _h
from wender.learn import script_store as _ss
import math as _math
from wender.scripts import predicates as _p
from wender.learn import catalogue as _cat, script_ledger as _sl, dream_replay as _dr

_need, _check, _closed = _h._need, _h._check, _h._closed


# @anchor:wa.learn.script_promotion.SPRTPolicy
@_dc.dataclass(frozen=True, slots=True)
class SPRTPolicy(_t._Record):
    policy_version: str
    p0_ppm: int
    p1_ppm: int
    alpha_ppm: int
    beta_ppm: int
    max_horizon: int
    accept_log_q16: int
    reject_magnitude_q16: int

    def _validate(self):
        _need(0 < self.p1_ppm < self.p0_ppm < 1000000, _t.Reason.BOUNDS)
        _need(0 < self.alpha_ppm < 500000 and 0 < self.beta_ppm < 500000 and self.max_horizon >= 3, _t.Reason.BOUNDS)
        _need(self.accept_log_q16 > 0 and self.reject_magnitude_q16 > 0, _t.Reason.BOUNDS)


# @anchor:wa.learn.script_promotion.PromotionPolicy
@_dc.dataclass(frozen=True, slots=True)
class PromotionPolicy(_t._Record):
    policy_version: str
    metric_unit: str
    baseline_weight_num: int
    candidate_weight_num: int
    weight_den: int
    min_gain_num: int
    min_gain_den: int
    min_support_episodes: int
    min_shadow_episodes: int
    min_replay_runs: int
    max_repair_attempts: int
    policy_digest: _t.PlanDigest
    compiler_digest: _t.PlanDigest
    verifier_digest: _t.PlanDigest
    environment_digest: _t.PlanDigest
    suite_digest: _t.PlanDigest
    sprt_policy: SPRTPolicy | None = None

    def _validate(self):
        _need(self.baseline_weight_num > 0 and self.candidate_weight_num > 0 and self.weight_den > 0, _t.Reason.BOUNDS)
        _need(self.min_gain_num > 0 and self.min_gain_den > 0, _t.Reason.BOUNDS)
        _need(self.min_support_episodes >= 3 and self.min_shadow_episodes >= 3 and self.min_replay_runs >= 2 and self.max_repair_attempts >= 1, _t.Reason.BOUNDS)


# @anchor:wa.learn.script_promotion.PairedEpisodeUtility
@_dc.dataclass(frozen=True, slots=True)
class PairedEpisodeUtility(_t._Record):
    episode_id: str
    baseline_cost: _t.MeasuredValue
    candidate_cost: _t.MeasuredValue
    baseline_terminal_verified: bool
    candidate_terminal_verified: bool
    quality_claim_kind: str = 'deterministic_reproduction'

    def _validate(self):
        _need(self.quality_claim_kind in ('deterministic_reproduction', 'avoided_inference_speed'))
        _need(self.baseline_cost.value is not None and self.candidate_cost.value is not None, _t.Reason.MISSING)
        _need(self.baseline_cost.unit == self.candidate_cost.unit)
        _need(self.baseline_terminal_verified and self.candidate_terminal_verified, _t.Reason.MISSING)


# @anchor:wa.learn.script_promotion.MutantProofEvidence
@_dc.dataclass(frozen=True, slots=True)
class MutantProofEvidence(_t._Record):
    mutant_id: str
    intended_predicate: _t.PredicateSpec
    mutant_observation: _t.ObservationRef
    mutant_evidence: tuple[_p._PredicateEvidence, ...] = _dc.field(repr=False)
    safety_vetoes: tuple[_t.PredicateResult, ...] = ()
    expected_rejection_reason: _t.Reason | None = None

    def _validate(self):
        _need(self.intended_predicate.scope == self.mutant_observation.scope, _t.Reason.STALE)


# @anchor:wa.learn.script_promotion.ShadowTrialRecord
@_dc.dataclass(frozen=True, slots=True)
class ShadowTrialRecord(_t._Record):
    episode_id: str
    run_id: str
    artifact: _t.ArtifactIdentity
    scope: _t.ScopeIdentity
    authority: _t.AuthorityRevision
    seal: _sl.EpisodeSeal
    predicted_step_ids: tuple[str, ...]
    evidenced_outcome: _t.Truth
    postcondition_result: _t.PredicateResult
    terminal_verified: bool
    regression_detected: bool
    live_input_dispatched: bool
    baseline_cost: _t.MeasuredValue
    shadow_cost: _t.MeasuredValue
    sprt_step_Pass: bool = True

    def _validate(self):
        _need(self.artifact.scope == self.scope == self.seal.scope and self.authority == self.seal.authority and self.authority.scope == self.scope.scope_revision, _t.Reason.STALE)
        _need(self.episode_id == self.seal.episode_id and self.run_id == self.seal.run_id, _t.Reason.FOREIGN)
        _need(not self.live_input_dispatched, _t.Reason.FOREIGN)
        _need(bool(self.predicted_step_ids), _t.Reason.MISSING)


# @anchor:wa.learn.script_promotion.PromotionEvidence
@_dc.dataclass(frozen=True, slots=True)
class PromotionEvidence(_t._Record):
    artifact: _t.ArtifactIdentity
    scope: _t.ScopeIdentity
    authority: _t.AuthorityRevision
    dream_certificate: _dr.DreamCertificate = _dc.field(repr=False)
    support_identity: _t.PrivateRef = _dc.field(repr=False)
    mutant_proof: MutantProofEvidence
    paired_utilities: tuple[PairedEpisodeUtility, ...]
    shadow_trials: tuple[ShadowTrialRecord, ...] = ()
    probes: tuple[_cat.CatalogueProbe, ...] = ()
    expected_predecessor_revision: int = 0
    repair_attempt: int = 0
    secret_canaries: tuple[str, ...] = ()

    def _validate(self):
        _need(self.artifact.scope == self.scope and self.authority.scope == self.scope.scope_revision, _t.Reason.STALE)
        _need(bool(self.paired_utilities), _t.Reason.MISSING)


# @anchor:wa.learn.script_promotion.UtilityEvaluation
@_dc.dataclass(frozen=True, slots=True)
class UtilityEvaluation(_t._Record):
    passed: bool
    reason: _t.Reason | None
    episode_count: int
    weighted_baseline_num: int
    weighted_candidate_num: int
    net_gain_num: int
    net_gain_den: int


# @anchor:wa.learn.script_promotion.TransitionResult
@_dc.dataclass(frozen=True, slots=True)
class TransitionResult(_t._Record):
    artifact_digest: _t.ArtifactDigest
    previous_lifecycle: _t.Lifecycle
    new_lifecycle: _t.Lifecycle
    lifecycle_revision: int
    deny_sequence: int
    frozen_support_cohort_ids: tuple[str, ...]
    shadow_cohort_ids: tuple[str, ...]
    published_snapshot: _cat.CatalogueSnapshot | None = _dc.field(default=None, repr=False)


def _digests(policy):
    return (policy.policy_digest, policy.compiler_digest, policy.verifier_digest,
            policy.environment_digest, policy.suite_digest)


# @anchor:wa.learn.script_promotion.evaluate
@_closed
def evaluate(policy, evidence, *, index):
    _need(type(policy) is PromotionPolicy and type(evidence) is PromotionEvidence and
          type(index) is _dr.SupportIndex, _t.Reason.TYPE)
    _check(index.check_certificate(evidence.dream_certificate, evidence.artifact, evidence.scope,
                                   evidence.authority, evidence.support_identity))
    c = evidence.dream_certificate
    _need(_dr._cert_digests(c) == _digests(policy), _t.Reason.STALE)
    _need(c.coverage_kind == 'full_plan' and c.terminal_goal_verified and
          c.disposition == 'supported_reproduction' and c.replay_runs >= policy.min_replay_runs, _t.Reason.MISSING)
    _need(len(c.support_cohort_ids) >= policy.min_support_episodes, _t.Reason.MISSING)
    mp = evidence.mutant_proof
    result = _dr._predicate(index, mp.intended_predicate, mp.mutant_observation,
                           mp.mutant_evidence, mp.safety_vetoes)
    _need(result.value is not _t.Truth.TRUE, _t.Reason.CONFLICT)
    if mp.expected_rejection_reason is not None and result.value is _t.Truth.UNKNOWN:
        _need(result.reason == mp.expected_rejection_reason, _t.Reason.CONFLICT)
    ids = tuple(u.episode_id for u in evidence.paired_utilities)
    _need(len(set(ids)) == len(ids), _t.Reason.CONFLICT)
    _need(set(ids) == set(c.support_cohort_ids), _t.Reason.MISSING)
    B = C = 0
    regression = False
    for u in evidence.paired_utilities:
        u.__post_init__()
        _need(u.baseline_cost.unit == policy.metric_unit == u.candidate_cost.unit)
        _need(u.baseline_cost.value > 0, _t.Reason.BOUNDS)
        _need(u.quality_claim_kind == 'avoided_inference_speed', _t.Reason.MISSING)
        b, v = u.baseline_cost.value * policy.baseline_weight_num, u.candidate_cost.value * policy.candidate_weight_num
        regression |= v > b
        B, C = B + b, C + v
    passed = not regression and B > C and (B - C) * policy.min_gain_den > B * policy.min_gain_num
    reason = None if passed else (_t.Reason.CONFLICT if regression else _t.Reason.BOUNDS)
    # Signed losses are conveyed by passed/reason; _Record integer fields are unsigned.
    return UtilityEvaluation(passed, reason, len(ids), B, C, max(0, B - C), B)


# @anchor:wa.learn.script_promotion.record_shadow
@_closed
def record_shadow(store, trial, *, frozen_support_cohort_ids, existing_trials=(), ledger_owner=None):
    _need(type(store) is _ss.ScriptStore and type(trial) is ShadowTrialRecord, _t.Reason.TYPE)
    trial.__post_init__()
    with store._lock:
        cell = store._lookup(trial.artifact.artifact, trial.scope).cell
        _need(cell.lifecycle is _t.Lifecycle.SHADOW and cell.resident, _t.Reason.CONFLICT)
        _need(cell.identity == trial.artifact and trial.authority == cell.authority_revision ==
              store.current_authority_revision(trial.scope), _t.Reason.STALE)
        # Require an authenticated seal even when the optional owner is omitted:
        # it must already have entered through load_verified's host boundary.
        if ledger_owner is not None:
            _need(type(ledger_owner) is _sl.LedgerOwner, _t.Reason.TYPE)
            _check(ledger_owner.acknowledge_seal(trial.seal, trial.scope))
        else:
            _need(any(ref() is not None and ref().seal is trial.seal for ref, _, serial in _dr._verified.values()), _t.Reason.FOREIGN)
        _need(trial.seal.complete, _t.Reason.MISSING)
        _need(trial.episode_id not in frozen_support_cohort_ids, _t.Reason.CONFLICT)
        _dr._independent(_dr._episode_keys(trial.seal), {_dr._episode_keys(t.seal) for t in existing_trials})
        _need(trial.evidenced_outcome is _t.Truth.TRUE and trial.postcondition_result.value is _t.Truth.TRUE and
              trial.terminal_verified and not trial.regression_detected, _t.Reason.MISSING)
        _need(trial.baseline_cost.value is not None and trial.shadow_cost.value is not None, _t.Reason.MISSING)
        _need(trial.baseline_cost.unit == trial.shadow_cost.unit)
        _need(trial.shadow_cost.value <= trial.baseline_cost.value, _t.Reason.CONFLICT)
        return existing_trials + (trial,)


def _sprt(policy, trials):
    if policy is None:
        return
    _need(len(trials) <= policy.max_horizon, _t.Reason.BOUNDS)
    llr = 0
    for trial in trials:
        ratio = ((1000000 - policy.p1_ppm) / (1000000 - policy.p0_ppm) if trial.sprt_step_Pass
                 else policy.p1_ppm / policy.p0_ppm)
        llr += int(round(65536 * _math.log(ratio)))
        _need(llr > -policy.reject_magnitude_q16, _t.Reason.MISSING)
        if llr >= policy.accept_log_q16:
            return
    _need(False, _t.Reason.MISSING)


# @anchor:wa.learn.script_promotion.transition
@_closed
def transition(store, target_lifecycle, *, policy, evidence, index, tokenizer=None,
               export_receipt_port=None, export_receipts=None, privacy_policy_revision=1):
    _need(type(store) is _ss.ScriptStore, _t.Reason.TYPE)
    _need(target_lifecycle in (_t.Lifecycle.SHADOW, _t.Lifecycle.PROMOTED))
    util = evaluate(policy, evidence, index=index)
    _need(util.passed, util.reason or _t.Reason.CONFLICT)
    _need(evidence.repair_attempt <= policy.max_repair_attempts, _t.Reason.BOUNDS)
    with store._lock:
        store._live()
        state = store._lookup(evidence.artifact.artifact, evidence.scope)
        c = state.cell
        _need(c.resident and c.identity == evidence.artifact and c.authority_revision == evidence.authority ==
              store.current_authority_revision(evidence.scope), _t.Reason.STALE)
        values = (c.uso_text or '', c.cli_text or '', *c.fts_terms)
        _need(not any(secret in text for secret in evidence.secret_canaries for text in values), _t.Reason.PRIVACY)
        _need(not any(_cat._ROLE.search(text) for text in values), _t.Reason.PRIVACY)
        # Frozen cohorts live with the owner, not with a caller-provided tuple.
        if not hasattr(store, '_dream_cohorts'):
            store._dream_cohorts = {}
        key = (_ss._scope_tuple(evidence.scope), c.key.artifact_digest.full_digest)
        support = evidence.dream_certificate.support_cohort_ids
        if target_lifecycle is _t.Lifecycle.SHADOW:
            _need(c.lifecycle is _t.Lifecycle.DRAFT, _t.Reason.CONFLICT)
            store._change(state, lifecycle=_t.Lifecycle.SHADOW, lifecycle_revision=c.lifecycle_revision + 1)
            store._dream_cohorts[key] = (support, _dr._verification_serial)
            return TransitionResult(c.key.artifact_digest, c.lifecycle, _t.Lifecycle.SHADOW,
                state.cell.lifecycle_revision, store.deny_sequence, support, ())
        _need(c.lifecycle is _t.Lifecycle.SHADOW, _t.Reason.CONFLICT)
        frozen = store._dream_cohorts.get(key)
        _need(frozen is not None and frozen[0] == support, _t.Reason.CONFLICT)
        _need(len(evidence.shadow_trials) >= policy.min_shadow_episodes, _t.Reason.MISSING)
        trials = ()
        support_keys = {_dr._episode_keys(ep) for ep in index._episodes.values()}
        for trial in evidence.shadow_trials:
            _need(any(ref() is not None and ref().seal is trial.seal and serial > frozen[1]
                      for ref, _, serial in _dr._verified.values()), _t.Reason.MISSING)
            _need(trial.artifact == evidence.artifact and trial.scope == evidence.scope and
                  trial.authority == evidence.authority, _t.Reason.STALE)
            _dr._independent(_dr._episode_keys(trial.seal), support_keys)
            trials = record_shadow(store, trial, frozen_support_cohort_ids=support, existing_trials=trials)
        _sprt(policy.sprt_policy, trials)
        _need(bool(evidence.probes) and callable(tokenizer), _t.Reason.MISSING)
        previous_defaults = store._default_by_slot.copy()
        previous_cell = state.cell
        # The store lock covers eligibility staging, probes and snapshot publish.
        # Readers cannot observe a promoted cell before its catalogue passes.
        try:
            cert = store.mint_certificate('promotion_certificate', c.key.artifact_digest.full_digest)
            store.publish_default(c.key.artifact_digest, scope=evidence.scope,
                expected_predecessor_revision=evidence.expected_predecessor_revision, certificate=cert)
            snap = _cat.build_snapshot(store, scope=evidence.scope, authority_revision=evidence.authority,
                probes=evidence.probes, tokenizer=tokenizer, export_receipt_port=export_receipt_port,
                export_receipts=export_receipts, privacy_policy_revision=privacy_policy_revision,
                secret_canaries=evidence.secret_canaries)
            _cat.publish(store, snap)
        except BaseException:
            state.cell = previous_cell
            store._default_by_slot = previous_defaults
            store._bump()
            raise
        return TransitionResult(c.key.artifact_digest, c.lifecycle, _t.Lifecycle.PROMOTED,
            state.cell.lifecycle_revision, store.deny_sequence, support,
            tuple(sorted(t.episode_id for t in trials)), snap)


# @anchor:wa.learn.script_promotion.suspend
@_closed
def suspend(store, classification, *, target_digest=None, lease=None):
    _need(type(store) is _ss.ScriptStore, _t.Reason.TYPE)
    return store.revoke(classification, target_digest=target_digest, lease=lease)


# @anchor:wa.learn.script_promotion.reinstate
@_closed
def reinstate(store, artifact_digest, *, scope, expected_lifecycle_revision, recovery_certificate,
              reconciled_environments=(), target_lifecycle=_t.Lifecycle.PROMOTED):
    _need(type(store) is _ss.ScriptStore and type(artifact_digest) is _t.ArtifactDigest, _t.Reason.TYPE)
    _need(type(reconciled_environments) is tuple and all(type(e) is str and e for e in reconciled_environments), _t.Reason.TYPE)
    with store._lock:
        store._live()
        state = store._lookup(artifact_digest, scope)
        _need(artifact_digest.full_digest not in store._tombstones.get(_ss._scope_tuple(scope), set()), _t.Reason.CONFLICT)
        if reconciled_environments and state.cell.lifecycle is _t.Lifecycle.PROMOTED:
            store._certificate(recovery_certificate, ('recovery_certificate',))
            _need(state.cell.lifecycle_revision == expected_lifecycle_revision and
                  state.cell.authority_revision == store.current_authority_revision(scope), _t.Reason.STALE)
            store._degraded_environments.get(_ss._scope_tuple(scope), set()).difference_update(reconciled_environments)
            store._change(state, denied_environments=tuple(e for e in state.cell.denied_environments
                if e not in reconciled_environments), lifecycle_revision=state.cell.lifecycle_revision + 1)
            return store._view(state)
        return store.reinstate_quarantined(artifact_digest, scope=scope,
            expected_lifecycle_revision=expected_lifecycle_revision, recovery_certificate=recovery_certificate,
            target_lifecycle=target_lifecycle)


# @anchor:wa.learn.script_promotion.retire
@_closed
def retire(store, artifact_digest, *, scope, expected_lifecycle_revision, reason):
    _need(type(store) is _ss.ScriptStore and type(artifact_digest) is _t.ArtifactDigest, _t.Reason.TYPE)
    _need(type(expected_lifecycle_revision) is int and expected_lifecycle_revision >= 1, _t.Reason.BOUNDS)
    _need(type(reason) is str and bool(reason), _t.Reason.MISSING)
    with store._lock:
        state = store._lookup(artifact_digest, scope)
        c = state.cell
        _need(c.lifecycle_revision == expected_lifecycle_revision, _t.Reason.STALE)
        _need(c.lifecycle is not _t.Lifecycle.RETIRED, _t.Reason.CONFLICT)
        for lease in tuple(state.holders.values()):
            store._deny_lease(state, lease)
        if store._default_by_slot.get(c.key.slot_key()) == artifact_digest.full_digest:
            del store._default_by_slot[c.key.slot_key()]
        store._tombstones.setdefault(_ss._scope_tuple(scope), set()).add(artifact_digest.full_digest)
        store._deny_sequence += 1
        store._change(state, lifecycle=_t.Lifecycle.RETIRED, lifecycle_revision=c.lifecycle_revision + 1)
        return store._view(state)
