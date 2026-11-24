# SPDX-License-Identifier: AGPL-3.0-only
"""Register comparator mini-tools with the common REA projection and control lanes."""

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ._evaluate import evaluate
from ._model import Comparison, is_digest, is_identifier
from ._visual import Vision


_SCHEMA_VERSION = 'https://json-schema.org/draft/2020-12/schema'
_BASE = 'https://viewpoint.local/contracts/rea/comparators/'
_PUBLIC_BASE = 'https://viewpoint.local/contracts/rea/projections/host/'
_PROFILE = 'https://viewpoint.local/contracts/rea/rea.slot_evidence_profile_v1.schema.json'
_REQUEST_ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$'
_ROUTES = {
    'behaviour.compare': ('Compare retained structured observations and scoped waivers.',
                          ('behaviour', 'differential', 'coverage', 'waiver'), 'none'),
    'timeline.compare': ('Compare event coverage, timing and precedence on bound clocks.',
                         ('timeline', 'occurrence', 'events', 'timing'), 'none'),
    'visual.compare': ('Compare encoded presentation times and decoded regions under a vision lease.',
                       ('visual', 'frame', 'PTS', 'pixels'), 'vision'),
}
_REFS = ('reference_ref', 'candidate_ref', 'policy_ref')


@dataclass(frozen=True)
class VerifiedComparisonReceipt:
    """Identity obtained from host-authorized evidence.inspect readback.

    The adapter supplies this only after validating the pinned shared receipt
    schema, the receipt digest and disclosure authority for the caller.
    """

    receipt_ref: str
    attempt_id: str
    request_id: str
    artifact_ref: str
    artifact_sha256: str


@dataclass(frozen=True)
class RetainedComparison:
    artifact_ref: str
    artifact_sha256: str
    receipt: VerifiedComparisonReceipt


def _comparison_digest(document):
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('ascii')).hexdigest()


@dataclass(frozen=True)
class _ComparisonReportWitness:
    receipt: VerifiedComparisonReceipt
    reference_episode_id: str
    candidate_episode_id: str
    policy_sha256: str
    verdict_sha256: str
    slot_result_digest: str


class ComparisonHost(Protocol):
    """Ports of the authenticated host registry and shared evidence store.

    The control lane authorizes the context, opens the attempt and claims
    the normalized request in its existing RequestStore. The retention port
    receives the full restricted artifact. A live adapter returns persisted
    artifact identity and verified authorized receipt readback. Each port
    can receive distinct owner-configured runner and inspector clients.
    A string artifact reference supports synthetic invocations without an attempt ID.

    A host that consumes comparison custody can implement
    receive_comparison_witness. That port requires resolved episode IDs and
    a digest of the report encoded as sorted, compact ASCII JSON. The witness
    stays in host custody and is excluded from the public result.
    """

    def resolve_comparison(self, tool_id: str, request_id: str, *, reference_ref: str,
                           candidate_ref: str, policy_ref: str, runner_client=None,
                           inspector_client=None) -> Comparison: ...

    def verify_comparison_sources(self, tool_id: str, request_id: str, comparison: Comparison,
                                  sources: dict) -> bool: ...

    def vision_for(self, tool_id: str, request_id: str) -> Vision | None: ...

    def retain_comparison(self, tool_id: str, request_id: str, report: dict, *,
                          runner_client=None, inspector_client=None) -> RetainedComparison | str: ...


def _text_ref(value):
    return type(value) is str and 0 < len(value) <= 512 and bool(value.strip())


def _payload(payload):
    if type(payload) is not dict or payload.keys() != set(_REFS):
        raise ValueError('payload requires exactly reference_ref, candidate_ref and policy_ref.')
    if any(not _text_ref(payload[key]) for key in _REFS):
        raise ValueError('Comparison references must be nonblank strings of at most 512 characters.')
    for key in _REFS:
        payload[key].encode('utf-8')
    return dict(payload)


