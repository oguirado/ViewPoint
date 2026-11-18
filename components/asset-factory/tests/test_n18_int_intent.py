"""Production matte approval at the native CLI and input-cache boundaries."""

from pathlib import Path
import os
import unittest
from unittest.mock import patch

from gates import input_d18
from tools.input import matte as raster_matte
from tools.qualification import verify_receipt
from tools.pipeline import engine, stages
import test_n18_sat_replay as replay
from test_n18_sat_replay import sha, stage_record, write
import test_n18_wire_a3 as wire


def approval_store(root, intent, pin, raw, **pair):
    root.mkdir(parents=True, exist_ok=True)
    witness = root / "matte-intent" / pin[7:]
    witness.parent.mkdir(exist_ok=True)
    if raw is not None:
        witness.write_bytes(raw)
    store = write(root / "approvals.json", {
        "schema": "af.matte-approval-store/v1",
        "approvals": [{
            "original_digest": intent["original_digest"],
            "selection_digest": intent["selection_digest"],
            "intent_digest": pin,
            **pair,
        }],
    })
    return {"path": str(store), "sha256": sha(store)}


class IntentApprovalTests(unittest.TestCase):
    def setUp(self):
        wire.WireA3.setUp(self)
        self.approved = self.witnesses["intent_digest"]
        self.approval_root = self.root / "operator"
        self.config = self.root / "factory.json"
        self.bind_approval()

    def bind_approval(self, **pair):
        self.store = approval_store(
            self.approval_root, self.fixture.intent, self.approved,
            self.fixture.store.read("matte-intent", self.approved), **pair,
        )
        write(self.config, {"schema": "af.factory-config/v1", "matte_approval_store": self.store})
        self.config_pin = sha(self.config)

    def run_intake(self, *, pin=True, receipt=None, config=None, matte_source="authored"):
        write(self.matte_path, dict(self.matte if receipt is None else receipt, matte_source=matte_source))
        with patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=self.config_pin if pin else ""):
            result, _ = replay.Replay().invoke_pipeline(
                "image", self.image, self.original, self.out, "--matte-receipt", self.matte_path,
                "--factory-config", config or self.config,
            )
        row = stage_record(result, "input")
        return row, engine.load(row["receipt"]["path"])

    def other_intent_receipt(self):
        intent = dict(self.fixture.intent, max_pixels=self.fixture.intent["max_pixels"] + 1)
        pin = self.fixture.store.add("matte-intent", intent)
        (self.root / "matte-intent" / pin[7:]).write_bytes(self.fixture.store.read("matte-intent", pin))
        return raster_matte.inspect_matte(
            self.original.read_bytes(), self.master, self.image.read_bytes(), self.mask,
            intent_digest=pin, read_checked=self.fixture.store.read,
        )

    def assert_refusal(self, rc, reason, **invocation):
        row, record = self.run_intake(**invocation)
        self.assertEqual((row["rc"], record["reason_codes"], row["cache_hit"]), (rc, [reason], False))

    def test_replacement_intent_cold_refused_before_witness_reads(self):
        receipt = self.matte
        self.set_model_intent()
        self.approved = self.witnesses["intent_digest"]
        self.bind_approval()
        read = stages.read_bytes
        paths = []

        def track(path):
            paths.append(Path(path))
            return read(path)

        with patch.object(stages, "read_bytes", side_effect=track):
            self.assert_refusal(4, "matte_intent_not_approved", receipt=receipt)
        self.assertFalse(any(path.parent.name in {"matte-intent", "straight-alpha", "approved-mask",
                                                  "original-rgb", "input-selection"} for path in paths))

    def test_replacement_intent_after_cache_pass_refused(self):
        row, _ = self.run_intake()
        self.assertEqual((row["rc"], row["cache_hit"]), (0, False))
        row, record = self.run_intake()
        self.assertEqual((row["rc"], row["cache_hit"]), (0, True))
        self.assertIn(self.store, record["inputs"])
        self.assert_refusal(4, "matte_intent_not_approved", receipt=self.other_intent_receipt())

    set_model_intent = wire.WireA3.set_model_intent

    def test_authored_replacement_refused_after_approved_model_cache_hit(self):
        authored = self.matte
        # Present witness bytes alone should never replace the approved digest.
        unapproved = self.approval_root / "matte-intent" / self.approved[7:]
        self.assertTrue(unapproved.is_file())
        verified = wire.WireA3.prepare_model_receipt(self)
        self.approved = self.witnesses["intent_digest"]
        self.bind_approval()
        with patch.object(input_d18, "verify_receipt", return_value=verified):
            row, record = self.run_intake(matte_source="birefnet")
            self.assertEqual((row["rc"], row["cache_hit"]), (0, False), record["reason_codes"])
        with patch("tools.qualification.verify_receipt", spec=verify_receipt, return_value=verified):
            row, _ = self.run_intake(matte_source="birefnet")
            self.assertEqual((row["rc"], row["cache_hit"]), (0, True))
        self.assert_refusal(4, "matte_intent_not_approved", receipt=authored)

    def test_approval_store_requires_absolute_canonical_path(self):
        path = Path(self.store["path"])
        alias = self.root / "operator-link"
        alias.symlink_to(path.parent, target_is_directory=True)
        for supplied, reason in (
            (str(path.relative_to(path.anchor)), "relative_reference"),
            (str(path.parent) + "/./" + path.name, "noncanonical_reference"),
            (str(path.parent / ".." / path.parent.name / path.name), "noncanonical_reference"),
            (str(alias / path.name), "noncanonical_reference"),
        ):
            with self.subTest(path=supplied):
                config = {"schema": "af.factory-config/v1",
                          "matte_approval_store": dict(self.store, path=supplied)}
                write(self.config, config)
                self.config_pin = sha(self.config)
                self.assert_refusal(2, reason)

    def test_approval_store_is_opened_once_per_lookup(self):
        opts = {"factory_config": self.config}
        native = stages.NativeStages(source=self.image, reference=self.original,
                                    provider="trellis2", options=opts)
        with (patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=self.config_pin),
              patch.object(engine, "read_bytes", wraps=engine.read_bytes) as reads):
            refs, _ = native._approved_matte_intent(dict(self.matte, matte_source="authored"))
        self.assertEqual(refs[1], self.store)
        self.assertEqual(sum(Path(call.args[0]) == Path(self.store["path"])
                             for call in reads.call_args_list), 1)

    def test_approval_store_authenticates_parsed_bytes(self):
        opts = {"factory_config": self.config}
        native = stages.NativeStages(source=self.image, reference=self.original,
                                    provider="trellis2", options=opts)
        path = Path(self.store["path"])
        path.write_bytes(path.read_bytes() + b"\n")
        with (patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=self.config_pin),
              self.assertRaises(engine.Refusal) as caught):
            native._approved_matte_intent(dict(self.matte, matte_source="authored"))
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "bytes_stale"))

    def test_approval_store_sensitive_path_refuses_before_read(self):
        for basename in ("auth.json", ".netrc", ".npmrc"):
            with self.subTest(basename=basename):
                config = {"schema": "af.factory-config/v1",
                          "matte_approval_store": dict(self.store, path=str(self.root / basename))}
                write(self.config, config)
                self.config_pin = sha(self.config)
                with patch.object(engine, "read_bytes", wraps=engine.read_bytes) as reads:
                    self.assert_refusal(2, "sensitive_path_refused")
                self.assertFalse(any(Path(call.args[0]).name == basename for call in reads.call_args_list))

    def test_non_matte_input_policy_refuses_before_authority_lookup(self):
        write(self.matte_path, dict(self.matte, gate_id="input.reference", source_rc=0))
        with (patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=""),
              patch.object(stages.NativeStages, "_approved_matte_intent", autospec=True,
                           side_effect=engine.Refusal(3, "matte_approval_authority_missing")) as lookup):
            result, _ = replay.Replay().invoke_pipeline(
                "image", self.image, self.original, self.out, "--input-policy", self.matte_path,
            )
        row = stage_record(result, "input")
        record = engine.load(row["receipt"]["path"])
        self.assertEqual((row["rc"], record["reason_codes"]), (2, ["matte_receipt_invalid"]))
        lookup.assert_not_called()

    def test_missing_authority_refused(self):
        self.assert_refusal(3, "matte_approval_authority_missing", pin=False)
        write(self.config, {"schema": "af.factory-config/v1"})
        self.config_pin = sha(self.config)
        self.assert_refusal(3, "matte_approval_authority_missing")

    def test_approval_for_different_image_or_selection_refused(self):
        for field in ("original_digest", "selection_digest"):
            with self.subTest(field=field):
                self.bind_approval(**{field: "sha256:" + "f" * 64})
                self.assert_refusal(4, "matte_approval_pair_mismatch")

    def test_intent_comes_from_authority_and_is_reauthenticated_on_reuse(self):
        (self.root / "matte-intent" / self.approved[7:]).unlink()
        row, _ = self.run_intake()
        self.assertEqual(row["rc"], 0)
        row, _ = self.run_intake()
        self.assertTrue(row["cache_hit"])
        intent = self.approval_root / "matte-intent" / self.approved[7:]
        intent.write_bytes(intent.read_bytes() + b"\n")
        self.assert_refusal(4, "matte_intent_stale")

    def test_store_and_config_cannot_drift(self):
        self.assertEqual(self.run_intake()[0]["rc"], 0)
        store = Path(self.store["path"])
        store.write_bytes(store.read_bytes() + b"\n")
        self.assert_refusal(4, "bytes_stale")
        supplied = write(self.root / "caller-factory.json", {"schema": "af.factory-config/v1"})
        self.assert_refusal(4, "matte_factory_config_stale", config=supplied)
