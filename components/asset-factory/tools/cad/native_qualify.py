"""Qualify the native CAD path on bounded analytic fixtures, offline.

Run with the approved CAD venv under the mandatory resource scope. PASS is
limited to these analytic parts; it does not unblock the production adapter.
All exports run in isolated processes, then independent Blender imports measure
STL dimensions. Diagnostics are external to the repository, never slot assets.
"""
import argparse
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import shutil
import struct
import sys
import tempfile
import time

from asset_factory import core
from tools.cad.contracts import ROOT, Refusal, require
from tools.cad.fixtures import analytic_part
from tools.cad.gate import evaluate
from tools.cad.process import environment, publish_new_directory, run


def runtime_identity(freeze):
    """Bind the operator's freeze and check installed distribution versions.

    A version freeze is not a wheel hash or a reproducible installation recipe.
    The two claims remain separate in the qualification report.
    """
    pin = core.load_json(ROOT/'providers/build123d/runtime.json', max_bytes=1024*1024)
    core.validate(pin, core.load_json(ROOT/'tools/cad/runtime.schema.json', max_bytes=1024*1024))
    require(core.sha256_file(freeze) == pin['freeze']['sha256'], 4, 'runtime_freeze_stale')
    require(str(Path(sys.executable).absolute()) == pin['python'], 4, 'runtime_interpreter_stale')
    for source in pin['api_sources']:
        require(core.sha256_file(Path(source['path'])) == source['sha256'], 4, 'runtime_api_stale')
    lines = Path(freeze).read_text().splitlines()
    require(bool(lines), 3, 'runtime_freeze_empty')
    packages = {}
    for line in lines:
        name, separator, version = line.partition('==')
        require(bool(separator and name and version), 2, 'runtime_freeze_invalid')
        require(metadata.version(name) == version, 4, 'runtime_distribution_stale')
        packages[name] = version
    return {'python': str(Path(sys.executable).absolute()), 'python_version': platform.python_version(),
            'machine': platform.machine(), 'freeze': {'path': str(freeze), 'sha256': pin['freeze']['sha256']},
            'packages': packages, 'api_sources': pin['api_sources'],
            'runtime_manifest_sha256': core.sha256_file(ROOT/'providers/build123d/runtime.json')}