def _result_fields(tool_id, report):
    """Project details that belong to admitted checks."""
    identity = lambda row: (row['check_id'], row['kind'], row['outcome'])
    admitted = {identity(row) for row in report['verdict']['checks']}
    by_kind = {row['kind']: row['diagnostics'] or {} for row in report['diagnostics']['checks']
               if identity(row) in admitted}
    fields = {}
    if tool_id == 'behaviour.compare':
        detail = by_kind.get('structured_value', {})
        fields['exception_refs'] = [row['waiver_ref'] for row in detail.get('accepted_differences', [])]
        if 'structured_value' in by_kind:
            fields['coverage'] = {key: detail[key] for key in (
                'reference_count', 'candidate_count', 'reference_complete', 'candidate_complete') if key in detail}
            fields['deltas'] = list(detail.get('mismatches', []))
    elif tool_id == 'timeline.compare' and 'event_timeline' in by_kind:
        detail = by_kind['event_timeline']
        fields['deltas'] = list(detail.get('events', []))
        fields['order_conflicts'] = list(detail.get('order_conflicts', []))
        fields['coverage'] = {key: detail[key] for key in (
            'reference_count', 'candidate_count', 'reference_complete', 'candidate_complete',
            'reference_missing_kinds', 'candidate_missing_kinds') if key in detail}
    elif tool_id == 'visual.compare':
        if 'presentation_time' in by_kind:
            detail = by_kind['presentation_time']
            fields['aligned_occurrences'] = list(detail.get('pairs', []))
            fields['coverage'] = {key: detail[key] for key in ('reference_count', 'candidate_count') if key in detail}
        if 'pixels' in by_kind:
            fields['spatial_difference'] = list(by_kind['pixels'].get('frames', []))
    return fields


def _retained_refs(retained, attempt_id, request_id):
    if attempt_id is None and _text_ref(retained):
        return {'artifact_ref': retained}
    if (not is_identifier(attempt_id) or type(retained) is not RetainedComparison
            or not _text_ref(retained.artifact_ref) or not is_digest(retained.artifact_sha256)):
        raise ValueError('The store returned no verified comparison receipt for the opened attempt.')
    receipt = retained.receipt
    if (type(receipt) is not VerifiedComparisonReceipt or not _text_ref(receipt.receipt_ref)
            or receipt.receipt_ref == retained.artifact_ref or receipt.attempt_id != attempt_id
            or receipt.request_id != request_id or receipt.artifact_ref != retained.artifact_ref
            or receipt.artifact_sha256 != retained.artifact_sha256):
        raise ValueError('Authorized receipt readback does not bind the opened attempt and retained artifact.')
    return {'artifact_ref': retained.artifact_ref, 'receipt_ref': receipt.receipt_ref}


def _retention_start(retention, resolved, request_id, payload, host, tool_id):
    from ..runtime import evidence_projection as projection
    from ..runtime.receipts import _shared_adapter

    verify_sources = getattr(host, "verify_comparison_sources", None)
    if not callable(verify_sources):
        raise ValueError("comparison retention is parked pending owner source verification")
    attempt = retention.admit_comparison(
        attempt_id=resolved.attempt_id, request_id=request_id, request_sha256=resolved.request_sha256,
        reference=resolved.reference_attempt_id, candidate=resolved.candidate_attempt_id,
    )
    sources = {side: retention.read_source(payload[f"{side}_ref"]) for side in ("reference", "candidate")}
    for side, source in sources.items():
        inspection = source.inspection
        if (inspection["attempt_id"] != attempt.admission[f"{side}_attempt_id"]
                or source.profile["episode"]["episode_id"] != getattr(resolved, side).episode_id
                or source.profile["attempt"].get("control_outcome") is not None
                or inspection["terminal"]["state"] != "observed"
                or inspection["terminal"]["cleanup"]["state"] != "confirmed_retired"
                or source.profile["observation"]["producer"]["process_bound"] is not True):
            raise ValueError("comparison source differs from the admitted, independently retained observation")
    # The owner resolver must bind its structured observations and tape to
    # these verified sources, including channels unrepresented in v1.
    if verify_sources(tool_id, request_id, resolved, sources) is not True:
        raise ValueError("comparison resolver did not bind the exact retained inputs")
    profile = {
        "attempt": {"attempt_id": resolved.attempt_id, "request_id": request_id,
                    "operation": "compare", "opened_before_dispatch": True},
        "episode": {"episode_id": f"ep-{resolved.attempt_id}", "role": "comparison",
                    "reference_episode_id": resolved.reference.episode_id,
                    "candidate_episode_id": resolved.candidate.episode_id},
        "slot": copy.deepcopy(sources["candidate"].profile["slot"]),
        "shared_receipt_adapter": _shared_adapter(projection.V03_REF, projection.V03_SHA),
    }
    started = projection.build_compare_started(
        profile, executor_id=attempt.admission["executor_id"],
        reference_attempt_id=resolved.reference_attempt_id, candidate_attempt_id=resolved.candidate_attempt_id,
        request_sha=attempt.admission["request_sha256"], retained=True,
    )
    attempt.start(started)
    return attempt, started, sources, profile


