"""G10's measured STL/analytic subset. This is not whole-provider readiness."""
from pathlib import Path

from asset_factory import core
from tools.cad.contracts import Refusal, policy_validate, record
from tools.cad.mesh import measure


def evaluate(stl_path, policy, *, density_kg_m3, units):
    """Run independent nominal checks; missing B-Rep proofs stay explicit.

    A local mesh PASS certifies only this subset. The adapter cannot promote it
    to a full G10 PASS while exact B-Rep mass and chord checks are unavailable.
    """
    try:
        policy_validate(policy)
        before = core.sha256_file(Path(stl_path))
        mass = measure(stl_path, density_kg_m3=density_kg_m3, units=units)
        if core.sha256_file(Path(stl_path)) != before:
            return record(4, ['mesh_stale'], scope='G10-mesh-analytic', measurements=None)
        failed = []
        bbox_error = max(abs(mass['bbox_um'][j][i]-policy['expected_bbox_um'][j][i])
                         for j in range(2) for i in range(3))
        centre_error = max(abs(mass['centre_um'][i]-policy['expected_centre_um'][i]) for i in range(3))
        volume_delta = abs(mass['volume_um3']-policy['expected_volume_um3'])
        expected_volume = policy['expected_volume_um3']
        volume_error = (volume_delta*10**6 + expected_volume-1)//expected_volume
        inertia_delta = max(abs(mass['inertia_kg_m2_e18'][i][j]
                                -policy['expected_inertia_kg_m2_e18'][i][j])
                            for i in range(3) for j in range(3))
        inertia_scale = max(abs(v) for row in policy['expected_inertia_kg_m2_e18'] for v in row)
        if inertia_scale <= 0:
            raise Refusal(2, 'policy_inertia_invalid')
        inertia_error = (inertia_delta*10**6 + inertia_scale-1)//inertia_scale
        if bbox_error > policy['dimension_tolerance_um']:
            failed.append('dimensions_mismatch')
        if centre_error > policy['dimension_tolerance_um']:
            failed.append('centre_mismatch')
        if volume_error > policy['volume_tolerance_ppm']:
            failed.append('volume_mismatch')
        if mass['topology']['genus'] != policy['expected_genus']:
            failed.append('hole_topology_mismatch')
        if inertia_error > policy['moment_tolerance_ppm']:
            failed.append('inertia_mismatch')
        return record(5 if failed else 0, failed, scope='G10-mesh-analytic',
                      subject_sha256=before, policy_sha256=core.sha256_json(policy),
                      measurements=mass, metrics={'bbox_error_um': bbox_error,
                         'centre_error_um': centre_error, 'volume_error_ppm': volume_error,
                         'inertia_error_ppm': inertia_error},
                      unavailable=['exact_brep_centre', 'exact_brep_inertia', 'global_chord_error'])
    except Refusal as exc:
        return record(exc.rc, [exc.code], scope='G10-mesh-analytic', measurements=None)
    except core.ContractError as exc:
        return record(2, [exc.code], scope='G10-mesh-analytic', measurements=None)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return record(2, ['mesh_or_policy_invalid'], scope='G10-mesh-analytic', measurements=None)
