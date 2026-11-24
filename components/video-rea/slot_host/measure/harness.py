# SPDX-License-Identifier: AGPL-3.0-only
"""Drive the published REA projections through a sealed offline composition root."""

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from time import perf_counter_ns
from unittest.mock import patch
from uuid import uuid4

from slot_host.compare import tools as compare
from slot_host.projections import SlotProjector, control_envelope, generate, verify_publication
from slot_host.projections import cli, project
from slot_host.projections.mcp_lane import control_reply
from slot_host.projections.schema import PublicationError, digest, jcs_bytes, require
from slot_host.runtime import evidence_projection, tools
from video_rea.expose.wire import encode, loads

from . import oracle
from .evidence import Store, check_truth, occurrences, readback
from .host import Host
from .runtime import INSTALLATION, Runtime
from .seal import MANIFEST_SHA256, FixtureError, FixtureReferenceError, Seal, read_regular, sha

CONTEXT = {'caller': 'agent', 'agent_instance': 'rea-medicion-fixture'}
FACES = tuple(tools.MINI_TOOLS) + tuple(compare.MINI_TOOLS)
TRANSPORTS = ('router', 'mcp', 'cli', 'json')
RUNTIME = frozenset({'slot.inspect', 'behaviour.record', 'behaviour.replay'})
CONTROL_HANDLERS = {
    'journal.verify': tools.handle_journal_control,
    'reconstruction.obligations': tools.handle_reconstruction_control,
    'tape.generate': tools.handle_tape_control,
    'effect.isolate': tools.handle_effect_control,
}
ROOT = Path(__file__).resolve().parents[2]
GUARD = Lock()
CHECK_SCHEMAS = ROOT / 'slot_host/runtime/tests/fixtures_v03'
REGISTRY_SCHEMAS = (
    ROOT / 'contracts/manifest/rea.slot_manifest_v2.schema.json',
    ROOT / 'contracts/evidence/rea.slot_evidence_profile_v1.schema.json',
    ROOT / '../vp-direct/schemas/vp.control.req_v1.schema.json',
    ROOT / '../vp-direct/schemas/vp.control.reply_v1.schema.json',
)


def registry(seal, schemas):
    documents = [loads(schemas[path]) for path in REGISTRY_SCHEMAS]
    documents += [seal.inputs['request_schema'], seal.inputs['result_schema']]
    resources = {document['$id']: document for document in documents}
    for module in (tools, compare):
        resources.update(module.ARGUMENT_SCHEMAS)
        resources.update(module.RESULT_SCHEMAS)
    resources['host_mini_tools'] = {key: value for key, value in (tools.MINI_TOOLS | compare.MINI_TOOLS).items()
                                    if key != 'slot.inspect'}
    return resources


