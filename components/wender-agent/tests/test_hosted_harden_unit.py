# @module {"id":"wa.tests.test_hosted_harden_unit","layer":2,"owner":"ACT","deps":["wa.act.hosted_guard","wa.perceive.cdp_pipe","wa.perceive.cdp_port","wa.plan.cli"],"book":"HB-WA-s2-act"}
"""Unit and AST-mutation tests for WA-HOSTED-HARDEN (sec_wender Findings A, B, C, D, E, F, H)."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from wender.act import hosted_guard
from wender.act.hosted_guard import (
    ACT_IMPORT_REFUSAL_CODE,
    HOSTED_ENV_VAR,
    HOSTED_READONLY_CDP_METHODS,
    REFUSAL_CODE,
    HostedActuationRefused,
    assert_actuation_allowed,
    assert_cdp_method_allowed,
    assert_plan_has_no_act_imports,
    hosted_guard_scope,
    hosted_reason,
    is_hosted,
    is_planner_entry_active,
    is_readonly_cdp_method,
    mark_planner_entry,
    planner_entry_scope,
    scan_plan_act_imports,
    set_hosted_mode,
)
from wender.perceive import cdp_pipe, cdp_port
from wender.perceive.cdp_pipe import CdpError, CdpPipe, Deframer, MAX_DEFRAMER_BYTES
from wender.perceive.cdp_port import CdpPort
from wender.plan import cli as plan_cli
from wender.plan.policy import EXIT_CODES, PlannerError

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "wender" / "plan"


def _valid_envelope() -> dict:
    digest = hashlib.sha256(b"wa-hosted-harden-frame").hexdigest()
    return {
        "observation": {
            "schema": "observation/v0.2",
            "frame_ref": {
                "schema": "frame_ref/v0.2",
                "surface_id": "harden-surface",
                "generation": 1,
                "tick": 3,
                "ts_ns": 500,
                "width": 120,
                "height": 80,
                "format": "rgb24",
                "ramws_path": "/dev/shm/harden-frame",
                "sha256": digest,
                "capture_us": 10,
            },
            "vision": {
                "summary": "ok",
                "changed_regions": [{"x": 10, "y": 10, "w": 30, "h": 20}],
                "elements": [
                    {
                        "bbox": {"x": 10, "y": 10, "w": 30, "h": 20},
                        "role": "button",
                        "name": "Continue",
                        "confidence": 0.9,
                    }
                ],
            },
        },
        "element_table": {
            "schema": "vqa.element-table/v1",
            "frame_sha256": digest,
            "width": 120,
            "height": 80,
            "elements": [
                {
                    "bbox": {"x": 10, "y": 10, "w": 30, "h": 20},
                    "role": "button",
                    "name": "Continue",
                    "score_ppm": 900_000,
                }
            ],
        },
        "goal": "Continue",
    }


def _compile_mutant_module(src_path: Path, transform_fn, mod_name: str):
    tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
    mutated = transform_fn(tree)
    ast.fix_missing_locations(mutated)
    code = compile(mutated, f"<{mod_name}>", "exec")
    spec = importlib.util.spec_from_loader(mod_name, loader=None)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(src_path)
    orig_meta_path = list(sys.meta_path)
    try:
        exec(code, mod.__dict__)
    finally:
        sys.meta_path[:] = orig_meta_path
        hosted_guard.install_executor_guards()
    return mod


class _ChunkedBinaryReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _ChunkedBinaryStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.buffer = _ChunkedBinaryReader(chunks)


class _ChunkedTextStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int = -1) -> str:
        if not self._chunks:
            return ""
        return self._chunks.pop(0)


# @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests
class HostedHardenUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._reset_private_guard_state()

    def tearDown(self) -> None:
        self._reset_private_guard_state()

    def _reset_private_guard_state(self) -> None:
        with hosted_guard._STATE_LOCK:
            hosted_guard._HOSTED_MODE_LATCHED = False
            hosted_guard._HOSTED_MODE_OVERRIDE = None
            hosted_guard._PLANNER_ENTRY_LATCHED = False
            hosted_guard._PLANNER_ENTRY_DEPTH = 0
            hosted_guard._PLANNER_SCOPE_DEPTH = 0
        sys._wender_hosted_latched = False  # type: ignore[attr-defined]
        sys._wender_planner_entry_depth = 0  # type: ignore[attr-defined]
        os.environ.pop(HOSTED_ENV_VAR, None)

    # ------------------------------------------------------------------
    # Finding A: exact bounded stdin & trailing-data drain in cli.py
    # ------------------------------------------------------------------
    # @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests.test_finding_a_exact_bounded_stdin_and_mutant
    def test_finding_a_exact_bounded_stdin_and_mutant(self) -> None:
        valid_bytes = json.dumps(_valid_envelope(), separators=(",", ":")).encode("utf-8")

        # 1. Exact multi-chunk valid stream succeeds when total <= MAX_STDIN_BYTES
        stream_ok = _ChunkedBinaryStream([valid_bytes[:40], valid_bytes[40:]])
        mode, parsed = plan_cli.read_stdin_request(stream_ok)
        self.assertEqual(mode, "wender_envelope")
        self.assertEqual(parsed["goal"], "Continue")

        # 2. Single-chunk and multi-chunk oversize inputs (> MAX_STDIN_BYTES) are refused
        oversize_tail = b" " * (plan_cli.MAX_STDIN_BYTES - len(valid_bytes) + 1)
        for stream_over in (
            _ChunkedBinaryStream([valid_bytes + oversize_tail]),
            _ChunkedBinaryStream([valid_bytes, oversize_tail]),
            _ChunkedTextStream([valid_bytes.decode("utf-8"), oversize_tail.decode("utf-8")]),
        ):
            with self.assertRaises(PlannerError) as ctx_over:
                plan_cli.read_stdin_request(stream_over)
            self.assertEqual(ctx_over.exception.code, "bad_shape")
            self.assertIn("exceeds maximum size", ctx_over.exception.detail)

        # 3. Trailing data in a second pipe chunk after a complete JSON object is refused
        for stream_trailing in (
            _ChunkedBinaryStream([valid_bytes, b'{"trailing":true}']),
            _ChunkedTextStream([valid_bytes.decode("utf-8"), "\nextra_token"]),
        ):
            with self.assertRaises(PlannerError) as ctx_trail:
                plan_cli.read_stdin_request(stream_trailing)
            self.assertEqual(ctx_trail.exception.code, "bad_shape")

        # 4. Named mutation: remove the drain loop in _drain_stream_text so only the first
        # read(MAX_STDIN_BYTES + 1) is inspected (matching the pre-fix single-read bug).
        def mutate_single_read(tree: ast.AST) -> ast.AST:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_drain_stream_text":
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.While):
                            # Break after the first chunk in the while loop
                            sub.body.append(ast.Break())
            return tree

        mutant_cli = _compile_mutant_module(
            Path(plan_cli.__file__), mutate_single_read, "wender.plan.cli_mutant_a"
        )
        # On the mutant, a 2-chunk stream where chunk 1 is valid JSON and chunk 2 is oversize
        # or trailing garbage erroneously succeeds instead of raising PlannerError!
        with self.assertRaises(AssertionError):
            with self.assertRaises(PlannerError):
                mutant_cli.read_stdin_request(
                    _ChunkedBinaryStream([valid_bytes, b'{"trailing":true}'])
                )

    # ------------------------------------------------------------------
    # Finding B: sticky hosted latch & deterministic CLI self-entry
    # ------------------------------------------------------------------
    # @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests.test_finding_b_sticky_hosted_guard_and_cli_self_entry_and_mutants
    def test_finding_b_sticky_hosted_guard_and_cli_self_entry_and_mutants(self) -> None:
        # 1. Once set_hosted_mode(True) is triggered, no public clear call can disable it.
        self.assertFalse(is_hosted())
        set_hosted_mode(True)
        self.assertTrue(is_hosted())
        self.assertEqual(hosted_reason(), "hosted_mode_override")

        for clear_attempt in (
            lambda: set_hosted_mode(False),
            lambda: set_hosted_mode(None),
            lambda: mark_planner_entry(False),
        ):
            clear_attempt()
            self.assertTrue(is_hosted())
            self.assertEqual(hosted_reason(), "hosted_mode_override")
            with self.assertRaises(HostedActuationRefused):
                assert_actuation_allowed("test.sticky")

        with hosted_guard_scope(False):
            self.assertTrue(is_hosted())
            with self.assertRaises(HostedActuationRefused):
                assert_actuation_allowed("test.sticky_scope")

        self._reset_private_guard_state()

        # 2. Once mark_planner_entry(True) is called, mark_planner_entry(False) and
        # set_hosted_mode(False) cannot re-enable actuation.
        mark_planner_entry(True)
        self.assertTrue(is_planner_entry_active())
        self.assertTrue(is_hosted())
        mark_planner_entry(False)
        set_hosted_mode(False)
        self.assertTrue(is_planner_entry_active())
        self.assertTrue(is_hosted())
        with self.assertRaises(HostedActuationRefused):
            assert_actuation_allowed("test.sticky_planner")

        self._reset_private_guard_state()

        # 3. While MV_HOSTED=1 or planner_entry_scope() is active, set_hosted_mode(False)
        # cannot override or clear the active guard.
        with patch.dict(os.environ, {HOSTED_ENV_VAR: "1"}, clear=False):
            set_hosted_mode(False)
            self.assertTrue(is_hosted())
            self.assertEqual(hosted_reason(), "MV_HOSTED=1")

        with planner_entry_scope():
            set_hosted_mode(False)
            mark_planner_entry(False)
            self.assertTrue(is_hosted())
            self.assertEqual(hosted_reason(), "planner_entry")

        # 4. CLI deterministic self-entry into hosted mode without path/stack heuristics:
        valid_req = json.dumps(_valid_envelope(), separators=(",", ":"))
        observed_inside_cli: list[tuple[bool, str | None]] = []
        orig_plan = plan_cli.plan

        def _spy_plan(obs, table, goal):
            observed_inside_cli.append((is_hosted(), hosted_reason()))
            with self.assertRaises(HostedActuationRefused):
                assert_actuation_allowed("cli.spy")
            return orig_plan(obs, table, goal)

        with patch.object(hosted_guard, "_main_is_planner_entry", return_value=False), \
             patch.object(hosted_guard, "_planner_on_call_stack", return_value=False), \
             patch.object(plan_cli, "plan", side_effect=_spy_plan):
            out_buf, err_buf = io.StringIO(), io.StringIO()
            rc = plan_cli.main([], stdin=io.StringIO(valid_req), stdout=out_buf, stderr=err_buf)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertEqual(observed_inside_cli, [(True, "planner_entry")])

        # 5. Subprocess invocation of wender.plan.cli with scrubbed env sets MV_HOSTED=1
        # and _wender_hosted_latched=True without importing wender.act.
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import io, json, os, sys, wender.plan.cli as cli; "
                    "out, err = io.StringIO(), io.StringIO(); "
                    "rc = cli.main(None, stdin=io.StringIO(sys.stdin.read()), stdout=out, stderr=err); "
                    "act_mods = [m for m in sys.modules if m == 'wender.act' or m.startswith('wender.act.')]; "
                    "print(json.dumps({"
                    "'rc': rc, "
                    "'mv_hosted': os.environ.get('MV_HOSTED'), "
                    "'latched': getattr(sys, '_wender_hosted_latched', False), "
                    "'act_mods': act_mods"
                    "}))"
                ),
            ],
            cwd=str(ROOT),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            input=valid_req,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        sub_info = json.loads(proc.stdout)
        self.assertEqual(sub_info["rc"], 0)
        self.assertEqual(sub_info["mv_hosted"], "1")
        self.assertTrue(sub_info["latched"])
        self.assertEqual(sub_info["act_mods"], [])

        # 6. Named mutation 1: allow clearing the hosted flag in set_hosted_mode
        def mutate_clearable_hosted(tree: ast.AST) -> ast.AST:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "set_hosted_mode":
                    node.body = [
                        ast.Global(names=["_HOSTED_MODE_OVERRIDE", "_HOSTED_MODE_LATCHED"]),
                        ast.Assign(
                            targets=[ast.Name(id="_HOSTED_MODE_OVERRIDE", ctx=ast.Store())],
                            value=ast.Name(id="enabled", ctx=ast.Load()),
                        ),
                        ast.Assign(
                            targets=[ast.Name(id="_HOSTED_MODE_LATCHED", ctx=ast.Store())],
                            value=ast.Constant(value=False),
                        ),
                        ast.Return(value=ast.Constant(value=None)),
                    ]
            return tree

        mutant_guard = _compile_mutant_module(
            Path(hosted_guard.__file__), mutate_clearable_hosted, "wender.act.hosted_guard_mutant_b1"
        )
        mutant_guard.set_hosted_mode(True)
        mutant_guard.set_hosted_mode(False)
        with self.assertRaises(AssertionError):
            self.assertTrue(mutant_guard.is_hosted())

        # 7. Named mutation 2: drop the CLI's self-entry into hosted mode
        def mutate_drop_cli_scope(tree: ast.AST) -> ast.AST:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "planner_cli_guard_scope":
                    node.body = [ast.Expr(value=ast.Yield(value=None))]
            return tree

        mutant_cli_b = _compile_mutant_module(
            Path(plan_cli.__file__), mutate_drop_cli_scope, "wender.plan.cli_mutant_b2"
        )
        mutant_observed: list[bool] = []
        mutant_cli_b.plan = lambda obs, tbl, g: (
            mutant_observed.append(is_hosted()) or orig_plan(obs, tbl, g)
        )
        with patch.object(hosted_guard, "_main_is_planner_entry", return_value=False), \
             patch.object(hosted_guard, "_planner_on_call_stack", return_value=False):
            mutant_cli_b.main(
                [], stdin=io.StringIO(valid_req), stdout=io.StringIO(), stderr=io.StringIO()
            )
        with self.assertRaises(AssertionError):
            self.assertEqual(mutant_observed, [True])

    # ------------------------------------------------------------------
    # Finding C: CDP transport guard & explicit read-only allowlist
    # ------------------------------------------------------------------
    # @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests.test_finding_c_cdp_transport_guard_and_readonly_allowlist_and_mutant
    def test_finding_c_cdp_transport_guard_and_readonly_allowlist_and_mutant(self) -> None:
        # 1. Explicit allowlist contains perception reads and excludes mutating methods.
        self.assertIsInstance(HOSTED_READONLY_CDP_METHODS, frozenset)
        for read_method in (
            "Accessibility.enable",
            "Accessibility.getFullAXTree",
            "Accessibility.disable",
            "DOM.getDocument",
            "DOM.getBoxModel",
            "Page.enable",
            "Page.getLayoutMetrics",
            "Page.captureScreenshot",
            "Target.getTargets",
            "Target.getTargetInfo",
            "Target.attachToTarget",
            "Target.detachFromTarget",
        ):
            self.assertIn(read_method, HOSTED_READONLY_CDP_METHODS)
            self.assertTrue(is_readonly_cdp_method(read_method))

        for write_method in (
            "Input.dispatchMouseEvent",
            "Input.dispatchKeyEvent",
            "Input.insertText",
            "Input.dispatchTouchEvent",
            "Runtime.evaluate",
            "Runtime.callFunctionOn",
            "DOM.resolveNode",
            "DOM.scrollIntoViewIfNeeded",
            "DOM.focus",
            "DOM.setAttributeValue",
            "Page.navigate",
            "Page.reload",
            "Target.createTarget",
            "Target.closeTarget",
            "Browser.close",
        ):
            self.assertNotIn(write_method, HOSTED_READONLY_CDP_METHODS)
            self.assertFalse(is_readonly_cdp_method(write_method))

        # 2. Construct a CdpPipe and a CdpPort with mocked _write_command that auto-replies.
        pipe = CdpPipe(chrome_bin="/bin/true", timeout=1.0)
        pipe._closed = False
        pipe_writes: list[str] = []

        def _fake_pipe_write(msg: dict) -> None:
            pipe_writes.append(msg["method"])
            with pipe._cv:
                pipe._store_reply_locked({"id": msg["id"], "result": {"ok": msg["method"]}})
                pipe._cv.notify_all()

        pipe._write_command = _fake_pipe_write  # type: ignore[method-assign]

        port = CdpPort(port=9222, host="127.0.0.1", timeout=1.0)
        port._closed = False
        port_writes: list[str] = []

        def _fake_port_write(msg: dict) -> None:
            port_writes.append(msg["method"])
            with port._cv:
                port._store_reply_locked({"id": msg["id"], "result": {"ok": msg["method"]}})
                port._cv.notify_all()

        port._write_command = _fake_port_write  # type: ignore[method-assign]

        # Standalone: both reads and writes are allowed.
        self.assertEqual(pipe.send("Input.dispatchMouseEvent", {"type": "mouseMoved"}), {"ok": "Input.dispatchMouseEvent"})
        self.assertEqual(port.send("Input.dispatchKeyEvent", {"type": "keyDown"}), {"ok": "Input.dispatchKeyEvent"})
        pipe_writes.clear()
        port_writes.clear()

        # Hosted (both MV_HOSTED=1 and planner_entry_scope):
        for trigger_reason, ctx in (
            ("MV_HOSTED=1", patch.dict(os.environ, {HOSTED_ENV_VAR: "1"}, clear=False)),
            ("planner_entry", planner_entry_scope()),
        ):
            with self.subTest(trigger=trigger_reason), ctx:
                # Perception reads succeed on both CdpPipe and CdpPort
                self.assertEqual(
                    pipe.send("Accessibility.getFullAXTree"),
                    {"ok": "Accessibility.getFullAXTree"},
                )
                self.assertEqual(
                    port.send("DOM.getBoxModel", {"backendNodeId": 1}),
                    {"ok": "DOM.getBoxModel"},
                )
                self.assertEqual(pipe_writes, ["Accessibility.getFullAXTree"])
                self.assertEqual(port_writes, ["DOM.getBoxModel"])
                pipe_writes.clear()
                port_writes.clear()

                # Every mutating/actuation method is refused BEFORE _write_command
                for blocked_method in (
                    "Input.dispatchMouseEvent",
                    "Input.dispatchKeyEvent",
                    "Input.insertText",
                    "Runtime.evaluate",
                    "Runtime.callFunctionOn",
                    "Page.navigate",
                    "Target.createTarget",
                    "DOM.scrollIntoViewIfNeeded",
                ):
                    with self.assertRaises(HostedActuationRefused) as ctx_pipe:
                        pipe.send(blocked_method, {})
                    self.assertEqual(ctx_pipe.exception.code, REFUSAL_CODE)
                    self.assertEqual(ctx_pipe.exception.entry, f"CdpPipe.send:{blocked_method}")
                    self.assertEqual(ctx_pipe.exception.reason, trigger_reason)

                    with self.assertRaises(HostedActuationRefused) as ctx_port:
                        port.send(blocked_method, {})
                    self.assertEqual(ctx_port.exception.code, REFUSAL_CODE)
                    self.assertEqual(ctx_port.exception.entry, f"CdpPipe.send:{blocked_method}")
                    self.assertEqual(ctx_port.exception.reason, trigger_reason)

                self.assertEqual(pipe_writes, [])
                self.assertEqual(port_writes, [])

        # 3. Named mutation: remove assert_actuation_allowed from CdpPipe.send
        def mutate_remove_cdp_send_guard(tree: ast.AST) -> ast.AST:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "CdpPipe":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "send":
                            item.body = [
                                stmt
                                for stmt in item.body
                                if "assert_actuation_allowed" not in ast.unparse(stmt)
                            ]
            return tree

        mutant_cdp = _compile_mutant_module(
            Path(cdp_pipe.__file__), mutate_remove_cdp_send_guard, "wender.perceive.cdp_pipe_mutant_c"
        )
        m_pipe = mutant_cdp.CdpPipe(chrome_bin="/bin/true", timeout=1.0)
        m_pipe._closed = False

        def _fake_m_pipe_write(msg: dict) -> None:
            with m_pipe._cv:
                m_pipe._store_reply_locked({"id": msg["id"], "result": {"mutated": True}})
                m_pipe._cv.notify_all()

        m_pipe._write_command = _fake_m_pipe_write
        with patch.dict(os.environ, {HOSTED_ENV_VAR: "1"}, clear=False):
            with self.assertRaises(AssertionError):
                with self.assertRaises(HostedActuationRefused):
                    m_pipe.send("Input.dispatchMouseEvent", {})

    # ------------------------------------------------------------------
    # Finding D: obfuscated dynamic imports, exec/eval, transitive scan
    # ------------------------------------------------------------------
    # @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests.test_finding_d_hardened_and_transitive_act_import_scan_and_mutant
    def test_finding_d_hardened_and_transitive_act_import_scan_and_mutant(self) -> None:
        # 1. Obfuscated dynamic imports and exec/eval in planner sources are rejected.
        obfuscated_snippets = (
            "import importlib\nimportlib.import_module('wender.' + 'act.cdp_executor')",
            "import importlib\nmod = 'wender.act'\nimportlib.import_module(mod)",
            "import importlib\nimportlib.import_module('.'.join(['wender', 'act']))",
            "import importlib\ngetattr(importlib, 'import_module')('wender.act')",
            "exec('import wender.act')",
            "eval('__import__(\"wender.act\")')",
            "import sys\n_ = sys.modules['wender.act']",
        )
        for snippet in obfuscated_snippets:
            with self.subTest(snippet=snippet), tempfile.TemporaryDirectory() as tmpdir:
                fixture_plan = Path(tmpdir) / "plan"
                shutil.copytree(PLAN_DIR, fixture_plan)
                target = fixture_plan / "cli.py"
                target.write_text(
                    target.read_text(encoding="utf-8") + f"\n{snippet}\n",
                    encoding="utf-8",
                )
                violations = scan_plan_act_imports(fixture_plan)
                self.assertGreaterEqual(len(violations), 1, f"missed snippet: {snippet}")
                with self.assertRaises(HostedActuationRefused) as ctx_v:
                    assert_plan_has_no_act_imports(fixture_plan)
                self.assertEqual(ctx_v.exception.code, ACT_IMPORT_REFUSAL_CODE)

        # 2. Transitive closure: injecting an act import into a transitive dependency
        # (wender/learn/cascade.py in a fixture tree) is caught when scanning plan/.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            fixture_plan = tmp_root / "plan"
            fixture_learn = tmp_root / "learn"
            shutil.copytree(PLAN_DIR, fixture_plan)
            shutil.copytree(ROOT / "wender" / "learn", fixture_learn)
            cascade_file = fixture_learn / "cascade.py"
            cascade_file.write_text(
                cascade_file.read_text(encoding="utf-8") + "\nimport wender.act.actor\n",
                encoding="utf-8",
            )
            trans_violations = scan_plan_act_imports(fixture_plan)
            self.assertEqual(len(trans_violations), 1)
            self.assertEqual(trans_violations[0]["imported"], "wender.act.actor")

            # Named mutation: disable transitive queueing in scan_plan_act_imports
            def mutate_no_transitive(tree: ast.AST) -> ast.AST:
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "_resolve_wender_module_file":
                        node.body = [ast.Return(value=ast.Constant(value=None))]
                return tree

            mutant_guard_d = _compile_mutant_module(
                Path(hosted_guard.__file__), mutate_no_transitive, "wender.act.hosted_guard_mutant_d"
            )
            with self.assertRaises(AssertionError):
                self.assertEqual(len(mutant_guard_d.scan_plan_act_imports(fixture_plan)), 1)

    # ------------------------------------------------------------------
    # Findings E & F: element-count caps and bounded sanitized stderr
    # ------------------------------------------------------------------
    # @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests.test_finding_e_f_element_bounds_and_bounded_error_replies_and_mutant
    def test_finding_e_f_element_bounds_and_bounded_error_replies_and_mutant(self) -> None:
        # 1. Oversize element lists in element_table or vision are refused before planning.
        env_many = _valid_envelope()
        row = copy.deepcopy(env_many["element_table"]["elements"][0])
        env_many["element_table"]["elements"] = [row for _ in range(plan_cli.MAX_PLANNER_ELEMENTS + 1)]
        raw_many = json.dumps(env_many, separators=(",", ":"))
        out_m, err_m = io.StringIO(), io.StringIO()
        rc_m = plan_cli.main([], stdin=io.StringIO(raw_many), stdout=out_m, stderr=err_m)
        self.assertEqual(rc_m, EXIT_CODES["bad_shape"])
        self.assertEqual(out_m.getvalue(), "")
        self.assertIn("element_table.elements exceeds maximum count", err_m.getvalue())

        # 2. Unexpected internal exceptions never leak raw exception text to stderr,
        # and huge PlannerError details are truncated to MAX_ERROR_DETAIL_BYTES.
        valid_raw = json.dumps(_valid_envelope(), separators=(",", ":"))
        secret_marker = "FAKE_SECRET_INTERNAL_PATH_/home/example/token_999"
        with patch.object(plan_cli, "plan", side_effect=RuntimeError(secret_marker)):
            out_e, err_e = io.StringIO(), io.StringIO()
            rc_e = plan_cli.main([], stdin=io.StringIO(valid_raw), stdout=out_e, stderr=err_e)
        self.assertEqual(rc_e, EXIT_CODES["bad_shape"])
        self.assertEqual(out_e.getvalue(), "")
        self.assertNotIn(secret_marker, err_e.getvalue())
        self.assertEqual(err_e.getvalue().strip(), "bad_shape: internal planner failure")

        huge_detail = "x" * (plan_cli.MAX_ERROR_DETAIL_BYTES * 4)
        with patch.object(plan_cli, "plan", side_effect=PlannerError("bad_shape", huge_detail)):
            out_h, err_h = io.StringIO(), io.StringIO()
            rc_h = plan_cli.main([], stdin=io.StringIO(valid_raw), stdout=out_h, stderr=err_h)
        self.assertEqual(rc_h, EXIT_CODES["bad_shape"])
        self.assertEqual(out_h.getvalue(), "")
        prefix, detail_out = err_h.getvalue().strip().split(": ", 1)
        self.assertEqual(prefix, "bad_shape")
        self.assertLessEqual(len(detail_out.encode("utf-8")), plan_cli.MAX_ERROR_DETAIL_BYTES)

        # 3. Named mutation: disable _validate_request_element_bounds in cli.py
        def mutate_no_element_cap(tree: ast.AST) -> ast.AST:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_validate_request_element_bounds":
                    node.body = [ast.Return(value=ast.Constant(value=None))]
            return tree

        mutant_cli_ef = _compile_mutant_module(
            Path(plan_cli.__file__), mutate_no_element_cap, "wender.plan.cli_mutant_ef"
        )
        with self.assertRaises(AssertionError):
            with self.assertRaises(PlannerError):
                mutant_cli_ef.read_stdin_request(io.StringIO(raw_many))

    # ------------------------------------------------------------------
    # Finding H: bounded Deframer buffer & session lane fencing
    # ------------------------------------------------------------------
    # @anchor:wa.tests.test_hosted_harden_unit.HostedHardenUnitTests.test_finding_h_deframer_buffer_cap_and_lane_fencing_and_mutant
    def test_finding_h_deframer_buffer_cap_and_lane_fencing_and_mutant(self) -> None:
        # 1. Deframer rejects NUL-less stream exceeding max_bytes and clears its buffer.
        deframer = Deframer(max_bytes=128)
        self.assertEqual(deframer.feed(b"a" * 100), [])
        with self.assertRaises(CdpError) as ctx_df:
            deframer.feed(b"b" * 30)
        self.assertIn("deframer buffer overflow", str(ctx_df.exception))
        self.assertEqual(deframer._buf, b"")

        # 2. CdpPipe._read_loop fences active session lanes when the stream overflows Deframer.
        cmd_r, cmd_w = os.pipe()
        rep_r, rep_w = os.pipe()
        pipe = CdpPipe(chrome_bin="/bin/true", timeout=1.0)
        pipe._w = cmd_w
        pipe._r = rep_r
        pipe.proc = MagicMock()
        pipe._session_incarnations["sid-overflow"] = 1

        with patch.object(cdp_pipe, "Deframer", side_effect=lambda: Deframer(max_bytes=64)):
            reader = threading.Thread(target=pipe._read_loop, daemon=True)
            reader.start()
            os.write(rep_w, b"x" * 128)
            reader.join(timeout=2.0)
            self.assertFalse(reader.is_alive())

        self.assertTrue(pipe._closed)
        self.assertIn("sid-overflow", pipe._lane_errors)
        self.assertIn("deframer buffer overflow", pipe._lane_errors["sid-overflow"])
        os.close(cmd_r)
        os.close(rep_w)
        pipe.close()

        # 3. Named mutation: remove the buffer cap check in Deframer.feed
        def mutate_unbounded_deframer(tree: ast.AST) -> ast.AST:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == "Deframer":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "feed":
                            for sub in ast.walk(item):
                                if isinstance(sub, ast.If) and isinstance(sub.test, ast.Compare):
                                    if isinstance(sub.test.left, ast.Name) and sub.test.left.id == "i":
                                        sub.body = [ast.Return(value=ast.Name(id="out", ctx=ast.Load()))]
            return tree

        mutant_cdp_h = _compile_mutant_module(
            Path(cdp_pipe.__file__), mutate_unbounded_deframer, "wender.perceive.cdp_pipe_mutant_h"
        )
        m_df = mutant_cdp_h.Deframer(max_bytes=64)
        with self.assertRaises(AssertionError):
            with self.assertRaises(CdpError):
                m_df.feed(b"z" * 128)


if __name__ == "__main__":
    unittest.main()
