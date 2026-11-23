"""Integration obligations for provider metadata under the expanded slot registry."""
import copy
from pathlib import Path
import unittest

from asset_factory import contracts, core


ROOT = Path(__file__).resolve().parents[1]


class IntegratedProviders(unittest.TestCase):
    def manifests(self):
        paths = set((ROOT / "providers").glob("*/provider.json"))
        paths.update((ROOT / "tools").rglob("*provider.json"))
        self.assertTrue(paths, "provider inventory cannot be empty")
        return [(path, core.load_json(path, max_bytes=100000)) for path in sorted(paths)]

    def test_all_integrated_manifests_validate_with_shared_contracts(self):
        for path, provider in self.manifests():
            with self.subTest(path=path.relative_to(ROOT)):
                contracts.validate_provider(provider)

    def test_each_manifest_retains_unknown_slot_and_revision_refusals(self):
        for path, provider in self.manifests():
            for field, value, code in (("slot", "unregistered_integration_slot", "schema_mismatch"),
                                       ("revision", "main", "unpinned_revision")):
                with self.subTest(path=path.relative_to(ROOT), field=field):
                    bad = copy.deepcopy(provider)
                    bad[field] = value
                    with self.assertRaises(core.ContractError) as caught:
                        contracts.validate_provider(bad)
                    self.assertEqual(caught.exception.rc, 2)
                    self.assertEqual(caught.exception.code, code)
