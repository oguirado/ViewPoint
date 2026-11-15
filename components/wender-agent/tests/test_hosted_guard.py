"""Hosted-safe unit tests for wender.act.hosted_guard (wender-hosted-no-actuation)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import importlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from wender.act import (
    ACT_IMPORT_REFUSAL_CODE,
    HOSTED_ENV_VAR,
    REFUSAL_CODE,
    ActuationError,
    HostedActuationRefused,
    assert_actuation_allowed,
    assert_plan_has_no_act_imports,
    cdp_executor,
    guard_executor,
    hosted_guard,
    hosted_guard_scope,
    hosted_reason,
    is_hosted,
    is_planner_entry_active,
    mark_planner_entry,
    planner_entry_scope,
    scan_plan_act_imports,
    set_hosted_mode,
)
from wender.perceive import ax

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "wender" / "plan"


def _raw_ax_tree(*, pressed: bool = False) -> list[dict]:
    return [
        {
            "nodeId": "1",
            "role": {"type": "role", "value": "RootWebArea"},
            "name": {"type": "computedString", "value": "App"},
            "childIds": ["2"],
            "ignored": False,
        },
        {
            "nodeId": "2",
            "role": {"type": "role", "value": "main"},
            "name": {"type": "computedString", "value": "Workspace"},
            "childIds": ["10"],
            "ignored": False,
        },
        {
            "nodeId": "10",
            "role": {"type": "role", "value": "button"},
            "name": {"type": "computedString", "value": "Send"},
            "childIds": [],
            "ignored": False,
            "properties": [
                {"name": "pressed", "value": {"type": "value", "value": pressed}},
                {"name": "disabled", "value": {"type": "value", "value": False}},
            ],
            "backendDOMNodeId": 100,
        },
    ]


class _RecordingCdp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        payload = dict(params or {})
        self.calls.append((method, payload, session_id))
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{payload.get('backendNodeId')}"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 50, 20, 50, 40, 10, 40]}}
        return {}


class _RecordingReader:
    def __init__(self, snapshot: list[dict]) -> None:
        self.snapshot = list(snapshot)
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        return list(self.snapshot)


class HostedGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        set_hosted_mode(None)
        while is_planner_entry_active() and hosted_guard._PLANNER_ENTRY_DEPTH > 0:
            mark_planner_entry(False)
        self.initial_nodes = ax.prune(_raw_ax_tree(pressed=False))
        self.after_nodes = ax.prune(_raw_ax_tree(pressed=True))
        self.btn = self.initial_nodes[2]
        self.click_action = {
            "op": "click",
            "target": self.btn["id"],
            "expect": {"target_states": {"pressed": "true"}, "target_name": "Send"},
            "irreversible": False,
        }

    def tearDown(self) -> None:
        set_hosted_mode(None)
        while hosted_guard._PLANNER_ENTRY_DEPTH > 0:
            mark_planner_entry(False)

    def test_hosted_blocks_cdp(self) -> None:
        # 1. Standalone baseline when guard is off (unset or "0"): CDP action succeeds.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(HOSTED_ENV_VAR, None)
            self.assertFalse(is_hosted())
            self.assertIsNone(hosted_reason())
            cdp_ok = _RecordingCdp()
            reader_ok = _RecordingReader(self.after_nodes)
            receipt = cdp_executor.run_ax_action(
                cdp_ok,
                "sid-standalone",
                self.initial_nodes,
                self.click_action,
                reader_ok,
                wait_ms=0,
                sleep=lambda _: None,
            )
            self.assertEqual(receipt["ok"], 1)
            self.assertEqual(receipt["verified"], 1)
            self.assertEqual(len(cdp_ok.calls), 3)
            self.assertEqual(reader_ok.calls, 1)

        with patch.dict(os.environ, {HOSTED_ENV_VAR: "0"}, clear=False):
            self.assertFalse(is_hosted())
            cdp_zero = _RecordingCdp()
            reader_zero = _RecordingReader(self.after_nodes)
            receipt_zero = cdp_executor.run_ax_action(
                cdp_zero,
                "sid-zero",
                self.initial_nodes,
                self.click_action,
                reader_zero,
                wait_ms=0,
                sleep=lambda _: None,
            )
            self.assertEqual(receipt_zero["ok"], 1)
            self.assertEqual(len(cdp_zero.calls), 3)

        # 2. When MV_HOSTED=1, every cdp_executor entry refuses before any CDP or reader I/O.
        for env_val in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(env_val=env_val), patch.dict(
                os.environ, {HOSTED_ENV_VAR: env_val}, clear=False
            ):
                self.assertTrue(is_hosted())
                self.assertEqual(hosted_reason(), "MV_HOSTED=1")
                cdp_blocked = _RecordingCdp()
                reader_blocked = _RecordingReader(self.after_nodes)

                with self.assertRaises(HostedActuationRefused) as ctx_legacy:
                    cdp_executor.run_ax_action(
                        cdp_blocked,
                        "sid-hosted",
                        self.initial_nodes,
                        self.click_action,
                        reader_blocked,
                        wait_ms=0,
                        sleep=lambda _: None,
                    )
                exc = ctx_legacy.exception
                self.assertIsInstance(exc, cdp_executor.ActionError)
                self.assertIsInstance(exc, ActuationError)
                self.assertIsInstance(exc, ValueError)
                self.assertIsInstance(exc, RuntimeError)
                self.assertEqual(exc.code, REFUSAL_CODE)
                self.assertEqual(exc.entry, "cdp_executor.run_ax_action")
                self.assertEqual(exc.reason, "MV_HOSTED=1")
                self.assertEqual(cdp_blocked.calls, [])
                self.assertEqual(reader_blocked.calls, 0)

                # Target-aware / authorized path also refuses before touching fence or focus_policy
                fence_spy_calls: list[str] = []

                class _SpyFence:
                    def acquire(self, _lease) -> bool:
                        fence_spy_calls.append("acquire")
                        return True

                    def release(self, _lease) -> None:
                        fence_spy_calls.append("release")

                    def quarantine(self, _lease) -> None:
                        fence_spy_calls.append("quarantine")

                with self.assertRaises(HostedActuationRefused) as ctx_auth:
                    cdp_executor.run_ax_action(
                        cdp_blocked,
                        "sid-hosted-auth",
                        self.initial_nodes,
                        self.click_action,
                        reader_blocked,
                        lease=object(),
                        focus=object(),
                        observation=object(),
                        registry=object(),
                        focus_policy=object(),
                        writer_fence=_SpyFence(),
                    )
                self.assertEqual(ctx_auth.exception.code, REFUSAL_CODE)
                self.assertEqual(fence_spy_calls, [])
                self.assertEqual(cdp_blocked.calls, [])

                # Direct internal CDP helpers also refuse before any cdp.send
                with self.assertRaises(HostedActuationRefused) as ctx_disp:
                    cdp_executor._dispatch_ax_op(
                        cdp_blocked, "sid-hosted", self.btn, self.click_action
                    )
                self.assertEqual(ctx_disp.exception.entry, "cdp_executor._dispatch_ax_op")
                self.assertEqual(cdp_blocked.calls, [])

                with self.assertRaises(HostedActuationRefused) as ctx_dom:
                    cdp_executor._resolve_dom_object(cdp_blocked, "sid-hosted", self.btn)
                self.assertEqual(ctx_dom.exception.entry, "cdp_executor._resolve_dom_object")
                self.assertEqual(cdp_blocked.calls, [])

                with self.assertRaises(HostedActuationRefused) as ctx_box:
                    cdp_executor._resolve_box(cdp_blocked, "sid-hosted", self.btn, {})
                self.assertEqual(ctx_box.exception.entry, "cdp_executor._resolve_box")
                self.assertEqual(cdp_blocked.calls, [])

                with self.assertRaises(HostedActuationRefused) as ctx_auth_fn:
                    cdp_executor._run_authorized_ax_action(
                        cdp_blocked,
                        "sid-hosted",
                        self.initial_nodes,
                        self.click_action,
                        reader_blocked,
                        1,
                        0,
                        lambda _: None,
                        False,
                        object(),
                        object(),
                        object(),
                        object(),
                        object(),
                        _SpyFence(),
                        None,
                    )
                self.assertEqual(
                    ctx_auth_fn.exception.entry, "cdp_executor._run_authorized_ax_action"
                )
                self.assertEqual(fence_spy_calls, [])
                self.assertEqual(cdp_blocked.calls, [])

        # 3. Planner entry active (via planner_entry_scope, __main__ spec, or wender.plan call stack)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(HOSTED_ENV_VAR, None)
            with planner_entry_scope():
                self.assertTrue(is_planner_entry_active())
                self.assertTrue(is_hosted())
                self.assertEqual(hosted_reason(), "planner_entry")
                cdp_plan = _RecordingCdp()
                reader_plan = _RecordingReader(self.after_nodes)
                with self.assertRaises(HostedActuationRefused) as ctx_plan:
                    cdp_executor.run_ax_action(
                        cdp_plan,
                        "sid-plan",
                        self.initial_nodes,
                        self.click_action,
                        reader_plan,
                    )
                self.assertEqual(ctx_plan.exception.reason, "planner_entry")
                self.assertEqual(cdp_plan.calls, [])
                self.assertEqual(reader_plan.calls, 0)

            # Simulated wender.plan call-stack frame without env or scope flags
            plan_frame_globals = {"__name__": "wender.plan.cli", "cdp_executor": cdp_executor}
            plan_caller_code = compile(
                "def _from_plan(cdp, nodes, action, reader):\n"
                "    return cdp_executor.run_ax_action(cdp, 'sid-stack', nodes, action, reader)\n",
                filename=str(PLAN_DIR / "cli.py"),
                mode="exec",
            )
            exec(plan_caller_code, plan_frame_globals)
            cdp_stack = _RecordingCdp()
            reader_stack = _RecordingReader(self.after_nodes)
            with self.assertRaises(HostedActuationRefused) as ctx_stack:
                plan_frame_globals["_from_plan"](
                    cdp_stack, self.initial_nodes, self.click_action, reader_stack
                )
            self.assertEqual(ctx_stack.exception.reason, "planner_entry")
            self.assertEqual(cdp_stack.calls, [])
            self.assertEqual(reader_stack.calls, 0)

            # Simulated __main__ as wender.plan.cli
            fake_main = types.ModuleType("__main__")
            fake_main.__spec__ = types.SimpleNamespace(name="wender.plan.cli")
            fake_main.__file__ = str(PLAN_DIR / "cli.py")
            with patch.dict(sys.modules, {"__main__": fake_main}):
                self.assertTrue(is_planner_entry_active())
                self.assertEqual(hosted_reason(), "planner_entry")
                cdp_main = _RecordingCdp()
                reader_main = _RecordingReader(self.after_nodes)
                with self.assertRaises(HostedActuationRefused) as ctx_main:
                    cdp_executor.run_ax_action(
                        cdp_main,
                        "sid-main",
                        self.initial_nodes,
                        self.click_action,
                        reader_main,
                    )
                self.assertEqual(ctx_main.exception.reason, "planner_entry")
                self.assertEqual(cdp_main.calls, [])

    def test_hosted_blocks_all_act_executors(self) -> None:
        before_mods = set(sys.modules)
        act_pkg = sys.modules["wender.act"]
        try:
            actor = importlib.import_module("wender.act.actor")
            api_path = importlib.import_module("wender.act.api_path")
            godot_executor = importlib.import_module("wender.act.godot_executor")
            godot_window_driver = importlib.import_module("wender.act.godot_window_driver")
            holdings_dispatch = importlib.import_module("wender.act.holdings_dispatch")
            jq3_t2_broker_client = importlib.import_module("wender.act.jq3_t2_broker_client")
            multiwin_isolation_verifier = importlib.import_module("wender.act.multiwin_isolation_verifier")
            proto1_action_policy = importlib.import_module("wender.act.proto1_action_policy")
            pslot_action_gate = importlib.import_module("wender.act.pslot_action_gate")
            w8u_client = importlib.import_module("wender.act.w8u_client")

            # Standalone sanity check when guard is off
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(HOSTED_ENV_VAR, None)
                fake_api = api_path.FakeApiAdapter(name="fake", allowed=("demo.ping",))
                spec = api_path.ApiCallSpec(program="fake", call="demo.ping", params={"x": 1})
                res_api = api_path.run_api_action(fake_api, spec)
                self.assertEqual(res_api["result"]["status"], "ok")
                with self.assertRaises(NotImplementedError):
                    actor.execute("ax", {})

            # When hosted (both MV_HOSTED=1 and planner_entry_scope), every wender.act executor refuses
            sink_calls: list[bytes] = []
            send_calls: list[object] = []

            godot_exec = godot_executor.GodotExecutor(
                {"principal": "p1", "transport": "OFF"},
                {"noop": {"irreversible": False, "args": {}}},
                lambda batch: b"encoded",
                lambda payload: sink_calls.append(payload),
            )

            for trigger_name, ctx_mgr in (
                ("MV_HOSTED=1", patch.dict(os.environ, {HOSTED_ENV_VAR: "1"}, clear=False)),
                ("planner_entry", planner_entry_scope()),
            ):
                with self.subTest(trigger=trigger_name), ctx_mgr:
                    for entry_label, call_fn in (
                        ("actor.execute", lambda: actor.execute("ax", {})),
                        ("api_path.run_api_action", lambda: api_path.run_api_action(fake_api, spec)),
                        ("api_path.BpyExampleAdapter.call", lambda: api_path.BpyExampleAdapter().call("bpy.ops.wm.redraw_timer", {})),
                        ("api_path.FakeApiAdapter.call", lambda: fake_api.call("demo.ping", {})),
                        ("godot_executor.GodotExecutor.dispatch", lambda: godot_exec.dispatch("noop", {})),
                        ("godot_executor.GodotExecutor.dispatch_batch", lambda: godot_exec.dispatch_batch([{"cmd": "noop", "args": {}}])),
                        ("godot_window_driver.GodotWindowDriver.execute", lambda: godot_window_driver.GodotWindowDriver.execute(object(), object(), lease=object(), focus=object(), observation=object())),
                        ("godot_window_driver.GodotWindowDriver.confirm_completion", lambda: godot_window_driver.GodotWindowDriver.confirm_completion(object(), object(), object())),
                        ("holdings_dispatch.dispatch_guarded", lambda: holdings_dispatch.dispatch_guarded(object(), locks=object(), authorize=object(), send=lambda *a: send_calls.append(a), clock=object(), stop=object(), resolve=object(), observe=object(), schedule=object(), admit=object())),
                        ("holdings_dispatch.dispatch_batch", lambda: holdings_dispatch.dispatch_batch([])),
                        ("jq3_t2_broker_client.submit_batch", lambda: jq3_t2_broker_client.submit_batch([])),
                        ("multiwin_isolation_verifier.authorize_cdp_action", lambda: multiwin_isolation_verifier.authorize_cdp_action()),
                        ("multiwin_isolation_verifier.verify_cdp_isolation", lambda: multiwin_isolation_verifier.verify_cdp_isolation()),
                        ("multiwin_isolation_verifier.authorize_godot_command", lambda: multiwin_isolation_verifier.authorize_godot_command()),
                        ("multiwin_isolation_verifier.verify_godot_isolation", lambda: multiwin_isolation_verifier.verify_godot_isolation()),
                        ("proto1_action_policy.ActionPolicy.dispatch", lambda: proto1_action_policy.ActionPolicy.dispatch(object(), object(), object())),
                        ("pslot_action_gate.run_vision_action", lambda: pslot_action_gate.run_vision_action(object(), "s", {}, target=object(), observation=object(), registry=object(), lease=object(), focus=object(), focus_policy=object(), writer_fence=object(), reader=object())),
                        ("w8u_client.W8UStepClient.execute", lambda: w8u_client.W8UStepClient.execute(object(), {}, dispatch=object())),
                    ):
                        with self.assertRaises(HostedActuationRefused, msg=entry_label) as ctx_exec:
                            call_fn()
                        self.assertEqual(ctx_exec.exception.code, REFUSAL_CODE)
                        self.assertEqual(ctx_exec.exception.entry, entry_label)
                        self.assertEqual(ctx_exec.exception.reason, trigger_name)
                    self.assertEqual(sink_calls, [])
                    self.assertEqual(send_calls, [])
        finally:
            for mod_name in list(sys.modules):
                if mod_name not in before_mods:
                    sys.modules.pop(mod_name, None)
                    if mod_name.startswith("wender.act."):
                        short = mod_name.rsplit(".", 1)[-1]
                        if hasattr(act_pkg, short):
                            delattr(act_pkg, short)

    def test_plan_has_no_act_import(self) -> None:
        # 1. Real wender/plan/* must be statically free of wender.act imports.
        self.assertEqual(scan_plan_act_imports(PLAN_DIR), [])
        assert_plan_has_no_act_imports(PLAN_DIR)

        # 2. Executing wender.plan.cli in a clean subprocess never loads wender.act.
        frame_sha = hashlib.sha256(b"hosted-guard-frame").hexdigest()
        envelope = {
            "observation": {
                "schema": "observation/v0.2",
                "frame_ref": {
                    "schema": "frame_ref/v0.2",
                    "surface_id": "guard-surface",
                    "generation": 1,
                    "tick": 1,
                    "ts_ns": 100,
                    "width": 100,
                    "height": 80,
                    "format": "rgb24",
                    "ramws_path": "/dev/shm/guard-frame",
                    "sha256": frame_sha,
                    "capture_us": 10,
                },
                "vision": {
                    "summary": "s",
                    "changed_regions": [{"x": 10, "y": 10, "w": 20, "h": 20}],
                    "elements": [
                        {
                            "bbox": {"x": 10, "y": 10, "w": 20, "h": 20},
                            "role": "button",
                            "name": "Continue",
                            "confidence": 0.9,
                        }
                    ],
                },
            },
            "element_table": {
                "schema": "vqa.element-table/v1",
                "frame_sha256": frame_sha,
                "width": 100,
                "height": 80,
                "elements": [
                    {
                        "bbox": {"x": 10, "y": 10, "w": 20, "h": 20},
                        "role": "button",
                        "name": "Continue",
                        "score_ppm": 900_000,
                    }
                ],
            },
            "goal": "Continue",
        }
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import io, json, sys, wender.plan.cli as cli; "
                    "out, err = io.StringIO(), io.StringIO(); "
                    "rc = cli.main([], stdin=io.StringIO(sys.stdin.read()), stdout=out, stderr=err); "
                    "act_mods = sorted(m for m in sys.modules if m == 'wender.act' or m.startswith('wender.act.')); "
                    "print(json.dumps({'rc': rc, 'act_mods': act_mods, 'err': err.getvalue()}))"
                ),
            ],
            cwd=str(ROOT),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            input=json.dumps(envelope),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        probe_res = json.loads(probe.stdout)
        self.assertEqual(probe_res["rc"], 0, probe_res["err"])
        self.assertEqual(probe_res["act_mods"], [])

        # 3. Injecting any wender.act import into a plan fixture must be caught by scan_plan_act_imports.
        injected_statements = (
            ("from wender.act import cdp_executor", "wender.act"),
            ("from wender.act.cdp_executor import run_ax_action", "wender.act.cdp_executor"),
            ("import wender.act", "wender.act"),
            ("import wender.act.cdp_executor", "wender.act.cdp_executor"),
            ("from wender import act", "wender.act"),
            ("from ..act import cdp_executor", "wender.act"),
            ("from .. import act", "wender.act"),
            ("import importlib\nimportlib.import_module('wender.act.cdp_executor')", "wender.act.cdp_executor"),
        )
        for stmt, expected_prefix in injected_statements:
            with self.subTest(stmt=stmt), tempfile.TemporaryDirectory() as tmpdir:
                fixture_dir = Path(tmpdir) / "plan"
                shutil.copytree(PLAN_DIR, fixture_dir)
                self.assertEqual(scan_plan_act_imports(fixture_dir), [])
                target_file = fixture_dir / "cli.py"
                target_file.write_text(
                    target_file.read_text(encoding="utf-8") + f"\n{stmt}\n",
                    encoding="utf-8",
                )
                violations = scan_plan_act_imports(fixture_dir)
                self.assertEqual(len(violations), 1, f"expected violation for {stmt!r}, got {violations}")
                self.assertEqual(violations[0]["file"], "cli.py")
                self.assertTrue(
                    str(violations[0]["imported"]).startswith(expected_prefix),
                    violations[0],
                )
                with self.assertRaises(HostedActuationRefused) as ctx_imp:
                    assert_plan_has_no_act_imports(fixture_dir)
                self.assertEqual(ctx_imp.exception.code, ACT_IMPORT_REFUSAL_CODE)
                self.assertEqual(ctx_imp.exception.reason, "static_act_import")


if __name__ == "__main__":
    unittest.main()
