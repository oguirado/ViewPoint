# SPDX-License-Identifier: AGPL-3.0-only
"""Generate, verify and normalize slot projections from host-owned inputs."""

import copy
import json
import re

from .schema import PublicationError, accepts, digest, jcs_bytes, require, resource, standalone

_SCHEMA_VERSION = 'https://json-schema.org/draft/2020-12/schema'
_PUBLIC_BASE = 'https://viewpoint.local/contracts/rea/projections/'
_MANIFEST_REF = 'https://viewpoint.local/contracts/rea/rea.slot_manifest_v2.schema.json'
_CONTROL_REQUEST_REF = 'urn:vp.control.req:v1'
_CONTROL_REPLY_REF = 'urn:vp.control.reply:v1'
_CONTROL_OP = 'rea.slot.project'
_REQUEST_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}')
_SLOT_ID_RE = re.compile(r'[a-z][a-z0-9]*(-[a-z0-9]+)*')
_OP_ID_RE = re.compile(r'[a-z][a-z0-9_]*')
_SHA256_RE = re.compile(r'[0-9a-f]{64}')
_LEVELS = ('none', 'cdp_atspi', 'vision')
_STATES = ('admitted', 'parked', 'blocked', 'not_installed')
_MINI_TOOLS = (
    'slot.inspect', 'behaviour.record', 'behaviour.replay', 'behaviour.compare',
    'timeline.compare', 'visual.compare', 'evidence.inspect', 'fixture.ground_truth',
)
# Runtime-supplied host routes extend new publications without rewriting the
# inventory of already frozen adapter bundles.
_REGISTERED_MINI_TOOLS = (
    'error.diagnose', 'effect.isolate', 'journal.verify', 'reconstruction.obligations', 'tape.generate',
)
_MINI_SUMMARIES = {
    'behaviour.record': 'Record the original on a declared input tape.',
    'behaviour.replay': 'Exercise the candidate with the retained input tape.',
    'behaviour.compare': 'Resolve original and candidate references and compare.',
    'timeline.compare': 'Check event identity, coverage, ordering and timing.',
    'visual.compare': 'Compare equal-geometry rasters inside the declared region.',
    'evidence.inspect': 'Resolve and page an authorized shared receipt.',
    'fixture.ground_truth': 'Produce host-owned controlled inputs and expected observations.',
}


def manifest_digest(manifest):
    return digest(manifest)


def policy_sha256(policy):
    if type(policy) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'The authenticated policy snapshot is required.')
    return digest(policy)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError('SCHEMA_INVALID', f'Duplicate JSON object key {key!r}.')
        result[key] = value
    return result


def load_manifest_bytes(raw):
    try:
        manifest = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw,
                              object_pairs_hook=_object_pairs)
        jcs_bytes(manifest)
    except (ValueError, TypeError, RecursionError) as error:
        raise PublicationError('SCHEMA_INVALID', 'Manifest is not strict JSON.') from error
    if type(manifest) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'Manifest must be a JSON object.')
    return manifest


def _indexed(rows, key):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise PublicationError('SCHEMA_INVALID', f'Duplicate declaration for {key}.')
    return result


def _validate_structure(manifest, registry):
    require(manifest, standalone(resource(registry, _MANIFEST_REF), registry), 'Manifest')
    operations = _indexed(manifest['operations'], 'id')
    contracts = _indexed(manifest['contract_refs'], 'role')
    probes = manifest['capabilities']['prerequisite_probes']
    _indexed(probes, 'id')
    perception = manifest['perception']
    sources = _indexed(perception['observation_sources'], 'id')
    levels = _declared_levels(manifest)
    if levels != sorted(set(levels), key=_LEVELS.index) or set(levels) != {row['level'] for row in sources.values()}:
        raise PublicationError('SCHEMA_INVALID', 'Observation sources and ordered perception levels disagree.')
    transport = manifest['transport']
    transports = {transport['kind'], *(item['kind'] for item in transport.get('alternates', []))}
    for operation in operations.values():
        name = operation['id']
        if name == 'conform':
            raise PublicationError('COLLISION', 'The conform segment is reserved.')
        role = operation['role']
        contract = contracts.get(role)
        if contract is None or any(
            operation[selector] != contract[ref] + '#slot=' + role
            for selector, ref in (('input_ref', 'request_schema_ref'), ('output_ref', 'result_schema_ref'))
        ):
            raise PublicationError('SCHEMA_INVALID', f'Exchange selectors disagree for {name}.')
        adapter = operation['binding']
        if adapter['kind'] == 'cli_subcommand':
            args = adapter['typed_args']
            _indexed(args, 'name')
            if 'cli_argv' not in transports or sorted(arg['position'] for arg in args) != list(range(len(args))):
                raise PublicationError('SCHEMA_INVALID', 'Invalid cli_subcommand transport or position mapping.')
        if adapter['kind'] == 'mcp_tool' and 'mcp_stdio' not in transports:
            raise PublicationError('SCHEMA_INVALID', 'mcp_tool requires mcp_stdio.')
        if adapter['kind'] == 'host_composition' and adapter['mini_tool'] not in _MINI_TOOLS:
            raise PublicationError('SCHEMA_INVALID', 'Unknown host composition.')
    if set(operations) != set(manifest['discovery']['operation_descriptions']):
        raise PublicationError('SCHEMA_INVALID', 'Discovery descriptions disagree with the operation inventory.')
    covered = {effect for name, operation in operations.items() if name in ('record', 'replay')
               for effect in operation['required_effects']}
    if not set(manifest['record_replay']['effects']) <= covered:
        raise PublicationError('SCHEMA_INVALID', 'Record and replay effects do not cover the declared effects.')
    if set(manifest['conformance']['supported_entry_points']) != {'cli', 'mcp', 'json'}:
        raise PublicationError('MISSING_PROJECTION', 'The conformance kit requires CLI, MCP and JSON.')
    return operations, contracts


def _declared_levels(manifest):
    perception = manifest['perception']
    return [perception['required_level'], *perception['optional_levels']]


