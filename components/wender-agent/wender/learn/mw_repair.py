# @module {"id":"wa.learn.mw_repair","layer":2,"owner":"LEARN","deps":["wa.learn.mw_lifecycle","wa.tools.memoryweb_gate","wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Inactive bounded JSON patch repair, with frozen evidence and first-touch undo.

Origin: mwp6_w.md / MEMORYWEB_PROTOCOL.md. Numeric caps are [TARGET].
No runner import: shadow_executor is injected at composition time, with exact
runner keyword arguments. Owner ports below cannot be supplied by workflow JSON.
"""
from dataclasses import replace
from pathlib import Path
import re
import tempfile

from tools import memoryweb_gate as gate
from wender.learn import mw_lifecycle as local
from wender.learn.mw_lifecycle import Decision, RepairState
from wender.scripts.types import Reason


# @anchor:wa.learn.mw_repair.diagnose
def diagnose(evidence, *, owner, policy):
    """Trusted view: classification, nominal_pass, mutant IDs/rejections, check.
    Counts are optional; no ranking is invented when counts are absent.
    """
    with local._capacity(policy):
        return _diagnose(evidence, owner=owner, policy=policy)


def _diagnose(evidence, *, owner, policy):
    """Requires the caller to hold this operation's capacity reservation."""
    view = owner.diagnosis(evidence)
    local._need(type(view) is dict and set(view) == {'classification', 'nominal_pass', 'mutants', 'check'} and
                local._admitted(view['check']), 'DIAGNOSIS_AUTH')
    local._need(view['classification'] in ('observation_failure', 'workflow_fault', 'stale_knowledge',
                                          'invalid_evidence', 'oracle_fault'), 'CLASSIFICATION')
    mutants = view['mutants']
    local._need(type(mutants) is dict and len(mutants) >= policy.mutants and
                all(local._id(k) and v is False for k, v in mutants.items()) and view['nominal_pass'] is True,
                'ORACLE_QUALIFICATION')
    return view['classification']


# @anchor:wa.learn.mw_repair.apply_patch
def apply_patch(workflow_bytes, patch_bytes, *, policy):
    """Detached test/replace only. Every replacement requires its prior test.
    Full semantic candidate validation is mandatory in repair_candidate.
    """
    with local._capacity(policy):
        return _apply_patch(workflow_bytes, patch_bytes, policy=policy)


def _apply_patch(workflow_bytes, patch_bytes, *, policy):
    """Requires the caller to hold this operation's capacity reservation."""
    local._need(type(workflow_bytes) is bytes and len(workflow_bytes) <= 65536 and
                type(patch_bytes) is bytes and len(patch_bytes) <= policy.patch_bytes, 'PATCH_BYTES', Reason.CAPACITY)
    workflow, patch = gate.strict_load(workflow_bytes), gate.strict_load(patch_bytes)
    local._need(workflow_bytes == (gate.canon(workflow) + '\n').encode() and
                type(workflow) is dict and type(patch) is list and 0 < len(patch) <= policy.patch_operations and
                patch_bytes == (gate.canon(patch) + '\n').encode(),
                'PATCH_CANONICAL')
    tested = set()
    for op in patch:
        local._need(type(op) is dict and set(op) == {'op', 'path', 'value'} and
                    op['op'] in ('test', 'replace') and type(op['path']) is str, 'PATCH_OPERATION')
        match = re.fullmatch(r'/steps/(0|[1-9][0-9]*)/(target|fallback_targets|locate_wait_ms|locate_poll_ms)', op['path'])
        local._need(match is not None, 'PATCH_PATH')
        index, field = int(match[1]), match[2]
        local._need(type(workflow.get('steps')) is list and index < len(workflow['steps']) and
                    field in workflow['steps'][index], 'PATCH_INDEX')
        step = workflow['steps'][index]
        if op['op'] == 'test':
            local._need(type(step[field]) is type(op['value']) and step[field] == op['value'], 'PATCH_PREIMAGE', Reason.CONFLICT)
            tested.add(op['path'])
        else:
            local._need(op['path'] in tested, 'EXPECTED_VALUE_TEST')
            tested.remove(op['path'])
            if field.startswith('locate_'):
                local._need(type(op['value']) is int and 0 < op['value'] <= step[field], 'TIMING_ENVELOPE')
            step[field] = op['value']
    raw = (gate.canon(workflow) + '\n').encode()
    local._need(len(raw) <= 65536, 'CANDIDATE_BYTES', Reason.CAPACITY)
    return raw