class Publication:
    def __init__(self, seal):
        self.manifest = seal.inputs['slot']
        self.manifest_digest = digest(self.manifest)
        self.policy = {'perception_ceiling': 'vision', 'licence': 'allowed', 'evidence_schema_bound': True}
        slot = self.manifest
        provider = {key: slot['provider_ref'][key] for key in
                    ('provider_id', 'provider_revision', 'provider_sha256', 'implementation_sha256')}
        adapter = slot['capabilities']['adapter_identity']
        schemas = {path: read_regular(path) for path in REGISTRY_SCHEMAS}
        self.options = {
            'registry': registry(seal, schemas), 'policy': self.policy,
            'binding': {'installations': [INSTALLATION], **provider,
                        'adapter_id': adapter['adapter_id'], 'adapter_sha256': adapter['adapter_sha256']},
        }
        started = perf_counter_ns()
        self.bundle = generate(slot, **self.options)
        self.compile_ns = perf_counter_ns() - started
        verify_publication(self.bundle, slot, **self.options)
        regenerated = generate(slot, **self.options)
        raw = jcs_bytes(self.bundle)
        if jcs_bytes(regenerated) != raw:
            raise FixtureError('Regenerated projection bytes differ.')
        self.projection_sha256 = sha(raw)
        rows = self.bundle['router_rows']
        ids = {row['id'] for row in rows}
        if len(ids) != len(rows) or {'host.' + face for face in FACES} - ids:
            raise FixtureError('Projection inventory is incomplete.')
        for face in FACES:
            route = 'host.' + face
            row = next(row for row in rows if row['id'] == route)
            # The installed publication explicitly parks visual; fixture
            # admission below exercises its schema and real decoder/handler.
            if (route not in self.bundle['json_contracts'] or route not in self.bundle['cli_shapes']
                    or not row['input_schema_ref']
                    or (face != 'visual.compare' and row['mcp_name'] not in self.bundle['mcp_tools'])):
                raise FixtureError('Requested fixture face lacks an expected projection.')
        self.rows = len(rows)
        paths = [*ROOT.joinpath('slot_host/projections').glob('*.py'),
                 *ROOT.joinpath('slot_host/runtime').glob('*.py'),
                 *ROOT.joinpath('slot_host/compare').glob('*.py'),
                 *ROOT.joinpath('video_rea/expose').glob('*.py'),
                 *ROOT.joinpath('slot_host/measure').glob('*.py'),
                 ROOT / 'slot_host/__init__.py', ROOT / 'video_rea/__init__.py',
                 ROOT / 'pixel_compare.py', ROOT / 'replay_compare.py', ROOT / 'episode_provenance.py',
                 CHECK_SCHEMAS / 'attempt_evidence_record.v0.3.schema.json',
                 CHECK_SCHEMAS / 'rea_retention_index.v1.schema.json']
        self.build_sources = {str(path.relative_to(ROOT)): sha(read_regular(path)) for path in sorted(paths)}
        self.build_sources.update({str(path.relative_to(ROOT)): sha(raw) for path, raw in schemas.items()})
        self.build_sources['slot_host/measure/fixtures/fixture_runner.py'] = sha(seal.executable)
        self.build_sha256 = digest(self.build_sources)

    def policy_pins(self, seal, face, assignment):
        pins = {'publication': digest(self.policy)}
        if face in RUNTIME:
            operation = assignment['arguments'].get('operation_id', 'record')
            pins['runtime'] = digest(Runtime.policy(self.manifest, operation))
        if face in compare.MINI_TOOLS:
            # A closed-argument refusal still names the sealed fixture policy in
            # the workload identity. The reference grants no dispatch authority.
            ref = assignment['arguments'].get('policy_ref', 'policy:fixture')
            row = seal.inputs['assets'][ref]
            policy = Host.comparison_policy(face, row)
            pins['comparator_source'] = row['sha256']
            pins['comparator'] = digest(json.loads(json.dumps(asdict(policy), default=sorted)))
        return pins