def _installation_refs(binding):
    refs = binding.get('installations', [])
    if type(refs) is not list or any(type(ref) is not str or not ref for ref in refs) or len(set(refs)) != len(refs):
        raise PublicationError('SCHEMA_INVALID', 'Invalid installation inventory.')
    return refs


def _resolve_installation(manifest, arguments, binding, *, discovery=False):
    refs = _installation_refs(binding)
    configured = binding.get('selected_ref')
    explicit = arguments.get('installation_ref')
    if configured is not None and configured not in refs:
        raise PublicationError('SCHEMA_INVALID', 'Selected installation is unknown.')
    if explicit is not None:
        if type(explicit) is not str or explicit not in refs:
            raise PublicationError('SCHEMA_INVALID', 'Unknown installation_ref.')
        return explicit
    if configured is not None:
        return configured
    if len(refs) == 1:
        return refs[0]
    if refs and not discovery:
        raise PublicationError('SCHEMA_INVALID', 'Multiple bindings are ambiguous.')
    return None


def _assert_installation_identity(manifest, bundle, route_id, arguments, binding, *, discovery=False):
    if binding is None:
        binding = {}
    refs = _installation_refs(binding)
    verified = bundle.get('installation_refs')
    if verified is not None and refs != verified:
        raise PublicationError('SCHEMA_INVALID', 'Installation inventory differs from the verified publication.')
    selected = _resolve_installation(manifest, arguments, binding, discovery=discovery)
    expected = bundle['route_installation'][route_id]
    if selected == expected:
        return selected
    if expected is None and discovery and verified is not None and selected is not None:
        route_avail = bundle.get('route_availability', {}).get(route_id)
        if route_avail != 'not_installed' and selected in verified:
            return selected
    raise PublicationError('SCHEMA_INVALID', 'Selected installation differs from the verified publication.')


def _check_identity(manifest, binding):
    provider = manifest['provider_ref']
    adapter = manifest['capabilities']['adapter_identity']
    expected = {key: provider[key] for key in ('provider_id', 'provider_revision', 'provider_sha256', 'implementation_sha256')}
    expected.update({key: adapter[key] for key in ('adapter_id', 'adapter_sha256')})
    for key, pin in expected.items():
        if not pin or binding.get(key) != pin:
            raise PublicationError('SCHEMA_INVALID', f'Measured installation identity differs for {key}.')


def _probe_passed(manifest, binding):
    probes = binding.get('probes', {})
    if probes.get('required') and probes.get('passed') is not True:
        return False
    return all(probes.get(probe['id']) == 'passed' for probe in manifest['capabilities']['prerequisite_probes'])


def _receipt_ref_for(policy, operation):
    value = policy.get('conformance_receipts', {}).get(operation)
    return value if type(value) is str and value.strip() else None


def availability_for(manifest, op_id=None, *, binding=None, policy=None, current_licence=None):
    binding = binding if binding is not None else {}
    if manifest['status'] == 'not_installed' or not _installation_refs(binding):
        return 'not_installed'
    if type(policy) is not dict:
        return 'blocked'
    licence = current_licence if current_licence is not None else policy.get('licence')
    ceiling = policy.get('perception_ceiling')
    if licence != 'allowed' or policy.get('licence') != 'allowed' or not _probe_passed(manifest, binding):
        return 'blocked'
    if ceiling not in _LEVELS or _LEVELS.index(ceiling) < _LEVELS.index(_declared_levels(manifest)[0]):
        return 'blocked'
    selected = _resolve_installation(manifest, {}, binding, discovery=True)
    if selected is None:
        return 'parked'
    try:
        _check_identity(manifest, binding)
    except PublicationError:
        return 'blocked'
    if policy.get('evidence_schema_bound') is not True:
        return 'parked'
    if op_id is not None:
        operation = next((row for row in manifest['operations'] if row['id'] == op_id), None)
        if (operation is None or operation['binding']['kind'] != 'cli_subcommand'
                or _receipt_ref_for(policy, op_id) is None):
            return 'parked'
    return 'admitted'


def _verify_request_result_pins(contract, registry, *, invocation=False):
    resolved = {}
    missing = []
    for kind in ('request', 'result'):
        ref, pin = contract[f'{kind}_schema_ref'], contract[f'{kind}_digest']
        if pin is None or ref not in registry:
            missing.append(ref)
            continue
        if type(pin) is not str or not _SHA256_RE.fullmatch(pin) or pin == '0' * 64:
            raise PublicationError('SCHEMA_INVALID', f'Invalid {kind}_digest.')
        try:
            resolved[kind] = resource(registry, ref, pin)
        except PublicationError as error:
            raise PublicationError(error.code, f'{kind}_digest or schema identity differs for {ref}.') from error
    if missing and invocation:
        raise PublicationError('SCHEMA_INVALID', f'Exchange schema pin no longer resolves for {missing[0]}.')
    return resolved if not missing else None


def _resolve_params(contract, request, registry):
    role = contract['role']
    ref = contract['request_schema_ref']
    matches = []
    for index, branch in enumerate(request.get('allOf', [])):
        if type(branch) is not dict:
            continue
        condition = branch.get('if', {})
        if type(condition) is not dict or 'slot' not in condition.get('required', []):
            continue
        if condition.get('properties', {}).get('slot', {}).get('const') == role:
            matches.append((index, branch.get('then', {}).get('properties', {}).get('params')))
    if len(matches) != 1:
        raise PublicationError('MISSING_PROJECTION', f'No unique verified request role projection for {role}.')
    index, schema = matches[0]
    if schema is None and 'params' not in request.get('properties', {}):
        return None, _closed({}), True
    if type(schema) is not dict or schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
        raise PublicationError('SCHEMA_INVALID', f'Params schema for {role} is not closed.')
    pointer = f'{ref}#/allOf/{index}/then/properties/params'
    probe = {'$id': _PUBLIC_BASE + 'validation/params.json', '$ref': pointer}
    return pointer, schema, accepts({}, standalone(probe, registry))


