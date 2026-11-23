"""Shared offline image boundary; no model imports in the host process.

Budgets are resource limits, never calibrated image-quality thresholds. Providers
remain blocked until rights, dependency pins and measured peaks are established.
"""
from __future__ import annotations
from asset_factory import proc as af_proc

import argparse
import hashlib
import importlib.metadata
import importlib.util
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import warnings

from asset_factory import core

HERE = Path(__file__).resolve().parent
MAX_JSON = 2**20


class Issue(Exception):
    def __init__(self, rc, code):
        self.rc, self.code = rc, code
        super().__init__(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def is_digest(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def closed(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise Issue(2, 'contract_invalid')


def recipe_for(name):
    return core.load_json(HERE.parent / name / 'build/recipe.json', max_bytes=MAX_JSON)


def provider_for(name):
    return core.load_json(HERE.parent / name / 'provider.json', max_bytes=MAX_JSON)


def validate_recipe(recipe, name):
    schema = core.load_json(HERE / 'build/recipe.schema.json', max_bytes=MAX_JSON)
    core.validate(recipe, schema)
    if (recipe['provider_id'] != name or recipe['device'] != 'cpu'
            or recipe['network'] is not False or name not in ('birefnet', 'realesrgan')):
        raise Issue(2, 'recipe_invalid')
    for key, limit in (('max_input_bytes', 32*2**20), ('max_pixels', 4*2**20),
                       ('timeout_s', 300), ('threads', 16), ('memory_max_bytes', 8*2**30)):
        if type(recipe[key]) is not int or not 0 < recipe[key] <= limit:
            raise Issue(2, 'budget_invalid')
    if recipe['scale'] != (1 if name == 'birefnet' else 4):
        raise Issue(2, 'scale_invalid')
    if recipe['input_side'] != (1024 if name == 'birefnet' else None):
        raise Issue(2, 'resolution_invalid')
    paths = [f['path'] for f in recipe['files']]
    required = ({'birefnet.py', 'BiRefNet_config.py', 'config.json', 'model.safetensors'}
                if name == 'birefnet' else {'RealESRGAN_x4plus.pth'})
    if len(paths) != len(set(paths)) or set(paths) != required:
        raise Issue(2, 'inventory_invalid')
    pin = 'local-sha256:' + core.sha256_json(recipe['files'])
    if recipe['revision'] != pin:
        raise Issue(4, 'revision_stale')
    for item in recipe['files']:
        path = PurePosixPath(item['path'])
        if (path.is_absolute() or '..' in path.parts or '\\' in item['path']
                or not is_digest(item['sha256']) or type(item['bytes']) is not int
                or not 0 < item['bytes'] <= recipe['memory_max_bytes']):
            raise Issue(2, 'inventory_invalid')
    package_names = [p['distribution'] for p in recipe['packages']]
    required_packages = {'torch', 'torchvision', 'Pillow', 'numpy'} | (
        {'transformers', 'timm', 'kornia', 'einops', 'safetensors'} if name == 'birefnet'
        else {'basicsr'})
    if len(package_names) != len(set(package_names)) or set(package_names) != required_packages:
        raise Issue(2, 'packages_invalid')
    for package in recipe['packages']:
        if package['module'] != {'Pillow': 'PIL'}.get(package['distribution'], package['distribution']):
            raise Issue(2, 'packages_invalid')


def verify_local(root, recipe):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise Issue(2, 'model_root_invalid')
    for item in recipe['files']:
        path = (root / item['path']).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise Issue(2, 'model_path_escape')
        if path.stat().st_size != item['bytes'] or core.sha256_file(path) != item['sha256']:
            raise Issue(4, 'model_bytes_stale')
    if recipe['provider_id'] == 'birefnet':
        config = core.load_json(root / 'config.json', max_bytes=MAX_JSON)
        if config.get('bb_pretrained') is not False:
            raise Issue(5, 'backbone_download_refused')
    return root


def package_observation(recipe):
    rows = []
    for item in recipe['packages']:
        try:
            version = importlib.metadata.version(item['distribution'])
            available = importlib.util.find_spec(item['module']) is not None
        except (importlib.metadata.PackageNotFoundError, ImportError, ValueError):
            version, available = None, False
        rows.append({**item, 'installed_version': version, 'available': available})
    return rows


def decode(data, recipe):
    if type(data) is not bytes:
        raise Issue(2, 'image_invalid')
    if len(data) > recipe['max_input_bytes']:
        raise Issue(6, 'encoded_byte_budget')
    try:
        from PIL import Image
    except ImportError as exc:
        raise Issue(3, 'decoder_unavailable') from exc
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=('PNG',)) as image:
                if image.width * image.height > recipe['max_pixels']:
                    raise Issue(6, 'decoded_pixel_budget')
                if image.mode not in ('RGB', 'RGBA') or data[24] != 8 or image.n_frames != 1:
                    raise Issue(2, 'image_encoding_unsupported')
                image.load()
                return image.copy()
    except Issue:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise Issue(6, 'decoded_pixel_budget') from exc
    except Exception as exc:
        raise Issue(2, 'image_invalid') from exc


def measurements(original, candidate, name, scale):
    """Integer observables; no invented semantic/edge-quality acceptance bound."""
    from PIL import Image
    expected = (original.width * scale, original.height * scale)
    if candidate.size != expected or candidate.mode != 'RGBA':
        raise Issue(5, 'output_shape_or_channels')
    rgb = candidate.convert('RGB')
    alpha = candidate.getchannel('A')
    hist = alpha.histogram()
    metrics = {'input_width': original.width, 'input_height': original.height,
               'output_width': candidate.width, 'output_height': candidate.height,
               'scale': scale, 'alpha_min': alpha.getextrema()[0],
               'alpha_max': alpha.getextrema()[1], 'alpha_nonzero_pixels': sum(hist[1:]),
               'alpha_nonopaque_pixels': sum(hist[:255]),
               'alpha_sum': sum(i*n for i, n in enumerate(hist)),
               'rgb_dynamic_range': max(b-a for a, b in rgb.getextrema())}
    if name == 'birefnet':
        if rgb.tobytes() != original.convert('RGB').tobytes():
            raise Issue(5, 'source_rgb_changed')
        if metrics['alpha_min'] == metrics['alpha_max']:
            raise Issue(5, 'constant_alpha')
    else:
        expected_alpha = original.convert('RGBA').getchannel('A').resize(expected, Image.Resampling.NEAREST)
        if alpha.tobytes() != expected_alpha.tobytes():
            raise Issue(5, 'alpha_changed')
        if metrics['rgb_dynamic_range'] == 0:
            raise Issue(5, 'flat_upscale')
    return metrics


def record(name, rc, reasons=(), **values):
    result = {'schema': 'af.offline-image-result/v1', 'provider_id': name,
            'rc': rc, 'status': core.STATUS[rc], 'reason_codes': list(reasons),
            'quality_verdict': 'CANNOT_SEE', **values}
    core.validate(result, core.load_json(HERE / 'offline-image-result.schema.json', max_bytes=MAX_JSON))
    return result


def admit_memory(factory, peak_bytes, recipe):
    from . import resources
    memory = factory['memory']
    if (memory['max_resident_models'] != 1
            or peak_bytes > min(memory['ceiling_gb_default'] * 2**30, recipe['memory_max_bytes'])):
        raise Issue(5, 'peak_over_ceiling')
    try:
        available = resources.host_available()
        group = resources.cgroup_observation()
    except resources.ObservationUnavailable as exc:
        raise Issue(3, str(exc)) from exc
    headroom = group['headroom_bytes']
    # None here means every observed ancestor explicitly says "max"; a missing
    # file raises above. Host availability then supplies the finite bound.
    effective = available if headroom is None else min(available, headroom)
    if effective - peak_bytes < memory['min_free_gb_before_start'] * 2**30:
        raise Issue(5, 'headroom_below_floor')


def probe(name, source, model_root, *, recipe=None):
    """Read-only measured admission; never loads a model or publishes an image."""
    values = {'source_sha256': digest(source) if type(source) is bytes else None,
              'model_files_verified': False, 'packages': [], 'metrics': None}
    try:
        recipe = recipe_for(name) if recipe is None else recipe
        validate_recipe(recipe, name)
        image = decode(source, recipe)
        values['metrics'] = {'input_width': image.width, 'input_height': image.height}
        verify_local(model_root, recipe)
        values['model_files_verified'] = True
        values['recipe_sha256'] = core.sha256_json(recipe)
        values['packages'] = package_observation(recipe)
        reasons = []
        if any(not p['available'] for p in values['packages']):
            reasons.append('runtime_dependencies_missing')
        if any(p['version'] is None for p in values['packages']):
            reasons.append('runtime_dependencies_unpinned')
        if any(p['available'] and p['version'] is not None and p['installed_version'] != p['version']
               for p in values['packages']):
            return record(name, 4, ['runtime_version_stale'], **values)
        if recipe['rights_status'] != 'PASS':
            reasons.append('rights_unverified')
        return record(name, 3 if reasons else 0, reasons, **values)
    except Issue as exc:
        return record(name, exc.rc, [exc.code], **values)
    except FileNotFoundError:
        return record(name, 3, ['local_model_missing'], **values)
    except core.ContractError as exc:
        return record(name, 2, [exc.code], **values)
    except (OSError, TypeError, ValueError, KeyError):
        return record(name, 2, ['input_invalid'], **values)


def kill_group(process):
    try:
        af_proc.signal_group(process, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def supervised(argv, *, env, timeout_s, cwd):
    """Disk spools and bounded lifetime; no PIPE buffering or inherited secrets."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = af_proc.popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=errors, env=env, cwd=cwd, start_new_session=True)
        try:
            af_proc.wait_exit(process, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            kill_group(process)
            raise Issue(6, 'deadline_exceeded') from exc
        except BaseException:
            kill_group(process)
            raise
        # Retire descendants even when their leader returned successfully.
        kill_group(process)
        if process.returncode not in core.RC or type(process.returncode) is not int:
            raise Issue(2, 'worker_contract')
        if process.returncode:
            raise Issue(process.returncode, 'worker_refused')


def run_worker(name, source, model_root, recipe, scratch, timeout_s):
    source_path = scratch / 'source.png'
    source_path.write_bytes(source)
    core.atomic_write_json(scratch / 'recipe.json', recipe)
    env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(HERE.parents[1]), 'CUDA_VISIBLE_DEVICES': '',
           'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
           'HF_HOME': str(scratch / 'cache'), 'TORCH_HOME': str(scratch / 'cache'),
           'OMP_NUM_THREADS': str(recipe['threads']), 'MKL_NUM_THREADS': str(recipe['threads']),
           'OPENBLAS_NUM_THREADS': str(recipe['threads'])}
    argv = [sys.executable, '-B', '-m', 'providers.birefnet.worker',
            '--provider', name, '--root', str(model_root), '--scratch', str(scratch)]
    supervised(argv, env=env, timeout_s=timeout_s, cwd=HERE.parents[1])
    metadata = core.load_json(scratch / 'worker.json', max_bytes=MAX_JSON)
    closed(metadata, ('rc', 'source_sha256', 'candidate_sha256', 'recipe_sha256', 'peak_rss_bytes'))
    candidate_path = scratch / 'candidate.png'
    if candidate_path.stat().st_size > recipe['max_input_bytes'] * recipe['scale']**2:
        raise Issue(6, 'encoded_byte_budget')
    candidate = candidate_path.read_bytes()
    if (type(metadata['rc']) is not int or metadata['rc'] != 0
            or type(metadata['peak_rss_bytes']) is not int or metadata['peak_rss_bytes'] <= 0
            or metadata['source_sha256'] != digest(source)
            or metadata['candidate_sha256'] != digest(candidate)
            or metadata['recipe_sha256'] != core.sha256_json(recipe)):
        raise Issue(4, 'worker_evidence_stale')
    return candidate, metadata


def preparation_receipt(original, candidate, provider, decision):
    return {'schema': 'af.reference-preparation/v1', 'rc': 0, 'status': 'OK',
            'provider_id': 'birefnet', 'revision': provider['revision'],
            'code_sha256': code_identity(), 'weights_sha256': core.sha256_json(provider['weights']),
            'original_sha256': digest(original), 'candidate_sha256': digest(candidate),
            'decision_sha256': core.sha256_json(decision)}


def code_identity(name='birefnet'):
    return core.sha256_json({p.name: core.sha256_file(p) for p in
                            (HERE / 'offline.py', HERE / 'worker.py', HERE / 'resources.py', HERE.parent / name / 'adapter.py',
                             Path(core.__file__), HERE / 'build/recipe.schema.json')})


def write_bytes(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish(target, candidate, receipt):
    """Exclusive output directory; receipt is the last commit marker."""
    target = Path(target)
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise Issue(5, 'output_exists') from exc
    try:
        write_bytes(target / 'candidate.png', candidate)
        if digest((target / 'candidate.png').read_bytes()) != receipt['candidate_sha256']:
            raise Issue(4, 'publication_stale')
        core.atomic_write_json(target / 'receipt.json', receipt)
    except BaseException:
        shutil.rmtree(target)
        raise


def prepare(name, source, model_root, target, *, factory, licence_policy, gate_policy,
            calibration_bytes, lease_path, timeout_s, recipe=None, provider=None):
    """An OK stage is measured preparation, never semantic appearance approval.

    All runtime inputs are explicit. The common lease must be shared with the
    install's scheduler. Stale-owner recovery is intentionally unsupported.
    """
    started = time.monotonic_ns()
    lease_fd = None
    try:
        from licence.check import check_provider
        from asset_factory.contracts import validate_provider
        from gates import input_gate
        recipe = recipe_for(name) if recipe is None else recipe
        provider = provider_for(name) if provider is None else provider
        validate_recipe(recipe, name)
        validate_provider(provider)
        original = decode(source, recipe)
        if type(timeout_s) is not int or not 0 < timeout_s <= recipe['timeout_s']:
            raise Issue(2, 'timeout_invalid')
        if provider['id'] != name or provider['revision'] != recipe['revision']:
            raise Issue(4, 'provider_stale')
        expected_weights = [{k: f[k] for k in ('path', 'sha256')} for f in recipe['files']
                            if f['path'].endswith(('.safetensors', '.pth'))]
        if (len(provider['weights']) != 1 or provider['weights'][0]['files'] != expected_weights
                or provider['weights'][0]['revision'] != recipe['revision']):
            raise Issue(4, 'provider_weights_stale')
        decision = check_provider(factory, provider, operation='generate', policy=licence_policy)
        if decision['rc'] != 0 or decision['allowed'] is not True:
            raise Issue(decision['rc'] or 5, 'licence_refused')
        admission = probe(name, source, model_root, recipe=recipe)
        if admission['rc']:
            raise Issue(admission['rc'], admission['reason_codes'][0])
        peak = provider['runtime']['ram_peak_gb_measured']
        peak_ref = provider['runtime']['ram_peak_measurement_ref']
        if peak is None or peak_ref is None:
            raise Issue(3, 'peak_unmeasured')
        if peak * 2**30 > recipe['memory_max_bytes']:
            raise Issue(5, 'peak_over_ceiling')
        peak_record = core.load_json(Path(peak_ref['path']), max_bytes=MAX_JSON)
        if (core.sha256_file(Path(peak_ref['path'])) != peak_ref['sha256']
                or peak_record.get('schema') != 'af.offline-image-peak/v1'
                or peak_record.get('weights_sha256') != core.sha256_json(provider['weights'])
                or peak_record.get('recipe_sha256') != core.sha256_json(recipe)
                or peak_record.get('code_sha256') != code_identity(name)
                or type(peak_record.get('peak_rss_bytes')) is not int
                or not 0 < peak_record['peak_rss_bytes'] <= peak * 2**30):
            raise Issue(4, 'peak_measurement_stale')
        admit_memory(factory, peak * 2**30, recipe)
        if Path(target).exists() or Path(target).is_symlink():
            raise Issue(5, 'output_exists')
        # Check calibration BEFORE allocating/loading the model.
        input_gate._policy(gate_policy)
        input_gate._calibration(gate_policy, calibration_bytes)
        try:
            lease_fd = os.open(lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise Issue(5, 'lease_held') from exc
        with tempfile.TemporaryDirectory(prefix='af-image-') as directory:
            scratch = Path(directory)
            candidate, worker = run_worker(name, source, Path(model_root).resolve(), recipe, scratch, timeout_s)
            # Bind the same selected files after inference as before it.
            verify_local(model_root, recipe)
            candidate_recipe = {**recipe, 'max_pixels': recipe['max_pixels'] * recipe['scale']**2,
                                'max_input_bytes': recipe['max_input_bytes'] * recipe['scale']**2}
            decoded = decode(candidate, candidate_recipe)
            measured = measurements(original, decoded, name, recipe['scale'])
            if worker['peak_rss_bytes'] > recipe['memory_max_bytes']:
                raise Issue(6, 'memory_budget')
            if worker['peak_rss_bytes'] > peak * 2**30:
                return record(name, 6, ['admitted_peak_exceeded'],
                              peak_rss_bytes=worker['peak_rss_bytes'],
                              metrics={'admitted_peak_bytes': peak * 2**30})
            prep = preparation_receipt(source, candidate, provider, decision) if name == 'birefnet' else None
            gate_args = dict(policy=gate_policy, policy_sha256=core.sha256_json(gate_policy),
                             toolchain_sha256=input_gate.toolchain_identity(), calibration_bytes=calibration_bytes)
            if name == 'birefnet':
                gate = input_gate.inspect_prepared_reference(source, candidate,
                    preparation_receipt=prep, decision=decision, provider=provider,
                    configuration_sha256=core.sha256_json(factory), licence_policy_sha256=core.sha256_json(licence_policy),
                    jurisdiction=factory['jurisdiction'], commercial_use=factory['commercial_use'],
                    code_sha256=code_identity(), **gate_args)
            else:
                gate = input_gate.inspect_reference(candidate, **gate_args)
            if gate['rc']:
                return record(name, gate['rc'], gate['reason_codes'], gate=gate)
            result = record(name, 0, source_sha256=digest(source), candidate_sha256=digest(candidate),
                recipe_sha256=core.sha256_json(recipe), provider_sha256=core.sha256_json(provider),
                decision_sha256=core.sha256_json(decision), preparation=prep, metrics=measured,
                code_sha256=code_identity(name),
                peak_rss_bytes=worker['peak_rss_bytes'], wall_ms=(time.monotonic_ns()-started)//10**6,
                gate=gate, absence_reasons={'quality_verdict': 'Independent semantic and golden-mask/SR review absent'})
            publish(target, candidate, result)
            return result
    except Issue as exc:
        return record(name, exc.rc, [exc.code])
    except core.ContractError as exc:
        return record(name, 2, [exc.code])
    except (FileNotFoundError, ModuleNotFoundError):
        return record(name, 3, ['dependency_or_local_file_missing'])
    except (OSError, TypeError, ValueError, KeyError):
        return record(name, 2, ['input_invalid'])
    except Exception as exc:
        # The existing gate has its own explicit refusal type.
        if type(exc).__name__ == 'GateIssue' and getattr(exc, 'rc', None) in core.RC:
            return record(name, exc.rc, [exc.code])
        raise
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
            Path(lease_path).unlink()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Issue(2, 'arguments_invalid')


def main(name, argv=None):
    parser = Parser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--model-root', type=Path, required=True)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--factory', type=Path)
    parser.add_argument('--licence-policy', type=Path)
    parser.add_argument('--gate-policy', type=Path)
    parser.add_argument('--calibration', type=Path)
    parser.add_argument('--lease', type=Path)
    parser.add_argument('--timeout-s', type=int)
    try:
        args = parser.parse_args(argv)
        recipe = recipe_for(name)
        if args.source.stat().st_size > recipe['max_input_bytes']:
            raise Issue(6, 'encoded_byte_budget')
        source = args.source.read_bytes()
        if args.probe:
            result = probe(name, source, args.model_root, recipe=recipe)
        else:
            if any(getattr(args, k) is None for k in ('output', 'factory', 'licence_policy', 'gate_policy', 'calibration', 'lease', 'timeout_s')):
                raise Issue(2, 'arguments_invalid')
            def load(path):
                return core.load_json(path, max_bytes=MAX_JSON)
            if args.calibration.stat().st_size > MAX_JSON:
                raise Issue(6, 'calibration_byte_budget')
            result = prepare(name, source, args.model_root, args.output,
                factory=load(args.factory), licence_policy=load(args.licence_policy),
                gate_policy=load(args.gate_policy), calibration_bytes=args.calibration.read_bytes(),
                lease_path=args.lease, timeout_s=args.timeout_s, recipe=recipe)
    except Issue as exc:
        result = record(name, exc.rc, [exc.code])
    except core.ContractError as exc:
        result = record(name, 2, [exc.code])
    except (OSError, ValueError, TypeError):
        result = record(name, 2, ['input_invalid'])
    return core.emit(result)