class Session:
    def __init__(self, publication, host, root):
        self.publication = publication
        self.host = host
        self.face = host.face
        self.runtime = Runtime(host, root) if self.face in RUNTIME else None
        self.routed = self.dispatch_ns = 0
        self.normalized_sha256 = None
        self.contrast = None
        self.projector = SlotProjector(
            publication.manifest, publication.bundle, **publication.options,
            context=CONTEXT, authenticated_context=CONTEXT, dispatch=self.dispatch,
            vision_admission=self.vision_admission)
        self.availability = project._gate_availability

    def availability_for(self, manifest, bundle, row, arguments, binding, policy, current_licence):
        if (self.face == 'visual.compare' and bundle is self.projector.bundle
                and row['id'] == 'host.visual.compare' and row['availability'] == 'parked'):
            row = dict(row, availability='admitted')
        return self.availability(manifest, bundle, row, arguments, binding, policy, current_licence)

    def vision_admission(self, context, normalized):
        allowed = (self.face == 'visual.compare' and context == CONTEXT
                   and normalized['route_id'] == 'host.visual.compare'
                   and normalized['request_id'] == self.host.store.request_id)
        return 'OK' if allowed else 'PERCEPTION_DENIED'

    def inspect_projection(self, inspection):
        manifest = self.publication.manifest
        slot = {key: manifest[key] for key in self.projector._DESCRIBE_SLOT_KEYS - {'manifest_digest'}}
        slot['manifest_digest'] = self.publication.manifest_digest
        selected = INSTALLATION if inspection['readiness']['ready'] else None
        return {'slot': slot, 'installation_refs': [INSTALLATION], 'selected_installation_ref': selected,
                'conformance_receipt_ref': None, 'unresolved_conformance_refs': []}

    def dispatch(self, envelope):
        normalized = self.projector.prepare(envelope, authenticated_context=CONTEXT,
                                            vision_admission=self.vision_admission)
        request = normalized['request_id']
        self.host.authorize(request)
        arguments = normalized['arguments']
        expected = self.host.assignment['arguments']
        if self.face == 'slot.inspect':
            actual = {k: v for k, v in arguments.items() if k != 'installation_ref'}
        else:
            actual = arguments['payload']
        if (normalized['route_id'] != 'host.' + self.face or normalized['dry_run']
                or jcs_bytes(expected) != jcs_bytes(actual)):
            raise FixtureError('Normalized intent differs from the sealed assignment.')
        self.normalized_sha256 = digest({k: v for k, v in normalized.items() if k != 'request_id'})
        self.routed += 1
        start = perf_counter_ns()
        try:
            if self.face == 'error.diagnose':
                reply = tools.handle_diagnostic_control(normalized)
            elif self.face in CONTROL_HANDLERS:
                reply = CONTROL_HANDLERS[self.face](normalized, host=self.host)
            else:
                if self.face in RUNTIME:
                    result = self.runtime.dispatch(self.face, request, arguments if self.face == 'slot.inspect' else actual)
                else:
                    result = compare.handle(self.face, actual, request_id=request, host=self.host)
                visible = self.inspect_projection(result) if self.face == 'slot.inspect' else result
                reply = control_reply(request, 'OK', ok=True, effective=digest(normalized),
                                      result={'kind': 'host_result', 'outside_capsule': True, 'payload': visible})
            return reply
        except (tools._ControlError, PublicationError) as error:
            return control_reply(request, error.code, ok=False, effective=digest(normalized))
        except FixtureReferenceError as error:
            if self.face not in compare.MINI_TOOLS:
                raise
            self.contrast = str(error)
            return control_reply(request, 'SCHEMA_INVALID', ok=False, effective=digest(normalized))
        finally:
            self.dispatch_ns += perf_counter_ns() - start

    def invoke(self, transport):
        arguments = {'request_id': self.host.store.request_id}
        route = 'host.' + self.face
        payload = self.host.assignment['arguments']
        arguments.update(payload if self.face == 'slot.inspect' else {'payload': payload})
        if transport == 'router':
            return self.projector.invoke(route, arguments)
        if transport == 'mcp':
            name = next(row['mcp_name'] for row in self.projector.bundle['router_rows'] if row['id'] == route)
            return self.projector.call_tool(name, loads(encode(arguments)))['structuredContent']
        if transport == 'json':
            envelope = control_envelope(self.publication.manifest, route, arguments, context=CONTEXT)
            return self.projector.handle_envelope(loads(encode(envelope)))
        command = ['inspect', arguments['slot_id']] if self.face == 'slot.inspect' else ['mini', self.face]
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            cli.main([*command, '--args', jcs_bytes(arguments).decode()], projector=self.projector)
        return loads(stdout.getvalue().encode())


def check_runtime(artifacts, objects, owner, runtime, expected_parent):
    opened, terminal, output = (artifacts[key] for key in ('open-profile', 'terminal-profile', 'stdout'))
    started, observed, index = (artifacts[key] for key in ('started', 'terminal', 'retention_index'))
    schema = loads(read_regular(CHECK_SCHEMAS / 'attempt_evidence_record.v0.3.schema.json'))
    for record in (started, observed):
        require(record, schema, 'Fixture attempt evidence')
    require(index, loads(read_regular(CHECK_SCHEMAS / 'rea_retention_index.v1.schema.json')),
            'Fixture retention index')
    if any(record.get('parent_episode_id') != expected_parent
           for record in (opened['episode'], terminal['episode'], started, observed, index)):
        raise FixtureError('Paged runtime parent differs from the sealed assignment and tape.')

    payload = output['result_payload'].encode()
    provider_result = terminal['attempt']['slot_result']
    process = observed['execution']['process']
    attempt_id = evidence_projection.v03_attempt_id(owner['attempt_id'])
    if (any(item['attempt'][key] != expected for item in (opened, terminal) for key, expected in owner.items())
            or output['request_id'] != owner['request_id']
            or output['sha256'] != sha(payload)
            or any(provider_result[key] != output[key] for key in ('sha256', 'schema', 'status', 'rc'))
            or objects['stdout']['sha256'] != runtime.producer['host_result_sha256']
            or any(record['attempt_id'] != attempt_id for record in (started, observed, index))
            or observed['start_sha256'] != evidence_projection.record_digest(started)
            or started['external_process']['request_id'] != owner['request_id']
            or started['external_process']['implementation_sha256'] != runtime.implementation
            or index['request_id'] != owner['request_id']
            or index['request_sha256'] != started['external_process']['request_sha256']
            or index['profile_sha256'] != evidence_projection.record_digest(terminal)
            or observed['artifact']['sha256'] != output['sha256']
            or observed['cleanup']['state'] != 'confirmed_retired'
            or process['pid'] != runtime.producer['process_id']
            or process['starttime_ticks'] != runtime.producer['starttime']):
        raise FixtureError('Runtime v0.3 start, terminal, output or retention index does not bind.')
    return {'receipt': terminal}, loads(payload)