def fixture_params(*, hole, offset=(0, 0, 0), scale=1, opened=False):
    return {'units': 'mm', 'density_kg_m3': 1000, 'linear_deflection_um': 1,
            'angular_deflection_mdeg': 1000, 'fixture_hole': hole,
            'fixture_scale': scale, 'fixture_open': opened,
            **dict(zip(('offset_x_um', 'offset_y_um', 'offset_z_um'), offset))}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--freeze', type=Path, required=True)
    parser.add_argument('--blender', default='blender')
    args = parser.parse_args(argv)
    started = time.monotonic_ns()
    try:
        require(not args.out.exists() and not args.out.is_symlink(), 5, 'output_exists')
        require(args.out.parent.is_dir(), 2, 'output_parent_missing')
        identity = runtime_identity(args.freeze)
        blender = shutil.which(args.blender)
        require(blender is not None, 3, 'blender_unavailable')
        source = ROOT/'providers/build123d/fixtures/analytic.py'
        source_sha = core.sha256_file(source)
        source_paths = [ROOT/name for name in (
            'providers/build123d/worker.py', 'providers/build123d/fixtures/analytic.py',
            'providers/build123d/runtime.json', 'tools/cad/brep.py', 'tools/cad/native_qualify.py',
            'tools/cad/fixtures.py', 'tools/cad/gate.py', 'tools/cad/mesh.py', 'tools/cad/contracts.py',
            'tools/cad/process.py', 'tools/cad/blender_import.py', 'tools/cad/native.schema.json',
            'tools/cad/runtime.schema.json', 'tools/cad/qualification.schema.json')]
        source_files = [{'path': str(p.relative_to(ROOT)), 'sha256': core.sha256_file(p)} for p in source_paths]
        with tempfile.TemporaryDirectory(prefix='.cad-native-', dir=args.out.parent) as temporary:
            base = Path(temporary)/'bundle'
            base.mkdir()
            home = Path(temporary)/'home'
            home.mkdir()
            env = environment(home, ROOT)
            process_records = []

            def export(label, params, policy):
                parameters = base/(label+'.params.json')
                nominal = base/(label+'.policy.json')
                core.atomic_write_json(parameters, params)
                core.atomic_write_json(nominal, policy)
                target = base/label
                process = run([sys.executable, '-B', '-m', 'providers.build123d.worker',
                               '--source', str(source), '--params', str(parameters),
                               '--policy', str(nominal), '--out', str(target)],
                              cwd=base, timeout_s=60, env=env)
                core.atomic_write_json(base/(label+'.process.json'), process)
                process_records.append(process)
                return target, process

            cases = []
            for name, hole, offset in (('box', False, (0, 0, 0)),
                                       ('through_hole', True, (0, 0, 0)),
                                       ('offset_hole', True, (123000, -456000, 789000))):
                policy = analytic_part(hole=hole, offset_um=offset)[1]
                params = fixture_params(hole=hole, offset=offset)
                first, process = export(name+'-a', params, policy)
                require(process['rc'] == 0, process['rc'], 'native_export_failed:'+process['tail'][-1000:])
                second, process = export(name+'-b', params, policy)
                require(process['rc'] == 0, process['rc'], 'native_replay_failed')
                hashes = {}
                for filename in ('part.step', 'part.stl', 'mass.json', 'roundtrip.mass.json',
                                 'native.gate.json', 'mesh.gate.json'):
                    hashes[filename] = core.sha256_file(first/filename)
                    require(hashes[filename] == core.sha256_file(second/filename), 5, 'native_replay_drift')
                mass = core.load_json(first/'mass.json', max_bytes=1024*1024)
                native = core.load_json(first/'native.gate.json', max_bytes=1024*1024)
                mesh = core.load_json(first/'mesh.gate.json', max_bytes=1024*1024)
                witness = core.load_json(first/'export.json', max_bytes=1024*1024)
                require(witness['source_sha256'] == source_sha, 4, 'source_stale')
                imported = base/(name+'.blender.json')
                command = [blender, '--background', '--factory-startup', '--python-exit-code', '2',
                           '--python', str(ROOT/'tools/cad/blender_import.py'), '--',
                           '--input', str(first/'part.stl'), '--out', str(imported)]
                observation = run(command, cwd=base, timeout_s=60, env=env)
                core.atomic_write_json(base/(name+'.blender.process.json'), observation)
                require(observation['rc'] == 0 and imported.is_file(), observation['rc'] or 3,
                        'blender_import_failed')
                measured = core.load_json(imported, max_bytes=1024*1024)
                require(measured['source_sha256'] == hashes['part.stl'] == core.sha256_file(first/'part.stl'),
                        4, 'import_source_stale')
                error = max(abs(measured['bbox_um'][j][i]-policy['expected_bbox_um'][j][i])
                            for j in range(2) for i in range(3))
                require(error <= policy['dimension_tolerance_um'], 5, 'blender_dimensions_mismatch')
                cases.append({'name': name, 'hashes': hashes, 'native_gate': native,
                              'mesh_gate': mesh, 'mass': mass, 'blender': measured,
                              'blender_bbox_error_um': error,
                              'step_roundtrip_identical_properties': witness['step_roundtrip_identical_properties'],
                              'replay_identical': True, 'bundle': name+'-a'})

            policy = analytic_part(hole=True)[1]
            negatives = []
            for name, parameters in (
                ('unit_swap', fixture_params(hole=True, scale=1000)),
                ('missing_hole', fixture_params(hole=False)),
                ('open_solid', fixture_params(hole=True, opened=True))):
                target, red = export(name+'-red', parameters, policy)
                require(red['rc'] == 5 and not target.exists(), 5, 'negative_control_survived')
                _, green = export(name+'-green', fixture_params(hole=True), policy)
                require(green['rc'] == 0, 5, 'restored_control_failed')
                negatives.append({'name': name, 'red_rc': red['rc'], 'green_rc': green['rc'],
                                  'refusal': red['tail'], 'refusal_preserved_outputs': True})
            # The mesh check must also reject an actually opened native STL.
            stl = base/'through_hole-a/part.stl'
            raw = bytearray(stl.read_bytes())
            count = struct.unpack_from('<I', raw, 80)[0]
            struct.pack_into('<I', raw, 80, count-1)
            opened = base/'open-native.stl'
            opened.write_bytes(raw[:-50])
            red = evaluate(opened, policy, density_kg_m3=1000, units='mm')
            green = evaluate(stl, policy, density_kg_m3=1000, units='mm')
            require(red['rc'] == 5 and green['rc'] == 0, 5, 'native_stl_control_failed')
            core.atomic_write_json(base/'open-native.gate.json', red)
            negatives.append({'name': 'open_native_stl', 'red_rc': red['rc'], 'green_rc': green['rc'],
                              'reason_codes': red['reason_codes']})
            peaks = [json.loads(p['tail'].splitlines()[-1])['peak_rss_bytes'] for p in process_records]
            peak = max(peaks)
            require(peak < 8*2**30, 6, 'provider_ceiling_exceeded')
            require(core.sha256_file(source) == source_sha, 4, 'source_stale')
            require(all(core.sha256_file(ROOT/f['path']) == f['sha256'] for f in source_files), 4, 'source_stale')
            require(runtime_identity(args.freeze) == identity, 4, 'runtime_stale')
            report = {'schema': 'af.cad-native-qualification/v1', 'scope': 'G10-native-analytic-fixtures',
                      'rc': 0, 'status': 'OK', 'verdict': 'PASS', 'runtime': identity,
                      'source_sha256': source_sha, 'source_files': source_files,
                      'cases': cases, 'negative_controls': negatives,
                      'peak_rss_bytes': peak, 'peak_gib_rounded_up': math.ceil(peak/2**30),
                      'resource_ceiling_bytes': 8*2**30, 'provider_readiness': 'CANNOT_SEE',
                      'unavailable': ['arbitrary_curved_surface_chord_bound', 'dependency_rights_review',
                                      'production_scheduler_admission_and_release', 'reproducible_offline_installation'],
                      'wall_ms': (time.monotonic_ns()-started)//10**6}
            core.validate(report, core.load_json(ROOT/'tools/cad/qualification.schema.json', max_bytes=1024*1024))
            core.atomic_write_json(base/'qualification.json', report)
            publish_new_directory(base, args.out)
            return core.emit(report)
    except Refusal as exc:
        return core.emit({'rc': exc.rc, 'status': core.STATUS[exc.rc],
                          'verdict': core.VERDICT[exc.rc], 'reason_codes': [exc.code]})
    except (OSError, ValueError, KeyError, TypeError, core.ContractError, metadata.PackageNotFoundError):
        return core.emit({'rc': 2, 'status': 'ERROR', 'verdict': 'ERROR',
                          'reason_codes': ['native_qualification_contract_or_io_error']})


if __name__ == '__main__':
    raise SystemExit(main())