# @anchor:wa.learn.mw_repair.repair_candidate
def repair_candidate(state, patch_bytes, *, owners, bundle, policy, clock):
    """Owner ports: check/save, candidate_identity, base, preimage/store_preimage,
    active_pin, diagnosis, support, validation_context, staging_root,
    shadow_authorization, run_owners, limits, shadow_executor, evaluation.
    evaluation returns a closed authenticated frozen-inventory view. The owner
    ensures isolated reversible tasks; irreversible dispatch is never duplicated.
    """
    with local._capacity(policy):
        started = local._time(clock, policy)
        local._policy_key(state.key, policy)
        local._need(type(state) is RepairState and local._admitted(owners.check(state.key)), 'REPAIR_AUTHORITY')
        if state.candidate_count >= policy.candidates or state.quarantine:
            stopped = local._state_save(state, replace(state, quarantine=True), owners, policy)
            return stopped, Decision('handoff', Reason.CAPACITY, 5, 'REPAIR_EXHAUSTED'), None
        identity = owners.candidate_identity(state)
        local._need(local._id(identity) and identity not in state.attempted, 'CANDIDATE_ID')
        charged = replace(state, candidate_count=state.candidate_count + 1, attempted=state.attempted + (identity,))
        charged = local._state_save(state, charged, owners, policy)  # charge before base read or parse
        base, original_pin = None, owners.active_pin()
        try:
            base = owners.base(state.base_digest, 65536)
            local._need(type(base) is bytes and len(base) <= 65536 and local._hash(base) == state.base_digest, 'BASE_PIN')
            if charged.preimage_digest is None:
                ref = owners.store_preimage(base, state.base_digest)
                local._need(local._id(ref), 'PREIMAGE_REF')
                updated = replace(charged, preimage_digest=state.base_digest, preimage_ref=ref)
                charged = local._state_save(charged, updated, owners, policy)
            local._need(local._hash(owners.preimage(charged.preimage_ref, 65536)) == charged.preimage_digest,
                        'FIRST_TOUCH_UNDO')
            classification = _diagnose(identity, owner=owners, policy=policy)
            local._need(classification in ('workflow_fault', 'stale_knowledge'), 'REPAIR_DIAGNOSIS')
            raw = _apply_patch(base, patch_bytes, policy=policy)
            local._need(local._admitted(owners.support(base, raw, state)), 'PATCH_SUPPORT')
            local._need(owners.run_owners.mode == 'shadow' and
                        local._admitted(owners.shadow_authorization(state, identity)) and
                        local._admitted(owners.shadow_limits(state, policy, started)), 'SHADOW_CUSTODY')
            with tempfile.TemporaryDirectory(dir=owners.staging_root) as tmp:
                workflow = gate.strict_load(raw)
                local._need(local._id(workflow.get('id')), 'CANDIDATE_ID')
                path = Path(tmp) / (workflow['id'] + '.json')
                path.write_bytes(raw)
                local._need(local._passed(gate.validate_file(path, bundle, context=owners.validation_context)), 'CANDIDATE_GATE')
                local._time(clock, policy)
                run = owners.shadow_executor(path, bundle=bundle, owners=owners.run_owners, limits=owners.limits)
                view = owners.evaluation(run, charged)
            local._need(type(view) is dict and set(view) == {'check', 'passing', 'suite_digest', 'oracle_digest',
                        'profile_digest', 'support_digest', 'protected', 'support', 'privacy', 'nominal'} and
                        local._admitted(view['check']), 'EVALUATION_AUTH')
            local._need(all(view[n] == getattr(state, n) for n in
                            ('suite_digest', 'oracle_digest', 'profile_digest', 'support_digest')), 'FROZEN_EVALUATION')
            passing = view['passing']
            local._need(local._distinct(passing) and set(passing) <= set(state.required) and
                        set(passing) > set(state.passing), 'STRICT_PASS_GROWTH')
            local._need(all(view[n] is True for n in ('protected', 'support', 'privacy', 'nominal')), 'REPAIR_CONTROLS')
            local._need(local._time(clock, policy) < started + policy.attempt_ns, 'CANDIDATE_DEADLINE', Reason.DEADLINE)
            local._need(owners.active_pin() == original_pin and
                        local._hash(owners.preimage(charged.preimage_ref, 65536)) == charged.preimage_digest,
                        'ACTIVE_UNDO')
            updated = replace(charged, passing=tuple(sorted(passing)), outcomes=charged.outcomes + ('accepted',))
            updated = local._state_save(charged, updated, owners, policy)
            return updated, Decision('shadow', None, 0, 'CANDIDATE', (identity,)), raw
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            local._need(owners.active_pin() == original_pin, 'ACTIVE_CHANGED', Reason.RECONCILIATION)
            if charged.preimage_digest is not None:
                local._need(local._hash(owners.preimage(charged.preimage_ref, 65536)) == charged.preimage_digest,
                            'UNDO_HASH', Reason.RECONCILIATION)
            updated = replace(charged, outcomes=charged.outcomes + ('rejected',))
            updated = local._state_save(charged, updated, owners, policy)
            return updated, Decision('stop', Reason.SCHEMA, 5, 'REJECTED', (identity,)), None