def _closed(properties, required=()):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(required)}


def _wrapper(path, properties=None, required=()):
    fields = {'request_id': {'type': 'string', 'minLength': 8, 'maxLength': 128, 'pattern': '^' + _REQUEST_ID_RE.pattern + '$'},
              'dry_run': {'type': 'boolean', 'default': False}}
    fields.update(properties or {})
    return {'$schema': _SCHEMA_VERSION, '$id': _PUBLIC_BASE + path,
            **_closed(fields, ('request_id', *required))}


def _external_schema(manifest, operation, resolved):
    pointer, params_schema, optional = resolved
    levels = _declared_levels(manifest)
    modes = sorted(manifest['record_replay']['determinism_modes'])
    params = {'$ref': pointer} if pointer else copy.deepcopy(params_schema)
    required = []
    if optional:
        params['default'] = {}
    else:
        required.append('params')
    mode = {'type': 'string', 'enum': modes}
    if len(modes) == 1:
        mode['default'] = modes[0]
    else:
        required.append('determinism_mode')
    text = {'type': 'string', 'minLength': 1}
    return _wrapper(f'{manifest["slot_id"]}/{operation["id"]}.input.json', {
        'installation_ref': text,
        'inputs': {'type': 'array', 'default': [], 'items': _closed({
            'role': text, 'ref': text, 'sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64, 'pattern': '^[0-9a-f]{64}$', 'not': {'const': '0' * 64}},
        }, ('role', 'ref'))},
        'params': params,
        'perception_level': {'type': 'string', 'enum': levels, 'default': levels[0]},
        'determinism_mode': mode,
    }, required)


def _host_mini_schema(name, descriptor, registry):
    if descriptor is None:
        return None, {}
    pins = {}
    unresolved = False
    for key in ('argument_schema_ref', 'result_schema_ref'):
        pin_key = key + '_sha256'
        pin = descriptor.get(pin_key)
        if name in _REGISTERED_MINI_TOOLS and pin is None:
            raise PublicationError('SCHEMA_INVALID', f'Missing {pin_key} for host mini-tool {name}.')
        ref = descriptor.get(key)
        if type(ref) is not str or ref not in registry:
            unresolved = True
            continue
        schema = resource(registry, ref, pin)
        pins[ref] = digest(schema)
    if unresolved:
        return None, {}
    arg_ref = descriptor['argument_schema_ref']
    if registry[arg_ref].get('type') != 'object' or registry[arg_ref].get('additionalProperties') is not False:
        raise PublicationError('SCHEMA_INVALID', f'Host payload for {name} must be a closed object.')
    domain, operation = name.split('.')
    return _wrapper(f'host/{domain}/{operation}.input.json', {'payload': {'$ref': arg_ref}}, ('payload',)), pins


def _perception(value):
    if type(value) is not dict or set(value) != {'required_level', 'optional_levels'}:
        raise PublicationError('SCHEMA_INVALID', 'Invalid route perception declaration.')
    level, optional = value['required_level'], value['optional_levels']
    if level not in _LEVELS or type(optional) is not list or any(item not in _LEVELS for item in optional):
        raise PublicationError('SCHEMA_INVALID', 'Unknown perception level.')
    ranks = [_LEVELS.index(item) for item in (level, *optional)]
    if ranks != sorted(set(ranks)):
        raise PublicationError('SCHEMA_INVALID', 'Route perception levels are out of order.')
    return copy.deepcopy(value)


def _host_mini_cli(name):
    return ['slot', 'mini', name, '--args', '<json>']


def _route_names(route, slot):
    normalized = slot.replace('-', '_')
    if route == f'ext.{slot}':
        return f'rea_slot_describe__{normalized}', ['slot', 'describe', slot]
    if route.startswith(f'ext.{slot}.'):
        operation = route[len(slot) + 5:]
        if operation == 'conform':
            return f'rea_conform_{normalized}', ['slot', 'conform', slot]
        if operation.startswith('conform.'):
            case = operation[len('conform.'):]
            return f'rea_conform_{normalized}__{case}', ['slot', 'conform', slot, case]
        return f'rea_ext_{normalized}__{operation}', ['slot', 'call', slot, operation, '--args', '<json>']
    if route == 'host.slot.list':
        return 'rea_slot_list', ['slot', 'list']
    if route == 'host.slot.inspect':
        return 'rea_slot_inspect', ['slot', 'inspect', '<slot_id>']
    name = route[len('host.'):]
    return 'rea_' + name.replace('.', '_'), _host_mini_cli(name)


def _case_inventory(host_cases, registry):
    cases = {}
    for case in host_cases or ():
        if type(case) is not dict or type(case.get('id')) is not str or not _OP_ID_RE.fullmatch(case['id']) or len(case['id']) > 24:
            raise PublicationError('SCHEMA_INVALID', 'Invalid conformance case id.')
        case_id = case['id']
        if case_id in cases and jcs_bytes(cases[case_id]) != jcs_bytes(case):
            raise PublicationError('COLLISION', f'Conflicting conformance case {case_id}.')
        ref, pin = case.get('ref'), case.get('digest')
        if type(ref) is not str or not ref:
            raise PublicationError('SCHEMA_INVALID', 'Conformance case reference is required.')
        if pin is not None and (type(pin) is not str or not _SHA256_RE.fullmatch(pin)):
            raise PublicationError('SCHEMA_INVALID', 'Invalid conformance case digest.')
        if pin is not None and ref in registry and digest(registry[ref]) != pin:
            raise PublicationError('SCHEMA_INVALID', f'Conformance case digest differs for {ref}.')
        cases[case_id] = case
    return cases


def _registered_minis(registry, host_mini_tools):
    descriptors = host_mini_tools if host_mini_tools is not None else registry.get('host_mini_tools', {})
    return descriptors, tuple(name for name in _REGISTERED_MINI_TOOLS if name in descriptors)


