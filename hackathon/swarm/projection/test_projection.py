"""Unit and integration test suite for swarm.projection.

Tests:
1. OpenSwitchboard §7.1 act mappings and prove every refute carries --ref <event_id>.
2. Real-MCP-subprocess tests with bridge.room.mcp for all 4 agent IDs (post_plan, post_step, post_status, close_task).
3. Negative control: look-agent with post_plan + post_step but NO close_task triggers MISSING_CLOSE_TASK (rc 1), and clean (rc 0) when close_task is posted.
4. Incremental cursor-state persistence preventing duplicate posts.
5. Mandatory regressions:
   - M1: close_task returning isError: True triggers MISSING_CLOSE_TASK rc == 1 even in Projector.finalize.
   - M2: control stop pauses tasks, resume resumes tasks, post_step keeps task open until --finalize.
   - m1: 14 closes roll over cleanly without overwriting step 11.
   - m3: \r in broker reason sanitized so osb succeeds.
   - m5: partial line without \n held safely until \n appended.
   - m6: per-event reconcile ordering.
   - m4 & m2: non-empty ref on null event_id and park target extraction from reason.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from swarm.projection.projector import (
    ADMITTED_VERDICTS,
    DEFAULT_OSB_BIN,
    REFUSAL_VERDICTS,
    TRACKED_AGENTS,
    DryRunSink,
    Projector,
    ProjectionState,
    TaskLightMcpSink,
    main as projector_main,
    sanitize_osb_text,
)
from swarm.projection.reconciler import (
    Reconciler,
    audit_events,
)

REPO_PATH = os.environ.get("VP_CW_REPO", "FireBase_BuildHybridAI/repo")
MCP_AVAILABLE = Path(REPO_PATH).is_dir()


class TestOsbActMapping(unittest.TestCase):
    """Verifies every CONTRACT.md §7.1 OSB act mapping and refute --ref presence."""

    def setUp(self) -> None:
        self.sink = DryRunSink()
        self.tmpdir = tempfile.mkdtemp()
        self.events_path = Path(self.tmpdir) / "events.jsonl"
        self.events_path.touch()
        self.projector = Projector(
            events_file=self.events_path,
            sink=self.sink,
            osb_room="test-capsule",
            cw_room="viewpoint",
        )

    def tearDown(self) -> None:
        self.projector.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_auto_open_room(self) -> None:
        """On initialization, the projector ensures room opened."""
        open_calls = [c for c in self.sink.osb_calls if c["act"] == "open"]
        self.assertEqual(len(open_calls), 1)
        self.assertEqual(open_calls[0]["room"], "test-capsule")
        self.assertEqual(open_calls[0]["agent"], "broker-projector")
        self.assertEqual(open_calls[0]["text"], "ViewPoint Swarm capsule projection room")

    def test_enqueue_mapping(self) -> None:
        """rec == 'enqueue' -> post act=claim, --ref <event_id>."""
        rec = {
            "v": 1,
            "rec": "enqueue",
            "event_id": "modeler-agent-7",
            "agent_id": "modeler-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": 41,
        }
        self.projector.process_record(rec)
        calls = [c for c in self.sink.osb_calls if c["act"] == "claim"]
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["agent"], "modeler-agent")
        self.assertEqual(c["ref"], "modeler-agent-7")
        self.assertIn("enqueue modeler-agent-7 (script/job) frame=41", c["text"])

        # Also verify TaskLight post_plan was triggered for first enqueue
        plan_calls = [c for c in self.sink.mcp_calls if c["tool"] == "post_plan"]
        self.assertEqual(len(plan_calls), 1)
        self.assertEqual(plan_calls[0]["agent"], "modeler-agent")

    def test_turn_ack_and_token_transfer_handoff(self) -> None:
        """rec == 'turn' -> ack; consecutive turns with prev_agent != next_agent -> handoff."""
        rec1 = {
            "v": 1,
            "rec": "turn",
            "turn": 1,
            "turn_epoch": 1,
            "agent_id": "modeler-agent",
            "head_frame": 10,
            "hold_ms": 60000,
        }
        rec2 = {
            "v": 1,
            "rec": "turn",
            "turn": 2,
            "turn_epoch": 2,
            "agent_id": "props-agent",
            "head_frame": 11,
            "hold_ms": 60000,
        }
        self.projector.process_record(rec1)
        self.projector.process_record(rec2)

        # Acks
        ack_calls = [c for c in self.sink.osb_calls if c["act"] == "ack"]
        self.assertEqual(len(ack_calls), 2)
        self.assertEqual(ack_calls[0]["agent"], "broker-projector")
        self.assertEqual(ack_calls[0]["ref"], "turn-1")
        self.assertEqual(ack_calls[1]["agent"], "broker-projector")
        self.assertEqual(ack_calls[1]["ref"], "turn-2")

        # Handoff on turn 2
        handoff_calls = [c for c in self.sink.osb_calls if c["act"] == "handoff"]
        self.assertEqual(len(handoff_calls), 1)
        h = handoff_calls[0]
        self.assertEqual(h["agent"], "modeler-agent")
        self.assertEqual(h["ref"], "turn-2")
        self.assertIn("handoff -> props-agent (turn_epoch=2)", h["text"])

    def test_close_admitted_verdicts(self) -> None:
        """Admitted verdicts -> evidence with --ref <event_id>."""
        for verdict in sorted(ADMITTED_VERDICTS):
            self.sink.osb_calls.clear()
            rec = {
                "v": 1,
                "rec": "close",
                "tick": 42,
                "turn": 3,
                "event_id": f"evt-{verdict.lower()}",
                "agent_id": "camera-agent",
                "lane": "gui",
                "verdict": verdict,
                "verify_frame_ref": 44,
            }
            self.projector.process_record(rec)
            evid_calls = [c for c in self.sink.osb_calls if c["act"] == "evidence"]
            self.assertEqual(len(evid_calls), 1, f"Failed for verdict {verdict}")
            c = evid_calls[0]
            self.assertEqual(c["agent"], "camera-agent")
            self.assertEqual(c["ref"], f"evt-{verdict.lower()}")
            self.assertIn(f"verdict={verdict}", c["text"])
            self.assertIn("tick=42", c["text"])
            self.assertIn("verify_frame=44", c["text"])

    def test_close_all_refusal_verdicts_and_refute_ref_presence(self) -> None:
        """Every refusal verdict must emit act=refute AND MUST carry non-empty --ref <event_id>."""
        self.assertEqual(len(REFUSAL_VERDICTS), 13)
        for verdict in sorted(REFUSAL_VERDICTS):
            self.sink.osb_calls.clear()
            event_id = f"refuse-{verdict.lower()}"
            rec = {
                "v": 1,
                "rec": "close",
                "tick": 10,
                "turn": 5,
                "event_id": event_id,
                "agent_id": "look-agent",
                "lane": "script",
                "verdict": verdict,
                "drops": 1,
                "reason": f"test refusal {verdict}",
            }
            self.projector.process_record(rec)
            refute_calls = [c for c in self.sink.osb_calls if c["act"] == "refute"]
            self.assertEqual(len(refute_calls), 1, f"Missing refute for {verdict}")
            c = refute_calls[0]
            self.assertEqual(c["agent"], "broker-projector")
            # PROVE: --ref MUST ALWAYS be passed on refute
            self.assertIsNotNone(c["ref"], f"--ref missing on refute for {verdict}")
            self.assertTrue(len(str(c["ref"]).strip()) > 0, f"--ref empty for {verdict}")
            self.assertEqual(c["ref"], event_id)
            self.assertIn(f"verdict={verdict}", c["text"])
            self.assertIn("drops=1", c["text"])

    def test_control_op_park(self) -> None:
        """control op='park' -> refute with non-empty --ref."""
        rec = {
            "v": 1,
            "rec": "control",
            "op": "park",
            "agent_id": "look-agent",
            "reason": "two consecutive timeouts",
            "drops": 3,
        }
        self.projector.process_record(rec)
        refute_calls = [c for c in self.sink.osb_calls if c["act"] == "refute"]
        self.assertEqual(len(refute_calls), 1)
        c = refute_calls[0]
        self.assertEqual(c["agent"], "broker-projector")
        self.assertTrue(len(str(c["ref"]).strip()) > 0)
        self.assertEqual(c["ref"], "look-agent")
        self.assertIn("refute look-agent", c["text"])

    def test_control_stop_and_resume(self) -> None:
        """control stop/resume -> verdict with ref control-<op>."""
        rec_stop = {
            "v": 1,
            "rec": "control",
            "op": "stop",
            "by": "director-agent",
            "reason": "Capsule run completed",
        }
        self.projector.process_record(rec_stop)
        calls = [c for c in self.sink.osb_calls if c["act"] == "verdict"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["agent"], "director-agent")
        self.assertEqual(calls[0]["ref"], "control-stop")
        self.assertIn("control op=stop reason=Capsule run completed", calls[0]["text"])

    def test_skips_noisy_pass_and_cursor(self) -> None:
        """Cursor and noisy PASS records must be skipped."""
        self.sink.osb_calls.clear()
        self.sink.mcp_calls.clear()

        # Cursor
        rec_cursor = {
            "v": 1,
            "rec": "cursor",
            "agent_id": "camera-agent",
            "phase": "result",
            "x": 800,
            "y": 500,
        }
        self.projector.process_record(rec_cursor)
        self.assertEqual(len(self.sink.osb_calls), 0)
        self.assertEqual(len(self.sink.mcp_calls), 0)

        # PASS close
        rec_pass = {
            "v": 1,
            "rec": "close",
            "tick": 0,
            "turn": 1,
            "event_id": None,
            "agent_id": "modeler-agent",
            "verdict": "PASS",
        }
        self.projector.process_record(rec_pass)
        self.assertEqual(len(self.sink.osb_calls), 0)
        self.assertEqual(len(self.sink.mcp_calls), 0)

        # PASS turn (hold_ms == 0)
        rec_pass_turn = {
            "v": 1,
            "rec": "turn",
            "turn": 1,
            "turn_epoch": 1,
            "agent_id": "modeler-agent",
            "head_frame": 1,
            "hold_ms": 0,
        }
        self.projector.process_record(rec_pass_turn)
        self.assertEqual(len(self.sink.osb_calls), 0)
        self.assertEqual(len(self.sink.mcp_calls), 0)


@unittest.skipUnless(MCP_AVAILABLE, "bridge.room.mcp repo not available")
class TestTaskLightRealMcp(unittest.TestCase):
    """Subprocess integration tests against real bridge.room.mcp MCP server."""

    def setUp(self) -> None:
        self.tmp_cw = tempfile.mkdtemp()
        self.sink = TaskLightMcpSink(cw_home=self.tmp_cw, repo_path=REPO_PATH)

    def tearDown(self) -> None:
        self.sink.close()
        shutil.rmtree(self.tmp_cw, ignore_errors=True)

    def test_all_four_agents_succeed(self) -> None:
        """Verifies post_plan, post_step, post_status, close_task succeed (isError: False) for all 4 agents."""
        for agent_id in TRACKED_AGENTS:
            # 1. post_plan
            plan_res = self.sink.post_plan(
                room="viewpoint",
                agent=agent_id,
                title=f"Plan for {agent_id}",
                steps=[
                    {"text": f"Step 1 for {agent_id}", "accept": "Accepted step 1"},
                    {"text": f"Step 2 for {agent_id}", "accept": "Accepted step 2"},
                ],
            )
            self.assertFalse(plan_res.get("isError"), f"post_plan failed for {agent_id}: {plan_res}")

            # 2. post_status
            status_res = self.sink.post_status(
                room="viewpoint",
                agent=agent_id,
                text="Acting on turn #1",
            )
            self.assertFalse(status_res.get("isError"), f"post_status failed for {agent_id}: {status_res}")

            # 3. post_step (done)
            step_res1 = self.sink.post_step(
                room="viewpoint",
                agent=agent_id,
                index=0,
                state="done",
                outcome=f"{agent_id}-1: STATE_VERIFIED",
                took="45ms",
            )
            self.assertFalse(step_res1.get("isError"), f"post_step (done) failed for {agent_id}: {step_res1}")

            # 4. post_step (blocked)
            step_res2 = self.sink.post_step(
                room="viewpoint",
                agent=agent_id,
                index=1,
                state="blocked",
                outcome=f"{agent_id}-2: STALE_FRAME",
            )
            self.assertFalse(step_res2.get("isError"), f"post_step (blocked) failed for {agent_id}: {step_res2}")

            # 5. close_task
            close_res = self.sink.close_task(
                room="viewpoint",
                agent=agent_id,
                outcome=f"Successfully completed capsule tasks for {agent_id}",
            )
            self.assertFalse(close_res.get("isError"), f"close_task failed for {agent_id}: {close_res}")

        # Check the resulting events file
        events_file = Path(self.tmp_cw) / "rooms" / "viewpoint" / "events.jsonl"
        self.assertTrue(events_file.is_file())
        with open(events_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        # 4 agents * 5 calls = 20 events
        self.assertEqual(len(lines), 20)
        acts = {line["act"] for line in lines}
        self.assertEqual(acts, {"plan", "heartbeat", "step", "close"})


class TestCursorStatePersistence(unittest.TestCase):
    """Verifies incremental cursor offset tracking and restarts without double-posting."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.events_path = Path(self.tmpdir) / "events.jsonl"
        self.state_path = Path(self.tmpdir) / "cursor_state.json"
        self.sink = DryRunSink()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cursor_offset_prevents_duplicate_posts(self) -> None:
        # Write 2 events
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "enqueue", "event_id": "modeler-1",
                "agent_id": "modeler-agent", "lane": "script", "kind": "job",
            }) + "\n")
            f.write(json.dumps({
                "v": 1, "rec": "turn", "turn": 1, "agent_id": "modeler-agent",
                "hold_ms": 60000,
            }) + "\n")

        p1 = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=self.sink,
        )
        p1.run_once()
        p1.close()

        initial_calls_count = len(self.sink.osb_calls)
        self.assertTrue(initial_calls_count > 0)
        self.assertTrue(self.state_path.is_file())

        state_data = json.loads(self.state_path.read_text())
        self.assertEqual(state_data["line_number"], 2)
        self.assertTrue(state_data["offset_bytes"] > 0)

        # Run projector again without new events
        self.sink.osb_calls.clear()
        p2 = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=self.sink,
        )
        p2.run_once()
        p2.close()

        # Zero new events posted
        self.assertEqual(len(self.sink.osb_calls), 0)

        # Now append 1 event and verify ONLY the new event is processed
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "close", "event_id": "modeler-1",
                "agent_id": "modeler-agent", "verdict": "STATE_VERIFIED",
            }) + "\n")

        p3 = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=self.sink,
        )
        p3.run_once()
        p3.close()

        evid_calls = [c for c in self.sink.osb_calls if c["act"] == "evidence"]
        self.assertEqual(len(evid_calls), 1)
        self.assertEqual(evid_calls[0]["ref"], "modeler-1")


