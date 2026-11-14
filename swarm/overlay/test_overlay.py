#!/usr/bin/env python3
"""Unit and integration test suite for ViewPoint Swarm H3 overlay & tools.

Run inside bounded cgroup scope:
  systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
    python3 -m unittest discover -s swarm/overlay -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

import cv2
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from swarm.overlay.compositor import (  # noqa: E402
    AGENT_SPECS,
    BrokerTimelineTracker,
    VideoMeta,
    compose_video,
    evaluate_agent_cursor,
    filter_events_for_timeline,
    load_events,
    resolve_timeline,
)

COMPOSE_SH = REPO_ROOT / "swarm" / "tools" / "compose.sh"
RECORD_SH = REPO_ROOT / "swarm" / "tools" / "record.sh"


def build_realistic_events(base_ts: int = 1731580000000) -> List[Dict[str, Any]]:
    """Construct a contract-compliant (`v: 1`) multi-agent `events.jsonl` dataset."""
    t = base_ts
    return [
        # 1. Disjoint claims for all 4 planning agents (CONTRACT.md Section 4)
        {
            "v": 1,
            "rec": "claim",
            "agent_id": "modeler-agent",
            "lease_id": "lease-modeler-0001",
            "lease_epoch": 1,
            "ttl_ms": 300000,
            "issued_ts_ms": t,
            "expires_ts_ms": t + 300000,
            "allow_gui": False,
            "write_prefixes": ["col:CHAR_", "obj:CHAR_"],
        },
        {
            "v": 1,
            "rec": "claim",
            "agent_id": "props-agent",
            "lease_id": "lease-props-0001",
            "lease_epoch": 1,
            "ttl_ms": 300000,
            "issued_ts_ms": t + 5,
            "expires_ts_ms": t + 300005,
            "allow_gui": False,
            "write_prefixes": ["col:PROPS_", "obj:PROP_"],
        },
        {
            "v": 1,
            "rec": "claim",
            "agent_id": "look-agent",
            "lease_id": "lease-look-0001",
            "lease_epoch": 1,
            "ttl_ms": 300000,
            "issued_ts_ms": t + 10,
            "expires_ts_ms": t + 300010,
            "allow_gui": False,
            "write_prefixes": ["mat:", "light:", "world:"],
        },
        {
            "v": 1,
            "rec": "claim",
            "agent_id": "camera-agent",
            "lease_id": "lease-camera-0001",
            "lease_epoch": 1,
            "ttl_ms": 300000,
            "issued_ts_ms": t + 15,
            "expires_ts_ms": t + 300015,
            "allow_gui": True,
            "write_prefixes": ["cam:", "render:"],
        },
        # 2. Parallel agent enqueues + cursor intents
        {
            "v": 1,
            "rec": "enqueue",
            "event_id": "modeler-agent-1",
            "agent_id": "modeler-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": 1,
            "view_epoch": 0,
            "scene_epoch": 0,
            "lease_epoch": 1,
            "write_set": ["obj:CHAR_hero"],
            "target_region": None,
            "footprint": None,
            "expect": "obj:CHAR_hero exists",
            "script_sha256": "a" * 64,
            "max_acts": 1,
            "ts_ms": t + 100,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "modeler-agent",
            "event_id": "modeler-agent-1",
            "phase": "intent",
            "x": 480,
            "y": 420,
            "frame": 1,
            "label": "modeler-agent: CHAR_hero",
            "color": "#ff6b6b",
            "ts_ms": t + 110,
        },
        {
            "v": 1,
            "rec": "enqueue",
            "event_id": "props-agent-1",
            "agent_id": "props-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": 1,
            "view_epoch": 0,
            "scene_epoch": 0,
            "lease_epoch": 1,
            "write_set": ["obj:PROP_pedestal"],
            "target_region": None,
            "footprint": None,
            "expect": "obj:PROP_pedestal exists",
            "script_sha256": "b" * 64,
            "max_acts": 1,
            "ts_ms": t + 140,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "props-agent",
            "event_id": "props-agent-1",
            "phase": "intent",
            "x": 1080,
            "y": 620,
            "frame": 1,
            "label": "props-agent: PROP_pedestal",
            "color": "#4ecdc4",
            "ts_ms": t + 150,
        },
        {
            "v": 1,
            "rec": "enqueue",
            "event_id": "look-agent-bad-1",
            "agent_id": "look-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": 1,
            "view_epoch": 0,
            "scene_epoch": 0,
            "lease_epoch": 1,
            "write_set": ["obj:CHAR_illegal"],
            "target_region": None,
            "footprint": None,
            "expect": "refused",
            "script_sha256": "c" * 64,
            "max_acts": 1,
            "ts_ms": t + 170,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "look-agent",
            "event_id": "look-agent-bad-1",
            "phase": "intent",
            "x": 780,
            "y": 300,
            "frame": 1,
            "label": "look-agent: shader",
            "color": "#ffe66d",
            "ts_ms": t + 180,
        },
        {
            "v": 1,
            "rec": "enqueue",
            "event_id": "camera-agent-1",
            "agent_id": "camera-agent",
            "lane": "gui",
            "action_class": "view_change",
            "kind": "key",
            "based_on_frame": 1,
            "view_epoch": 0,
            "scene_epoch": 0,
            "lease_epoch": 1,
            "write_set": [],
            "target_region": [600, 300, 200, 200],
            "footprint": [0, 80, 1580, 900],
            "expect": "viewport front ortho",
            "script_sha256": None,
            "max_acts": 1,
            "ts_ms": t + 200,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "camera-agent",
            "event_id": "camera-agent-1",
            "phase": "intent",
            "x": 800,
            "y": 500,
            "frame": 1,
            "label": "camera-agent: KP_1",
            "color": "#38bdf8",
            "ts_ms": t + 210,
        },
        # 3. Turn 1: modeler-agent script -> STATE_VERIFIED
        {
            "v": 1,
            "rec": "turn",
            "turn": 1,
            "turn_epoch": 1,
            "agent_id": "modeler-agent",
            "head_frame": 1,
            "hold_ms": 120000,
            "ts_ms": t + 350,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "modeler-agent",
            "event_id": "modeler-agent-1",
            "phase": "act",
            "x": 540,
            "y": 450,
            "frame": 1,
            "label": "modeler-agent: build CHAR_hero",
            "color": "#ff6b6b",
            "ts_ms": t + 400,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 1,
            "turn": 1,
            "event_id": "modeler-agent-1",
            "agent_id": "modeler-agent",
            "lane": "script",
            "verdict": "STATE_VERIFIED",
            "rebased": False,
            "dispatch_capture_seq": 1,
            "verify_frame_ref": 2,
            "own_regions": [[440, 380, 140, 140]],
            "spill_regions": [],
            "drops": 0,
            "reason": "",
            "ts_ms": t + 750,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "modeler-agent",
            "event_id": "modeler-agent-1",
            "phase": "result",
            "x": 540,
            "y": 450,
            "frame": 2,
            "label": "modeler-agent: STATE_VERIFIED",
            "color": "#ff6b6b",
            "ts_ms": t + 760,
        },
        # 4. Turn 2: props-agent script -> STATE_VERIFIED
        {
            "v": 1,
            "rec": "turn",
            "turn": 2,
            "turn_epoch": 2,
            "agent_id": "props-agent",
            "head_frame": 2,
            "hold_ms": 120000,
            "ts_ms": t + 900,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "props-agent",
            "event_id": "props-agent-1",
            "phase": "act",
            "x": 1130,
            "y": 580,
            "frame": 2,
            "label": "props-agent: spawn pedestal",
            "color": "#4ecdc4",
            "ts_ms": t + 950,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 2,
            "turn": 2,
            "event_id": "props-agent-1",
            "agent_id": "props-agent",
            "lane": "script",
            "verdict": "STATE_VERIFIED",
            "rebased": False,
            "dispatch_capture_seq": 2,
            "verify_frame_ref": 3,
            "own_regions": [[1040, 540, 160, 140]],
            "spill_regions": [],
            "drops": 0,
            "reason": "",
            "ts_ms": t + 1300,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "props-agent",
            "event_id": "props-agent-1",
            "phase": "result",
            "x": 1130,
            "y": 580,
            "frame": 3,
            "label": "props-agent: STATE_VERIFIED",
            "color": "#4ecdc4",
            "ts_ms": t + 1310,
        },
        # 5. Turn 3: look-agent -> CLAIM_REFUSED, followed by valid enqueue
        {
            "v": 1,
            "rec": "turn",
            "turn": 3,
            "turn_epoch": 3,
            "agent_id": "look-agent",
            "head_frame": 3,
            "hold_ms": 120000,
            "ts_ms": t + 1450,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 2,
            "turn": 3,
            "event_id": "look-agent-bad-1",
            "agent_id": "look-agent",
            "lane": "script",
            "verdict": "CLAIM_REFUSED",
            "rebased": False,
            "dispatch_capture_seq": None,
            "verify_frame_ref": None,
            "own_regions": [],
            "spill_regions": [],
            "drops": 0,
            "reason": "write_set outside mat:/light:/world:",
            "ts_ms": t + 1520,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "look-agent",
            "event_id": "look-agent-bad-1",
            "phase": "result",
            "x": 780,
            "y": 300,
            "frame": 3,
            "label": "look-agent: CLAIM_REFUSED",
            "color": "#ffe66d",
            "ts_ms": t + 1530,
        },
        # 6. Turn 4: camera-agent GUI -> STALE_FRAME refusal (based_on_frame 1 < head_frame 3)
        {
            "v": 1,
            "rec": "turn",
            "turn": 4,
            "turn_epoch": 4,
            "agent_id": "camera-agent",
            "head_frame": 3,
            "hold_ms": 60000,
            "ts_ms": t + 1700,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 2,
            "turn": 4,
            "event_id": "camera-agent-1",
            "agent_id": "camera-agent",
            "lane": "gui",
            "verdict": "STALE_FRAME",
            "rebased": False,
            "dispatch_capture_seq": None,
            "verify_frame_ref": None,
            "own_regions": [],
            "spill_regions": [],
            "drops": 1,
            "reason": "based_on_frame 1 < head_frame 3",
            "ts_ms": t + 1780,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "camera-agent",
            "event_id": "camera-agent-1",
            "phase": "result",
            "x": 800,
            "y": 500,
            "frame": 3,
            "label": "camera-agent: STALE_FRAME",
            "color": "#38bdf8",
            "ts_ms": t + 1790,
        },
        # 7. Turn 5: look-agent valid key-light job -> STATE_VERIFIED
        {
            "v": 1,
            "rec": "enqueue",
            "event_id": "look-agent-2",
            "agent_id": "look-agent",
            "lane": "script",
            "action_class": "state_job",
            "kind": "job",
            "based_on_frame": 3,
            "view_epoch": 0,
            "scene_epoch": 2,
            "lease_epoch": 1,
            "write_set": ["mat:MAT_gold", "light:LIGHT_key"],
            "target_region": None,
            "footprint": None,
            "expect": "mat:MAT_gold assigned",
            "script_sha256": "d" * 64,
            "max_acts": 1,
            "ts_ms": t + 1900,
        },
        {
            "v": 1,
            "rec": "turn",
            "turn": 5,
            "turn_epoch": 5,
            "agent_id": "look-agent",
            "head_frame": 3,
            "hold_ms": 120000,
            "ts_ms": t + 1950,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "look-agent",
            "event_id": "look-agent-2",
            "phase": "act",
            "x": 740,
            "y": 330,
            "frame": 3,
            "label": "look-agent: MAT_gold",
            "color": "#ffe66d",
            "ts_ms": t + 2000,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 3,
            "turn": 5,
            "event_id": "look-agent-2",
            "agent_id": "look-agent",
            "lane": "script",
            "verdict": "STATE_VERIFIED",
            "rebased": False,
            "dispatch_capture_seq": 3,
            "verify_frame_ref": 4,
            "own_regions": [[700, 260, 150, 130]],
            "spill_regions": [],
            "drops": 0,
            "reason": "",
            "ts_ms": t + 2250,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "look-agent",
            "event_id": "look-agent-2",
            "phase": "result",
            "x": 740,
            "y": 330,
            "frame": 4,
            "label": "look-agent: STATE_VERIFIED",
            "color": "#ffe66d",
            "ts_ms": t + 2260,
        },
        # 8. Turn 6: camera-agent rebased GUI turn -> VERIFIED
        {
            "v": 1,
            "rec": "turn",
            "turn": 6,
            "turn_epoch": 6,
            "agent_id": "camera-agent",
            "head_frame": 4,
            "hold_ms": 60000,
            "ts_ms": t + 2350,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "camera-agent",
            "event_id": "camera-agent-1",
            "phase": "act",
            "x": 860,
            "y": 470,
            "frame": 4,
            "label": "camera-agent: KP_1",
            "color": "#38bdf8",
            "ts_ms": t + 2400,
        },
        {
            "v": 1,
            "rec": "close",
            "tick": 4,
            "turn": 6,
            "event_id": "camera-agent-1",
            "agent_id": "camera-agent",
            "lane": "gui",
            "verdict": "VERIFIED",
            "rebased": True,
            "dispatch_capture_seq": 4,
            "verify_frame_ref": 5,
            "own_regions": [[200, 140, 1200, 760]],
            "spill_regions": [],
            "drops": 1,
            "reason": "",
            "ts_ms": t + 2750,
        },
        {
            "v": 1,
            "rec": "cursor",
            "agent_id": "camera-agent",
            "event_id": "camera-agent-1",
            "phase": "result",
            "x": 860,
            "y": 470,
            "frame": 5,
            "label": "camera-agent: KP_1 VERIFIED",
            "color": "#38bdf8",
            "ts_ms": t + 2760,
        },
        {
            "v": 1,
            "rec": "control",
            "op": "checkpoint",
            "by": "director-agent",
            "reason": "milestone_scene_v1",
            "ts_ms": t + 2850,
        },
    ]


def _write_events_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    lines = [json.dumps(r, separators=(",", ":")) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _synthesize_solid_mp4(path: Path, width: int = 1920, height: int = 1080, duration_s: int = 3, fps: int = 15) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1e1e2e:s={width}x{height}:d={duration_s}:r={fps}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _read_all_frames(path: Path) -> List[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open {path}")
    frames: List[np.ndarray] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            frames.append(frame)
    finally:
        cap.release()
    return frames


class TestSwarmOverlayAndTools(unittest.TestCase):
    """End-to-end and unit verification for H3 off-display compositor & tools."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="swarm_h3_test_", dir="/tmp"))

    def tearDown(self) -> None:
        if os.environ.get("KEEP_TEST_ARTIFACTS") != "1":
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cursor_smoothstep_lerp_250ms(self) -> None:
        """Cursor positions smoothly interpolate (`C^1` smoothstep) over 250 ms."""
        cursor_events = [
            {
                "v": 1,
                "rec": "cursor",
                "agent_id": "modeler-agent",
                "event_id": "m-1",
                "phase": "act",
                "x": 680,
                "y": 520,
                "color": "#ff6b6b",
                "_ts_ms": 1000,
            }
        ]
        ts_list = [1000]

        # Before event -> default anchor [480, 420], idle phase
        s_pre = evaluate_agent_cursor("modeler-agent", cursor_events, ts_list, 950.0, 250.0, {})
        self.assertEqual((s_pre.x, s_pre.y), (480.0, 420.0))
        self.assertEqual(s_pre.phase, "idle")

        # At exact event start (dt = 0) -> no jump discontinuity
        s_0 = evaluate_agent_cursor("modeler-agent", cursor_events, ts_list, 1000.0, 250.0, {})
        self.assertAlmostEqual(s_0.x, 480.0, places=4)
        self.assertAlmostEqual(s_0.y, 420.0, places=4)
        self.assertEqual(s_0.phase, "act")

        # Midpoint (dt = 125 ms -> smoothstep(0.5) == 0.5)
        s_mid = evaluate_agent_cursor("modeler-agent", cursor_events, ts_list, 1125.0, 250.0, {})
        self.assertAlmostEqual(s_mid.x, 580.0, places=3)
        self.assertAlmostEqual(s_mid.y, 470.0, places=3)

        # Quarter & three-quarter monotonicity check
        s_q1 = evaluate_agent_cursor("modeler-agent", cursor_events, ts_list, 1062.5, 250.0, {})
        s_q3 = evaluate_agent_cursor("modeler-agent", cursor_events, ts_list, 1187.5, 250.0, {})
        self.assertTrue(480.0 < s_q1.x < s_mid.x < s_q3.x < 680.0)
        self.assertTrue(420.0 < s_q1.y < s_mid.y < s_q3.y < 520.0)

        # Completed lerp (dt >= 250 ms) -> exact target [680, 520]
        s_end = evaluate_agent_cursor("modeler-agent", cursor_events, ts_list, 1250.0, 250.0, {})
        self.assertAlmostEqual(s_end.x, 680.0, places=4)
        self.assertAlmostEqual(s_end.y, 520.0, places=4)

    def test_broker_timeline_tracker_ring_and_stale_frame_requeue(self) -> None:
        """Tracker preserves deterministic ring order and requeues STALE_FRAME intents."""
        events_path = self.tmpdir / "events.jsonl"
        _write_events_jsonl(events_path, build_realistic_events(base_ts=10000))
        events = load_events(events_path, strict=True)
        tracker = BrokerTimelineTracker(events)

        # Advance past Turn 1 open (10350 ms): head is modeler-agent
        tracker.advance_to(10450)
        self.assertEqual(tracker.active_head, "modeler-agent")
        self.assertEqual(
            tracker.waiting_ring(),
            ["props-agent", "look-agent", "camera-agent"],
        )

        # Advance past Turn 4 STALE_FRAME refusal (11780 ms): camera-agent-1 remains queued!
        tracker.advance_to(11800)
        self.assertEqual(tracker.last_verdict_rec["verdict"], "STALE_FRAME")
        self.assertEqual(len(tracker.pending_fifo["camera-agent"]), 1)
        self.assertEqual(tracker.tick_num, 2)  # STALE_FRAME does not advance tick

        # Advance to end (13000 ms): camera-agent-1 closed VERIFIED, tick == 4
        tracker.advance_to(13000)
        self.assertEqual(tracker.last_verdict_rec["verdict"], "VERIFIED")
        self.assertTrue(tracker.last_verdict_rec["rebased"])
        self.assertEqual(len(tracker.pending_fifo["camera-agent"]), 0)
        self.assertEqual(tracker.tick_num, 4)

    def test_timeline_inference_modes(self) -> None:
        """Timeline resolver supports `--start-ts-ms`, sidecar, `mtime`, and `span` fallback."""
        vid = self.tmpdir / "clip.mp4"
        vid.write_bytes(b"placeholder")
        meta = VideoMeta(width=1920, height=1080, fps=15.0, duration_ms=3000, nb_frames=45)

        ev_path = self.tmpdir / "events.jsonl"
        _write_events_jsonl(ev_path, build_realistic_events(base_ts=1731580000000))
        events = load_events(ev_path, strict=True)

        # 1. Time-shifted video mtime vs historical events -> auto `span` fallback
        t0, t1, src = resolve_timeline(vid, meta, events)
        self.assertEqual(src, "span")
        self.assertLessEqual(t0, 1731580000100)
        self.assertGreaterEqual(t1, 1731580002850)

        # 2. Explicit `--start-ts-ms`
        t0_exp, t1_exp, src_exp = resolve_timeline(vid, meta, events, start_ts_ms=1731580000000)
        self.assertEqual(src_exp, "explicit")
        self.assertEqual((t0_exp, t1_exp), (1731580000000, 1731580003000))

        # 3. Matching `.meta.json` sidecar
        sidecar = Path(f"{vid}.meta.json")
        sidecar.write_text(json.dumps({"v": 1, "start_ts_ms": 1731580000000}), encoding="utf-8")
        t0_sc, t1_sc, src_sc = resolve_timeline(vid, meta, events)
        self.assertEqual(src_sc, "sidecar")
        self.assertEqual((t0_sc, t1_sc), (1731580000000, 1731580003000))

    def test_end_to_end_compose_sh_1080p_and_negative_controls(self) -> None:
        """Synthesize 3s 1920x1080 MP4, compose via `compose.sh`, verify stream & HUD pixels."""
        raw_mp4 = self.tmpdir / "raw_1080p.mp4"
        events_jsonl = self.tmpdir / "events.jsonl"
        composed_mp4 = self.tmpdir / "composed_1080p.mp4"
        summary_json = self.tmpdir / "summary.json"

        _synthesize_solid_mp4(raw_mp4, width=1920, height=1080, duration_s=3, fps=15)
        _write_events_jsonl(events_jsonl, build_realistic_events(base_ts=1731580000000))

        # Run compose.sh with DISPLAY="" (negative control: zero X11 access)
        env = dict(os.environ)
        env["DISPLAY"] = ""
        proc = subprocess.run(
            [
                str(COMPOSE_SH),
                str(raw_mp4),
                str(events_jsonl),
                str(composed_mp4),
                "--strict",
                "--summary-json",
                str(summary_json),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=f"compose.sh failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
        self.assertTrue(composed_mp4.is_file() and composed_mp4.stat().st_size > 0)

        # Verify ffprobe stream parameters: 1920x1080, h264, yuv420p
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,pix_fmt,nb_frames",
                "-of",
                "json",
                str(composed_mp4),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        st = json.loads(probe.stdout)["streams"][0]
        self.assertEqual(st["codec_name"], "h264")
        self.assertEqual(int(st["width"]), 1920)
        self.assertEqual(int(st["height"]), 1080)
        self.assertEqual(st["pix_fmt"], "yuv420p")
        self.assertEqual(int(st["nb_frames"]), 45)

        # Verify `+faststart`: `moov` atom precedes `mdat` atom in MP4 container
        mp4_bytes = composed_mp4.read_bytes()
        moov_pos = mp4_bytes.find(b"moov")
        mdat_pos = mp4_bytes.find(b"mdat")
        self.assertGreater(moov_pos, 0)
        self.assertGreater(mdat_pos, moov_pos, "Expected +faststart (moov atom before mdat)")

        # Decode raw vs composed frames and verify pixel differences across HUD zones
        raw_frames = _read_all_frames(raw_mp4)
        comp_frames = _read_all_frames(composed_mp4)
        self.assertEqual(len(raw_frames), 45)
        self.assertEqual(len(comp_frames), 45)

        # 1. Top Turn-Queue Strip (y = 0..56) differs strongly on every frame & evolves over turns
        for check_idx in (0, 12, 26, 42):
            diff_strip = np.abs(
                comp_frames[check_idx][0:56, :, :].astype(np.int16)
                - raw_frames[check_idx][0:56, :, :].astype(np.int16)
            )
            changed_px = int(np.sum(np.max(diff_strip, axis=2) > 15))
            self.assertGreater(changed_px, 8000, f"Top strip missing on frame {check_idx}")

        strip_early_vs_late = np.abs(
            comp_frames[8][0:56, :, :].astype(np.int16)
            - comp_frames[40][0:56, :, :].astype(np.int16)
        )
        self.assertGreater(
            int(np.sum(np.max(strip_early_vs_late, axis=2) > 20)),
            1000,
            "Top strip did not update across turns/verdicts",
        )

        # 2. Per-Agent Cursor Anchor Regions differ from raw solid background
        anchor_checks = {
            "modeler-agent": (480, 420, 540, 450),
            "props-agent": (1080, 620, 1130, 580),
            "look-agent": (780, 300, 740, 330),
            "camera-agent": (800, 500, 860, 470),
        }
        for agent_id, (ax0, ay0, ax1, ay1) in anchor_checks.items():
            x_lo = max(0, min(ax0, ax1) - 30)
            x_hi = min(1920, max(ax0, ax1) + 180)
            y_lo = max(56, min(ay0, ay1) - 30)
            y_hi = min(1080, max(ay0, ay1) + 65)
            max_changed = 0
            for f_idx in (2, 15, 30, 42):
                roi_diff = np.abs(
                    comp_frames[f_idx][y_lo:y_hi, x_lo:x_hi, :].astype(np.int16)
                    - raw_frames[f_idx][y_lo:y_hi, x_lo:x_hi, :].astype(np.int16)
                )
                changed = int(np.sum(np.max(roi_diff, axis=2) > 25))
                max_changed = max(max_changed, changed)
            self.assertGreater(max_changed, 300, f"Cursor missing for {agent_id}")

        # 3. Standing Caption Region (bottom-right) differs from raw input
        cap_diff = np.abs(
            comp_frames[20][1020:1080, 1180:1920, :].astype(np.int16)
            - raw_frames[20][1020:1080, 1180:1920, :].astype(np.int16)
        )
        self.assertGreater(int(np.sum(np.max(cap_diff, axis=2) > 20)), 1500)

        # 4. Unoccupied viewport region stays untouched (0 spurious artifacts)
        clean_diff = np.abs(
            comp_frames[20][760:900, 80:260, :].astype(np.int16)
            - raw_frames[20][760:900, 80:260, :].astype(np.int16)
        )
        self.assertLessEqual(int(np.max(clean_diff)), 4)

    def test_arbitrary_resolution_scaling_and_invalid_display_isolation(self) -> None:
        """Compositor maps 1920x1080 coordinates cleanly onto 960x540 even with poisoned DISPLAY."""
        raw_540p = self.tmpdir / "raw_540p.mp4"
        events_jsonl = self.tmpdir / "events.jsonl"
        out_540p = self.tmpdir / "composed_540p.mp4"

        _synthesize_solid_mp4(raw_540p, width=960, height=540, duration_s=1, fps=10)
        _write_events_jsonl(events_jsonl, build_realistic_events(base_ts=1731580000000))

        # Poison DISPLAY=":99421" to prove compositor never attempts X11 connection
        env = dict(os.environ)
        env["DISPLAY"] = ":99421"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "swarm.overlay.compositor",
                "--input",
                str(raw_540p),
                "--events",
                str(events_jsonl),
                "--output",
                str(out_540p),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        frames = _read_all_frames(out_540p)
        self.assertEqual(len(frames), 10)
        self.assertEqual(frames[0].shape, (540, 960, 3))

    def test_negative_control_record_sh_refuses_display1_and_display0(self) -> None:
        """`record.sh` unconditionally refuses `:1` / `:1.0` (exit 2) and guards `:0`."""
        isolated_home = self.tmpdir / "fake_home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["HOME"] = str(isolated_home)

        forbidden_invocations = [
            [str(RECORD_SH), ":1", "unit_neg_d1"],
            [str(RECORD_SH), ":1.0", "unit_neg_d1_0"],
            [str(RECORD_SH), "--allow-display0", ":1", "unit_neg_d1_override"],
            [str(RECORD_SH), "localhost:1.0", "unit_neg_d1_host"],
            [str(RECORD_SH), ":1+0,0", "unit_neg_d1_offset"],
            [str(RECORD_SH), ":0", "unit_neg_d0"],
            [str(RECORD_SH), ":0.0", "unit_neg_d0_0"],
            # B1 leading-zero bypass probes
            [str(RECORD_SH), ":01", "unit_neg_d01"],
            [str(RECORD_SH), ":001", "unit_neg_d001"],
            [str(RECORD_SH), "--allow-display0", ":001", "unit_neg_d001_override"],
            [str(RECORD_SH), "localhost:01.0", "unit_neg_d01_host"],
            [str(RECORD_SH), "unix:001", "unit_neg_d001_unix"],
            [str(RECORD_SH), "unix:001+0,0", "unit_neg_d001_unix_off"],
            [str(RECORD_SH), ":00", "unit_neg_d00"],
            [str(RECORD_SH), ":000", "unit_neg_d000"],
        ]
        for cmd in forbidden_invocations:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(
                res.returncode,
                2,
                msg=f"Expected exit 2 for {cmd}, got {res.returncode} (stderr={res.stderr!r})",
            )
            self.assertIn("REFUSED", res.stderr)

        # Ensure refused calls never created ~/Vídeos/ViewPoint_hackathon or /tmp artifacts
        self.assertFalse((isolated_home / "Vídeos" / "ViewPoint_hackathon").exists())
        self.assertFalse(Path("/tmp/swarm_record_unit_neg_d1.pid").exists())

    def test_record_sh_positive_lavfi_shim_lifecycle(self) -> None:
        """`record.sh :99 <name>` emits bare PID, writes pointer files, and finalizes MP4 on SIGINT."""
        isolated_home = self.tmpdir / "rec_home"
        run_dir = self.tmpdir / "run_state"
        mock_bin = self.tmpdir / "mockbin"
        isolated_home.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        mock_bin.mkdir(parents=True, exist_ok=True)

        # Headless lavfi shim substituting x11grab with synthetic color source (0 X11 connections)
        shim = mock_bin / "ffmpeg"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "out = sys.argv[-1]\n"
            "os.execv('/usr/bin/ffmpeg', [\n"
            "  '/usr/bin/ffmpeg', '-y', '-f', 'lavfi',\n"
            "  '-i', 'color=c=0x1e1e2e:s=320x180:r=15',\n"
            "  '-c:v', 'libx264', '-preset', 'ultrafast',\n"
            "  '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out\n"
            "])\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

        rec_name = f"h3_test_{os.getpid()}"
        pid: int = 0
        env = dict(os.environ)
        env["HOME"] = str(isolated_home)
        env["SWARM_RUN_DIR"] = str(run_dir)
        env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
        env["DISPLAY"] = ""

        try:
            res = subprocess.run(
                [str(RECORD_SH), ":99", rec_name],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            stdout_lines = res.stdout.strip().splitlines()
            self.assertEqual(len(stdout_lines), 1, f"Expected bare PID on stdout, got {res.stdout!r}")
            self.assertTrue(stdout_lines[0].isdigit(), f"Non-integer PID stdout: {stdout_lines[0]!r}")
            pid = int(stdout_lines[0])

            time.sleep(0.5)
            os.kill(pid, signal.SIGINT)
            for _ in range(40):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break

            pid_mp4_ptr = Path(f"/tmp/swarm_record_{pid}.mp4.path")
            name_out_ptr = Path(f"/tmp/swarm_record_{rec_name}.outpath")
            name_pid_ptr = Path(f"/tmp/swarm_record_{rec_name}.pid")
            self.assertTrue(pid_mp4_ptr.is_file())
            self.assertTrue(name_out_ptr.is_file())
            self.assertEqual(int(name_pid_ptr.read_text(encoding="utf-8").strip()), pid)
            self.assertTrue((run_dir / f"swarm_record_{rec_name}.pid").is_file())

            out_mp4 = Path(pid_mp4_ptr.read_text(encoding="utf-8").strip())
            self.assertTrue(out_mp4.is_file() and out_mp4.stat().st_size > 0)
            self.assertRegex(out_mp4.name, rf"^{re.escape(f'swarm_{rec_name}_')}[0-9]{{6}}\.mp4$")
            meta_path = Path(f"{out_mp4}.meta.json")
            self.assertTrue(meta_path.is_file())
            meta_json = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta_json["display"], ":99")

            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(out_mp4),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), "h264")
        finally:
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                Path(f"/tmp/swarm_record_{pid}.mp4.path").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{rec_name}.outpath").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{rec_name}.pid").unlink(missing_ok=True)

    def test_m1_sidecar_unconditional_wallclock_over_multi_run_log(self) -> None:
        """M1: Valid sidecar wins unconditionally in `auto` mode even when 80/100 events are from an earlier run."""
        vid = self.tmpdir / "rehearsal_take2.mp4"
        vid.write_bytes(b"placeholder")
        meta = VideoMeta(width=1920, height=1080, fps=15.0, duration_ms=10000, nb_frames=150)

        records: List[Dict[str, Any]] = []
        # 80 historical events from an earlier run at t = 1_000_000 (an hour earlier)
        for idx in range(80):
            records.append(
                {
                    "v": 1,
                    "rec": "turn",
                    "turn": idx + 1,
                    "turn_epoch": idx + 1,
                    "agent_id": "modeler-agent",
                    "head_frame": 1,
                    "hold_ms": 120000,
                    "ts_ms": 1_000_000 + idx * 100,
                }
            )
        # 20 events from the recorded run at t = 4_600_000..4_609_500
        for idx in range(20):
            records.append(
                {
                    "v": 1,
                    "rec": "turn",
                    "turn": idx + 1,
                    "turn_epoch": idx + 1,
                    "agent_id": "camera-agent",
                    "head_frame": 10 + idx,
                    "hold_ms": 60000,
                    "ts_ms": 4_600_000 + idx * 500,
                }
            )

        ev_path = self.tmpdir / "multi_run_events.jsonl"
        _write_events_jsonl(ev_path, records)
        events = load_events(ev_path, strict=True)

        sidecar = Path(f"{vid}.meta.json")
        sidecar.write_text(json.dumps({"v": 1, "start_ts_ms": 4_600_000}), encoding="utf-8")

        t0, t1, src = resolve_timeline(vid, meta, events, timeline_mode="auto")
        self.assertEqual(src, "sidecar")
        self.assertEqual((t0, t1), (4_600_000, 4_610_000))

        # Explicit --timeline-mode span still maps full event span when requested
        t0_span, t1_span, src_span = resolve_timeline(vid, meta, events, timeline_mode="span")
        self.assertEqual(src_span, "span")
        self.assertLessEqual(t0_span, 1_000_000)
        self.assertGreaterEqual(t1_span, 4_609_500)

    def test_m2_distinguish_event_park_vs_agent_park_and_targeted_resume(self) -> None:
        """M2/n5: 3 stale drops parks only the event; TURN_TIMEOUT / explicit agent park marks `[PARK]`."""
        t = 50000
        records: List[Dict[str, Any]] = [
            {
                "v": 1,
                "rec": "enqueue",
                "event_id": "camera-agent-1",
                "agent_id": "camera-agent",
                "lane": "gui",
                "ts_ms": t + 10,
            },
            {
                "v": 1,
                "rec": "close",
                "tick": 0,
                "turn": 1,
                "event_id": "camera-agent-1",
                "agent_id": "camera-agent",
                "lane": "gui",
                "verdict": "STALE_FRAME",
                "drops": 3,
                "reason": "parked_after_3_stale_drops",
                "ts_ms": t + 50,
            },
            {
                "v": 1,
                "rec": "control",
                "op": "park",
                "by": "broker",
                "agent_id": "camera-agent",
                "event_id": "camera-agent-1",
                "drops": 3,
                "verdict": "STALE_FRAME",
                "reason": "parked_after_3_stale_drops:camera-agent-1",
                "ts_ms": t + 55,
            },
            # camera-agent keeps receiving subsequent turns without being marked [PARK]
            {
                "v": 1,
                "rec": "turn",
                "turn": 2,
                "turn_epoch": 2,
                "agent_id": "camera-agent",
                "head_frame": 5,
                "hold_ms": 60000,
                "ts_ms": t + 100,
            },
            # Later, 2 consecutive TURN_TIMEOUTs DO park camera-agent
            {
                "v": 1,
                "rec": "control",
                "op": "park",
                "by": "broker",
                "agent_id": "camera-agent",
                "event_id": "camera-agent-2",
                "reason": "camera-agent: 2 consecutive TURN_TIMEOUT",
                "ts_ms": t + 200,
            },
            # Explicit agent-level director park on props-agent (no event_id)
            {
                "v": 1,
                "rec": "control",
                "op": "park",
                "by": "director-agent",
                "agent_id": "props-agent",
                "reason": "operator hold",
                "ts_ms": t + 250,
            },
            # Targeted resume on camera-agent leaves props-agent parked
            {
                "v": 1,
                "rec": "control",
                "op": "resume",
                "by": "director-agent",
                "agent_id": "camera-agent",
                "reason": "resume camera only",
                "ts_ms": t + 300,
            },
            # Untargeted resume clears remaining parked agents
            {
                "v": 1,
                "rec": "control",
                "op": "resume",
                "by": "director-agent",
                "reason": "resume all",
                "ts_ms": t + 350,
            },
        ]
        ev_path = self.tmpdir / "park_events.jsonl"
        _write_events_jsonl(ev_path, records)
        tracker = BrokerTimelineTracker(load_events(ev_path, strict=False))

        tracker.advance_to(t + 100)
        self.assertEqual(len(tracker.pending_fifo["camera-agent"]), 0)
        self.assertNotIn("camera-agent", tracker.parked_agents)

        tracker.advance_to(t + 260)
        self.assertIn("camera-agent", tracker.parked_agents)
        self.assertIn("props-agent", tracker.parked_agents)

        tracker.advance_to(t + 310)
        self.assertNotIn("camera-agent", tracker.parked_agents)
        self.assertIn("props-agent", tracker.parked_agents)

        tracker.advance_to(t + 360)
        self.assertEqual(len(tracker.parked_agents), 0)

    def test_m3_ffmpeg_stderr_drain_thread_exceeds_pipe_buffer_without_deadlock(self) -> None:
        """M3: Encoder passes `-nostats -loglevel error` and drains > 128 KiB stderr continuously without deadlock."""
        raw_mp4 = self.tmpdir / "raw_m3.mp4"
        out_mp4 = self.tmpdir / "out_m3.mp4"
        ev_path = self.tmpdir / "events_m3.jsonl"
        mock_bin = self.tmpdir / "mockbin_m3"
        mock_bin.mkdir(parents=True, exist_ok=True)
        flags_log = self.tmpdir / "ffmpeg_args.json"

        _synthesize_solid_mp4(raw_mp4, width=320, height=180, duration_s=1, fps=15)
        _write_events_jsonl(ev_path, build_realistic_events(base_ts=1731580000000))

        # Mock ffmpeg on PATH that writes 192 KiB (> 64 KiB pipe buffer & > 128 KiB threshold)
        # to stderr while reading rawvideo from stdin, then delegates to /usr/bin/ffmpeg.
        shim = mock_bin / "ffmpeg"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys\n"
            f"with open({str(flags_log)!r}, 'w', encoding='utf-8') as f:\n"
            "    json.dump(sys.argv[1:], f)\n"
            "assert '-nostats' in sys.argv, f'missing -nostats: {sys.argv}'\n"
            "idx = sys.argv.index('-loglevel')\n"
            "assert sys.argv[idx + 1] == 'error', f'unexpected loglevel: {sys.argv}'\n"
            "proc = subprocess.Popen(['/usr/bin/ffmpeg'] + sys.argv[1:], stdin=subprocess.PIPE, stdout=sys.stdout, stderr=subprocess.PIPE)\n"
            "spam = (b'E' * 4096) * 48  # 196,608 bytes (> 128 KiB, > 64 KiB pipe buffer)\n"
            "os.write(2, spam)\n"
            "while True:\n"
            "    chunk = sys.stdin.buffer.read(65536)\n"
            "    if not chunk:\n"
            "        break\n"
            "    proc.stdin.write(chunk)\n"
            "proc.stdin.close()\n"
            "err = proc.stderr.read()\n"
            "if err:\n"
            "    os.write(2, err)\n"
            "sys.exit(proc.wait())\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        result_box: Dict[str, Any] = {}
        err_box: List[BaseException] = []

        def _run() -> None:
            try:
                result_box["summary"] = compose_video(
                    input_path=raw_mp4,
                    events_path=ev_path,
                    output_path=out_mp4,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                err_box.append(exc)

        os.environ["PATH"] = f"{mock_bin}:{old_path}"
        try:
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            worker.join(timeout=20.0)
            self.assertFalse(worker.is_alive(), "compose_video deadlocked on > 128 KiB stderr output")
        finally:
            os.environ["PATH"] = old_path

        self.assertEqual(err_box, [], msg=f"compose_video raised: {err_box!r}")
        self.assertEqual(result_box["summary"]["frames_written"], 15)
        self.assertTrue(out_mp4.is_file() and out_mp4.stat().st_size > 0)
        recorded_args = json.loads(flags_log.read_text(encoding="utf-8"))
        self.assertIn("-nostats", recorded_args)
        self.assertIn("-loglevel", recorded_args)

    def test_m1_m2_n4_duplicate_event_torn_line_and_equal_ts_order(self) -> None:
        """m1/m2/n4: DUPLICATE_EVENT/QUEUE_FULL preserve FIFO & verdicts; torn line skipped; equal ts keeps file order."""
        t = 90000
        valid_lines = [
            json.dumps({"v": 1, "rec": "enqueue", "event_id": "m-1", "agent_id": "modeler-agent", "lane": "script", "ts_ms": t}),
            json.dumps({"v": 1, "rec": "enqueue", "event_id": "m-2", "agent_id": "modeler-agent", "lane": "script", "ts_ms": t}),
            json.dumps(
                {
                    "v": 1,
                    "rec": "close",
                    "tick": 1,
                    "turn": 1,
                    "event_id": "m-1",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "verdict": "STATE_VERIFIED",
                    "ts_ms": t + 20,
                }
            ),
            # Duplicate close for already-closed m-1 must NOT overwrite STATE_VERIFIED
            json.dumps(
                {
                    "v": 1,
                    "rec": "close",
                    "tick": 1,
                    "turn": 1,
                    "event_id": "m-1",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "verdict": "DUPLICATE_EVENT",
                    "ts_ms": t + 30,
                }
            ),
            # Duplicate close for still-queued m-2 must NOT drop m-2 from pending_fifo
            json.dumps(
                {
                    "v": 1,
                    "rec": "close",
                    "tick": 1,
                    "turn": 1,
                    "event_id": "m-2",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "verdict": "DUPLICATE_EVENT",
                    "ts_ms": t + 40,
                }
            ),
            # QUEUE_FULL close must NOT mutate pending_fifo or close_by_event
            json.dumps(
                {
                    "v": 1,
                    "rec": "close",
                    "tick": 1,
                    "turn": 1,
                    "event_id": "m-9",
                    "agent_id": "modeler-agent",
                    "lane": "script",
                    "verdict": "QUEUE_FULL",
                    "ts_ms": t + 50,
                }
            ),
            # Invalid ts_ms + torn trailing line
            json.dumps({"v": 1, "rec": "turn", "turn": 9, "agent_id": "props-agent", "ts_ms": "bad_ts"}),
            '{"v":1,"rec":"close","verdict":"TRUNCATED_TORN_LINE',
        ]
        ev_path = self.tmpdir / "torn_events.jsonl"
        ev_path.write_text("\n".join(valid_lines), encoding="utf-8")

        # Strict mode rejects malformed lines
        with self.assertRaises(ValueError):
            load_events(ev_path, strict=True)

        # Non-strict mode skips torn trailing line and bad ts_ms cleanly
        events = load_events(ev_path, strict=False)
        self.assertEqual(len(events), 6)
        # n4: equal ts_ms records preserve file line order
        self.assertEqual([events[0]["event_id"], events[1]["event_id"]], ["m-1", "m-2"])
        self.assertLess(events[0]["_line_no"], events[1]["_line_no"])

        tracker = BrokerTimelineTracker(events)
        tracker.advance_to(t + 60)
        self.assertEqual(tracker.close_by_event[("modeler-agent", "m-1")]["verdict"], "STATE_VERIFIED")
        self.assertNotIn(("modeler-agent", "m-2"), tracker.close_by_event)
        self.assertNotIn(("modeler-agent", "m-9"), tracker.close_by_event)
        self.assertEqual([item["event_id"] for item in tracker.pending_fifo["modeler-agent"]], ["m-2"])

    def test_n3_record_sh_sidecar_json_escapes_special_paths_and_refuses_bad_host(self) -> None:
        """n3/N1: Strict host grammar refuses quotes/backslashes; sidecar JSON escapes special output paths."""
        # Under N1, quotes/backslashes in display host are strictly refused with exit 2
        bad_home = self.tmpdir / "bad_host_home"
        bad_home.mkdir(parents=True, exist_ok=True)
        bad_env = dict(os.environ)
        bad_env["HOME"] = str(bad_home)
        bad_res = subprocess.run(
            [str(RECORD_SH), 'host"quote\\slash:98', f"h3_bad_host_{os.getpid()}"],
            env=bad_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad_res.returncode, 2)
        self.assertIn("REFUSED", bad_res.stderr)

        isolated_home = self.tmpdir / 'esc"quote\\home'
        mock_bin = self.tmpdir / "esc_mockbin"
        isolated_home.mkdir(parents=True, exist_ok=True)
        mock_bin.mkdir(parents=True, exist_ok=True)

        shim = mock_bin / "ffmpeg"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "out = sys.argv[-1]\n"
            "os.execv('/usr/bin/ffmpeg', [\n"
            "  '/usr/bin/ffmpeg', '-y', '-f', 'lavfi',\n"
            "  '-i', 'color=c=0x1e1e2e:s=320x180:r=15',\n"
            "  '-c:v', 'libx264', '-preset', 'ultrafast',\n"
            "  '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out\n"
            "])\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

        rec_name = f"h3_esc_{os.getpid()}"
        valid_display = "localhost:98.0+10,20"
        pid = 0
        env = dict(os.environ)
        env["HOME"] = str(isolated_home)
        env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
        env["DISPLAY"] = ""
        try:
            res = subprocess.run(
                [str(RECORD_SH), valid_display, rec_name],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            pid = int(res.stdout.strip())
            out_mp4 = Path(Path(f"/tmp/swarm_record_{pid}.mp4.path").read_text(encoding="utf-8").strip())
            sidecar = json.loads(Path(f"{out_mp4}.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["display"], valid_display)
            self.assertEqual(sidecar["output"], str(out_mp4))
            self.assertIn('esc"quote\\home', sidecar["output"])
        finally:
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                for _ in range(30):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.05)
                    except ProcessLookupError:
                        break
                Path(f"/tmp/swarm_record_{pid}.mp4.path").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{rec_name}.outpath").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{rec_name}.pid").unlink(missing_ok=True)

    def test_n1_record_sh_strict_display_grammar_and_ambient_display_scrubbing(self) -> None:
        """N1/K9: Reject leading +x,y:N and malformed specs (rc=2); scrub DISPLAY/WAYLAND_DISPLAY for ffmpeg."""
        isolated_home = self.tmpdir / "n1_home"
        run_dir = self.tmpdir / "n1_run"
        mock_bin = self.tmpdir / "n1_mockbin"
        ffmpeg_log = self.tmpdir / "n1_ffmpeg_invocations.jsonl"
        isolated_home.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        mock_bin.mkdir(parents=True, exist_ok=True)

        shim = mock_bin / "ffmpeg"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            f"log_path = {str(ffmpeg_log)!r}\n"
            "entry = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'DISPLAY': os.environ.get('DISPLAY'),\n"
            "    'WAYLAND_DISPLAY': os.environ.get('WAYLAND_DISPLAY'),\n"
            "}\n"
            "with open(log_path, 'a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps(entry) + '\\n')\n"
            "time.sleep(0.4)\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

        env = dict(os.environ)
        env["HOME"] = str(isolated_home)
        env["SWARM_RUN_DIR"] = str(run_dir)
        env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
        env["DISPLAY"] = ":1"
        env["WAYLAND_DISPLAY"] = "wayland-99"

        rejected_cases = [
            (["+0,0:5"], f"n1_rej_1_{os.getpid()}"),
            (["+0,0:2"], f"n1_rej_2_{os.getpid()}"),
            (["+10,20:7.0"], f"n1_rej_3_{os.getpid()}"),
            (["+:5"], f"n1_rej_4_{os.getpid()}"),
            (["+0,0:0"], f"n1_rej_5_{os.getpid()}"),
            (["--allow-display0", "+0,0:0"], f"n1_rej_6_{os.getpid()}"),
            ([":١"], f"n1_rej_k9_{os.getpid()}"),
            ([":5 "], f"n1_rej_space_{os.getpid()}"),
            ([":5+0,0extra"], f"n1_rej_trail_{os.getpid()}"),
            (["a:b:5"], f"n1_rej_colons_{os.getpid()}"),
        ]
        for args, rec_name in rejected_cases:
            try:
                res = subprocess.run(
                    [str(RECORD_SH), *args, rec_name],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    res.returncode,
                    2,
                    msg=f"Expected rc=2 for {args}, got {res.returncode} (stderr={res.stderr!r})",
                )
                self.assertIn("REFUSED", res.stderr)
                self.assertFalse(ffmpeg_log.exists(), f"fake ffmpeg was invoked for {args}")
                self.assertFalse((isolated_home / "Vídeos" / "ViewPoint_hackathon").exists())
                self.assertFalse(Path(f"/tmp/swarm_record_{rec_name}.pid").exists())
                self.assertFalse(Path(f"/tmp/swarm_record_{rec_name}.outpath").exists())
                self.assertFalse((run_dir / f"swarm_record_{rec_name}.pid").exists())
                self.assertFalse((run_dir / f"swarm_record_{rec_name}.outpath").exists())
            finally:
                Path(f"/tmp/swarm_record_{rec_name}.pid").unlink(missing_ok=True)
                Path(f"/tmp/swarm_record_{rec_name}.outpath").unlink(missing_ok=True)

        # Positive control: :5+0,0 with ambient DISPLAY=:1 -> rc=0, exact arg passed, DISPLAY scrubbed
        pos_name = f"n1_pos_{os.getpid()}"
        pid = 0
        try:
            pos_res = subprocess.run(
                [str(RECORD_SH), ":5+0,0", pos_name],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(pos_res.returncode, 0, msg=f"positive case failed: {pos_res.stderr!r}")
            pid = int(pos_res.stdout.strip())
            self.assertTrue(ffmpeg_log.is_file())
            logged_lines = [json.loads(line) for line in ffmpeg_log.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(logged_lines), 1)
            entry = logged_lines[0]
            self.assertIn("-i", entry["argv"])
            idx_i = entry["argv"].index("-i")
            self.assertEqual(entry["argv"][idx_i + 1], ":5+0,0")
            self.assertIn(entry["DISPLAY"], (None, ""))
            self.assertIn(entry["WAYLAND_DISPLAY"], (None, ""))
        finally:
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for _ in range(30):
                    try:
                        os.kill(pid, 0)
                        time.sleep(0.05)
                    except ProcessLookupError:
                        break
                Path(f"/tmp/swarm_record_{pid}.mp4.path").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{pos_name}.outpath").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{pos_name}.pid").unlink(missing_ok=True)

    def test_k4_record_sh_cleans_up_pointer_files_on_immediate_ffmpeg_exit(self) -> None:
        """K4: Immediate ffmpeg exit removes PID and path pointer files for that take."""
        isolated_home = self.tmpdir / "k4_home"
        run_dir = self.tmpdir / "k4_run"
        mock_bin = self.tmpdir / "k4_mockbin"
        isolated_home.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        mock_bin.mkdir(parents=True, exist_ok=True)

        shim = mock_bin / "ffmpeg"
        shim.write_text("#!/usr/bin/env bash\necho 'x11grab open failed' >&2\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)

        rec_name = f"k4_fail_{os.getpid()}"
        env = dict(os.environ)
        env["HOME"] = str(isolated_home)
        env["SWARM_RUN_DIR"] = str(run_dir)
        env["PATH"] = f"{mock_bin}:{env.get('PATH', '')}"
        env["DISPLAY"] = ""

        tmp_mp4_ptrs_before = set(Path("/tmp").glob("swarm_record_*.mp4.path"))
        try:
            res = subprocess.run(
                [str(RECORD_SH), ":99", rec_name],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 1)
            self.assertFalse(Path(f"/tmp/swarm_record_{rec_name}.pid").exists())
            self.assertFalse(Path(f"/tmp/swarm_record_{rec_name}.outpath").exists())
            self.assertFalse((run_dir / f"swarm_record_{rec_name}.pid").exists())
            self.assertFalse((run_dir / f"swarm_record_{rec_name}.outpath").exists())
            self.assertEqual(set(Path("/tmp").glob("swarm_record_*.mp4.path")), tmp_mp4_ptrs_before)
            self.assertEqual(list(run_dir.glob("swarm_record_*.mp4.path")), [])
        finally:
            Path(f"/tmp/swarm_record_{rec_name}.pid").unlink(missing_ok=True)
            Path(f"/tmp/swarm_record_{rec_name}.outpath").unlink(missing_ok=True)

    def test_n2_sidecar_and_explicit_timeline_exclude_prior_run_state(self) -> None:
        """N2: Sidecar or explicit start excludes pre-window prior-run parks, queues, stop, verdicts, and cursors."""
        t_old = 1_731_580_000_000
        t_new = t_old + 600_000  # 10 minutes later

        records: List[Dict[str, Any]] = [
            # Earlier run leaving:
            # 1) camera-agent timeout-parked
            {
                "v": 1,
                "rec": "turn",
                "turn": 80,
                "turn_epoch": 80,
                "agent_id": "camera-agent",
                "head_frame": 12,
                "hold_ms": 60000,
                "ts_ms": t_old + 100,
            },
            {
                "v": 1,
                "rec": "close",
                "tick": 40,
                "turn": 80,
                "event_id": "camera-old-1",
                "agent_id": "camera-agent",
                "lane": "gui",
                "verdict": "TURN_TIMEOUT",
                "ts_ms": t_old + 200,
            },
            {
                "v": 1,
                "rec": "control",
                "op": "park",
                "by": "broker",
                "agent_id": "camera-agent",
                "event_id": "camera-old-1",
                "reason": "camera-agent: 2 consecutive TURN_TIMEOUT",
                "ts_ms": t_old + 250,
            },
            # 2) Two unclosed enqueues for props-agent
            {
                "v": 1,
                "rec": "enqueue",
                "event_id": "props-old-1",
                "agent_id": "props-agent",
                "lane": "script",
                "ts_ms": t_old + 300,
            },
            {
                "v": 1,
                "rec": "enqueue",
                "event_id": "props-old-2",
                "agent_id": "props-agent",
                "lane": "script",
                "ts_ms": t_old + 320,
            },
            # 3) Active acting cursor for look-agent away from its default slot
            {
                "v": 1,
                "rec": "cursor",
                "agent_id": "look-agent",
                "event_id": "look-old-1",
                "phase": "act",
                "x": 1400,
                "y": 850,
                "frame": 12,
                "label": "look-agent: old shader",
                "color": "#ffe66d",
                "ts_ms": t_old + 350,
            },
            # 4) Director stop control record
            {
                "v": 1,
                "rec": "control",
                "op": "stop",
                "by": "director-agent",
                "reason": "old run halted",
                "ts_ms": t_old + 400,
            },
            # Later run (>= 10 min later): only turns 1..3 for formerly parked camera-agent
            {
                "v": 1,
                "rec": "turn",
                "turn": 1,
                "turn_epoch": 1,
                "agent_id": "camera-agent",
                "head_frame": 1,
                "hold_ms": 60000,
                "ts_ms": t_new,
            },
            {
                "v": 1,
                "rec": "turn",
                "turn": 2,
                "turn_epoch": 2,
                "agent_id": "camera-agent",
                "head_frame": 2,
                "hold_ms": 60000,
                "ts_ms": t_new + 300,
            },
            {
                "v": 1,
                "rec": "turn",
                "turn": 3,
                "turn_epoch": 3,
                "agent_id": "camera-agent",
                "head_frame": 3,
                "hold_ms": 60000,
                "ts_ms": t_new + 600,
            },
        ]

        ev_path = self.tmpdir / "n2_events.jsonl"
        _write_events_jsonl(ev_path, records)
        events = load_events(ev_path, strict=False)

        vid = self.tmpdir / "n2_take.mp4"
        _synthesize_solid_mp4(vid, width=320, height=180, duration_s=1, fps=15)
        sidecar = Path(f"{vid}.meta.json")
        sidecar.write_text(json.dumps({"v": 1, "start_ts_ms": t_new}), encoding="utf-8")

        meta = VideoMeta(width=320, height=180, fps=15.0, duration_ms=1000, nb_frames=15)
        tl_start, tl_end, tl_source = resolve_timeline(vid, meta, events, timeline_mode="auto")
        self.assertEqual(tl_source, "sidecar")
        self.assertEqual(tl_start, t_new)

        for mode_label, start_arg in (("sidecar", tl_source), ("explicit", "explicit")):
            filtered = filter_events_for_timeline(events, tl_start, start_arg)
            tracker = BrokerTimelineTracker(filtered)

            for check_ts in (float(tl_start), float(tl_end)):
                tracker.advance_to(check_ts)
                self.assertEqual(tracker.parked_agents, set(), msg=f"{mode_label} leaked parked_agents")
                self.assertTrue(
                    all(len(q) == 0 for q in tracker.pending_fifo.values()),
                    msg=f"{mode_label} leaked pending_fifo: {tracker.pending_fifo}",
                )
                self.assertIsNone(tracker.control_state, msg=f"{mode_label} leaked control_state")
                self.assertIsNone(tracker.last_verdict_rec, msg=f"{mode_label} leaked earlier verdict")
                self.assertIsNone(tracker.last_pass_rec, msg=f"{mode_label} leaked earlier pass")
                self.assertGreaterEqual(tracker.turn_num, 1)
                self.assertLessEqual(tracker.turn_num, 3)

                look_cur = evaluate_agent_cursor(
                    agent_id="look-agent",
                    cursor_events=tracker.cursors_by_agent.get("look-agent") or (),
                    cursor_ts_list=tracker.cursor_ts_by_agent.get("look-agent") or (),
                    cur_ts_ms=check_ts,
                    lerp_ts_ms=250.0,
                    close_by_event=tracker.close_by_event,
                )
                self.assertEqual(look_cur.phase, "idle")
                self.assertEqual((look_cur.x, look_cur.y), AGENT_SPECS["look-agent"]["slot"])

        # End-to-end compose_video check with sidecar
        out_mp4 = self.tmpdir / "n2_composed.mp4"
        summary = compose_video(input_path=vid, events_path=ev_path, output_path=out_mp4)
        self.assertEqual(summary["timeline_source"], "sidecar")
        self.assertEqual(summary["events_loaded"], len(records))
        self.assertEqual(summary["events_in_window"], 3)
        self.assertEqual(summary["final_turn"], 3)
        self.assertEqual(summary["final_tick"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
