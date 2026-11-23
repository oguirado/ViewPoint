"""D1 file-fallback, compatibility and nonblocking-probe tests."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from jsonschema import ValidationError


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from abi import BlockRuntimeAdapter, DataResult, RuntimeBinding
from locator import ArtifactLocator


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        content = b"synthetic artifact data"
        (root / "fixture.bin").write_bytes(content)
        self.locator = ArtifactLocator({"test": root})
        self.ref = {
            "schema": "vp.artifact_ref/v1",
            "locator": "af://test/fixture.bin",
            "sha256": sha256(content).hexdigest(),
            "bytes": len(content),
            "media_type": "application/octet-stream",
            "stage": "fixture",
            "copy": "none",
        }
        self.lease = {
            "schema": "vp.model_lease/v1",
            "model_sha256": self.ref["sha256"],
            "holder": "fixture-holder",
            "device": "cpu",
            "state": "held",
            "evict_policy": "explicit",
            "expires_on": "release",
        }
        self.file_calls = []

    def file_dispatch(self, args, *, dry_run):
        op = args["op"]
        self.file_calls.append((op, dry_run))
        if op in ("put", "resolve"):
            return self.locator.handle(args, dry_run=dry_run)
        # D4 supplies the lease implementation. These fixtures exercise only D1 routing.
        if op == "status":
            return {"copy": "none", "leases": []}
        return dict(self.lease, state="held" if op == "lease_acquire" else "released")

    def assert_file_fallback(self, adapter_type):
        adapter = adapter_type(self.file_dispatch)
        self.assertFalse(adapter.start_probe(lambda: None, expected_version="fixture-v1").result(5))
        artifact_result = dict(self.ref, copy="file")
        lease_args = {name: self.lease[name] for name in ("model_sha256", "holder", "device")}
        requests = (
            ({"op": "put", "artifact_ref": self.ref}, artifact_result, False),
            ({"op": "resolve", "locator": self.ref["locator"], "sha256": self.ref["sha256"]}, artifact_result, True),
            (dict(lease_args, op="lease_acquire"), self.lease, False),
            (dict(lease_args, op="lease_release"), dict(self.lease, state="released"), False),
            ({"op": "status"}, {"copy": "file", "leases": []}, True),
        )
        for args, expected, dry_run in requests:
            outcome = adapter.dispatch(args, dry_run=dry_run)
            self.assertEqual(outcome.result, expected)
            self.assertEqual(outcome.copy, "file")
        self.assertEqual(self.file_calls, [(args["op"], dry_run) for args, _, dry_run in requests])

    def test_absent_runtime_uses_file_backend_for_every_operation(self):
        self.assert_file_fallback(BlockRuntimeAdapter)

    def test_surface_creation_progresses_while_probe_is_pending(self):
        entered = Event()
        release = Event()
        native_dispatch = Mock(return_value=DataResult({"copy": "none", "leases": []}, "none"))

        def pending_probe():
            entered.set()
            release.wait()
            return RuntimeBinding("fixture-v1", native_dispatch)

        def create_surface():
            return adapter.dispatch({"op": "status"})

        adapter = BlockRuntimeAdapter(self.file_dispatch)
        with ThreadPoolExecutor() as pool:
            starting = pool.submit(adapter.start_probe, pending_probe, expected_version="fixture-v1")
            try:
                self.assertTrue(entered.wait(5))
                probe = starting.result(5)
                self.assertEqual(pool.submit(create_surface).result(5).copy, "file")
                self.assertFalse(probe.done())
            finally:
                release.set()
            self.assertFalse(probe.result(5))
        self.assertEqual(adapter.dispatch({"op": "status"}).copy, "file")
        native_dispatch.assert_not_called()

    def test_abi_version_mismatch_falls_back(self):
        native_dispatch = Mock(side_effect=AssertionError("an incompatible binding dispatched"))
        mismatched = RuntimeBinding("fixture-v2", native_dispatch)
        adapter = BlockRuntimeAdapter(self.file_dispatch)
        self.assertFalse(adapter.start_probe(lambda: mismatched, expected_version="fixture-v1").result(5))
        self.assertEqual(adapter.dispatch({"op": "status"}).result, {"copy": "file", "leases": []})
        native_dispatch.assert_not_called()

    def test_runtime_load_error_falls_back(self):
        adapter = BlockRuntimeAdapter(self.file_dispatch)
        probe = Mock(side_effect=OSError("fixture library unavailable"))
        self.assertFalse(adapter.start_probe(probe, expected_version="fixture-v1").result(5))
        self.assertEqual(adapter.dispatch({"op": "status"}).copy, "file")

    def test_matching_binding_is_selected_without_claiming_file_fallback(self):
        result = DataResult({"copy": "none", "leases": []}, "none")
        handler = Mock(return_value=result)
        adapter = BlockRuntimeAdapter(self.file_dispatch)
        binding = RuntimeBinding("fixture-v1", handler)
        self.assertTrue(adapter.start_probe(lambda: binding, expected_version="fixture-v1").result(5))
        args = {"op": "status"}
        self.assertEqual(adapter.dispatch(args, dry_run=True), result)
        handler.assert_called_once_with(args, dry_run=True)
        self.assertEqual(self.file_calls, [])

    def test_native_dispatch_error_does_not_retry_file(self):
        native_dispatch = Mock(side_effect=OSError("native dispatch outcome unavailable"))
        adapter = BlockRuntimeAdapter(self.file_dispatch)
        binding = RuntimeBinding("fixture-v1", native_dispatch)
        self.assertTrue(adapter.start_probe(lambda: binding, expected_version="fixture-v1").result(5))
        with self.assertRaisesRegex(OSError, "native dispatch outcome unavailable"):
            adapter.dispatch({"op": "status"})
        native_dispatch.assert_called_once()
        self.assertEqual(self.file_calls, [])

    def test_closed_args_are_checked_before_dispatch(self):
        adapter = BlockRuntimeAdapter(self.file_dispatch)
        with self.assertRaises(ValidationError):
            adapter.dispatch({"op": "status", "unexpected": True})
        self.assertEqual(self.file_calls, [])

    def test_fallback_performs_no_filesystem_writes(self):
        attempts = []
        active = False

        def audit(event, args):
            if not active:
                return
            if event == "open" and args[2] & (os.O_ACCMODE | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                attempts.append(event)
            if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink", "os.truncate"}:
                attempts.append(event)

        sys.addaudithook(audit)
        active = True
        try:
            self.assert_file_fallback(BlockRuntimeAdapter)
            with self.assertRaises(FileNotFoundError):
                (Path(self.temp.name) / "missing" / "negative").write_bytes(b"negative control")
        finally:
            active = False
        # Audit records even the deliberately failed write attempt.
        self.assertEqual(attempts, ["open"])

    def load_mutant(self, original, replacement):
        path = Path(sys.modules[BlockRuntimeAdapter.__module__].__file__)
        source = path.read_text(encoding="utf-8")
        self.assertEqual(source.count(original), 1)
        mutant = ModuleType("vp_data_abi_mutant")
        mutant.__file__ = str(path)
        with patch.dict(sys.modules, {mutant.__name__: mutant}):
            exec(compile(source.replace(original, replacement), str(path), "exec"), mutant.__dict__)
        return mutant.BlockRuntimeAdapter

    def test_mutant_hard_runtime_dependency_is_rejected(self):
        mutant = self.load_mutant(
            "        if runtime is None:\n            result =",
            "        if runtime is None:\n            raise ImportError('runtime required')\n            result =",
        )
        with self.assertRaisesRegex(ImportError, "runtime required"):
            self.assert_file_fallback(mutant)

    def test_mutant_silent_fallback_is_rejected(self):
        mutant = self.load_mutant('                result["copy"] = "file"', '                result.pop("copy", None)')
        with self.assertRaisesRegex(ValidationError, "'copy' is a required property"):
            self.assert_file_fallback(mutant)


if __name__ == "__main__":
    unittest.main()
