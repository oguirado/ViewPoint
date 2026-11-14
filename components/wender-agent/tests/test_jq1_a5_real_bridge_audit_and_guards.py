"""Attack and audit tests for RealVisionEngine RAM-only child boundary and guards."""
import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from wender.perceive.vision_adapter import Frame
from wender.perceive.vision_engine_real import RealVisionEngine, _WORKER


def extract_ram_only_guard_code() -> str:
    """Extract only ram_only function and sys.addaudithook registration from _WORKER using stdlib AST."""
    tree = ast.parse(_WORKER)
    extracted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "ram_only":
            extracted.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if (isinstance(func, ast.Name) and func.id == "sys") or (
                isinstance(func, ast.Attribute) and func.attr == "addaudithook"
            ):
                extracted.append(node)
    header = ast.parse("import os, sys").body
    mod = ast.Module(body=header + extracted, type_ignores=[])
    ast.fix_missing_locations(mod)
    return ast.unparse(mod)


class TestRealBridgeAuditAndGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard_code = extract_ram_only_guard_code()

    def test_ast_extraction(self):
        code = self.guard_code
        self.assertIn("def ram_only", code)
        self.assertIn("sys.addaudithook", code)
        self.assertNotIn("importlib", code)
        self.assertNotIn("pickle", code)
        self.assertNotIn("visioncore", code)

    def test_forbidden_fs_mutation_audit_events_raise_storage_law(self):
        forbidden_events = (
            ("os.mkdir", ("inert_path", 511)),
            ("os.remove", ("inert_path",)),
            ("os.rmdir", ("inert_path",)),
            ("os.rename", ("inert_src", "inert_dst")),
            ("os.link", ("inert_src", "inert_dst")),
            ("os.symlink", ("inert_src", "inert_dst")),
        )
        for event, args in forbidden_events:
            with self.subTest(event=event):
                script = (
                    f"{self.guard_code}\n"
                    f"sys.audit({event!r}, *{args!r})\n"
                    f"print('HARNESS_UNREACHABLE')\n"
                )
                res = subprocess.run(
                    [sys.executable, "-B", "-c", script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(
                    res.returncode, 0, f"Audit event {event} did not trigger guard"
                )
                self.assertNotIn("HARNESS_UNREACHABLE", res.stdout)
                self.assertIn("RuntimeError", res.stderr)
                self.assertIn("STORAGE LAW", res.stderr)

    def test_forbidden_open_write_audit_events_raise_storage_law(self):
        write_flags = (
            ("O_WRONLY", os.O_WRONLY),
            ("O_RDWR", os.O_RDWR),
            ("O_CREAT", os.O_CREAT),
            ("O_TRUNC", os.O_TRUNC),
            ("O_APPEND", os.O_APPEND),
        )
        for name, flag in write_flags:
            with self.subTest(flag=name):
                script = (
                    f"{self.guard_code}\n"
                    f"sys.audit('open', 'inert_path', 'r', {flag})\n"
                    f"print('HARNESS_UNREACHABLE')\n"
                )
                res = subprocess.run(
                    [sys.executable, "-B", "-c", script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(
                    res.returncode, 0, f"Open flag {name} did not trigger guard"
                )
                self.assertNotIn("HARNESS_UNREACHABLE", res.stdout)
                self.assertIn("RuntimeError", res.stderr)
                self.assertIn("STORAGE LAW", res.stderr)

    def test_allowed_read_only_open_audit_event_succeeds(self):
        script = (
            f"{self.guard_code}\n"
            f"sys.audit('open', 'inert_path', 'r', {os.O_RDONLY})\n"
            f"print('HARNESS_SUCCESS')\n"
        )
        res = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"Allowed read-only open failed: {res.stderr}")
        self.assertIn("HARNESS_SUCCESS", res.stdout)

    def test_constructor_partial_checkout_missing_eval_fails(self):
        def mock_is_file(path_obj):
            s = str(path_obj)
            return s.endswith("visioncore/pipeline.py")

        with patch.object(Path, "is_file", autospec=True, side_effect=mock_is_file):
            with patch.dict(os.environ, {"VISION_ENGINE_ROOT": "/mock/engine"}):
                with self.assertRaises(FileNotFoundError) as cm:
                    RealVisionEngine()
                self.assertIn("eval/v2x/sf_tiledesc.py", str(cm.exception))

    def test_constructor_partial_checkout_missing_pipeline_fails(self):
        def mock_is_file(path_obj):
            s = str(path_obj)
            return s.endswith("eval/v2x/sf_tiledesc.py")

        with patch.object(Path, "is_file", autospec=True, side_effect=mock_is_file):
            with patch.dict(os.environ, {"VISION_ENGINE_ROOT": "/mock/engine"}):
                with self.assertRaises(FileNotFoundError) as cm:
                    RealVisionEngine()
                self.assertIn("visioncore/pipeline.py", str(cm.exception))

    def test_nonzero_child_status_raises_runtimeerror_with_diagnostic(self):
        with patch.object(Path, "is_file", autospec=True, return_value=True):
            with patch.dict(os.environ, {"VISION_ENGINE_ROOT": "/mock/engine"}):
                engine = RealVisionEngine()

        mock_completed = subprocess.CompletedProcess(
            args=["python3"],
            returncode=1,
            stdout=b"",
            stderr=b"CUSTOM_CHILD_FAILURE_DIAGNOSTIC_ERR",
        )
        with patch("subprocess.run", return_value=mock_completed):
            frame = Frame(10, 10, b"\x00" * 300)
            with self.assertRaises(RuntimeError) as cm:
                engine.screen_fingerprint(frame)
            self.assertIn("vision-engine fingerprint failed:", str(cm.exception))
            self.assertIn("CUSTOM_CHILD_FAILURE_DIAGNOSTIC_ERR", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
