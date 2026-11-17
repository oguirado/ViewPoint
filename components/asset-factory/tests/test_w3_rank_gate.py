"""Boundary and mutant controls. Learned scores are synthetic in unit tests."""
import contextlib
import copy
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from asset_factory import core
from gates.rank import evaluate as ev
from gates.rank import metrics
from gates.rank import rank
from gates.rank.dino import LocalDino, offline_environment
from gates.rank.finalize import finalize
from tools.mesh import contracts as mesh_contracts


def mesh_receipt(subject, rc=0):
    """Complete synthetic intake evidence; regression distances are inapplicable."""
    receipt = mesh_contracts.receipt(subject, None, "a" * 64, "b" * 64)
    receipt['metrics']['component_inventories'] = [
        {'components': 1, 'tris': 12, 'boundary_edges': 0, 'failed': []}]
    receipt['unavailable'].pop('component_inventories')
    return mesh_contracts.finish(receipt, rc, 'synthetic_failure' if rc else None)[1]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text('{"model_type":"dinov2","architectures":["Dinov2Model"]}')
        (self.model / "model.safetensors").write_bytes(b"synthetic-not-a-checkpoint")
        self.white = [0] * 16
        self.target = [255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0, 0, 0, 0, 0]
        reference = self.image("reference.png", self.target)
        mask = self.image("reference-mask.png", self.target, mask=True)
        capture = {"id": "front", "width": 4, "height": 4,
                   "projection": "orthographic", "axis": "front",
                   "framing": "explicit-no-registration/v1", "camera_sha256": "c" * 64}
        self.request = {"schema": "af.seed-rank-request/v1", "mode": "seeds",
                        "evidence_root": str(self.root), "reference": {"image": reference, "mask": mask},
                        "capture": capture,
                        "model": {"id": "dinov2", "root": str(self.model), "revision": "synthetic-v1",
                                  "config_sha256": core.sha256_file(self.model / "config.json"),
                                  "weights_sha256": core.sha256_file(self.model / "model.safetensors"),
                                  "licence": "synthetic", "commercial_use": False},
                        "policy": {"iou_weight": 3, "dino_weight": 1, "min_iou_ppm": 500000,
                                   "min_dino_ppm": 500000, "timeout_s": 30}, "candidates": []}
        # L-shape, shortened L, solid cube. Distinct render bytes, same reference.
        masks = [self.target, [255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0], [255] * 16]
        for seed, pixels in enumerate(masks, 1):
            artifact = self.write(f"mesh{seed}.glb", f"synthetic-mesh-{seed}".encode())
            render = self.image(f"render{seed}.png", pixels, tint=seed)
            silhouette = self.image(f"mask{seed}.png", pixels, mask=True)
            generation = self.write_json(f"generation{seed}.json", {
                "rc": 0, "seed": seed, "input_sha256": reference["sha256"],
                "output_sha256": artifact["sha256"]})
            gate = self.write_json(f"gate{seed}.json", mesh_receipt(artifact['sha256']))
            cap = self.write_json(f"capture{seed}.json", {
                "schema": "af.rank-capture/v1", "rc": 0, "input_sha256": artifact["sha256"],
                "capture_sha256": core.sha256_json(capture), "render_sha256": render["sha256"],
                "mask_sha256": silhouette["sha256"], "mask_policy": "alpha_ge_128",
                "renderer_sha256": core.sha256_file(Path(ev.__file__).with_name("capture.py"))})
            self.request["candidates"].append({"id": f"seed-{seed}", "seed": seed,
                "reference_sha256": reference["sha256"], "capture_sha256": core.sha256_json(capture),
                "render": render, "mask": silhouette, "artifact": artifact,
                "generation": generation, "gate": gate, "capture_receipt": cap,
                "gate_expectation": {"operation": "metrics", "policy_sha256": "a" * 64,
                                     "toolchain_sha256": "b" * 64}})

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return {"path": name, "sha256": core.sha256_file(path)}

    def write_json(self, name, obj):
        return self.write(name, core.canonical_json(obj).encode())

    def image(self, name, pixels, *, mask=False, tint=0):
        im = Image.new("L" if mask else "RGB", (4, 4))
        im.putdata(pixels if mask else [(tint, 80, 120) if p else (255, 255, 255) for p in pixels])
        out = io.BytesIO()
        im.save(out, format="PNG")
        return self.write(name, out.getvalue())

    def evaluate(self, request=None, loader=None):
        class SyntheticDino:
            identity = {"device": "cpu", "test_only": True}
            def __init__(self, root):
                pass
            def embed(self, image):
                return [1, 0, 0]  # Equal cosine isolates actual silhouette measurement.
        return ev.evaluate(request or self.request, _loader=loader or SyntheticDino)

    def assert_rc(self, request, expected, reason):
        result = self.evaluate(request)
        self.assertEqual(result["rc"], expected, result)
        self.assertIn(reason, result["reason_codes"])
        self.assertIsNone(result["winner_id"])
        return result


