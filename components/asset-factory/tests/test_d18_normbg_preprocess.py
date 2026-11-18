"""CPU-only controls for the admitted matte handoff."""
from contextlib import contextmanager
import builtins
import importlib
import io
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from gates.input_d18 import inspect_matte
import test_d18_input_contract as input_fixtures
from tools.input.evidence import InputIssue, METRIC_RULES, bytes_digest
from tools.qualification.manifest import JSON_LIMIT, Pins, Refusal, check_candidate


PROVIDERS = ("partcrafter", "triposg")
ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOTS = {
    "partcrafter": Path("/home/admin/AIModelos_hf/PartCrafter"),
    "triposg": Path("/home/admin/AIModelos_hf/TripoSG"),
}
PIPELINE_CONFIGS = ("model_index.json", "transformer/config.json", "vae/config.json",
                    "scheduler/scheduler_config.json")
# PRE_INVENTORY records the RMBG-1.4 weights as AUSENTE.
RMBG_WEIGHT_DIGESTS = frozenset()
png = input_fixtures.png


def fixture():
    matte = input_fixtures.Mattes()
    matte.setUp()
    matte.intent["regions"]["fine_structure"] = [[3, 2, 4, 3]]
    matte.intent["fine_structure_alpha"] = "fractional"
    master = png("RGBA", matte.size, [v for i, a in enumerate(matte.alpha)
                                     for v in (*matte.rgb[i*3:i*3+3], a)])
    submitted = matte.payload()
    kwargs = dict(original=matte.original, straight_alpha=master, submitted=submitted,
                  approved_mask=matte.reference, read_checked=matte.store.read,
                  intent_digest=matte.store.add("matte-intent", matte.intent))
    native = inspect_matte(**kwargs)
    store = matte.store
    gate_id = native["gate_id"]
    subject = native["subject_digest"]
    bindings = {key: bytes_digest(key.encode()) for key in
                ("policy_digest", "evaluator_digest", "toolchain_digest")}
    bindings["domain_digest"] = native["domain_digest"]
    scope = {key: native[key] for key in ("gate_id", "input_refs", "reference_refs")}
    control = store.add("negative-control", {**bindings, "gate_id": gate_id,
                        "expected_rc": 5, "observed_rc": 5, "witness_digest": bytes_digest(b"fixture")})
    profile = {"schema": "af.acceptance-profile/v1", **bindings, **scope, "operation": "input",
               "role": "synthetic-character", "negative_control_receipt_refs": [control], "criteria": []}
    observations = []
    limits = {"alpha_iou": 1_000_000, "boundary_iou": 1_000_000, "alpha_outside_pixels": 0,
              "margin": 200_000, "foreground": 200_000, "border_pixels": 0}
    for metric, (unit, scale, operator, basis) in METRIC_RULES[gate_id].items():
        method = bytes_digest(metric.encode())
        rule = dict(id=metric, unit=unit, scale=scale, operator=operator, limit=limits[metric],
                    method_digest=method, uncertainty_kind="exact", basis=basis, min_confidence_ppm=None)
        role, witness_role = (("proof", "derivation") if basis == "DERIVED" else
                              ("calibration", "held-out-calibration"))
        witness = store.add(witness_role, {"fixture": metric})
        reviewed = store.add(role, {**bindings, **scope, **rule, "witness_digest": witness,
                                    "operation": profile["operation"], "role": profile["role"]})
        criterion = dict(rule, target_reason=None, proof_receipt_digest=None,
                         calibration_receipt_digest=None)
        criterion[role + "_receipt_digest"] = reviewed
        profile["criteria"].append(criterion)
        lower, upper = native["metrics"][metric]
        observations.append(dict(id=metric, unit=unit, method_digest=method, lower=lower,
                                 upper=upper, confidence_ppm=None))
    profile_pin = store.add("profile", profile)
    for obs in observations:
        obs["certificate_ref"] = store.add("certificate", {
            **obs, "subject_digest": subject, "profile_digest": profile_pin, "gate_id": gate_id,
            "domain_digest": native["domain_digest"], "witness_digest": bytes_digest(b"fixture"),
        })
    stage = {"schema": "af.math-evidence/v1", **bindings, **scope, "subject_digest": subject,
             "profile_digest": profile_pin, "negative_control_receipt_refs": [control], "source_rc": 0,
             "wall_ns": 100, "peak_bytes": 10, "work_counts": {"pixels": 64}, "observations": observations}
    kwargs.update(profile_digest=profile_pin, receipt_digest=store.add("stage", stage),
                  submitted_digest=subject)
    return matte, kwargs