class TestReconcilerNegativeControl(unittest.TestCase):
    """Negative control and anomaly detection tests for Reconciler."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.events_path = Path(self.tmpdir) / "events.jsonl"
        self.cw_home = Path(self.tmpdir) / "cw_home"
        self.cw_room_dir = self.cw_home / "rooms" / "viewpoint"
        self.cw_room_dir.mkdir(parents=True, exist_ok=True)
        self.cw_events_path = self.cw_room_dir / "events.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_negative_control_missing_close_task(self) -> None:
        """Negative control: look-agent with post_plan + post_step but NO close_task
        flags MISSING_CLOSE_TASK and rc == 1; once close_task is posted, rc == 0!
        """
        # 1. Authoritative broker events has tasks for modeler-agent and look-agent
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "enqueue", "event_id": "modeler-1",
                "agent_id": "modeler-agent", "lane": "script", "kind": "job",
            }) + "\n")
            f.write(json.dumps({
                "v": 1, "rec": "enqueue", "event_id": "look-1",
                "agent_id": "look-agent", "lane": "script", "kind": "job",
            }) + "\n")

        # 2. TaskLight has:
        # - modeler-agent: plan, step, close
        # - look-agent: plan, step (NO close!)
        with open(self.cw_events_path, "w", encoding="utf-8") as f:
            # modeler-agent
            f.write(json.dumps({
                "id": "e-1", "lamport": 1, "ts": 1000, "room": "viewpoint",
                "agent": "modeler-agent", "act": "plan",
                "plan": {"title": "Modeler plan", "steps": [{"text": "s1"}]},
                "text": "Modeler plan",
            }) + "\n")
            f.write(json.dumps({
                "id": "e-2", "lamport": 2, "ts": 1001, "room": "viewpoint",
                "agent": "modeler-agent", "act": "step",
                "step": {"index": 0, "state": "done", "outcome": "modeler-1: STATE_VERIFIED"},
                "text": "done",
            }) + "\n")
            f.write(json.dumps({
                "id": "e-3", "lamport": 3, "ts": 1002, "room": "viewpoint",
                "agent": "modeler-agent", "act": "close",
                "text": "Completed successfully",
            }) + "\n")

            # look-agent: plan and step only!
            f.write(json.dumps({
                "id": "e-4", "lamport": 4, "ts": 1003, "room": "viewpoint",
                "agent": "look-agent", "act": "plan",
                "plan": {"title": "Look plan", "steps": [{"text": "s1"}]},
                "text": "Look plan",
            }) + "\n")
            f.write(json.dumps({
                "id": "e-5", "lamport": 5, "ts": 1004, "room": "viewpoint",
                "agent": "look-agent", "act": "step",
                "step": {"index": 0, "state": "done", "outcome": "look-1: STATE_VERIFIED"},
                "text": "done",
            }) + "\n")

        # 3. Run reconciler audit -> MUST flag look-agent with MISSING_CLOSE_TASK
        reconciler = Reconciler(
            events_file=self.events_path,
            cw_home=self.cw_home,
            cw_room="viewpoint",
        )
        report = reconciler.audit()
        self.assertFalse(report.clean, "Expected audit to fail with anomalies")
        self.assertEqual(report.status, "ANOMALIES_DETECTED")

        missing_close = [a for a in report.anomalies if a.get("code") == "MISSING_CLOSE_TASK"]
        self.assertEqual(len(missing_close), 1)
        self.assertEqual(missing_close[0]["agent_id"], "look-agent")

        # Check CLI exit code is rc == 1
        cli_res = subprocess.run(
            [
                sys.executable,
                "-m",
                "swarm.projection.reconciler",
                "--events",
                str(self.events_path),
                "--cw-home",
                str(self.cw_home),
                "--cw-room",
                "viewpoint",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(cli_res.returncode, 1, f"Expected rc 1, got {cli_res.returncode}")

        # 4. Now post close_task for look-agent
        with open(self.cw_events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": "e-6", "lamport": 6, "ts": 1005, "room": "viewpoint",
                "agent": "look-agent", "act": "close",
                "text": "Completed look tasks",
            }) + "\n")

        # 5. Run reconciler again -> MUST be clean (rc == 0)
        report2 = reconciler.audit()
        self.assertTrue(report2.clean, f"Expected clean audit, got anomalies: {report2.anomalies}")
        self.assertEqual(report2.status, "CLEAN")
        self.assertEqual(len(report2.anomalies), 0)

        cli_res2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "swarm.projection.reconciler",
                "--events",
                str(self.events_path),
                "--cw-home",
                str(self.cw_home),
                "--cw-room",
                "viewpoint",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(cli_res2.returncode, 0, f"Expected rc 0, got {cli_res2.returncode}")

    def test_unreconciled_uncertain_event_detection(self) -> None:
        """Unreconciled INPUT_UNCERTAIN flags UNRECONCILED_UNCERTAIN_EVENT."""
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "close", "event_id": "cam-timeout",
                "agent_id": "camera-agent", "verdict": "INPUT_UNCERTAIN",
            }) + "\n")

        reconciler = Reconciler(
            events_file=self.events_path,
            cw_home=self.cw_home,
            cw_room="viewpoint",
        )
        report = reconciler.audit()
        uncertain_anomalies = [a for a in report.anomalies if a.get("code") == "UNRECONCILED_UNCERTAIN_EVENT"]
        self.assertEqual(len(uncertain_anomalies), 1)
        self.assertEqual(uncertain_anomalies[0]["event_id"], "cam-timeout")

        # Now append control reconcile record
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "control", "op": "reconcile", "by": "director-agent",
            }) + "\n")

        report2 = reconciler.audit()
        uncertain_anomalies2 = [a for a in report2.anomalies if a.get("code") == "UNRECONCILED_UNCERTAIN_EVENT"]
        self.assertEqual(len(uncertain_anomalies2), 0)


class TestEndToEndProjectorPipeline(unittest.TestCase):
    """End-to-end test verifying full session projection, finalization, and reconciliation."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.events_path = Path(self.tmpdir) / "events.jsonl"
        self.state_path = Path(self.tmpdir) / "state.json"
        self.cw_home = Path(self.tmpdir) / "cw_home"
        self.sink = DryRunSink(cw_home=self.cw_home)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_capsule_lifecycle_and_finalize(self) -> None:
        # Simulate a full capsule session with all 4 agents
        records = [
            # 1. Enqueue across all 4 agents
            {"v": 1, "rec": "enqueue", "event_id": "mod-1", "agent_id": "modeler-agent", "lane": "script", "kind": "job", "based_on_frame": 1},
            {"v": 1, "rec": "enqueue", "event_id": "prop-1", "agent_id": "props-agent", "lane": "script", "kind": "job", "based_on_frame": 1},
            {"v": 1, "rec": "enqueue", "event_id": "look-1", "agent_id": "look-agent", "lane": "script", "kind": "job", "based_on_frame": 1},
            {"v": 1, "rec": "enqueue", "event_id": "cam-1", "agent_id": "camera-agent", "lane": "gui", "kind": "key", "based_on_frame": 1},
            # 2. Modeler turn & close
            {"v": 1, "rec": "turn", "turn": 1, "turn_epoch": 1, "agent_id": "modeler-agent", "head_frame": 1, "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 1, "turn": 1, "event_id": "mod-1", "agent_id": "modeler-agent", "verdict": "STATE_VERIFIED", "verify_frame_ref": 2},
            # 3. Props turn & close
            {"v": 1, "rec": "turn", "turn": 2, "turn_epoch": 2, "agent_id": "props-agent", "head_frame": 2, "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 2, "turn": 2, "event_id": "prop-1", "agent_id": "props-agent", "verdict": "STATE_VERIFIED", "verify_frame_ref": 3},
            # 4. Look turn & close
            {"v": 1, "rec": "turn", "turn": 3, "turn_epoch": 3, "agent_id": "look-agent", "head_frame": 3, "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 3, "turn": 3, "event_id": "look-1", "agent_id": "look-agent", "verdict": "STATE_VERIFIED", "verify_frame_ref": 4},
            # 5. Camera turn & close
            {"v": 1, "rec": "turn", "turn": 4, "turn_epoch": 4, "agent_id": "camera-agent", "head_frame": 4, "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 4, "turn": 4, "event_id": "cam-1", "agent_id": "camera-agent", "verdict": "VERIFIED", "verify_frame_ref": 5},
            # 6. Director control stop (M2: pauses tasks, does NOT close)
            {"v": 1, "rec": "control", "op": "stop", "by": "director-agent", "reason": "Target frames captured"},
        ]

        with open(self.events_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        projector = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=self.sink,
            cw_home=self.cw_home,
        )
        lines_processed = projector.run_once()
        self.assertEqual(lines_processed, len(records))

        # Check all 4 agents received post_plan
        plan_agents = {c["agent"] for c in self.sink.mcp_calls if c["tool"] == "post_plan"}
        self.assertEqual(plan_agents, set(TRACKED_AGENTS))

        # Check all 4 agents received post_step
        step_agents = {c["agent"] for c in self.sink.mcp_calls if c["tool"] == "post_step"}
        self.assertEqual(step_agents, set(TRACKED_AGENTS))

        # M2: Verify control op="stop" paused active agents with post_status, NO close_task before finalize
        paused_status = [c for c in self.sink.mcp_calls if c["tool"] == "post_status" and "Paused" in c["text"]]
        self.assertEqual(len(paused_status), 4)
        close_calls_before_finalize = [c for c in self.sink.mcp_calls if c["tool"] == "close_task"]
        self.assertEqual(len(close_calls_before_finalize), 0)

        # Run finalize to execute close_task and reconciliation audit
        report = projector.finalize()

        # Check finalize executed close_task for all 4 agents
        closed_agents = {c["agent"] for c in self.sink.mcp_calls if c["tool"] == "close_task"}
        self.assertEqual(closed_agents, set(TRACKED_AGENTS))

        self.assertTrue(report.clean, f"Expected clean report: {report.anomalies}")
        self.assertEqual(report.status, "CLEAN")
        self.assertEqual(len(report.anomalies), 0)
        self.assertEqual(report.stats["total_broker_events"], len(records))
        projector.close()


class TestMandatoryRepairRegressions(unittest.TestCase):
    """Must-pass regression suite covering all P4_H4 Major and Minor findings."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.events_path = Path(self.tmpdir) / "events.jsonl"
        self.state_path = Path(self.tmpdir) / "state.json"
        self.cw_home = Path(self.tmpdir) / "cw_home"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_m1_close_task_error_triggers_missing_close_task_in_finalize(self) -> None:
        """M1: If close_task returns isError: True, the agent is NOT marked closed,
        and projector.finalize() produces MISSING_CLOSE_TASK (rc 1)."""
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "enqueue", "event_id": "props-1",
                "agent_id": "props-agent", "lane": "script", "kind": "job",
            }) + "\n")
            f.write(json.dumps({
                "v": 1, "rec": "turn", "turn": 1, "agent_id": "props-agent", "hold_ms": 60000,
            }) + "\n")
            f.write(json.dumps({
                "v": 1, "rec": "close", "tick": 1, "turn": 1,
                "event_id": "props-1", "agent_id": "props-agent",
                "verdict": "STATE_VERIFIED",
            }) + "\n")

        # Sink that simulates close_task failure with isError: True
        sink = DryRunSink(cw_home=self.cw_home, simulate_close_error=True)
        projector = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=sink,
            cw_home=self.cw_home,
        )
        projector.run_once()
        report = projector.finalize()
        projector.close()

        self.assertFalse(report.clean, "Expected audit to fail when close_task returns isError")
        self.assertEqual(report.status, "ANOMALIES_DETECTED")
        missing_close = [a for a in report.anomalies if a.get("code") == "MISSING_CLOSE_TASK"]
        self.assertEqual(len(missing_close), 1)
        self.assertEqual(missing_close[0]["agent_id"], "props-agent")
        self.assertNotIn("props-agent", projector.state.closed_agents)

    def test_m2_stop_resume_post_step_lifecycle(self) -> None:
        """M2: control op="stop" pauses active tasks with post_status; op="resume" resumes them;
        subsequent post_step calls succeed on still-open task, and close_task is deferred to finalize."""
        records = [
            {"v": 1, "rec": "enqueue", "event_id": "mod-1", "agent_id": "modeler-agent", "lane": "script", "kind": "job"},
            {"v": 1, "rec": "turn", "turn": 1, "turn_epoch": 1, "agent_id": "modeler-agent", "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 1, "turn": 1, "event_id": "mod-1", "agent_id": "modeler-agent", "verdict": "STATE_VERIFIED"},
            # Director pauses capsule
            {"v": 1, "rec": "control", "op": "stop", "by": "director-agent", "reason": "operator pause"},
            # Director resumes capsule
            {"v": 1, "rec": "control", "op": "resume", "by": "director-agent", "reason": "operator resume"},
            # Modeler performs another step on the still-open task
            {"v": 1, "rec": "enqueue", "event_id": "mod-2", "agent_id": "modeler-agent", "lane": "script", "kind": "job"},
            {"v": 1, "rec": "turn", "turn": 2, "turn_epoch": 2, "agent_id": "modeler-agent", "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 2, "turn": 2, "event_id": "mod-2", "agent_id": "modeler-agent", "verdict": "STATE_VERIFIED"},
        ]
        with open(self.events_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        sink = DryRunSink(cw_home=self.cw_home)
        projector = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=sink,
            cw_home=self.cw_home,
        )
        projector.run_once()

        # At this point, close_task MUST NOT have been called yet
        close_calls_before_finalize = [c for c in sink.mcp_calls if c["tool"] == "close_task"]
        self.assertEqual(len(close_calls_before_finalize), 0)

        # Verify pause and resume statuses were emitted
        statuses = [c["text"] for c in sink.mcp_calls if c["tool"] == "post_status"]
        self.assertIn("Paused: broker control stop", statuses)
        self.assertIn("Resumed: broker control resume", statuses)

        # Verify 2 steps were posted
        step_calls = [c for c in sink.mcp_calls if c["tool"] == "post_step"]
        self.assertEqual(len(step_calls), 2)
        self.assertEqual(step_calls[0]["index"], 0)
        self.assertEqual(step_calls[1]["index"], 1)

        # Now finalize capsule
        report = projector.finalize()
        projector.close()

        # close_task was called in finalize
        close_calls_after_finalize = [c for c in sink.mcp_calls if c["tool"] == "close_task"]
        self.assertEqual(len(close_calls_after_finalize), 1)
        self.assertTrue(report.clean, f"Expected clean audit: {report.anomalies}")

    def test_m1_twelve_step_rollover(self) -> None:
        """m1: When an agent exceeds 12 steps (index >= 12), a continuation post_plan
        is issued with title '(cont. #2)' and step index resets to 0, preventing step 11 overwrite."""
        records = [
            {"v": 1, "rec": "enqueue", "event_id": "mod-init", "agent_id": "modeler-agent", "lane": "script", "kind": "job"}
        ]
        # 14 close receipts
        for i in range(14):
            records.append({
                "v": 1, "rec": "close", "tick": i + 1, "turn": i + 1,
                "event_id": f"mod-{i + 1}", "agent_id": "modeler-agent",
                "verdict": "STATE_VERIFIED",
            })

        with open(self.events_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        sink = DryRunSink(cw_home=self.cw_home)
        projector = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=sink,
            cw_home=self.cw_home,
        )
        projector.run_once()
        projector.close()

        plan_calls = [c for c in sink.mcp_calls if c["tool"] == "post_plan"]
        self.assertEqual(len(plan_calls), 2, f"Expected 2 plans (initial + continuation), got {len(plan_calls)}")
        self.assertIn("(cont. #2)", plan_calls[1]["title"])

        step_calls = [c for c in sink.mcp_calls if c["tool"] == "post_step"]
        self.assertEqual(len(step_calls), 14)
        indices = [c["index"] for c in step_calls]
        # Should be [0..11, 0, 1] - NOT saturating at 11!
        expected_indices = list(range(12)) + [0, 1]
        self.assertEqual(indices, expected_indices)

    def test_m3_osb_text_sanitization(self) -> None:
        """m3: Control characters like \\r, \\x00, \\x07 are stripped/replaced, and
        text length is capped at <= 3800 bytes before calling osb post."""
        dirty_text = "error\rwith\rcarriage\r\nreturn\x00and\x07bell" + ("X" * 5000)
        cleaned = sanitize_osb_text(dirty_text)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertIn("\n", cleaned)
        self.assertLessEqual(len(cleaned.encode("utf-8")), 3800)

        # Test through projector record
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "close", "tick": 1, "turn": 1,
                "event_id": "err-event", "agent_id": "look-agent",
                "verdict": "JOB_ERROR",
                "reason": "Traceback:\r\n  File 'bad.py', line 1\rError\x00\x08",
            }) + "\n")

        sink = DryRunSink()
        projector = Projector(events_file=self.events_path, sink=sink)
        projector.run_once()
        projector.close()

        refute_calls = [c for c in sink.osb_calls if c["act"] == "refute"]
        self.assertEqual(len(refute_calls), 1)
        refute_text = refute_calls[0]["text"]
        self.assertNotIn("\r", refute_text)
        self.assertNotIn("\x00", refute_text)
        self.assertLessEqual(len(refute_text.encode("utf-8")), 3800)

    def test_m5_partial_trailing_line_safety(self) -> None:
        """m5: Incomplete trailing line without \\n is held safely without advancing offset,
        and completes cleanly when flushed with \\n on subsequent poll."""
        # Write line 1 complete, line 2 partial without \n
        line1 = json.dumps({"v": 1, "rec": "enqueue", "event_id": "evt-1", "agent_id": "modeler-agent", "lane": "script", "kind": "job"}) + "\n"
        line2_part = json.dumps({"v": 1, "rec": "turn", "turn": 1, "agent_id": "modeler-agent", "hold_ms": 60000})[:25]

        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(line1)
            f.write(line2_part)

        sink = DryRunSink()
        projector = Projector(events_file=self.events_path, state_file=self.state_path, sink=sink)
        processed = projector.run_once()
        self.assertEqual(processed, 1, "Only the complete line should be processed")
        claim_calls = [c for c in sink.osb_calls if c["act"] == "claim"]
        self.assertEqual(len(claim_calls), 1)
        ack_calls_before = [c for c in sink.osb_calls if c["act"] == "ack"]
        self.assertEqual(len(ack_calls_before), 0)
        self.assertEqual(projector.state.offset_bytes, len(line1.encode("utf-8")))

        # Complete the partial line and flush with \n
        remaining_line2 = json.dumps({"v": 1, "rec": "turn", "turn": 1, "agent_id": "modeler-agent", "hold_ms": 60000})[25:] + "\n"
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(remaining_line2)

        # Run projector again
        processed2 = projector.run_once()
        projector.close()
        self.assertEqual(processed2, 1, "Completed line should now be processed cleanly")
        ack_calls = [c for c in sink.osb_calls if c["act"] == "ack"]
        self.assertEqual(len(ack_calls), 1)

    def test_m6_per_event_reconcile_ordering(self) -> None:
        """m6: An explicit control reconcile clears prior uncertain events;
        uncertain events arriving AFTER reconcile are not cleared, and resume does not clear them."""
        records = [
            # Event 1: uncertain
            {"v": 1, "rec": "close", "event_id": "unc-1", "agent_id": "camera-agent", "verdict": "INPUT_UNCERTAIN", "ts_ms": 100},
            # Explicit reconcile clears Event 1
            {"v": 1, "rec": "control", "op": "reconcile", "by": "director-agent", "ts_ms": 200},
            # Event 2: uncertain (arrives AFTER reconcile)
            {"v": 1, "rec": "close", "event_id": "unc-2", "agent_id": "props-agent", "verdict": "OUTCOME_UNCERTAIN", "ts_ms": 300},
            # Resume arrives (must NOT clear Event 2)
            {"v": 1, "rec": "control", "op": "resume", "by": "operator", "ts_ms": 400},
        ]
        with open(self.events_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        reconciler = Reconciler(events_file=self.events_path, cw_home=self.cw_home)
        report = reconciler.audit()

        uncertain_anomalies = [a for a in report.anomalies if a.get("code") == "UNRECONCILED_UNCERTAIN_EVENT"]
        # Only unc-2 should be flagged; unc-1 was cleared by reconcile, and resume did NOT clear unc-2!
        self.assertEqual(len(uncertain_anomalies), 1)
        self.assertEqual(uncertain_anomalies[0]["event_id"], "unc-2")

    def test_m4_and_m2_ref_on_null_id_and_park_parsing(self) -> None:
        """m4 & m2: Non-PASS refusal with null event_id gets synthetic ref refusal-turn-{turn}-{agent_id}.
        Control park parses target from reason e.g. parked_after_3_stale_drops:<event_id>."""
        with open(self.events_path, "w", encoding="utf-8") as f:
            # Null event_id refusal
            f.write(json.dumps({
                "v": 1, "rec": "close", "tick": 5, "turn": 7,
                "event_id": None, "agent_id": "look-agent",
                "verdict": "NO_LEASE",
                "reason": "lease expired",
            }) + "\n")
            # Park with target in reason
            f.write(json.dumps({
                "v": 1, "rec": "control", "op": "park",
                "by": "broker",
                "reason": "parked_after_3_stale_drops:cam-act-42",
            }) + "\n")

        sink = DryRunSink()
        projector = Projector(events_file=self.events_path, sink=sink)
        projector.run_once()
        projector.close()

        refute_calls = [c for c in sink.osb_calls if c["act"] == "refute"]
        self.assertEqual(len(refute_calls), 2)
        # 1. Null event_id gets synthetic ref
        self.assertEqual(refute_calls[0]["ref"], "refusal-turn-7-look-agent")
        # 2. Park parses target from reason
        self.assertEqual(refute_calls[1]["ref"], "cam-act-42")

    def test_m1_state_room_mismatch_anomaly(self) -> None:
        """M1: If projection state claims an agent is closed, but TaskLight room lacks
        act == 'close', reconciler flags both STATE_ROOM_MISMATCH and MISSING_CLOSE_TASK."""
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "v": 1, "rec": "enqueue", "event_id": "cam-1",
                "agent_id": "camera-agent", "lane": "gui", "kind": "key",
            }) + "\n")

        # Fake state where camera-agent is falsely claimed closed
        state = ProjectionState(
            planned_agents={"camera-agent"},
            closed_agents={"camera-agent"},
        )
        reconciler = Reconciler(
            events_file=self.events_path,
            cw_home=self.cw_home,
            state=state,
        )
        report = reconciler.audit()
        self.assertFalse(report.clean)
        codes = [a["code"] for a in report.anomalies]
        self.assertIn("STATE_ROOM_MISMATCH", codes)
        self.assertIn("MISSING_CLOSE_TASK", codes)

    def test_m8_idle_run_once_does_not_save_state(self) -> None:
        """m8: run_once does not save or fsync state if 0 lines processed and offset unchanged."""
        # Touch empty events file
        self.events_path.touch()
        # Save initial state file
        initial_state = ProjectionState(offset_bytes=0, line_number=0)
        initial_state.save(self.state_path)
        mtime_before = os.path.getmtime(self.state_path)

        projector = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=DryRunSink(),
        )
        processed = projector.run_once()
        projector.close()
        self.assertEqual(processed, 0)
        mtime_after = os.path.getmtime(self.state_path)
        self.assertEqual(mtime_before, mtime_after, "State file was rewritten during idle poll")

    def test_n6_osb_sink_scrubs_osb_lane(self) -> None:
        """n6: OsbSink._env scrubs OSB_LANE so lane caps/policies are not inherited."""
        from swarm.projection.projector import OsbSink
        old_val = os.environ.get("OSB_LANE")
        try:
            os.environ["OSB_LANE"] = "1"
            sink = OsbSink()
            env = sink._env()
            self.assertNotIn("OSB_LANE", env)
        finally:
            if old_val is not None:
                os.environ["OSB_LANE"] = old_val
            else:
                os.environ.pop("OSB_LANE", None)


class TestH4RepairLoop2Regressions(unittest.TestCase):
    """Must-pass regressions for H4_r2 (P4_H4_r1 Probes STALE, P3, P4b, r1-5)."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.events_path = Path(self.tmpdir) / "events.jsonl"
        self.state_path = Path(self.tmpdir) / "state.json"
        self.cw_home = Path(self.tmpdir) / "cw_home"
        self.cw_room_dir = self.cw_home / "rooms" / "viewpoint"
        self.cw_room_dir.mkdir(parents=True, exist_ok=True)
        self.cw_events_path = self.cw_room_dir / "events.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_w0_history(self) -> None:
        """Seed cw_home viewpoint room with earlier W0 plan + close for modeler-agent and props-agent."""
        w0_events = [
            {
                "id": "e-0001", "lamport": 1, "ts": 1000, "room": "viewpoint",
                "agent": "modeler-agent", "act": "plan",
                "plan": {"title": "W0 probe plan", "steps": [{"text": "W0 step"}]},
                "text": "W0 probe plan for modeler-agent",
            },
            {
                "id": "e-0002", "lamport": 2, "ts": 1001, "room": "viewpoint",
                "agent": "modeler-agent", "act": "close",
                "text": "W0 probe ok for modeler-agent",
            },
            {
                "id": "e-0003", "lamport": 3, "ts": 1002, "room": "viewpoint",
                "agent": "props-agent", "act": "plan",
                "plan": {"title": "W0 probe plan", "steps": [{"text": "W0 step"}]},
                "text": "W0 probe plan for props-agent",
            },
            {
                "id": "e-0004", "lamport": 4, "ts": 1003, "room": "viewpoint",
                "agent": "props-agent", "act": "close",
                "text": "W0 probe passed for props-agent",
            },
        ]
        with open(self.cw_events_path, "w", encoding="utf-8") as f:
            for ev in w0_events:
                f.write(json.dumps(ev) + "\n")

    def _write_new_session_events(self) -> None:
        """Write a new broker session where props-agent and look-agent enqueue and close a step."""
        records = [
            {"v": 1, "rec": "enqueue", "event_id": "props-new-1", "agent_id": "props-agent", "lane": "script", "kind": "job"},
            {"v": 1, "rec": "enqueue", "event_id": "look-new-1", "agent_id": "look-agent", "lane": "script", "kind": "job"},
            {"v": 1, "rec": "turn", "turn": 1, "turn_epoch": 1, "agent_id": "props-agent", "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 1, "turn": 1, "event_id": "props-new-1", "agent_id": "props-agent", "verdict": "STATE_VERIFIED"},
            {"v": 1, "rec": "turn", "turn": 2, "turn_epoch": 2, "agent_id": "look-agent", "hold_ms": 60000},
            {"v": 1, "rec": "close", "tick": 2, "turn": 2, "event_id": "look-new-1", "agent_id": "look-agent", "verdict": "STATE_VERIFIED"},
        ]
        with open(self.events_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_probe_stale_r1_blocks_stale_close_and_propagates_finalize_errors(self) -> None:
        """Probe STALE (R1): Earlier W0 close MUST NOT mask a failed close_task on a new plan.
        finalize() attempts close_task for ALL open agents, emits both CLOSE_TASK_REFUSED
        and MISSING_CLOSE_TASK, sets report.clean == False, and main() returns rc != 0."""
        self._seed_w0_history()
        self._write_new_session_events()

        sink = DryRunSink(cw_home=self.cw_home, error_agents={"props-agent"})
        projector = Projector(
            events_file=self.events_path,
            state_file=self.state_path,
            sink=sink,
            cw_home=self.cw_home,
        )
        projector.run_once()
        report = projector.finalize()
        projector.close()

        # close_task attempted for ALL open agents (both look-agent and props-agent)
        close_attempts = [c["agent"] for c in sink.mcp_calls if c["tool"] == "close_task"]
        self.assertEqual(set(close_attempts), {"look-agent", "props-agent"})

        self.assertFalse(report.clean)
        self.assertEqual(report.status, "ANOMALIES_DETECTED")
        codes = [a["code"] for a in report.anomalies]
        self.assertIn("CLOSE_TASK_REFUSED", codes)
        self.assertIn("MISSING_CLOSE_TASK", codes)

        refused = [a for a in report.anomalies if a["code"] == "CLOSE_TASK_REFUSED"]
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0]["details"].startswith("CLOSE_TASK_REFUSED:"))
        self.assertIn("props-agent", refused[0]["details"])

        missing = [a for a in report.anomalies if a["code"] == "MISSING_CLOSE_TASK"]
        self.assertEqual([m["agent_id"] for m in missing], ["props-agent"])

        # Verify main() returns non-zero (rc != 0) when close_task fails in seeded W0 room
        state_path_cli = Path(self.tmpdir) / "state_cli.json"
        self._seed_w0_history()
        orig_close_task = DryRunSink.close_task

        def failing_props_close(sink_self: DryRunSink, room: str, agent: str, outcome: str) -> dict:
            if agent == "props-agent":
                sink_self.mcp_calls.append({"tool": "close_task", "room": room, "agent": agent, "outcome": outcome})
                return {
                    "content": [{"type": "text", "text": "close_task refused (simulated error)"}],
                    "isError": True,
                    "error": "close_task simulated failure",
                }
            return orig_close_task(sink_self, room, agent, outcome)

        with patch.object(DryRunSink, "close_task", failing_props_close):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = projector_main([
                    "--events-file", str(self.events_path),
                    "--state-file", str(state_path_cli),
                    "--cw-home", str(self.cw_home),
                    "--sink", "dry-run",
                    "--finalize",
                ])
        self.assertNotEqual(rc, 0)
        cli_report = json.loads(buf.getvalue())
        self.assertFalse(cli_report["clean"])
        cli_codes = [a["code"] for a in cli_report["anomalies"]]
        self.assertIn("CLOSE_TASK_REFUSED", cli_codes)
        self.assertIn("MISSING_CLOSE_TASK", cli_codes)

    @unittest.skipUnless(MCP_AVAILABLE, "bridge.room.mcp repo not available")
    def test_probe_stale_r1_real_mcp_read_only_refusal(self) -> None:
        """Probe STALE (R1) against real bridge.room.mcp: seeded W0 room + new plan,
        then room events.jsonl made read-only before --finalize -> rc != 0,
        CLOSE_TASK_REFUSED + MISSING_CLOSE_TASK."""
        self._seed_w0_history()
        self._write_new_session_events()

        rc_once = projector_main([
            "--events-file", str(self.events_path),
            "--state-file", str(self.state_path),
            "--cw-home", str(self.cw_home),
            "--sink", "mcp",
            "--once",
        ])
        self.assertEqual(rc_once, 0)

        os.chmod(self.cw_events_path, 0o444)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_fin = projector_main([
                    "--events-file", str(self.events_path),
                    "--state-file", str(self.state_path),
                    "--cw-home", str(self.cw_home),
                    "--sink", "mcp",
                    "--finalize",
                ])
        finally:
            os.chmod(self.cw_events_path, 0o644)

        self.assertNotEqual(rc_fin, 0)
        cli_report = json.loads(buf.getvalue())
        self.assertFalse(cli_report["clean"])
        cli_codes = [a["code"] for a in cli_report["anomalies"]]
        self.assertIn("CLOSE_TASK_REFUSED", cli_codes)
        self.assertIn("MISSING_CLOSE_TASK", cli_codes)

    def test_probe_stale_r1_clean_when_close_succeeds_after_new_plan(self) -> None:
        """Probe STALE (R1) positive control: When close_task succeeds AFTER the new plan
        in a seeded W0 room, report.clean == True and main() returns rc == 0."""
        self._seed_w0_history()
        self._write_new_session_events()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = projector_main([
                "--events-file", str(self.events_path),
                "--state-file", str(self.state_path),
                "--cw-home", str(self.cw_home),
                "--sink", "dry-run",
                "--finalize",
            ])
        self.assertEqual(rc, 0)
        cli_report = json.loads(buf.getvalue())
        self.assertTrue(cli_report["clean"], f"Expected clean report, got: {cli_report}")
        self.assertEqual(cli_report["status"], "CLEAN")
        self.assertEqual(cli_report["anomalies"], [])

    def test_r1_5_finalize_open_tasks_exception_isolation(self) -> None:
        """r1-5: A JSON-RPC/transport exception on one agent's close_task MUST be recorded
        in last_finalize_errors and MUST NOT prevent close_task attempts on remaining agents."""
        self._seed_w0_history()
        records = [
            {"v": 1, "rec": "enqueue", "event_id": "look-1", "agent_id": "look-agent", "lane": "script", "kind": "job"},
            {"v": 1, "rec": "enqueue", "event_id": "mod-1", "agent_id": "modeler-agent", "lane": "script", "kind": "job"},
            {"v": 1, "rec": "enqueue", "event_id": "props-1", "agent_id": "props-agent", "lane": "script", "kind": "job"},
        ]
        with open(self.events_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        attempted_agents: list[str] = []
        orig_close_task = DryRunSink.close_task

        def mixed_close(sink_self: DryRunSink, room: str, agent: str, outcome: str) -> dict:
            attempted_agents.append(agent)
            if agent == "look-agent":
                raise RuntimeError("MCP call_tool close_task error: {'code': -32000, 'message': 'simulated RPC failure'}")
            if agent == "modeler-agent":
                return {
                    "content": [{"type": "text", "text": "close_task refused"}],
                    "isError": True,
                }
            return orig_close_task(sink_self, room, agent, outcome)

        with patch.object(DryRunSink, "close_task", mixed_close):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = projector_main([
                    "--events-file", str(self.events_path),
                    "--state-file", str(self.state_path),
                    "--cw-home", str(self.cw_home),
                    "--sink", "dry-run",
                    "--finalize",
                ])

        self.assertNotEqual(rc, 0)
        # All 3 open agents received a close_task attempt despite look-agent raising
        self.assertEqual(set(attempted_agents), {"look-agent", "modeler-agent", "props-agent"})

        cli_report = json.loads(buf.getvalue())
        self.assertFalse(cli_report["clean"])
        refused = [a for a in cli_report["anomalies"] if a["code"] == "CLOSE_TASK_REFUSED"]
        missing = [a["agent_id"] for a in cli_report["anomalies"] if a["code"] == "MISSING_CLOSE_TASK"]
        self.assertEqual(len(refused), 2)
        self.assertEqual(set(missing), {"look-agent", "modeler-agent"})

    def test_probe_p3_r1_2_r1_3_del_and_lone_surrogate_sanitization(self) -> None:
        """Probe P3 (r1-2 + r1-3): sanitize_osb_text strips DEL (U+007F) and replaces lone
        UTF-16 surrogates (\\ud800) with U+FFFD without UnicodeEncodeError, passes osb.text_error,
        and per-record projection guards prevent malformed strings from aborting run_once()."""
        dirty = "prefix\x7fmiddle\ud800\udfffsuffix\r\nline2\x00" + ("A" * 4500)
        cleaned = sanitize_osb_text(dirty)
        self.assertNotIn("\x7f", cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertIn("\ufffd", cleaned)
        self.assertLessEqual(len(cleaned.encode("utf-8")), 3800)

        # Verify against real osb.text_error if osb binary is present
        osb_path = Path(DEFAULT_OSB_BIN)
        if osb_path.is_file():
            loader = importlib.machinery.SourceFileLoader("osb_validation_probe", str(osb_path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            self.assertIsNotNone(spec)
            assert spec is not None
            osb_mod = importlib.util.module_from_spec(spec)
            loader.exec_module(osb_mod)
            self.assertIsNone(osb_mod.text_error(cleaned))
            self.assertIsNone(osb_mod.text_error(sanitize_osb_text("\x7f")))
            self.assertIsNone(osb_mod.text_error(sanitize_osb_text("\ud800")))

        # Feed raw JSON containing escaped lone surrogate \\ud800 and \\u007f + a subsequent normal record
        raw_bad_line = (
            '{"v":1,"rec":"close","tick":1,"turn":1,"event_id":"err-surr",'
            '"agent_id":"look-agent","verdict":"JOB_ERROR","reason":"bad\\u007fchar\\ud800here"}\n'
        )
        good_line = json.dumps({
            "v": 1, "rec": "close", "tick": 2, "turn": 2,
            "event_id": "ok-after", "agent_id": "modeler-agent", "verdict": "STATE_VERIFIED",
        }) + "\n"
        with open(self.events_path, "wb") as f:
            f.write(raw_bad_line.encode("utf-8"))
            f.write(good_line.encode("utf-8"))

        sink = DryRunSink(cw_home=self.cw_home)
        projector = Projector(events_file=self.events_path, state_file=self.state_path, sink=sink, cw_home=self.cw_home)
        processed = projector.run_once()
        projector.close()

        self.assertEqual(processed, 2)
        refute_calls = [c for c in sink.osb_calls if c["act"] == "refute"]
        evid_calls = [c for c in sink.osb_calls if c["act"] == "evidence"]
        self.assertEqual(len(refute_calls), 1)
        self.assertNotIn("\x7f", refute_calls[0]["text"])
        self.assertIn("\ufffd", refute_calls[0]["text"])
        self.assertEqual(len(evid_calls), 1)
        self.assertEqual(evid_calls[0]["ref"], "ok-after")

        # Also verify per-record exception isolation in run_once if process_record raises on one record
        class PoisonFirstSink(DryRunSink):
            def post_osb(self, room: str, agent: str, act: str, text: str, ref: str | None = None) -> None:
                if ref == "poison-1":
                    raise ValueError("simulated malformed record failure")
                super().post_osb(room, agent, act, text, ref)

        events_path2 = Path(self.tmpdir) / "events_iso.jsonl"
        with open(events_path2, "w", encoding="utf-8") as f:
            f.write(json.dumps({"v": 1, "rec": "enqueue", "event_id": "poison-1", "agent_id": "look-agent", "lane": "script", "kind": "job"}) + "\n")
            f.write(json.dumps({"v": 1, "rec": "enqueue", "event_id": "healthy-2", "agent_id": "modeler-agent", "lane": "script", "kind": "job"}) + "\n")

        sink2 = PoisonFirstSink(cw_home=self.cw_home)
        p2 = Projector(events_file=events_path2, sink=sink2, cw_home=self.cw_home)
        processed2 = p2.run_once()
        p2.close()
        self.assertEqual(processed2, 2)
        claims = [c for c in sink2.osb_calls if c["act"] == "claim"]
        self.assertEqual([c["ref"] for c in claims], ["healthy-2"])

    def test_probe_p4b_r1_4_binary_framing_multibyte_utf8_split(self) -> None:
        """Probe P4b (r1-4): Writing half of a 3-byte/4-byte UTF-8 character without \\n
        holds offset_bytes unchanged with 0 lines processed and zero UnicodeDecodeError;
        completing the character + \\n processes and projects the event exactly once."""
        for label, utf8_char, split_len in [
            ("3-byte", "€", 1),
            ("4-byte", "🚀", 2),
        ]:
            with self.subTest(label=label):
                epath = Path(self.tmpdir) / f"events_{label}.jsonl"
                spath = Path(self.tmpdir) / f"state_{label}.json"
                prefix = b'{"v":1,"rec":"close","tick":1,"turn":1,"event_id":"utf8-' + label.encode("ascii") + b'","agent_id":"look-agent","verdict":"JOB_ERROR","reason":"split-'
                char_bytes = utf8_char.encode("utf-8")
                suffix = b'-end"}\n'

                first_half = prefix + char_bytes[:split_len]
                second_half = char_bytes[split_len:] + suffix

                with open(epath, "wb") as f:
                    f.write(first_half)

                sink = DryRunSink(cw_home=self.cw_home)
                projector = Projector(events_file=epath, state_file=spath, sink=sink, cw_home=self.cw_home)
                processed1 = projector.run_once()
                self.assertEqual(processed1, 0)
                self.assertEqual(projector.state.offset_bytes, 0)
                self.assertEqual([c for c in sink.osb_calls if c["act"] == "refute"], [])

                with open(epath, "ab") as f:
                    f.write(second_half)

                processed2 = projector.run_once()
                self.assertEqual(processed2, 1)
                self.assertEqual(projector.state.offset_bytes, len(first_half) + len(second_half))
                refutes = [c for c in sink.osb_calls if c["act"] == "refute"]
                self.assertEqual(len(refutes), 1)
                self.assertEqual(refutes[0]["ref"], f"utf8-{label}")
                self.assertIn(f"split-{utf8_char}-end", refutes[0]["text"])

                # Subsequent poll must not re-project
                processed3 = projector.run_once()
                projector.close()
                self.assertEqual(processed3, 0)
                self.assertEqual(len([c for c in sink.osb_calls if c["act"] == "refute"]), 1)


if __name__ == "__main__":
    unittest.main()
