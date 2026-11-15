# @module {"id":"wa.tools.mw_watch","layer":4,"owner":"ARCH","deps":["wa.learn.mw_lifecycle","wa.scripts.types","wa.tools.memoryweb_gate"],"book":"HB-WA-s4-learn"}
"""Offline persistent observer. It never dispatches, repairs or selects workflows.

Origin: mwp6_w.md; native event framing is owned by mw_workflow's journal owner.
All numeric protocol caps are [TARGET]. Protocols below are proposed trusted
composition ports. Missing owners close the barrier, including in the CLI.
"""
import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import memoryweb_gate as gate
from wender.learn import mw_lifecycle as local
from wender.learn.mw_lifecycle import Decision, RecoveryKey, WatchState
from wender.scripts.types import AdmissionCheck, Reason


# @anchor:wa.tools.mw_watch.JournalOwner
class JournalOwner(Protocol):
    # @anchor:wa.tools.mw_watch.JournalOwner.check
    def check(self, binding) -> AdmissionCheck: ...
    # @anchor:wa.tools.mw_watch.JournalOwner.authenticate
    def authenticate(self, event, binding) -> AdmissionCheck: ...
    # @anchor:wa.tools.mw_watch.JournalOwner.recover
    def recover(self, raw, suffix) -> AdmissionCheck: ...


# @anchor:wa.tools.mw_watch.EvidenceOwner
class EvidenceOwner(Protocol):
    # @anchor:wa.tools.mw_watch.EvidenceOwner.check
    def check(self, key) -> AdmissionCheck: ...
    # @anchor:wa.tools.mw_watch.EvidenceOwner.progress
    def progress(self, state, journal): ...
    # @anchor:wa.tools.mw_watch.EvidenceOwner.authenticate
    def authenticate(self, evidence, state, journal) -> AdmissionCheck: ...
    # @anchor:wa.tools.mw_watch.EvidenceOwner.finish
    def finish(self, outcome, state, attempt) -> AdmissionCheck: ...
    # @anchor:wa.tools.mw_watch.EvidenceOwner.recovery
    def recovery(self, state, journal) -> AdmissionCheck: ...


# @anchor:wa.tools.mw_watch.StateOwner
class StateOwner(Protocol):
    # @anchor:wa.tools.mw_watch.StateOwner.check
    def check(self, key) -> AdmissionCheck: ...
    # @anchor:wa.tools.mw_watch.StateOwner.save
    def save(self, raw, expected_digest) -> AdmissionCheck: ...


# @anchor:wa.tools.mw_watch.JournalBinding
@dataclass(frozen=True)
class JournalBinding:
    key: RecoveryKey
    source_generation: str
    workflow_digest: str
    journal_generation: str
    steps: tuple
    run_id: str

    # @anchor:wa.tools.mw_watch.JournalBinding.__post_init__
    def __post_init__(self):
        local._need(type(self.key) is RecoveryKey and local._id(self.source_generation) and
                    local._digest(self.workflow_digest) and local._id(self.journal_generation) and
                    local._id(self.run_id) and type(self.steps) is tuple and self.steps and
                    all(local._positive(x) for x in self.steps) and
                    len(set(self.steps)) == len(self.steps), 'BINDING')


# @anchor:wa.tools.mw_watch.JournalView
@dataclass(frozen=True)
class JournalView:
    binding: JournalBinding
    prefix: bytes
    prefix_digest: str
    count: int
    outstanding: tuple
    torn: bool
    suffix_digest: str | None
    authentication: AdmissionCheck


# @anchor:wa.tools.mw_watch.ProgressEvidence
@dataclass(frozen=True)
class ProgressEvidence:
    identity: str
    predicate: str
    key: RecoveryKey
    workflow_digest: str
    checkpoint: str
    provenance: str

    # @anchor:wa.tools.mw_watch.ProgressEvidence.__post_init__
    def __post_init__(self):
        local._need(all(local._id(x) for x in (self.identity, self.predicate, self.checkpoint, self.provenance)) and
                    type(self.key) is RecoveryKey and local._digest(self.workflow_digest), 'PROGRESS_RECORD')


# @anchor:wa.tools.mw_watch.read_native_journal
def read_native_journal(raw, *, owner, binding, policy):
    with local._capacity(policy):
        return _read_native_journal(raw, owner=owner, binding=binding, policy=policy)


