# SPDX-License-Identifier: AGPL-3.0-only
"""Validate and qualify the rust-anything discovery package with offline host fixtures."""

import argparse
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
import sys

from slot_host.projections import generate, manifest_digest, verify_publication
from slot_host.projections.schema import digest, jcs_bytes, require, standalone
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.receipts import build_attempt_receipt, finalize_receipt, receipt_digest
from video_rea.expose.wire import MAX_LINE_BYTES, loads

from . import fixture_observers as observers


PACKAGE = Path(__file__).resolve().parent
COMPONENT = PACKAGE.parents[2]
CONTRACTS = COMPONENT / 'contracts'
BANK = CONTRACTS / 'oracle'
GENERATION = '6deabcc80b13c15c792858fb62c36a103bbf84d40691e32bc755785f12cd4d35'
PROFILE = 'offline_fixture'
UPSTREAM = 'https://github.com/erkamyaman/rust-anything'
SOURCE_PIN = 'b30206b1af11c58706303dfe20a3bd2d819c1e68'
SOURCE = f'{UPSTREAM}/blob/{SOURCE_PIN}'
CASE_IDS = {
    'admit-and-project': ('admission', observers.admission),
    'outside-record-replay-compare': ('record_replay', observers.record_replay),
    'perception-and-pool': ('perception', observers.perception),
    'freshness-and-value-semantics': ('freshness', observers.freshness),
    'event-and-presentation-time': ('timing', observers.timing),
    'custody-and-wrong-binding': ('custody', observers.custody),
    'transport-parity-and-retry': ('transport', observers.transport),
    'oracle-independence-and-coverage': ('independence', observers.independence),
    'update-and-remove': ('lifecycle', observers.lifecycle),
}


def _check(condition, detail):
    if not condition:
        raise ValueError(detail)


def _read(path, pin=None):
    with path.open('rb') as stream:
        raw = stream.read(MAX_LINE_BYTES + 1)
    if pin is not None:
        _check(sha256(raw).hexdigest() == pin, f'fixture or schema bytes changed at {path.name}')
    return loads(raw)


def _tokens(pointer):
    _check(re.fullmatch(r'(?:/(?:[^~/]|~[01])*)+', pointer) is not None, 'invalid JSON Pointer')
    return [token.replace('~1', '/').replace('~0', '~') for token in pointer.split('/')[1:]]


def _key(container, token):
    if type(container) is list:
        _check(re.fullmatch(r'0|[1-9][0-9]*', token) is not None, 'invalid JSON array index')
        return int(token)
    _check(type(container) is dict, 'JSON Pointer traverses a scalar')
    return token


def _at(value, tokens):
    for token in tokens:
        value = value[_key(value, token)]
    return value


def _negative_fixture(nominal, mutation):
    tokens = _tokens(mutation['path'])
    _check(tokens[0] == 'fixture' and len(tokens) > 1, 'mutation must be strictly below the fixture')
    work = {'fixture': deepcopy(nominal)}
    parent = _at(work, tokens[:-1])
    key = _key(parent, tokens[-1])
    _check(jcs_bytes(parent[key]) == jcs_bytes(mutation['from']), 'mutation from value differs')
    _check(jcs_bytes(mutation['from']) != jcs_bytes(mutation['to']), 'mutation must change a value')
    parent[key] = deepcopy(mutation['to'])
    return work['fixture']


