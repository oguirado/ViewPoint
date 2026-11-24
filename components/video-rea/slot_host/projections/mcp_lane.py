# SPDX-License-Identifier: AGPL-3.0-only
"""Generated descriptors and control-lane adapter for slot routes."""

import copy
import inspect

from .project import (
    _CONTROL_REPLY_REF, _LEVELS, _OP_ID_RE, _REQUEST_ID_RE, _current_resource, _row_for,
    control_envelope, json_invoke, jcs_bytes, verify_publication,
)
from .schema import PublicationError, digest, require, standalone


_LOCAL_RETENTION_OPERATIONS = {
    'host.behaviour.record': 'record', 'host.behaviour.replay': 'replay',
    'host.behaviour.compare': 'compare', 'host.timeline.compare': 'compare',
}
_LOCAL_RETENTION_UNAVAILABLE = 'Local-open retention is unavailable. Check the Tools Unix service and REA_RETENTION_SRC.'


def _accepts_retention(dispatch):
    try:
        parameters = inspect.signature(dispatch).parameters
    except (TypeError, ValueError):
        return False
    port = parameters.get('retention')
    if port is not None and port.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _local_retention(row, dispatch, authenticated_context):
    operation = _LOCAL_RETENTION_OPERATIONS.get(row['id'])
    if row['role'] is not None and row['json_contract_ref']['execution_profile'] == 'external_process':
        operation = row['operation']
    if operation is None or not _accepts_retention(dispatch):
        return None
    if authenticated_context is None:
        raise PublicationError('NO_LEASE', 'Retention requires authenticated host context.')

    from ..runtime.evidence_projection import V03_SHA
    from ..runtime.retention_deployment import load_local_retention

    try:
        clients = load_local_retention().clients
        if clients.ready(record_pin=V03_SHA, operation_id=operation):
            return clients
    except (ImportError, OSError, ValueError):
        pass
    raise PublicationError('LIVE_EXECUTION_PARKED', _LOCAL_RETENTION_UNAVAILABLE)


def control_reply(request_id, code, *, ok, result=None, effective=None):
    if ok is False and result is None:
        result = {}
    return {'schema': 'vp.control.reply/v1', 'request_id': request_id, 'op': 'rea.slot.project',
            'ok': ok, 'code': code, 'result': result, 'effective_request_sha256': effective,
            'evidence': [], 'not_verified': []}