def _read_native_journal(raw, *, owner, binding, policy):
    """Requires the caller to hold this operation's capacity reservation."""
    local._need(type(binding) is JournalBinding and local._admitted(owner.check(binding)), 'JOURNAL_BINDING', Reason.FOREIGN)
    local._policy_key(binding.key, policy)
    local._need(type(raw) is bytes and len(raw) <= policy.journal_bytes, 'JOURNAL_BYTES', Reason.CAPACITY)
    end = len(raw) if not raw or raw.endswith(b'\n') else raw.rfind(b'\n') + 1
    prefix, suffix = raw[:end], raw[end:]
    local._need(len(suffix) <= policy.line_bytes, 'SUFFIX_BYTES', Reason.CAPACITY)
    seq, prev, outstanding = 0, None, set()
    for line in prefix.splitlines(keepends=True):
        local._need(len(line) <= policy.line_bytes and seq < policy.records, 'JOURNAL_LIMIT', Reason.CAPACITY)
        event = gate.strict_load(line)
        local._need(type(event) is dict and set(event) == {'seq', 'prev', 'kind', 'step_seq', 'phase', 'digest'}, 'EVENT_KEYS')
        local._need(type(event['seq']) is int and event['seq'] == seq + 1 and event['prev'] == prev and
                    local._digest(event['digest']) and line == (gate.canon(event) + '\n').encode(), 'EVENT_CHAIN')
        kind, step = event['kind'], event['step_seq']
        local._need(kind in ('intent', 'complete', 'verified', 'satisfied_skip', 'condition_skip', 'failed') and
                    type(step) is int and step in binding.steps and
                    event['phase'] == ('delivery' if kind in ('intent', 'complete') else 'terminal'), 'EVENT_PHASE')
        local._need(local._admitted(owner.authenticate(event, binding)), 'EVENT_AUTH', Reason.FOREIGN)
        if kind == 'intent':
            local._need(step not in outstanding, 'INTENT_PAIR')
            outstanding.add(step)
        elif kind == 'complete':
            local._need(step in outstanding, 'COMPLETE_PAIR')
            outstanding.remove(step)
        seq, prev = event['seq'], event['digest']
    torn = bool(suffix)
    if torn:
        # recover owns exact private suffix custody; no bytes enter decisions.
        local._need(local._admitted(owner.recover(prefix, suffix)), 'TORN_RECOVERY', Reason.RECONCILIATION)
    return JournalView(binding, prefix, local._hash(prefix), seq, tuple(sorted(outstanding)), torn,
                       local._hash(suffix) if torn else None, owner.check(binding))


# @anchor:wa.tools.mw_watch.observe
def observe(state, journal, *, evidence_owner, policy, clock):
    with local._capacity(policy):
        return _observe(state, journal, evidence_owner=evidence_owner, policy=policy, clock=clock)


def _observe(state, journal, *, evidence_owner, policy, clock):
    """Requires the caller to hold this operation's capacity reservation."""
    local._time(clock, policy)
    local._need(type(state) is WatchState and type(journal) is JournalView and
                local._admitted(journal.authentication) and local._admitted(evidence_owner.check(state.key)),
                'OBSERVATION_AUTH', Reason.FOREIGN)
    local._policy_key(state.key, policy)
    b = journal.binding
    local._need(b.key == state.key and b.workflow_digest == state.workflow_digest and
                b.source_generation == state.source_generation, 'OBSERVATION_BINDING', Reason.FOREIGN)
    prefix_matches = len(journal.prefix) >= state.cursor and local._hash(journal.prefix[:state.cursor]) == state.prefix_digest
    if b.journal_generation != state.journal_generation or not prefix_matches:
        local._need(local._admitted(evidence_owner.recovery(state, journal)), 'PREFIX_SUBSTITUTION', Reason.RECONCILIATION)
    if journal.torn:
        return replace(state, barrier=True, quarantine=True), Decision('stop', Reason.RECONCILIATION, 3, 'TORN')
    if journal.outstanding:
        return replace(state, barrier=True, quarantine=True), Decision('stop', Reason.RECONCILIATION, 5, 'INPUT_UNCERTAIN')
    # An existing startup barrier needs separate authenticated reconciliation.
    local._need(not state.retired, 'RETIRED_BARRIER', Reason.RECONCILIATION)
    if state.barrier:
        local._need(local._admitted(evidence_owner.recovery(state, journal)), 'RESTART_BARRIER', Reason.RECONCILIATION)
    rows = evidence_owner.progress(state, journal)
    local._need(type(rows) is tuple and len(rows) <= policy.evidence_rows, 'PROGRESS_LIMIT', Reason.CAPACITY)
    consumed, progress = set(state.consumed), set(state.progress)
    for e in rows:
        local._need(type(e) is ProgressEvidence and e.key == state.key and e.workflow_digest == state.workflow_digest and
                    local._admitted(evidence_owner.authenticate(e, state, journal)), 'PROGRESS_AUTH', Reason.FOREIGN)
        if e.identity not in consumed:
            consumed.add(e.identity)
            progress.add(e.predicate)
    local._need(len(consumed) <= policy.evidence_rows and len(progress) <= policy.evidence_rows, 'PROGRESS_CAPACITY')
    grew = progress > set(state.progress)
    # Authenticated observation/recovery clears quarantine; attempts remain,
    # and no-progress resets only on verified growth. begin_attempt still checks
    # attempt exhaustion, then barrier, quarantine and retired (not handoff).
    updated = replace(state, cursor=len(journal.prefix), prefix_digest=journal.prefix_digest,
                      journal_generation=b.journal_generation, consumed=tuple(sorted(consumed)),
                      progress=tuple(sorted(progress)), no_progress=0 if grew else state.no_progress,
                      barrier=False, quarantine=False)
    if updated.no_progress >= policy.attempts:
        return replace(updated, quarantine=True, handoff=True), Decision('handoff', Reason.CAPACITY, 5, 'STUCK')
    return updated, Decision('observe', None, 0, 'OBSERVED')


