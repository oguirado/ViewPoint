"""Local BiRefNet foreground interface, separate from unverified upstream code.

Call install_network_guard only in an owned worker process: audit hooks are
permanent. This does not certify native-library egress containment or the
unavailable MoCap feature bridge, so the production runner remains blocked.
"""

import os
from pathlib import Path
import sys

from .bvh import Refusal, require
from .build.recipe import verify_inventory
from .contract import ROOT, load


def install_network_guard():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", HF_DATASETS_OFFLINE="1")

    def deny(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo",
                     "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            raise Refusal(2, "implicit_network_or_process_refused")

    sys.addaudithook(deny)


def load_birefnet(root):
    """Hash local model and custom code before importing any ML dependency.

    Preprocessing follows the pinned BiRefNet handler, not RMBG's tensor API.
    This function intentionally does not impersonate the BriaRMBG interface.
    """
    recipe = load(ROOT / "build" / "recipe.json")
    root = Path(root).resolve(strict=True)
    verify_inventory(root, recipe["models"]["birefnet"])
    # Extra local Python modules could be imported by the pinned custom model.
    expected = {f["path"] for f in recipe["models"]["birefnet"]["files"] if f["path"].endswith(".py")}
    actual = {str(p.relative_to(root)) for p in root.rglob("*.py")
              if ".cache" not in p.relative_to(root).parts}
    require(actual == expected, 4, "unlisted_model_code")
    install_network_guard()
    try:
        from transformers import AutoModelForImageSegmentation
        import torch
        from PIL import Image
        from torchvision import transforms
    except ImportError as exc:
        raise Refusal(3, "birefnet_runtime_missing") from exc
    model = AutoModelForImageSegmentation.from_pretrained(
        str(root), trust_remote_code=True, local_files_only=True).to("cpu").eval()
    transform = transforms.Compose([transforms.Resize((1024, 1024)), transforms.ToTensor(),
                                    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])

    def foreground(image):
        require(isinstance(image, Image.Image) and image.width * image.height <= 16 * 2**20,
                6, "image_pixel_budget")
        image = image.convert("RGB")
        with torch.inference_mode():
            mask = model(transform(image).unsqueeze(0))[-1].sigmoid().cpu()[0].squeeze()
        alpha = transforms.ToPILImage()(mask).resize(image.size)
        result = image.convert("RGBA")
        result.putalpha(alpha)
        return result

    return foreground