def _expected_routes(manifest, cases, registered_minis):
    prefix = 'ext.' + manifest['slot_id']
    return ({prefix, prefix + '.conform', 'host.slot.list'}
            | {prefix + '.' + op['id'] for op in manifest['operations']}
            | {prefix + '.conform.' + case_id for case_id in cases}
            | {'host.' + mini for mini in (*_MINI_TOOLS, *registered_minis)})


def _control_pins(registry):
    return {ref: digest(resource(registry, ref)) for ref in (_CONTROL_REQUEST_REF, _CONTROL_REPLY_REF) if ref in registry}


def _operation_state(manifest, operation, contract, *, base, resolved, selected, policy_hash, admissions, control_ready):
    if base != 'admitted':
        return base
    if resolved is None or not control_ready or operation['binding']['kind'] != 'cli_subcommand':
        return 'parked'
    if contract['execution_profile'] == 'external_process':
        scope = (manifest_digest(manifest), operation['id'], selected, policy_hash)
        if scope not in admissions or manifest['evidence']['shared_schema_ref'].endswith('@pending'):
            return 'parked'
    return 'admitted'


def _compile(manifest, *, binding, policy, registry, host_cases, host_mini_tools, admissions):
    operations, contracts = _validate_structure(manifest, registry)
    cases = _case_inventory(host_cases, registry)
    descriptors, registered_minis = _registered_minis(registry, host_mini_tools)
    slot, manifest_hash = manifest['slot_id'], manifest_digest(manifest)
    policy_hash = policy_sha256(policy)
    prefix = 'ext.' + slot
    selected = _resolve_installation(manifest, {}, binding, discovery=True)
    controls = _control_pins(registry)
    control_ready = set(controls) == {_CONTROL_REQUEST_REF, _CONTROL_REPLY_REF}
    levels = _declared_levels(manifest)
    perception = {'required_level': levels[0], 'optional_levels': levels[1:]}
    passive = {'required_level': 'none', 'optional_levels': []}
    base = availability_for(manifest, binding=binding, policy=policy)
    ceiling = policy.get('perception_ceiling') if isinstance(policy, dict) else None
    def _ceiling_blocks(required_level):
        if required_level is None:
            return False
        if required_level not in _LEVELS or ceiling not in _LEVELS:
            return True
        return _LEVELS.index(ceiling) < _LEVELS.index(required_level)
    upstream = manifest['upstream']
    facets = {
        'target_kinds': sorted(manifest['capabilities']['target_kinds']),
        'platforms': sorted(manifest['capabilities']['platforms']),
        'transport_kind': manifest['transport']['kind'],
        'adapter_identity': manifest['capabilities']['adapter_identity']['adapter_id'],
        'determinism_modes': sorted(manifest['record_replay']['determinism_modes']),
        'artifact_roles': sorted(manifest['evidence']['artifact_roles']),
        'comparator_id': manifest['comparison']['comparator_id'],
        'upstream': {key: upstream[key] for key in ('name', 'version', 'licence_id')},
        'binding_kind': None,
    }
    terms = (set(manifest['discovery']['search_terms']) | {slot, upstream['name'], facets['comparator_id']}
             | set(slot.split('-')) | set(facets['target_kinds']) | set(manifest['record_replay']['observable_channels']))
    rows, schemas, standalone_schemas, resolved_params = {}, {}, {}, {}
    request_pins, mini_pins = {}, {}

    def append(route, operation, summary, schema, state, *, effects=(), levels=passive,
               contract=None, keywords=(), receipt=None, extra_facets=None):
        name, cli = _route_names(route, slot)
        is_external = route.startswith('ext.')
        json_contract = {'control_request_schema_ref': _CONTROL_REQUEST_REF,
                         'control_reply_schema_ref': _CONTROL_REPLY_REF, 'op': _CONTROL_OP,
                         'route_id': route, **(contract or {})}
        if schema is not None:
            ref = schema['$id']
            if ref in schemas:
                raise PublicationError('COLLISION', f'Duplicate generated schema {ref}.')
            schemas[ref] = schema
            standalone_schemas[ref] = standalone(schema, registry)
        else:
            ref = None
        row = {
            'id': route, 'owner': 'viewpoint-rea', 'manifest_digest': manifest_hash if is_external else None,
            'provider_ref': manifest['provider_ref'] if is_external else None,
            'role': json_contract.get('role'), 'operation': operation, 'summary': summary,
            'search_terms': sorted((terms if is_external else {route}) | {operation} | set(keywords)),
            'input_schema_ref': ref, 'output_schema_ref': _CONTROL_REPLY_REF,
            'mcp_name': name, 'cli_argv_shape': cli, 'json_contract_ref': json_contract,
            'required_effects': sorted(set(effects)), 'perception': levels, 'availability': state,
            'conformance_receipt_ref': receipt, 'outside_capsule': True,
            'facets': {**facets, **(extra_facets or {})} if is_external else {},
        }
        if route in rows:
            raise PublicationError('COLLISION', f'Duplicate route {route}.')
        rows[route] = row

    for name, operation in operations.items():
        contract = contracts[operation['role']]
        pinned = _verify_request_result_pins(contract, registry)
        resolved = _resolve_params(contract, pinned['request'], registry) if pinned else None
        resolved_params[name] = resolved[0] if resolved else None
        if pinned:
            for kind in ('request', 'result'):
                request_pins[contract[f'{kind}_schema_ref']] = contract[f'{kind}_digest']
        receipt = _receipt_ref_for(policy, name)
        initial = 'parked' if base == 'admitted' and receipt is None else base
        state = _operation_state(manifest, operation, contract, base=initial, resolved=resolved,
                                 selected=selected, policy_hash=policy_hash, admissions=admissions,
                                 control_ready=control_ready)
        append(prefix + '.' + name, name, manifest['discovery']['operation_descriptions'][name],
               _external_schema(manifest, operation, resolved) if resolved else None, state,
               effects=operation['required_effects'], levels=perception,
               contract={**contract, 'request_selector': operation['input_ref'],
                         'result_selector': operation['output_ref']}, receipt=receipt,
               extra_facets={'binding_kind': operation['binding']['kind']})
    readiness = {rows[prefix + '.' + operation]['availability'] for operation in operations}
    describe_state = next(state for state in _STATES if state in readiness)
    append(prefix, 'describe', manifest['discovery']['summary'],
           _wrapper(f'{slot}/discovery/describe.input.json', {'op_id': {'type': 'string', 'enum': sorted(operations)}}),
           describe_state, levels=perception)
    install_property = {'installation_ref': {'type': 'string', 'minLength': 1}}
    case_effects = {effect for case in cases.values() for effect in case.get('effects', [])}
    case_refs = {case['ref'] for case in cases.values() if case.get('digest') and case['ref'] in registry}
    unresolved_cases = sorted((set(manifest['conformance']['fixture_refs']) | set(manifest['conformance']['negative_control_refs'])) - case_refs)
    kit_eligible = base == 'admitted' and control_ready and bool(cases) and not unresolved_cases
    case_policy = policy.get('case_availability', {})
    case_levels = [_perception(case['perception']) for case in cases.values() if 'perception' in case]
    kit_perception = max(case_levels, key=lambda item: _LEVELS.index(item['required_level'])) if case_levels else perception
    kit_state = base
    if base == 'not_installed':
        kit_state = 'not_installed'
    elif _ceiling_blocks(kit_perception['required_level']):
        kit_state = 'blocked'
    elif base == 'admitted':
        kit_state = 'admitted' if kit_eligible and case_policy.get('conform') == 'admitted' else 'parked'
    append(prefix + '.conform', 'conform', f'Run conformance for {slot}',
           _wrapper(f'{slot}/conform.input.json', install_property), kit_state,
           effects=case_effects, levels=kit_perception, receipt=_receipt_ref_for(policy, 'conform'))
    for case_id, case in cases.items():
        name = 'conform.' + case_id
        levels_for_case = _perception(case['perception']) if 'perception' in case else None
        available_case = (case['ref'] in case_refs and levels_for_case is not None and control_ready
                          and case_policy.get(case_id) == 'admitted')
        if base == 'not_installed':
            state = 'not_installed'
        elif levels_for_case is not None and _ceiling_blocks(levels_for_case['required_level']):
            state = 'blocked'
        else:
            state = base if base != 'admitted' else ('admitted' if available_case else 'parked')
        append(prefix + '.' + name, name, case.get('summary', f'Conformance case {case_id}'),
               _wrapper(f'{slot}/conform/{case_id}.input.json', install_property) if available_case else None,
               state, effects=case.get('effects', []), levels=levels_for_case,
               keywords=[case_id, *case.get('keywords', [])], receipt=_receipt_ref_for(policy, case_id),
               extra_facets={'case_id': case_id, 'case_ref': case['ref']})
    append('host.slot.list', 'list', 'List REA slot router entries',
           _wrapper('host/slot/list.input.json', {'all': {'type': 'boolean', 'default': False}}), 'admitted')
    append('host.slot.inspect', 'slot.inspect', 'Inspect a slot manifest and its readiness.',
           _wrapper('host/slot/inspect.input.json', {
               'slot_id': {'type': 'string', 'pattern': '^' + _SLOT_ID_RE.pattern + '$', 'maxLength': 24},
               **install_property,
           }, ('slot_id',)), 'admitted')
    for name in (*_MINI_SUMMARIES, *registered_minis):
        descriptor = descriptors.get(name)
        schema, pins = _host_mini_schema(name, descriptor, registry)
        descriptor = descriptor or {}
        mini_pins.update(pins)
        levels_for_mini = _perception(descriptor['perception']) if 'perception' in descriptor else None
        eligible = schema is not None and levels_for_mini is not None and control_ready
        if name in registered_minis and not eligible:
            raise PublicationError('MISSING_PROJECTION', f'Missing pinned resources for host mini-tool {name}.')
        if name in ('visual.compare', 'evidence.inspect'):
            eligible = False
        mini_state = 'admitted' if eligible else 'parked'
        if levels_for_mini is not None and _ceiling_blocks(levels_for_mini['required_level']):
            mini_state = 'blocked'
        elif not eligible and levels_for_mini is not None and _ceiling_blocks(levels_for_mini['required_level']):
            mini_state = 'blocked'
        elif eligible and levels_for_mini is not None and _ceiling_blocks(levels_for_mini['required_level']):
            mini_state = 'blocked'
            eligible = False
        append('host.' + name, name, descriptor.get('summary', _MINI_SUMMARIES.get(name, name)), schema,
               mini_state, levels=levels_for_mini,
               effects=descriptor.get('effects', []), keywords=descriptor.get('keywords', []),
               contract={key: descriptor[key] for key in ('argument_schema_ref', 'result_schema_ref')} if pins else None)
    examples = {}
    for example in manifest['discovery']['examples']:
        operation = example['op']
        row = rows.get(prefix + '.' + operation)
        if operation not in operations or row['input_schema_ref'] is None:
            raise PublicationError('SCHEMA_INVALID', f'Discovery example {operation} has no verified schema.')
        _validate_against_public(example['args'], standalone_schemas[row['input_schema_ref']])
        examples.setdefault(operation, []).append(example['args'])
    mcp_tools = {}
    for route, row in rows.items():
        schema_ref = row['input_schema_ref']
        passive_route = route in (prefix, 'host.slot.list', 'host.slot.inspect')
        if not control_ready or schema_ref is None or (not passive_route and row['availability'] != 'admitted'):
            continue
        operation = row['operation']
        external = operation in operations and route == prefix + '.' + operation
        description = row['summary']
        if external:
            description += (f' (slot {slot} {upstream["version"]}, perception {levels[0]}, '
                            f'effects {",".join(row["required_effects"])}, availability {row["availability"]})')
            description += ''.join('\n' + jcs_bytes(example).decode('utf-8') for example in examples.get(operation, ()))
        actions = {effect.partition(':')[0].partition('.')[2] for effect in row['required_effects']}
        retry_mode = operations[operation]['retry_semantics']['mode'] if external else None
        qualified = policy.get('qualified_idempotent_ops', {}).get(operation) is True
        mcp_tools[row['mcp_name']] = {
            'name': row['mcp_name'], 'title': f'{upstream["name"]} / {operation}' if external else route,
            'description': description, 'inputSchema': standalone_schemas[schema_ref],
            'annotations': {
                'readOnlyHint': actions <= {'read', 'observe'},
                'destructiveHint': bool(actions & {'delete', 'overwrite'}),
                'openWorldHint': any(effect.startswith('network.') for effect in row['required_effects']),
                'idempotentHint': retry_mode == 'idempotent' and qualified,
            },
        }
    installation_refs = _installation_refs(binding)
    return copy.deepcopy({
        'manifest_digest': manifest_hash, 'slot_id': slot, 'policy_sha256': policy_hash,
        'router_rows': [rows[route] for route in sorted(rows)], 'schemas': schemas, 'mcp_tools': mcp_tools,
        'cli_shapes': {route: row['cli_argv_shape'] for route, row in rows.items()},
        'json_contracts': {route: row['json_contract_ref'] for route, row in rows.items()},
        'resolved_params': resolved_params, 'pinned_schema_digests': request_pins,
        'mini_schema_digests': mini_pins, 'control_schema_digests': controls,
        'route_availability': {route: row['availability'] for route, row in rows.items()},
        'route_installation': {route: selected if route.startswith(prefix + '.') else None for route in rows},
        'installation_refs': installation_refs,
        'unresolved_conformance_refs': unresolved_cases,
    })


