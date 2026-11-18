"""Single-use CPU model child. Local code/weights are rehashed before imports.

BiRefNet is imported directly from its authenticated local package, avoiding HF
remote-code/cache loading. RealESRGAN uses its independently installed package;
neither path fetches a model or uses an ambient GPU.
"""
from __future__ import annotations

import argparse
import importlib
import io
import os
from pathlib import Path
import resource
import sys
import types

from asset_factory import core
from providers.birefnet import offline


def deny_network(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.sendto', 'urllib.Request'):
        raise offline.Issue(5, 'network_refused')


def infer(name, image, root, recipe):
    # Check before importing Torch or any authenticated third-party model code.
    # CPU tensors alone do not prevent upstream initialization from seeing CUDA.
    if os.environ.get('CUDA_VISIBLE_DEVICES') != '':
        raise offline.Issue(5, 'cuda_visibility_refused')
    import torch
    from PIL import Image
    torch.set_num_threads(recipe['threads'])
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True)
    with torch.inference_mode():
        if name == 'birefnet':
            from torchvision import transforms
            from safetensors.torch import load_file
            package = types.ModuleType('_af_birefnet_local')
            package.__path__ = [str(root)]
            sys.modules[package.__name__] = package
            config_module = importlib.import_module(package.__name__ + '.BiRefNet_config')
            model_module = importlib.import_module(package.__name__ + '.birefnet')
            model = model_module.BiRefNet(config=config_module.BiRefNetConfig(bb_pretrained=False))
            model.load_state_dict(load_file(str(root / 'model.safetensors'), device='cpu'), strict=True)
            model = model.to('cpu').float().eval()
            transform = transforms.Compose([
                transforms.Resize((recipe['input_side'], recipe['input_side'])),
                transforms.ToTensor(),
                transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])
            prediction = model(transform(image.convert('RGB')).unsqueeze(0))[-1].sigmoid()
            if not torch.isfinite(prediction).all().item():
                raise offline.Issue(5, 'nonfinite_alpha')
            alpha = transforms.ToPILImage()(prediction[0].squeeze().cpu()).resize(image.size, Image.Resampling.BILINEAR)
            result = image.convert('RGBA')
            result.putalpha(alpha)
            return result
        import numpy as np
        from basicsr.archs.rrdbnet_arch import RRDBNet
        # Restricted tensor-only deserialization before passing the authenticated
        # checkpoint to the upstream loader, whose runtime remains unqualified.
        checkpoint = torch.load(root / 'RealESRGAN_x4plus.pth', map_location='cpu', weights_only=True)
        if not isinstance(checkpoint, dict):
            raise offline.Issue(2, 'checkpoint_invalid')
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                        num_block=23, num_grow_ch=32, scale=4)
        # Load the verified tensors ourselves; do not call an upstream loader
        # that may use unrestricted pickle or fetch missing weights.
        state = checkpoint.get('params_ema', checkpoint.get('params'))
        if not isinstance(state, dict):
            raise offline.Issue(2, 'checkpoint_invalid')
        model.load_state_dict(state, strict=True)
        model = model.to('cpu').float().eval()
        pixels = np.array(image.convert('RGB'), dtype=np.float32) / 255.0
        tensor = torch.from_numpy(pixels.transpose(2, 0, 1).copy()).unsqueeze(0)
        # Call the installed RRDBNet directly: the learned x4 model is the
        # upscaler, with no implicit resize fallback or colour-channel swap.
        result_tensor = model(tensor)
        if not torch.isfinite(result_tensor).all().item():
            raise offline.Issue(5, 'nonfinite_upscale')
        array = result_tensor.squeeze(0).clamp(0, 1).mul(255).round().to(torch.uint8).permute(1, 2, 0).numpy()
        result = Image.fromarray(array, 'RGB').convert('RGBA')
        result.putalpha(image.convert('RGBA').getchannel('A').resize(result.size, Image.Resampling.NEAREST))
        return result


def main(argv=None):
    parser = offline.Parser()
    parser.add_argument('--provider', choices=('birefnet', 'realesrgan'), required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        sys.addaudithook(deny_network)
        recipe = core.load_json(args.scratch / 'recipe.json', max_bytes=offline.MAX_JSON)
        offline.validate_recipe(recipe, args.provider)
        root = offline.verify_local(args.root, recipe)
        packages = offline.package_observation(recipe)
        if any(not p['available'] or p['version'] is None for p in packages):
            raise offline.Issue(3, 'runtime_dependencies_missing_or_unpinned')
        if any(p['version'] != p['installed_version'] for p in packages):
            raise offline.Issue(4, 'runtime_version_stale')
        source_path = args.scratch / 'source.png'
        if source_path.stat().st_size > recipe['max_input_bytes']:
            raise offline.Issue(6, 'encoded_byte_budget')
        source = source_path.read_bytes()
        image = offline.decode(source, recipe)
        result = infer(args.provider, image, root, recipe)
        offline.measurements(image, result, args.provider, recipe['scale'])
        offline.verify_local(root, recipe)
        buffer = io.BytesIO()
        result.save(buffer, format='PNG')
        candidate = buffer.getvalue()
        offline.write_bytes(args.scratch / 'candidate.png', candidate)
        core.atomic_write_json(args.scratch / 'worker.json', {
            'rc': 0, 'source_sha256': offline.digest(source),
            'candidate_sha256': offline.digest(candidate), 'recipe_sha256': core.sha256_json(recipe),
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024})
        return 0
    except offline.Issue as exc:
        return core.emit(offline.record('worker', exc.rc, [exc.code]))
    except core.ContractError as exc:
        return core.emit(offline.record('worker', 2, [exc.code]))
    except (ImportError, FileNotFoundError):
        return core.emit(offline.record('worker', 3, ['runtime_unavailable']))
    except MemoryError:
        return core.emit(offline.record('worker', 6, ['memory_budget']))
    except Exception:
        return core.emit(offline.record('worker', 2, ['inference_error']))


if __name__ == '__main__':
    raise SystemExit(main())
