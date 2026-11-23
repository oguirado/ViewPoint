"""Run a bounded offline qualification attempt and preserve measured evidence.

Run under the operator's systemd memory scope. Synthetic fixture evidence and
GLB import evidence never qualify unavailable build123d or exact B-Rep APIs.
The command exits 3 while full G10 is CANNOT_SEE; that is an expected refusal.
"""
import argparse
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import time

from asset_factory import core
from tools.cad.contracts import ROOT, Refusal, record, require
from tools.cad.fixtures import analytic_part, write_stl
from tools.cad.gate import evaluate
from tools.cad.process import environment, publish_new_directory, run


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--blender', default='blender')
    parser.add_argument('--raw-glb', action='append', type=Path, default=[])
    args = parser.parse_args(argv)
    start = time.monotonic_ns()
    try:
        require(not args.out.exists() and not args.out.is_symlink(), 5, 'output_exists')
        require(args.out.parent.is_dir(), 2, 'output_parent_missing')
        with tempfile.TemporaryDirectory(prefix='.cad-qualification-', dir=args.out.parent) as temporary:
            base = Path(temporary)/'bundle'
            base.mkdir()
            home = Path(temporary)/'home'
            home.mkdir()
            env = environment(home, ROOT)
            cases = []
            for name, hole in (('box', False), ('through_hole', True)):
                triangles, policy, density = analytic_part(hole=hole)
                stl = base/(name+'.stl')
                write_stl(stl, triangles)
                first_sha = core.sha256_file(stl)
                first = evaluate(stl, policy, density_kg_m3=density, units='mm')
                write_stl(stl, triangles)
                second = evaluate(stl, policy, density_kg_m3=density, units='mm')
                replay = first_sha == core.sha256_file(stl) and core.sha256_json(first) == core.sha256_json(second)
                require(first['rc'] == 0 and replay, 5, 'analytic_or_replay_failed')
                core.atomic_write_json(base/(name+'.policy.json'), policy)
                core.atomic_write_json(base/(name+'.mass.json'), first['measurements'])
                core.atomic_write_json(base/(name+'.gate.json'), first)
                cases.append({'name': name, 'gate': first, 'replay_identical': replay})
            triangles, box_policy, density = analytic_part()
            hole_policy = analytic_part(hole=True)[1]
            controls = []
            for name, tris, scale, policy, rho in (
                ('unit_swap', triangles, 1000, box_policy, density),
                ('missing_hole', triangles, 1, hole_policy, density),
                ('open_solid', triangles[:-1], 1, box_policy, density),
                ('density_swap', triangles, 1, box_policy, density*1000)):
                stl = base/(name+'.stl')
                write_stl(stl, tris, scale=scale)
                negative = evaluate(stl, policy, density_kg_m3=rho, units='mm')
                require(negative['rc'] == 5, 5, 'negative_control_survived')
                core.atomic_write_json(base/(name+'.gate.json'), negative)
                controls.append({'name': name, 'rc': negative['rc'], 'reason_codes': negative['reason_codes']})
            blender = shutil.which(args.blender)
            imports = []
            if blender:
                sources = [base/'box.stl', base/'through_hole.stl', base/'unit_swap.stl']+args.raw_glb
                for index, source in enumerate(sources):
                    source = source.resolve(strict=True)
                    before = core.sha256_file(source)
                    out = base/('import-'+str(index)+'.json')
                    process = run([blender, '--background', '--factory-startup', '--python-exit-code', '2',
                                   '--python', str(ROOT/'tools/cad/blender_import.py'), '--',
                                   '--input', str(source), '--out', str(out)],
                                  cwd=base, timeout_s=60, env=env)
                    core.atomic_write_json(base/('import-'+str(index)+'.process.json'), process)
                    require(process['rc'] == 0 and out.is_file(), 6 if process['timed_out'] else 2, 'blender_import_failed')
                    measured = core.load_json(out, max_bytes=1024*1024)
                    require(core.sha256_file(source) == before == measured['source_sha256'], 4, 'import_source_stale')
                    dimension_check = None
                    if index < 3:
                        policy = cases[1 if index == 1 else 0]['gate']['measurements']['bbox_um']
                        error = max(abs(measured['bbox_um'][j][i]-policy[j][i]) for i in range(3) for j in range(2))
                        dimension_check = 0 if error <= 10 else 5
                        require(dimension_check == (5 if index == 2 else 0), 5,
                                'blender_negative_survived' if index == 2 else 'blender_unit_or_axis_drift')
                        measured['bbox_error_um'] = error
                    imports.append({'input_path': str(source) if index >= 3 else source.name,
                                    'input_unchanged': True, 'measurement': measured,
                                    'dimension_check_rc': dimension_check})
            else:
                imports.append({'rc': 3, 'reason_codes': ['blender_unavailable']})
            source = base/'runtime_probe.py'
            source.write_text('raise RuntimeError("probe must refuse before script execution")\n')
            params = {'units': 'mm', 'linear_deflection_um': 1, 'angular_deflection_mdeg': 1000}
            core.atomic_write_json(base/'probe.params.json', params)
            probe = run([sys.executable, '-B', '-m', 'providers.build123d.worker', '--source', str(source),
                         '--params', str(base/'probe.params.json'), '--out', str(base/'runtime-candidate')],
                        cwd=base, timeout_s=30, env=env)
            core.atomic_write_json(base/'runtime.process.json', probe)
            available = importlib.util.find_spec('build123d') is not None
            unavailable = ['qualified_build123d_runtime', 'step_replay', 'step_roundtrip',
                           'exact_brep_centre', 'exact_brep_inertia', 'global_chord_error',
                           'provider_peak_calibration', 'dependency_rights_review']
            report = record(3, ['runtime_qualification_incomplete'], scope='G10-provider',
                            runtime_importable=available, runtime_probe=probe,
                            analytic_cases=cases, negative_controls=controls,
                            blender_imports=imports, unavailable=unavailable,
                            wall_ms=(time.monotonic_ns()-start)//10**6)
            core.atomic_write_json(base/'qualification.json', report)
            publish_new_directory(base, args.out)
            return core.emit(report)
    except Refusal as exc:
        return core.emit(record(exc.rc, [exc.code], scope='G10-provider'))
    except (OSError, core.ContractError, KeyError, ValueError) as exc:
        return core.emit(record(2, ['qualification_io_or_contract_error'], scope='G10-provider'))


if __name__ == '__main__':
    raise SystemExit(main())