def _collision_key(word):
    return word.replace('-', '_')


def _check_collisions(bundle, existing_rows):
    indexes = {'ROUTER id': {}, 'MCP name': {}, 'CLI shape': {}, 'schema id': {}}

    def register(row, *, generated):
        name = row.get('mcp_name')
        if generated and name and _collision_key(name) == 'vp_rea_call':
            raise PublicationError('COLLISION', 'Reserved MCP tool vp_rea_call.')
        cli = row.get('cli_argv_shape')
        fields = {'ROUTER id': row['id'], 'MCP name': _collision_key(name) if name else None,
                  'CLI shape': tuple(map(_collision_key, cli)) if cli else None,
                  'schema id': row.get('input_schema_ref')}
        for label, key in fields.items():
            if key is None:
                continue
            index = indexes[label]
            if key in index:
                previous, from_bundle = index[key]
                shared = (row['id'].startswith('host.') and not (generated and from_bundle)
                          and jcs_bytes(previous) == jcs_bytes(row))
                if not shared:
                    raise PublicationError('COLLISION', f'Colliding {label} {key!r}.')
            index[key] = (row, generated)

    for row in existing_rows or ():
        register(row, generated=False)
    for row in bundle['router_rows']:
        register(row, generated=True)
    names = [_collision_key(name) for name in bundle['mcp_tools']]
    cli_shapes = [tuple(map(_collision_key, shape)) for shape in bundle['cli_shapes'].values()]
    if 'vp_rea_call' in names or len(names) != len(set(names)) or len(cli_shapes) != len(set(cli_shapes)):
        raise PublicationError('COLLISION', 'Duplicate or reserved name in a callable face.')