def _retention_finish(state, report):
    from ..runtime import evidence_projection as projection
    from ..runtime.receipts import read_object

    attempt, started, sources, profile = state
    profile["observation"] = copy.deepcopy(report["observation"])
    if profile["observation"]["producer"]["process_bound"] is not False:
        raise ValueError("retained comparison requires a host producer")
    profile["observation"]["sources"] = [
        {"side": side, "episode_id": source.profile["episode"]["episode_id"],
         "perception_level": source.profile["observation"]["perception_level"],
         "producer": source.profile["observation"]["producer"],
         "receipt_ref": source.inspection["receipt_ref"]}
        for side, source in sources.items()
    ]
    profile["verdict"] = copy.deepcopy(report["verdict"])
    recordings = {}
    checks = {check["kind"] for check in report["verdict"]["checks"]}
    channels = []
    if checks & {"pixels", "presentation_time"}:
        channels.append("media")
    if "event_timeline" in checks:
        channels.append("timeline")
    for side, source in sources.items():
        source_side = "reference" if source.profile["attempt"]["operation"] == "record" else "candidate"
        for field in channels:
            value = source.profile.get(field, {}).get(source_side)
            if value is not None:
                profile.setdefault(field, {})[side] = value
        if side in profile.get("media", {}):
            entry = next(entry for entry in source.index["objects"]
                         if entry["role"] == "recording" and entry["side"] == source_side)
            with attempt.inspector_client.session(attempt.tenant) as inspector:
                recordings[side] = read_object(inspector, source.inspection["attempt_id"], entry, collect=True)
    raw_report = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if attempt.custody is not None:
        attempt.custody.report(attempt, raw_report)
    proof = attempt.observation(started, {"comparison_sha256": hashlib.sha256(raw_report).hexdigest()})
    if proof.no_spawn is not False or proof.process is not None:
        raise ValueError("comparison evidence must bind the host evaluation")
    if (proof.captured_sha256 != hashlib.sha256(raw_report).hexdigest()
            or proof.cleanup_retirement_sha256 is None):
        raise ValueError("comparison evidence does not bind the report and retirement")
    terminal = projection.build_compare_observed(
        profile, started, retained=True, **proof.terminal_fields(cleanup_observed=True),
    )
    readback = attempt.finish(
        started, terminal, profile, recordings=recordings, payloads={"comparison": raw_report},
    )
    inspection = readback.inspection
    artifact = inspection["artifact_ref"]
    digest = inspection["terminal"]["artifact"]["sha256"]
    return RetainedComparison(artifact, digest, VerifiedComparisonReceipt(
        inspection["receipt_ref"], inspection["attempt_id"], attempt.admission["request_id"], artifact, digest,
    )), hashlib.sha256(raw_report).hexdigest()


def comparison_handoff(retention):
    """Reduce the coordinator's configuration to the host comparison ports."""
    # The import stays local so offline comparators do not load the runtime.
    from ..runtime.retention import ComparisonHandoff, RetentionClients

    if isinstance(retention, RetentionClients):
        return retention.comparison_handoff()
    if isinstance(retention, ComparisonHandoff):
        return retention
    raise ValueError('Comparison retention requires the owner-approved admission adapter.')


