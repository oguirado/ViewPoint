"""Deterministic negative controls for adversarial findings F-A through F-D."""
import builtins
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_f0u4_cache as fixtures

cache = fixtures.cache


class RepairTests(unittest.TestCase):
    setUp = fixtures.CacheTests.setUp
    write = fixtures.CacheTests.write
    evidence = fixtures.CacheTests.evidence
    publish = fixtures.CacheTests.publish
    lookup = fixtures.CacheTests.lookup
    entry = fixtures.CacheTests.entry
    assertResult = fixtures.CacheTests.assertResult

    def plant_entry(self):
        """A disk fixture tests lookup independently of publication guards."""
        for ref in self.manifest["artifacts"] + self.manifest["gate_refs"]:
            self.write(ref["path"], (self.source / ref["path"]).read_bytes(), root=self.entry())
        self.write("manifest.json", fixtures.encoded(self.manifest), root=self.entry())

    def test_fa_late_parent_fsync_rolls_back_new_entry(self):
        real_sync = cache._sync_dir
        observed = []

        def fail_after_rename(path):
            if Path(path) == self.store and (self.entry() / "manifest.json").exists():
                observed.append(self.lookup())
                raise OSError("injected parent fsync failure after rename")
            return real_sync(path)

        with patch.object(cache, "_sync_dir", fail_after_rename):
            result = self.publish()
        self.assertResult(result, 2, "ERROR")
        self.assertEqual(len(observed), 1)
        self.assertResult(self.lookup(), 0, "MISS")
        self.assertResult(observed[0], 0, "MISS")
        self.assertFalse((self.entry() / "manifest.json").exists())
        self.assertResult(self.publish(), 0, "HIT_PASS")

    def test_fa_success_path_unlink_failure_rolls_back(self):
        real_unlink = cache.os.unlink

        def fail_lock(path, *args, **kwargs):
            if Path(path).name.startswith(".publish-"):
                raise OSError("injected lease cleanup failure")
            return real_unlink(path, *args, **kwargs)

        with patch.object(cache.os, "unlink", fail_lock):
            result = self.publish()
        self.assertResult(result, 2, "ERROR")
        self.assertEqual(result["reason_codes"], ["cleanup_failed"])
        self.assertResult(self.lookup(), 0, "MISS")
        self.assertFalse((self.entry() / "manifest.json").exists())

    def test_fb_replacement_symlink_cannot_be_opened(self):
        # Both leaf and directory swaps happen after containment/stat checks.
        for kind in ("leaf", "directory"):
            with self.subTest(kind=kind):
                outside = self.root / ("outside-" + kind)
                outside.mkdir()
                (outside / "cart.glb").write_bytes(self.asset)
                victim = self.source / "assets" / "cart.glb" if kind == "leaf" else self.source / "assets"
                external = outside / "cart.glb" if kind == "leaf" else outside
                saved = victim.with_name(victim.name + ".saved")
                real_open, real_os_open = builtins.open, cache.os.open
                swaps, external_opens = [], []

                def swap_then_open(call, path, *args, **kwargs):
                    if isinstance(path, (str, os.PathLike)):
                        name = os.fspath(path)
                        targeted = name == str(victim) or (kwargs.get("dir_fd") is not None and name == victim.name)
                        if targeted:
                            victim.rename(saved)
                            victim.symlink_to(external, target_is_directory=kind == "directory")
                            swaps.append(name)
                            try:
                                fd = call(path, *args, **kwargs)
                                external_opens.append(name)
                                return fd
                            finally:
                                victim.unlink()
                                saved.rename(victim)
                    return call(path, *args, **kwargs)

                # The old ordinary open reaches the leaf in either case.
                def ordinary(path, *args, **kwargs):
                    if kind == "directory" and str(path) == str(victim / "cart.glb"):
                        victim.rename(saved)
                        victim.symlink_to(external, target_is_directory=True)
                        swaps.append(str(path))
                        try:
                            fd = real_open(path, *args, **kwargs)
                            external_opens.append(str(path))
                            return fd
                        finally:
                            victim.unlink()
                            saved.rename(victim)
                    return swap_then_open(real_open, path, *args, **kwargs)

                with patch("builtins.open", ordinary), patch.object(cache.os, "open", lambda *a, **kw: swap_then_open(real_os_open, *a, **kw)):
                    result = cache.publish(self.root / ("store-" + kind), self.request, self.manifest, self.source)
                self.assertTrue(swaps, "race must actually be injected")
                self.assertResult(result, 2, "ERROR")
                self.assertEqual(external_opens, [])
                self.assertEqual((self.source / "assets/cart.glb").read_bytes(), self.asset)

    def test_fc_unbound_error_preserves_error_precedence(self):
        self.bridge.update(rc=2, verdict="ERROR")
        self.manifest["gate_refs"][0]["subject_sha256"] = "a" * 64
        self.evidence(self.bridge)
        self.plant_entry()
        for operation in (self.lookup, lambda: cache.publish(self.root / "fresh", self.request, self.manifest, self.source)):
            with self.subTest(operation=operation):
                self.assertResult(operation(), 2, "ERROR")

    def test_fc_malformed_refusal_fields_are_error(self):
        self.bridge.update(rc=3, verdict="CANNOT_SEE", profile=[], receipt_file_sha256={}, trace_sha256=7)
        self.evidence(self.bridge)
        self.plant_entry()
        for operation in (self.lookup, lambda: cache.publish(self.root / "fresh", self.request, self.manifest, self.source)):
            with self.subTest(operation=operation):
                self.assertResult(operation(), 2, "ERROR")

    def test_fd_deadline_after_final_evidence_parsing(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        clock = [100]
        real_parse = cache._json_bytes

        def parse_then_expire(data):
            payload = real_parse(data)
            if payload.get("schema") == "af.bridge-verdict/v1":
                clock[0] += cache.TIMEOUT_S + 1
            return payload

        with patch.object(cache.time, "monotonic", lambda: clock[0]), patch.object(cache, "_json_bytes", parse_then_expire):
            self.assertResult(self.lookup(), 6, "BUDGET_EXCEEDED")

    def test_fd_validation_time_counts_toward_budget(self):
        clock = [100]
        real_key = cache.request_key

        def key_then_expire(request):
            key = real_key(request)
            clock[0] += cache.TIMEOUT_S + 1
            return key

        for operation in (self.lookup, self.publish):
            with self.subTest(operation=operation), patch.object(cache.time, "monotonic", lambda: clock[0]), patch.object(cache, "request_key", key_then_expire):
                self.assertResult(operation(), 6, "BUDGET_EXCEEDED")

    def test_fd_publish_deadline_during_final_fsync(self):
        clock = [100]
        real_sync = cache._sync_dir

        def sync_then_expire(path):
            real_sync(path)
            if Path(path) == self.store and (self.entry() / "manifest.json").exists():
                clock[0] += cache.TIMEOUT_S + 1

        with patch.object(cache.time, "monotonic", lambda: clock[0]), patch.object(cache, "_sync_dir", sync_then_expire):
            self.assertResult(self.publish(), 6, "BUDGET_EXCEEDED")
        self.assertResult(self.lookup(), 0, "MISS")
        self.assertFalse((self.entry() / "manifest.json").exists())

    def test_fd_deadline_during_lease_release(self):
        clock = [100]
        real_unlink = cache.os.unlink

        def unlink_then_expire(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if Path(path).name.startswith(".publish-"):
                clock[0] += cache.TIMEOUT_S + 1
            return result

        with patch.object(cache.time, "monotonic", lambda: clock[0]), patch.object(cache.os, "unlink", unlink_then_expire):
            self.assertResult(self.publish(), 6, "BUDGET_EXCEEDED")
        self.assertResult(self.lookup(), 0, "MISS")
        self.assertFalse((self.entry() / "manifest.json").exists())

    def test_fa_cleanup_refusal_preserves_preexisting_bytes(self):
        self.assertResult(self.publish(), 0, "HIT_PASS")
        before = {p.relative_to(self.entry()): p.read_bytes() for p in self.entry().rglob("*") if p.is_file()}
        real_unlink = cache.os.unlink

        def fail_lock(path, *args, **kwargs):
            if Path(path).name.startswith(".publish-"):
                raise OSError("injected lease cleanup failure")
            return real_unlink(path, *args, **kwargs)

        with patch.object(cache.os, "unlink", fail_lock):
            self.assertResult(self.publish(), 2, "ERROR")
        after = {p.relative_to(self.entry()): p.read_bytes() for p in self.entry().rglob("*") if p.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
