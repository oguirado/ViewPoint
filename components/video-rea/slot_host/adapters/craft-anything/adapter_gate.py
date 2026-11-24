# SPDX-License-Identifier: AGPL-3.0-only
"""Run the shared host oracle bank against the offline Craft Anything slot."""

import argparse
from copy import deepcopy
from pathlib import Path
import sys

from slot_host.adapters.rea import adapter_gate as prior
from slot_host.compare import behaviour
from slot_host.projections import verify_publication
from slot_host.projections.schema import digest, jcs_bytes, require, standalone
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.receipts import build_attempt_receipt, finalize_receipt, receipt_digest


# The preceding adapters already load the shared bank and run its host observers.
oracle = prior.oracle
PACKAGE = Path(__file__).resolve().parent
SLOT = 'craft-anything'
PROFILE = 'offline_fixture'
PRODUCER = 'craft-anything.adapter_gate'
UPSTREAM = 'https://github.com/storytold/craft-anything'
SOURCE_PIN = '7073462fcca6088989f9127f3473b95471a758b1'
SOURCE = f'{UPSTREAM}/blob/{SOURCE_PIN}'


def _require(condition, detail):
    if not condition:
        raise ValueError(detail)


def _observe_continuation(fixture):
    """Resolve synthetic pages for one evidence identity before comparing them."""
    evidence_id, reference, pages = (fixture[key] for key in ('evidence_id', 'reference', 'pages'))
    _require(reference and evidence_id == 'ev_' + digest(reference), 'invalid fixture evidence pin')
    offsets, rows = [], []
    offset = 0
    # Each accepted page consumes at least one independently expected occurrence.
    while True:
        page = pages.get(str(offset))
        if page is None or page['evidence_id'] != evidence_id:
            code = 'ORACLE_UNRESOLVED_CONTINUATION' if offsets else 'ORACLE_EMPTY_CACHE'
            return code, {'evidence_id': evidence_id, 'resolved_offsets': offsets,
                          'unresolved_offset': offset, 'verdict': 'abstain'}
        offsets.append(offset)
        _require(page['observations'], 'fixture evidence page is empty')
        rows.extend(page['observations'])
        _require(len(rows) <= len(reference), 'fixture evidence exceeded the expected coverage')
        next_offset = page['next_offset']
        if next_offset is None:
            break
        _require(type(next_offset) is int and next_offset == len(rows) and next_offset < len(reference),
                 'fixture continuation is out of sequence')
        offset = next_offset
    policy = {'id': 'craft-anything-fixture-exact', 'version': '1'}
    verdict = behaviour.compare(reference, rows, policy={**policy, 'sha256': digest(policy)},
                                scope='craft-anything-offline', waivers=[])
    _require(verdict['result'] == 'match' and len(rows) == len(reference),
             'resolved fixture evidence did not cover and match the expected observations')
    return 'ORACLE_PASS', {'evidence_id': evidence_id, 'resolved_offsets': offsets,
                           'observations_sha256': digest(rows), 'verdict': verdict['result']}


