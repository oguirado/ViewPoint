"""Unit and negative-control tests for `swarm.broker.bridge` and `swarm.broker.broker`.

All tests run deterministically offline (`--vision-mode mock` / mocked `vision` runner)
inside `/tmp` or `/dev/shm/vqa-cu/swarm_test_*`, never touching `swarm/run/`,
display `:1`, or `/dev/shm/vqa-cu/hybrid/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from swarm.broker import bridge, broker


def _make_test_root() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="swarm_test_", dir="/tmp")


def _write_intent(intents_dir: Path, agent_id: str, payload: dict[str, Any]) -> Path:
    agent_dir = intents_dir / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    event_id = payload["event_id"]
    target = agent_dir / f"{event_id}.json"
    tmp = agent_dir / f".{event_id}.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _script_intent(
    event_id: str,
    agent_id: str,
    write_set: list[str],
    script: str = "print('ok')\n",
    *,
    based_on_frame: int = 0,
    lease_epoch: int = 1,
    created_ts_ms: int = 1000,
    lane: str = "script",
) -> dict[str, Any]:
    return {
        "v": 1,
        "event_id": event_id,
        "agent_id": agent_id,
        "lane": lane,
        "action_class": "state_job",
        "kind": "job",
        "based_on_frame": based_on_frame,
        "view_epoch": 0,
        "scene_epoch": 0,
        "lease_epoch": lease_epoch,
        "write_set": write_set,
        "target_region": None,
        "footprint": None,
        "expect": "object created",
        "script": script,
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "keys": None,
        "max_acts": 1,
        "cursor_xy": None,
        "created_ts_ms": created_ts_ms,
    }


def _gui_intent(
    event_id: str,
    agent_id: str = "camera-agent",
    *,
    based_on_frame: int = 0,
    lease_epoch: int = 1,
    keys: list[str] | None = None,
    footprint: list[int] | None = None,
    created_ts_ms: int = 1000,
) -> dict[str, Any]:
    k = keys if keys is not None else ["KP_1"]
    return {
        "v": 1,
        "event_id": event_id,
        "agent_id": agent_id,
        "lane": "gui",
        "action_class": "view_change",
        "kind": "key",
        "based_on_frame": based_on_frame,
        "view_epoch": 0,
        "scene_epoch": 0,
        "lease_epoch": lease_epoch,
        "write_set": [],
        "target_region": [0, 80, 1580, 900],
        "footprint": footprint,
        "expect": "viewport updated",
        "script": None,
        "script_sha256": None,
        "keys": k,
        "max_acts": len(k),
        "cursor_xy": [800, 500],
        "created_ts_ms": created_ts_ms,
    }


def _read_events(events_log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in events_log.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSwarmBrokerAndBridge(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = _make_test_root()
        self.root = Path(self._tmp.name)
        self.inbox = self.root / "inbox"
        self.outbox = self.root / "outbox"
        self.rejected = self.root / "rejected"
        self.frames = self.root / "frames"
        self.intents = self.root / "intents"
        self.events_log = self.root / "events.jsonl"
        self.nonce_file = self.root / "nonce.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_config(self, **overrides: Any) -> broker.BrokerConfig:
        kwargs: dict[str, Any] = {
            "job": "vpswarm_test",
            "vision_mode": "mock",
            "inbox_dir": self.inbox,
            "outbox_dir": self.outbox,
            "rejected_dir": self.rejected,
            "frames_dir": self.frames,
            "intents_dir": self.intents,
            "events_log": self.events_log,
            "nonce_file": self.nonce_file,
            "script_timeout_s": 2.0,
            "gui_timeout_s": 2.0,
            "poll_ms": 10,
            "drain_and_exit": True,
        }
        kwargs.update(overrides)
        return broker.BrokerConfig(**kwargs)

    def _bridge_hook(self, exec_log: list[str] | None = None):
        def _hook() -> None:
            def _exec(source: str, job_path: Path) -> None:
                if exec_log is not None:
                    exec_log.append(job_path.name)
                exec(compile(source, str(job_path), "exec"), {"__name__": "__main__", "bpy": None})

            bridge.process_inbox_once(
                inbox_dir=self.inbox,
                outbox_dir=self.outbox,
                rejected_dir=self.rejected,
                nonce_file=self.nonce_file,
                exec_fn=_exec,
                max_jobs=1,
            )

        return _hook

    def test_01_three_agent_fifo_rotation(self) -> None:
        """modeler, props, look enqueue scripts; broker rotates round-robin and advances tick 1..3."""
        exec_log: list[str] = []
        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook(exec_log))

        _write_intent(
            self.intents,
            "modeler-agent",
            _script_intent("modeler-1", "modeler-agent", ["col:CHAR_hero"], "print('hero')\n"),
        )
        _write_intent(
            self.intents,
            "props-agent",
            _script_intent("props-1", "props-agent", ["obj:PROP_chair"], "print('chair')\n"),
        )
        _write_intent(
            self.intents,
            "look-agent",
            _script_intent("look-1", "look-agent", ["mat:Skin"], "print('skin')\n"),
        )

        rc = sb.run()
        self.assertEqual(rc, 0)
        self.assertEqual(sb.tick, 3)
        self.assertEqual(sb.scene_epoch, 3)
        self.assertEqual(sb.head_frame, 3)
        self.assertEqual(len(exec_log), 3)

        events = _read_events(self.events_log)
        closes = [e for e in events if e["rec"] == "close"]
        verified = [c for c in closes if c["verdict"] == "STATE_VERIFIED"]
        passes = [c for c in closes if c["verdict"] == "PASS"]

        self.assertEqual(
            [(c["agent_id"], c["event_id"], c["tick"], c["verify_frame_ref"]) for c in verified],
            [
                ("modeler-agent", "modeler-1", 1, 1),
                ("props-agent", "props-1", 2, 2),
                ("look-agent", "look-1", 3, 3),
            ],
        )
        self.assertTrue(any(p["agent_id"] == "camera-agent" and p["event_id"] is None for p in passes))
        self.assertTrue((self.frames / "latest.json").is_file())
        latest = json.loads((self.frames / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["frame"], 3)
        self.assertEqual(latest["scene_epoch"], 3)

    def test_02_bridge_rejects_direct_or_wrong_nonce_writes(self) -> None:
        """Direct un-nonced writes and wrong-nonce files are moved to SWARM_REJECTED and never executed."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.rejected.mkdir(parents=True, exist_ok=True)
        self.nonce_file.write_text("0123456789abcdef\n", encoding="utf-8")

        executed_sources: list[str] = []

        def _spy_exec(source: str, job_path: Path) -> None:
            executed_sources.append(job_path.name)

        # Transient .tmp and hidden dotfiles must be ignored
        (self.inbox / ".hidden.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
        (self.inbox / "0123456789abcdef__1__staging.py.tmp").write_text("x = 1\n", encoding="utf-8")

        # Evil direct write and wrong-nonce writes
        (self.inbox / "evil_job.py").write_text("raise RuntimeError('evil')\n", encoding="utf-8")
        (self.inbox / "bad_ext.txt").write_text("not python\n", encoding="utf-8")
        (self.inbox / "deadbeefdeadbeef__1__ev1.py").write_text("raise RuntimeError('wrong nonce')\n", encoding="utf-8")

        # One valid file (seq=2) and one valid file (seq=1) to also verify seq sort
        (self.inbox / "0123456789abcdef__2__ev2.py").write_text("print('two')\n", encoding="utf-8")
        (self.inbox / "0123456789abcdef__1__ev1.py").write_text("print('one')\n", encoding="utf-8")

        old_env = os.environ.pop("SWARM_BRIDGE_NONCE", None)
        try:
            self.assertEqual(bridge.discover_nonce(self.nonce_file), "0123456789abcdef")
            res = bridge.process_inbox_once(
                inbox_dir=self.inbox,
                outbox_dir=self.outbox,
                rejected_dir=self.rejected,
                nonce_file=self.nonce_file,
                exec_fn=_spy_exec,
            )
            # Rotate nonce dynamically and verify discover_nonce picks up new value
            self.nonce_file.write_text("fedcba9876543210\n", encoding="utf-8")
            self.assertEqual(bridge.discover_nonce(self.nonce_file), "fedcba9876543210")
        finally:
            if old_env is not None:
                os.environ["SWARM_BRIDGE_NONCE"] = old_env

        self.assertEqual(executed_sources, ["0123456789abcdef__1__ev1.py", "0123456789abcdef__2__ev2.py"])
        self.assertEqual(len(res["executed"]), 2)
        self.assertCountEqual(res["rejected"], ["evil_job.py", "bad_ext.txt", "deadbeefdeadbeef__1__ev1.py"])

        for fname in ("evil_job.py", "bad_ext.txt", "deadbeefdeadbeef__1__ev1.py"):
            self.assertFalse((self.inbox / fname).exists())
            self.assertTrue((self.rejected / fname).is_file())

        self.assertTrue((self.inbox / ".hidden.py").is_file())
        self.assertTrue((self.inbox / "0123456789abcdef__1__staging.py.tmp").is_file())

        rej_logs = [
            json.loads(line)
            for line in (self.rejected / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        reasons = {entry["file"]: entry["reason"] for entry in rej_logs}
        self.assertEqual(reasons["evil_job.py"], "INVALID_NAME")
        self.assertEqual(reasons["bad_ext.txt"], "INVALID_NAME")
        self.assertEqual(reasons["deadbeefdeadbeef__1__ev1.py"], "NONCE_MISMATCH")

    def test_03_stale_frame_and_3_drop_park(self) -> None:
        """camera-agent GUI intent citing frame 0 after modeler advances to frame 1 is refused 3x then parked."""
        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())

        _write_intent(
            self.intents,
            "modeler-agent",
            _script_intent("mod-1", "modeler-agent", ["obj:CHAR_body"], based_on_frame=0),
        )
        _write_intent(
            self.intents,
            "camera-agent",
            _gui_intent("cam-stale-1", based_on_frame=0, keys=["KP_1"], footprint=[100, 100, 400, 400]),
        )

        rc = sb.run()
        self.assertEqual(rc, 0)
        self.assertEqual(sb.tick, 1)
        self.assertEqual(len(sb.fifos["camera-agent"]), 0)

        events = _read_events(self.events_log)
        stale_closes = [
            e
            for e in events
            if e["rec"] == "close" and e["event_id"] == "cam-stale-1" and e["verdict"] == "STALE_FRAME"
        ]
        self.assertEqual([c["drops"] for c in stale_closes], [1, 2, 3])
        self.assertEqual(stale_closes[-1]["reason"], "parked_after_3_stale_drops")

        park_controls = [e for e in events if e["rec"] == "control" and e["op"] == "park"]
        self.assertEqual(len(park_controls), 1)
        self.assertIn("cam-stale-1", park_controls[0]["reason"])

    def test_03b_gui_verified_spill_no_change_and_frame_evicted(self) -> None:
        """Verify GUI VERIFIED, VERIFIED_WITH_SPILL, NO_VISIBLE_CHANGE, FOOTPRINT_MISSING, and FRAME_EVICTED."""
        cfg = self._make_config(frame_ring_size=2)
        sb = broker.SwarmBroker(cfg)

        # 1. Valid mock GUI act -> VERIFIED inside footprint
        _write_intent(
            self.intents,
            "camera-agent",
            _gui_intent("cam-ok", based_on_frame=0, keys=["KP_1"], footprint=[200, 200, 300, 300]),
        )
        sb.run()
        self.assertEqual(sb.head_frame, 1)
        self.assertEqual(sb.view_epoch, 1)

        # 2. Missing footprint -> FOOTPRINT_MISSING
        _write_intent(
            self.intents,
            "camera-agent",
            _gui_intent("cam-nofp", based_on_frame=1, keys=["KP_3"], footprint=None),
        )
        sb.run()

        # 3. Advance frame twice to evict frame 0 from ring_size=2
        sb.capture_frame()
        sb.capture_frame()
        self.assertNotIn(0, sb.frame_ring)
        _write_intent(
            self.intents,
            "camera-agent",
            _gui_intent("cam-evicted", based_on_frame=0, keys=["KP_7"], footprint=[200, 200, 300, 300]),
        )
        sb.run()

        # 4. Mock vision runner returning spill region -> VERIFIED_WITH_SPILL
        def _spill_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
            if "--expect-change" in cmd and "none" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok":true}', stderr="")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"changed_regions": [[210, 210, 50, 50], [1400, 800, 40, 40]]}),
                stderr="",
            )

        sb_spill = broker.SwarmBroker(
            self._make_config(
                frames_dir=self.root / "frames_spill",
                events_log=self.root / "events_spill.jsonl",
                intents_dir=self.root / "intents_spill",
            ),
            vision_runner=_spill_runner,
        )
        _write_intent(
            self.root / "intents_spill",
            "camera-agent",
            _gui_intent("cam-spill", based_on_frame=0, keys=["KP_Add"], footprint=[200, 200, 300, 300]),
        )
        sb_spill.run()

        # 5. Mock vision runner returning rc=3 -> NO_VISIBLE_CHANGE
        def _no_change_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
            if "--expect-change" in cmd and "none" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok":true}', stderr="")
            return subprocess.CompletedProcess(cmd, 3, stdout="{}", stderr="")

        sb_nochg = broker.SwarmBroker(
            self._make_config(
                frames_dir=self.root / "frames_nc",
                events_log=self.root / "events_nc.jsonl",
                intents_dir=self.root / "intents_nc",
            ),
            vision_runner=_no_change_runner,
        )
        _write_intent(
            self.root / "intents_nc",
            "camera-agent",
            _gui_intent("cam-nc", based_on_frame=0, keys=["Home"], footprint=[200, 200, 300, 300]),
        )
        sb_nochg.run()

        closes_main = {
            e["event_id"]: e["verdict"]
            for e in _read_events(self.events_log)
            if e["rec"] == "close" and e["event_id"] is not None
        }
        self.assertEqual(closes_main["cam-ok"], "VERIFIED")
        self.assertEqual(closes_main["cam-nofp"], "FOOTPRINT_MISSING")
        self.assertEqual(closes_main["cam-evicted"], "FRAME_EVICTED")

        spill_closes = [
            e
            for e in _read_events(self.root / "events_spill.jsonl")
            if e["rec"] == "close" and e["event_id"] is not None
        ]
        self.assertEqual(spill_closes[0]["verdict"], "VERIFIED_WITH_SPILL")
        self.assertEqual(spill_closes[0]["own_regions"], [[210, 210, 50, 50]])
        self.assertEqual(spill_closes[0]["spill_regions"], [[1400, 800, 40, 40]])

        nc_closes = [
            e
            for e in _read_events(self.root / "events_nc.jsonl")
            if e["rec"] == "close" and e["event_id"] is not None
        ]
        self.assertEqual(nc_closes[0]["verdict"], "NO_VISIBLE_CHANGE")
        self.assertEqual(sb_nochg.view_epoch, 0)

    def test_04_claim_refused_prefix_and_gui_lane(self) -> None:
        """modeler-agent attempting mat:Gold or lane:gui is refused CLAIM_REFUSED without dispatch."""
        exec_log: list[str] = []
        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook(exec_log))

        _write_intent(
            self.intents,
            "modeler-agent",
            _script_intent("mod-bad-prefix", "modeler-agent", ["mat:Gold"], created_ts_ms=100),
        )
        _write_intent(
            self.intents,
            "modeler-agent",
            _gui_intent("mod-bad-gui", agent_id="modeler-agent", footprint=[0, 0, 100, 100], created_ts_ms=200),
        )
        _write_intent(
            self.intents,
            "camera-agent",
            _gui_intent(
                "cam-bad-key",
                agent_id="camera-agent",
                keys=["MouseLeft"],
                footprint=[0, 0, 100, 100],
                created_ts_ms=300,
            ),
        )

        sb.run()
        self.assertEqual(len(exec_log), 0)
        self.assertEqual(sb.tick, 0)

        closes = {
            e["event_id"]: e["verdict"]
            for e in _read_events(self.events_log)
            if e["rec"] == "close" and e["event_id"] is not None
        }
        self.assertEqual(closes["mod-bad-prefix"], "CLAIM_REFUSED")
        self.assertEqual(closes["mod-bad-gui"], "CLAIM_REFUSED")
        self.assertEqual(closes["cam-bad-key"], "CLAIM_REFUSED")

    def test_05_queue_full_and_duplicate_event(self) -> None:
        """9th queued intent returns QUEUE_FULL without overwriting first 8; duplicate returns DUPLICATE_EVENT."""
        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())

        for i in range(1, 9):
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent(f"mod-{i}", "modeler-agent", ["obj:CHAR_part"], created_ts_ms=100 + i),
            )
        # 9th intent -> QUEUE_FULL
        _write_intent(
            self.intents,
            "modeler-agent",
            _script_intent("mod-9", "modeler-agent", ["obj:CHAR_part"], created_ts_ms=200),
        )
        # Duplicate exact event_id "mod-2" in a differently named file -> DUPLICATE_EVENT
        dup_payload = _script_intent("mod-2", "modeler-agent", ["obj:CHAR_part"], created_ts_ms=202)
        dup_path = self.intents / "modeler-agent" / "mod-2-copy.json"
        dup_path.write_text(json.dumps(dup_payload), encoding="utf-8")

        sb.ingest_intents()
        self.assertEqual(len(sb.fifos["modeler-agent"]), 8)
        queued_ids = [item["event_id"] for item in sb.fifos["modeler-agent"]]
        self.assertEqual(queued_ids, [f"mod-{i}" for i in range(1, 9)])

        # Run remaining queued items
        sb.run()
        self.assertEqual(sb.tick, 8)  # mod-1..mod-8 (mod-9 and dup mod-2 refused at ingest)

        closes = [e for e in _read_events(self.events_log) if e["rec"] == "close" and e["event_id"] is not None]
        by_id_verdicts = [(c["event_id"], c["verdict"]) for c in closes]
        self.assertIn(("mod-9", "QUEUE_FULL"), by_id_verdicts)
        self.assertIn(("mod-2", "DUPLICATE_EVENT"), by_id_verdicts)
        self.assertIn(("mod-2", "STATE_VERIFIED"), by_id_verdicts)

    def test_06_lease_expired_turn_timeout_and_input_uncertain_reconcile(self) -> None:
        """Verify LEASE_EXPIRED, 2 consecutive TURN_TIMEOUT -> park, and INPUT_UNCERTAIN -> RECONCILE."""
        # 6a. LEASE_EXPIRED (stale lease_epoch and expired TTL with auto_renew_leases=False)
        cfg = self._make_config(lease_ttl_ms=100, auto_renew_leases=False)
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())
        _write_intent(
            self.intents,
            "modeler-agent",
            _script_intent("mod-stale-lease", "modeler-agent", ["obj:CHAR_a"], lease_epoch=99, created_ts_ms=10),
        )
        sb.run()

        # Expired TTL
        time.sleep(0.15)
        _write_intent(
            self.intents,
            "props-agent",
            _script_intent("props-expired-ttl", "props-agent", ["obj:PROP_a"], lease_epoch=1, created_ts_ms=20),
        )
        sb.run()
        sb.close()

        closes_lease = {
            e["event_id"]: e["verdict"]
            for e in _read_events(self.events_log)
            if e["rec"] == "close" and e["event_id"] is not None
        }
        self.assertEqual(closes_lease["mod-stale-lease"], "LEASE_EXPIRED")
        self.assertEqual(closes_lease["props-expired-ttl"], "LEASE_EXPIRED")

        # 6b. 2 consecutive TURN_TIMEOUT -> agent parked, 3rd intent not dispatched
        cfg_to = self._make_config(
            frames_dir=self.root / "frames_to",
            events_log=self.root / "events_to.jsonl",
            intents_dir=self.root / "intents_to",
            inbox_dir=self.root / "inbox_to",
            outbox_dir=self.root / "outbox_to",
            rejected_dir=self.root / "rejected_to",
            nonce_file=self.root / "nonce_to.txt",
            script_timeout_s=0.03,
        )
        sb_to = broker.SwarmBroker(cfg_to)
        for i in (1, 2, 3):
            _write_intent(
                cfg_to.intents_dir,
                "look-agent",
                _script_intent(f"look-to-{i}", "look-agent", ["mat:Slow"], created_ts_ms=100 + i),
            )
        sb_to.run()
        # First timeout enters RECONCILE; director reconciles before second attempt
        self.assertEqual(sb_to.queue_state, "RECONCILE")
        self.assertTrue(sb_to.actuator_busy)
        _write_intent(
            cfg_to.intents_dir,
            "director-agent",
            {"event_id": "dir-rec-to", "op": "reconcile", "by": "director-agent", "reason": "ok", "created_ts_ms": 200},
        )
        sb_to.run()
        sb_to.close()
        self.assertIn("look-agent", sb_to.parked_agents)
        # 3rd intent remains queued un-dispatched because agent is parked
        self.assertEqual(len(sb_to.fifos["look-agent"]), 1)
        events_to = _read_events(cfg_to.events_log)
        to_closes = [e for e in events_to if e["rec"] == "close" and e["verdict"] == "TURN_TIMEOUT"]
        self.assertEqual(len(to_closes), 2)
        self.assertTrue(any(e["rec"] == "control" and e["op"] == "park" for e in events_to))

        # 6c. INPUT_UNCERTAIN -> RECONCILE (halts further dispatches, no auto-repeat)
        def _uncertain_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
            if "--expect-change" in cmd and "none" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok":true}', stderr="")
            return subprocess.CompletedProcess(cmd, 6, stdout="{}", stderr="budget exceeded")

        cfg_rec = self._make_config(
            frames_dir=self.root / "frames_rec",
            events_log=self.root / "events_rec.jsonl",
            intents_dir=self.root / "intents_rec",
            inbox_dir=self.root / "inbox_rec",
            outbox_dir=self.root / "outbox_rec",
            rejected_dir=self.root / "rejected_rec",
            nonce_file=self.root / "nonce_rec.txt",
        )
        sb_rec = broker.SwarmBroker(cfg_rec, vision_runner=_uncertain_runner)
        sb_rec.ring_index = 3  # Start sweep at camera-agent
        _write_intent(
            cfg_rec.intents_dir,
            "camera-agent",
            _gui_intent("cam-uncertain", based_on_frame=0, keys=["KP_1"], footprint=[10, 10, 100, 100], created_ts_ms=1),
        )
        _write_intent(
            cfg_rec.intents_dir,
            "modeler-agent",
            _script_intent("mod-after-uncertain", "modeler-agent", ["obj:CHAR_x"], created_ts_ms=2),
        )
        sb_rec.run()
        self.assertEqual(sb_rec.queue_state, "RECONCILE")
        self.assertEqual(sb_rec.tick, 1)
        # modeler-agent's intent was NOT dispatched after RECONCILE entered
        self.assertEqual(len(sb_rec.fifos["modeler-agent"]), 1)

        rec_closes = [e for e in _read_events(cfg_rec.events_log) if e["rec"] == "close" and e["event_id"] is not None]
        self.assertEqual(len(rec_closes), 1)
        self.assertEqual(rec_closes[0]["verdict"], "INPUT_UNCERTAIN")

        # Director sends reconcile -> returns to IDLE and processes remaining intent
        _write_intent(
            cfg_rec.intents_dir,
            "director-agent",
            {"event_id": "dir-reconcile-1", "op": "reconcile", "by": "director-agent", "reason": "verified", "created_ts_ms": 3},
        )
        sb_rec._outbox_poll_hook = lambda: bridge.process_inbox_once(
            inbox_dir=cfg_rec.inbox_dir,
            outbox_dir=cfg_rec.outbox_dir,
            rejected_dir=cfg_rec.rejected_dir,
            nonce_file=cfg_rec.nonce_file,
            exec_fn=lambda src, p: None,
        )
        sb_rec.run()
        self.assertEqual(sb_rec.queue_state, "IDLE")
        self.assertEqual(sb_rec.tick, 2)

        # 6d. NO_LEASE when lease entry is absent
        sb_rec.leases.pop("props-agent")
        _write_intent(
            cfg_rec.intents_dir,
            "props-agent",
            _script_intent("props-no-lease", "props-agent", ["obj:PROP_z"], created_ts_ms=4),
        )
        sb_rec.run()
        nolease_closes = [
            e for e in _read_events(cfg_rec.events_log) if e["rec"] == "close" and e["event_id"] == "props-no-lease"
        ]
        self.assertEqual(nolease_closes[0]["verdict"], "NO_LEASE")

    def test_07_cli_drain_and_exit_with_background_bridge(self) -> None:
        """End-to-end CLI `broker.main(['--drain-and-exit', ...])` paired with a bridge worker thread."""
        _write_intent(
            self.intents,
            "modeler-agent",
            _script_intent("cli-mod-1", "modeler-agent", ["col:CHAR_cli"], "print('cli-ok')\n"),
        )

        stop_evt = threading.Event()

        def _bridge_loop() -> None:
            while not stop_evt.is_set():
                bridge.process_inbox_once(
                    inbox_dir=self.inbox,
                    outbox_dir=self.outbox,
                    rejected_dir=self.rejected,
                    nonce_file=self.nonce_file,
                    exec_fn=lambda src, p: exec(compile(src, str(p), "exec"), {"__name__": "__main__", "bpy": None}),
                )
                time.sleep(0.02)

        t = threading.Thread(target=_bridge_loop, daemon=True)
        t.start()
        try:
            rc = broker.main(
                [
                    "--vision-mode",
                    "mock",
                    "--inbox",
                    str(self.inbox),
                    "--outbox",
                    str(self.outbox),
                    "--rejected",
                    str(self.rejected),
                    "--frames-dir",
                    str(self.frames),
                    "--intents-dir",
                    str(self.intents),
                    "--events-log",
                    str(self.events_log),
                    "--nonce-file",
                    str(self.nonce_file),
                    "--poll-ms",
                    "10",
                    "--drain-and-exit",
                ]
            )
        finally:
            stop_evt.set()
            t.join(timeout=2.0)

        self.assertEqual(rc, 0)
        events = _read_events(self.events_log)
        verified = [e for e in events if e["rec"] == "close" and e["verdict"] == "STATE_VERIFIED"]
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["event_id"], "cli-mod-1")

    def test_08_b1_event_id_path_traversal_rejected(self) -> None:
        """B1: event_id with path traversal ('../../../victim') is refused without touching inbox or victim.py."""
        victim = self.root / "victim.py"
        victim.write_text("KEEP = True\n", encoding="utf-8")

        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())
        try:
            evil_payload = _script_intent("../../../victim", "modeler-agent", ["col:CHAR_x"], "PWNED = 1\n")
            evil_path = self.intents / "modeler-agent" / "evil.json"
            evil_path.parent.mkdir(parents=True, exist_ok=True)
            evil_path.write_text(json.dumps(evil_payload), encoding="utf-8")

            rc = sb.run()
            self.assertEqual(rc, 0)
            self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP = True\n")
            self.assertFalse(evil_path.exists())
            self.assertTrue((self.intents / "modeler-agent" / "rejected" / "evil.json").is_file())
            self.assertEqual(list(self.inbox.iterdir()), [])
            self.assertEqual(sb.tick, 0)

            closes = [e for e in _read_events(self.events_log) if e["rec"] == "close"]
            refusals = [c for c in closes if c["verdict"] == "CLAIM_REFUSED"]
            self.assertEqual(len(refusals), 1)
            self.assertIsNone(refusals[0]["event_id"])

            with self.assertRaises(ValueError):
                broker._safe_child_path(self.inbox, "../../../victim.py")
        finally:
            sb.close()

    def test_09_b2_poison_pill_intents_and_bad_outbox(self) -> None:
        """B2: malformed/non-dict/null-ts/missing-id intents are archived without crash loop; bad outbox -> RECONCILE."""
        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())
        try:
            mod_dir = self.intents / "modeler-agent"
            dir_dir = self.intents / "director-agent"
            (mod_dir / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
            (mod_dir / "corrupt.json").write_text("{bad json", encoding="utf-8")
            (mod_dir / "missing_id.json").write_text(
                json.dumps({"v": 1, "agent_id": "modeler-agent", "lane": "script", "created_ts_ms": 1}),
                encoding="utf-8",
            )
            null_ts = _script_intent("mod-null-ts", "modeler-agent", ["col:CHAR_a"])
            null_ts["created_ts_ms"] = None
            (mod_dir / "mod-null-ts.json").write_text(json.dumps(null_ts), encoding="utf-8")
            (dir_dir / "bad_dir.json").write_text(
                json.dumps({"op": "checkpoint", "by": "director-agent", "created_ts_ms": "not-a-num"}),
                encoding="utf-8",
            )
            (dir_dir / "list_dir.json").write_text("[]", encoding="utf-8")

            self.assertEqual(sb.run(), 0)
            # Second run must not crash on leftover poison pills
            self.assertEqual(sb.run(), 0)

            for leftover in ("list.json", "corrupt.json", "missing_id.json", "mod-null-ts.json"):
                self.assertFalse((mod_dir / leftover).exists())
            self.assertFalse((dir_dir / "bad_dir.json").exists())
            self.assertFalse((dir_dir / "list_dir.json").exists())

            closes = [e for e in _read_events(self.events_log) if e["rec"] == "close"]
            self.assertTrue(any(c["event_id"] == "mod-null-ts" and c["verdict"] == "STATE_VERIFIED" for c in closes))

            # Non-dict JSON outbox receipt -> OUTCOME_UNCERTAIN + RECONCILE (no AttributeError)
            def _bad_outbox_hook() -> None:
                for p in self.inbox.iterdir():
                    if p.suffix == ".py":
                        out_name = p.stem + ".json"
                        (self.outbox / out_name).write_text('["not","a","dict"]', encoding="utf-8")

            sb._outbox_poll_hook = _bad_outbox_hook
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("mod-bad-outbox", "modeler-agent", ["col:CHAR_b"]),
            )
            sb.run()
            self.assertEqual(sb.queue_state, "RECONCILE")
            closes = [e for e in _read_events(self.events_log) if e["rec"] == "close"]
            self.assertTrue(
                any(c["event_id"] == "mod-bad-outbox" and c["verdict"] == "OUTCOME_UNCERTAIN" for c in closes)
            )
        finally:
            sb.close()

    def test_10_m1_strips_untrusted_rebased_and_drops(self) -> None:
        """M1: broker strips forged 'rebased:true' and '_drops:-50' from intent JSON."""
        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())
        try:
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("mod-advance-1", "modeler-agent", ["col:CHAR_m1"], based_on_frame=0),
            )
            sb.run()
            self.assertEqual(sb.head_frame, 1)

            forged = _gui_intent("cam-forged", based_on_frame=0, keys=["KP_1"], footprint=[10, 10, 200, 200])
            forged["rebased"] = True
            forged["_drops"] = -50
            forged["drops"] = -50
            _write_intent(self.intents, "camera-agent", forged)

            sb.run()
            events = _read_events(self.events_log)
            stale_closes = [
                e
                for e in events
                if e["rec"] == "close" and e["event_id"] == "cam-forged" and e["verdict"] == "STALE_FRAME"
            ]
            self.assertEqual([c["drops"] for c in stale_closes], [1, 2, 3])
            self.assertFalse(any(e.get("event_id") == "cam-forged" and e.get("verdict") == "VERIFIED" for e in events))

            park_ctrls = [e for e in events if e["rec"] == "control" and e["op"] == "park"]
            self.assertEqual(len(park_ctrls), 1)
            self.assertEqual(park_ctrls[0]["agent_id"], "camera-agent")
            self.assertEqual(park_ctrls[0]["event_id"], "cam-forged")
        finally:
            sb.close()

    def test_11_m2_script_timeout_reconcile_and_actuator_busy(self) -> None:
        """M2: script TURN_TIMEOUT sets actuator_busy & RECONCILE, advances tick, preserves job, enforces ACTUATOR_BUSY."""
        cfg = self._make_config(script_timeout_s=0.03)
        sb = broker.SwarmBroker(cfg)
        try:
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("mod-hang", "modeler-agent", ["col:CHAR_hang"], created_ts_ms=10),
            )
            sb.run()

            self.assertEqual(sb.queue_state, "RECONCILE")
            self.assertTrue(sb.actuator_busy)
            self.assertEqual(sb.tick, 1)
            # Job file must NOT be unlinked on timeout
            job_files = list(self.inbox.glob("*__1__mod-hang.py"))
            self.assertEqual(len(job_files), 1)

            # Director sends 'resume' without 'reconcile'/'checkpoint' -> actuator_busy still True -> ACTUATOR_BUSY
            _write_intent(
                self.intents,
                "director-agent",
                {"event_id": "dir-resume-only", "op": "resume", "by": "director-agent", "reason": "premature", "created_ts_ms": 20},
            )
            _write_intent(
                self.intents,
                "props-agent",
                _script_intent("props-while-busy", "props-agent", ["obj:PROP_b"], created_ts_ms=30),
            )
            sb.run()

            events = _read_events(self.events_log)
            busy_closes = [
                e for e in events if e["rec"] == "close" and e["event_id"] == "props-while-busy"
            ]
            self.assertEqual(len(busy_closes), 1)
            self.assertEqual(busy_closes[0]["verdict"], "ACTUATOR_BUSY")
            self.assertEqual(sb.tick, 1)  # tick unchanged on ACTUATOR_BUSY

            # Now checkpoint clears actuator_busy and next dispatch uses seq=2 (not 1!)
            sb._outbox_poll_hook = self._bridge_hook()
            _write_intent(
                self.intents,
                "director-agent",
                {"event_id": "dir-ckpt", "op": "checkpoint", "by": "director-agent", "reason": "restored", "created_ts_ms": 40},
            )
            _write_intent(
                self.intents,
                "props-agent",
                _script_intent("props-after-ckpt", "props-agent", ["obj:PROP_c"], created_ts_ms=50),
            )
            sb.run()
            self.assertFalse(sb.actuator_busy)
            self.assertEqual(sb.tick, 2)
            events = _read_events(self.events_log)
            self.assertTrue(
                any(
                    e["rec"] == "close"
                    and e["event_id"] == "props-after-ckpt"
                    and e["verdict"] == "STATE_VERIFIED"
                    and e["tick"] == 2
                    for e in events
                )
            )
        finally:
            sb.close()

    def test_12_m3_mid_key_gui_timeout_enters_reconcile(self) -> None:
        """M3: mid-sequence GUI timeout captures post-timeout frame, bumps tick/turn_epoch, and enters RECONCILE."""
        def _mid_timeout_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
            if "--expect-change" in cmd and "none" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok":true}', stderr="")
            if "KP_1" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"changed_regions": [[100, 100, 50, 50]]}), stderr=""
                )
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_s)

        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, vision_runner=_mid_timeout_runner)
        try:
            _write_intent(
                self.intents,
                "camera-agent",
                _gui_intent("cam-mid-to", based_on_frame=0, keys=["KP_1", "KP_3"], footprint=[50, 50, 300, 300]),
            )
            sb.run()

            self.assertEqual(sb.queue_state, "RECONCILE")
            self.assertEqual(sb.tick, 1)
            self.assertGreaterEqual(sb.head_frame, 1)

            closes = [e for e in _read_events(self.events_log) if e["rec"] == "close" and e["event_id"] == "cam-mid-to"]
            self.assertEqual(len(closes), 1)
            self.assertEqual(closes[0]["verdict"], "TURN_TIMEOUT")
            self.assertEqual(closes[0]["reason"], "gui_timeout_reconcile_required")
            self.assertIsNotNone(closes[0]["verify_frame_ref"])
        finally:
            sb.close()

    def test_13_m4_mixed_keys_and_hover_rc3(self) -> None:
        """M4 & m2: hover rc=3 accepted; KP_1 rc=0 + KP_3 rc=3 -> VERIFIED & view_epoch=1; all rc=3 -> NO_VISIBLE_CHANGE."""
        def _mixed_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
            if "--expect-change" in cmd and "none" in cmd:
                return subprocess.CompletedProcess(cmd, 3, stdout="{}", stderr="")
            if "KP_1" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"changed_regions": [[210, 210, 50, 50]]}), stderr=""
                )
            return subprocess.CompletedProcess(cmd, 3, stdout="{}", stderr="")

        cfg = self._make_config()
        sb = broker.SwarmBroker(cfg, vision_runner=_mixed_runner)
        try:
            _write_intent(
                self.intents,
                "camera-agent",
                _gui_intent("cam-mixed", based_on_frame=0, keys=["KP_1", "KP_3"], footprint=[200, 200, 300, 300]),
            )
            sb.run()
            self.assertEqual(sb.queue_state, "IDLE")
            self.assertEqual(sb.view_epoch, 1)
            closes = [e for e in _read_events(self.events_log) if e["rec"] == "close" and e["event_id"] == "cam-mixed"]
            self.assertEqual(closes[0]["verdict"], "VERIFIED")
        finally:
            sb.close()

        # All keys rc=3 -> NO_VISIBLE_CHANGE
        def _all_rc3_runner(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 3, stdout="{}", stderr="")

        sb2 = broker.SwarmBroker(
            self._make_config(
                frames_dir=self.root / "frames_rc3",
                events_log=self.root / "events_rc3.jsonl",
                intents_dir=self.root / "intents_rc3",
                nonce_file=self.root / "nonce_rc3.txt",
            ),
            vision_runner=_all_rc3_runner,
        )
        try:
            _write_intent(
                sb2.cfg.intents_dir,
                "camera-agent",
                _gui_intent("cam-all-rc3", based_on_frame=0, keys=["KP_1", "KP_3"], footprint=[200, 200, 300, 300]),
            )
            sb2.run()
            self.assertEqual(sb2.view_epoch, 0)
            closes2 = [
                e for e in _read_events(sb2.cfg.events_log) if e["rec"] == "close" and e["event_id"] == "cam-all-rc3"
            ]
            self.assertEqual(closes2[0]["verdict"], "NO_VISIBLE_CHANGE")
        finally:
            sb2.close()

    def test_14_m5_auto_lease_renewal_and_explicit_expire(self) -> None:
        """M5: unparked agent leases auto-renew within final 25% of TTL keeping lease_epoch; expire_lease forces LEASE_EXPIRED."""
        cfg = self._make_config(lease_ttl_ms=120, auto_renew_leases=True)
        sb = broker.SwarmBroker(cfg, outbox_poll_hook=self._bridge_hook())
        try:
            time.sleep(0.10)  # >= 90ms (120 - 120//4)
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("mod-renewed", "modeler-agent", ["col:CHAR_r"], lease_epoch=1),
            )
            sb.run()

            events = _read_events(self.events_log)
            claims = [e for e in events if e["rec"] == "claim" and e["agent_id"] == "modeler-agent"]
            self.assertGreaterEqual(len(claims), 2)
            self.assertEqual(claims[-1]["lease_epoch"], 1)
            self.assertGreater(claims[-1]["expires_ts_ms"], claims[0]["expires_ts_ms"])
            self.assertTrue(
                any(e["rec"] == "close" and e["event_id"] == "mod-renewed" and e["verdict"] == "STATE_VERIFIED" for e in events)
            )

            # Explicit expire_lease makes LEASE_EXPIRED testable even with auto_renew_leases=True
            sb.expire_lease("props-agent")
            _write_intent(
                self.intents,
                "props-agent",
                _script_intent("props-exp", "props-agent", ["obj:PROP_exp"], lease_epoch=1),
            )
            sb.run()
            events = _read_events(self.events_log)
            self.assertTrue(
                any(e["rec"] == "close" and e["event_id"] == "props-exp" and e["verdict"] == "LEASE_EXPIRED" for e in events)
            )
        finally:
            sb.close()

    def test_15_m6_m8_and_interop_polish(self) -> None:
        """M6/M8/m1/m3/m4/m5: env overrides, nonce validation, flock, capture_frame guard, idle exit rc, FIFO/control polish."""
        # 1. SWARM_BRIDGE_NONCE rejects file paths
        old_nonce = os.environ.get("SWARM_BRIDGE_NONCE")
        os.environ["SWARM_BRIDGE_NONCE"] = "/dev/shm/vqa-cu/swarm/nonce.txt"
        try:
            with self.assertRaises(ValueError):
                bridge.discover_nonce(self.nonce_file)
            with self.assertRaises(ValueError):
                broker.SwarmBroker(self._make_config())
        finally:
            if old_nonce is None:
                os.environ.pop("SWARM_BRIDGE_NONCE", None)
            else:
                os.environ["SWARM_BRIDGE_NONCE"] = old_nonce

        # 2. SWARM_FRAMES_DIR and SWARM_INTENTS_DIR env vars
        old_frames = os.environ.get("SWARM_FRAMES_DIR")
        old_intents = os.environ.get("SWARM_INTENTS_DIR")
        os.environ["SWARM_FRAMES_DIR"] = str(self.root / "env_frames")
        os.environ["SWARM_INTENTS_DIR"] = str(self.root / "env_intents")
        try:
            env_cfg = broker.BrokerConfig()
            self.assertEqual(env_cfg.frames_dir, self.root / "env_frames")
            self.assertEqual(env_cfg.intents_dir, self.root / "env_intents")
        finally:
            if old_frames is None:
                os.environ.pop("SWARM_FRAMES_DIR", None)
            else:
                os.environ["SWARM_FRAMES_DIR"] = old_frames
            if old_intents is None:
                os.environ.pop("SWARM_INTENTS_DIR", None)
            else:
                os.environ["SWARM_INTENTS_DIR"] = old_intents

        # 3. Exclusive flock prevents two concurrent brokers on same events_log
        sb1 = broker.SwarmBroker(self._make_config())
        try:
            with self.assertRaises(RuntimeError):
                broker.SwarmBroker(self._make_config())

            # 4. m3/m4: QUEUE_FULL does not burn event_id and neither QUEUE_FULL nor DUPLICATE_EVENT emits enqueue
            for i in range(1, 9):
                _write_intent(
                    self.intents,
                    "modeler-agent",
                    _script_intent(f"q-{i}", "modeler-agent", ["col:CHAR_q"], created_ts_ms=10 + i),
                )
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("q-9", "modeler-agent", ["col:CHAR_q"], created_ts_ms=20),
            )
            dup = _script_intent("q-1", "modeler-agent", ["col:CHAR_q"], created_ts_ms=21)
            (self.intents / "modeler-agent" / "q-1-dup.json").write_text(json.dumps(dup), encoding="utf-8")
            sb1.ingest_intents()
            events = _read_events(self.events_log)
            enqueues = [e for e in events if e["rec"] == "enqueue"]
            self.assertEqual(len(enqueues), 8)

            sb1._outbox_poll_hook = self._bridge_hook()
            sb1.run()
            # Re-submit q-9 after FIFO drained -> must succeed, NOT DUPLICATE_EVENT!
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("q-9", "modeler-agent", ["col:CHAR_q"], created_ts_ms=30),
            )
            sb1.run()
            events = _read_events(self.events_log)
            q9_closes = [e for e in events if e["rec"] == "close" and e["event_id"] == "q-9"]
            self.assertEqual([c["verdict"] for c in q9_closes], ["QUEUE_FULL", "STATE_VERIFIED"])

            # 5. m5: invalid op ignored; reconcile cannot unfreeze FROZEN; only resume exits FROZEN bumping epochs
            _write_intent(
                self.intents,
                "director-agent",
                {"event_id": "dir-bogus", "op": "bogus", "by": "director-agent", "reason": "bad", "created_ts_ms": 40},
            )
            _write_intent(
                self.intents,
                "director-agent",
                {"event_id": "dir-stop", "op": "stop", "by": "director-agent", "reason": "freeze", "created_ts_ms": 41},
            )
            _write_intent(
                self.intents,
                "director-agent",
                {"event_id": "dir-rec", "op": "reconcile", "by": "director-agent", "reason": "try_unfreeze", "created_ts_ms": 42},
            )
            sb1.ingest_intents()
            sb1.apply_control_events()
            self.assertEqual(sb1.queue_state, "FROZEN")
            events = _read_events(self.events_log)
            self.assertFalse(any(e["rec"] == "control" and e["op"] == "bogus" for e in events))

            prev_view = sb1.view_epoch
            prev_scene = sb1.scene_epoch
            _write_intent(
                self.intents,
                "director-agent",
                {"event_id": "dir-resume", "op": "resume", "by": "director-agent", "reason": "unfreeze", "created_ts_ms": 43},
            )
            sb1.ingest_intents()
            sb1.apply_control_events()
            self.assertEqual(sb1.queue_state, "IDLE")
            self.assertEqual(sb1.view_epoch, prev_view + 1)
            self.assertEqual(sb1.scene_epoch, prev_scene + 1)
        finally:
            sb1.close()

        # 6. M8 + idle_timeout_s exit code 2 from RECONCILE
        sb_cap = broker.SwarmBroker(
            self._make_config(
                inbox_dir=self.root / "inbox_cap",
                outbox_dir=self.root / "outbox_cap",
                rejected_dir=self.root / "rejected_cap",
                frames_dir=self.root / "frames_cap",
                events_log=self.root / "events_cap.jsonl",
                intents_dir=self.root / "intents_cap",
                nonce_file=self.root / "nonce_cap.txt",
                drain_and_exit=False,
                idle_timeout_s=0.05,
                poll_ms=10,
            ),
            outbox_poll_hook=lambda: bridge.process_inbox_once(
                inbox_dir=self.root / "inbox_cap",
                outbox_dir=self.root / "outbox_cap",
                rejected_dir=self.root / "rejected_cap",
                nonce_file=self.root / "nonce_cap.txt",
                exec_fn=lambda src, p: None,
                max_jobs=1,
            ),
        )
        try:
            def _fail_capture(*args: Any, **kwargs: Any) -> int:
                raise RuntimeError("transient vision look failure")

            sb_cap.capture_frame = _fail_capture  # type: ignore[method-assign]
            _write_intent(
                sb_cap.cfg.intents_dir,
                "modeler-agent",
                _script_intent("mod-cap-fail", "modeler-agent", ["col:CHAR_cap"]),
            )
            rc = sb_cap.run()
            self.assertEqual(rc, 2)
            self.assertEqual(sb_cap.queue_state, "RECONCILE")
            cap_closes = [
                e for e in _read_events(sb_cap.cfg.events_log) if e["rec"] == "close" and e["event_id"] == "mod-cap-fail"
            ]
            self.assertEqual(len(cap_closes), 1)
            self.assertEqual(cap_closes[0]["verdict"], "OUTCOME_UNCERTAIN")
        finally:
            sb_cap.close()

    def test_16_l1_bridge_base_exception_containment_and_guaranteed_removal(self) -> None:
        """L1: sys.exit/SystemExit/KeyboardInterrupt/exit() in script never wedges bridge or kills timer."""
        nonce = "0123456789abcdef"
        cases = [
            ("sys_exit_0", "import sys\nsys.exit(0)\n", "SystemExit"),
            ("raise_system_exit_3", "raise SystemExit(3)\n", "SystemExit"),
            ("raise_keyboard_interrupt", "raise KeyboardInterrupt()\n", "KeyboardInterrupt"),
            ("builtin_exit", "exit()\n", "SystemExit"),
        ]

        for label, term_stmt, expected_exc_name in cases:
            with self.subTest(style=label):
                sub_root = self.root / f"l1_{label}"
                inbox = sub_root / "inbox"
                outbox = sub_root / "outbox"
                rejected = sub_root / "rejected"
                nonce_file = sub_root / "nonce.txt"
                marker = sub_root / "marker.txt"

                inbox.mkdir(parents=True, exist_ok=True)
                outbox.mkdir(parents=True, exist_ok=True)
                rejected.mkdir(parents=True, exist_ok=True)
                nonce_file.write_text(nonce + "\n", encoding="utf-8")

                evil_src = (
                    f"with open({str(marker)!r}, 'a', encoding='utf-8') as _f:\n"
                    f"    _f.write('X')\n"
                    f"{term_stmt}"
                )
                next_src = "ok_value = 42\n"
                (inbox / f"{nonce}__1__evil.py").write_text(evil_src, encoding="utf-8")
                (inbox / f"{nonce}__2__next.py").write_text(next_src, encoding="utf-8")

                env_keys = ("SWARM_BRIDGE_NONCE", "SWARM_INBOX", "SWARM_OUTBOX", "SWARM_REJECTED", "SWARM_NONCE_FILE")
                saved_env = {k: os.environ.get(k) for k in env_keys}
                os.environ.pop("SWARM_BRIDGE_NONCE", None)
                os.environ["SWARM_INBOX"] = str(inbox)
                os.environ["SWARM_OUTBOX"] = str(outbox)
                os.environ["SWARM_REJECTED"] = str(rejected)
                os.environ["SWARM_NONCE_FILE"] = str(nonce_file)
                try:
                    # Scan 1: executes evil (seq 1) using real default executor, unlinks it, writes ERROR receipt
                    res1 = bridge.process_inbox_once(
                        inbox_dir=inbox,
                        outbox_dir=outbox,
                        rejected_dir=rejected,
                        nonce_file=nonce_file,
                        max_jobs=1,
                    )
                    self.assertEqual(len(res1["executed"]), 1)
                    self.assertFalse((inbox / f"{nonce}__1__evil.py").exists())
                    evil_receipt_path = outbox / f"{nonce}__1__evil.json"
                    self.assertTrue(evil_receipt_path.is_file())
                    evil_receipt = json.loads(evil_receipt_path.read_text(encoding="utf-8"))
                    self.assertEqual(evil_receipt["seq"], 1)
                    self.assertEqual(evil_receipt["event_id"], "evil")
                    self.assertEqual(evil_receipt["status"], "ERROR")
                    self.assertIn(expected_exc_name, evil_receipt["error"] or "")

                    # Scan 2: executes next (seq 2), unlinks it, writes OK receipt
                    res2 = bridge.process_inbox_once(
                        inbox_dir=inbox,
                        outbox_dir=outbox,
                        rejected_dir=rejected,
                        nonce_file=nonce_file,
                        max_jobs=1,
                    )
                    self.assertEqual(len(res2["executed"]), 1)
                    self.assertFalse((inbox / f"{nonce}__2__next.py").exists())
                    next_receipt_path = outbox / f"{nonce}__2__next.json"
                    self.assertTrue(next_receipt_path.is_file())
                    next_receipt = json.loads(next_receipt_path.read_text(encoding="utf-8"))
                    self.assertEqual(next_receipt["seq"], 2)
                    self.assertEqual(next_receipt["event_id"], "next")
                    self.assertEqual(next_receipt["status"], "OK")

                    # Scan 3: inbox empty, nothing executed
                    res3 = bridge.process_inbox_once(
                        inbox_dir=inbox,
                        outbox_dir=outbox,
                        rejected_dir=rejected,
                        nonce_file=nonce_file,
                        max_jobs=1,
                    )
                    self.assertEqual(res3["executed"], [])

                    # Timer callback: must also handle BaseException and return normal polling interval
                    (inbox / f"{nonce}__3__timer_evil.py").write_text(term_stmt, encoding="utf-8")
                    interval = bridge._timer_tick()
                    self.assertIsInstance(interval, float)
                    self.assertEqual(interval, bridge.POLL_INTERVAL_S)
                    self.assertFalse((inbox / f"{nonce}__3__timer_evil.py").exists())
                    self.assertTrue((outbox / f"{nonce}__3__timer_evil.json").is_file())

                    self.assertEqual(marker.read_text(encoding="utf-8"), "X")
                    self.assertEqual(list(inbox.iterdir()), [])
                finally:
                    for k, v in saved_env.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v

    def test_17_l1_companion_broker_with_real_default_bridge(self) -> None:
        """L1 companion: broker in mock mode with real default bridge handles sys.exit(0) then valid script."""
        def _real_bridge_hook() -> None:
            bridge.process_inbox_once(
                inbox_dir=self.inbox,
                outbox_dir=self.outbox,
                rejected_dir=self.rejected,
                nonce_file=self.nonce_file,
                max_jobs=1,
            )

        sb = broker.SwarmBroker(self._make_config(), outbox_poll_hook=_real_bridge_hook)
        try:
            _write_intent(
                self.intents,
                "modeler-agent",
                _script_intent("mod-sys-exit", "modeler-agent", ["col:CHAR_exit"], "import sys\nsys.exit(0)\n"),
            )
            _write_intent(
                self.intents,
                "props-agent",
                _script_intent("props-ok", "props-agent", ["obj:PROP_ok"], "ok = 1 + 1\n"),
            )

            rc = sb.run()
            self.assertEqual(rc, 0)
            self.assertEqual(sb.queue_state, "IDLE")
            self.assertEqual(list(self.inbox.iterdir()), [])

            closes = [
                e for e in _read_events(self.events_log) if e["rec"] == "close" and e["event_id"] is not None
            ]
            self.assertEqual(
                [(c["event_id"], c["verdict"]) for c in closes],
                [("mod-sys-exit", "JOB_ERROR"), ("props-ok", "STATE_VERIFIED")],
            )
        finally:
            sb.close()

    def test_18_b1_b2_b4_b7_regex_surrogate_and_newline_hardening(self) -> None:
        """B1, B2, B4, B7: strict fullmatch event_id/job filename, lone surrogates, and EOF newline repair."""
        # B1: event_id="ok\n" -> CLAIM_REFUSED, queue IDLE, nothing in inbox or bridge rejected/
        sb_b1 = broker.SwarmBroker(self._make_config())
        try:
            bad_id_intent = _script_intent("ok", "modeler-agent", ["col:CHAR_b1"])
            bad_id_intent["event_id"] = "ok\n"
            (self.intents / "modeler-agent" / "bad_nl.json").write_text(
                json.dumps(bad_id_intent), encoding="utf-8"
            )
            rc = sb_b1.run()
            self.assertEqual(rc, 0)
            self.assertEqual(sb_b1.queue_state, "IDLE")
            self.assertEqual(list(self.inbox.iterdir()), [])
            self.assertEqual(list(self.rejected.iterdir()), [])
            self.assertTrue((self.intents / "modeler-agent" / "rejected" / "bad_nl.json").is_file())

            closes = [e for e in _read_events(self.events_log) if e["rec"] == "close"]
            self.assertTrue(
                any(
                    c["verdict"] == "CLAIM_REFUSED"
                    and c["reason"] == "missing or invalid event_id"
                    and c["event_id"] is None
                    for c in closes
                )
            )
        finally:
            sb_b1.close()

        # B2: lone surrogate "\ud800" in kind, expect, and script -> zero UnicodeEncodeError, CLAIM_REFUSED, IDLE
        for idx, field in enumerate(("kind", "expect", "script"), start=1):
            with self.subTest(surrogate_field=field):
                sub = self.root / f"b2_{field}"
                sb_b2 = broker.SwarmBroker(
                    self._make_config(
                        inbox_dir=sub / "inbox",
                        outbox_dir=sub / "outbox",
                        rejected_dir=sub / "rejected",
                        frames_dir=sub / "frames",
                        intents_dir=sub / "intents",
                        events_log=sub / "events.jsonl",
                        nonce_file=sub / "nonce.txt",
                    )
                )
                try:
                    ev_id = f"surrogate-{idx}"
                    payload = _script_intent(ev_id, "modeler-agent", ["col:CHAR_b2"])
                    if field == "script":
                        payload["script"] = "x = '\ud800'\n"
                        payload["script_sha256"] = None
                    else:
                        payload[field] = "\ud800"
                    intent_path = sub / "intents" / "modeler-agent" / f"{ev_id}.json"
                    intent_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

                    rc = sb_b2.run()
                    self.assertEqual(rc, 0)
                    self.assertEqual(sb_b2.queue_state, "IDLE")
                    raw_log = (sub / "events.jsonl").read_bytes()
                    raw_log.decode("ascii")
                    closes = [
                        e
                        for e in _read_events(sub / "events.jsonl")
                        if e["rec"] == "close" and e["event_id"] == ev_id
                    ]
                    self.assertEqual(len(closes), 1)
                    self.assertEqual(closes[0]["verdict"], "CLAIM_REFUSED")
                finally:
                    sb_b2.close()

        # B7: bridge rejects trailing newline and leading-zero sequence numbers like 010 as INVALID_NAME
        b7_root = self.root / "b7"
        b7_inbox = b7_root / "inbox"
        b7_outbox = b7_root / "outbox"
        b7_rejected = b7_root / "rejected"
        b7_nonce = b7_root / "nonce.txt"
        b7_inbox.mkdir(parents=True, exist_ok=True)
        b7_outbox.mkdir(parents=True, exist_ok=True)
        b7_rejected.mkdir(parents=True, exist_ok=True)
        nonce = "0123456789abcdef"
        b7_nonce.write_text(nonce + "\n", encoding="utf-8")

        executed_b7: list[str] = []
        (b7_inbox / f"{nonce}__1__nl.py\n").write_text("x = 1\n", encoding="utf-8")
        (b7_inbox / f"{nonce}__010__leadzero.py").write_text("x = 2\n", encoding="utf-8")
        old_env_nonce = os.environ.pop("SWARM_BRIDGE_NONCE", None)
        try:
            res_b7 = bridge.process_inbox_once(
                inbox_dir=b7_inbox,
                outbox_dir=b7_outbox,
                rejected_dir=b7_rejected,
                nonce_file=b7_nonce,
                exec_fn=lambda src, p: executed_b7.append(p.name),
            )
        finally:
            if old_env_nonce is not None:
                os.environ["SWARM_BRIDGE_NONCE"] = old_env_nonce

        self.assertEqual(executed_b7, [])
        self.assertEqual(res_b7["executed"], [])
        self.assertCountEqual(res_b7["rejected"], [f"{nonce}__1__nl.py\n", f"{nonce}__010__leadzero.py"])
        rej_entries = [
            json.loads(line)
            for line in (b7_rejected / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(all(r["reason"] == "INVALID_NAME" for r in rej_entries))

        # B4: pre-seeded torn line without trailing newline does not swallow next broker's claim record
        b4_root = self.root / "b4"
        b4_log = b4_root / "events.jsonl"
        b4_root.mkdir(parents=True, exist_ok=True)
        b4_log.write_bytes(b'{"v":1,"rec":"torn"')
        sb_b4 = broker.SwarmBroker(
            self._make_config(
                inbox_dir=b4_root / "inbox",
                outbox_dir=b4_root / "outbox",
                rejected_dir=b4_root / "rejected",
                frames_dir=b4_root / "frames",
                intents_dir=b4_root / "intents",
                events_log=b4_log,
                nonce_file=b4_root / "nonce.txt",
            )
        )
        sb_b4.close()
        parsed_claims: list[dict[str, Any]] = []
        for line in b4_log.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("rec") == "claim":
                parsed_claims.append(obj)
        self.assertEqual(len(parsed_claims), 4)


if __name__ == "__main__":
    unittest.main()
