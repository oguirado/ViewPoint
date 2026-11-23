"""Explicitly selected, pinned local DINO model. No hub or model fallback.

    DINOv2: bicubic short edge 256 then centre 224. DINOv3: bilinear square
    224. Both use RGB on white, ImageNet mean/std and final-layernorm CLS.
    CPU float32, one image at a time; no automatic checkpoint substitution.
"""
import os
import json
from pathlib import Path
from unittest.mock import patch

from gates.rank.metrics import RankError, require

PREPROCESS = "rgb-white-short256-bicubic-centre224-imagenet-cls/v1"
PREPROCESS_V3 = "rgb-white-square224-bilinear-imagenet-cls/v1"
FAMILIES = {"dinov2": ("dinov2", "Dinov2Model", PREPROCESS),
            "dinov3": ("dinov3_vit", "DINOv3ViTModel", PREPROCESS_V3)}


def validate_model_document(document, expected_id):
    require(expected_id in FAMILIES, 2, "model_family_unsupported")
    kind, architecture, _ = FAMILIES[expected_id]
    require(document.get("model_type") == kind and
            document.get("architectures") == [architecture], 2, "model_family_mismatch")


def offline_environment():
    return {"CUDA_VISIBLE_DEVICES": "", "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _no_network(*args, **kwargs):
    raise RankError(3, "network_forbidden")


class LocalDino:
    def __init__(self, root):
        os.environ.update(offline_environment())
        try:
            import torch
            import transformers
            import safetensors
            from safetensors.torch import load_file
        except ImportError as exc:
            raise RankError(3, "dino_runtime_unavailable") from exc
        torch.set_num_threads(4)
        torch.manual_seed(0)
        torch.use_deterministic_algorithms(True)
        require(Path(root).is_dir(), 3, "dino_weights_missing")
        try:
            with patch("socket.socket.connect", _no_network), patch("socket.create_connection", _no_network):
                # Read exactly the pinned files, without adapter/base-model search.
                document = json.loads((Path(root) / "config.json").read_text())
                matches = [name for name, (kind, arch, _) in FAMILIES.items()
                           if document.get("model_type") == kind and document.get("architectures") == [arch]]
                require(len(matches) == 1, 2, "model_family_unsupported")
                self.model_id = matches[0]
                self.preprocess = FAMILIES[self.model_id][2]
                if self.model_id == "dinov2":
                    from transformers import Dinov2Config, Dinov2Model
                    config = Dinov2Config.from_dict(document)
                    self.model = Dinov2Model(config)
                else:
                    from transformers import DINOv3ViTConfig, DINOv3ViTModel
                    config = DINOv3ViTConfig.from_dict(document)
                    self.model = DINOv3ViTModel(config)
                state = load_file(str(Path(root) / "model.safetensors"), device="cpu")
                self.model.load_state_dict(state, strict=True)
                del state
            self.model.to(device="cpu", dtype=torch.float32).eval()
            require(all(p.device.type == "cpu" for p in self.model.parameters()), 3, "dino_device_unproven")
        except ImportError as exc:
            raise RankError(3, "dino_runtime_unavailable") from exc
        except RuntimeError as exc:
            raise RankError(4, "dino_checkpoint_mismatch") from exc
        except (OSError, ValueError) as exc:
            raise RankError(3, "dino_load_failed") from exc
        self.torch = torch
        import PIL
        import sys
        self.identity = {"device": "cpu", "torch": str(torch.__version__),
                         "transformers": transformers.__version__,
                         "safetensors": safetensors.__version__, "pillow": PIL.__version__,
                         "python": sys.version.split()[0], "preprocess": self.preprocess}

    def embed(self, image):
        from PIL import Image
        torch = self.torch
        width, height = image.size
        if self.model_id == "dinov3":
            # Local DINOv3 processor declares square 224, bilinear, no crop.
            crop = image.convert("RGB").resize((224, 224), Image.Resampling.BILINEAR)
        else:
            # Short edge and centre-crop semantics are explicit, including floor.
            scale = 256 / min(width, height)
            resized = image.convert("RGB").resize((int(width * scale), int(height * scale)), Image.Resampling.BICUBIC)
            x, y = (resized.width - 224) // 2, (resized.height - 224) // 2
            crop = resized.crop((x, y, x + 224, y + 224))
        pixels = torch.tensor(list(crop.getdata()), dtype=torch.float32).reshape(224, 224, 3)
        tensor = pixels.permute(2, 0, 1).unsqueeze(0) / 255
        tensor = (tensor - torch.tensor([.485, .456, .406])[None, :, None, None]) / torch.tensor([.229, .224, .225])[None, :, None, None]
        with torch.inference_mode(), patch("socket.socket.connect", _no_network), patch("socket.create_connection", _no_network):
            vector = self.model(pixel_values=tensor).last_hidden_state[0, 0].float().tolist()
        require(all(p.device.type == "cpu" for p in self.model.parameters()), 3, "dino_device_unproven")
        return vector