@contextmanager
def deny_stock_access():
    blocked = ("rmbg", "rembg", "bria", "transformers", "huggingface_hub", "torch", "diffusers",
               "image_process", "image_utils")

    def guard_import(original_import):
        def guarded_import(name, *args, **kwargs):
            if any(token in name.lower() for token in blocked):
                raise AssertionError(f"model or stock preprocessing import: {name}")
            return original_import(name, *args, **kwargs)
        return guarded_import

    def guard_open(original_open):
        def guarded_open(path, *args, **kwargs):
            if not isinstance(path, int):
                name = os.fsdecode(path).lower()
                if any(token in name for token in (*blocked, "aimodelos_hf", "pretrained_weights")):
                    raise AssertionError(f"preprocessor tried opening a stock or model path: {path}")
            return original_open(path, *args, **kwargs)
        return guarded_open

    with (patch("builtins.__import__", guard_import(builtins.__import__)),
          patch.object(importlib, "import_module", guard_import(importlib.import_module)),
          patch("builtins.open", guard_open(builtins.open)),
          patch("io.open", guard_open(io.open)), patch("os.open", guard_open(os.open)),
          patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")),
          patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network attempted"))):
        yield


def adapter(provider):
    return importlib.import_module(f"providers.{provider}.qualification.adapter")


def assert_no_remover_modules():
    loaded = sorted(name for name in sys.modules
                    if any(part.startswith(("rmbg", "rembg", "bria")) for part in name.casefold().split(".")))
    if loaded:
        raise AssertionError(f"stock remover loaded: {loaded}")


def candidate_manifest(provider):
    return core.load_json(ROOT / "providers" / provider / "qualification" / "candidate.json",
                          max_bytes=JSON_LIMIT)


def assert_no_stock_pins(candidate, excluded_digests=RMBG_WEIGHT_DIGESTS):
    def visit(value):
        if isinstance(value, dict):
            if {"path", "sha256"} <= value.keys():
                path, digest = value["path"], value["sha256"]
                for name in (path, str(Path(path).resolve())):
                    if any(token in name.casefold() for token in ("rmbg", "bria")):
                        raise AssertionError(f"stock remover path pinned: {name}")
                if digest in excluded_digests:
                    raise AssertionError(f"stock remover digest pinned: {digest}")
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for section in ("code", "runtime", "weights", "conditioners", "dependencies"):
        visit(candidate[section])


class OfflinePreprocessing(unittest.TestCase):
    def setUp(self):
        self.matte, self.args = fixture()

    def test_fractional_alpha_with_stock_unavailable(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider), deny_stock_access():
                image = adapter(provider).preprocess(**self.args)
                self.assertEqual((image.mode, image.size), ("RGB", (2, 2)))
                self.assertEqual(image.tobytes(),
                                 bytes([200, 160, 120, 227, 207, 187, 200, 160, 120, 255, 255, 255]))
                assert_no_remover_modules()

    def test_implicit_fallback_controls(self):
        with deny_stock_access():
            for provider in PROVIDERS:
                with self.subTest(provider=provider):
                    with self.assertRaises(InputIssue) as error:
                        adapter(provider).preprocess(**dict(self.args, submitted=None))
                    self.assertEqual((error.exception.rc, error.exception.code),
                                     (3, "submitted_rgb_bytes_missing"))
                    assert_no_remover_modules()
                    with self.assertRaises(InputIssue) as error:
                        adapter(provider).preprocess(**dict(self.args, submitted=Path("stock-matte.png")))
                    self.assertEqual((error.exception.rc, error.exception.code),
                                     (2, "invalid_submitted_rgb_bytes"))
                    assert_no_remover_modules()

    def test_binary_master_still_refused_with_submitted_digest_recomputed(self):
        self.matte.alpha[2 * 8 + 3] = 255
        master = png("RGBA", self.matte.size, [v for i, a in enumerate(self.matte.alpha)
                                              for v in (*self.matte.rgb[i*3:i*3+3], a)])
        submitted = self.matte.payload()
        args = self.args | {"straight_alpha": master, "submitted": submitted,
                            "submitted_digest": bytes_digest(submitted)}
        for provider in PROVIDERS:
            with self.subTest(provider=provider), self.assertRaises(InputIssue) as error:
                adapter(provider).preprocess(**args)
            self.assertEqual((error.exception.rc, error.exception.code),
                             (5, "protected_fine_structure_alpha_binarized"))

    def test_substituted_matte_cannot_reuse_admission(self):
        self.matte.alpha[4 * 8 + 4] = 254  # Outside the submitted crop, unchanged submitted RGB.
        master = png("RGBA", self.matte.size, [v for i, a in enumerate(self.matte.alpha)
                                              for v in (*self.matte.rgb[i*3:i*3+3], a)])
        for provider in PROVIDERS:
            with self.subTest(provider=provider), self.assertRaises(InputIssue) as error:
                adapter(provider).preprocess(**dict(self.args, straight_alpha=master))
            self.assertEqual((error.exception.rc, error.exception.code),
                             (4, "input_profile_reference_drift"))

    def test_double_premultiplication_fails_even_with_rehashed_derivative(self):
        twice = self.matte.payload(passes=2)
        for provider in PROVIDERS:
            with self.subTest(provider=provider), self.assertRaises(InputIssue) as error:
                adapter(provider).preprocess(**dict(self.args, submitted=twice,
                                                    submitted_digest=bytes_digest(twice)))
            self.assertEqual((error.exception.rc, error.exception.code),
                             (5, "submitted_derivative_mismatch"))

    def test_missing_base_admission_and_substituted_submitted_bytes(self):
        for provider in PROVIDERS:
            preprocess = adapter(provider).preprocess
            for changed, expected in [
                (dict(profile_digest=None, receipt_digest=None), (3, "base_qualification_missing")),
                (dict(submitted_digest=bytes_digest(b"foreign derivative")), (4, "submitted_pin_drift")),
            ]:
                with self.subTest(provider=provider, expected=expected), self.assertRaises(InputIssue) as error:
                    preprocess(**(self.args | changed))
                self.assertEqual((error.exception.rc, error.exception.code), expected)


class CandidatePins(unittest.TestCase):
    def test_exact_pipeline_config_pins_and_edited_config_refusal(self):
        for provider, root in MODEL_ROOTS.items():
            weights = candidate_manifest(provider)["weights"]
            for relative in PIPELINE_CONFIGS:
                with self.subTest(provider=provider, config=relative), TemporaryDirectory() as directory:
                    path = root / relative
                    descriptors = [pin for pin in weights if pin["path"] == str(path)]
                    self.assertEqual(len(descriptors), 1, path)
                    pin, = descriptors
                    Pins().check(pin)
                    config = Path(directory) / path.name
                    config.write_bytes(path.read_bytes())
                    fixture_pin = pin | {"path": str(config)}
                    Pins().check(fixture_pin)
                    config.write_bytes(config.read_bytes() + b"\n")
                    with self.assertRaises(Refusal) as error:
                        Pins().check(fixture_pin)
                    self.assertEqual((error.exception.rc, error.exception.code), (4, "pin_digest_mismatch"))

    def test_checked_in_candidates_exclude_stock_remover_pins(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                assert_no_stock_pins(candidate_manifest(provider))

    def test_stock_remover_path_or_denylisted_digest_refused(self):
        fixture_digest = bytes_digest(b"synthetic prohibited checkpoint")
        for provider in PROVIDERS:
            candidate = candidate_manifest(provider)
            for path, digest, reason in (
                ("/models/RmBg-1.4/model.safetensors", bytes_digest(b"weight"), "path"),
                ("/models/BRIA/model.safetensors", bytes_digest(b"weight"), "path"),
                ("/models/renamed.safetensors", fixture_digest, "digest"),
            ):
                with self.subTest(provider=provider, path=path):
                    mutant = candidate | {"weights": [*candidate["weights"], {"path": path, "sha256": digest}]}
                    with self.assertRaisesRegex(AssertionError, f"stock remover {reason} pinned"):
                        assert_no_stock_pins(mutant, {fixture_digest})

    def test_exact_conditioners_and_current_rights_refusal(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                directory = ROOT / "providers" / provider / "qualification"
                candidate = candidate_manifest(provider)
                policy = core.load_json(directory / "preprocessing.json", max_bytes=JSON_LIMIT)
                self.assertFalse(policy["stock_rmbg_present"])
                pins = Pins()
                pins.check_list(candidate["code"]["files"], required=True)
                encoder = candidate["conditioners"][0]["files"]
                self.assertEqual({Path(pin["path"]).name for pin in encoder},
                                 {"model.safetensors", "config.json", "preprocessor_config.json"})
                # Large weights are rehashed in the manifest evidence check.
                pins.check_list([pin for pin in encoder if pin["path"].endswith(".json")], required=True)
                rights = core.load_json(Path(candidate["rights"]["path"]), max_bytes=JSON_LIMIT)
                self.assertEqual(rights["verdict"], "CANNOT_SEE")
                self.assertIsNone(rights["commercial_use"])
                for subject, reason in [
                    (candidate, "commercial_eu_rights_unproven"),
                    (candidate | {"conditioners": []}, "rights_subject_mismatch"),
                    (candidate | {"background_remover": "pretrained_weights/RMBG-1.4"},
                     "candidate_identity_required"),
                ]:
                    with self.assertRaises(Refusal) as error:
                        check_candidate(subject, Pins())
                    self.assertEqual(error.exception.code, reason)
