"""Unit tests for swarm/agents (H2)."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from swarm.agents import CANONICAL_ROLES, DEFAULT_WAIT_TIMEOUT_S, REPO_ROOT
from swarm.agents.intent_cli import (
    IntentValidationError,
    build_parser as build_intent_parser,
    main as intent_cli_main,
    read_events_records,
    validate_event_id,
    validate_intent_dict,
)
from swarm.agents.prompts import (
    ROLE_SUFFIXES,
    SHARED_BRIEF_PREFIX,
    build_brief,
    validate_shared_prefix,
)
from swarm.agents.runner import (
    AGY_BIN,
    build_agent_env,
    build_scope_prefix,
    kill_recorded_pids,
    read_proc_starttime_ticks,
    run_agents,
    scope_unit_name,
)
from swarm.agents.scripted_smoke import (
    build_smoke_plan,
    main as smoke_main,
    resolve_output_dir,
    run_smoke,
)


def _run_cli_quiet(argv: list[str]) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = intent_cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def _write_frame(frames_dir: Path, frame: int, view_epoch: int = 1, scene_epoch: int = 1) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "v": 1,
        "frame": frame,
        "sha256": "0" * 64,
        "width": 1920,
        "height": 1080,
        "view_epoch": view_epoch,
        "scene_epoch": scene_epoch,
        "changed_regions": [[600, 300, 80, 80]],
        "ts_ms": 1731580000000 + frame * 100,
    }
    (frames_dir / f"frame_{frame}.json").write_text(json.dumps(meta), encoding="utf-8")
    (frames_dir / f"frame_{frame}.png").write_bytes(b"PNG_PLACEHOLDER")
    tmp = frames_dir / ".latest.json.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, frames_dir / "latest.json")


class TestSharedPrefixBriefs(unittest.TestCase):
    def test_shared_prefix_byte_identical_and_sized(self) -> None:
        prefix_bytes = SHARED_BRIEF_PREFIX.encode("utf-8")
        self.assertGreaterEqual(len(prefix_bytes), 1200)
        sizes = validate_shared_prefix(min_bytes=1200)
        self.assertEqual(set(sizes.keys()), set(CANONICAL_ROLES))
        for role in CANONICAL_ROLES:
            rendered = build_brief(role, run_id="run_test99")
            self.assertTrue(
                rendered.startswith(SHARED_BRIEF_PREFIX),
                f"{role} brief must start with exact SHARED_BRIEF_PREFIX",
            )
            self.assertEqual(
                rendered.encode("utf-8")[: len(prefix_bytes)],
                prefix_bytes,
            )
            # B1: All example --event-id flags in rendered brief must be prefixed with run_id-
            suffix_part = rendered[len(SHARED_BRIEF_PREFIX) :]
            for match in re.finditer(r"--event-id\s+([A-Za-z0-9._-]+)", suffix_part):
                self.assertTrue(
                    match.group(1).startswith("run_test99-"),
                    f"{role} example event_id {match.group(1)!r} must be run_id-prefixed",
                )

    def test_m5_single_tasklight_owner_h4_only(self) -> None:
        self.assertIn("sole owner of TaskLight", SHARED_BRIEF_PREFIX)
        self.assertIn("swarm/projection/projector.py", SHARED_BRIEF_PREFIX)
        self.assertIn("MUST NOT call `post_plan`, `post_step`, `post_status`, or `close_task`", SHARED_BRIEF_PREFIX)
        for role, suffix in ROLE_SUFFIXES.items():
            self.assertNotIn("context-wall", suffix, f"{role} suffix must not reference context-wall")
            self.assertNotIn("close_task", suffix, f"{role} suffix must not instruct close_task")

    def test_l1_camera_brief_uses_absolute_deliverable_paths_and_active_builder_rule(self) -> None:
        brief = build_brief("camera-agent", "take1")
        expected_dir = "~/Documentos/viewpoint-hackathon/swarm/run/output"
        for fname in (
            "starforge_sentinel.blend",
            "starforge_sentinel.glb",
            "front.png",
            "turntable.png",
            "hero.png",
        ):
            self.assertIn(f"{expected_dir}/{fname}", brief)
        self.assertNotIn('"swarm/run/output/', brief)
        for match in re.finditer(r"swarm/run/output", brief):
            prefix = brief[max(0, match.start() - len("~/Documentos/viewpoint-hackathon/")) : match.start()]
            self.assertEqual(prefix, "~/Documentos/viewpoint-hackathon/")
        self.assertIn("active builder agents present in `events.jsonl`", brief)


class TestIntentCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="swarm_h2_test_")
        self.root = Path(self.tmp.name)
        self.frames_dir = self.root / "frames"
        self.intents_dir = self.root / "intents"
        self.events_log = self.root / "events.jsonl"
        _write_frame(self.frames_dir, frame=12, view_epoch=2, scene_epoch=5)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_observe_and_enqueue_script_atomic(self) -> None:
        claim = {
            "v": 1,
            "rec": "claim",
            "agent_id": "modeler-agent",
            "lease_id": "lease-m-1",
            "lease_epoch": 4,
            "ttl_ms": 300000,
            "issued_ts_ms": 1731580000000,
            "expires_ts_ms": 1731580300000,
            "allow_gui": False,
            "write_prefixes": ["col:CHAR_", "obj:CHAR_"],
        }
        self.events_log.write_text(json.dumps(claim) + "\n", encoding="utf-8")
        expected_offset = self.events_log.stat().st_size

        rc, out, _ = _run_cli_quiet(
            [
                "observe",
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
                "--agent",
                "modeler-agent",
            ]
        )
        self.assertEqual(rc, 0)
        obs = json.loads(out)
        self.assertEqual(obs["frame"], 12)
        self.assertEqual(obs["lease_epoch"], 4)

        script_file = self.root / "build.py"
        script_src = "import bpy\nprint('ok')\n"
        script_file.write_text(script_src, encoding="utf-8")

        rc, enq_out, _ = _run_cli_quiet(
            [
                "enqueue-script",
                "--agent",
                "modeler-agent",
                "--event-id",
                "r1-modeler-agent-1",
                "--write-set",
                "col:CHAR_mesh,obj:CHAR_Torso",
                "--expect",
                "CHAR_Torso created",
                "--script-file",
                str(script_file),
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc, 0)
        enq_summary = json.loads(enq_out)
        self.assertEqual(enq_summary["events_log_offset"], expected_offset)

        offset_file = self.intents_dir / "modeler-agent" / ".offsets" / "r1-modeler-agent-1.json"
        self.assertTrue(offset_file.is_file())
        offset_meta = json.loads(offset_file.read_text(encoding="utf-8"))
        self.assertEqual(offset_meta["offset_bytes"], expected_offset)
        self.assertEqual(
            offset_meta["script_sha256"],
            hashlib.sha256(script_src.encode("utf-8")).hexdigest(),
        )

        intent_path = self.intents_dir / "modeler-agent" / "r1-modeler-agent-1.json"
        self.assertTrue(intent_path.is_file())
        data = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["lane"], "script")
        self.assertEqual(data["based_on_frame"], 12)
        self.assertEqual(data["view_epoch"], 2)
        self.assertEqual(data["scene_epoch"], 5)
        self.assertEqual(data["lease_epoch"], 4)
        self.assertEqual(
            data["script_sha256"],
            hashlib.sha256(script_src.encode("utf-8")).hexdigest(),
        )
        validate_intent_dict(data)

    def test_enqueue_gui_atomic(self) -> None:
        rc, enq_out, _ = _run_cli_quiet(
            [
                "enqueue-gui",
                "--agent",
                "camera-agent",
                "--event-id",
                "r1-camera-agent-1",
                "--keys",
                "KP_1,KP_3,KP_7",
                "--footprint",
                "0,80,1580,900",
                "--expect",
                "Front/side/top review",
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc, 0)
        enq_summary = json.loads(enq_out)
        self.assertEqual(enq_summary["events_log_offset"], 0)
        intent_path = self.intents_dir / "camera-agent" / "r1-camera-agent-1.json"
        data = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["lane"], "gui")
        self.assertEqual(data["action_class"], "view_change")
        self.assertEqual(data["kind"], "key")
        self.assertEqual(data["keys"], ["KP_1", "KP_3", "KP_7"])
        self.assertEqual(data["max_acts"], 3)
        self.assertEqual(data["footprint"], [0, 80, 1580, 900])
        validate_intent_dict(data)

    def test_negative_controls_keys_agent_prefix_and_inbox(self) -> None:
        # 1. Disallowed GUI keys (KP_9, Delete)
        for bad_key in ("KP_9", "Delete"):
            rc, _, err = _run_cli_quiet(
                [
                    "enqueue-gui",
                    "--agent",
                    "camera-agent",
                    "--event-id",
                    f"bad-{bad_key}",
                    "--keys",
                    f"KP_1,{bad_key}",
                    "--expect",
                    "Should fail",
                    "--intents-dir",
                    str(self.intents_dir),
                    "--frames-dir",
                    str(self.frames_dir),
                    "--events-log",
                    str(self.events_log),
                ]
            )
            self.assertEqual(rc, 1)
            self.assertIn("CLAIM_REFUSED", err)
            self.assertFalse((self.intents_dir / "camera-agent" / f"bad-{bad_key}.json").exists())

        # 2. GUI intent from non-camera agent
        rc, _, err = _run_cli_quiet(
            [
                "enqueue-gui",
                "--agent",
                "modeler-agent",
                "--event-id",
                "modeler-bad-gui",
                "--keys",
                "KP_1",
                "--expect",
                "Should fail",
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc, 1)
        self.assertIn("CLAIM_REFUSED", err)

        # 3. Cross-prefix write_set refusal
        script_file = self.root / "bad_prefix.py"
        script_file.write_text("import bpy\n", encoding="utf-8")
        rc, _, err = _run_cli_quiet(
            [
                "enqueue-script",
                "--agent",
                "modeler-agent",
                "--event-id",
                "modeler-bad-prefix",
                "--write-set",
                "obj:PROP_Pedestal",
                "--expect",
                "Should fail",
                "--script-file",
                str(script_file),
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc, 1)
        self.assertIn("CLAIM_REFUSED", err)

        # 4. Negative control: refusing to write to SWARM_INBOX (/tmp test inbox only)
        test_inbox = self.root / "mock_inbox"
        test_inbox.mkdir(parents=True, exist_ok=True)
        old_env = os.environ.get("SWARM_INBOX")
        os.environ["SWARM_INBOX"] = str(test_inbox)
        try:
            rc, _, err = _run_cli_quiet(
                [
                    "enqueue-script",
                    "--agent",
                    "modeler-agent",
                    "--event-id",
                    "inbox-should-fail",
                    "--write-set",
                    "col:CHAR_mesh",
                    "--expect",
                    "Must refuse SWARM_INBOX",
                    "--script-file",
                    str(script_file),
                    "--intents-dir",
                    str(test_inbox),
                    "--frames-dir",
                    str(self.frames_dir),
                    "--events-log",
                    str(self.events_log),
                ]
            )
            self.assertEqual(rc, 1)
            self.assertIn("BOUNDARY_VIOLATION", err)
            self.assertEqual(list(test_inbox.iterdir()), [])
        finally:
            if old_env is None:
                os.environ.pop("SWARM_INBOX", None)
            else:
                os.environ["SWARM_INBOX"] = old_env

    def test_wait_event_exit_codes_and_m4_timeout(self) -> None:
        # M4: default --timeout-s must be 540
        parser = build_intent_parser()
        parsed_wait = parser.parse_args(["wait-event", "--event-id", "ev-1"])
        self.assertEqual(parsed_wait.timeout_s, DEFAULT_WAIT_TIMEOUT_S)
        self.assertEqual(DEFAULT_WAIT_TIMEOUT_S, 540.0)

        cases = [
            ("ev-ok", "STATE_VERIFIED", 0, {}),
            ("ev-gui", "VERIFIED", 0, {}),
            ("ev-spill", "VERIFIED_WITH_SPILL", 0, {}),
            ("ev-nvc", "NO_VISIBLE_CHANGE", 2, {}),
            ("ev-stale-parked", "STALE_FRAME", 3, {"drops": 3}),
            ("ev-dup", "DUPLICATE_EVENT", 4, {}),
            ("ev-qfull", "QUEUE_FULL", 4, {}),
            ("ev-uncertain-in", "INPUT_UNCERTAIN", 1, {}),
            ("ev-uncertain-out", "OUTCOME_UNCERTAIN", 1, {}),
            ("ev-refused", "CLAIM_REFUSED", 1, {}),
            ("ev-err", "JOB_ERROR", 1, {}),
            ("ev-evicted", "FRAME_EVICTED", 1, {}),
            ("ev-nolease", "NO_LEASE", 1, {}),
            ("ev-expired", "LEASE_EXPIRED", 1, {}),
        ]
        lines = [
            json.dumps({"v": 1, "rec": "close", "event_id": eid, "verdict": verdict, **extra})
            for eid, verdict, _, extra in cases
        ]
        self.events_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        for eid, _, expected_rc, _ in cases:
            rc, _, _ = _run_cli_quiet(
                [
                    "wait-event",
                    "--event-id",
                    eid,
                    "--events-log",
                    str(self.events_log),
                    "--intents-dir",
                    str(self.intents_dir),
                    "--timeout-s",
                    "2.0",
                ]
            )
            self.assertEqual(rc, expected_rc, f"{eid} expected rc={expected_rc}, got {rc}")

        # M4: wait-event timeout returns rc=124
        rc_to, _, err_to = _run_cli_quiet(
            [
                "wait-event",
                "--event-id",
                "never-appears",
                "--events-log",
                str(self.events_log),
                "--intents-dir",
                str(self.intents_dir),
                "--timeout-s",
                "0.05",
                "--poll-interval-s",
                "0.01",
            ]
        )
        self.assertEqual(rc_to, 124)
        self.assertIn("TIMEOUT", err_to)

    def test_b1_preexisting_verified_plus_fresh_duplicate_returns_rc4(self) -> None:
        # Prior run wrote VERIFIED for reused-id into append-only events.jsonl
        prior_close = {
            "v": 1,
            "rec": "close",
            "tick": 5,
            "event_id": "reused-id",
            "agent_id": "camera-agent",
            "lane": "gui",
            "verdict": "VERIFIED",
        }
        self.events_log.write_text(json.dumps(prior_close) + "\n", encoding="utf-8")

        rc, enq_out, _ = _run_cli_quiet(
            [
                "enqueue-gui",
                "--agent",
                "camera-agent",
                "--event-id",
                "reused-id",
                "--keys",
                "KP_1",
                "--expect",
                "Should not match prior run VERIFIED",
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc, 0)
        offset_bytes = json.loads(enq_out)["events_log_offset"]
        self.assertGreater(offset_bytes, 0)

        # Broker refuses reused-id with DUPLICATE_EVENT after enqueue
        dup_close = {
            "v": 1,
            "rec": "close",
            "tick": 5,
            "event_id": "reused-id",
            "agent_id": "camera-agent",
            "lane": "gui",
            "verdict": "DUPLICATE_EVENT",
        }
        with self.events_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dup_close) + "\n")

        rc_wait, out_wait, _ = _run_cli_quiet(
            [
                "wait-event",
                "--event-id",
                "reused-id",
                "--events-log",
                str(self.events_log),
                "--intents-dir",
                str(self.intents_dir),
                "--timeout-s",
                "2.0",
            ]
        )
        self.assertEqual(rc_wait, 4)
        self.assertEqual(json.loads(out_wait)["verdict"], "DUPLICATE_EVENT")

    def test_m3_wait_event_waits_through_stale_drops_lt_3(self) -> None:
        # Broker emits STALE_FRAME drops=1, drops=2 (requeued), then VERIFIED -> rc=0
        lines_ok = [
            json.dumps({"v": 1, "rec": "close", "event_id": "gui-requeue-ok", "verdict": "STALE_FRAME", "drops": 1}),
            json.dumps({"v": 1, "rec": "close", "event_id": "gui-requeue-ok", "verdict": "STALE_FRAME", "drops": 2}),
            json.dumps({"v": 1, "rec": "close", "event_id": "gui-requeue-ok", "verdict": "VERIFIED", "drops": 2}),
        ]
        # Broker emits STALE_FRAME drops=1, 2, 3 (parked) -> rc=3
        lines_park = [
            json.dumps({"v": 1, "rec": "close", "event_id": "gui-requeue-park", "verdict": "STALE_FRAME", "drops": 1}),
            json.dumps({"v": 1, "rec": "close", "event_id": "gui-requeue-park", "verdict": "STALE_FRAME", "drops": 2}),
            json.dumps({"v": 1, "rec": "close", "event_id": "gui-requeue-park", "verdict": "STALE_FRAME", "drops": 3}),
        ]
        self.events_log.write_text("\n".join(lines_ok + lines_park) + "\n", encoding="utf-8")

        rc1, out1, _ = _run_cli_quiet(
            [
                "wait-event",
                "--event-id",
                "gui-requeue-ok",
                "--events-log",
                str(self.events_log),
                "--intents-dir",
                str(self.intents_dir),
                "--timeout-s",
                "2.0",
            ]
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(json.loads(out1)["verdict"], "VERIFIED")

        rc2, out2, _ = _run_cli_quiet(
            [
                "wait-event",
                "--event-id",
                "gui-requeue-park",
                "--events-log",
                str(self.events_log),
                "--intents-dir",
                str(self.intents_dir),
                "--timeout-s",
                "2.0",
            ]
        )
        self.assertEqual(rc2, 3)
        parsed2 = json.loads(out2)
        self.assertEqual(parsed2["verdict"], "STALE_FRAME")
        self.assertEqual(parsed2["drops"], 3)

    def test_l2_k7_strict_event_id_fullmatch_and_bridge_filename_compat(self) -> None:
        bridge_job_re = re.compile(
            r"\A([0-9a-fA-F]+)__(\d+)__([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.py\Z"
        )
        script_file = self.root / "id_test.py"
        script_file.write_text("import bpy\n", encoding="utf-8")

        base_script_intent = {
            "v": 1,
            "event_id": "valid-id",
            "agent_id": "modeler-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": 12,
            "view_epoch": 2,
            "scene_epoch": 5,
            "lease_epoch": 1,
            "write_set": ["col:CHAR_mesh"],
            "target_region": None,
            "footprint": None,
            "expect": "Check event_id validation",
            "script": "import bpy\n",
            "script_sha256": hashlib.sha256(b"import bpy\n").hexdigest(),
            "keys": None,
            "max_acts": 1,
            "created_ts_ms": 1731580000000,
        }

        for bad_id in ("abc\n", "abc\nxyz", "a..b", "a b", "abc\r", ".leading"):
            with self.assertRaises(IntentValidationError):
                validate_event_id(bad_id)
            bad_payload = dict(base_script_intent, event_id=bad_id)
            with self.assertRaises(IntentValidationError):
                validate_intent_dict(bad_payload)

            rc_scr, _, err_scr = _run_cli_quiet(
                [
                    "enqueue-script",
                    "--agent",
                    "modeler-agent",
                    "--event-id",
                    bad_id,
                    "--write-set",
                    "col:CHAR_mesh",
                    "--expect",
                    "Must reject invalid event_id",
                    "--script-file",
                    str(script_file),
                    "--intents-dir",
                    str(self.intents_dir),
                    "--frames-dir",
                    str(self.frames_dir),
                    "--events-log",
                    str(self.events_log),
                ]
            )
            self.assertNotEqual(rc_scr, 0, f"enqueue-script unexpectedly accepted {bad_id!r}")
            self.assertIn("VALIDATION_ERROR", err_scr)
            mod_dir = self.intents_dir / "modeler-agent"
            self.assertFalse(mod_dir.exists() and any(mod_dir.rglob("*")))

            rc_gui, _, err_gui = _run_cli_quiet(
                [
                    "enqueue-gui",
                    "--agent",
                    "camera-agent",
                    "--event-id",
                    bad_id,
                    "--keys",
                    "KP_1",
                    "--expect",
                    "Must reject invalid event_id",
                    "--intents-dir",
                    str(self.intents_dir),
                    "--frames-dir",
                    str(self.frames_dir),
                    "--events-log",
                    str(self.events_log),
                ]
            )
            self.assertNotEqual(rc_gui, 0, f"enqueue-gui unexpectedly accepted {bad_id!r}")
            self.assertIn("VALIDATION_ERROR", err_gui)
            cam_dir = self.intents_dir / "camera-agent"
            self.assertFalse(cam_dir.exists() and any(cam_dir.rglob("*")))

        max_128_id = "A" + ("b1._-" * 25) + "cd"
        self.assertEqual(len(max_128_id), 128)
        accepted_ids = [
            max_128_id,
            "take1-modeler-agent-m1",
            "take1-camera-agent-gui-1",
            "run_99.step-2",
        ]
        for good_id in accepted_ids:
            self.assertEqual(validate_event_id(good_id), good_id)
            self.assertIsNotNone(
                bridge_job_re.fullmatch(f"deadbeef01234567__12__{good_id}.py"),
                f"{good_id!r} must match bridge job filename regex",
            )
            rc_ok, _, err_ok = _run_cli_quiet(
                [
                    "enqueue-script",
                    "--agent",
                    "modeler-agent",
                    "--event-id",
                    good_id,
                    "--write-set",
                    "col:CHAR_mesh",
                    "--expect",
                    "Valid event_id accepted",
                    "--script-file",
                    str(script_file),
                    "--intents-dir",
                    str(self.intents_dir),
                    "--frames-dir",
                    str(self.frames_dir),
                    "--events-log",
                    str(self.events_log),
                ]
            )
            self.assertEqual(rc_ok, 0, f"enqueue-script failed for {good_id!r}: {err_ok}")

    def test_k2_binary_newline_framing_handles_torn_multibyte_utf8(self) -> None:
        claim_bytes = (
            json.dumps(
                {
                    "v": 1,
                    "rec": "claim",
                    "agent_id": "camera-agent",
                    "lease_id": "lease-cam-k2",
                    "lease_epoch": 4,
                    "ttl_ms": 300000,
                    "issued_ts_ms": 1731580000000,
                    "expires_ts_ms": 1731580300000,
                    "allow_gui": True,
                    "write_prefixes": ["cam:", "render:"],
                }
            ).encode("utf-8")
            + b"\n"
        )
        torn_other = (
            json.dumps(
                {
                    "v": 1,
                    "rec": "close",
                    "event_id": "k2-other",
                    "agent_id": "camera-agent",
                    "lane": "gui",
                    "verdict": "VERIFIED",
                    "reason": "prior ✓",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        cut_other = torn_other.index(b"\xe2\x9c\x93") + 1
        self.events_log.write_bytes(claim_bytes + torn_other[:cut_other])

        # read_events_records, observe, and enqueue-gui must ignore the torn tail without UnicodeDecodeError
        records = read_events_records(self.events_log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["lease_id"], "lease-cam-k2")

        rc_obs, out_obs, err_obs = _run_cli_quiet(
            [
                "observe",
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
                "--agent",
                "camera-agent",
            ]
        )
        self.assertEqual(rc_obs, 0, err_obs)
        self.assertEqual(json.loads(out_obs)["lease_epoch"], 4)

        rc_enq, _, err_enq = _run_cli_quiet(
            [
                "enqueue-gui",
                "--agent",
                "camera-agent",
                "--event-id",
                "k2-enq-while-torn",
                "--keys",
                "KP_1",
                "--expect",
                "Ignore torn UTF-8 tail",
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc_enq, 0, err_enq)

        # Now enqueue a fresh event cleanly, append a torn close containing a 3-byte UTF-8 char
        # cut 1 byte in, run wait-event in a thread, and complete the remaining bytes + '\n'.
        self.events_log.write_bytes(claim_bytes)
        rc_enq2, _, err_enq2 = _run_cli_quiet(
            [
                "enqueue-gui",
                "--agent",
                "camera-agent",
                "--event-id",
                "k2-wait-torn",
                "--keys",
                "KP_1",
                "--expect",
                "Wait across torn UTF-8 close",
                "--intents-dir",
                str(self.intents_dir),
                "--frames-dir",
                str(self.frames_dir),
                "--events-log",
                str(self.events_log),
            ]
        )
        self.assertEqual(rc_enq2, 0, err_enq2)

        full_close = (
            json.dumps(
                {
                    "v": 1,
                    "rec": "close",
                    "event_id": "k2-wait-torn",
                    "agent_id": "camera-agent",
                    "lane": "gui",
                    "verdict": "VERIFIED",
                    "reason": "done ✓",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        cut_close = full_close.index(b"\xe2\x9c\x93") + 1
        with self.events_log.open("ab") as fh:
            fh.write(full_close[:cut_close])

        wait_result: dict[str, object] = {}

        def _run_wait() -> None:
            rc_w, out_w, err_w = _run_cli_quiet(
                [
                    "wait-event",
                    "--event-id",
                    "k2-wait-torn",
                    "--events-log",
                    str(self.events_log),
                    "--intents-dir",
                    str(self.intents_dir),
                    "--agent",
                    "camera-agent",
                    "--timeout-s",
                    "5.0",
                    "--poll-interval-s",
                    "0.05",
                ]
            )
            wait_result["rc"] = rc_w
            wait_result["out"] = out_w
            wait_result["err"] = err_w

        t = threading.Thread(target=_run_wait, daemon=True)
        t.start()
        time.sleep(0.25)
        with self.events_log.open("ab") as fh:
            fh.write(full_close[cut_close:])
        t.join(timeout=5.0)

        self.assertFalse(t.is_alive())
        self.assertEqual(wait_result.get("rc"), 0, wait_result)
        receipt = json.loads(str(wait_result.get("out", "")))
        self.assertEqual(receipt["verdict"], "VERIFIED")
        self.assertEqual(receipt["reason"], "done ✓")


class TestRunnerDryRun(unittest.TestCase):
    def test_runner_dry_run_exact_agy_flag_order_and_6digit_tag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm_h2_runner_") as tmpdir:
            root = Path(tmpdir)
            proof_dir = root / "proof"
            briefs_dir = root / "briefs"
            summary = run_agents(
                roles=list(CANONICAL_ROLES),
                model="gemini-4-hydrogen-high",
                print_timeout="1800s",
                run_tag=None,
                proof_dir=proof_dir,
                briefs_dir=briefs_dir,
                dry_run=True,
            )
            tag = summary["run_tag"]
            self.assertRegex(tag, r"^\d{6}$")
            self.assertEqual(summary["mode"], "dry_run")
            self.assertEqual(len(summary["agents"]), 4)
            for item in summary["agents"]:
                role = item["role"]
                argv = item["argv"]
                expected_unit = scope_unit_name(tag, role)
                self.assertEqual(
                    argv[:10],
                    [
                        "systemd-run",
                        "--user",
                        "--scope",
                        f"--unit={expected_unit}",
                        "-q",
                        "-p",
                        "MemoryHigh=6G",
                        "-p",
                        "MemoryMax=8G",
                        "-p",
                    ],
                )
                self.assertEqual(argv[10], "MemorySwapMax=0")
                self.assertEqual(argv[11], AGY_BIN)
                self.assertNotIn("shims/agy", argv[11])
                self.assertEqual(
                    argv[12:-1],
                    [
                        "--model",
                        "gemini-4-hydrogen-high",
                        "--dangerously-skip-permissions",
                        "--output-format",
                        "stream-json",
                        "--log-file",
                        str(proof_dir / f"run_{tag}_{role}.cli.log"),
                        "--print-timeout",
                        "1800s",
                        "-p",
                    ],
                )
                self.assertEqual(argv[-2], "-p")
                self.assertTrue(argv[-1].startswith(SHARED_BRIEF_PREFIX))
                self.assertTrue(Path(item["brief_path"]).is_file())
                env = build_agent_env(role, base_env={})
                self.assertEqual(env["SWARM_AGENT_ID"], role)
                self.assertIn("GOMEMLIMIT", env)

    def test_m1_scope_prefix_enforces_8gib_memory_max(self) -> None:
        unit = f"vpswarm-agent-testm1-{os.getpid()}"
        probe = (
            "import os, pathlib; "
            "cg = pathlib.Path(f'/proc/{os.getpid()}/cgroup').read_text().strip().split(':')[-1]; "
            "print(pathlib.Path('/sys/fs/cgroup' + cg + '/memory.max').read_text().strip())"
        )
        cmd = [*build_scope_prefix(unit), sys.executable, "-c", probe]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0, check=True)
        self.assertEqual(proc.stdout.strip(), "8589934592")

    def test_m6_kill_recorded_pids_checks_starttime_ticks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm_h2_kill_") as tmpdir:
            proof_dir = Path(tmpdir)
            # 1. Mismatched starttime_ticks must NEVER kill the process
            safe_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                safe_ticks = read_proc_starttime_ticks(safe_proc.pid)
                self.assertIsNotNone(safe_ticks)
                assert safe_ticks is not None
                (proof_dir / "run_120001_props-agent.pid").write_text(f"{safe_proc.pid}\n", encoding="utf-8")
                (proof_dir / "run_120001_props-agent.proc.json").write_text(
                    json.dumps(
                        {
                            "pid": safe_proc.pid,
                            "starttime_ticks": safe_ticks + 999999,
                            "unit": "vpswarm-agent-120001-props-agent",
                        }
                    ),
                    encoding="utf-8",
                )
                mismatch_outcomes = kill_recorded_pids(proof_dir, run_tag="120001", grace_s=0.5)
                self.assertEqual(len(mismatch_outcomes), 1)
                self.assertEqual(mismatch_outcomes[0]["status"], "skipped_starttime_mismatch")
                self.assertIsNone(safe_proc.poll(), "Mismatched starttime_ticks process must remain alive")
            finally:
                if safe_proc.poll() is None:
                    safe_proc.kill()
                    safe_proc.wait(timeout=2.0)

            # 2. Matching starttime_ticks kills the process and cleans up .pid/.proc.json
            target_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                target_ticks = read_proc_starttime_ticks(target_proc.pid)
                self.assertIsNotNone(target_ticks)
                pid_file = proof_dir / "run_120002_modeler-agent.pid"
                proc_file = proof_dir / "run_120002_modeler-agent.proc.json"
                pid_file.write_text(f"{target_proc.pid}\n", encoding="utf-8")
                proc_file.write_text(
                    json.dumps(
                        {
                            "pid": target_proc.pid,
                            "starttime_ticks": target_ticks,
                            "unit": "",
                        }
                    ),
                    encoding="utf-8",
                )
                outcomes = kill_recorded_pids(proof_dir, run_tag="120002", grace_s=1.0)
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["pid"], target_proc.pid)
                self.assertEqual(outcomes[0]["status"], "signaled")
                target_proc.wait(timeout=3.0)
            finally:
                if target_proc.poll() is None:
                    target_proc.kill()
                    target_proc.wait(timeout=2.0)

    def test_m6_run_agents_removes_pid_and_proc_json_on_clean_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm_h2_clean_") as tmpdir:
            root = Path(tmpdir)
            proof_dir = root / "proof"
            briefs_dir = root / "briefs"
            fake_cmd = [sys.executable, "-c", "print('agent_done')"]
            with mock.patch("swarm.agents.runner.build_agent_argv", return_value=fake_cmd):
                summary = run_agents(
                    roles=["modeler-agent"],
                    run_tag="120501",
                    proof_dir=proof_dir,
                    briefs_dir=briefs_dir,
                    dry_run=False,
                )
            self.assertEqual(summary["overall_returncode"], 0)
            self.assertFalse((proof_dir / "run_120501_modeler-agent.pid").exists())
            self.assertFalse((proof_dir / "run_120501_modeler-agent.proc.json").exists())
            self.assertTrue(Path(summary["summary_path"]).is_file())


class TestScriptedSmoke(unittest.TestCase):
    def test_m2_disjoint_scripts_no_cube_or_cross_claim_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm_h2_m2_") as tmpdir:
            plan = build_smoke_plan(num_agents=4, output_dir=Path(tmpdir) / "out")

            for step in plan["modeler-agent"]:
                script = step["script"]
                self.assertNotIn("Cube", script)
                self.assertNotIn("PROP_", script)
                self.assertNotIn("scene.eevee", script)
                self.assertNotIn("scene.render", script)

            for step in plan["props-agent"]:
                script = step["script"]
                self.assertNotIn("Cube", script)
                self.assertNotIn("CHAR_", script)
                self.assertNotIn("scene.eevee", script)
                self.assertNotIn("scene.render", script)

            for step in plan["look-agent"]:
                script = step["script"]
                self.assertNotIn("Cube", script)
                self.assertNotIn("CHAR_", script)
                self.assertNotIn("PROP_", script)
                self.assertNotIn("scene.eevee", script)
                self.assertNotIn("scene.render", script)

            for step in plan["camera-agent"]:
                if step["lane"] == "script":
                    script = step["script"]
                    if "scene.render.engine" in script:
                        self.assertIn("render:engine", step["write_set"])
                    if "save_as_mainfile" in script:
                        self.assertIn("copy=True", script)
                else:
                    self.assertTrue(step.get("after_builders"), "GUI steps must wait for builders")

    def test_smoke_plan_scripts_valid_python_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm_h2_smoke_dry_") as tmpdir:
            out_dir = Path(tmpdir) / "out"
            for n_agents in (2, 4):
                plan = build_smoke_plan(num_agents=n_agents, output_dir=out_dir)
                for _, steps in plan.items():
                    for step in steps:
                        if step["lane"] == "script":
                            ast.parse(step["script"])
                res = run_smoke(
                    num_agents=n_agents,
                    output_dir=out_dir,
                    frames_dir=Path(tmpdir) / "frames",
                    intents_dir=Path(tmpdir) / "intents",
                    events_log=Path(tmpdir) / "events.jsonl",
                    run_id=f"dry-{n_agents}",
                    dry_run=True,
                )
                self.assertEqual(res["mode"], "dry_run")
                self.assertGreater(res["intents_validated"], 0)
                for step_info in res["steps"]:
                    self.assertTrue(
                        step_info["event_id"].startswith(f"dry-{n_agents}-"),
                        f"Expected run-scoped event_id, got {step_info['event_id']!r}",
                    )

    def test_smoke_live_against_mock_broker_with_stale_rebase(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm_h2_smoke_live_") as tmpdir:
            root = Path(tmpdir)
            frames_dir = root / "frames"
            intents_dir = root / "intents"
            events_log = root / "events.jsonl"
            output_dir = root / "output"
            _write_frame(frames_dir, frame=5, view_epoch=1, scene_epoch=1)
            events_log.write_text("", encoding="utf-8")

            stop_flag = threading.Event()
            seen_events: set[str] = set()
            fifos: dict[str, list[dict[str, object]]] = {r: [] for r in CANONICAL_ROLES}
            frame_seq = 5

            def mock_broker() -> None:
                nonlocal frame_seq
                tick = 0
                while not stop_flag.is_set():
                    for role in CANONICAL_ROLES:
                        rdir = intents_dir / role
                        if rdir.is_dir():
                            for fpath in sorted(rdir.glob("*.json")):
                                if fpath.name.startswith("."):
                                    continue
                                data = json.loads(fpath.read_text(encoding="utf-8"))
                                eid = data["event_id"]
                                if eid in seen_events:
                                    continue
                                seen_events.add(eid)
                                data["_drops"] = 0
                                fifos[role].append(data)

                    for role in CANONICAL_ROLES:
                        if not fifos[role]:
                            continue
                        data = fifos[role].pop(0)
                        eid = str(data["event_id"])
                        if data["lane"] == "gui" and int(data["based_on_frame"]) < frame_seq:
                            drops = int(data["_drops"]) + 1
                            data["_drops"] = drops
                            if drops < 3:
                                fifos[role].append(data)
                            rec = {
                                "v": 1,
                                "rec": "close",
                                "tick": tick,
                                "event_id": eid,
                                "agent_id": role,
                                "lane": "gui",
                                "verdict": "STALE_FRAME",
                                "drops": drops,
                            }
                        elif data["lane"] == "gui":
                            tick += 1
                            frame_seq += 1
                            _write_frame(frames_dir, frame=frame_seq, view_epoch=2, scene_epoch=frame_seq)
                            rec = {
                                "v": 1,
                                "rec": "close",
                                "tick": tick,
                                "event_id": eid,
                                "agent_id": role,
                                "lane": "gui",
                                "verdict": "VERIFIED",
                                "drops": int(data["_drops"]),
                            }
                        else:
                            tick += 1
                            frame_seq += 1
                            _write_frame(frames_dir, frame=frame_seq, view_epoch=1, scene_epoch=frame_seq)
                            rec = {
                                "v": 1,
                                "rec": "close",
                                "tick": tick,
                                "event_id": eid,
                                "agent_id": role,
                                "lane": "script",
                                "verdict": "STATE_VERIFIED",
                            }
                        with events_log.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps(rec) + "\n")
                    time.sleep(0.03)

            t = threading.Thread(target=mock_broker, daemon=True)
            t.start()
            try:
                res = run_smoke(
                    num_agents=4,
                    output_dir=output_dir,
                    frames_dir=frames_dir,
                    intents_dir=intents_dir,
                    events_log=events_log,
                    run_id="mock4",
                    timeout_s=15.0,
                    demo_stale_rebase=True,
                    dry_run=False,
                )
            finally:
                stop_flag.set()
                t.join(timeout=2.0)

            self.assertEqual(res["mode"], "live")
            cam_results = res["results"]["camera-agent"]
            gui_step = cam_results[1]
            self.assertEqual(len(gui_step["stale_retries"]), 1)
            self.assertEqual(gui_step["stale_retries"][0]["verdict"], "STALE_FRAME")
            self.assertEqual(gui_step["stale_retries"][0]["drops"], 3)
            self.assertEqual(gui_step["receipt"]["verdict"], "VERIFIED")
            self.assertEqual(gui_step["event_id"], "mock4-camera-agent-c2-gui-r1")

    def test_l1_relative_output_dir_resolves_against_repo_root_from_tmp_cwd(self) -> None:
        expected_out = str((REPO_ROOT / "swarm/run/output").resolve())
        old_cwd = os.getcwd()
        os.chdir("/tmp")
        try:
            self.assertEqual(str(resolve_output_dir("swarm/run/output")), expected_out)
            plan = build_smoke_plan(num_agents=4, output_dir=Path("swarm/run/output"))
            render_steps = [s for s in plan["camera-agent"] if s.get("suffix") == "c4-render"]
            self.assertEqual(len(render_steps), 1)
            script = render_steps[0]["script"]
            tree = ast.parse(script)
            out_dir_vals = [
                ast.literal_eval(node.value)
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "out_dir" for t in node.targets)
            ]
            self.assertEqual(out_dir_vals, [expected_out])

            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = smoke_main(
                    ["--agents", "4", "--output-dir", "swarm/run/output", "--dry-run"]
                )
            self.assertEqual(rc, 0)
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
