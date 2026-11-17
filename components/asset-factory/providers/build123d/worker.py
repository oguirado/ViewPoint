"""Export a reviewed script's ``part`` using the evidenced build123d API.

API evidence: upstream exporters3d.py export_step/export_stl. Timestamp is an
explicit export argument; STEP geometry is never modified by text scrubbing.
Native B-Rep properties and STEP read-back use the installed qualified API.
Worker OK means a complete export; independent analytic approval needs --policy.
"""
import argparse
from importlib import metadata
import math
from pathlib import Path
import resource
import runpy
import tempfile

from asset_factory import core
from tools.cad.contracts import Refusal, require
from tools.cad.process import publish_new_directory
from tools.cad.brep import properties, compare


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--params', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--policy', type=Path)
    args = parser.parse_args(argv)
    try:
        try:
            import build123d as cad
        except ImportError:
            raise Refusal(3, 'build123d_unavailable')
        params = core.load_json(args.params, max_bytes=1024*1024)
        require(params['units'] == 'mm', 2, 'units_unsupported')
        source_sha = core.sha256_file(args.source)
        params_sha = core.sha256_json(params)
        # Script params are part of the request identity; no ambient defaults.
        namespace = runpy.run_path(str(args.source), init_globals={'CAD_PARAMS': params})
        part = namespace.get('part')
        require(part is not None, 3, 'solid_unavailable')
        volume = part.volume  # Shape.volume property evidenced in upstream README.
        require(type(volume) in (int, float) and math.isfinite(volume) and volume > 0,
                5, 'nonpositive_brep_volume')
        with tempfile.TemporaryDirectory(prefix='.cad-', dir=args.out.parent) as temporary:
            staged = Path(temporary)/'bundle'
            staged.mkdir()
            # [TARGET] fixed metadata epoch, not a claim about build time.
            require(cad.export_step(part, staged/'part.step', timestamp='2000-01-01T00:00:00'),
                    2, 'step_export_failed')
            require(cad.export_stl(part, staged/'part.stl',
                                  tolerance=params['linear_deflection_um']/1000,
                                  angular_tolerance=params['angular_deflection_mdeg']*math.pi/180000,
                                  ascii_format=False), 2, 'stl_export_failed')
            mass = properties(part, density_kg_m3=params['density_kg_m3'])
            step_sha = core.sha256_file(staged/'part.step')
            roundtrip = properties(cad.import_step(staged/'part.step'),
                                   density_kg_m3=params['density_kg_m3'])
            # A semantic round-trip is checked separately from byte replay.
            require(mass == roundtrip, 5, 'step_roundtrip_properties_mismatch')
            core.atomic_write_json(staged/'mass.json', mass)
            core.atomic_write_json(staged/'roundtrip.mass.json', roundtrip)
            if args.policy:
                from tools.cad.gate import evaluate
                policy = core.load_json(args.policy, max_bytes=1024*1024)
                native_gate = compare(mass, policy)
                mesh_gate = evaluate(staged/'part.stl', policy,
                                     density_kg_m3=params['density_kg_m3'], units='mm')
                require(native_gate['rc'] == 0, native_gate['rc'],
                        'analytic_brep_refused:'+','.join(native_gate['reason_codes']))
                require(mesh_gate['rc'] == 0, mesh_gate['rc'],
                        'analytic_mesh_refused:'+','.join(mesh_gate['reason_codes']))
                core.atomic_write_json(staged/'native.gate.json', native_gate)
                core.atomic_write_json(staged/'mesh.gate.json', mesh_gate)
            require(core.sha256_file(args.source) == source_sha
                    and core.sha256_json(core.load_json(args.params, max_bytes=1024*1024)) == params_sha,
                    4, 'export_input_stale')
            witness = {'schema': 'af.cad-export/v1', 'rc': 0, 'status': 'OK',
                       'build123d_version': metadata.version('build123d'),
                       'brep_volume_um3': round(volume*10**9),
                       'source_sha256': source_sha,
                       'params_sha256': params_sha,
                       'step_sha256': step_sha,
                       'stl_sha256': core.sha256_file(staged/'part.stl'),
                       'mass_sha256': core.sha256_file(staged/'mass.json'),
                       'roundtrip_mass_sha256': core.sha256_file(staged/'roundtrip.mass.json'),
                       'step_roundtrip_identical_properties': True,
                       'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                       'absence_reasons': {'global_chord_error': 'not_proven_for_arbitrary_curved_parts'}}
            core.atomic_write_json(staged/'export.json', witness)
            publish_new_directory(staged, args.out)
        return core.emit(witness)
    except Refusal as exc:
        return core.emit({'rc': exc.rc, 'status': core.STATUS[exc.rc], 'reason_codes': [exc.code],
                          'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024})
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except (Exception, SystemExit):
        return core.emit({'rc': 2, 'status': 'ERROR', 'reason_codes': ['cad_script_or_api_error'],
                          'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024})


if __name__ == '__main__':
    raise SystemExit(main())