def handle(tool_id, payload, *, request_id, host, runner_client=None, inspector_client=None, retention=None):
    """Execute a host-authorized mini-tool and return its host_result payload."""
    if tool_id not in _ROUTES:
        raise ValueError('Unknown comparator mini-tool.')
    if type(request_id) is not str or re.fullmatch(_REQUEST_ID_PATTERN, request_id) is None:
        raise ValueError('Invalid comparison request id.')
    clients = {}
    if runner_client is not None or inspector_client is not None:
        if runner_client is None or inspector_client is None or runner_client is inspector_client:
            raise ValueError('Separate runner and inspector clients are required.')
        clients = {'runner_client': runner_client, 'inspector_client': inspector_client}
    payload = _payload(payload)
    if retention is not None:
        retention = comparison_handoff(retention)
        supplied = {'runner_client': retention.runner.client, 'inspector_client': retention.inspector.client}
        if clients and any(clients[key] is not value for key, value in supplied.items()):
            raise ValueError('Comparison clients differ from the owner-approved retention credentials.')
        clients = supplied
    elif clients:
        raise ValueError('Comparison retention requires the owner-approved admission adapter.')
    resolved = host.resolve_comparison(tool_id, request_id, **payload, **clients)
    if resolved.attempt_id is not None and not is_identifier(resolved.attempt_id):
        raise ValueError('The host returned an invalid comparison attempt identity.')
    state = None
    if retention is not None:
        if tool_id == "visual.compare" and resolved.vision_authorized is True:
            raise ValueError("owner-pending: T06 (retained vision custody and profile approval)")
        state = _retention_start(retention, resolved, request_id, payload, host, tool_id)
    vision = None
    if (tool_id == 'visual.compare' and resolved.perception_ceiling == 'vision'
            and resolved.vision_authorized is True):
        vision = host.vision_for(tool_id, request_id)
    report = evaluate(tool_id, resolved, vision=vision)
    if state is not None:
        # The frozen record describes a host composition with no child process.
        report['observation']['producer']['process_bound'] = False
    fields = _result_fields(tool_id, report)
    report['result_fields'] = fields
    receive_witness = getattr(host, 'receive_comparison_witness', None)
    if receive_witness is not None:
        reference_id, candidate_id = resolved.reference.episode_id, resolved.candidate.episode_id
        if (resolved.attempt_id is None or not is_identifier(reference_id)
                or not is_identifier(candidate_id) or reference_id == candidate_id
                or not is_digest(resolved.policy.sha256)):
            raise ValueError('Comparison custody requires distinct resolved episodes and a pinned policy.')
        report['binding'] = {
            'attempt_id': resolved.attempt_id, 'request_id': request_id,
            'reference_episode_id': reference_id, 'candidate_episode_id': candidate_id,
            'policy_sha256': resolved.policy.sha256,
            'slot_result_digest': _comparison_digest(resolved.slot_result),
        }
        report_sha256 = _comparison_digest(report)
        verdict_sha256 = _comparison_digest(report['verdict'])
    if state is None:
        retained = host.retain_comparison(tool_id, request_id, report)
        retained_report_sha = retained.artifact_sha256 if type(retained) is RetainedComparison else None
    else:
        retained, retained_report_sha = _retention_finish(state, report)
    refs = _retained_refs(retained, resolved.attempt_id, request_id)
    if receive_witness is not None:
        if retained_report_sha != report_sha256 or _comparison_digest(report) != report_sha256:
            raise ValueError('Authorized readback does not bind the exact comparison report.')
        receive_witness(_ComparisonReportWitness(
            retained.receipt, reference_id, candidate_id, resolved.policy.sha256,
            verdict_sha256, report['binding']['slot_result_digest'],
        ))
    return {**refs, 'verdict': report['verdict'], 'observation': report['observation'], **fields}


