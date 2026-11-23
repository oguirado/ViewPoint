"""Focused checks for WIRE-B draft candidates and matte admission."""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from asset_factory import core
from gates import input_d18
import test_d18_input_contract as input_contract
from tools.qualification.manifest import JSON_LIMIT, Pins, Refusal, check_candidate


ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("unirig", "mocapanything_v2")
RECEIPT_SHA = "a" * 64
CANDIDATE_SHA = "b" * 64


class CandidateTests(unittest.TestCase):
    def candidate(self, provider):
        return core.load_json(
            ROOT / "providers" / provider / "qualification" / "candidate.json",
            max_bytes=JSON_LIMIT,
        )

    def test_candidates_refuse_unproven_eu_rights(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.candidate(provider)
                with self.assertRaises(Refusal) as ctx:
                    check_candidate(candidate, Pins())
                self.assertEqual((ctx.exception.rc, str(ctx.exception)),
                                 (5, "commercial_eu_rights_unproven"))

    def test_candidate_rights_are_bound_to_the_lic_inventory_and_exact_subject(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.candidate(provider)
                pinned = {Path(item["path"]): item for item in candidate["code"]["files"]}
                # Candidate pins are absolute paths recorded at pin time; derive the
                # rights directory from the candidate itself, not from this checkout.
                rights_dir = Path(candidate["rights"]["path"]).parents[1] / "rights"
                inventory_pin = pinned[rights_dir / "inventory-rights.json"]
                inventory = core.load_json(Pins().check(inventory_pin), max_bytes=JSON_LIMIT)
                binding = core.load_json(Path(candidate["rights"]["path"]), max_bytes=JSON_LIMIT)
                self.assertIsNone(binding["commercial_use"])
                self.assertEqual(binding["verdict"], inventory["verdict"])
                self.assertEqual(binding["subject_sha256"], core.sha256_json(
                    {key: value for key, value in candidate.items() if key != "rights"}))

                candidate["rights"] = inventory_pin
                with self.assertRaises(Refusal) as ctx:
                    check_candidate(candidate, Pins())
                self.assertEqual((ctx.exception.rc, str(ctx.exception)), (4, "rights_subject_mismatch"))

    def test_config_pin_rejects_one_appended_byte(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                candidate = self.candidate(provider)
                config = next(pin for pin in candidate["runtime"]["files"]
                              if Path(pin["path"]).name == "recipe.json")
                source = Pins().check(config)
                copy = Path(directory) / source.name
                copy.write_bytes(source.read_bytes() + b"\n")
                with self.assertRaises(Refusal) as ctx:
                    Pins().check({**config, "path": str(copy)})
                self.assertEqual((ctx.exception.rc, str(ctx.exception)), (4, "pin_digest_mismatch"))


class MatteQualificationTests(unittest.TestCase):
    def setUp(self):
        fixture = input_contract.Mattes()
        fixture.setUp()
        pixels = [channel for index, alpha in enumerate(fixture.alpha)
                  for channel in (*fixture.rgb[index * 3:index * 3 + 3], alpha)]
        self.original = fixture.original
        self.master = input_contract.png("RGBA", fixture.size, pixels)
        self.submitted = fixture.payload()
        self.mask = fixture.reference
        self.witnesses = {
            "intent_digest": fixture.store.add("matte-intent", fixture.intent),
            "read_checked": fixture.store.read,
        }

    def inspect(self, **kwargs):
        return input_d18.inspect_matte(self.original, self.master, self.submitted, self.mask,
                                       **self.witnesses, **kwargs)

    def prepare(self, **kwargs):
        return input_d18.prepare_matte(self.original, self.master, self.mask,
                                       **self.witnesses, **kwargs)

    def proof(self):
        return {"evidence_dir": Path("/unused-fixture-evidence"), "receipt_sha256": RECEIPT_SHA,
                "candidate_sha256": CANDIDATE_SHA}

    def verified_receipt(self):
        outputs = {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                   for name, raw in (("original.png", self.original), ("straight.png", self.master))}
        return {"provider": "birefnet", "rc": 0, "candidate_sha256": CANDIDATE_SHA,
                "runs": [{"role": "denied-fetch"}, {"role": "candidate", "outputs": outputs}]}

    def test_fixture_still_uses_existing_matte_checks(self):
        self.assertEqual(self.inspect()["source_rc"], 0)
        self.assertEqual(self.prepare()[1], self.inspect())

    def test_invalid_matte_source_and_unexpected_proof_refused_on_both_paths(self):
        cases = (
            ({"matte_source": "rembg"}, "invalid_matte_source"),
            ({"matte_source": "authored", "birefnet_qualification": self.proof()},
             "unexpected_birefnet_qualification"),
        )
        for options, reason in cases:
            with self.subTest(source=options["matte_source"]):
                with patch.object(input_d18, "verify_receipt") as verifier:
                    inspected = self.inspect(**options)
                    generated, prepared = self.prepare(**options)
                self.assertIsNone(generated)
                verifier.assert_not_called()
                for receipt in (inspected, prepared):
                    self.assertEqual((receipt["source_rc"], receipt["reason_codes"]),
                                     (2, [reason]))

    def test_model_matte_without_qualification_is_refused_on_both_paths(self):
        inspected = self.inspect(matte_source="birefnet")
        generated, prepared = self.prepare(matte_source="birefnet")
        self.assertIsNone(generated)
        for native in (inspected, prepared):
            self.assertEqual(native["source_rc"], 5)
            self.assertEqual(native["reason_codes"], ["birefnet_qualification_required"])
            self.assertEqual(input_d18.qualify_input(native)["rc"], 5)

    def test_verified_receipt_must_match_the_trusted_candidate_and_exact_matte(self):
        options = {"matte_source": "birefnet", "birefnet_qualification": self.proof()}
        good = self.verified_receipt()
        with patch.object(input_d18, "verify_receipt", return_value=good) as verifier:
            native = self.inspect(**options)
            self.assertEqual(native["source_rc"], 0, native)
            self.assertEqual(native["birefnet_qualification"],
                             {"receipt_sha256": RECEIPT_SHA, "candidate_sha256": CANDIDATE_SHA})
            verifier.assert_called_once_with(self.proof()["evidence_dir"], expected_sha256=RECEIPT_SHA)
            self.assertEqual(self.prepare(**options)[1], native)
        for key in ("candidate_sha256", "provider", "original.png", "straight.png"):
            with self.subTest(key=key):
                bad = self.verified_receipt()
                if key.endswith(".png"):
                    bad["runs"][1]["outputs"][key]["sha256"] = "c" * 64
                else:
                    bad[key] = "c" * 64
                with patch.object(input_d18, "verify_receipt", return_value=bad):
                    self.assertEqual(self.inspect(**options)["source_rc"], 4)

    def test_model_receipt_without_qual_pass_is_refused(self):
        with patch.object(input_d18, "verify_receipt",
                          side_effect=Refusal(4, "qualification_pass_unproven")):
            self.assertEqual(self.inspect(matte_source="birefnet",
                                          birefnet_qualification=self.proof())["source_rc"], 5)

    def test_nonzero_verifier_result_is_refused_on_both_paths(self):
        # The real verifier raises on nonzero rc. Exercise INPUT's own guard
        # with a stub that returns a nonzero result and otherwise matching pins.
        options = {"matte_source": "birefnet", "birefnet_qualification": self.proof()}
        with patch.object(input_d18, "verify_receipt",
                          return_value=self.verified_receipt() | {"rc": 5}):
            inspected = self.inspect(**options)
            generated, prepared = self.prepare(**options)
        self.assertIsNone(generated)
        for receipt in (inspected, prepared):
            self.assertEqual((receipt["source_rc"], receipt["reason_codes"]),
                             (5, ["birefnet_qualification_required"]))


if __name__ == "__main__":
    unittest.main()