def _compile_inputs(manifest, binding, policy, registry, host_cases, host_mini_tools, admissions):
    if type(manifest) is not dict or type(registry) is not dict:
        raise PublicationError('MISSING_PROJECTION', 'Independent manifest and registry are required.')
    if binding is None:
        binding = {}
    try:
        return _compile(manifest, binding=binding, policy=policy if policy is not None else {}, registry=registry,
                        host_cases=host_cases, host_mini_tools=host_mini_tools, admissions=admissions)
    except PublicationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration) as error:
        raise PublicationError('SCHEMA_INVALID', 'Malformed publication inputs.') from error


def generate(manifest, *, binding=None, policy=None, registry=None, existing_rows=None,
             host_cases=None, host_mini_tools=None, admissions=()):
    """Compile a publication from independently verified snapshots.

    Admissions are host-verified scopes of (manifest digest, operation id,
    selected installation ref, policy digest) for external_process execution.
    """
    bundle = _compile_inputs(manifest, binding, policy, registry, host_cases, host_mini_tools, admissions)
    _check_collisions(bundle, existing_rows)
    return bundle


def verify_publication(bundle, manifest=None, *, binding=None, policy=None, registry=None,
                       existing_rows=None, host_cases=None, host_mini_tools=None, admissions=()):
    """Compare every face and index against the independent compiler inputs."""
    table_names = {'mcp_tools', 'cli_shapes', 'json_contracts', 'schemas', 'resolved_params',
                   'pinned_schema_digests', 'mini_schema_digests', 'control_schema_digests',
                   'route_availability', 'route_installation'}
    if type(bundle) is not dict or type(bundle.get('router_rows')) is not list or any(
        type(bundle.get(name)) is not dict for name in table_names
    ):
        raise PublicationError('MISSING_PROJECTION', 'A publication table is missing.')
    try:
        _check_collisions(bundle, existing_rows)
        expected = _compile_inputs(manifest, binding, policy, registry, host_cases, host_mini_tools, admissions)
        _, registered_minis = _registered_minis(registry, host_mini_tools)
        inventory = _expected_routes(manifest, _case_inventory(host_cases, registry), registered_minis)
        if {row['id'] for row in bundle['router_rows']} != inventory:
            raise PublicationError('MISSING_PROJECTION', 'ROUTER differs from the trusted route inventory.')
        for table in sorted(table_names):
            if bundle[table].keys() != expected[table].keys():
                raise PublicationError('MISSING_PROJECTION', f'{table} differs from the trusted inventory.')
        if bundle.keys() != expected.keys():
            raise PublicationError('MISSING_PROJECTION', 'Publication has missing or unexpected indexes.')
        for name, value in expected.items():
            if jcs_bytes(bundle[name]) != jcs_bytes(value):
                raise PublicationError('DIVERGENT_DEFAULT', f'{name} differs from the trusted projection.')
    except PublicationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise PublicationError('SCHEMA_INVALID', 'Malformed publication tables.') from error


