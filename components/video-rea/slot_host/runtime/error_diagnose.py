# SPDX-License-Identifier: AGPL-3.0-only
"""Classify redacted failure records without resolving refs or running a slot."""

import signal
from dataclasses import dataclass

from ..projections.schema import require
from .result_custody import _PAIRED_RC

TOOL_ID = 'error.diagnose'
SUMMARY = 'Diagnose a retained operation failure from structured evidence.'
KEYWORDS = ('typed error', 'failure taxonomy', 'refusal panic', 'remediation', 'retryable')

_LABEL = {'type': 'string', 'maxLength': 128, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:-]*(?![\s\S])'}
_REF = {'type': 'string', 'maxLength': 512, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:@/-]*(?![\s\S])'}
_FLAG = {'type': 'boolean'}


def _closed(properties, *required):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(required)}


ARGUMENT = _closed({
    'operation': _LABEL,
    'attempt_ref': _REF,
    'provider_error': {'description': 'Redacted structured failure for this attempt.', **_closed({
        'source': {'enum': ['control', 'provider', 'adapter', 'target']},
        'code': _LABEL,
        'cause': _LABEL,
        'evidence_ref': _REF,
        'slot_result_ref': _REF,
    }, 'source', 'code', 'evidence_ref')},
    'exit_record': {'description': 'Host-observed exit facts for this attempt.', **_closed({
        'evidence_ref': _REF,
        'rc': {'type': ['integer', 'null'], 'minimum': -127, 'maximum': 255,
               'description': 'OS return code. A negative code is the terminating signal.'},
        'timed_out': _FLAG,
        'overflow': _FLAG,
        'control_outcome': _LABEL,
    }, 'evidence_ref')},
}, 'operation', 'attempt_ref')

RESULT = _closed({
    'slot_result_ref': {'anyOf': [_REF, {'type': 'null'}],
                        'description': 'The supplied target result reference when supported by the diagnostic input.'},
    'code': _LABEL,
    'category': {'enum': ['control_refusal', 'provider_refusal', 'provider_failure', 'adapter_failure',
                          'target_error', 'process_crash', 'uncertain_outcome', 'unknown', 'ambiguous']},
    'retryable': {'type': ['boolean', 'null']},
    'remediation': {'type': 'string'},
    'recovery': {'enum': ['reconcile_prior_attempt']},
    'evidence_refs': {'type': 'array', 'items': _REF, 'uniqueItems': True, 'maxItems': 2},
    'unknowns': {'type': 'array', 'items': _LABEL, 'uniqueItems': True},
}, 'slot_result_ref', 'code', 'category', 'retryable', 'evidence_refs', 'unknowns')


@dataclass(frozen=True)
class _Diagnosis:
    code: str
    category: str
    retryable: bool | None = False
    remediation: str | None = None
    slot_result_ref: str | None = None


_REFUSALS = {
    'HOLD': 'Check the control policy hold.',
    'HUMAN_NEEDED': 'Check the operator decision required by the control lane.',
    'OWNER': 'Check the operation owner required by the control lane.',
    'FORGE_APPROVAL_REQUIRED': 'Check the required approval in the control lane.',
    'ARGS_SCHEMA_ABSENT': 'Check the registered argument schema.',
    'NO_LEASE': 'Obtain the required host grant.',
    'PERCEPTION_DENIED': 'Check the effective perception ceiling and lease admission.',
    'POOL_TIMEOUT': 'Check the lease acquisition outcome.',
    'PROFILE_NOT_ADMITTED': 'Check the admitted profile and registered contract pins.',
    'AVAILABILITY_BLOCKED': 'Check the installation prerequisites and admission policy.',
    'AVAILABILITY_NOT_INSTALLED': 'Check the installation registration.',
    'AVAILABILITY_PARKED': 'Check route availability through host discovery.',
    'LIVE_EXECUTION_PARKED': 'Check the owner integration requirements.',
    'REQUEST_ID_CONFLICT': 'Use the original normalized request for that request ID.',
    'SCHEMA_INVALID': 'Correct the request using the published schema.',
}
_ADAPTER_FAILURES = frozenset({
    'exchange_invalid', 'evidence_binding_rejected', 'custody_unverified', 'lease_custody_unverified',
})
_TARGET_FAILURE_EXITS = _PAIRED_RC - {0}
_CRASH_EXITS = frozenset(-sig for sig in (
    signal.SIGABRT, signal.SIGBUS, signal.SIGFPE, signal.SIGILL, signal.SIGSEGV, signal.SIGSYS, signal.SIGTRAP,
))
_UNCERTAIN = _Diagnosis('UNCERTAIN_OUTCOME', 'uncertain_outcome',
                        remediation='Resolve the prior attempt through trusted reconciliation before any retry.')


