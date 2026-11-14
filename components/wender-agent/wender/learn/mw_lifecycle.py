# @module {"id":"wa.learn.mw_lifecycle","layer":2,"owner":"LEARN","deps":["wa.tools.memoryweb_gate","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Local recovery contracts and immutable generations; no production owners installed.

Origin: mwp6_w.md / MEMORYWEB_PROTOCOL.md. Protocol constants are [TARGET].
Owner methods return exact AdmissionCheck records. Owners, clocks, roots and
validation contexts are trusted composition, never deserialized from artifacts.
The store owner holds an exclusive writer lease throughout commit and attests
same-filesystem atomic replace and file/directory fsync semantics. Its check(pin)
attests the selector revision, scope, authority, deadline and immutable custody.
Candidate files are bounded relative JSON/Markdown metadata; selector and manifests are private.
resources.admit(policy) attests aggregate simultaneous base, decoded, candidate,
evidence and staging memory fits peak_bytes. reserve is atomic and denies new
work while prior cancellation/drain remains unproven; release follows proof.
No generic allocator or filesystem implementation is inferred from these ports.
Readers use pin_generation once and retain the owner's generation lease.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from tools import memoryweb_gate as gate
from wender.scripts.types import AdmissionCheck, Reason, Truth


class _Refusal(ValueError):
    def __init__(self, gate_name, reason=Reason.SCHEMA):
        self.gate = gate_name
        self.reason = reason
        super().__init__(gate_name)


def _need(ok, name, reason=Reason.SCHEMA):
    if not ok:
        raise _Refusal(name, reason)


