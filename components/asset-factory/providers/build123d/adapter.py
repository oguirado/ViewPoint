"""Fail-closed CAD slot boundary. The shipped provider is not runnable.

Reviewed precise-part scripts define `part` and consume `CAD_PARAMS` (millimetres).
The native worker has measured STEP/STL export, read-back and B-Rep mass APIs.
General chord-error, rights and scheduler qualification are incomplete, so this
adapter refuses production publication even when analytic fixture gates pass.

No source is run on a refused call. No caller output is changed on refusal.
The callable returns (af.cad-code/v1 result, af.cad-gate/v1 diagnosis).
"""
import argparse
from pathlib import Path
import time

from asset_factory import contracts as slot_contracts, core
from tools.cad.contracts import ROOT, Refusal, record, request_validate, require, policy_validate


def evaluate(request, *, provider, factory, licence_policy, decision, g10_policy,
             source_root, output_dir):
    start = time.monotonic_ns()
    binding = {}
    try:
        binding = request_validate(request)
        policy_validate(g10_policy)
        core.validate(provider, core.load_json(Path(__file__).parents[2]/'tools/cad/provider.schema.json',
                                              max_bytes=1024*1024))
        slot_contracts.validate_provider(provider)
        require(provider['id'] == 'build123d' and provider['slot'] == 'cad_code'
                and provider['kind'] == 'tool', 2, 'provider_invalid')
        require(provider['revision'] == request['provider_revision'], 4, 'provider_stale')
        require(core.sha256_json(g10_policy) == request['params']['g10_policy_sha256'], 4, 'g10_policy_stale')
        require(type(factory.get('commercial_use')) is bool
                and type(request['context']['commercial_use']) is bool, 2, 'commercial_use_required')
        require(factory['commercial_use'] == request['context']['commercial_use']
                and factory.get('jurisdiction') == request['context']['jurisdiction'], 4, 'context_stale')
        require(core.sha256_json(licence_policy) == request['context']['policy_sha256'], 4, 'policy_stale')
        require(decision.get('schema') == 'af.licence-decision/v1'
                and type(decision.get('allowed')) is bool
                and type(decision.get('rc')) is int, 2, 'decision_invalid')
        require(decision.get('provider_sha256') == core.sha256_json(provider)
                and decision.get('configuration_sha256') == core.sha256_json(factory)
                and decision.get('policy_sha256') == core.sha256_json(licence_policy)
                and decision.get('provider_id') == 'build123d'
                and decision.get('operation') == 'cad_code'
                and decision.get('jurisdiction') == request['context']['jurisdiction']
                and decision.get('commercial_use') is request['context']['commercial_use'], 4, 'decision_stale')
        source = Path(source_root).resolve(strict=True)/request['inputs'][0]['path']
        source = source.resolve(strict=True)
        require(source.is_relative_to(Path(source_root).resolve(strict=True)) and source.is_file(),
                2, 'source_path_escape')
        require(source.suffix == '.py', 2, 'source_invalid')
        require(source.stat().st_size <= 1024*1024, 6, 'source_budget_exceeded')
        require(core.sha256_file(source) == request['inputs'][0]['sha256'], 4, 'source_stale')
        require(not Path(output_dir).exists() and not Path(output_dir).is_symlink(), 5, 'output_exists')
        if provider['runnable'] is False and provider.get('blocked_reason') == 'research_only_unverified_runtime':
            raise Refusal(3, 'research_only_unverified_runtime')
        require(provider['runnable'] is True and decision['allowed'] is True
                and decision['rc'] == 0 and decision.get('verdict') == 'PASS', 5, 'licence_refused')
        # An analytic fixture receipt does not qualify arbitrary production CAD.
        runtime = provider['runtime']
        require(type(runtime.get('ram_peak_gb_measured')) is int
                and runtime['ram_peak_gb_measured'] > 0
                and runtime.get('ram_peak_measurement_ref') is not None, 3, 'peak_unmeasured')
        raise Refusal(3, 'production_cad_qualification_incomplete')
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
    except core.ContractError as exc:
        rc, reasons = 2, [exc.code]
    except (OSError, ValueError, KeyError, TypeError):
        rc, reasons = 2, ['cad_input_invalid']
    safe_request = request if type(request) is dict else {}
    result = {'schema': 'af.cad-code/v1', 'kind': 'result', 'slot': 'cad_code',
              'request_id': safe_request.get('request_id', ''), 'provider_id': 'build123d',
              'provider_revision': safe_request.get('provider_revision', ''),
              'rc': rc, 'status': core.STATUS[rc], 'outputs': [], 'receipts': [],
              'metrics': {'wall_ms': (time.monotonic_ns()-start)//10**6, 'peak_rss_bytes': None},
              'absence_reasons': {'stage': ','.join(reasons), 'peak_rss_bytes': 'no_runtime_started'}}
    return result, record(rc, reasons, scope='G10-provider', **binding)


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ('request', 'factory', 'licence-policy', 'decision', 'g10-policy'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        values = {name: core.load_json(getattr(args, name), max_bytes=1024*1024)
                  for name in ('request', 'factory', 'licence_policy', 'decision', 'g10_policy')}
        provider = core.load_json(Path(__file__).with_name('provider.json'), max_bytes=1024*1024)
        result, _ = evaluate(**values, provider=provider, source_root=args.source_root, output_dir=args.output_dir)
        return core.emit(result)
    except core.ContractError as exc:
        return core.emit(exc.as_dict())


if __name__ == '__main__':
    raise SystemExit(main())
