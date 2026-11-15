"""Hosted-safe unit and pipe-CLI tests for wender.plan (wender-mv-planner-cli)."""
from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from wender.plan import (
    DEFAULT_TICK_WINDOW,
    ELEMENT_TABLE_SCHEMA,
    EXIT_CODES,
    PlannerError,
    canonical_json,
    main,
    plan,
    plan_holdings,
    plan_holdings_step,
    project_vision_element_table,
    recheck_holdings_proposal,
    recheck_plan,
)
from wender.plan import cli as plan_cli
from wender.plan import policy as plan_policy
from wender.state.schema import validate_state

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "wender" / "plan"
FORBIDDEN_IMPORT_PREFIXES = (
    "wender.act",
    "vadapt",
    "visioncore",
    "vision_engine",
    "wender.perceive.vision_bridge",
    "wender.perceive.cdp_pipe",
    "wender.perceive.browser",
    "wender.perceive.atspi",
    "socket",
    "http",
    "urllib",
    "subprocess",
)


def _frame_ref(*, width: int = 120, height: int = 80, sha256: str | None = None) -> dict:
    digest = sha256 or hashlib.sha256(b"wender-plan-fixture-frame").hexdigest()
    return {
        "schema": "frame_ref/v0.2",
        "surface_id": "fixture-surface",
        "generation": 1,
        "tick": 5,
        "ts_ns": 1000,
        "width": width,
        "height": height,
        "format": "rgb24",
        "ramws_path": "/dev/shm/wender-plan-fixture",
        "sha256": digest,
        "capture_us": 12,
    }


def _observation(
    *,
    frame: dict | None = None,
    with_vision: bool = True,
    with_ground_truth: bool = False,
    vision_elements: list[dict] | None = None,
) -> dict:
    active_frame = _frame_ref() if frame is None else copy.deepcopy(frame)
    obs: dict = {
        "schema": "observation/v0.2",
        "frame_ref": active_frame,
    }
    if with_vision:
        elems = (
            [
                {
                    "bbox": {"x": 10, "y": 20, "w": 40, "h": 20},
                    "role": "button",
                    "name": "Continue",
                    "confidence": 0.9,
                }
            ]
            if vision_elements is None
            else copy.deepcopy(vision_elements)
        )
        obs["vision"] = {
            "summary": "fixture vision",
            "changed_regions": [{"x": 10, "y": 20, "w": 40, "h": 20}],
            "elements": elems,
        }
    if with_ground_truth:
        obs["ground_truth"] = {
            "source": "holdings_lane",
            "state_sha256": hashlib.sha256(b"holdings-lane-state-5").hexdigest(),
            "tick": active_frame["tick"],
        }
    return obs


def _element_table(*, frame: dict | None = None, elements: list[dict] | None = None) -> dict:
    active_frame = _frame_ref() if frame is None else frame
    rows = (
        [
            {
                "bbox": {"x": 10, "y": 20, "w": 40, "h": 20},
                "role": "button",
                "name": "Continue",
                "score_ppm": 900_000,
            },
            {
                "bbox": {"x": 60, "y": 10, "w": 30, "h": 16},
                "role": "link",
                "name": "Details",
                "score_ppm": 700_000,
            },
        ]
        if elements is None
        else copy.deepcopy(elements)
    )
    return {
        "schema": ELEMENT_TABLE_SCHEMA,
        "frame_sha256": active_frame["sha256"],
        "width": active_frame["width"],
        "height": active_frame["height"],
        "elements": rows,
    }