class OfflineKit(prior.OfflineKit):
    def __init__(self, package=PACKAGE):
        self.manifest = oracle._read(package / f'{SLOT}.slot.json')
        self.policy = {'perception_ceiling': 'none'}
        self.inventory = {f'{SLOT}-discovery': {'manifest': self.manifest, 'executable_path': None}}
        self.registry = self._schema_registry()
        self._verify_declaration()
        self.cases = {}
        self.host_cases = []
        catalog = oracle._read(package / 'fixtures.json')
        self.catalog_sha256 = digest(catalog)
        self.perception_recording = catalog['perception_recording']
        self.retained_replay = catalog['retained_replay']
        self.continuation = catalog['continuation']
        self._load_cases(catalog)
        self.bundle = self.compile()
        verify_publication(self.bundle, self.manifest, policy=self.policy, registry=self.registry,
                           host_cases=self.host_cases)
        _require(not self.bundle['unresolved_conformance_refs'], 'unresolved offline conformance references')
        _require(all(state == 'not_installed' for route, state in self.bundle['route_availability'].items()
                     if route == f'ext.{SLOT}' or route.startswith(f'ext.{SLOT}.')),
                 'the fixture kit published an available external route')
        self.evidence_schema = standalone(self.registry[prior.EVIDENCE_SCHEMA], self.registry)

    def _verify_declaration(self):
        manifest = self.manifest
        require(manifest, standalone(self.registry[prior.MANIFEST_SCHEMA], self.registry), 'Slot manifest')
        upstream = manifest['upstream']
        _require(upstream == {
            'name': SLOT, 'source_url': UPSTREAM, 'version': '0.4.0',
            'licence_id': 'MIT OR Apache-2.0', 'user_installed': True,
            'licence_evidence_refs': [f'{SOURCE}/Cargo.toml#L6-L10', f'{SOURCE}/LICENSE-MIT',
                                      f'{SOURCE}/LICENSE-APACHE', f'{SOURCE}/NOTICE#L1-L8'],
            'component_notices': [f'{SOURCE}/NOTICE', f'{SOURCE}/ATTRIBUTION.md'],
        }, 'Craft Anything release or licence evidence differs')
        transport = manifest['transport']
        _require(manifest['slot_id'] == SLOT and manifest['status'] == 'not_installed'
                 and manifest['provider_ref'] == {
                     'schema': 'rea.provider/v1', 'provider_id': SLOT, 'provider_revision': 'unpinned',
                     'provider_sha256': None, 'implementation_sha256': None,
                 }
                 and manifest['capabilities']['adapter_identity']['adapter_sha256'] is None
                 and (transport['kind'], transport['framing'], transport['argv_prefix'])
                 == ('cli_argv', 'none', [])
                 and transport['executable_binding'] == 'user_selected'
                 and transport['alternates'] == [{'kind': 'mcp_stdio', 'framing': 'jsonrpc_stdio',
                                                   'argv_prefix': ['mcp']}]
                 and manifest['perception']['required_level'] == 'none'
                 and not manifest['perception']['optional_levels']
                 and all(row['execution_profile'] == PROFILE and row['request_digest'] is None
                         and row['result_digest'] is None for row in manifest['contract_refs'])
                 and manifest['evidence']['shared_schema_ref'] == 'evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending',
                 'external installation or admission pins must remain pending')

    def _record(self, case_id, polarity, fixture, case, observe):
        fixture_sha = digest(fixture)
        identity = f'offline-{SLOT}-{case_id}-{polarity}-{fixture_sha[:16]}'
        upstream = self.manifest['upstream']
        opened = build_attempt_receipt(
            attempt_id=identity, request_id=identity, slot_id=SLOT, tool_version=upstream['version'],
            upstream_licence_id=upstream['licence_id'], source_url=upstream['source_url'],
            slot_role='replay', producer_kind='host_level', producer_id=PRODUCER,
            manifest_sha256=self.bundle['manifest_digest'],
            shared_schema_ref=self.manifest['evidence']['shared_schema_ref'],
        )
        code, observation = observe(fixture)
        expected = case['expected']
        outcome = 'pass' if code == 'ORACLE_PASS' else 'fail'
        assertions = {
            row['pointer']: jcs_bytes(oracle._at(observation, oracle._tokens(row['pointer'])))
            == jcs_bytes(row['equals'])
            for row in expected['assertions']
        }
        receipt = finalize_receipt(opened, None)
        # Fixture reports preserve ordering and carry no wall-clock claim.
        del receipt['attempt']['started_at']
        require(receipt, self.evidence_schema, 'Offline evidence receipt')
        row = {'fixture_sha256': fixture_sha, 'fixture_catalog_sha256': self.catalog_sha256,
               'case_sha256': digest(case), 'outcome': outcome, 'code': code,
               'observation': observation, 'expected_code': expected['code'], 'assertions': assertions,
               'passed': code == expected['code'] and outcome == expected['outcome'] and all(assertions.values()),
               'receipt': receipt, 'receipt_sha256': receipt_digest(receipt)}
        return {**row, 'record_sha256': digest(row)}

    def run(self, test_id):
        nominal = self.cases[test_id, 'nominal']['fixture']
        observer = oracle.CASE_IDS[test_id][1]

        def observe(fixture):
            code, observation = observer(fixture, self)
            if test_id == 'outside-record-replay-compare':
                observation['fixture_replay_lineage'] = self._verify_retained_replay(fixture)
            return code, observation

        results = {}
        for polarity in ('nominal', 'negative'):
            case = self.cases[test_id, polarity]
            fixture = deepcopy(nominal) if polarity == 'nominal' else oracle._negative_fixture(nominal, case['mutation'])
            results[polarity] = self._record(test_id, polarity, fixture, case, observe)
        return results

    def run_continuation(self):
        original = self.continuation
        pages = original['pages']
        without_next_page = dict(pages)
        del without_next_page[str(pages['0']['next_offset'])]
        fixtures = {
            'nominal': (pages, 'ORACLE_PASS', [0, 1], 'match'),
            'empty_cache': ({}, 'ORACLE_EMPTY_CACHE', [], 'abstain'),
            'unresolved_continuation': (without_next_page, 'ORACLE_UNRESOLVED_CONTINUATION',
                                        [0], 'abstain'),
        }
        results = {}
        for polarity, (pages, code, offsets, verdict) in fixtures.items():
            fixture = {**original, 'pages': pages}
            case = {'id': polarity, 'expected': {
                'code': code, 'outcome': 'pass' if polarity == 'nominal' else 'fail', 'assertions': [
                    {'pointer': '/resolved_offsets', 'equals': offsets},
                    {'pointer': '/verdict', 'equals': verdict},
                ],
            }}
            results[polarity] = self._record('continuation', polarity, fixture, case, _observe_continuation)
        return results

    def gate(self):
        cases = {test_id: self.run(test_id) for test_id in oracle.CASE_IDS}
        continuation = self.run_continuation()
        passed = all(row['passed'] for pair in (*cases.values(), continuation) for row in pair.values())
        return {'slot_id': SLOT, 'execution_profile': PROFILE, 'installation_ref': None, 'executable_path': None,
                'live_status': LIVE_EXECUTION_PARKED, 'owner_pending': ['MAIN', 'T01', 'T02', 'T03', 'T04', 'T05', 'T06'],
                'manifest_digest': self.bundle['manifest_digest'], 'fixture_catalog_sha256': self.catalog_sha256,
                'projection_sha256': digest(self.bundle), 'cases': cases, 'continuation': continuation,
                'passed': passed}


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
