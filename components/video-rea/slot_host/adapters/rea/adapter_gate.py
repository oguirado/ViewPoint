# SPDX-License-Identifier: AGPL-3.0-only
"""Run the host oracle bank against the offline morluto/rea slot declaration."""

import argparse
from copy import deepcopy
from importlib import import_module
from pathlib import Path
import sys

from slot_host.projections import generate, verify_publication
from slot_host.projections.schema import digest, jcs_bytes, require, standalone
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.receipts import build_attempt_receipt, finalize_receipt, receipt_digest
from slot_host.runtime.replay_lineage import (
    ReplayLineageError, resolve_replay_lineage, validate_parent_episode_id,
)


# This is the existing host-owned bank runner, shipped in the preceding adapter.
# Its observers consume synthetic fixture inputs and call host APIs.
oracle = import_module('slot_host.adapters.rust-anything.adapter_gate')
PACKAGE = Path(__file__).resolve().parent
PROFILE = 'offline_fixture'
PRODUCER = 'rea.adapter_gate'
MANIFEST_SCHEMA = 'https://viewpoint.local/contracts/rea/rea.slot_manifest_v2.schema.json'
CASE_SCHEMA = 'https://viewpoint.local/contracts/rea/rea.conformance_case_v1.schema.json'
EVIDENCE_SCHEMA = 'https://viewpoint.local/contracts/rea/rea.slot_evidence_profile_v1.schema.json'
UPSTREAM = 'https://github.com/morluto/rea'
RELEASE = f'{UPSTREAM}/blob/160d821ac9a0310afcb8d1fc427862dd6508add5'


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