def _row_for(bundle, route):
    for row in bundle['router_rows']:
        if row['id'] == route:
            return row
    raise PublicationError('SCHEMA_INVALID', f'Unknown route {route!r}.')


def _check_bundle_policy(manifest, bundle, policy):
    if bundle.get('manifest_digest') != manifest_digest(manifest) or bundle.get('slot_id') != manifest['slot_id']:
        raise PublicationError('SCHEMA_INVALID', 'Manifest differs from the verified publication.')
    if policy_sha256(policy) != bundle.get('policy_sha256'):
        raise PublicationError('SCHEMA_INVALID', 'Policy differs from the verified publication.')


def _current_resource(registry, ref, pin):
    if type(registry) is not dict or pin is None:
        raise PublicationError('SCHEMA_INVALID', f'Unverified registry resource {ref}.')
    try:
        return resource(registry, ref, pin)
    except PublicationError as error:
        raise PublicationError('SCHEMA_INVALID', f'Registry resource differs from pinned publication for {ref}.') from error


def _check_bundle_registry(bundle, row, registry):
    contract = row['json_contract_ref']
    if row['role'] is not None:
        _verify_request_result_pins(contract, registry if type(registry) is dict else {}, invocation=True)
    elif 'argument_schema_ref' in contract:
        for key in ('argument_schema_ref', 'result_schema_ref'):
            ref = contract[key]
            _current_resource(registry, ref, bundle['mini_schema_digests'].get(ref))


def _refuse_availability(state, route):
    codes = {'parked': 'AVAILABILITY_PARKED', 'not_installed': 'AVAILABILITY_NOT_INSTALLED',
             'blocked': 'AVAILABILITY_BLOCKED'}
    if state != 'admitted':
        raise PublicationError(codes.get(state, 'SCHEMA_INVALID'), f'Route {route} is {state}.')


def _gate_availability(manifest, bundle, row, arguments, binding, policy, current_licence):
    route = row['id']
    if route in (f'ext.{manifest["slot_id"]}', 'host.slot.inspect', 'host.slot.list'):
        return None
    if row['availability'] != 'admitted':
        _refuse_availability(row['availability'], route)
    if row['input_schema_ref'] is None:
        raise PublicationError('SCHEMA_INVALID', 'Admitted route has no verified input schema.')
    if not route.startswith('ext.'):
        return None
    if manifest['status'] == 'not_installed' or not _installation_refs(binding):
        _refuse_availability('not_installed', route)
    selected = _assert_installation_identity(manifest, bundle, route, arguments, binding)
    _check_identity(manifest, binding)
    state = availability_for(manifest, row['operation'] if row['role'] else None,
                             binding=binding, policy=policy, current_licence=current_licence)
    _refuse_availability(state, route)
    return selected


def _gate_perception(level, policy, normalized, vision_admission, context, *, perception_ceiling=None):
    configured = policy.get('perception_ceiling')
    observed = perception_ceiling if perception_ceiling is not None else configured
    if configured not in _LEVELS or observed not in _LEVELS or level not in _LEVELS:
        raise PublicationError('PERCEPTION_DENIED', 'Authenticated perception requirements are unresolved.')
    if _LEVELS.index(level) > min(_LEVELS.index(configured), _LEVELS.index(observed)):
        raise PublicationError('PERCEPTION_DENIED', 'Effective perception exceeds the authorized ceiling.')
    if level != 'vision' or normalized['dry_run']:
        return
    if vision_admission is None or context is None:
        raise PublicationError('PERCEPTION_DENIED', 'An authenticated ViewPoint_Tools lease decision is required.')
    decision = vision_admission(context, normalized)
    if decision == 'POOL_TIMEOUT':
        raise PublicationError('POOL_TIMEOUT', 'The authoritative vision acquisition timed out.')
    if decision != 'OK':
        raise PublicationError('PERCEPTION_DENIED', 'The authoritative vision lease check refused admission.')


def _validate_params(value, schema):
    require(value, schema, 'params')


def _validate_against_public(arguments, schema):
    require(arguments, schema, 'Arguments')
    if type(arguments) is not dict or type(arguments.get('request_id')) is not str or not _REQUEST_ID_RE.fullmatch(arguments['request_id']):
        raise PublicationError('SCHEMA_INVALID', 'Invalid request_id.')


def _inspect_target(manifest, bundle, binding, policy, slot_id, enrolled_slots):
    if type(slot_id) is not str or not _SLOT_ID_RE.fullmatch(slot_id):
        raise PublicationError('SCHEMA_INVALID', 'Invalid slot_id.')
    if slot_id == manifest['slot_id']:
        return manifest, bundle, binding if binding is not None else {}
    snapshot = (enrolled_slots or {}).get(slot_id)
    if snapshot is None:
        raise PublicationError('SCHEMA_INVALID', 'Inspect target has no verified enrollment.')
    target_manifest, target_bundle, target_binding = snapshot
    if target_binding is None:
        target_binding = {}
    if target_manifest['slot_id'] != slot_id:
        raise PublicationError('SCHEMA_INVALID', 'Inspect enrollment names a different slot.')
    _check_bundle_policy(target_manifest, target_bundle, policy)
    return target_manifest, target_bundle, target_binding