def _admitted(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


def _passed(value):
    return type(value) is gate.GateResult and type(value.rc) is int and value.rc == 0 and value.category == 'PASS'


def _id(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def _digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _positive(value):
    return type(value) is int and value > 0


def _counter(value):
    return type(value) is int and value >= 0


def _distinct(values):
    return type(values) in (list, tuple) and all(_id(x) for x in values) and len(set(values)) == len(values)


# @anchor:wa.learn.mw_lifecycle.RecoveryKey
@dataclass(frozen=True)
class RecoveryKey:
    scope: str
    authority: str
    site_id: str
    lineage_id: str
    episode_id: str
    policy_digest: str

    # @anchor:wa.learn.mw_lifecycle.RecoveryKey.__post_init__
    def __post_init__(self):
        _need(all(_id(getattr(self, f.name)) for f in fields(self) if f.name != 'policy_digest') and
              _digest(self.policy_digest), 'KEY')


# @anchor:wa.learn.mw_lifecycle.RecoveryPolicy
@dataclass(frozen=True)
class RecoveryPolicy:
    journal_bytes: int
    line_bytes: int
    records: int
    state_bytes: int
    patch_bytes: int
    patch_operations: int
    evidence_rows: int
    generation_bytes: int
    generation_files: int
    retained_generations: int
    peak_bytes: int
    poll_ns: int
    attempt_ns: int
    deadline_ns: int
    cancel_ns: int
    drain_ns: int
    resources: object
    attempts: int = 3
    candidates: int = 5
    mutants: int = 3
    retirement: int = 3

    # @anchor:wa.learn.mw_lifecycle.RecoveryPolicy.__post_init__
    def __post_init__(self):
        _need(all(_positive(getattr(self, f.name)) for f in fields(self) if f.name != 'resources'), 'POLICY')
        _need(self.attempts <= 3 and self.candidates <= 5 and self.mutants >= 3 and self.retirement == 3,
              'PROTOCOL_CAPS')
        _need(self.resources is not None, 'RESOURCE_OWNER', Reason.MISSING)


# @anchor:wa.learn.mw_lifecycle.WatchState
@dataclass(frozen=True)
class WatchState:
    key: RecoveryKey
    generation: int
    source_generation: str
    workflow_digest: str
    journal_generation: str
    cursor: int = 0
    prefix_digest: str = hashlib.sha256(b'').hexdigest()
    attempts: tuple = ()
    attempt_started: tuple = ()
    attempt_deadlines: tuple = ()
    consumed: tuple = ()
    progress: tuple = ()
    no_progress: int = 0
    online_failures: tuple = ()
    online_history: tuple = ()
    quarantine: bool = False
    handoff: bool = False
    barrier: bool = False
    retired: bool = False
    routable: bool = True
    previous_digest: str | None = None

    # @anchor:wa.learn.mw_lifecycle.WatchState.__post_init__
    def __post_init__(self):
        _need(type(self.key) is RecoveryKey and _counter(self.generation) and _counter(self.cursor) and
              _counter(self.no_progress), 'STATE_COUNTER')
        _need(all(_id(x) for x in (self.source_generation, self.journal_generation)) and
              _digest(self.workflow_digest) and _digest(self.prefix_digest) and
              (self.previous_digest is None or _digest(self.previous_digest)), 'STATE_PIN')
        _need(all(type(x) is tuple and _distinct(x) for x in
                  (self.attempts, self.consumed, self.progress, self.online_failures)), 'STATE_SET')
        _need(all(type(x) is bool for x in (self.quarantine, self.handoff, self.barrier, self.retired, self.routable)), 'STATE_FLAGS')
        _need(not self.retired or not self.routable, 'RETIRED_ROUTING')
        _need(type(self.attempt_started) is tuple and type(self.attempt_deadlines) is tuple and
              len(self.attempt_started) == len(self.attempt_deadlines) == len(self.attempts) and
              all(_counter(a) and _positive(b) and a < b for a, b in zip(self.attempt_started, self.attempt_deadlines)),
              'ATTEMPT_LEDGER')
        _need(type(self.online_history) is tuple and all(type(row) is tuple and len(row) == 2 and _id(row[0]) and
              row[1] in ('success', 'failure', 'outage', 'cancelled', 'torn', 'missing', 'unsupported', 'privacy')
              for row in self.online_history) and _distinct([row[0] for row in self.online_history]), 'ONLINE_HISTORY')


# @anchor:wa.learn.mw_lifecycle.RepairState
@dataclass(frozen=True)
class RepairState:
    key: RecoveryKey
    base_digest: str
    preimage_digest: str | None
    preimage_ref: str | None
    candidate_count: int
    attempted: tuple
    passing: tuple
    required: tuple
    suite_digest: str
    oracle_digest: str
    profile_digest: str
    support_digest: str
    outcomes: tuple = ()
    quarantine: bool = False

    # @anchor:wa.learn.mw_lifecycle.RepairState.__post_init__
    def __post_init__(self):
        _need(type(self.key) is RecoveryKey and _counter(self.candidate_count), 'REPAIR_STATE')
        _need(all(_digest(x) for x in (self.base_digest, self.suite_digest, self.oracle_digest,
                                      self.profile_digest, self.support_digest)), 'REPAIR_PINS')
        _need((self.preimage_digest is None and self.preimage_ref is None) or
              (self.preimage_digest == self.base_digest and _id(self.preimage_ref)), 'PREIMAGE')
        _need(all(type(x) is tuple and _distinct(x) for x in (self.attempted, self.passing, self.required)), 'REPAIR_SET')
        _need(self.required and set(self.passing) <= set(self.required) and
              self.candidate_count == len(self.attempted), 'REPAIR_INVENTORY')
        _need(type(self.quarantine) is bool, 'REPAIR_QUARANTINE')
        _need(type(self.outcomes) is tuple and all(x in ('accepted', 'rejected') for x in self.outcomes) and
              len(self.outcomes) <= self.candidate_count, 'REPAIR_OUTCOMES')


# @anchor:wa.learn.mw_lifecycle.Decision
@dataclass(frozen=True)
class Decision:
    disposition: str
    reason: Reason | None
    rc: int
    verdict: str
    evidence_refs: tuple = ()
    required_evidence: tuple = ()

    # @anchor:wa.learn.mw_lifecycle.Decision.__post_init__
    def __post_init__(self):
        _need(self.disposition in ('observe', 'attempt', 'stop', 'shadow', 'handoff') and
              self.verdict in ('OBSERVED', 'ADMITTED', 'TORN', 'INPUT_UNCERTAIN', 'STUCK',
                               'OUT_OF_SUPPORT', 'REPAIR_EXHAUSTED', 'CANDIDATE', 'REJECTED') and
              type(self.rc) is int and self.rc in (0, 2, 3, 4, 5), 'DECISION')
        _need((self.reason is None or type(self.reason) is Reason) and
              _distinct(self.evidence_refs) and _distinct(self.required_evidence), 'DECISION_PRIVACY')


# @anchor:wa.learn.mw_lifecycle.GenerationPin
@dataclass(frozen=True)
class GenerationPin:
    generation: str
    manifest_digest: str
    selector_revision: int
    handle: object

    # @anchor:wa.learn.mw_lifecycle.GenerationPin.__post_init__
    def __post_init__(self):
        _need(_id(self.generation) and _digest(self.manifest_digest) and
              _counter(self.selector_revision) and self.handle is not None, 'GENERATION_PIN')


# @anchor:wa.learn.mw_lifecycle.TransitionResult
@dataclass(frozen=True)
class TransitionResult:
    prior_generation: str | None
    new_generation: str | None
    prior_lifecycle: str
    new_lifecycle: str
    metrics: tuple
    committed: bool
    reason: Reason | None
    evidence_refs: tuple = ()
    workflow_bytes: bytes | None = None


# @anchor:wa.learn.mw_lifecycle.policy_digest
def policy_digest(policy):
    """Pin all resource limits and protocol thresholds, excluding trusted handles."""
    _need(type(policy) is RecoveryPolicy, 'POLICY')
    return _hash(gate.canon({f.name: getattr(policy, f.name) for f in fields(policy) if f.name != 'resources'}).encode())


def _policy_key(key, policy):
    _need(type(key) is RecoveryKey and key.policy_digest == policy_digest(policy), 'FROZEN_POLICY')


# @anchor:wa.learn.mw_lifecycle.encode_state
def encode_state(state, *, policy):
    """Closed versioned metadata only; reserve before serialization."""
    with _capacity(policy):
        return _encode_state(state, policy=policy)


def _encode_state(state, *, policy):
    _need(type(state) in (WatchState, RepairState), 'STATE_TYPE')
    _policy_key(state.key, policy)
    for f in fields(state):
        value = getattr(state, f.name)
        if type(value) is tuple:
            _need(len(value) <= policy.records if f.name.startswith('attempt') else len(value) <= policy.evidence_rows,
                  'STATE_ROWS', Reason.CAPACITY)
    value = {'schema': 'mwp6.' + type(state).__name__ + '.v1', 'record': asdict(state)}
    raw = (gate.canon(value) + '\n').encode()
    _need(len(raw) <= policy.state_bytes, 'STATE_CAPACITY', Reason.CAPACITY)
    return raw


# @anchor:wa.learn.mw_lifecycle.decode_state
def decode_state(raw, *, policy):
    with _capacity(policy):
        return _decode_state(raw, policy=policy)


def _decode_state(raw, *, policy):
    """Requires the caller to hold this operation's capacity reservation."""
    _need(type(raw) is bytes and len(raw) <= policy.state_bytes, 'STATE_CAPACITY', Reason.CAPACITY)
    value = gate.strict_load(raw)
    _need(type(value) is dict and set(value) == {'schema', 'record'}, 'STATE_SCHEMA')
    cls = {'mwp6.WatchState.v1': WatchState, 'mwp6.RepairState.v1': RepairState}.get(value['schema'])
    _need(cls is not None and type(value['record']) is dict and
          set(value['record']) == {f.name for f in fields(cls)}, 'STATE_SCHEMA')
    record = value['record'].copy()
    _need(type(record['key']) is dict and set(record['key']) == {f.name for f in fields(RecoveryKey)}, 'KEY')
    record['key'] = RecoveryKey(**record['key'])
    for name in ('attempts', 'attempt_started', 'attempt_deadlines', 'consumed', 'progress', 'online_failures', 'online_history', 'attempted', 'passing', 'required', 'outcomes'):
        if name in record:
            _need(type(record[name]) is list, 'STATE_ARRAY')
            if name == 'online_history':
                _need(all(type(row) is list for row in record[name]), 'ONLINE_HISTORY')
                record[name] = tuple(tuple(row) for row in record[name])
            else:
                record[name] = tuple(record[name])
    state = cls(**record)
    _need(raw == _encode_state(state, policy=policy), 'STATE_CANONICAL')
    return state


@contextmanager
def _capacity(policy):
    _need(type(policy) is RecoveryPolicy, 'POLICY')
    owner = policy.resources
    _need(_admitted(owner.admit(policy)), 'AGGREGATE_PEAK', Reason.CAPACITY)
    _need(_admitted(owner.reserve('mwp6_peak', policy.peak_bytes)), 'RESOURCE_ADMISSION', Reason.CAPACITY)
    body_error = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            # Unknown cancellation/drain deliberately retains the reservation.
            _need(_admitted(owner.cancel(policy.cancel_ns)) and _admitted(owner.drain(policy.drain_ns)),
                  'DRAIN', Reason.RECONCILIATION)
            _need(_admitted(owner.release()), 'RELEASE', Reason.RECONCILIATION)
        except BaseException as cleanup_error:
            if body_error is not None:
                raise cleanup_error from body_error
            raise


def _time(clock, policy):
    now = clock()
    _need(_counter(now) and now < policy.deadline_ns, 'DEADLINE', Reason.DEADLINE)
    return now


def _state_save(state, new, owner, policy):
    _need(_admitted(owner.check(state.key)), 'AUTHORITY', Reason.STALE)
    old = _encode_state(state, policy=policy)
    if type(new) is WatchState:
        new = replace(new, generation=state.generation + 1, previous_digest=_hash(old))
    raw = _encode_state(new, policy=policy)
    _need(_admitted(owner.save(raw, _hash(old))), 'DURABLE_STATE', Reason.RECONCILIATION)
    return new


def _load_evidence(kind, artifact, related, context):
    _need(type(context) is gate.ValidationContext and callable(context.evidence_load) and
          callable(context.evidence_check), 'EVIDENCE_OWNER', Reason.MISSING)
    view = context.evidence_load(kind, artifact, related)
    _need(type(view) is dict and _passed(context.evidence_check(kind, artifact, (view, *related))),
          'EVIDENCE_AUTH', Reason.FOREIGN)
    return view


def _metrics(workflow, view, target, policy):
    stats = {name: None for name in workflow['stats']}
    mutants = workflow['oracle']['planted_mutant_ids']
    _need(_distinct(mutants) and len(mutants) >= 3 and view.get('nominal_pass') is True and
          set(view.get('killed_mutant_ids', ())) == set(mutants), 'MUTANTS')
    _need(view.get('protected_pass') is True and view.get('supported') is True, 'PROTECTED_SUPPORT')
    if workflow['admission_basis'] == 'dream':
        episodes = view.get('episodes')
        _need(type(episodes) is list and 3 <= len(episodes) <= policy.evidence_rows and
              _distinct([e.get('id') for e in episodes]), 'DREAM_EPISODES')
        rewards, savings = [], []
        for e in episodes:
            _need(e.get('closed') is True and e.get('supported') is True, 'DREAM_SUPPORT')
            b, c, pb, pc = (e.get(k) for k in ('tokens_base', 'tokens_cand', 'pass_base', 'pass_cand'))
            _need(_positive(b) and _counter(c) and type(pb) is int and pb in (0, 1) and
                  type(pc) is int and pc in (0, 1), 'DREAM_DENOMINATOR')
            rewards.append(1000 * (pc - pb) - (c - b) // 10)
            savings.append((b - c) * 10000 // b)
        _need(min(rewards) >= 0 and min(savings) >= 1500, 'DREAM_REWARD')
        stats.update(min_r_e=min(rewards), saved_tokens_bps=min(savings), support_n=len(episodes),
                     replay_pass=sum(e['pass_cand'] for e in episodes))
    else:
        runs = view.get('admission_runs')
        _need(type(runs) is list and 2 <= len(runs) <= policy.evidence_rows and _distinct([r.get('id') for r in runs]), 'DEMO_INDEPENDENCE')
        for r in runs:
            segments = r.get('required_segments')
            _need(type(segments) is list and segments and all(x is True for x in segments) and
                  r.get('verified') is True and r.get('independent') is True and r.get('grounded') is True and
                  r.get('reproduced_ppm') == 1000000, 'DEMO_REPRODUCTION')
        if view.get('task_scope') == 'project_mutating':
            _need(any(r.get('operator_demo') is True for r in runs) and
                  any(r.get('supervised') is True and r.get('authorized') is True and
                      r.get('operator_demo') is not True for r in runs), 'SUPERVISED')
        else:
            _need(view.get('task_scope') in ('read_only', 'resettable'), 'TASK_SCOPE')
        stats.update(reproduced_ppm=1000000, support_n=len(runs), replay_pass=len(runs))
    if target != 'promoted':
        return stats
    profile, cohort, trials = workflow['evaluation_profile'], view.get('cohort'), view.get('trials')
    _need(type(cohort) is dict and type(trials) is list and
          _hash(gate.canon(cohort).encode()) == profile['cohort_sha256'] and
          view.get('profile_sha256') == _hash(gate.canon(profile).encode()), 'FROZEN_COHORT')
    tasks, fixtures = cohort.get('task_ids'), profile['fixture_ids']
    _need(_distinct(tasks) and 3 <= len(tasks) <= policy.evidence_rows and len(trials) <= policy.evidence_rows and _distinct(fixtures) and
          cohort.get('fixture_ids') == fixtures and
          _distinct([t.get('task_id') for t in trials]) and
          set(t['task_id'] for t in trials) == set(tasks), 'TASK_INVENTORY')
    initial = profile['kind'] == 'initial_task_v1'
    _need(profile['kind'] in ('initial_task_v1', 'paired_replacement_v1') and
          (workflow['baseline_id'] is None) == initial and
          (profile['baseline_workflow_sha256'] is None) == initial, 'BASELINE')
    if not initial:
        _need(view.get('baseline_eligible') is True and view.get('baseline_id') == workflow['baseline_id'] and
              view.get('baseline_workflow_sha256') == profile['baseline_workflow_sha256'], 'INCUMBENT')
    gain = 0
    for t in trials:
        _need(all(t.get(k) is True for k in ('authorized', 'live', 'supported', 'required_pass', 'reviewed',
                                            'semantic_verification')) and t.get('regression') is False,
              'FINAL_SEMANTICS')
        c = t.get('candidate')
        _need(type(c) is dict and type(c.get('task_pass')) is int and c['task_pass'] == 1 and
              type(c.get('fixtures')) is dict and set(c['fixtures']) == set(fixtures) and
              all(type(x) is bool for x in c['fixtures'].values()), 'ALL_PASS')
        if initial:
            _need(all(c['fixtures'].values()), 'FIXTURE_PASS')
            gain += 10000
        else:
            b = t.get('baseline')
            _need(type(b) is dict and type(b.get('task_pass')) is int and b['task_pass'] in (0, 1) and
                  type(b.get('fixtures')) is dict and set(b['fixtures']) == set(fixtures) and
                  all(type(x) is bool for x in b['fixtures'].values()), 'BASELINE_OUTCOMES')
            _need(c['task_pass'] >= b['task_pass'] and
                  all(not b['fixtures'][f] or c['fixtures'][f] for f in fixtures), 'REGRESSION')
            ct, bt = (c['task_pass'], sum(c['fixtures'].values())), (b['task_pass'], sum(b['fixtures'].values()))
            if ct == bt:
                _need(_counter(c.get('tokens_used')) and _counter(b.get('tokens_used')), 'TIED_TOKENS')
                ct, bt = ct + (-c['tokens_used'],), bt + (-b['tokens_used'],)
            gain += (ct > bt) - (ct < bt)
    bound = 9500 if initial else 0
    _need(gain > len(tasks) * bound, 'MARGIN')
    stats.update(margin_gain=gain, margin_mass=len(tasks), margin_bound=bound,
                 online_shadow_runs=len(tasks), shadow_regressions=0)
    return stats


# @anchor:wa.learn.mw_lifecycle.evaluate_transition
def evaluate_transition(workflow, target_status, *, evidence, owners, policy):
    """Return an inactive, real-gate-validated workflow. No active selector changes.

    owners: check(key/evidence), context, bundle, clock, validation_root. Evidence
    is an opaque owner-bound reference, not an import or authority configuration.
    """
    with _capacity(policy):
        _time(owners.clock, policy)
        _need(type(workflow) is bytes and len(workflow) <= 65536, 'WORKFLOW_BYTES', Reason.CAPACITY)
        w = gate.strict_load(workflow)
        _need(workflow == (gate.canon(w) + '\n').encode(), 'WORKFLOW_CANONICAL')
        old = w['status']
        _need(_id(w['id']), 'WORKFLOW_ID')
        _need((old, target_status) in (('draft', 'shadow'), ('shadow', 'promoted'),
              ('shadow', 'retired'), ('promoted', 'retired'), ('draft', 'draft'),
              ('promoted', 'fallback')), 'TRANSITION')
        _need(_admitted(owners.check(evidence)), 'TRANSITION_AUTHORITY', Reason.FOREIGN)
        kind = 'draft' if target_status == 'draft' else 'fallback' if target_status == 'fallback' else 'lifecycle'
        view = _load_evidence(kind, w, (evidence,), owners.context)
        stats = w['stats'].copy()
        _need(all(v is None or type(v) is int for v in stats.values()), 'STATS_TYPES')
        if target_status == 'draft':
            _need(all(view.get(k) is True for k in ('closed_manifest', 'reviewed_explanation', 'grounded_steps',
                  'capabilities', 'explicit_oracle', 'explicit_fallback', 'privacy_approved')), 'TRACE_ADMISSION')
            if w['admission_basis'] == 'dream':
                episodes = view.get('episodes')
                _need(type(episodes) is list and len(episodes) >= 3 and _distinct([e.get('id') for e in episodes]) and
                      all(e.get('closed') is True and e.get('supported') is True for e in episodes), 'TRACE_DREAM')
            old = 'trace'
        elif target_status == 'fallback':
            _need(view.get('epoch_closed') is True and view.get('failure_confirmed') is True and
                  view.get('reconciled') is True and view.get('baseline_eligible') is True and
                  _digest(view.get('baseline_workflow_sha256')), 'FALLBACK_RECONCILIATION')
            baseline = owners.baseline(view['baseline_workflow_sha256'], 65536)
            _need(type(baseline) is bytes and len(baseline) <= 65536 and
                  _hash(baseline) == view['baseline_workflow_sha256'], 'FALLBACK_PIN')
            w = gate.strict_load(baseline)
            _need(w.get('status') == 'promoted' and _id(w.get('id')), 'FALLBACK_ELIGIBILITY')
            stats = w['stats']
        elif target_status == 'retired':
            history = view.get('online_outcomes')
            _need(type(history) is list and len(history) <= policy.evidence_rows and _distinct([r.get('id') for r in history]), 'RETIREMENT_HISTORY')
            streak = 0
            for row in history:
                _need(row.get('kind') in ('success', 'failure', 'outage', 'cancelled', 'missing', 'torn'), 'RETIREMENT_OUTCOME')
                if row['kind'] == 'success': streak = 0
                elif row['kind'] == 'failure': streak += 1
            stats['online_fail_streak'] = streak
            _need((streak >= policy.retirement or view.get('unsupported_confirmed') is True or
                  view.get('unresolved_safety_privacy') is True) and view.get('routability_expired') is True, 'RETIREMENT')
        else:
            stats = _metrics(w, view, target_status, policy)
        w = {**w, 'status': w['status'] if target_status == 'fallback' else target_status, 'stats': stats}
        raw = (gate.canon(w) + '\n').encode()
        with tempfile.TemporaryDirectory(dir=owners.validation_root) as tmp:
            path = Path(tmp) / (w['id'] + '.json')
            path.write_bytes(raw)
            _need(_passed(gate.validate_file(path, owners.bundle, context=owners.context)), 'CANDIDATE_GATE')
        return TransitionResult(None, None, old, target_status, tuple(sorted(stats.items())), False, None,
                                (), raw)


# @anchor:wa.learn.mw_lifecycle.record_online_outcome
def record_online_outcome(state, receipt, *, owners, policy):
    """Authenticate outcome identity; outages fence the episode without retiring history."""
    with _capacity(policy):
        _time(owners.clock, policy)
        _policy_key(state.key, policy)
        _need(_admitted(owners.check(state.key)), 'OUTCOME_AUTHORITY', Reason.FOREIGN)
        view = owners.outcome(receipt, state.key)
        _need(type(view) is dict and set(view) == {'id', 'kind', 'check'} and
              _id(view['id']) and _admitted(view['check']), 'OUTCOME_AUTH')
        if view['id'] in state.consumed:
            return state
        _need(len(state.consumed) < policy.evidence_rows, 'EVIDENCE_CAPACITY', Reason.CAPACITY)
        kind = view['kind']
        _need(kind in ('success', 'failure', 'outage', 'cancelled', 'torn', 'missing', 'unsupported', 'privacy'), 'OUTCOME_KIND')
        failures = () if kind == 'success' else state.online_failures
        if kind == 'failure':
            failures += (view['id'],)
        retired = state.retired or len(failures) >= policy.retirement or kind in ('unsupported', 'privacy')
        updated = replace(state, consumed=state.consumed + (view['id'],), online_failures=failures,
                          online_history=state.online_history + ((view['id'], kind),),
                          quarantine=retired or kind != 'success', barrier=retired or kind != 'success', handoff=retired,
                          retired=retired, routable=not retired)
        return _state_save(state, updated, owners, policy)


def _safe_root(root):
    root = Path(root).absolute()
    _need(root.is_dir() and all(not p.is_symlink() for p in (root, *root.parents)), 'STORE_ROOT')
    return root


def _metadata_path(name):
    return (type(name) is str and re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.(?:json|jsonl|md)', name) is not None)


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        _need(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'READ_REGULAR')
        _need(os.fstat(stream.fileno()).st_size <= limit, 'READ_CAPACITY', Reason.CAPACITY)
        raw = stream.read(limit + 1)
    _need(len(raw) <= limit, 'READ_CAPACITY', Reason.CAPACITY)
    return raw


def _selector(root, policy):
    raw = _read(root / 'selector.json', policy.state_bytes)
    value = gate.strict_load(raw)
    _need(type(value) is dict and set(value) == {'schema', 'generation', 'manifest_digest', 'revision'} and
          value['schema'] == 'mwp6.selector.v1' and _id(value['generation']) and
          _digest(value['manifest_digest']) and _counter(value['revision']) and
          raw == (gate.canon(value) + '\n').encode(), 'SELECTOR')
    return value


# @anchor:wa.learn.mw_lifecycle.pin_generation
def pin_generation(*, owners, policy):
    """Owner lease must outlive reads; re-selection never changes this pin."""
    with _capacity(policy):
        root = _safe_root(owners.root)
        selector = _selector(root, policy)
        handle = owners.pin(selector)
        pin = GenerationPin(selector['generation'], selector['manifest_digest'], selector['revision'], handle)
        _need(_admitted(owners.check(pin)), 'PIN_AUTHORITY', Reason.STALE)
        _read_generation(pin, owners=owners, policy=policy)
        return pin


# @anchor:wa.learn.mw_lifecycle.read_generation
def read_generation(pin, *, owners, policy):
    """Read one complete manifest; never consult the live selector during a pinned read."""
    with _capacity(policy):
        return _read_generation(pin, owners=owners, policy=policy)


def _read_generation(pin, *, owners, policy):
    """Requires the caller to hold this operation's capacity reservation."""
    _need(type(pin) is GenerationPin and _admitted(owners.read_check(pin)), 'READ_LEASE')
    root = _safe_root(owners.root)
    directory = root / pin.generation
    _need(directory.is_dir() and not directory.is_symlink(), 'GENERATION_PATH')
    manifest = _read(directory / 'manifest.json', policy.state_bytes)
    _need(_hash(manifest) == pin.manifest_digest, 'MANIFEST_HASH')
    view = gate.strict_load(manifest)
    _need(type(view) is dict and set(view) == {'schema', 'files'} and
          view['schema'] == 'mwp6.generation.v1' and manifest == (gate.canon(view) + '\n').encode() and type(view['files']) is dict and
          0 < len(view['files']) <= policy.generation_files, 'MANIFEST')
    result, total = {}, 0
    for name, digest in view['files'].items():
        _need(_metadata_path(name) and _digest(digest), 'GENERATION_FILE')
        path = directory / 'artifacts' / name
        local_parents = path.relative_to(directory).parts[:-1]
        cursor = directory
        for part in local_parents:
            cursor = cursor / part
            _need(not cursor.is_symlink(), 'GENERATION_SYMLINK')
        raw = _read(path, min(65536, policy.generation_bytes))
        total += len(raw)
        _need(total <= policy.generation_bytes and _hash(raw) == digest, 'FILE_HASH')
        result[name] = raw
    return result


# @anchor:wa.learn.mw_lifecycle.commit_generation
def commit_generation(candidate, *, expected_pin, owners, policy, fault_hook=None):
    """Atomic publication. owner: root, writer(), check/read_check/pin, admit,
    context(stage), bundle, clock, attest(). Failed staging is retained privately;
    no automatic deletion can violate reader/rollback retention. The owner admits
    retained generation count before staging. Post-publication faults return a
    committed result, never an unchanged-transaction claim.

    The retention scan counts every immediate nonsymlink directory, including
    incomplete crashed staging. Crashed staging remains private, never deleted
    automatically. Operator maintenance must exclude concurrent writers, verify
    the selector and selected manifest, and obtain trusted confirmation that a
    directory is neither selected, pinned nor required for rollback/evidence
    retention. Only then may that confirmed abandoned directory be removed.
    If custody cannot be established, retain it and stop; do not increase limits
    or alter the selector. A catalogued cleanup command is AUSENTE — no inventar.
    """
    with _capacity(policy), owners.writer():
        _time(owners.clock, policy)
        _need(type(expected_pin) is GenerationPin and _admitted(owners.attest()) and
              _admitted(owners.check(expected_pin)), 'STORE_AUTHORITY', Reason.STALE)
        root = _safe_root(owners.root)
        retained = 0
        with os.scandir(root) as entries:
            for entry in entries:
                _need(not entry.is_symlink(), 'STORE_SYMLINK')
                retained += entry.is_dir(follow_symlinks=False)
                _need(retained < policy.retained_generations, 'GENERATION_RETENTION', Reason.CAPACITY)
        old = _selector(root, policy)
        _need((old['generation'], old['manifest_digest'], old['revision']) ==
              (expected_pin.generation, expected_pin.manifest_digest, expected_pin.selector_revision), 'EXPECTED_PIN')
        _need(type(candidate) is dict and 0 < len(candidate) <= policy.generation_files, 'GENERATION_FILES')
        _need(all(_metadata_path(k) and type(v) is bytes and len(v) <= 65536 for k, v in candidate.items()),
              'GENERATION_CONTENT')
        candidate = dict(candidate)  # freeze the caller mapping before staging
        _need(sum(map(len, candidate.values())) <= policy.generation_bytes, 'GENERATION_BYTES', Reason.CAPACITY)
        _need(_admitted(owners.admit(candidate, expected_pin, policy.retained_generations)), 'GENERATION_ADMISSION')
        hit = fault_hook if fault_hook is not None else lambda _: None
        hit('staging_creation')
        stage = Path(tempfile.mkdtemp(prefix='generation-', dir=root))
        artifacts = stage / 'artifacts'
        artifacts.mkdir()
        manifest = {}
        for name, raw in sorted(candidate.items()):
            hit('workflow_write')
            path = artifacts / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, raw)
            hit('workflow_sync')
            manifest[name] = _hash(raw)
        manifest_raw = (gate.canon({'schema': 'mwp6.generation.v1', 'files': manifest}) + '\n').encode()
        _need(len(manifest_raw) <= policy.state_bytes, 'MANIFEST_CAPACITY')
        hit('manifest_write')
        _write(stage / 'manifest.json', manifest_raw)
        hit('manifest_sync')
        hit('directory_sync')
        for directory in sorted((p for p in artifacts.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            _sync_dir(directory)
        _sync_dir(artifacts)
        _sync_dir(stage)
        _sync_dir(root)
        hit('validation')
        _need(_passed(gate.validate_directory(artifacts, owners.bundle, context=owners.context(artifacts))), 'GENERATION_GATE')
        hit('hash_reread')
        pin = GenerationPin(stage.name, _hash(manifest_raw), old['revision'] + 1, expected_pin.handle)
        _need(_read_generation(pin, owners=owners, policy=policy) == candidate, 'REREAD')
        selector = {'schema': 'mwp6.selector.v1', 'generation': stage.name,
                    'manifest_digest': pin.manifest_digest, 'revision': pin.selector_revision}
        selector_raw = (gate.canon(selector) + '\n').encode()
        temp_selector = stage / 'selector.pending'
        _write(temp_selector, selector_raw)
        hit('authority_selector_recheck')
        _time(owners.clock, policy)
        _need(_admitted(owners.check(expected_pin)) and _selector(root, policy) == old, 'PUBLICATION_RECHECK')
        hit('selector_replacement')
        os.replace(temp_selector, root / 'selector.json')
        try:
            _sync_dir(root)
            hit('postpublication')
        except Exception:
            # Publication already happened; caller must reconcile durability.
            return TransitionResult(old['generation'], stage.name, 'selected', 'selected', (), True,
                                    Reason.RECONCILIATION)
        return TransitionResult(old['generation'], stage.name, 'selected', 'selected', (), True, None)