def _control(code):
    if code == _UNCERTAIN.code:
        return _UNCERTAIN
    if code in _REFUSALS:
        return _Diagnosis(code, 'control_refusal', remediation=_REFUSALS[code])
    return None


def _reported_failure(error):
    source, code = error['source'], error['code']
    if source == 'control':
        return _control(code)
    if source == 'adapter' and code in _ADAPTER_FAILURES:
        return _Diagnosis(code, 'adapter_failure', remediation='Inspect the retained exchange and custody evidence.')
    if source == 'target' and 'slot_result_ref' in error:
        return _Diagnosis(code, 'target_error', slot_result_ref=error['slot_result_ref'])
    if source == 'provider':
        cause = error.get('cause')
        if cause == 'clean_room':
            return _Diagnosis('PROVIDER_REFUSED', 'provider_refusal',
                              remediation='Check the structured provider refusal details.')
        if (code, cause) == ('E_TARGET', 'claude_api_incomplete_response'):
            return _Diagnosis('PROVIDER_INCOMPLETE_RESPONSE', 'provider_failure', None)
        if code == 'provider_timeout':
            return _Diagnosis(code, 'provider_failure', None,
                              remediation='Inspect provider deadline, partial evidence and cleanup state.')
    return None


def _process_failure(record):
    if record.get('timed_out') is True or record.get('control_outcome') == _UNCERTAIN.code:
        return _UNCERTAIN
    if record.get('overflow') is True:
        return _Diagnosis('OUTPUT_OVERFLOW', 'adapter_failure',
                          remediation='Inspect the combined output cap and retained bounded diagnostics.')
    if record.get('rc') in _CRASH_EXITS and record.get('timed_out') is False and record.get('overflow') is False:
        return _Diagnosis('PROCESS_CRASH', 'process_crash', remediation='Inspect the retained exit and cleanup evidence.')
    return None


def diagnose(payload):
    """Classify supplied records. Retryability is unknown unless the record supports a refusal to retry."""
    require(payload, ARGUMENT, 'Diagnostic payload')
    error = payload.get('provider_error')
    record = payload.get('exit_record')
    refs, unknowns, findings = [], [], []
    reported = None
    conflicting = False
    if error is not None:
        refs.append(error['evidence_ref'])
        reported = _reported_failure(error)
        if reported is None:
            unknowns.append('unrecognized_reported_failure')
        else:
            findings.append(reported)
    if record is not None:
        refs.append(record['evidence_ref'])
        rc, outcome = record.get('rc'), record.get('control_outcome')
        control = _control(outcome)
        process = _process_failure(record)
        findings.extend(item for item in (control, process) if item is not None)
        if outcome not in (None, 'OK') and control is None:
            unknowns.append('unrecognized_control_outcome')
            conflicting = reported is not None or process is not None

        target_result = reported is not None and reported.category == 'target_error'
        paired_target_exit = (
            target_result and type(rc) is int and rc in _TARGET_FAILURE_EXITS
            and outcome in (None, 'OK') and process is None
        )
        if target_result and not paired_target_exit:
            conflicting = True
        if control is None and process is None and rc != 0 and not paired_target_exit:
            unknowns.append('inconclusive_process_exit')

        if error is not None and reported is None:
            # An unclassified adapter report can share the failure plane of host output overflow.
            if control is not None or (
                process is not None and (error['source'], process.category) != ('adapter', 'adapter_failure')
            ):
                conflicting = True
    distinct = set(findings)
    if conflicting or len(distinct) > 1:
        diagnosis = _Diagnosis('AMBIGUOUS', 'ambiguous')
        unknowns.append('conflicting_diagnostics')
    elif findings:
        diagnosis = findings[0]
    else:
        diagnosis = _Diagnosis('UNKNOWN', 'unknown', None)
        if not unknowns:
            unknowns.append('missing_failure_evidence')
    result = {'slot_result_ref': diagnosis.slot_result_ref, 'code': diagnosis.code,
              'category': diagnosis.category, 'retryable': diagnosis.retryable,
              'evidence_refs': list(dict.fromkeys(refs)), 'unknowns': unknowns}
    if diagnosis.remediation is not None:
        result['remediation'] = diagnosis.remediation
    if _UNCERTAIN in distinct:
        result['recovery'] = 'reconcile_prior_attempt'
    return result
