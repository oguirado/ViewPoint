"""Real CPU forward qualification on tiny synthetic checkpoints, never downloads."""
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from gates.rank.dino import LocalDino, PREPROCESS, PREPROCESS_V3
from gates.rank.metrics import RankError


class LocalForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import torch
            from transformers import Dinov2Config, Dinov2Model, DINOv3ViTConfig, DINOv3ViTModel
            from safetensors.torch import save_file
        except ImportError:
            raise unittest.SkipTest("Run with the installed AIModelos interpreter for CPU qualification")
        cls.torch = torch
        cls.families = (("dinov2", Dinov2Config, Dinov2Model, PREPROCESS),
                        ("dinov3", DINOv3ViTConfig, DINOv3ViTModel, PREPROCESS_V3))
        cls.save_file = staticmethod(save_file)

    def checkpoint(self, root, config_class, model_class):
        self.torch.manual_seed(0)
        config = config_class(hidden_size=32, num_hidden_layers=1, num_attention_heads=4,
                              intermediate_size=64, image_size=224, patch_size=16)
        config.architectures = [model_class.__name__]
        model = model_class(config).eval()
        (root / "config.json").write_text(json.dumps(config.to_dict()))
        self.save_file(model.state_dict(), str(root / "model.safetensors"))

    def test_strict_cpu_forwards_are_repeatable_and_never_fetch(self):
        for name, config_class, model_class, preprocess in self.families:
            with self.subTest(model=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.checkpoint(root, config_class, model_class)
                with patch("socket.socket.connect", side_effect=AssertionError("network attempted")), \
                        patch("socket.create_connection", side_effect=AssertionError("network attempted")):
                    runner = LocalDino(root)
                    image = Image.new("RGB", (256, 192), (37, 91, 127))
                    first, second = runner.embed(image), runner.embed(image)
                self.assertEqual(runner.model_id, name)
                self.assertEqual(runner.identity["preprocess"], preprocess)
                self.assertEqual(runner.identity["device"], "cpu")
                self.assertEqual(len(first), 32)
                self.assertTrue(all(math.isfinite(v) for v in first))
                self.assertEqual(first, second)
                self.assertTrue(all(p.device.type == "cpu" for p in runner.model.parameters()))

    def test_incomplete_checkpoint_red_then_restored_green(self):
        for name, config_class, model_class, _ in self.families:
            with self.subTest(model=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.checkpoint(root, config_class, model_class)
                saved = (root / "model.safetensors").read_bytes()
                self.save_file({"unrelated": self.torch.ones(1)}, str(root / "model.safetensors"))
                with self.assertRaises(RankError) as caught:
                    LocalDino(root)
                self.assertEqual(caught.exception.rc, 4)
                self.assertEqual(caught.exception.reason, "dino_checkpoint_mismatch")
                (root / "model.safetensors").write_bytes(saved)
                self.assertEqual(len(LocalDino(root).embed(Image.new("RGB", (224, 224)))), 32)


if __name__ == "__main__":
    unittest.main()