def normalize_arguments(manifest, route_id, arguments, *, binding=None, policy=None, registry=None,
                        bundle=None, enrolled_slots=None, current_licence=None, perception_ceiling=None,
                        vision_admission=None, context=None, admissions=(), manifest_digest=None):
    """Apply the verified schema and route admission to a public wrapper."""
    if bundle is None:
        bundle = generate(manifest, binding=binding, policy=policy, registry=registry, admissions=admissions)
    _check_bundle_policy(manifest, bundle, policy)
    if manifest_digest is not None and manifest_digest != bundle['manifest_digest']:
        raise PublicationError('SCHEMA_INVALID', 'Supplied manifest digest differs from the verified publication.')
    binding = binding if binding is not None else {}
    row = _row_for(bundle, route_id)
    selected = _gate_availability(manifest, bundle, row, arguments, binding, policy, current_licence)
    _check_bundle_registry(bundle, row, registry)
    try:
        schema = standalone(bundle['schemas'][row['input_schema_ref']], registry if registry is not None else {})
    except PublicationError as error:
        raise PublicationError('SCHEMA_INVALID', 'Published argument schema dependencies no longer resolve.') from error
    _validate_against_public(arguments, schema)
    props = schema['properties']
    filled = {key: copy.deepcopy(prop['default']) for key, prop in props.items() if 'default' in prop}
    filled.update(copy.deepcopy(arguments))
    request_id, dry_run = filled.pop('request_id'), filled.pop('dry_run')
    normalized = {'route_id': route_id, 'request_id': request_id, 'dry_run': dry_run,
                  'policy_sha256': bundle['policy_sha256'], 'manifest_digest': row['manifest_digest']}
    if row['role'] is not None:
        contract = row['json_contract_ref']
        if not dry_run and contract['execution_profile'] == 'external_process':
            scope = (bundle['manifest_digest'], row['operation'], selected, bundle['policy_sha256'])
            if scope not in admissions:
                raise PublicationError('PROFILE_NOT_ADMITTED', 'The current host profile admission is absent.')
        normalized.update({key: filled[key] for key in ('inputs', 'params', 'perception_level', 'determinism_mode')})
        normalized.update(slot_id=manifest['slot_id'], op_id=row['operation'], installation_ref=selected)
    else:
        if route_id == 'host.slot.inspect':
            target_manifest, target_bundle, target_binding = _inspect_target(
                manifest, bundle, binding, policy, filled['slot_id'], enrolled_slots)
            selected = _assert_installation_identity(target_manifest, target_bundle,
                        f'ext.{target_manifest["slot_id"]}.conform', filled, target_binding, discovery=True)
        if selected is not None:
            filled['installation_ref'] = selected
        normalized['arguments'] = filled
    passive_route = route_id in (f'ext.{manifest["slot_id"]}', 'host.slot.inspect', 'host.slot.list')
    if not passive_route:
        level = normalized.get('perception_level', row['perception']['required_level'])
        _gate_perception(level, policy, normalized, vision_admission, context, perception_ceiling=perception_ceiling)
    return normalized


def control_envelope(manifest, route_id, arguments, *, binding=None, policy=None, registry=None, context=None):
    """Map public request fields onto the control carrier before validation."""
    if type(arguments) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'Arguments must be an object.')
    request_id = arguments.get('request_id')
    if type(request_id) is not str or not _REQUEST_ID_RE.fullmatch(request_id):
        raise PublicationError('SCHEMA_INVALID', 'Invalid request_id.')
    dry_run = arguments.get('dry_run', False)
    if type(dry_run) is not bool:
        raise PublicationError('SCHEMA_INVALID', 'dry_run must be boolean.')
    return {'schema': 'vp.control.req/v1', 'request_id': request_id, 'op': _CONTROL_OP,
            'context': context, 'dry_run': dry_run,
            'args': {'route_id': route_id, 'arguments': {key: value for key, value in arguments.items()
                     if key not in ('request_id', 'dry_run')}}}


def router_invoke(manifest, bundle, route_id, arguments, **host):
    if type(arguments) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'Arguments must be an object.')
    return normalize_arguments(manifest, route_id, arguments, bundle=bundle, **host)


def mcp_invoke(manifest, bundle, mcp_name, arguments, **host):
    row = next((row for row in bundle['router_rows'] if row['mcp_name'] == mcp_name), None)
    if row is None:
        raise PublicationError('SCHEMA_INVALID', 'Unknown MCP tool.')
    return router_invoke(manifest, bundle, row['id'], arguments, **host)


def json_invoke(manifest, bundle, envelope, *, registry=None, context=None, **host):
    if type(envelope) is not dict or set(envelope) - {'schema', 'request_id', 'op', 'context', 'args', 'dry_run'}:
        raise PublicationError('SCHEMA_INVALID', 'Unexpected control envelope key or shape.')
    if envelope.get('schema') != 'vp.control.req/v1' or envelope.get('op') != _CONTROL_OP:
        raise PublicationError('SCHEMA_INVALID', 'Invalid control selector.')
    args = envelope.get('args')
    if type(args) is not dict or set(args) != {'route_id', 'arguments'}:
        raise PublicationError('SCHEMA_INVALID', 'Invalid control envelope args.')
    route, inner = args['route_id'], args['arguments']
    if type(route) is not str or route not in bundle['json_contracts'] or type(inner) is not dict:
        raise PublicationError('SCHEMA_INVALID', 'Unknown route or malformed arguments.')
    if {'request_id', 'dry_run'} & inner.keys():
        raise PublicationError('SCHEMA_INVALID', 'request_id and dry_run belong at the envelope root.')
    pin = bundle['control_schema_digests'].get(_CONTROL_REQUEST_REF)
    schema = _current_resource(registry, _CONTROL_REQUEST_REF, pin)
    try:
        require(envelope, standalone(schema, registry, control_op=_CONTROL_OP), 'Control request')
    except PublicationError as error:
        if error.code == 'MISSING_PROJECTION':
            raise
        raise PublicationError('SCHEMA_INVALID', 'Control request failed pinned schema validation.') from error
    public = dict(inner, request_id=envelope.get('request_id'), dry_run=envelope.get('dry_run', False))
    return router_invoke(manifest, bundle, route, public, registry=registry, context=context, **host)


def cli_invoke(manifest, bundle, argv, args_document=None, *, request_id=None, dry_run=None, **host):
    from .cli import parse_slot_command

    route, arguments, _ = parse_slot_command(argv, args_document=args_document,
                                             request_id=request_id, dry_run=dry_run)
    if route not in bundle['cli_shapes']:
        raise PublicationError('SCHEMA_INVALID', 'Unknown CLI route.')
    return router_invoke(manifest, bundle, route, arguments, **host)
