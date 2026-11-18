"""Frozen, serial evidence-replay bench; no provider or reviewer is launched.

Run from the component root: python3 -I bench/run_bench.py --cases PLAN
  --provider ID [--provider ID ...] --output-dir DIR --timeout-s SECONDS

PLAN uses bench/plan.schema.json. Each evidence path is relative to PLAN's
parent. All nested paths are relative to that evidence file's parent. Gate
receipts use bench/gate.schema.json: a bench-owned normalization boundary,
not a raw vqa receipt reader. Integration must produce these receipts from
verified upstream gates. This bench checks identity, chain, required checks
and verdicts; it does not establish independent visual quality itself.
Failures emit the complete diagnostic matrix on stdout, without publication.
Asset acceptance remains CANNOT_SEE until upstream verifiers are integrated;
replay-only success never replaces output-dir/results.json.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

# Explicit local bootstrap preserves -I isolation from PYTHONPATH and user site.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from asset_factory import core
from bench.frozen_contracts import validate_bundle

MAX_BYTES = 16 * 2**20
MAX_TOTAL_BYTES = 64 * 2**20
MAX_CELLS = 256
GATES = ('input', 'appearance', 'vqa3d')

class Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason
        super().__init__(reason)

def _require(condition, rc, reason):
    if not condition:
        raise Refusal(rc, reason)

def _schema(value, schema):
    """Validate the exact checked-in bench schema subset, even with the shim."""
    if 'oneOf' in schema:
        matches = 0
        for branch in schema['oneOf']:
            try:
                _schema(value, branch)
                matches += 1
            except Refusal:
                pass
        _require(matches == 1, 2, 'schema_mismatch')
        return
    if 'const' in schema:
        _require(type(value) is type(schema['const']) and value == schema['const'], 2, 'schema_mismatch')
    if 'enum' in schema:
        _require(any(type(value) is type(v) and value == v for v in schema['enum']), 2, 'schema_mismatch')
    types = schema.get('type')
    if types:
        types = [types] if isinstance(types, str) else types
        kinds = {'object': isinstance(value, dict), 'array': isinstance(value, list),
                 'string': isinstance(value, str), 'integer': type(value) is int,
                 'boolean': type(value) is bool, 'null': value is None}
        _require(any(kinds[t] for t in types), 2, 'schema_mismatch')
    if isinstance(value, dict):
        _require(all(k in value for k in schema.get('required', [])), 2, 'missing_required')
        properties = schema.get('properties', {})
        additional = schema.get('additionalProperties', True)
        for key, item in value.items():
            _require(isinstance(key, str), 2, 'schema_mismatch')
            if key in properties:
                _schema(item, properties[key])
            elif additional is False:
                raise Refusal(2, 'unknown_property')
            elif isinstance(additional, dict):
                _schema(item, additional)
    elif isinstance(value, list):
        _require(len(value) >= schema.get('minItems', 0), 2, 'schema_mismatch')
        for item in value:
            _schema(item, schema.get('items', {}))
    elif isinstance(value, str) and 'pattern' in schema:
        _require(re.fullmatch(schema['pattern'], value) is not None, 2, 'schema_mismatch')

def validate_record(value, name):
    core.canonical_json(value)  # Reject floats/NaN even in open object fields.
    schema = core.load_json(ROOT / 'bench' / (name + '.schema.json'), max_bytes=MAX_BYTES)
    _schema(value, schema)

def _digest(data):
    return hashlib.sha256(data).hexdigest()

def _json_bytes(data):
    def pairs(items):
        result = {}
        for key, item in items:
            _require(key not in result, 2, 'duplicate_key')
            result[key] = item
        return result
    def number(_):
        raise Refusal(2, 'non_integer_number')
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_float=number, parse_constant=number)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            _require(depth <= 128, 6, 'json_depth_budget')
            if isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal(2, 'malformed_json') from exc

def _integer(value):
    return type(value) is int and value >= 0

def _rc(value, verdict):
    _require(type(value) is int and value in core.RC, 2, 'invalid_stage_rc')
    _require(verdict == core.VERDICT[value], 2, 'rc_verdict_mismatch')

def _path(base, relative):
    _require(isinstance(relative, str) and bool(relative) and '\0' not in relative,
             2, 'path_invalid')
    path = Path(relative)
    _require(not path.is_absolute() and '..' not in path.parts, 2, 'path_escape')
    resolved = (base / path).resolve()
    _require(resolved.is_relative_to(base.resolve()), 2, 'path_escape')
    return resolved

def _read(base, relative, deadline, budget):
    _require(time.monotonic() < deadline, 6, 'deadline_exceeded')
    path = _path(base, relative)
    # Reject FIFOs/devices before opening, to keep file reads bounded.
    _require(path.is_file(), 3, 'missing_receipt')
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    _require(len(data) <= MAX_BYTES, 6, 'file_budget_exceeded')
    budget[0] += len(data)
    _require(budget[0] <= MAX_TOTAL_BYTES, 6, 'byte_budget_exceeded')
    _require(time.monotonic() < deadline, 6, 'deadline_exceeded')
    return data

def _file(files, ref):
    _require(ref['path'] in files, 3, 'missing_receipt')
    data = files[ref['path']]
    if isinstance(data, Refusal):
        raise data
    _require(isinstance(data, bytes), 2, 'evidence_bytes_required')
    _require(_digest(data) == ref['sha256'], 4, 'evidence_stale')
    return data

def _provider(bundle, case):
    manifest, factory, policy, decision = (bundle[k] for k in ('manifest', 'factory', 'policy', 'decision'))
    # Fail closed on consumed shapes even when only the narrow F0 shim exists.
    validate_bundle(bundle, schema=_schema, require=_require, slots=core.SLOTS)
    _require(isinstance(manifest, dict) and isinstance(decision, dict), 2, 'provider_invalid')
    _require(manifest.get('slot') == case['slot'], 2, 'provider_slot_mismatch')
    revision = manifest.get('revision')
    # A blocked provider is refused before its nullable revision is required.
    _require(manifest.get('runnable') is True, 5, 'provider_blocked')
    _require(isinstance(revision, str) and revision.strip() and revision not in ('main','master','latest','HEAD'), 2, 'unpinned_revision')
    _require(decision.get('schema') == 'af.licence-decision/v1', 2, 'decision_invalid')
    _rc(decision.get('rc'), decision.get('verdict'))
    _require(type(decision.get('allowed')) is bool, 2, 'decision_invalid')
    _require(decision['allowed'] == (decision['rc'] == 0), 2, 'decision_invalid')
    for key, item in [('configuration_sha256', factory), ('provider_sha256', manifest), ('policy_sha256', policy)]:
        _require(decision.get(key) == core.sha256_json(item), 4, 'decision_stale')
    _require(decision.get('provider_id') == manifest['id'] and decision.get('operation') == case['slot'], 4, 'decision_stale')
    _require(decision.get('jurisdiction') == factory.get('jurisdiction') and
             decision.get('commercial_use') is factory.get('commercial_use'), 4, 'decision_stale')
    # Decisions are data from U2; this does not infer or expand licence rights.
    return manifest

def _gate(record, name, bindings, required, previous, profile):
    validate_record(record, 'gate')
    _require(record['gate'] == name, 4, 'gate_name_stale')
    _require(record['profile'] == (profile if name == 'vqa3d' else None), 4, 'profile_stale')
    for key, value in bindings.items():
        _require(record[key] == value, 4, key + '_stale')
    _require(record['prev_receipt_sha256'] == previous, 4, 'receipt_chain_stale')
    _rc(record['rc'], record['verdict'])
    checks = record['checks']
    ids = [check['id'] for check in checks]
    _require(len(ids) == len(set(ids)), 2, 'duplicate_check')
    for check in checks:
        _rc(check['rc'], check['verdict'])
    _require(record['rc'] == core.worst_rc(check['rc'] for check in checks), 2, 'gate_summary_mismatch')
    if record['rc'] not in (2, 5):
        _require(set(required).issubset(ids), 3, 'required_check_missing')
    return record['rc']

def _measurement(record, files, row, rec):
    """Validate attributed observation data; never derive a peak from host totals."""
    validate_record(record, 'measurement')
    execution = record.get('execution')
    observation_ref = record.get('observation_ref')
    _require(record['scope'] == 'owned-provider-cgroup' and
             isinstance(execution, dict) and isinstance(observation_ref, dict),
             3, 'measurement_unattributed')
    _schema(execution, {'type':'object', 'additionalProperties':False,
        'required':['id','cgroup_path','provider_id','provider_revision'],
        'properties':{k:{'type':'string'} for k in
                      ('id','cgroup_path','provider_id','provider_revision')}})
    path = Path(execution['cgroup_path'])
    _require(execution['id'].strip() and str(path).startswith('/sys/fs/cgroup/') and
             '..' not in path.parts and
             execution['provider_id'] == row['provider_id'] and
             execution['provider_revision'] == row['provider_revision'], 3, 'measurement_unattributed')
    observation = _json_bytes(_file(files, observation_ref))
    validate_record(observation, 'observation')
    _require(observation['execution'] == execution and
             observation['source'] == 'cgroup-v2-memory.peak', 3, 'measurement_unattributed')
    for key in ('case_sha256','provider_sha256','toolchain_sha256','output_sha256'):
        _require(record[key] == row[key] and observation[key] == row[key], 4, 'measurement_stale')
    start, end = observation['started_monotonic_ns'], observation['finished_monotonic_ns']
    peak = observation['memory_peak_bytes']
    _require(_integer(start) and _integer(end) and end >= start and _integer(peak),
             2, 'measurement_invalid')
    measured = {'wall_ms':(end - start) // 1000000, 'memory_peak_bytes':peak}
    for key, value in measured.items():
        _require(type(record[key]) is type(rec[key]) and record[key] == rec[key] == value,
                 4, 'measurement_stale')
    return execution, observation_ref

def evaluate_case(case, provider, evidence):
    """Pure evaluator. evidence = {record, files:{relative_path:bytes}, gate_policy,
    toolchain_sha256}; provider = {manifest,factory,policy,decision}.
    No bytes, decisions or measurements are inferred from accepted=true.
    """
    row = {'schema':'af.bench-result/v1', 'case_id':'',
           'provider_id':'', 'provider_revision':None,
           'case_sha256':None, 'provider_sha256':None,
           'gate_policy_sha256':None, 'toolchain_sha256':None,
           'input_sha256':None, 'output_sha256':None,
           'gate_receipts':[], 'stage_rc':3, 'rc':3, 'status':'INCOMPLETE',
           'verdict':'CANNOT_SEE', 'reasons':[], 'wall_ms':None, 'memory_peak_bytes':None,
           'replay_rc':3, 'gate_results':[],
           'measurement_provenance':None,
           'absence_reasons':{'wall_ms':'not_observed','memory_peak_bytes':'not_observed'}}
    codes = []
    reasons = []
    try:
        _require(all(isinstance(v, dict) for v in (case, provider, evidence)), 2, 'schema_mismatch')
        _require(isinstance(provider.get('manifest'), dict), 2, 'provider_invalid')
        manifest = provider['manifest']
        validate_record(case, 'case')
        row.update(case_id=case['case_id'],
            provider_id=manifest.get('id') if isinstance(manifest.get('id'), str) else '',
            provider_revision=manifest.get('revision') if isinstance(manifest.get('revision'), str) else None,
            case_sha256=core.sha256_json(case),
            provider_sha256=core.sha256_json(manifest), gate_policy_sha256=case['gate_policy_sha256'],
            toolchain_sha256=evidence.get('toolchain_sha256') if
                isinstance(evidence.get('toolchain_sha256'), str) and
                re.fullmatch('[0-9a-f]{64}', evidence['toolchain_sha256']) else None,
            input_sha256=case['reference']['sha256'])
        policy = evidence['gate_policy']
        _require(case['gate_policy_sha256'] == core.sha256_json(policy), 4, 'gate_policy_stale')
        _require(set(policy['required_checks']) == set(GATES), 2, 'required_gates_invalid')
        for checks in policy['required_checks'].values():
            _require(bool(checks) and len(checks) == len(set(checks)) and all(isinstance(v, str) and v for v in checks), 2, 'required_checks_invalid')
        profile = policy.get('vqa3d_profile')
        _require(isinstance(profile, dict) and isinstance(profile.get('id'), str) and bool(profile['id']) and
                 isinstance(profile.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', profile['sha256']) is not None,
                 3, 'profile_unavailable')
        reference = case['reference']
        _require(reference['redistribution_allowed'] is True, 5, 'fixture_not_allowed')
        _require(bool(reference['provenance'].strip()), 2, 'fixture_provenance_missing')
        _provider(provider, case)
        _require(provider['factory']['slots'][case['slot']]['provider'] == manifest['id'],
                 2, 'configured_provider_mismatch')
        _require(provider['factory']['gates']['vqa3d_profile'] == profile['id'],
                 2, 'configured_profile_mismatch')
        if 'load_error' in evidence:
            raise Refusal(*evidence['load_error'])
        rec = evidence.get('record')
        _require(rec is not None, 3, 'missing_receipt')
        validate_record(rec, 'evidence')
        _require(rec['case_id'] == case['case_id'] and rec['provider_id'] == manifest['id'] and rec['provider_revision'] == manifest['revision'], 4, 'identity_stale')
        bindings = {key:row[key] for key in ('case_sha256','provider_sha256','gate_policy_sha256','toolchain_sha256','input_sha256')}
        for key, value in bindings.items():
            _require(rec[key] == value, 4, key + '_stale')
        files = evidence['files']
        _file(files, reference)
        for output in rec['outputs']:
            _file(files, output)
        _require(len({r['path'] for r in rec['outputs']}) == len(rec['outputs']), 2, 'duplicate_output')
        _require(rec['outputs'][0]['sha256'] == rec['output_sha256'], 4, 'output_stale')
        bindings['output_sha256'] = row['output_sha256'] = rec['output_sha256']
        row['stage_rc'] = rec['stage_rc']
        codes.append(rec['stage_rc'])
        gates = rec['gate_receipts']
        row['gate_receipts'] = gates
        _require(len({r['gate'] for r in gates}) == len(gates), 2, 'duplicate_gate')
        previous = None
        by_name = {r['gate']:r for r in gates}
        for name in GATES:
            ref = by_name.get(name)
            gate_rc, gate_reason = 3, 'missing_receipt'
            try:
                _require(ref is not None, 3, 'missing_receipt')
                gate = _json_bytes(_file(files, ref))
                gate_rc = _gate(gate, name, bindings, policy['required_checks'][name], previous, profile)
                gate_reason = None if gate_rc == 0 else 'required_observation_not_pass'
            except Refusal as exc:
                gate_rc, gate_reason = exc.rc, exc.reason
            except core.ContractError as exc:
                gate_rc, gate_reason = 2, exc.code
            codes.append(gate_rc)
            row['gate_results'].append({'gate':name,'rc':gate_rc,
                'verdict':core.VERDICT[gate_rc], 'reason':gate_reason})
            if gate_reason:
                reasons.append(gate_reason)
            previous = ref['sha256'] if ref is not None else None
        for metric in ('wall_ms', 'memory_peak_bytes'):
            value = rec[metric]
            if value is None:
                _require(isinstance(rec['absence_reasons'].get(metric), str) and bool(rec['absence_reasons'][metric]), 2, 'absence_reason_missing')
                row['absence_reasons'][metric] = rec['absence_reasons'][metric]
                codes.append(3)
            else:
                _require(_integer(value), 2, 'metric_invalid')
                row[metric] = value
                row['absence_reasons'].pop(metric, None)
        ref = rec['measurement_ref']
        if ref is None:
            codes.append(3)
            # Values without attributable observation are absent, never zero.
            for metric in ('wall_ms','memory_peak_bytes'):
                row[metric] = None
                row['absence_reasons'][metric] = 'measurement_unproven'
        else:
            measurement = _json_bytes(_file(files, ref))
            execution, observation_ref = _measurement(measurement, files, row, rec)
            row['measurement_provenance'] = {'path':ref['path'],'sha256':ref['sha256'],
                'scope':measurement['scope'], 'execution':execution, 'observation_ref':observation_ref}
        rc = core.worst_rc(codes)
        if rc == 3:
            # Keep specific stage diagnostics on the row; the matrix still
            # collapses unavailable/stale/budget outcomes through worst_rc.
            rc = 4 if 4 in codes else (6 if 6 in codes else 3)
        row['reasons'] = list(dict.fromkeys(reasons)) if reasons else ([] if rc == 0 else ['required_observation_not_pass'])
    except Refusal as exc:
        precedence = core.worst_rc(codes + [exc.rc])
        rc = precedence if precedence in (2, 5) else exc.rc
        row['reasons'] = list(dict.fromkeys(reasons + [exc.reason]))
    except core.ContractError as exc:
        rc = 2
        row['reasons'] = [exc.code]
    except (KeyError, TypeError, ValueError):
        rc = 2
        row['reasons'] = ['schema_mismatch']
    if row['measurement_provenance'] is None:
        for metric in ('wall_ms', 'memory_peak_bytes'):
            row[metric] = None
            row['absence_reasons'][metric] = 'measurement_unproven'
    row['replay_rc'] = rc
    if rc == 0:
        # No schema-specific upstream verification/appearance observation exists
        # in this lane. Supplied normalized assertions cannot approve an asset.
        rc = 3
        row['reasons'] = ['upstream_verification_unavailable']
    row.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc])
    return row

def evaluate_matrix(cases, providers, evidences, *, gate_policy, toolchain_sha256):
    """Exact Cartesian denominator, stable input order; absent cells are failures."""
    _require(bool(cases) and bool(providers), 3, 'empty_matrix')
    _require(len(cases) * len(providers) <= MAX_CELLS, 6, 'cell_budget_exceeded')
    case_ids = [case['case_id'] for case in cases]
    provider_ids = [p['manifest']['id'] for p in providers]
    _require(len(set(case_ids)) == len(case_ids) and len(set(provider_ids)) == len(provider_ids), 2, 'duplicate_identity')
    expected = {(c,p) for c in case_ids for p in provider_ids}
    _require(set(evidences).issubset(expected), 2, 'unexpected_cell')
    rows = []
    for case in cases:
        for provider in providers:
            evidence = dict(evidences.get((case['case_id'], provider['manifest']['id']), {}))
            evidence.update(gate_policy=gate_policy, toolchain_sha256=toolchain_sha256)
            rows.append(evaluate_case(case, provider, evidence))
    rc = core.worst_rc(row['rc'] for row in rows)
    return {'schema':'af.bench-matrix/v1','mode':'evidence-replay','rc':rc,
            'verdict':core.VERDICT[rc], 'cases':[c['case_id'] for c in cases],
            'providers':provider_ids, 'gate_policy_sha256':core.sha256_json(gate_policy),
            'toolchain_sha256':toolchain_sha256,'source_plan':None,'evidence_records':[],'rows':rows}

def run_bench(cases_path, provider_ids, output_dir, *, timeout_s):
    """Load a frozen plan and replay evidence; no adapter API is assumed."""
    try:
        _require(type(timeout_s) is int and 0 < timeout_s <= 300, 2, 'timeout_invalid')
        deadline = time.monotonic() + timeout_s
        cases_path = Path(cases_path).resolve()
        budget = [0]
        plan_bytes = _read(cases_path.parent, cases_path.name, deadline, budget)
        plan = _json_bytes(plan_bytes)
        validate_record(plan, 'plan')
        _require(bool(provider_ids) and len(set(provider_ids)) == len(provider_ids), 2, 'provider_selection_invalid')
        available = {p['manifest']['id']:p for p in plan['providers']}
        _require(len(available) == len(plan['providers']), 2, 'duplicate_identity')
        _require(all(p in available for p in provider_ids), 2, 'unknown_provider')
        providers = [available[p] for p in provider_ids]
        _require(len(plan['cases']) * len(providers) <= MAX_CELLS, 6, 'cell_budget_exceeded')
        cases = {c['case_id']:c for c in plan['cases']}
        _require(len(cases) == len(plan['cases']), 2, 'duplicate_identity')
        evidences = {}
        seen = set()
        for ref in plan['evidence']:
            key = (ref['case_id'],ref['provider_id'])
            _require(key not in seen and key[0] in cases and key[1] in available, 2, 'unexpected_cell')
            seen.add(key)
            if key[1] not in provider_ids:
                continue
            try:
                data = _read(cases_path.parent, ref['path'], deadline, budget)
                _require(_digest(data) == ref['sha256'], 4, 'receipt_stale')
                rec = _json_bytes(data)
                validate_record(rec, 'evidence')
                base = _path(cases_path.parent, ref['path']).parent
                refs = [cases[key[0]]['reference']] + rec['outputs'] + rec['gate_receipts']
                if rec['measurement_ref'] is not None:
                    refs.append(rec['measurement_ref'])
                files = {}
                for file_ref in refs:
                    if file_ref['path'] not in files:
                        try:
                            files[file_ref['path']] = _read(base, file_ref['path'], deadline, budget)
                        except Refusal as exc:
                            # Retain each file's failure independently. A stale
                            # or absent gate must not conceal a later ERROR/FAIL.
                            files[file_ref['path']] = exc
                measurement_ref = rec['measurement_ref']
                if measurement_ref is not None and isinstance(files.get(measurement_ref['path']), bytes):
                    try:
                        measurement = _json_bytes(files[measurement_ref['path']])
                        validate_record(measurement, 'measurement')
                        observation_ref = measurement.get('observation_ref')
                        if observation_ref is not None:
                            files[observation_ref['path']] = _read(base, observation_ref['path'], deadline, budget)
                    except Refusal as exc:
                        # Evaluation will still inspect the supplied gates.
                        files[measurement_ref['path']] = exc
                evidences[key] = {'record':rec,'files':files}
            except Refusal as exc:
                # Retain the cell and exact refusal in the diagnostic matrix.
                evidences[key] = {'load_error':(exc.rc, exc.reason)}
        report = evaluate_matrix(plan['cases'], providers, evidences,
                                gate_policy=plan['gate_policy'], toolchain_sha256=plan['toolchain_sha256'])
        report['source_plan'] = {'path':os.path.relpath(cases_path, Path(output_dir).resolve()),
                                 'sha256':_digest(plan_bytes)}
        report['evidence_records'] = [r for r in plan['evidence'] if r['provider_id'] in provider_ids]
        _require(time.monotonic() < deadline, 6, 'deadline_exceeded')
        if report['rc'] == 0:
            for row in report['rows']:
                validate_record(row, 'results/result')
            validate_record(report, 'results/matrix')
            target = Path(output_dir) / 'results.json'
            _require(not target.is_symlink(), 2, 'output_symlink')
            core.atomic_write_json(target, report)
        return core.emit(report)
    except Refusal as exc:
        return core.emit({'rc':exc.rc,'status':core.STATUS[exc.rc], 'verdict':core.VERDICT[exc.rc], 'reason_codes':[exc.reason]})
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except (OSError, ValueError, KeyError, TypeError):
        return core.emit({'rc':2,'status':'ERROR','verdict':'ERROR','reason_codes':['schema_or_io_error']})

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--provider', action='append', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--timeout-s', type=int, required=True)
    args = parser.parse_args(argv)
    return run_bench(args.cases, tuple(args.provider), args.output_dir, timeout_s=args.timeout_s)

if __name__ == '__main__':
    raise SystemExit(main())