class OfflineKit:
    def __init__(self, package=PACKAGE):
        self.manifest = oracle._read(package / 'rea.slot.json')
        self.policy = {'perception_ceiling': 'none'}
        self.inventory = {'rea-discovery': {'manifest': self.manifest, 'executable_path': None}}
        self.registry = self._schema_registry()
        self._verify_declaration()
        self.cases = {}
        self.host_cases = []
        catalog = oracle._read(package / 'fixtures.json')
        self.catalog_sha256 = digest(catalog)
        self.perception_recording = catalog['perception_recording']
        self.retained_replay = catalog['retained_replay']
        self._load_cases(catalog)
        self.bundle = self.compile()
        verify_publication(self.bundle, self.manifest, policy=self.policy, registry=self.registry,
                           host_cases=self.host_cases)
        _require(not self.bundle['unresolved_conformance_refs'], 'unresolved offline conformance references')
        _require(all(state == 'not_installed' for route, state in self.bundle['route_availability'].items()
                     if route == 'ext.rea' or route.startswith('ext.rea.')),
                 'the offline kit published an available external route')

    @staticmethod
    def _schema_registry():
        contracts = oracle.CONTRACTS
        control = oracle.COMPONENT.parent / 'vp-direct' / 'schemas'
        paths = (contracts / 'manifest/rea.slot_manifest_v2.schema.json',
                 oracle.BANK / 'rea.conformance_case_v1.schema.json',
                 contracts / 'evidence/rea.slot_evidence_profile_v1.schema.json',
                 control / 'vp.control.req_v1.schema.json',
                 control / 'vp.control.reply_v1.schema.json')
        schemas = [oracle._read(path) for path in paths]
        exchange = contracts / 'ext'
        pins = oracle._read(exchange / 'PINS.json')['files']
        for name in ('rea.provider_slot_manifest_v1.schema.json',
                     'rea.slot_request_v1.schema.json', 'rea.slot_result_v1.schema.json'):
            schemas.append(oracle._read(exchange / 'generations' / oracle.GENERATION / name, pins[name]))
        return {schema['$id']: schema for schema in schemas}

    def _verify_declaration(self):
        manifest = self.manifest
        require(manifest, standalone(self.registry[MANIFEST_SCHEMA], self.registry), 'Slot manifest')
        upstream = manifest['upstream']
        _require(manifest['slot_id'] == manifest['provider_ref']['provider_id'] == 'rea'
                 and upstream['name'] == 'morluto/rea' and upstream['version'] == '6.8.0'
                 and upstream['source_url'] == UPSTREAM and upstream['licence_id'] == 'MIT'
                 and upstream['licence_evidence_refs'] == [f'{RELEASE}/LICENSE#L1-L3']
                 and upstream['component_notices'] == [f'{RELEASE}/THIRD_PARTY_NOTICES.md'],
                 'morluto/rea release or licence evidence differs')
        provider = manifest['provider_ref']
        transport = manifest['transport']
        pending_pins = (provider['provider_sha256'], provider['implementation_sha256'],
                        manifest['capabilities']['adapter_identity']['adapter_sha256'])
        _require(manifest['status'] == 'not_installed' and upstream['user_installed'] is True
                 and provider['provider_revision'] == 'unpinned'
                 and all(pin is None for pin in pending_pins)
                 and transport['executable_binding'] == 'user_selected'
                 and (transport['kind'], transport['framing'], transport['argv_prefix'])
                 == ('mcp_stdio', 'jsonrpc_stdio', ['mcp'])
                 and all(row['execution_profile'] == PROFILE and row['request_digest'] is None
                         and row['result_digest'] is None for row in manifest['contract_refs'])
                 and manifest['evidence']['shared_schema_ref'] == 'evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending',
                 'external installation or admission pins must remain pending')

    def _load_cases(self, catalog):
        index = oracle._read(oracle.BANK / 'INDEX.json')
        _require(index['execution_profile'] == catalog['execution_profile'] == PROFILE,
                 'the oracle bank requires an offline fixture profile')
        polarities = {'nominal': 'fixture', 'negative': 'negctl'}
        expected = {(test_id, polarity) for test_id in oracle.CASE_IDS for polarity in polarities}
        bank_rows = {(row['test_id'], row['case']): row for row in index['cases']}
        _require(bank_rows.keys() == expected and len(index['cases']) == len(expected),
                 'oracle bank inventory differs')

        refs = {polarity: set() for polarity in polarities}
        for row in catalog['cases']:
            test_id, polarity = key = row['test_id'], row['case']
            _require(key in expected and key not in self.cases, f'unexpected or duplicate fixture {key}')
            bank_row = bank_rows[key]
            path = Path('cases') / test_id / f'{polarity}.json'
            _require(path.as_posix() == bank_row['path'], 'oracle bank case path differs')
            case = oracle._read(oracle.BANK / path, row['sha256'])
            require(case, self.registry[CASE_SCHEMA], 'Conformance case')
            case_id = oracle.CASE_IDS[test_id][0] + ('_negative' if polarity == 'negative' else '')
            ref = f'{polarities[polarity]}:host/oracle-bank/{test_id}@1'
            _require((case['test_id'], case['case']) == key and row['id'] == case_id and row['ref'] == ref,
                     'fixture identity differs')
            self.cases[key] = self.registry[ref] = case
            refs[polarity].add(ref)
            self.host_cases.append({'id': case_id, 'ref': ref, 'digest': digest(case),
                                    'summary': f'{test_id} ({polarity})', 'effects': [],
                                    'keywords': bank_row['router_keywords'],
                                    'perception': {'required_level': 'none', 'optional_levels': []}})
        conformance = self.manifest['conformance']
        _require(self.cases.keys() == expected
                 and set(conformance['required_checks']) == set(oracle.CASE_IDS)
                 and set(conformance['fixture_refs']) == refs['nominal']
                 and set(conformance['negative_control_refs']) == refs['negative'],
                 'the adapter does not resolve the complete oracle bank')

    def compile(self):
        return generate(self.manifest, policy=self.policy, registry=self.registry, host_cases=self.host_cases)

    def _verify_retained_replay(self, fixture):
        retained = self.retained_replay
        parent = retained['parent_episode_id']
        foreign_parent = validate_parent_episode_id(retained['foreign_parent_episode_id'])
        tape = digest(fixture['input_tape'])
        _require(tape == retained['input_tape_sha256'] and parent != foreign_parent,
                 'the adapter replay fixture must identify its retained tape and a distinct contrast')
        replay = {'input_tape_sha256': tape, 'determinism_mode': 'exact', 'controls': {
            'clock': 'not_applicable', 'rng': 'not_applicable', 'external_effects': 'not_applicable',
        }}
        retained_records = {(parent, retained['input_tape_sha256']): {
            'parent_episode_id': parent, 'input_tape_sha256': retained['input_tape_sha256'],
        }}

        def resolve(parent_id, tape_sha256):
            return retained_records.get((parent_id, tape_sha256))

        payload = {'parent_episode_id': parent, 'replay': replay}
        lineage = resolve_replay_lineage(payload, tape_sha256=tape, tape_resolver=resolve)
        try:
            resolve_replay_lineage({**payload, 'parent_episode_id': foreign_parent},
                                   tape_sha256=tape, tape_resolver=resolve)
        except ReplayLineageError:
            return {'parent_episode_id': lineage['parent_episode_id'],
                    'input_tape_sha256': lineage['tape_sha256'],
                    'foreign_parent_rejected': foreign_parent}
        raise ValueError('the replay fixture accepted a foreign retained reference')

    def run(self, test_id):
        nominal = self.cases[test_id, 'nominal']['fixture']
        observe = oracle.CASE_IDS[test_id][1]
        upstream = self.manifest['upstream']
        evidence_schema = standalone(self.registry[EVIDENCE_SCHEMA], self.registry)
        results = {}
        for polarity in ('nominal', 'negative'):
            case = self.cases[test_id, polarity]
            fixture = deepcopy(nominal) if polarity == 'nominal' else oracle._negative_fixture(nominal, case['mutation'])
            fixture_sha = digest(fixture)
            identity = f'offline-rea-{test_id}-{polarity}-{fixture_sha[:16]}'
            opened = build_attempt_receipt(
                attempt_id=identity, request_id=identity, slot_id='rea', tool_version=upstream['version'],
                upstream_licence_id=upstream['licence_id'], source_url=upstream['source_url'],
                slot_role='replay', producer_kind='host_level', producer_id=PRODUCER,
                manifest_sha256=self.bundle['manifest_digest'],
                shared_schema_ref=self.manifest['evidence']['shared_schema_ref'],
            )
            lineage = self._verify_retained_replay(fixture) if test_id == 'outside-record-replay-compare' else None
            code, observation = observe(fixture, self)
            if lineage is not None:
                observation['fixture_replay_lineage'] = lineage
            expected = case['expected']
            outcome = 'pass' if code == 'ORACLE_PASS' else 'fail'
            assertions = {
                row['pointer']: jcs_bytes(oracle._at(observation, oracle._tokens(row['pointer'])))
                == jcs_bytes(row['equals'])
                for row in expected['assertions']
            }
            receipt = finalize_receipt(opened, None)
            # The fixture report attests ordering and keeps a reproducible receipt digest.
            del receipt['attempt']['started_at']
            require(receipt, evidence_schema, 'Offline evidence receipt')
            result = {'fixture_sha256': fixture_sha, 'fixture_catalog_sha256': self.catalog_sha256,
                      'case_sha256': digest(case), 'outcome': outcome, 'code': code, 'observation': observation,
                      'expected_code': expected['code'], 'assertions': assertions, 'receipt': receipt,
                      'passed': code == expected['code'] and outcome == expected['outcome'] and all(assertions.values()),
                      'receipt_sha256': receipt_digest(receipt)}
            results[polarity] = {**result, 'record_sha256': digest(result)}
        return results

    def gate(self):
        cases = {test_id: self.run(test_id) for test_id in oracle.CASE_IDS}
        return {'slot_id': 'rea', 'execution_profile': PROFILE, 'installation_ref': None, 'executable_path': None,
                'live_status': LIVE_EXECUTION_PARKED, 'owner_pending': ['MAIN', 'T01', 'T02', 'T03', 'T04', 'T05', 'T06'],
                'manifest_digest': self.bundle['manifest_digest'], 'fixture_catalog_sha256': self.catalog_sha256,
                'projection_sha256': digest(self.bundle), 'cases': cases,
                'passed': all(result['passed'] for pair in cases.values() for result in pair.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projections', action='store_true', help='Emit the verified publication bundle.')
    args = parser.parse_args(argv)
    try:
        kit = OfflineKit()
        result = kit.bundle if args.projections else kit.gate()
    except (IndexError, KeyError, OSError, TypeError, ValueError) as error:
        result = {'passed': False, 'live_status': LIVE_EXECUTION_PARKED, 'error': str(error)}
    sys.stdout.buffer.write(jcs_bytes(result) + b'\n')
    return 0 if result.get('passed', args.projections) else 1


if __name__ == '__main__':
    raise SystemExit(main())