def control_request(tool_id, arguments, *, context):
    """Lift CLI or MCP wrapper arguments into the shared control lane."""
    if tool_id not in _ROUTES:
        raise ValueError('Unknown comparator mini-tool.')
    if (type(arguments) is not dict or not {'request_id', 'payload'} <= arguments.keys()
            or not arguments.keys() <= {'request_id', 'payload', 'dry_run'}):
        raise ValueError('Invalid comparison arguments.')
    request_id = arguments['request_id']
    if type(request_id) is not str or re.fullmatch(_REQUEST_ID_PATTERN, request_id) is None:
        raise ValueError('Invalid comparison request id.')
    dry_run = arguments.get('dry_run', False)
    if type(dry_run) is not bool:
        raise ValueError('dry_run must be boolean.')
    return {
        'schema': 'vp.control.req/v1', 'request_id': request_id, 'op': 'rea.slot.project',
        'context': context, 'dry_run': dry_run,
        'args': {'route_id': f'host.{tool_id}', 'arguments': {'payload': _payload(arguments['payload'])}},
    }


def _handler(tool):
    def dispatch(payload, *, request_id, host, runner_client=None, inspector_client=None, retention=None):
        if retention is not None:
            retention = comparison_handoff(retention)
        return handle(tool, payload, request_id=request_id, host=host,
                      runner_client=runner_client, inspector_client=inspector_client, retention=retention)

    return dispatch


def _descriptors():
    argument_schemas = {}
    result_schemas = {}
    mini_tools = {}
    mcp_tools = {}
    router_entries = {}
    for tool, (summary, keywords, level) in _ROUTES.items():
        domain = tool.split('.')[0]
        payload_ref = _BASE + domain + '/compare.payload.json'
        result_ref = _BASE + domain + '/compare.result.json'
        wrapper_ref = _PUBLIC_BASE + domain + '/compare.input.json'
        row_id = 'host.' + tool
        name = f'rea_{domain}_compare'
        argv = ['slot', 'mini', tool, '--args', '<json>']
        perception = {'required_level': level, 'optional_levels': []}
        argument = {
            '$schema': _SCHEMA_VERSION, '$id': payload_ref, 'type': 'object', 'additionalProperties': False,
            'required': list(_REFS),
            'properties': {ref: {'type': 'string', 'minLength': 1, 'maxLength': 512, 'pattern': r'\S'} for ref in _REFS},
        }
        result = {
            '$schema': _SCHEMA_VERSION, '$id': result_ref, 'type': 'object', 'additionalProperties': False,
            'required': ['artifact_ref', 'observation', 'verdict'],
            'properties': {
                'artifact_ref': {'type': 'string', 'minLength': 1, 'maxLength': 512, 'pattern': r'\S'},
                'receipt_ref': {'type': 'string', 'minLength': 1, 'maxLength': 512, 'pattern': r'\S'},
                'observation': {'$ref': _PROFILE + '#/$defs/observation'},
                'verdict': {'$ref': _PROFILE + '#/$defs/verdict'},
                'coverage': {'type': 'object'}, 'deltas': {'type': 'array'}, 'exception_refs': {'type': 'array'},
                'order_conflicts': {'type': 'array'}, 'aligned_occurrences': {'type': 'array'},
                'spatial_difference': {'type': 'array'},
            },
        }
        wrapper = {
            '$schema': _SCHEMA_VERSION, '$id': wrapper_ref, 'type': 'object', 'additionalProperties': False,
            'required': ['request_id', 'payload'],
            'properties': {
                'request_id': {'type': 'string', 'pattern': _REQUEST_ID_PATTERN},
                'dry_run': {'type': 'boolean', 'default': False}, 'payload': {'$ref': payload_ref},
            },
        }
        argument_schemas[payload_ref] = argument
        argument_schemas[wrapper_ref] = wrapper
        result_schemas[result_ref] = result
        # Production enrollment is owned by the projector and requires
        # authenticated dispatch and authorized shared receipt readback.
        mini_tools[tool] = {
            'id': tool, 'summary': summary, 'keywords': list(keywords), 'perception': perception,
            'argument_schema_ref': payload_ref, 'result_schema_ref': result_ref,
            'required_effects': [], 'handler': _handler(tool),
        }
        mcp_tools[name] = {'name': name, 'title': row_id, 'description': summary, 'inputSchema': wrapper}
        router_entries[row_id] = {
            'id': row_id, 'owner': 'viewpoint-rea', 'operation': tool, 'role': None,
            'manifest_digest': None, 'provider_ref': None, 'summary': summary,
            'search_terms': sorted({*keywords, tool, row_id}),
            'input_schema_ref': wrapper_ref, 'output_schema_ref': 'urn:vp.control.reply:v1',
            'mcp_name': name, 'cli_argv_shape': argv,
            'json_contract_ref': {
                'control_request_schema_ref': 'urn:vp.control.req:v1',
                'control_reply_schema_ref': 'urn:vp.control.reply:v1', 'op': 'rea.slot.project', 'route_id': row_id,
                'argument_schema_ref': payload_ref, 'result_schema_ref': result_ref,
            },
            'perception': perception, 'required_effects': [], 'availability': 'parked',
            'conformance_receipt_ref': None, 'outside_capsule': True, 'facets': {},
        }
    return argument_schemas, result_schemas, mini_tools, mcp_tools, router_entries