# @anchor:wa.tools.mw_watch.begin_attempt
def begin_attempt(state, *, state_owner, policy, clock):
    with local._capacity(policy):
        now = local._time(clock, policy)
        local._policy_key(state.key, policy)
        local._need(local._admitted(state_owner.check(state.key)), 'ATTEMPT_AUTHORITY', Reason.FOREIGN)
        if state.no_progress >= policy.attempts:
            stopped = local._state_save(state, replace(state, quarantine=True, handoff=True), state_owner, policy)
            return stopped, Decision('handoff', Reason.CAPACITY, 5, 'STUCK')
        if state.barrier or state.quarantine or state.retired:
            return state, Decision('stop', Reason.RECONCILIATION, 5, 'INPUT_UNCERTAIN')
        local._need(len(state.attempts) < policy.records, 'ATTEMPT_RECORDS', Reason.CAPACITY)
        local._need(now + policy.attempt_ns <= policy.deadline_ns, 'ATTEMPT_DEADLINE', Reason.DEADLINE)
        identity = 'attempt-' + str(len(state.attempts) + 1)
        # Persist the conservative charge and restart barrier before actor admission.
        updated = replace(state, attempts=state.attempts + (identity,),
                          attempt_started=state.attempt_started + (now,),
                          attempt_deadlines=state.attempt_deadlines + (now + policy.attempt_ns,),
                          no_progress=state.no_progress + 1, barrier=True)
        updated = local._state_save(state, updated, state_owner, policy)
        return updated, Decision('attempt', None, 0, 'ADMITTED', (identity,))


# @anchor:wa.tools.mw_watch.record_attempt
def record_attempt(state, outcome, *, evidence_owner, state_owner, policy, clock):
    """Only authenticated completion/reconciliation can open the admission barrier."""
    with local._capacity(policy):
        now = local._time(clock, policy)
        local._need(state.barrier and state.attempt_deadlines and now < state.attempt_deadlines[-1] and state.attempts and
                    local._admitted(evidence_owner.finish(outcome, state, state.attempts[-1])),
                    'ATTEMPT_COMPLETION', Reason.RECONCILIATION)
        updated = replace(state, barrier=False)
        decision = Decision('observe', None, 0, 'OBSERVED')
        if state.no_progress >= policy.attempts:
            updated = replace(updated, quarantine=True, handoff=True)
            decision = Decision('handoff', Reason.CAPACITY, 5, 'STUCK')
        return local._state_save(state, updated, state_owner, policy), decision


# @anchor:wa.tools.mw_watch.main
def main(argv=None, *, owners=None) -> int:
    parser = argparse.ArgumentParser(description='Offline authenticated journal observer')
    for name in ('journal', 'state', 'policy'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--polls', type=int, default=1)
    args = parser.parse_args(argv)
    if owners is None:
        return 5  # OUT_OF_SUPPORT; no permissive CLI authentication adapter.
    try:
        policy = owners.policy(args.policy)
        local._need(type(policy) is local.RecoveryPolicy and 0 < args.polls <= policy.records, 'POLL_LIMIT')
        with local._capacity(policy):
            state = local._decode_state(local._read(args.state, policy.state_bytes), policy=policy)
            for index in range(args.polls):
                journal = _read_native_journal(local._read(args.journal, policy.journal_bytes), owner=owners.journal,
                                              binding=owners.binding, policy=policy)
                updated, decision = _observe(state, journal, evidence_owner=owners.evidence, policy=policy, clock=owners.clock)
                state = local._state_save(state, updated, owners.state, policy)
                local._need(local._admitted(owners.decision(decision)), 'DECISION_CUSTODY')
                if decision.rc:
                    return decision.rc
                if index + 1 < args.polls:
                    local._time(owners.clock, policy)
                    owners.sleep(policy.poll_ns)
            return 0
    except local._Refusal as exc:
        return 2 if exc.reason is Reason.SCHEMA else 5
    except ValueError:
        return 2
    except (OSError, TypeError, AttributeError):
        return 5


if __name__ == '__main__':
    raise SystemExit(main())
