"""Native B-Rep mass and independent analytic checks, without mesh substitution.

Only called inside the approved CAD interpreter. build123d's installed
Shape.matrix_of_inertia uses OCCT VolumeProperties and MatrixOfInertia at
the centre of mass; Solid.center uses CentreOfMass. Calculations are floating
point, quantized once to integer SI units, never claimed mathematically exact.
"""
import math
from pathlib import Path

from asset_factory import core
from tools.cad.contracts import integer, policy_validate, require
from tools.cad.mesh import physical_validate


def properties(part, *, density_kg_m3):
    from build123d import CenterOf

    require(integer(density_kg_m3, 1), 2, 'density_invalid')
    require(part is not None, 3, 'solid_unavailable')
    require(part.is_valid, 5, 'brep_invalid')
    require(part.is_manifold, 5, 'brep_not_closed')
    solids = part.solids()
    require(len(solids) == 1, 5, 'single_solid_required')
    solid = solids[0]
    # A compound containing a solid plus loose faces must not lose those faces.
    require(len(part.faces()) == len(solid.faces()), 5, 'loose_faces_unsupported')
    volume = solid.volume
    centre = solid.center(CenterOf.MASS)
    tensor = solid.matrix_of_inertia
    box = solid.bounding_box()
    values = [volume, *centre, *box.min, *box.max,
              *(v for row in tensor for v in row)]
    require(all(math.isfinite(v) for v in values), 2, 'brep_nonfinite')
    require(volume > 0, 5, 'nonpositive_brep_volume')
    # mm^3 -> um^3: 1e9; rho * mm^3 -> ng: 1e3.
    # Unit-density OCCT inertia is mm^5. rho * mm^5 -> kg m^2 * 1e18: 1e3.
    mass = {'schema': 'af.cad-brep-mass/v1', 'units': 'SI',
            'method': 'occt_brep_volume_properties',
            'calculation': 'floating_point_brep_not_mesh',
            'inertia_frame': 'centre_of_mass_world_axes',
            'density_kg_m3': density_kg_m3, 'volume_um3': round(volume*10**9),
            'mass_ng': round(volume*density_kg_m3*1000),
            'centre_um': [round(v*1000) for v in centre],
            'inertia_kg_m2_e18': [[round(v*density_kg_m3*1000) for v in row]
                                 for row in tensor],
            'bbox_um': [[round(v*1000) for v in box.min], [round(v*1000) for v in box.max]],
            'solids': 1, 'faces': len(solid.faces()), 'valid': True, 'manifold': True}
    physical_validate(mass)
    core.validate(mass, core.load_json(Path(__file__).with_name('native.schema.json'),
                                     max_bytes=1024*1024))
    return mass


def compare(mass, policy):
    """Compare measured B-Rep to an independently supplied nominal oracle.

    The genus oracle is evaluated separately on STL, not inferred from moments.
    PASS applies only to declared analytic dimensions and physical properties.
    """
    policy_validate(policy)
    core.validate(mass, core.load_json(Path(__file__).with_name('native.schema.json'),
                                     max_bytes=1024*1024))
    require(len(mass['centre_um']) == 3 and len(mass['bbox_um']) == 2
            and all(len(row) == 3 for row in mass['bbox_um'])
            and len(mass['inertia_kg_m2_e18']) == 3
            and all(len(row) == 3 for row in mass['inertia_kg_m2_e18']), 2, 'mass_shape_invalid')
    require(integer(mass['density_kg_m3'], 1), 2, 'density_invalid')
    physical_validate(mass)
    errors = {
        'bbox_error_um': max(abs(mass['bbox_um'][j][i]-policy['expected_bbox_um'][j][i])
                             for j in range(2) for i in range(3)),
        'centre_error_um': max(abs(mass['centre_um'][i]-policy['expected_centre_um'][i]) for i in range(3)),
    }
    for key, expected, metric in (
        ('volume_um3', 'expected_volume_um3', 'volume_error_ppm'),
        ('inertia_kg_m2_e18', 'expected_inertia_kg_m2_e18', 'inertia_error_ppm')):
        if key == 'volume_um3':
            scale = policy[expected]
            delta = abs(mass[key]-scale)
        else:
            scale = max(abs(v) for row in policy[expected] for v in row)
            delta = max(abs(mass[key][i][j]-policy[expected][i][j]) for i in range(3) for j in range(3))
        require(scale > 0, 2, 'policy_inertia_invalid')
        errors[metric] = (delta*10**6+scale-1)//scale
    failures = [reason for metric, ceiling, reason in (
        ('bbox_error_um', 'dimension_tolerance_um', 'dimensions_mismatch'),
        ('centre_error_um', 'dimension_tolerance_um', 'centre_mismatch'),
        ('volume_error_ppm', 'volume_tolerance_ppm', 'volume_mismatch'),
        ('inertia_error_ppm', 'moment_tolerance_ppm', 'inertia_mismatch'),
    ) if errors[metric] > policy[ceiling]]
    # A changed density/mass witness cannot be made consistent by its schema.
    from fractions import Fraction
    expected_ng = round(Fraction(mass['volume_um3']*mass['density_kg_m3'], 10**6))
    if abs(mass['mass_ng']-expected_ng) > 1:
        failures.append('mass_density_mismatch')
    rc = 5 if failures else 0
    return {'schema': 'af.cad-native-gate/v1', 'scope': 'G10-analytic-properties',
            'rc': rc, 'status': core.STATUS[rc], 'verdict': core.VERDICT[rc],
            'reason_codes': failures, 'policy_sha256': core.sha256_json(policy),
            'mass_sha256': core.sha256_json(mass), 'metrics': errors}