class RankTests(Fixture):
    def test_explicit_model_family_mismatch_is_red_then_green(self):
        self.request["model"]["id"] = "dinov3"
        self.assert_rc(self.request, 2, "model_family_mismatch")
        self.request["model"]["id"] = "dinov2"
        self.assertEqual(self.evaluate()["rc"], 0)

    def test_dinov3_contract_uses_its_own_preprocessing(self):
        class SyntheticV3:
            identity = {"device": "cpu", "preprocess": "rgb-white-square224-bilinear-imagenet-cls/v1"}
            def __init__(self, root):
                pass
            def embed(self, image):
                return [1, 0, 0]
        self.request["model"]["id"] = "dinov3"
        (self.model / "config.json").write_text('{"model_type":"dinov3_vit","architectures":["DINOv3ViTModel"]}')
        self.request["model"]["config_sha256"] = core.sha256_file(self.model / "config.json")
        result = self.evaluate(loader=SyntheticV3)
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["preprocess"], "rgb-white-square224-bilinear-imagenet-cls/v1")
        core.validate(result, core.load_json(Path(ev.__file__).with_name("result.schema.json"), max_bytes=ev.MAX_JSON))

    def test_three_seeds_white_cube_last_and_repeatable(self):
        one, two = self.evaluate(), self.evaluate()
        self.assertEqual(one["rc"], 0, one)
        self.assertEqual([r["id"] for r in one["rankings"]], ["seed-1", "seed-2", "seed-3"])
        self.assertEqual(one["rankings"], two["rankings"])
        self.assertEqual(one["rankings"][0]["iou_ppm"], 1000000)
        self.assertEqual(one["rankings"][-1]["iou_ppm"], 312500)
        self.assertEqual(one["asset_verdict"], "CANNOT_SEE")
        core.canonical_json(one)  # No floating JSON or bool-as-integer.

    def test_red_then_green_iou_mutant_is_detected(self):
        def oracle():
            result = self.evaluate()
            self.assertEqual(result["rankings"][-1]["id"], "seed-3")
            self.assertEqual(result["rankings"][-1]["iou_ppm"], 312500)
        with patch.object(ev, "silhouette_iou_ppm", return_value=1000000):
            with self.assertRaises(AssertionError):
                oracle()  # Deliberate broken evaluator goes RED.
        oracle()  # The identical assertion goes GREEN without changing it.

    def test_gate_failure_cannot_be_overridden_by_perfect_similarity(self):
        c = self.request["candidates"][0]
        c["gate"] = self.write_json("refused.json", mesh_receipt(c['artifact']['sha256'], 5))
        result = self.evaluate()
        self.assertEqual(result["winner_id"], "seed-2")
        self.assertFalse(next(r for r in result["rankings"] if r["id"] == "seed-1")["eligible"])

    def test_all_below_floor_refused(self):
        self.request["policy"]["min_iou_ppm"] = 1000000
        self.request["candidates"] = self.request["candidates"][1:]
        self.assert_rc(self.request, 5, "no_candidate_clears_gates_and_floors")

    def test_all_upstream_fail_remain_fail(self):
        for c in self.request["candidates"]:
            c["gate"] = self.write_json(c["gate"]["path"], mesh_receipt(c['artifact']['sha256'], 5))
        self.assert_rc(self.request, 5, "no_candidate_clears_gates_and_floors")

    def test_missing_dino_keeps_iou_but_no_similarity_or_winner(self):
        def missing(root):
            raise metrics.RankError(3, "dino_runtime_unavailable")
        result = self.evaluate(loader=missing)
        self.assertEqual(result["rc"], 3)
        self.assertIsNone(result["preferred_id"])
        self.assertIsNone(result["winner_id"])
        self.assertEqual(result["rankings"][0]["iou_ppm"], 1000000)
        self.assertTrue(all(r["dino_ppm"] is None and r["composite_ppm"] is None for r in result["rankings"]))

    def test_changed_render_refused_before_inference(self):
        (self.root / "render1.png").write_bytes(b"tampered")
        self.assert_rc(self.request, 4, "evidence_stale")

    def test_missing_candidate_does_not_promote_survivor(self):
        (self.root / "render2.png").unlink()
        self.assert_rc(self.request, 3, "evidence_missing")

    def test_camera_reference_and_duplicate_seed_controls(self):
        for key, value, rc, reason in (("capture_sha256", "a" * 64, 4, "capture_mismatch"),
                                      ("reference_sha256", "a" * 64, 4, "reference_mismatch"),
                                      ("seed", 2, 2, "duplicate_seed")):
            req = copy.deepcopy(self.request)
            req["candidates"][0][key] = value
            self.assert_rc(req, rc, reason)

    def test_replayed_frame_is_red(self):
        self.request["candidates"][1]["render"] = self.request["candidates"][0]["render"]
        self.assert_rc(self.request, 5, "replayed_render")

    def test_generation_source_or_seed_cannot_drift(self):
        c = self.request["candidates"][0]
        c["generation"] = self.write_json("bad-generation.json", {
            "rc": 0, "seed": 99, "input_sha256": c["reference_sha256"], "output_sha256": c["artifact"]["sha256"]})
        self.assert_rc(self.request, 4, "generation_stale")

    def test_capture_bytes_cannot_be_rebound_to_unrelated_mesh(self):
        c = self.request["candidates"][0]
        cap = json.loads((self.root / c["capture_receipt"]["path"]).read_text())
        cap["input_sha256"] = "f" * 64
        c["capture_receipt"] = self.write_json("bad-capture.json", cap)
        self.assert_rc(self.request, 4, "capture_receipt_stale")

    def test_faked_zero_check_gate_cannot_pass(self):
        c = self.request["candidates"][0]
        receipt = mesh_receipt(c['artifact']['sha256'])
        receipt['metrics']['component_inventories'] = []
        c["gate"] = self.write_json("no-checks.json", receipt)
        self.assert_rc(self.request, 3, "gate_has_no_measurements")

    def test_foreign_rc_one_is_error(self):
        c = self.request["candidates"][0]
        c["gate"] = self.write_json("rc-one.json", {"input_sha256": c["artifact"]["sha256"], "rc": 1})
        self.assert_rc(self.request, 2, "gate_rc_invalid")

    def test_missing_and_changed_model_weights(self):
        self.request["model"]["weights_sha256"] = "b" * 64
        self.assert_rc(self.request, 4, "dino_weights_stale")
        (self.model / "model.safetensors").unlink()
        self.assert_rc(self.request, 3, "dino_weights_missing")

    def test_path_escape_and_symlink_refused(self):
        c = self.request["candidates"][0]
        c["render"]["path"] = "../render.png"
        self.assert_rc(self.request, 2, "path_escape")
        (self.root / "alias.png").symlink_to(self.root / "render1.png")
        c["render"]["path"] = "alias.png"
        self.assert_rc(self.request, 2, "evidence_io_or_symlink")

    def test_invalid_policies_and_unpinned_revision(self):
        for key, value in (("iou_weight", 0), ("dino_weight", -1), ("min_iou_ppm", 1000001),
                           ("timeout_s", 0), ("min_dino_ppm", True), ("iou_weight", 1.5)):
            req = copy.deepcopy(self.request)
            req["policy"][key] = value
            self.assertNotEqual(self.evaluate(req)["rc"], 0)
        self.request["model"]["revision"] = "main"
        self.assert_rc(self.request, 2, "unpinned_revision")

    def test_diagnostic_mode_never_selects_winner(self):
        self.request["mode"] = "diagnostic"
        result = self.assert_rc(self.request, 3, "diagnostic_not_asset_admission")
        self.assertEqual(result["preferred_id"], "seed-1")

    def test_cli_refusal_preserves_existing_output_and_all_inputs(self):
        inp, output = self.root / "request.json", self.root / "result.json"
        inp.write_text(core.canonical_json(self.request))
        output.write_bytes(b"CALLER BYTES")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(rank, "run", return_value=rank._refusal(3, "dino_runtime_unavailable")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rank.main(["--request", str(inp), "--receipt", str(output)]), 3)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_cli_success_is_atomic_and_refuses_input_alias(self):
        inp, output = self.root / "request.json", self.root / "result.json"
        inp.write_text(core.canonical_json(self.request))
        result = self.evaluate()
        with patch.object(rank, "run", return_value=result), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rank.main(["--request", str(inp), "--receipt", str(output)]), 0)
            self.assertEqual(core.load_json(output, max_bytes=ev.MAX_JSON)["winner_id"], "seed-1")
            self.assertEqual(rank.main(["--request", str(inp), "--receipt", str(inp)]), 2)
        self.assertEqual(inp.read_text(), core.canonical_json(self.request))

    def test_hard_timeout_kills_owned_process_group(self):
        import subprocess
        child = unittest.mock.Mock()
        child.pid = 123456
        with patch.object(rank.af_proc, "popen", return_value=child), \
             patch.object(rank.af_proc, 'communicate', side_effect=[subprocess.TimeoutExpired('worker', 1), (b'', b'')]), \
             patch.object(rank.af_proc, "signal_group") as kill:
            result = rank.run(self.request)
        self.assertEqual(result["rc"], 6)
        kill.assert_called_once_with(child, rank.signal.SIGKILL)

    def test_real_supervised_child_missing_weight_preserves_null_scores(self):
        (self.model / "model.safetensors").unlink()
        result = rank.run(self.request)
        self.assertEqual(result["rc"], 3, result)
        self.assertIsNone(result["winner_id"])
        self.assertTrue(all(r["dino_ppm"] is None for r in result["rankings"]))

    def test_shortened_shape_refused_and_original_stays_green(self):
        req = copy.deepcopy(self.request)
        req["policy"]["min_iou_ppm"] = 900000  # Synthetic control floor only.
        req["candidates"] = req["candidates"][1:]
        self.assert_rc(req, 5, "no_candidate_clears_gates_and_floors")
        self.assertEqual(self.evaluate()["winner_id"], "seed-1")

    def test_finalize_mask_uses_explicit_alpha_boundary_and_preserves_render(self):
        directory = self.root / "scratch"
        directory.mkdir()
        image = Image.new("RGBA", (2, 2))
        image.putdata([(255, 255, 255, a) for a in (0, 127, 128, 255)])
        image.save(directory / "front.png")
        pin = core.sha256_file(directory / "front.png")
        raw = {"schema": "af.rank-capture-raw/v1", "rc": 0,
               "input_sha256": "a" * 64, "render_sha256": pin,
               "renderer_sha256": "b" * 64, "capture": {"width": 2, "height": 2}}
        (directory / "capture.json").write_text(core.canonical_json(raw))
        result = finalize(directory)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(list(Image.open(directory / "mask.png").getdata()), [0, 0, 255, 255])
        self.assertEqual(core.sha256_file(directory / "front.png"), pin)
        with self.assertRaises(metrics.RankError) as caught:
            finalize(directory)
        self.assertEqual(caught.exception.rc, 2)

    def test_recheck_detects_inputs_changed_during_inference(self):
        root = self.root
        class MutatingDino:
            identity = {}
            def __init__(self, directory):
                (root / "render2.png").write_bytes(b"changed during inference")
            def embed(self, image):
                return [1, 0]
        result = self.evaluate(loader=MutatingDino)
        self.assertEqual(result["rc"], 4)
        self.assertIsNone(result["winner_id"])


