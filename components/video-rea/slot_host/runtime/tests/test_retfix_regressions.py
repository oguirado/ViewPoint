# SPDX-License-Identifier: AGPL-3.0-only
"""Contrasts for pinned imports and the parent-compatible retention test harness."""

import os
from contextlib import chdir
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from slot_host.runtime import tools
from slot_host.runtime.tests import test_retention_flow as flow
from slot_host.runtime.tests import test_retention_integration as integration
from slot_host.runtime.tests import test_retention_service_e2e as e2e


class RetfixHarnessTests(unittest.TestCase):
    def test_flow_harness_imports_and_builds_clients_without_retention_dataclasses(self):
        with patch.object(integration, "RetentionClients", SimpleNamespace), patch.object(
            integration, "RetentionIdentity", SimpleNamespace,
        ):
            try:
                namespace = runpy.run_path(flow.__file__)
                clients = namespace["Backend"]().live_clients(Path("/unused"), None)
            except TypeError as exc:
                self.fail(f"the flow harness never reached its assertions on the old runtime: {exc}")
        self.assertIsInstance(clients, SimpleNamespace)
        self.assertEqual(clients.runner.principal, "fixture/runner")
        self.assertIsNot(clients.inspector.client, clients.authority.client)
        self.assertFalse(namespace["_SUPPORTS_FLOW"])

    def test_e2e_harness_with_legacy_clients_reaches_server_setup(self):
        class SetupReached(Exception):
            pass

        def check_approval(destination, config):
            self.assertEqual(destination, "disposable approval")
            self.assertEqual(config, {})
            raise SetupReached()

        with tempfile.TemporaryDirectory() as directory:
            case = e2e.RetentionServiceTests()
            case.fixture(Path(directory))
            case.service = SimpleNamespace(
                TENANT="fixture/tenant", OTHER_TENANT="fixture/other", OTHER_AUTHORITY="fixture/other-authority",
                DENIAL="fixture/denial", ROLES={role: (role,) for role in ("authority", "runner", "inspector")},
            )
            case.wire = SimpleNamespace(admission_bytes=lambda *_: b"")
            case.server_fixture = SimpleNamespace(
                config={"policy_revision": "fixture/policy", "acl_revision": "fixture/acl"},
                client=lambda transport, role: (transport, role), approval={},
                approval_file="disposable approval", write=check_approval,
            )
            with patch.object(e2e, "RetentionClients", SimpleNamespace), patch.object(
                e2e, "RetentionIdentity", SimpleNamespace,
            ):
                try:
                    with self.assertRaises(SetupReached):
                        case.exercise_retained_flow("unix")
                except TypeError as exc:
                    self.fail(f"retained end-to-end setup crashed with legacy clients: {exc}")

    def test_measurement_fixture_passes_attempt_identity_only_out_of_band(self):
        from slot_host.measure import harness

        manifest = Path(harness.__file__).with_name("fixtures") / "manifest.json"
        dispatch = tools.dispatch

        def inspect_payload(tool, arguments, **options):
            self.assertNotIn("attempt_id", arguments["payload"])
            self.assertIsNotNone(options["live_authority"])
            return dispatch(tool, arguments, **options)

        with patch.object(tools, "dispatch", side_effect=inspect_payload) as observed:
            report = harness.run(manifest, "behaviour.record", "nominal", ("router",))
        observed.assert_called_once()
        self.assertTrue(report["ok"])

    def test_witness_parent_import_does_not_write_bytecode_to_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / "retention_service"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "store.py").write_text(
                "def PinnedProfiles(schemas, verify):\n    return schemas, verify.__name__\n"
            )
            raw = """
import sys
from slot_host.runtime.retention_witness import pinned_profiles
assert sys.dont_write_bytecode is False
assert pinned_profiles({}) == ({}, "verify_profile_binding")
assert sys.dont_write_bytecode is False
"""
            with chdir(directory):
                env = os.environ | {
                    "REA_RETENTION_SRC": directory,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                }
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            completed = subprocess.run([sys.executable, "-c", raw], env=env, capture_output=True,
                                       cwd=directory, timeout=10, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(sorted(path.name for path in package.iterdir()), ["__init__.py", "store.py"])

    def test_e2e_loader_disables_bytecode_only_while_importing_tools(self):
        class StopAfterImport(Exception):
            pass

        class LoaderFixture(e2e.RetentionServiceTests):
            pass

        def import_tools(name):
            self.assertEqual(name, "retention_service.server.test_e2e")
            self.assertIs(sys.dont_write_bytecode, True)
            raise StopAfterImport()

        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory, "retention_service", "server", "__main__.py")
            module.parent.mkdir(parents=True)
            module.touch()
            with patch.dict(os.environ, {"REA_RETENTION_SRC": directory}), patch.object(
                sys, "dont_write_bytecode", False,
            ), patch.object(e2e.importlib, "import_module", side_effect=import_tools):
                try:
                    with self.assertRaises(StopAfterImport):
                        LoaderFixture.setUpClass()
                    self.assertIs(sys.dont_write_bytecode, False)
                finally:
                    LoaderFixture.doClassCleanups()