class OfflineKit:
    def __init__(self, package=PACKAGE):
        self.manifest = _read(package / 'rust-anything.slot.json')
        self.registry = {}
        self.policy = {'perception_ceiling': 'none'}
        self.inventory = {'rust-anything-discovery': {'manifest': self.manifest, 'executable_path': None}}
        self.cases = {}
        self.host_cases = []
        self._load_schemas()
        self._load_cases(package)
        self.bundle = self.compile()
        verify_publication(self.bundle, self.manifest, policy=self.policy, registry=self.registry,
                           host_cases=self.host_cases)
        _check(not self.bundle['unresolved_conformance_refs'], 'unresolved offline fixture reference')
        routes = (row for row in self.bundle['router_rows'] if row['id'].startswith('ext.rust-anything'))
        for row in routes:
            route = row['id']
            _check(row['availability'] == 'not_installed' and route in self.bundle['cli_shapes']
                   and route in self.bundle['json_contracts'], 'external route mispublished')

    def _load_schemas(self):
        vp_direct = COMPONENT.parent / 'vp-direct' / 'schemas'
        for path in (CONTRACTS / 'manifest/rea.slot_manifest_v2.schema.json',
                     BANK / 'rea.conformance_case_v1.schema.json',
                     CONTRACTS / 'evidence/rea.slot_evidence_profile_v1.schema.json',
                     vp_direct / 'vp.control.req_v1.schema.json', vp_direct / 'vp.control.reply_v1.schema.json'):
            schema = _read(path)
            self.registry[schema['$id']] = schema
        ext = CONTRACTS / 'ext'
        pins = _read(ext / 'PINS.json')['files']
        for name in ('rea.provider_slot_manifest_v1.schema.json', 'rea.slot_request_v1.schema.json',
                     'rea.slot_result_v1.schema.json'):
            schema = _read(ext / 'generations' / GENERATION / name, pins[name])
            self.registry[schema['$id']] = schema
        schema = self.registry['https://viewpoint.local/contracts/rea/rea.slot_manifest_v2.schema.json']
        require(self.manifest, standalone(schema, self.registry), 'Slot manifest')
        manifest = self.manifest
        provider = manifest['provider_ref']
        upstream = manifest['upstream']
        _check(manifest['slot_id'] == provider['provider_id'] == upstream['name'] == 'rust-anything'
               and manifest['status'] == 'not_installed' and upstream['user_installed'] is True
               and provider['provider_revision'] == upstream['version'] == '3.3.0'
               and all(pin is None for pin in (provider['provider_sha256'], provider['implementation_sha256'],
                                               manifest['capabilities']['adapter_identity']['adapter_sha256']))
               and manifest['transport']['executable_binding'] == 'user_selected'
               and all(row['execution_profile'] == PROFILE and row['request_digest'] is None
                       and row['result_digest'] is None for row in manifest['contract_refs'])
               and manifest['evidence']['shared_schema_ref'] == 'evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending',
               'the offline manifest must leave installation, exchange and shared evidence pins pending')
        _check(upstream['licence_id'] == 'MIT' and upstream['source_url'] == UPSTREAM,
               'upstream licence or source differs from this package')
        _check(upstream['licence_evidence_refs'] == [f'{SOURCE}/LICENSE#L1-L21', f'{SOURCE}/Cargo.toml#L6'],
               'upstream licence evidence differs from the reviewed source pin')

    def _load_cases(self, package):
        catalog = _read(package / 'fixtures.json')
        index = _read(BANK / 'INDEX.json')
        _check(index['execution_profile'] == catalog['execution_profile'] == PROFILE, 'fixture profile differs')
        self.perception_recording = catalog['perception_recording']
        self.catalog_sha256 = digest(catalog)
        bank_rows = {(row['test_id'], row['case']): row for row in index['cases']}
        expected = {(test_id, polarity) for test_id in CASE_IDS for polarity in ('nominal', 'negative')}
        _check(bank_rows.keys() == expected and len(index['cases']) == len(expected), 'oracle-bank inventory differs')
        schema = self.registry['https://viewpoint.local/contracts/rea/rea.conformance_case_v1.schema.json']
        refs = {'nominal': set(), 'negative': set()}
        for row in catalog['cases']:
            key = (row['test_id'], row['case'])
            _check(key in expected and key not in self.cases, f'missing or duplicate fixture {key}')
            test_id, polarity = key
            relative = Path('cases') / test_id / f'{polarity}.json'
            _check(relative.as_posix() == bank_rows[key]['path'], 'oracle-bank path differs')
            case = _read(BANK / relative, row['sha256'])
            require(case, schema, 'Conformance case')
            ref_prefix = 'fixture' if polarity == 'nominal' else 'negctl'
            case_id = CASE_IDS[test_id][0] + ('_negative' if polarity == 'negative' else '')
            ref = f'{ref_prefix}:host/oracle-bank/{test_id}@1'
            _check(key == (case['test_id'], case['case']) and row['id'] == case_id and row['ref'] == ref,
                   'fixture identity differs')
            self.cases[key] = case
            self.registry[ref] = case
            refs[polarity].add(ref)
            self.host_cases.append({'id': case_id, 'ref': ref, 'digest': digest(case),
                                    'summary': f'{test_id} ({polarity})', 'effects': [],
                                    'keywords': bank_rows[key]['router_keywords'],
                                    'perception': {'required_level': 'none', 'optional_levels': []}})
        conf = self.manifest['conformance']
        _check(self.cases.keys() == expected and set(conf['required_checks']) == set(CASE_IDS)
               and set(conf['fixture_refs']) == refs['nominal']
               and set(conf['negative_control_refs']) == refs['negative'], 'adapter kit inventory is incomplete')

    def compile(self):
        return generate(self.manifest, policy=self.policy, registry=self.registry, host_cases=self.host_cases)

    def run(self, test_id):
        nominal = self.cases[test_id, 'nominal']['fixture']
        observe = CASE_IDS[test_id][1]
        upstream = self.manifest['upstream']
        manifest_sha = self.bundle['manifest_digest']
        profile_ref = 'https://viewpoint.local/contracts/rea/rea.slot_evidence_profile_v1.schema.json'
        evidence_schema = standalone(self.registry[profile_ref], self.registry)
        results = {}
        for polarity in ('nominal', 'negative'):
            case = self.cases[test_id, polarity]
            fixture = deepcopy(nominal) if polarity == 'nominal' else _negative_fixture(nominal, case['mutation'])
            fixture_sha = digest(fixture)
            identity = f'offline-{test_id}-{polarity}-{fixture_sha[:16]}'
            opened = build_attempt_receipt(attempt_id=identity, request_id=identity,
                                           slot_id=self.manifest['slot_id'], tool_version=upstream['version'],
                                           upstream_licence_id=upstream['licence_id'],
                                           source_url=upstream['source_url'], producer_kind='host_level',
                                           producer_id='rust-anything.adapter_gate',
                                           manifest_sha256=manifest_sha,
                                           shared_schema_ref=self.manifest['evidence']['shared_schema_ref'])
            code, observation = observe(fixture, self)
            expected = case['expected']
            outcome = 'pass' if code == 'ORACLE_PASS' else 'fail'
            assertions = {row['pointer']: jcs_bytes(_at(observation, _tokens(row['pointer']))) == jcs_bytes(row['equals'])
                          for row in expected['assertions']}
            receipt = finalize_receipt(opened, None)
            # These reproducible fixture receipts assert ordering, with no wall-clock claim.
            del receipt['attempt']['started_at']
            require(receipt, evidence_schema, 'Offline evidence receipt')
            body = {'fixture_sha256': fixture_sha, 'fixture_catalog_sha256': self.catalog_sha256,
                    'case_sha256': digest(case),
                    'outcome': outcome, 'code': code, 'observation': observation,
                    'expected_code': expected['code'], 'assertions': assertions,
                    'passed': code == expected['code'] and outcome == expected['outcome'] and all(assertions.values()),
                    'receipt': receipt, 'receipt_sha256': receipt_digest(receipt)}
            results[polarity] = {**body, 'record_sha256': digest(body)}
        return results

    def gate(self):
        cases = {key: self.run(key) for key in CASE_IDS}
        passed = all(row['passed'] for pair in cases.values() for row in pair.values())
        return {'slot_id': self.manifest['slot_id'], 'execution_profile': PROFILE,
                'installation_ref': None, 'executable_path': None, 'live_status': LIVE_EXECUTION_PARKED,
                'owner_pending': ['MAIN', 'T01', 'T02', 'T03', 'T04', 'T05', 'T06'],
                'manifest_digest': manifest_digest(self.manifest), 'fixture_catalog_sha256': self.catalog_sha256,
                'projection_sha256': digest(self.bundle), 'passed': passed, 'cases': cases}


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