class SlotProjector:
    """Expose validated routes to an enrolled control owner.

    The dispatch callback takes a control envelope. The host control owner must
    authenticate it, call prepare with the authenticated context, atomically
    claim the effective request, and own dispatch and retention. The projection
    has no independent request store or legacy selector handler. When
    the dispatcher declares a retention port, live routes default to authenticated
    local-open clients. Explicit host retention is passed through out of band.
    """

    _DESCRIBE_SLOT_KEYS = frozenset({
        'schema', 'slot_id', 'status', 'manifest_digest', 'upstream', 'provider_ref',
        'capabilities', 'perception', 'record_replay', 'comparison', 'evidence',
        'conformance', 'discovery', 'operations',
    })

    def __init__(self, manifest, bundle, *, binding=None, policy=None, registry=None,
                 dispatch=None, context=None, host_cases=None, host_mini_tools=None,
                 existing_rows=None, enrolled_slots=None, admissions=(), result_verifier=None,
                 authenticated_context=None, vision_admission=None,
                 perception_ceiling=None, current_licence=None, retention=None):
        verify_publication(bundle, manifest, binding=binding, policy=policy, registry=registry,
                           host_cases=host_cases, host_mini_tools=host_mini_tools,
                           existing_rows=existing_rows, admissions=admissions)
        self.manifest = manifest
        self.bundle = copy.deepcopy(bundle)
        self.binding = binding
        self.policy = policy
        self.registry = registry
        self.enrolled_slots = enrolled_slots
        self.admissions = admissions
        self.context = context
        self._dispatch = dispatch
        self._retention = retention
        self._result_verifier = result_verifier
        self._authenticated_context = authenticated_context
        self._vision_admission = vision_admission
        self._perception_ceiling = perception_ceiling
        self._current_licence = current_licence

    def list_tools(self):
        return [copy.deepcopy(self.bundle['mcp_tools'][name]) for name in sorted(self.bundle['mcp_tools'])]

    def prepare(self, envelope, *, authenticated_context, current_licence=None,
                perception_ceiling=None, vision_admission=None):
        if authenticated_context is None:
            raise PublicationError('NO_LEASE', 'The control owner must supply an authenticated context.')
        return json_invoke(self.manifest, self.bundle, envelope, binding=self.binding,
                           policy=self.policy, registry=self.registry, context=authenticated_context,
                           enrolled_slots=self.enrolled_slots, admissions=self.admissions,
                           current_licence=current_licence, perception_ceiling=perception_ceiling,
                           vision_admission=vision_admission)

    def invoke(self, route_id, arguments, *, context=None, authenticated_context=None,
               vision_admission=None, perception_ceiling=None, current_licence=None, retention=None):
        envelope = control_envelope(self.manifest, route_id, arguments,
                                    context=self.context if context is None else context)
        return self.handle_envelope(envelope, authenticated_context=authenticated_context,
                                    vision_admission=vision_admission,
                                    perception_ceiling=perception_ceiling,
                                    current_licence=current_licence, retention=retention)

    def call_tool(self, name, arguments, *, context=None, authenticated_context=None,
                  vision_admission=None, perception_ceiling=None, current_licence=None, retention=None):
        row = next((row for row in self.bundle['router_rows'] if row['mcp_name'] == name), None)
        if row is None:
            raise PublicationError('SCHEMA_INVALID', 'Unknown generated MCP name.')
        reply = self.invoke(row['id'], arguments, context=context,
                            authenticated_context=authenticated_context,
                            vision_admission=vision_admission,
                            perception_ceiling=perception_ceiling,
                            current_licence=current_licence, retention=retention)
        return {'isError': not reply['ok'], 'structuredContent': reply,
                'content': [{'type': 'text', 'text': jcs_bytes(reply).decode('utf-8')}]}

    @staticmethod
    def _is_ref(value):
        return value is None or (type(value) is str and bool(value))

    @staticmethod
    def _is_text(value):
        return type(value) is str and bool(value)

    def _perception_bound_ok(self, row, normalized, used):
        if used not in _LEVELS:
            return False
        used_idx = _LEVELS.index(used)
        bounds = []
        policy = self.policy if type(self.policy) is dict else None
        if policy is not None and policy.get('perception_ceiling') in _LEVELS:
            bounds.append(_LEVELS.index(policy['perception_ceiling']))
        row_perception = row.get('perception') or {}
        if type(row_perception) is dict:
            levels = [row_perception.get('required_level'), *(row_perception.get('optional_levels') or [])]
            levels = [item for item in levels if item in _LEVELS]
            if levels:
                bounds.append(max(_LEVELS.index(item) for item in levels))
        requested = normalized.get('perception_level') if type(normalized) is dict else None
        if requested in _LEVELS:
            bounds.append(_LEVELS.index(requested))
        return all(used_idx <= bound for bound in bounds)

    def _validate_verdict(self, verdict):
        if type(verdict) is not dict:
            raise PublicationError('SCHEMA_INVALID', 'Invalid compare verdict.')
        if set(verdict) not in ({'result', 'comparator_id', 'comparator_version', 'verdict_ref'},
                                 {'result', 'comparator_id', 'comparator_version', 'verdict_ref',
                                  'abstain_reason'}):
            raise PublicationError('SCHEMA_INVALID', 'Invalid compare verdict.')
        if verdict.get('result') not in ('match', 'mismatch', 'abstain'):
            raise PublicationError('SCHEMA_INVALID', 'Invalid compare verdict result.')
        if not self._is_text(verdict.get('comparator_id')) or not self._is_text(verdict.get('comparator_version')):
            raise PublicationError('SCHEMA_INVALID', 'Invalid compare verdict identity.')
        if not self._is_ref(verdict.get('verdict_ref')):
            raise PublicationError('SCHEMA_INVALID', 'Invalid compare verdict ref.')
        if verdict.get('result') == 'abstain':
            if not self._is_text(verdict.get('abstain_reason')):
                raise PublicationError('SCHEMA_INVALID', 'Abstaining verdict requires abstain_reason.')
        elif 'abstain_reason' in verdict:
            raise PublicationError('SCHEMA_INVALID', 'Abstain reason only when abstaining.')

    def _validate_result(self, normalized, reply):
        if type(reply) is not dict or reply.get('op') != 'rea.slot.project' \
                or reply.get('request_id') != normalized['request_id']:
            raise PublicationError('SCHEMA_INVALID', 'Control reply identity differs from the request.')
        pin = self.bundle['control_schema_digests'].get(_CONTROL_REPLY_REF)
        reply_schema = _current_resource(self.registry, _CONTROL_REPLY_REF, pin)
        require(reply, standalone(reply_schema, self.registry, control_op='rea.slot.project'),
                'Control reply')
        if reply.get('ok') is False and reply.get('code') != 'OK' and reply.get('result') == {}:
            return
        if reply.get('ok') is not True or reply.get('code') != 'OK' or type(reply.get('result')) is not dict:
            raise PublicationError('SCHEMA_INVALID', 'Invalid control outcome.')
        result = reply['result']
        row = _row_for(self.bundle, normalized['route_id'])
        kind = result.get('kind')
        if normalized['dry_run']:
            if (set(result) != {'kind', 'dry_run', 'normalized_request', 'outside_capsule'} or kind != 'dry_run'
                    or result['dry_run'] is not True or result['outside_capsule'] is not True
                    or jcs_bytes(result['normalized_request']) != jcs_bytes(normalized)):
                raise PublicationError('SCHEMA_INVALID', 'Preview does not match the normalized request.')
            return
        contract = row['json_contract_ref']
        if 'argument_schema_ref' in contract:
            if kind != 'host_result' or set(result) != {'kind', 'payload', 'outside_capsule'} or result['outside_capsule'] is not True:
                raise PublicationError('SCHEMA_INVALID', 'Invalid host result variant.')
            ref = contract['result_schema_ref']
            schema = _current_resource(self.registry, ref, self.bundle['mini_schema_digests'].get(ref))
            require(result['payload'], standalone(schema, self.registry), 'Host result payload')
        elif row['role'] is not None:
            status_codes = {'OK': 0, 'ERROR': 2, 'INCOMPLETE': 3, 'STALE': 4, 'FAIL': 5, 'BUDGET_EXCEEDED': 6}
            fields = {'kind', 'status', 'rc', 'slot_result_ref', 'evidence_ref', 'receipt_ref',
                      'availability', 'produced_by', 'perception_used', 'outside_capsule'}
            if row['operation'] == 'compare':
                fields.add('verdict')
            if (set(result) != fields or kind != 'slot_exchange' or result['availability'] != 'admitted'
                    or result['outside_capsule'] is not True or type(result['rc']) is not int
                    or type(result['status']) is not str or result['rc'] != status_codes.get(result['status'])
                    or type(result['produced_by']) is not str or not result['produced_by']
                    or result['perception_used'] not in ('none', 'cdp_atspi', 'vision')):
                raise PublicationError('SCHEMA_INVALID', 'Invalid provider result projection.')
            if not self._is_ref(result['slot_result_ref']) or not self._is_ref(result['evidence_ref']) \
                    or not self._is_ref(result['receipt_ref']):
                raise PublicationError('SCHEMA_INVALID', 'Invalid provider result refs.')
            if not self._perception_bound_ok(row, normalized, result['perception_used']):
                raise PublicationError('SCHEMA_INVALID', 'Perception exceeds authorized ceiling.')
            if row['operation'] == 'compare':
                self._validate_verdict(result['verdict'])
            elif 'verdict' in result:
                raise PublicationError('SCHEMA_INVALID', 'Verdict only on compare.')
            if self._result_verifier(normalized, reply) is not True:
                raise PublicationError('SCHEMA_INVALID', 'The host could not verify the executed exchange.')
        else:
            expected = {'describe': 'slot_describe', 'list': 'slot_list', 'slot.inspect': 'host_result'}.get(
                row['operation'], 'conformance')
            if kind != expected:
                raise PublicationError('SCHEMA_INVALID', 'Unexpected control result variant.')
            if expected == 'conformance':
                if set(result) != {'kind', 'cases', 'conformance_receipt_ref', 'outside_capsule'} \
                        or result.get('outside_capsule') is not True \
                        or not self._is_ref(result.get('conformance_receipt_ref')) \
                        or type(result.get('cases')) is not list:
                    raise PublicationError('SCHEMA_INVALID', 'Invalid conformance result.')
                for case in result['cases']:
                    if type(case) is not dict or set(case) != {'case_id', 'outcome', 'case_receipt_ref', 'reason'}:
                        raise PublicationError('SCHEMA_INVALID', 'Invalid conformance case.')
                    if type(case['case_id']) is not str or not _OP_ID_RE.fullmatch(case['case_id']):
                        raise PublicationError('SCHEMA_INVALID', 'Invalid conformance case id.')
                    if case['outcome'] not in ('pass', 'fail', 'abstain'):
                        raise PublicationError('SCHEMA_INVALID', 'Invalid conformance case outcome.')
                    if not self._is_ref(case['case_receipt_ref']):
                        raise PublicationError('SCHEMA_INVALID', 'Invalid conformance case ref.')
                    if case['outcome'] == 'pass':
                        if case['reason'] is not None:
                            raise PublicationError('SCHEMA_INVALID', 'Passing case reason must be null.')
                    elif not self._is_text(case['reason']):
                        raise PublicationError('SCHEMA_INVALID', 'Fail/abstain case requires reason.')
            elif expected == 'host_result':
                if set(result) != {'kind', 'payload', 'outside_capsule'} or result.get('outside_capsule') is not True:
                    raise PublicationError('SCHEMA_INVALID', 'Invalid inspect result variant.')
                payload = result.get('payload')
                if row['id'] == 'host.slot.inspect':
                    if type(payload) is not dict or set(payload) != {'slot', 'installation_refs',
                                                                      'selected_installation_ref',
                                                                      'conformance_receipt_ref',
                                                                      'unresolved_conformance_refs'}:
                        raise PublicationError('SCHEMA_INVALID', 'Invalid inspect payload.')
                    if type(payload['slot']) is not dict \
                            or set(payload['slot']) != self._DESCRIBE_SLOT_KEYS:
                        raise PublicationError('SCHEMA_INVALID', 'Invalid inspect slot.')
                    refs = payload['installation_refs']
                    if type(refs) is not list or any(not self._is_text(ref) for ref in refs) or refs != sorted(refs):
                        raise PublicationError('SCHEMA_INVALID', 'Invalid inspect installation refs.')
                    if not self._is_ref(payload['selected_installation_ref']) \
                            or not self._is_ref(payload['conformance_receipt_ref']):
                        raise PublicationError('SCHEMA_INVALID', 'Invalid inspect refs.')
                    unresolved = payload['unresolved_conformance_refs']
                    if type(unresolved) is not list or any(not self._is_text(ref) for ref in unresolved) \
                            or unresolved != sorted(unresolved):
                        raise PublicationError('SCHEMA_INVALID', 'Invalid inspect unresolved refs.')
                elif type(payload) is not dict:
                    raise PublicationError('SCHEMA_INVALID', 'Invalid host payload.')
            elif expected == 'slot_describe':
                if set(result) != {'kind', 'slot'}:
                    raise PublicationError('SCHEMA_INVALID', 'Invalid describe result variant.')
                if type(result.get('slot')) is not dict \
                        or set(result['slot']) != self._DESCRIBE_SLOT_KEYS:
                    raise PublicationError('SCHEMA_INVALID', 'Invalid describe slot.')
            else:
                if set(result) != {'kind', 'rows'}:
                    raise PublicationError('SCHEMA_INVALID', 'Invalid list result variant.')
                rows = result.get('rows')
                if type(rows) is not list or any(type(item) is not dict or type(item.get('id')) is not str
                                                for item in rows):
                    raise PublicationError('SCHEMA_INVALID', 'Invalid list rows.')
                router_rows = self.bundle.get('router_rows') or []
                row_fields = set(router_rows[0]) if router_rows else {
                    'id', 'owner', 'manifest_digest', 'provider_ref', 'role', 'operation',
                    'summary', 'search_terms', 'input_schema_ref', 'output_schema_ref',
                    'mcp_name', 'cli_argv_shape', 'json_contract_ref', 'required_effects',
                    'perception', 'availability', 'conformance_receipt_ref', 'outside_capsule',
                    'facets',
                }
                if any(set(item) != row_fields for item in rows):
                    raise PublicationError('SCHEMA_INVALID', 'Invalid list rows.')
                allowed = set(self.bundle.get('json_contracts', {}))
                for snapshot in (self.enrolled_slots or {}).values():
                    try:
                        _t_manifest, enrolled_bundle, _t_binding = tuple(snapshot)
                    except (TypeError, ValueError):
                        continue
                    if type(enrolled_bundle) is not dict:
                        continue
                    allowed |= set(enrolled_bundle.get('json_contracts', {}))
                    allowed |= {item['id'] for item in enrolled_bundle.get('router_rows', [])
                                if type(item) is dict and type(item.get('id')) is str}
                ids = [item['id'] for item in rows]
                if ids != sorted(ids) or any(item_id not in allowed for item_id in ids):
                    raise PublicationError('SCHEMA_INVALID', 'Invalid list rows.')
                all_flag = False
                if type(normalized) is dict and type(normalized.get('arguments')) is dict:
                    all_flag = bool(normalized['arguments'].get('all', False))
                if not all_flag and any(item.get('availability') != 'admitted' for item in rows):
                    raise PublicationError('SCHEMA_INVALID', 'Invalid list rows.')

    def handle_envelope(self, envelope, *, authenticated_context=None, vision_admission=None,
                        perception_ceiling=None, current_licence=None, retention=None):
        request_id = envelope.get('request_id') if type(envelope) is dict else None
        if type(request_id) is not str or not _REQUEST_ID_RE.fullmatch(request_id):
            raise PublicationError('SCHEMA_INVALID', 'Invalid request id for the control carrier.')
        try:
            # Authenticated host admission only: envelope.context is
            # caller-controlled and must never supply the vision grant.
            # A vision-live request without an authenticated grant must
            # fail PERCEPTION_DENIED (or POOL_TIMEOUT from the
            # authoritative check) before dispatch.
            effective_context = authenticated_context if authenticated_context is not None \
                else self._authenticated_context
            effective_vision = vision_admission if vision_admission is not None \
                else self._vision_admission
            effective_ceiling = perception_ceiling if perception_ceiling is not None \
                else self._perception_ceiling
            effective_licence = current_licence if current_licence is not None \
                else self._current_licence
            host_kwargs = {}
            if effective_ceiling is not None:
                host_kwargs['perception_ceiling'] = effective_ceiling
            if effective_licence is not None:
                host_kwargs['current_licence'] = effective_licence
            normalized = json_invoke(self.manifest, self.bundle, envelope, binding=self.binding,
                                     policy=self.policy, registry=self.registry,
                                     enrolled_slots=self.enrolled_slots,
                                     admissions=self.admissions, context=effective_context,
                                     vision_admission=effective_vision, **host_kwargs)
            row = _row_for(self.bundle, normalized['route_id'])
            if self._dispatch is None or (row['role'] is not None and not normalized['dry_run']
                                          and self._result_verifier is None):
                raise PublicationError('AVAILABILITY_PARKED', 'The authenticated execution boundary is unavailable.')
            effective_retention = self._retention if retention is None else retention
            if effective_retention is None and not normalized['dry_run']:
                effective_retention = _local_retention(row, self._dispatch, effective_context)
            options = {}
            if effective_retention is not None:
                if effective_context is None:
                    raise PublicationError('NO_LEASE', 'Retention requires authenticated host context.')
                from ..compare.tools import MINI_TOOLS, comparison_handoff
                route_id = normalized['route_id']
                if route_id.startswith('host.') and route_id[5:] in MINI_TOOLS:
                    effective_retention = comparison_handoff(effective_retention)
                options['retention'] = effective_retention
            reply = self._dispatch(envelope, **options)
            if inspect.isawaitable(reply):
                if inspect.iscoroutine(reply):
                    reply.close()
                raise PublicationError('AVAILABILITY_PARKED', 'A synchronous control dispatcher is required.')
            self._validate_result(normalized, reply)
            return reply
        except PublicationError as error:
            try:
                effective = digest(envelope)
            except Exception:
                effective = None
            reply = control_reply(request_id, error.code, ok=False, effective=effective)
            if error.code == 'LIVE_EXECUTION_PARKED':
                reply['not_verified'] = [str(error)]
            return reply