def fixture_result(host, runtime, artifacts, objects, owner, reply):
    face = host.face
    if face in ('behaviour.record', 'behaviour.replay'):
        args = host.assignment['arguments']
        tape = host.asset(host.assignment['refs'][0], 'tape')
        parent = args.get('parent_episode_id')
        if (tape['sha256'] != args['tape_sha256']
                or parent != (tape['identity'] if face == 'behaviour.replay' else None)):
            raise FixtureError('Runtime parent or digest differs from the sealed tape.')
        result, seen = check_runtime(artifacts, objects, owner, runtime, parent)
        oracle.same(reply['receipt'], result['receipt'], 'Projected runtime profile')
        oracle.same(reply['shared_records'], {key: artifacts[key] for key in ('started', 'terminal', 'retention_index')},
                    'Projected shared attempt records')
        oracle.same(sha(reply['stdout'].encode()), objects['stdout']['sha256'], 'Projected runtime output')
        return result, seen
    if face in ('slot.inspect', 'error.diagnose', 'reconstruction.obligations'):
        return oracle.stateless_result(host, runtime, owner['request_id'], reply)
    refs = {key: row['ref'] for key, row in objects.items()}
    if face == 'journal.verify':
        result, seen = oracle.journal_result(host, artifacts['journal'], refs['journal'], owner['request_id'])
    elif face == 'tape.generate':
        result, seen = oracle.tape_result(host, artifacts, refs)
    elif face == 'effect.isolate':
        result, seen = oracle.effect_result(host, artifacts, refs, owner['request_id'])
    else:
        if host.comparison_witness is None:
            raise FixtureError('Missing comparison receipt witness.')
        result, seen = oracle.comparison_result(host, artifacts, objects, owner)
    oracle.same(reply, result, f'Projected {face}')
    return result, seen