def _scan_plan_imports() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for path in sorted(PLAN_DIR.glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        package = rel[:-3].split("/")[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (
                    ".".join(
                        package[: len(package) - node.level + 1]
                        + ([node.module] if node.module else [])
                    )
                    if node.level
                    else (node.module or "")
                )
                names = [base] + [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
            else:
                continue
            for name in names:
                for forbidden in FORBIDDEN_IMPORT_PREFIXES:
                    if name == forbidden or name.startswith(forbidden + "."):
                        violations.append((rel, node.lineno, name))
    return violations


def _dump_req(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class MvPlannerCliTests(unittest.TestCase):
    def run_cli_inproc(self, payload: str, argv: list[str] | None = None) -> tuple[int, str, str]:
        in_buf = io.StringIO(payload)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        rc = main([] if argv is None else argv, stdin=in_buf, stdout=out_buf, stderr=err_buf)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def run_cli_subprocess(self, payload: str | bytes) -> subprocess.CompletedProcess:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        env = {
            key: os.environ[key]
            for key in ("PATH", "LANG")
            if key in os.environ
        }
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "wender.plan.cli"],
            cwd=str(ROOT),
            env=env,
            input=raw,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_cli_envelope_roundtrip(self) -> None:
        obs = _observation()
        table = _element_table()
        req = _dump_req({"observation": obs, "element_table": table, "goal": "Continue"})

        rc, out, err = self.run_cli_inproc(req)
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertEqual(out.strip(), canonical_json(parsed))
        self.assertEqual(set(parsed), {"action", "expectation", "state", "target_node"})
        self.assertEqual(parsed["action"], {"type": "click", "x": 30, "y": 30, "button": "left"})
        self.assertEqual(
            parsed["expectation"],
            {"type": "changed_region", "bbox": {"x": 10, "y": 20, "w": 40, "h": 20}},
        )
        self.assertEqual(parsed["target_node"]["name"], "Continue")
        self.assertEqual(parsed["target_node"]["ordinal"], 0)
        self.assertEqual(validate_state(parsed["state"]), [])
        self.assertEqual(recheck_plan(parsed, obs, table), parsed)

        proc = self.run_cli_subprocess(req)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(json.loads(proc.stdout.decode("utf-8")), parsed)

        # Second element selected when goal names "Details" (midpoint 60 + 30//2 = 75, 10 + 16//2 = 18)
        req_details = _dump_req({"observation": obs, "element_table": table, "goal": "Details"})
        rc2, out2, err2 = self.run_cli_inproc(req_details)
        self.assertEqual(rc2, 0, err2)
        parsed_details = json.loads(out2)
        self.assertEqual(parsed_details["action"], {"type": "click", "x": 75, "y": 18, "button": "left"})
        self.assertEqual(parsed_details["target_node"]["name"], "Details")
        self.assertEqual(parsed_details["target_node"]["ordinal"], 1)

        # Ground-truth observation in envelope emits state_sha256_change expectation
        obs_gt = _observation(with_ground_truth=True)
        table_gt = _element_table(frame=obs_gt["frame_ref"])
        req_gt = _dump_req({"observation": obs_gt, "element_table": table_gt, "goal": "Continue"})
        rc_gt, out_gt, err_gt = self.run_cli_inproc(req_gt)
        self.assertEqual(rc_gt, 0, err_gt)
        parsed_gt = json.loads(out_gt)
        self.assertEqual(parsed_gt["action"], {"type": "click", "x": 30, "y": 30, "button": "left"})
        self.assertEqual(
            parsed_gt["expectation"],
            {
                "type": "state_sha256_change",
                "before": obs_gt["ground_truth"]["state_sha256"],
                "min_tick": obs_gt["ground_truth"]["tick"] + 1,
                "max_tick": obs_gt["ground_truth"]["tick"] + DEFAULT_TICK_WINDOW,
            },
        )
        self.assertEqual(recheck_plan(parsed_gt, obs_gt, table_gt), parsed_gt)

    def test_frame_mismatch_refuses(self) -> None:
        obs = _observation()
        good_table = _element_table(frame=obs["frame_ref"])

        # SHA-256 mismatch with identical width and height
        sha_mismatch_table = dict(good_table, frame_sha256="f" * 64)
        with self.assertRaises(PlannerError) as ctx_sha:
            plan(obs, sha_mismatch_table, "Continue")
        self.assertEqual(ctx_sha.exception.code, "frame_mismatch")
        self.assertEqual(ctx_sha.exception.exit_code, EXIT_CODES["frame_mismatch"])

        req_sha = _dump_req(
            {"observation": obs, "element_table": sha_mismatch_table, "goal": "Continue"}
        )
        rc, out, err = self.run_cli_inproc(req_sha)
        self.assertEqual(rc, EXIT_CODES["frame_mismatch"])
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("frame_mismatch: "), err)

        # Dimension mismatch with identical frame_sha256
        dim_mismatch_table = dict(good_table, width=obs["frame_ref"]["width"] + 10)
        with self.assertRaises(PlannerError) as ctx_dim:
            plan(obs, dim_mismatch_table, "Continue")
        self.assertEqual(ctx_dim.exception.code, "frame_mismatch")

    def test_generation_control_refuses(self) -> None:
        obs = _observation()
        for stop_label in ("Stop", "Stop generating", "Detener", "Detener generación"):
            stop_table = _element_table(
                frame=obs["frame_ref"],
                elements=[
                    {
                        "bbox": {"x": 10, "y": 20, "w": 40, "h": 20},
                        "role": "button",
                        "name": stop_label,
                        "score_ppm": 950_000,
                    }
                ],
            )
            with self.subTest(stop_label=stop_label):
                with self.assertRaises(PlannerError) as ctx:
                    plan(obs, stop_table, stop_label)
                self.assertEqual(ctx.exception.code, "generation_control")
                self.assertEqual(ctx.exception.exit_code, EXIT_CODES["generation_control"])

                req = _dump_req(
                    {"observation": obs, "element_table": stop_table, "goal": stop_label}
                )
                rc, out, err = self.run_cli_inproc(req)
                self.assertEqual(rc, EXIT_CODES["generation_control"])
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("generation_control: "), err)

        # When both a Stop button and a safe button exist, Stop is skipped even if higher score_ppm
        mixed_table = _element_table(
            frame=obs["frame_ref"],
            elements=[
                {
                    "bbox": {"x": 10, "y": 20, "w": 40, "h": 20},
                    "role": "button",
                    "name": "Stop response",
                    "score_ppm": 990_000,
                },
                {
                    "bbox": {"x": 60, "y": 10, "w": 30, "h": 16},
                    "role": "button",
                    "name": "Continue",
                    "score_ppm": 800_000,
                },
            ],
        )
        mixed_res = plan(obs, mixed_table, "button")
        self.assertEqual(mixed_res["target_node"]["name"], "Continue")
        self.assertEqual(mixed_res["target_node"]["ordinal"], 1)
        self.assertFalse(mixed_res["state"]["nodes"][0]["actionable"])
        self.assertTrue(mixed_res["state"]["nodes"][1]["actionable"])

        # Plain observation/v0.2 with only Stop/Detener in vision.elements also refuses
        obs_stop_vis = _observation(
            with_vision=True,
            with_ground_truth=True,
            vision_elements=[
                {
                    "bbox": {"x": 10, "y": 20, "w": 40, "h": 20},
                    "role": "button",
                    "name": "Detener",
                    "confidence": 0.99,
                }
            ],
        )
        rc_vis, out_vis, err_vis = self.run_cli_inproc(_dump_req(obs_stop_vis))
        self.assertEqual(rc_vis, EXIT_CODES["generation_control"])
        self.assertEqual(out_vis, "")
        self.assertTrue(err_vis.startswith("generation_control: "), err_vis)

    def test_no_act_import(self) -> None:
        violations = _scan_plan_imports()
        self.assertEqual(violations, [], f"forbidden imports in wender/plan: {violations}")

        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys, wender.plan, wender.plan.cli, wender.plan.policy; "
                    "loaded = [m for m in sys.modules if m == 'wender.act' or m.startswith('wender.act.') "
                    "or m == 'vadapt' or m.startswith('vadapt.') or m == 'visioncore' or m.startswith('visioncore.')]; "
                    "print(json.dumps(loaded))"
                ),
            ],
            cwd=str(ROOT),
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        self.assertEqual(json.loads(probe.stdout), [])

    def test_observation_v02_and_cascade_controls(self) -> None:
        # 1. Plain observation/v0.2 with ground_truth and vision.elements -> action_proposal/v0.2
        obs_gt_vis = _observation(with_vision=True, with_ground_truth=True)
        rc, out, err = self.run_cli_inproc(_dump_req(obs_gt_vis))
        self.assertEqual(rc, 0, err)
        proposal = json.loads(out)
        self.assertEqual(proposal["schema"], "action_proposal/v0.2")
        self.assertEqual(proposal["action"], {"type": "click", "x": 30, "y": 30, "button": "left"})
        self.assertEqual(
            proposal["expectation"],
            {
                "type": "state_sha256_change",
                "before": obs_gt_vis["ground_truth"]["state_sha256"],
                "min_tick": obs_gt_vis["ground_truth"]["tick"] + 1,
                "max_tick": obs_gt_vis["ground_truth"]["tick"] + DEFAULT_TICK_WINDOW,
            },
        )
        self.assertEqual(recheck_holdings_proposal(proposal, obs_gt_vis), proposal)

        # 2. Plain observation/v0.2 with vision.elements only -> action_proposal/v0.2 with changed_region
        obs_vis_only = _observation(with_vision=True, with_ground_truth=False)
        rc_vo, out_vo, err_vo = self.run_cli_inproc(_dump_req(obs_vis_only))
        self.assertEqual(rc_vo, 0, err_vo)
        prop_vo = json.loads(out_vo)
        self.assertEqual(prop_vo["schema"], "action_proposal/v0.2")
        self.assertEqual(prop_vo["action"], {"type": "click", "x": 30, "y": 30, "button": "left"})
        self.assertEqual(
            prop_vo["expectation"],
            {"type": "changed_region", "bbox": {"x": 10, "y": 20, "w": 40, "h": 20}},
        )

        # 3. Project vision element table helper binds frame SHA/dims and feeds plan()
        projected = project_vision_element_table(obs_vis_only)
        self.assertEqual(projected["frame_sha256"], obs_vis_only["frame_ref"]["sha256"])
        self.assertEqual(projected["elements"][0]["score_ppm"], 900_000)
        planned_from_proj = plan(obs_vis_only, projected, "Continue")
        self.assertEqual(planned_from_proj["action"], {"type": "click", "x": 30, "y": 30, "button": "left"})

        # 4. Verify cascade tier-0 intent_to_action is invoked during singleton target selection
        with patch.object(plan_policy, "intent_to_action", wraps=plan_policy.intent_to_action) as spy_cascade:
            res = plan(obs_vis_only, _element_table(frame=obs_vis_only["frame_ref"]), "Details")
            self.assertEqual(res["target_node"]["name"], "Details")
            self.assertGreaterEqual(spy_cascade.call_count, 1)

        # 5. Out-of-frame, empty goal, no_target, policy (lane_submit), and malformed JSON controls
        oob_table = _element_table(
            frame=obs_vis_only["frame_ref"],
            elements=[
                {
                    "bbox": {"x": 110, "y": 20, "w": 20, "h": 20},
                    "role": "button",
                    "name": "Continue",
                    "score_ppm": 900_000,
                }
            ],
        )
        for expected_code, fn in (
            ("empty_goal", lambda: plan(obs_vis_only, projected, "   ")),
            ("out_of_bounds", lambda: plan(obs_vis_only, oob_table, "Continue")),
            ("no_target", lambda: plan(obs_vis_only, _element_table(frame=obs_vis_only["frame_ref"], elements=[]), "Continue")),
            ("policy", lambda: plan_holdings_step(obs_gt_vis, action={"type": "lane_submit", "verb": "x", "payload": {"nonce": "n", "seq": 1, "command": "c", "args": []}})),
            ("bad_shape", lambda: plan({"schema": "observation/v0.2", "frame_ref": _frame_ref()})),
        ):
            with self.subTest(code=expected_code):
                with self.assertRaises(PlannerError) as ctx:
                    fn()
                self.assertEqual(ctx.exception.code, expected_code)

        for raw_bad in (
            "",
            "{}",
            '{"observation":{},"element_table":{},"goal":"a","goal":"b"}',
            '{"observation":{},"element_table":{},"goal":NaN}',
        ):
            rc_b, out_b, err_b = self.run_cli_inproc(raw_bad)
            self.assertEqual(rc_b, EXIT_CODES["bad_shape"])
            self.assertEqual(out_b, "")
            self.assertTrue(err_b.startswith("bad_shape: "), err_b)

        rc_arg, out_arg, err_arg = self.run_cli_inproc("{}", argv=["--unexpected"])
        self.assertEqual(rc_arg, EXIT_CODES["bad_shape"])
        self.assertEqual(out_arg, "")
        self.assertTrue(err_arg.startswith("bad_shape: "), err_arg)
        self.assertEqual(plan_cli.MAX_STDIN_BYTES, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
