"""Bounded decision-envelope tests for optional licence declarations."""
from unittest import TestCase, mock

from asset_factory import core
from licence import check
import test_f0u2_licence as fixtures


class DeclarationRecords(TestCase):
    def setUp(self):
        fixture = fixtures.LicenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.factory, self.provider, self.policy = fixture.factory, fixture.provider, fixture.policy

    def test_omitted_declarations_emit_valid_null_metadata(self):
        factory = {k: v for k, v in self.factory.items()
                   if k not in ("jurisdiction", "commercial_use")}
        result = check.check_provider(factory, self.provider, operation="generate", policy=self.policy)
        self.assertEqual(result["rc"], 0, result)
        self.assertIsNone(result["jurisdiction"])
        self.assertIsNone(result["commercial_use"])
        self.assertEqual(result["configuration_sha256"], core.sha256_json(factory))
        core.validate(result, check.DECISION_SCHEMA)

    def test_null_metadata_can_be_recorded_on_factory_schema_refusal(self):
        factory = self.factory | {"commercial_use": None, "jurisdiction": None}
        result = check.check_provider(factory, self.provider, operation="generate", policy=self.policy)
        self.assertEqual((result["rc"], result["reason_codes"]), (2, ["config_invalid"]))
        self.assertIsNone(result["commercial_use"])
        self.assertIsNone(result["jurisdiction"])
        core.validate(result, check.DECISION_SCHEMA)

    def test_malformed_declarations_refuse_before_decision_construction(self):
        for key, bad in (("jurisdiction", 5), ("commercial_use", 1)):
            with self.subTest(key=key), mock.patch.object(check, "_shape", wraps=check._shape) as shape:
                factory = self.factory | {key: bad}
                with self.assertRaises(check._Refusal) as caught:
                    check.check_provider(factory, self.provider, operation="generate", policy=self.policy)
                self.assertEqual((caught.exception.rc, caught.exception.code), (2, "config_invalid"))
                self.assertFalse(any(call.args[1] is check.DECISION_SCHEMA
                                     for call in shape.call_args_list))