def _one(publication, seal, face, case, transport, policy_sha256):
    key = face + '/' + case
    assignment = seal.inputs['cases'][key]
    truth = seal.truth['assignments'][key]
    request = 'req-medicion-' + uuid4().hex
    owner = {'request_id': request, 'attempt_id': 'att-' + request}
    with tempfile.TemporaryDirectory(prefix='rea-medicion-') as directory:
        root = Path(directory)
        store = Store(root / 'evidence', **owner)
        host = Host(seal, face, assignment, store)
        session = Session(publication, host, root)
        start = perf_counter_ns()
        with (patch.object(tempfile, 'tempdir', directory),
              patch.object(project, '_gate_availability', session.availability_for)):
            reply = session.invoke(transport)
        transport_ns = perf_counter_ns() - start
        runtime = session.runtime
        launches = len(runtime.processes) if runtime is not None else 0
        if (session.routed != truth['dispatches'] or launches != truth['launches']
                or host.visual.grants != truth['leases'] or len(store.index) != truth['artifacts']):
            raise FixtureError(f'{face}/{case}/{transport}: dispatch, launch, lease or evidence inventory differs.')
        artifacts, paging = readback(store, tuple(store.index), owner)
        reply_payload = reply['result'].get('payload')
        result, seen = ({}, [])
        if reply['ok']:
            result, seen = fixture_result(host, runtime, artifacts, paging['objects'], owner, reply_payload)
        if reply['code'] == truth['code']:
            check_truth(truth['checks'], result, artifacts)
        quality = occurrences(truth['occurrences'], seen)
        if not reply['ok'] and reply['code'] == truth['code']:
            quality.update(refused=quality['missing'], missing=0)
        qualified = {key: quality[key] for key in ('missing', 'duplicate', 'wrong', 'late', 'refused', 'unknown') if quality[key]}
        result_summary = result
        if 'stdout' in artifacts:
            result_summary = {'occurrences': seen, 'provider_result': result['receipt']['attempt']['slot_result']}
        result_digests = {key: row['sha256'] for key, row in paging['objects'].items()}
        bound = {'assignment': key, **owner, 'normalized_intent_sha256': session.normalized_sha256,
                 'build_sha256': publication.build_sha256, 'projection_sha256': publication.projection_sha256,
                 'policy_sha256': policy_sha256, 'slot_manifest_sha256': publication.manifest_digest,
                 'input_sha256': {ref: seal.inputs['assets'][ref]['sha256'] for ref in assignment['refs']},
                 'result_sha256': digest(result_digests) if result_digests else None,
                 'reply_payload_sha256': digest(reply_payload) if reply_payload is not None else None,
                 'index_sha256': paging['index_sha256'], 'control_code': reply['code']}
        module = compare if face in compare.MINI_TOOLS else tools
        bound['handler_sha256'] = publication.build_sources[str(Path(module.__file__).relative_to(ROOT))]
        if 'stdout' in artifacts:
            bound['invoked_artifact_sha256'] = runtime.implementation
        if face in ('behaviour.record', 'behaviour.replay'):
            bound['parent_episode_id'] = artifacts['started'].get('parent_episode_id') if artifacts else None
        if 'report' in artifacts:
            bound['comparison_policy_sha256'] = artifacts['report']['binding']['policy_sha256']
        cleanup_start = perf_counter_ns()
        expected_entries = {'fixture-adapter', 'evidence'} if face in RUNTIME else {'evidence'}
        if (not host.visual.settled() or (runtime is not None and not runtime.settled())
                or set(os.listdir(root)) != expected_entries):
            raise FixtureError('Fixture processes, scratch, decode or lease did not settle.')
    if root.exists():
        raise FixtureError('Fixture custody directory survived cleanup.')
    bound['terminal_state'] = 'fixture_settled'
    sample = {'transport': transport, **owner, 'code': reply['code'],
              'normalized_intent_sha256': session.normalized_sha256, 'quality': quality,
              'terminal_state': bound['terminal_state'], 'readback': paging, 'result': result_summary,
              'witness': bound | {'sha256': digest(bound)}, 'contrast': session.contrast,
              'transport_ns': transport_ns, 'dispatch_ns': session.dispatch_ns,
              'cleanup_ns': perf_counter_ns() - cleanup_start,
              'dispatches': session.routed, 'launches': launches,
              'leases_acquired': host.visual.grants, 'leases_released': host.visual.releases}
    if (reply['code'] != truth['code'] or reply['ok'] is not (truth['code'] == 'OK')
            or qualified != truth.get('quality_errors', {})):
        raise FixtureError(f'{face}/{case}/{transport}: control outcome or occurrence witness differs from frozen truth.',
                           sample=sample)
    return sample


def run(manifest_path, face, case, transports):
    with GUARD:
        seal = Seal(manifest_path)
        publication = Publication(seal)
        transports = tuple(transports)
        assignment = None
        if face == 'projection':
            if case != 'nominal':
                raise FixtureError('No such sealed projection case.')
            transports = ()
        else:
            assignment = seal.inputs['cases'].get(face + '/' + case)
            if assignment is None:
                raise FixtureError('Fixture assignment is unlisted.')
        pins = publication.policy_pins(seal, face, assignment)
        policy_sha256 = digest(pins)
        inputs = {ref: seal.inputs['assets'][ref]['sha256'] for ref in assignment['refs']} if assignment else {}
        runs = [_one(publication, seal, face, case, transport, policy_sha256) for transport in transports]
        if assignment and len({(row['normalized_intent_sha256'], row['code']) for row in runs}) != 1:
            raise FixtureError('ROUTER, MCP, CLI and JSON changed normalized intent or outcome.')
        identity = {'fixture_manifest_sha256': MANIFEST_SHA256, 'generation': seal.generation,
                    'face': face, 'case': case, 'transports': list(transports),
                    'assignment_sha256': digest(assignment) if assignment else None,
                    'projection_sha256': publication.projection_sha256, 'build_sha256': publication.build_sha256,
                    'policy_sha256': policy_sha256, 'policy_pins': pins,
                    'slot_manifest_sha256': publication.manifest_digest, 'input_sha256': inputs}
        return {'schema': 'rea.medicion.fixture_value/v1', 'scope': 'fixture',
                'ok': True, 'value_id': 'fixture:medicion:sha256:' + digest(identity), **identity,
                'projection': {'verified_rows': publication.rows, 'compile_ns': publication.compile_ns,
                               'regenerated_sha256': publication.projection_sha256,
                               'rows_per_second': publication.rows * 1_000_000_000 / publication.compile_ns},
                'runs': runs}
