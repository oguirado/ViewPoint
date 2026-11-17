"""Local CAD admission contracts; no external runtime is imported here.

slot.schema.json and provider.schema.json are byte-identical snapshots of the
research drafts. Prefer the registered slot schema once W3-CONTRACTS lands.
All limits below are DESIGN_CHOICE [TARGET], not calibrated CAD tolerances.
"""
from pathlib import Path
import re

from asset_factory import contracts as slot_contracts, core

MAX_BYTES = 32 * 2**20  # [TARGET] bounded binary STL and STEP input
MAX_TRIANGLES = 200000  # [TARGET] bounded topology/moment work
MAX_TIMEOUT_S = 300  # [TARGET] includes export, replay and import
ROOT = Path(__file__).resolve().parents[2]
PIN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class Refusal(Exception):
    def __init__(self, rc, code):
        self.rc, self.code = rc, code
        super().__init__(code)


def require(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def policy_validate(policy):
    """An independent nominal specification; never derived from the candidate.

    Bounding box and genus are limited oracles. They do not certify arbitrary
    local dimensions or a general B-Rep-to-mesh chord-error bound.
    """
    require(type(policy) is dict and set(policy) == {
        'schema', 'expected_bbox_um', 'expected_volume_um3', 'expected_genus',
        'dimension_tolerance_um', 'volume_tolerance_ppm', 'moment_tolerance_ppm',
        'expected_centre_um', 'expected_inertia_kg_m2_e18',
    }, 2, 'policy_invalid')
    require(policy['schema'] == 'af.cad-policy/v1', 2, 'policy_invalid')
    box = policy['expected_bbox_um']
    require(type(box) is list and len(box) == 2
            and all(type(row) is list and len(row) == 3
                    and all(type(v) is int for v in row) for row in box), 2, 'policy_invalid')
    require(all(box[1][i] > box[0][i] for i in range(3)), 2, 'policy_invalid')
    for name in ('expected_volume_um3', 'dimension_tolerance_um',
                 'volume_tolerance_ppm', 'moment_tolerance_ppm'):
        require(integer(policy[name], 1), 2, 'policy_invalid')
    require(policy['dimension_tolerance_um'] <= 10, 2, 'tolerance_exceeds_g10')
    require(policy['volume_tolerance_ppm'] <= 1000
            and policy['moment_tolerance_ppm'] <= 1000, 2, 'tolerance_exceeds_g10')
    require(integer(policy['expected_genus']), 2, 'policy_invalid')
    centre, inertia = policy['expected_centre_um'], policy['expected_inertia_kg_m2_e18']
    require(type(centre) is list and len(centre) == 3
            and all(type(v) is int for v in centre), 2, 'policy_invalid')
    require(type(inertia) is list and len(inertia) == 3 and all(
        type(row) is list and len(row) == 3 and all(type(v) is int for v in row)
        for row in inertia), 2, 'policy_invalid')
    core.canonical_json(policy)


def request_validate(request):
    registered = ROOT / 'slots' / 'cad_code.schema.json'
    schema_path = registered if registered.is_file() else Path(__file__).with_name('slot.schema.json')
    core.validate(request, core.load_json(schema_path, max_bytes=1024*1024))
    require(request['kind'] == 'request' and request['provider_id'] == 'build123d', 2, 'request_invalid')
    require(type(request['request_id']) is str and bool(request['request_id']), 2, 'request_invalid')
    require(PIN.fullmatch(request['provider_revision']) is not None, 2, 'unpinned_revision')
    require(len(request['inputs']) == 1, 2, 'input_exclusivity')
    require(DIGEST.fullmatch(request['inputs'][0]['sha256']) is not None, 2, 'digest_invalid')
    context = request['context']
    require(integer(context['timeout_s'], 1) and context['timeout_s'] <= MAX_TIMEOUT_S, 2, 'timeout_invalid')
    require(all(DIGEST.fullmatch(context[k]) is not None
                for k in ('policy_sha256', 'toolchain_sha256')), 2, 'digest_invalid')
    params = request['params']
    require(set(params) == {'units', 'density_kg_m3', 'linear_deflection_um',
                           'angular_deflection_mdeg', 'g10_policy_sha256'}, 2, 'params_invalid')
    require(params['units'] == 'mm', 2, 'units_unsupported')
    require(all(integer(params[k], 1) for k in ('density_kg_m3', 'linear_deflection_um',
                                              'angular_deflection_mdeg')), 2, 'params_invalid')
    require(params['linear_deflection_um'] <= 10
            and params['angular_deflection_mdeg'] <= 90000, 2, 'params_invalid')
    require(type(params['g10_policy_sha256']) is str
            and DIGEST.fullmatch(params['g10_policy_sha256']) is not None, 2, 'digest_invalid')
    if 'cad_code' in slot_contracts.SLOTS:
        try:
            slot_contracts.validate_slot(request, 'cad_code', 'request')
        except core.ContractError as exc:
            raise Refusal(exc.rc, exc.code) from exc
    return {'slot_schema_sha256': core.sha256_file(schema_path),
            'slot_registration': 'REGISTERED' if registered.is_file() else 'DRAFT'}


def record(rc, reasons, **data):
    return {'schema': 'af.cad-gate/v1', 'rc': rc, 'status': core.STATUS[rc],
            'verdict': core.VERDICT[rc], 'reason_codes': list(reasons), **data}