class MetricTests(unittest.TestCase):
    def test_analytic_iou_and_cosine(self):
        self.assertEqual(metrics.silhouette_iou_ppm(bytes([255, 255, 0]), bytes([0, 255, 255])), 333333)
        self.assertEqual(metrics.cosine_ppm([1, 0], [1, 0]), 1000000)
        self.assertEqual(metrics.cosine_ppm([1, 0], [0, 1]), 500000)
        self.assertEqual(metrics.cosine_ppm([1, 0], [-1, 0]), 0)

    def test_absent_and_invalid_masks_vectors(self):
        for func, args, rc in ((metrics.silhouette_iou_ppm, (b"", b""), 2),
                               (metrics.silhouette_iou_ppm, (b"\0", b"\0"), 3),
                               (metrics.silhouette_iou_ppm, (b"\x80", b"\xff"), 2),
                               (metrics.cosine_ppm, ([0], [1]), 3),
                               (metrics.cosine_ppm, ([math.nan], [1]), 2)):
            with self.assertRaises(metrics.RankError) as caught:
                func(*args)
            self.assertEqual(caught.exception.rc, rc)

    def test_offline_loader_refuses_missing_dependency_without_download(self):
        import builtins
        original = builtins.__import__
        def imports(name, *args, **kwargs):
            if name == "transformers":
                raise ImportError("synthetic absent runtime")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=imports), patch("socket.create_connection") as connect:
            with self.assertRaises(metrics.RankError) as caught:
                LocalDino("synthetic-missing-runtime")
            self.assertEqual(caught.exception.rc, 3)
            connect.assert_not_called()
        self.assertEqual(offline_environment()["CUDA_VISIBLE_DEVICES"], "")

    def test_ordinal_ties_use_seed_then_id(self):
        policy = {"iou_weight": 1, "dino_weight": 1, "min_iou_ppm": 1, "min_dino_ppm": 1}
        rows = [{"id": str(s), "seed": s, "iou_ppm": 500000, "dino_ppm": 500000, "gate_rc": 0} for s in (3, 1, 2)]
        self.assertEqual([r["seed"] for r in metrics.order(rows, policy)], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