ARGUMENT_SCHEMAS, RESULT_SCHEMAS, MINI_TOOLS, MCP_TOOLS, ROUTER_ENTRIES = _descriptors()


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key.')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON constant {value}.')


def main(argv=None, *, dispatch=None, context=None, retention=None):
    """CLI adapter for slot mini. Dispatch is the existing authenticated control lane."""
    parser = argparse.ArgumentParser(prog='slot')
    commands = parser.add_subparsers(dest='command', required=True)
    mini = commands.add_parser('mini')
    mini.add_argument('tool_id', choices=tuple(_ROUTES))
    source = mini.add_mutually_exclusive_group(required=True)
    source.add_argument('--args')
    source.add_argument('--args-file', metavar='PATH')
    mini.add_argument('--request-id')
    mini.add_argument('--dry-run', default=None, action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.args_file == '-':
            encoded = sys.stdin.read()
        elif args.args_file is not None:
            encoded = Path(args.args_file).read_text(encoding='utf-8')
        else:
            encoded = args.args
        arguments = json.loads(encoded, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
        if type(arguments) is not dict:
            raise ValueError('Expected a JSON argument object.')
        for key, flag in (('request_id', args.request_id), ('dry_run', args.dry_run)):
            if flag is not None:
                if key in arguments and (type(arguments[key]) is not type(flag) or arguments[key] != flag):
                    raise ValueError(f'The flag disagrees with argument {key}.')
                arguments[key] = flag
        envelope = control_request(args.tool_id, arguments, context=context)
    except (OSError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 14
    if dispatch is None:
        print('AVAILABILITY_PARKED: The authenticated slot control lane is unavailable.', file=sys.stderr)
        return 16
    options = {}
    if retention is not None:
        handoff = comparison_handoff(retention)
        options = {'retention': handoff, 'runner_client': handoff.runner.client,
                   'inspector_client': handoff.inspector.client}
    reply = dispatch(envelope, **options)
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')))
    if reply['ok'] is True:
        return 0
    return {
        'HOLD': 10, 'HUMAN_NEEDED': 10, 'NO_LEASE': 11, 'REQUEST_ID_CONFLICT': 12,
        'UNCERTAIN_OUTCOME': 13, 'SCHEMA_INVALID': 14, 'PERCEPTION_DENIED': 15, 'POOL_TIMEOUT': 15,
        'PROFILE_NOT_ADMITTED': 16, 'AVAILABILITY_NOT_INSTALLED': 16,
        'AVAILABILITY_BLOCKED': 16, 'AVAILABILITY_PARKED': 16,
    }.get(reply['code'], 1)


if __name__ == '__main__':
    raise SystemExit(main())
